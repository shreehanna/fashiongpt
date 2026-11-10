"""Real style files for muses, so Ella dresses people the way the muse actually dresses, not the way the model
imagines they might.

The model that writes looks only knows what it read before its training, and for many muses (new names, newer
celebrities, people famous in one country) it guesses, then states its guesses as facts ("her consistent love of
YSL"). So for each muse, once a month, the server looks them up:
  1. Google (through Serper, which Shop links already use): articles about what they wear, and photos of them;
  2. Claude reads only that evidence (the photos too) and writes a short style file: silhouettes, colours, the
     labels they've really been seen in, hair and makeup, and what they never wear;
  3. the file is kept in el_kv for 30 days and added to every look for that muse.
The app asks for the file as soon as someone picks a muse, so it's usually ready before they finish the next steps.
"""
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests as req
from flask import Blueprint, jsonify, request

from pro import client_ip, env, kv_get, kv_set, now, owner_request

bp = Blueprint('muses', __name__)
TTL = 30 * 86400            # rebuild a muse's file once a month (style changes)
EMPTY_TTL = 3 * 86400       # nothing found: try again in 3 days
_ai = {}
_events = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4)


def init(client, model):
    _ai.update(client=client, model=model)


# Muses the model may not know by this name (a recent name change) or may mix up with someone else.
_LECLERC = ("Alexandra Leclerc is Alexandra Saint Mleux: she took the name Leclerc when she married Formula 1 driver "
            "Charles Leclerc in Monaco in 2026. She is the French-Italian content creator and paddock 'It girl' "
            "you may know as Alexandra Saint Mleux. She is NOT Alessandra Ambrosio (a different person, a Brazilian model).")
KNOWN = {
    'alexandra leclerc': {'aka': ['Alexandra Saint Mleux'], 'who': _LECLERC},
    'alexandra saint mleux': {'aka': ['Alexandra Leclerc'], 'who': _LECLERC},
    'alexandra saint-mleux': {'aka': ['Alexandra Leclerc'], 'who': _LECLERC},
    'alex leclerc': {'aka': ['Alexandra Saint Mleux'], 'who': _LECLERC},
}


def norm(name):
    return ' '.join(str(name or '').lower().split())[:60]


def usable(name):
    n = norm(name)
    return len(n) >= 3 and n not in ('no specific muse', 'none', 'no muse')


def muse_identity(name):
    return (KNOWN.get(norm(name)) or {}).get('who', '')


# ── Looking them up ───────────────────────────────────────────────────────────
def _serper(path, payload):
    try:
        r = req.post('https://google.serper.dev/' + path, json=payload, timeout=8,
                     headers={'X-API-KEY': env('SERPER_API_KEY'), 'Content-Type': 'application/json'})
    except Exception as e:
        print('[muses] search failed:', e)
        return {}
    try:
        from alerts import search_result
        search_result(r)
    except Exception:
        pass
    try:
        return r.json() if r.status_code == 200 else {}
    except ValueError:
        return {}


def _evidence(name):
    """Text snippets and photo thumbnails about what this person wears."""
    known = KNOWN.get(norm(name)) or {}
    names = [name.strip()] + [a for a in known.get('aka', []) if norm(a) != norm(name)]
    jobs = [('search', {'q': f'{names[0]} outfit style', 'num': 10}),
            ('search', {'q': f'{names[0]} fashion designer wore', 'num': 10}),
            ('images', {'q': f'{names[-1]} outfit', 'num': 10})]
    if len(names) > 1:
        jobs.append(('search', {'q': f'{names[1]} outfit style', 'num': 10}))
    results = list(_pool.map(lambda j: _serper(*j), jobs))
    snippets, seen = [], set()
    for res in results:
        for row in (res.get('organic') or []) + (res.get('news') or []):
            text = ' '.join(f"{row.get('title', '')}: {row.get('snippet', '')}".split())
            if text and text not in seen:
                seen.add(text)
                snippets.append(text[:400])
        kg = res.get('knowledgeGraph') or {}
        if kg.get('description') and kg['description'] not in seen:
            seen.add(kg['description'])
            snippets.append(f"About {kg.get('title', name)}: {kg['description'][:400]}")
    photos = []
    for res in results:
        for img in res.get('images') or []:
            url = img.get('thumbnailUrl') or ''
            if url.startswith('https://') and 'gstatic.com' in url and url not in photos:
                photos.append(url)
    return snippets[:36], photos[:6]


def _write_file(name, snippets, photos):
    who = muse_identity(name)
    prompt = (
        f"You are a fashion researcher. Below are Google results{' and photos' if photos else ''} about {name}."
        + (f" {who}" if who else '') + f"\n\nWrite a STYLE FILE for a stylist who must dress clients exactly like {name}. "
        "Use ONLY what these results and photos show. Ignore anything about other people with a similar name. "
        "Don't add labels, events or outfits that aren't in the evidence; if something isn't shown, write 'not shown'.\n\n"
        "Plain text, under 1,000 characters, exactly these lines:\n"
        "Who: (one line)\nEveryday and off-duty:\nEvents and evenings:\nSilhouettes and fits:\nColours:\n"
        "Labels they've actually been seen in:\nShoes, bags and jewellery:\nHair and makeup:\nNever (what they don't wear):\n\n"
        "If the evidence is mostly about someone else, or says nothing about their clothes, reply with exactly: NONE\n\n"
        "Google results:\n" + '\n'.join(f'- {s}' for s in snippets))
    client, model = _ai.get('client'), _ai.get('model')
    content = ([{'type': 'image', 'source': {'type': 'url', 'url': u}} for u in photos] +
               [{'type': 'text', 'text': prompt}]) if photos else prompt
    try:
        msg = client.messages.create(model=model, max_tokens=700, messages=[{'role': 'user', 'content': content}])
    except Exception as e:
        from alerts import ServiceDown
        if isinstance(e, ServiceDown) or not photos:
            raise
        print('[muses] photos could not be used, writing from text only:', repr(e)[:160])
        msg = client.messages.create(model=model, max_tokens=700, messages=[{'role': 'user', 'content': prompt}])
    text = (msg.content[0].text or '').strip()
    return None if not text or text.upper().startswith('NONE') else text[:1400]


def _build(key, name):
    text = None
    try:
        snippets, photos = _evidence(name)
        if len(snippets) >= 3 or photos:
            text = _write_file(name, snippets, photos)
        kv_set('muse:' + key, json.dumps({'at': now(), 'name': name.strip()[:60], 'text': text}, ensure_ascii=False))
        print(f'[muses] style file for {name!r}: {"ready" if text else "nothing found"}')
    except Exception as e:
        print(f'[muses] style file for {name!r} failed: {e!r}')
    finally:
        with _lock:
            ev = _events.pop(key, None)
        if ev:
            ev.set()
    return text


def _cached(key):
    try:
        hit = json.loads(kv_get('muse:' + key) or 'null')
    except Exception:
        return None, False
    if not isinstance(hit, dict):
        return None, False
    age = now() - int(hit.get('at') or 0)
    fresh = age < (TTL if hit.get('text') else EMPTY_TTL)
    return hit.get('text'), fresh


def ready():
    return bool(env('SERPER_API_KEY') and _ai.get('client'))


def prepare(name):
    """Start building this muse's file in the background, unless it's cached or already being built."""
    if not usable(name) or not ready():
        return False
    key = norm(name)
    _, fresh = _cached(key)
    if fresh:
        return False
    with _lock:
        if key in _events:
            return True
        _events[key] = threading.Event()
    threading.Thread(target=_build, args=(key, name), daemon=True).start()
    return True


def style_file(name, wait=14):
    """This muse's style file, waiting up to `wait` seconds if it's being built right now."""
    if not usable(name) or not ready():
        return None
    key = norm(name)
    text, fresh = _cached(key)
    if fresh:
        return text
    prepare(name)
    with _lock:
        ev = _events.get(key)
    if ev:
        ev.wait(wait)
    text, _ = _cached(key)
    return text


# ── Routes ────────────────────────────────────────────────────────────────────
_hits = {}


@bp.post('/api/muse/prepare')
def prepare_route():
    """The muse screen calls this as soon as someone picks a muse."""
    name = str((request.get_json(silent=True) or {}).get('name') or '')[:60]
    ip, t = client_ip(), time.time()
    q = _hits.setdefault(ip, deque())
    while q and t - q[0] > 600:
        q.popleft()
    if len(q) >= 20 and not owner_request():
        return jsonify({'ok': False}), 429
    q.append(t)
    if len(_hits) > 5000:
        _hits.clear()
    return jsonify({'ok': True, 'building': prepare(name)})


@bp.route('/api/owner/muse', methods=['GET', 'POST'])
def owner_muse():
    """Owner tools: read (or rebuild) the style file Ella uses for a muse."""
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    name = str(body.get('name') or request.args.get('name') or '')[:60]
    if not usable(name):
        return jsonify({'error': 'Type a muse’s name.'}), 400
    if not ready():
        return jsonify({'error': 'Style files need SERPER_API_KEY in Render.'}), 503
    key = norm(name)
    if request.method == 'POST' and body.get('rebuild'):
        kv_set('muse:' + key, json.dumps({'at': 0, 'name': name, 'text': None}))
    text = style_file(name, wait=40)
    return jsonify({'name': name, 'text': text, 'identity': muse_identity(name)})
