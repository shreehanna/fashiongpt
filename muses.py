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
date and every piece, with each piece's exact product name when the coverage gives one, like Zimmermann's "Carousel
Metallic Twist Gown"), plus the other real pieces they've been seen in (bags, shoes, jewellery). A look for that muse
is then the real outfit that best suits the occasion, worn exactly as they wore it, not something Ella imagines.
A product name is only kept if it's written in the coverage itself, so Ella can't make one up.
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
V = 2                       # record format; bump to rebuild every muse (2: exact product names, her other real pieces)
TTL = 30 * 86400            # rebuild a muse's file once a month (style changes)
EMPTY_TTL = 3 * 86400       # nothing found: try again in 3 days
_ai = {}
_events = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8)     # searches and article downloads
_jobs = ThreadPoolExecutor(max_workers=4)     # the two Claude reads (they use _pool themselves, so they get their own)
_reads = ThreadPoolExecutor(max_workers=6)    # reading the articles, a few at a time, all at once


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
    """Text snippets, photo thumbnails and article links about what this person wears."""
    known = KNOWN.get(norm(name)) or {}
    names = [name.strip()] + [a for a in known.get('aka', []) if norm(a) != norm(name)]
    jobs = [('search', {'q': f'{names[0]} outfit style', 'num': 10}),
            ('search', {'q': f'{names[0]} wore dress designer', 'num': 10}),
            ('search', {'q': f'{names[0]} gown red carpet gala', 'num': 10}),
            ('search', {'q': f'{names[0]} outfit details brand shop the look', 'num': 10}),
            ('news', {'q': f'{names[0]} outfit', 'num': 10}),
            ('images', {'q': f'{names[-1]} outfit', 'num': 10})]
    if len(names) > 1:
        jobs += [('search', {'q': f'{names[1]} outfit wore', 'num': 10}),
                 ('search', {'q': f'{names[1]} gown dress designer', 'num': 10})]
    results = list(_pool.map(lambda j: _serper(*j), jobs))
    snippets, seen, per_query = [], set(), []
    for res in results:
        mine = []
        for row in (res.get('organic') or []) + (res.get('news') or []):
            link = row.get('link') or ''
            if link:
                mine.append((link, row.get('title') or ''))
            text = ' '.join(f"{row.get('title', '')}: {row.get('snippet', '')}".split())
            if text and text not in seen:
                seen.add(text)
                snippets.append(text[:400])
        per_query.append(mine)
        kg = res.get('knowledgeGraph') or {}
        if kg.get('description') and kg['description'] not in seen:
            seen.add(kg['description'])
            snippets.append(f"About {kg.get('title', name)}: {kg['description'][:400]}")
    links = []              # the top result of every search first, then the second of each, and so on
    for k in range(10):
        for mine in per_query:
            if k < len(mine) and mine[k][0] not in [l for l, _ in links]:
                links.append(mine[k])
    photos = []
    for res in results:
        for img in res.get('images') or []:
            url = img.get('thumbnailUrl') or ''
            if url.startswith('https://') and 'gstatic.com' in url and url not in photos:
                photos.append(url)
    return snippets[:60], photos[:6], links


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
_WORE = re.compile(r'\b(wore|wearing|wears|dressed|outfit|look|paired|styled|carried|carrying|accessori[sz]ed|finished|'
                   r'gown|dress|skirt|jacket|blazer|heels|bag|clutch|tote|trousers|jeans|top|coat|set|suit|co-ord|'
                   r'shoes|sandals|pumps|boots|loafers|flats|sneakers|earrings|necklace|bracelet|ring|jewel(?:le)?ry|'
                   r'sunglasses|shades|cardigan|knit|shirt|blouse|corset|minidress|maxi|mini|midi)s?\b', re.I)


_HIM_HER = re.compile(r'\b(she|her|he|his)\b', re.I)


def _article_text(url, names):
    """The parts of a page that talk about what this person wore (at most ~7,000 characters). On a page that's
    about them (their name all over it, like a "what she wore" site), product lines count too
    ("Zimmermann Sundance Frill Maxi Dress"), since that's where exact product names are."""
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
    whole = ' '.join(lines).lower()
    about = sum(whole.count(f) for f in first) >= 3
    keep, seen = [], set()
    for l in lines:
        if len(l) < 20 or l in seen or not _WORE.search(l):
            continue
        low = l.lower()
        mentions = any(f in low for f in first) or _HIM_HER.search(low)
        if (mentions and len(l) >= 40) or (about and len(l) <= 160):
            seen.add(l)
            keep.append(l)
    return '\n'.join(keep)[:7000]


def _plain(text):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', htmllib.unescape(str(text or '')).lower()).split())


def _json(text):
    t = (text or '').strip()
    if '```' in t:
        t = t.split('```json')[1] if '```json' in t else t.split('```')[1]
        t = t.split('```')[0]
    try:
        return json.loads(t.strip())
    except ValueError:
        m = re.search(r'\{.*\}', t, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            return {}


_QUOTES = '"\'\u201c\u201d\u2018\u2019 '


def _piece(p, source):
    """One piece, cleaned. A product name is kept only if it's written in the source itself (never made up)."""
    name = str(p.get('name') or '').strip(_QUOTES)[:90]
    if name and _plain(name) not in source:
        name = ''
    return {'category': str(p.get('category') or '')[:40], 'item': str(p.get('item') or '')[:140],
            'brand': str(p.get('brand') or '')[:60], 'name': name}


def _read_chunk(name, chunk):
    """Real outfits (and single pieces) from a few articles. chunk: [(url, title, text)]."""
    who = muse_identity(name)
    numbered = '\n\n'.join(f'[{i + 1}] {title}\n{text}' for i, (url, title, text) in enumerate(chunk))
    prompt = (
        f"Below are fashion articles about {name}." + (f" {who}" if who else '') +
        f"\n\nList every SPECIFIC outfit these articles say {name} actually wore. Only outfits the text really describes "
        f"(a full dress, or at least two pieces), never guesses, never outfits worn by someone else, never \"shop similar\" "
        f"or \"get the look for less\" suggestions. For every piece:\n"
        f"- \"name\": its EXACT product name if the text gives one (like \"Carousel Metallic Twist Gown\", \"Sundance Frill "
        f"Maxi Dress\", \"Kelly 20\"), copied word for word, without the brand. If the text doesn't name the product, leave "
        f"it empty. Never make one up.\n"
        f"- \"brand\": exactly as the text names it, or empty.\n"
        f"- \"item\": colour, fabric and cut in plain words, like \"silver metallic twist-front gown\".\n"
        f"Under \"extras\", also list single pieces the text says {name} wore that aren't part of an outfit above "
        f"(bags, shoes, jewellery, sunglasses), with the same fields plus the event.\n\n"
        'Return ONLY valid JSON: {"outfits": [{"event": "where or what for, e.g. Australian Grand Prix paddock", '
        '"date": "month and year if given, else \'\'", "formality": "one of: casual, smart, evening, black-tie, sport, beach, ethnic", '
        '"pieces": [{"category": "e.g. Dress", "name": "exact product name or \'\'", "brand": "brand or \'\'", '
        '"item": "colour, fabric and cut"}], "hair_makeup": "if described, else \'\'", "article": 1}], '
        '"extras": [{"category": "e.g. Bag", "name": "", "brand": "", "item": "", "event": "", "article": 1}]}\n'
        'If the articles describe nothing specific, return {"outfits": [], "extras": []}.\n\nArticles:\n' + numbered)
    msg = _ai['client'].messages.create(model=_ai['model'], max_tokens=3000, messages=[{'role': 'user', 'content': prompt}])
    data = _json(msg.content[0].text)
    source = _plain(' '.join(f'{t} {x}' for _, t, x in chunk))

    def src(o):
        try:
            k = int(o.get('article') or 0) - 1
        except (ValueError, TypeError):
            return ('', '', '')
        return chunk[k] if 0 <= k < len(chunk) else ('', '', '')
    outfits, extras = [], []
    for o in (data.get('outfits') or [])[:24]:
        if not isinstance(o, dict):
            continue
        pieces = [_piece(p, source) for p in (o.get('pieces') or [])[:10] if isinstance(p, dict) and p.get('item')]
        if not pieces:
            continue
        u, t, _ = src(o)
        outfits.append({'event': str(o.get('event') or '')[:120], 'date': str(o.get('date') or '')[:40],
                        'formality': str(o.get('formality') or '')[:20], 'pieces': pieces,
                        'hair_makeup': str(o.get('hair_makeup') or '')[:200], 'url': u[:300], 'source': t[:120]})
    for e in (data.get('extras') or [])[:30]:
        if isinstance(e, dict) and e.get('item'):
            extras.append(dict(_piece(e, source), event=str(e.get('event') or '')[:120]))
    return outfits, extras


def _same(a, b):
    """The same outfit, written up by two different articles."""
    if _plain(a['event'])[:40] != _plain(b['event'])[:40] or _plain(a['date']) != _plain(b['date']):
        return False
    ca, cb = {_plain(p['category']) for p in a['pieces']}, {_plain(p['category']) for p in b['pieces']}
    return len(ca & cb) * 2 >= max(len(ca), len(cb))


def _named(o):
    return sum(1 for p in o['pieces'] if p['name'])


def _extract_outfits(name, articles, snippets=()):
    """Every article is read (two at a time, all at once), then the results merged: the same outfit from two
    articles is kept once, with every exact product name either article gave."""
    chunks = [articles[i:i + 2] for i in range(0, len(articles), 2)]
    if snippets:
        chunks.append([('', 'Google results', '\n'.join(snippets[:40]))])
    reads = [_reads.submit(_read_chunk, name, c) for c in chunks]
    outfits, extras = [], []
    for f in reads:
        try:
            o, e = f.result()
        except Exception as ex:
            print(f'[muses] an article read failed: {ex!r}')
            continue
        outfits += o
        extras += e
    merged = []
    for o in outfits:
        twin = next((m for m in merged if _same(m, o)), None)
        if not twin:
            merged.append(o)
            continue
        keep, other = (o, twin) if (_named(o), bool(o['url'])) > (_named(twin), bool(twin['url'])) else (twin, o)
        for p in keep['pieces']:
            if not p['name']:
                q = next((q for q in other['pieces'] if q['name'] and _plain(q['brand']) == _plain(p['brand'])
                          and _plain(q['category']) == _plain(p['category'])), None)
                if q:
                    p['name'] = q['name']
        merged[merged.index(twin)] = keep
    merged = merged[:24]
    for i, o in enumerate(merged):
        o['id'] = f'o{i + 1}'
    seen, pieces = set(), []
    for e in extras:
        k = _plain(e['brand'] + ' ' + (e['name'] or e['item']))
        if k not in seen:
            seen.add(k)
            pieces.append(e)
    return merged, pieces[:30]


_MAIN = re.compile(r'dress|gown|jacket|blazer|coat|skirt|suit|top|shirt|trouser|jean|set', re.I)


def _find_names(name, outfits):
    """Pieces with a brand but no product name ("a silver Zimmermann gown"): one search each. A name is kept only
    if one result clearly says they wore it, or is that brand's product in that colour and cut, AND the name is
    written in that result, with the brand, and it's the same kind of thing (a gown, not a bag)."""
    todo = [(o, p) for o in outfits for p in o['pieces'] if p['brand'] and not p['name']]
    todo.sort(key=lambda op: not _MAIN.search(op[1]['category'] + ' ' + op[1]['item']))   # the main pieces first
    todo = todo[:8]
    if not todo:
        return
    results = list(_pool.map(lambda op: _serper('search', {'q': f"{name} {op[1]['brand']} {op[1]['item']}"[:140], 'num': 8}), todo))
    rows, blocks = [], []
    for k, ((o, p), res) in enumerate(zip(todo, results)):
        mine = [' '.join(f"{r.get('title', '')}: {r.get('snippet', '')} ({urlparse(r.get('link') or '').hostname or ''})".split())[:320]
                for r in (res.get('organic') or [])[:8]]
        rows.append(mine)
        when = ', '.join(x for x in (o['event'], o['date']) if x) or 'an event'
        blocks.append(f"[{k + 1}] {p['brand']} {p['item']} (worn at {when})\n" +
                      '\n'.join(f'  {j + 1}. {r}' for j, r in enumerate(mine)))
    if not any(rows):
        return
    prompt = (
        f"PRODUCT NAMES: each numbered piece below is something {name} really wore. Under it are Google results.\n"
        f"For each piece, give its EXACT product name only if one result either clearly says {name} wore that product, "
        f"or is a product page or listing from that same brand in the same colour, fabric and cut. Copy the name word for "
        f"word from that result, without the brand. If no result is clearly the piece, give \"\". Never guess.\n"
        'Return ONLY valid JSON: {"names": [{"piece": 1, "name": "", "result": 1}]}\n\n' + '\n\n'.join(blocks))
    try:
        msg = _ai['client'].messages.create(model=_ai['model'], max_tokens=800, messages=[{'role': 'user', 'content': prompt}])
    except Exception as e:
        print(f'[muses] product names for {name!r} failed: {e!r}')
        return
    from shop import _families, _tokens
    for row in (_json(msg.content[0].text).get('names') or []):
        try:
            k, j = int(row.get('piece')) - 1, int(row.get('result')) - 1
            o, p = todo[k]
            text = rows[k][j]
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        found = str(row.get('name') or '').strip(_QUOTES)[:90]
        plain = _plain(text)
        kinds = set(_families(_tokens(p['category'] + ' ' + p['item'])))
        if (len(found) >= 3 and _plain(found) in plain and _plain(p['brand']).replace(' ', '') in plain.replace(' ', '')
                and (not kinds or kinds & set(_families(_tokens(found + ' ' + text))))):
            p['name'] = found


def _outfit_bank(name, links, snippets=()):
    known = KNOWN.get(norm(name)) or {}
    names = [name.strip()] + known.get('aka', [])
    picked = [(u, t) for u, t in links if not any(h in u for h in SKIP_HOSTS)][:12]
    texts = list(_pool.map(lambda lt: _article_text(lt[0], names), picked))
    articles = [(u, t, x) for (u, t), x in zip(picked, texts) if len(x) > 80][:8]
    if not articles and not snippets:
        return [], []
    outfits, pieces = _extract_outfits(name, articles, snippets)
    try:
        _find_names(name, outfits)
    except Exception as e:
        print(f'[muses] product names for {name!r} skipped: {e!r}')
    return outfits, pieces


def piece_line(p):
    """Zimmermann "Carousel Metallic Twist Gown" (silver metallic twist-front gown), or what's known of it."""
    if p.get('name'):
        return f"{p['brand'] + ' ' if p['brand'] else ''}\"{p['name']}\" ({p['item']})"
    return f"{p['item']} ({p['brand'] + ', product name not known' if p['brand'] else 'label not known'})"


def outfit_line(o):
    pieces = ' + '.join(piece_line(p) for p in o['pieces'])
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
    text, outfits, pieces = None, [], []
    try:
        snippets, photos, links = _evidence(name)
        jobs = []
        if len(snippets) >= 3 or photos:
            jobs.append(_jobs.submit(_write_file, name, snippets[:40], photos))
        bank = _jobs.submit(_outfit_bank, name, links, snippets) if (links or snippets) else None
        if jobs:
            text = jobs[0].result()
        if bank:
            try:
                outfits, pieces = bank.result()
            except Exception as e:
                print(f'[muses] outfits for {name!r} failed: {e!r}')
        kv_set('muse:' + key, json.dumps({'v': V, 'at': now(), 'name': name.strip()[:60], 'text': text, 'outfits': outfits,
                                          'pieces': pieces}, ensure_ascii=False))
        named = sum(_named(o) for o in outfits)
        print(f'[muses] {name!r}: style file {"ready" if text else "not found"}, {len(outfits)} real outfits '
              f'({named} pieces with exact names), {len(pieces)} other real pieces')
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
    if hit.get('v') != V:           # made before exact product names were kept: read up on them again
        return hit, False
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
    """{'text': style file, 'outfits': real outfits, 'pieces': other real pieces}, waiting up to `wait` seconds if
    it's being built right now."""
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
    return {'text': hit.get('text'), 'outfits': hit.get('outfits') or [], 'pieces': hit.get('pieces') or []}


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
        kv_set('muse:' + key, json.dumps({'v': V, 'at': 0, 'name': name, 'text': None}))
    d = muse_data(name, wait=60)
    return jsonify({'name': name, 'text': d.get('text'), 'outfits': d.get('outfits') or [], 'pieces': d.get('pieces') or [],
                    'identity': muse_identity(name)})


# ── A look that recreates a real outfit: her exact pieces, whatever the model wrote ──
_COLOURS = ('silver', 'gold', 'black', 'white', 'ivory', 'cream', 'red', 'pink', 'blue', 'navy', 'green', 'emerald', 'beige',
            'nude', 'brown', 'grey', 'gray', 'lilac', 'purple', 'yellow', 'orange', 'burgundy', 'champagne', 'blush', 'camel',
            'khaki', 'teal', 'coral', 'bronze', 'chocolate', 'olive', 'sage', 'lavender', 'fuchsia', 'scarlet')


def display_name(p):
    """What a piece is called in a look: its exact product name (with its colour), or plain words. Never a made-up name."""
    if p.get('name'):
        name = p['name']
        colour = next((c for c in _COLOURS if re.search(rf'\b{c}\b', (p.get('item') or '').lower())), '')
        if colour and not re.search(rf'\b{colour}\b', name.lower()):
            name += f' in {colour.title()}'
        return name
    item = (p.get('item') or '').strip()
    return item[:1].upper() + item[1:]


def _match(p, it):
    """How sure we are that look item `it` is real piece `p` (1 or more: the same piece)."""
    from shop import _families, _tokens
    pb, ib = _plain(p.get('brand')), _plain(it.get('brand'))
    score = 0.0
    if pb and ib:
        score += 1.5 if (pb == ib or pb in ib or ib in pb) else -0.5    # a swapped label is still that piece
    pt = _tokens(' '.join((p.get('category') or '', p.get('item') or '', p.get('name') or '')))
    got = _tokens(' '.join(str(it.get(k) or '') for k in ('category', 'name')))
    pf, gf = set(_families(pt)), set(_families(got))
    if pf and gf:
        score += 1 if pf & gf else -3
    if pt:
        score += len(pt & got) / len(pt)
    return score


def apply_real(look, outfit, pieces=(), muse=''):
    """Make the look's items exactly the real outfit's pieces (names and brands), in the outfit's order, adding any
    piece the model left out; other items that are things they've really worn are marked too. Each real piece gets
    "worn": the muse's name, so the app can say so."""
    items = [i for i in (look.get('items') or []) if isinstance(i, dict)]
    who = (muse or '').strip()[:60]
    real = outfit.get('pieces') or []
    pairs = sorted(((_match(p, it), pi, ii) for pi, p in enumerate(real) for ii, it in enumerate(items)), reverse=True)
    got, taken = {}, set()
    for score, pi, ii in pairs:
        if score >= 1 and pi not in got and ii not in taken:
            got[pi] = ii
            taken.add(ii)
    ordered = []
    for pi, p in enumerate(real):
        if pi in got:
            it = items[got[pi]]
            old = str(it.get('name') or '')
            it['name'] = display_name(p)
            if p.get('brand'):
                it['brand'] = p['brand']
            if old and old != it['name'] and isinstance(it.get('description'), str):
                it['description'] = it['description'].replace(old, it['name'])
        else:
            it = {'category': p.get('category') or 'Piece', 'name': display_name(p), 'brand': p.get('brand') or '',
                  'store': '', 'price': '', 'description': 'Her original piece' + (f" from {outfit['event']}" if outfit.get('event') else '') + '.'}
        it['worn'] = who
        ordered.append(it)
    known = [_plain(f"{w.get('brand') or ''} {w['name']}") for w in pieces if w.get('name')]
    rest = []
    for ii, it in enumerate(items):
        if ii in taken:
            continue
        full = _plain(f"{it.get('brand') or ''} {it.get('name') or ''}")
        it.pop('worn', None)
        if full and any(k and (k == full or (len(k) > 6 and k in full)) for k in known):
            it['worn'] = who
        rest.append(it)
    look['items'] = ordered + rest
    return look
