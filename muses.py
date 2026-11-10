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

It also keeps a bank of REAL OUTFITS the muse has actually worn (read from the articles themselves: the event, the
date and every piece). A look for that muse is then a recreation of the real outfit that best suits the occasion,
not something Ella imagines in their style.
"""
import html as htmllib
import ipaddress
import json
import re
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests as req
from flask import Blueprint, jsonify, request

from pro import client_ip, env, kv_get, kv_set, now, owner_request

bp = Blueprint('muses', __name__)
TTL = 30 * 86400            # rebuild a muse's file once a month (style changes)
EMPTY_TTL = 3 * 86400       # nothing found: try again in 3 days
_ai = {}
_events = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8)     # searches and article downloads
_jobs = ThreadPoolExecutor(max_workers=4)     # the two Claude reads (they use _pool themselves, so they get their own)


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
            ('search', {'q': f'{names[0]} wore dress designer', 'num': 10}),
            ('news', {'q': f'{names[0]} outfit', 'num': 10}),
            ('images', {'q': f'{names[-1]} outfit', 'num': 10})]
    if len(names) > 1:
        jobs.append(('search', {'q': f'{names[1]} outfit wore', 'num': 10}))
    results = list(_pool.map(lambda j: _serper(*j), jobs))
    snippets, seen, links = [], set(), []
    for res in results:
        for row in (res.get('organic') or []) + (res.get('news') or []):
            link = row.get('link') or ''
            if link and link not in [l for l, _ in links]:
                links.append((link, row.get('title') or ''))
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
    return snippets[:40], photos[:6], links


# ── Real outfits, read from the articles ──────────────────────────────────────
SKIP_HOSTS = ('instagram.com', 'tiktok.com', 'pinterest.', 'youtube.com', 'youtu.be', 'facebook.com', 'x.com',
              'twitter.com', 'reddit.com', 'threads.net', 'wikipedia.org')


def _public_https(url):
    """Only public web pages: no internal addresses (the server never fetches anything else)."""
    try:
        u = urlparse(url)
        if u.scheme not in ('https', 'http') or not u.hostname or u.port not in (None, 80, 443):
            return False
        if any(h in u.hostname for h in SKIP_HOSTS):
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


_TAGS = re.compile(r'<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>', re.S | re.I)
_ANY_TAG = re.compile(r'<[^>]+>')
_WORE = re.compile(r'\b(wore|wearing|wears|dressed|outfit|look|paired|styled|gown|dress|skirt|jacket|blazer|heels|bag|'
                   r'trousers|jeans|top|coat|set|suit)\b', re.I)


def _article_text(url, names):
    """The parts of an article that talk about what this person wore (at most ~5,000 characters)."""
    if not _public_https(url):
        return ''
    try:
        r = req.get(url, timeout=6, headers={'User-Agent': 'Mozilla/5.0 (E&L stylist research)'}, stream=True)
        if r.status_code != 200 or 'html' not in (r.headers.get('Content-Type') or ''):
            return ''
        raw = r.raw.read(1_500_000, decode_content=True).decode(r.encoding or 'utf-8', 'replace')
    except Exception:
        return ''
    body = _ANY_TAG.sub('\n', _TAGS.sub(' ', raw))
    lines = [' '.join(htmllib.unescape(l).split()) for l in body.split('\n')]
    first = [n.split()[0].lower() for n in names if n.split()]
    keep = []
    for l in lines:
        if len(l) < 40:
            continue
        low = l.lower()
        if _WORE.search(l) and (any(f in low for f in first) or ' she ' in f' {low} ' or ' he ' in f' {low} '):
            keep.append(l)
    return '\n'.join(keep)[:5000]


def _extract_outfits(name, articles):
    who = muse_identity(name)
    numbered = '\n\n'.join(f'[{i + 1}] {title}\n{text}' for i, (url, title, text) in enumerate(articles))
    prompt = (
        f"Below are fashion articles about {name}." + (f" {who}" if who else '') +
        f"\n\nList every SPECIFIC outfit these articles say {name} actually wore. Only outfits the text really describes "
        f"(a full dress, or at least two pieces), never guesses, never outfits worn by someone else. Keep brands exactly as "
        f"the articles name them; if no brand is named, leave it empty.\n\n"
        'Return ONLY valid JSON: {"outfits": [{"event": "where or what for, e.g. Australian Grand Prix paddock", '
        '"date": "month and year if given, else \'\'", "formality": "one of: casual, smart, evening, black-tie, sport, beach, ethnic", '
        '"pieces": [{"category": "e.g. Jacket", "item": "colour, fabric and cut, e.g. pink boucle tweed cropped jacket", '
        '"brand": "brand or \'\'"}], "hair_makeup": "if described, else \'\'", "article": 1}]}\n'
        'If the articles describe no specific outfit, return {"outfits": []}.\n\nArticles:\n' + numbered)
    msg = _ai['client'].messages.create(model=_ai['model'], max_tokens=3000, messages=[{'role': 'user', 'content': prompt}])
    t = (msg.content[0].text or '').strip()
    if '```' in t:
        t = t.split('```json')[1] if '```json' in t else t.split('```')[1]
        t = t.split('```')[0]
    try:
        data = json.loads(t.strip())
    except ValueError:
        m = re.search(r'\{.*\}', t, re.S)
        data = json.loads(m.group(0)) if m else {}
    out = []
    for o in (data.get('outfits') or [])[:24]:
        if not isinstance(o, dict):
            continue
        pieces = [{'category': str(p.get('category') or '')[:40], 'item': str(p.get('item') or '')[:140],
                   'brand': str(p.get('brand') or '')[:60]}
                  for p in (o.get('pieces') or [])[:10] if isinstance(p, dict) and p.get('item')]
        if not pieces:
            continue
        try:
            src = articles[int(o.get('article') or 0) - 1]
        except (ValueError, IndexError):
            src = ('', '', '')
        out.append({'id': f'o{len(out) + 1}', 'event': str(o.get('event') or '')[:120], 'date': str(o.get('date') or '')[:40],
                    'formality': str(o.get('formality') or '')[:20], 'pieces': pieces,
                    'hair_makeup': str(o.get('hair_makeup') or '')[:200], 'url': src[0][:300], 'source': src[1][:120]})
    return out


def _outfit_bank(name, links):
    known = KNOWN.get(norm(name)) or {}
    names = [name.strip()] + known.get('aka', [])
    picked = [(u, t) for u, t in links if not any(h in u for h in SKIP_HOSTS)][:8]
    texts = list(_pool.map(lambda lt: _article_text(lt[0], names), picked))
    articles = [(u, t, x) for (u, t), x in zip(picked, texts) if len(x) > 80][:6]
    if not articles:
        return []
    return _extract_outfits(name, articles)


def outfit_line(o):
    pieces = ' + '.join(f"{p['item']}" + (f" ({p['brand']})" if p['brand'] else '') for p in o['pieces'])
    when = ', '.join(x for x in (o['event'], o['date']) if x)
    return f"[{o['id']}] {when} ({o['formality'] or 'occasion not given'}): {pieces}" + (
        f". Hair and makeup: {o['hair_makeup']}" if o.get('hair_makeup') else '')


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
    text, outfits = None, []
    try:
        snippets, photos, links = _evidence(name)
        jobs = []
        if len(snippets) >= 3 or photos:
            jobs.append(_jobs.submit(_write_file, name, snippets, photos))
        bank = _jobs.submit(_outfit_bank, name, links) if links else None
        if jobs:
            text = jobs[0].result()
        if bank:
            try:
                outfits = bank.result()
            except Exception as e:
                print(f'[muses] outfits for {name!r} failed: {e!r}')
        kv_set('muse:' + key, json.dumps({'at': now(), 'name': name.strip()[:60], 'text': text, 'outfits': outfits},
                                         ensure_ascii=False))
        print(f'[muses] {name!r}: style file {"ready" if text else "not found"}, {len(outfits)} real outfits')
    except Exception as e:
        print(f'[muses] style file for {name!r} failed: {e!r}')
    finally:
        with _lock:
            ev = _events.pop(key, None)
        if ev:
            ev.set()
    return text


def _record(key):
    try:
        hit = json.loads(kv_get('muse:' + key) or 'null')
    except Exception:
        return None, False
    if not isinstance(hit, dict):
        return None, False
    age = now() - int(hit.get('at') or 0)
    return hit, age < (TTL if (hit.get('text') or hit.get('outfits')) else EMPTY_TTL)


def _cached(key):
    hit, fresh = _record(key)
    return (hit or {}).get('text'), fresh


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


def muse_data(name, wait=25):
    """{'text': style file, 'outfits': real outfits}, waiting up to `wait` seconds if it's being built right now."""
    if not usable(name) or not ready():
        return {}
    key = norm(name)
    hit, fresh = _record(key)
    if not fresh:
        prepare(name)
        with _lock:
            ev = _events.get(key)
        if ev:
            ev.wait(wait)
        hit, _ = _record(key)
    hit = hit or {}
    return {'text': hit.get('text'), 'outfits': hit.get('outfits') or []}


def style_file(name, wait=25):
    return muse_data(name, wait).get('text')


# Which real outfits this person has already been given, so asking again gives a different one
def used(who, name):
    from growth import hash_who
    try:
        return json.loads(kv_get(f'muse_used:{hash_who(who)}:{norm(name)}') or '[]')
    except Exception:
        return []


def mark_used(who, name, outfit_id):
    from growth import hash_who
    ids = [i for i in used(who, name) if i != outfit_id] + [outfit_id]
    kv_set(f'muse_used:{hash_who(who)}:{norm(name)}', json.dumps(ids[-30:]))


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
    d = muse_data(name, wait=60)
    return jsonify({'name': name, 'text': d.get('text'), 'outfits': d.get('outfits') or [], 'identity': muse_identity(name)})
