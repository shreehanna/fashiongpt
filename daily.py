"""Reasons to open E&L every day: today's fit from your own closet, characters from the series as muses, and
fashion reels with "Get this look". (Ella's Sunday Edit, the story of the week, is in story.py.)

  /api/daily-fit        Ella picks today's outfit from the closet you typed in (fast model, free once a day)
  /api/closet/label     names a closet photo ("black satin slip skirt") so Ella knows what it is
  /api/weather          today's weather near you (MET Norway), for the daily fit
  /api/characters       characters from the series, picked as a muse
  /api/reels            fashion Shorts from YouTube plus Instagram reels the owner picked
Owner tools (owner only): /api/owner/characters, /api/owner/reels
"""
import base64
import html
import json
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import requests as req
from flask import Blueprint, jsonify, request

from pro import IST, client_ip, env, identity, kv_get, kv_set, now, owner_request, refund_look, reserve_look, today

bp = Blueprint('daily', __name__)
SERIES = 'The Social Representation'
_ai = {'client': None, 'quick': None, 'style': None}


def init(client, quick_model, style_model):
    _ai.update(client=client, quick=quick_model, style=style_model)


def _ask(prompt, max_tokens, quick=True, content=None):
    """One call to Claude; the fast model for small jobs, falling back to the styling model."""
    from alerts import ServiceDown
    client = _ai['client']
    msgs = [{'role': 'user', 'content': content or prompt}]
    models = [_ai['quick'], _ai['style']] if quick else [_ai['style']]
    last = None
    for m in models:
        try:
            return client.messages.create(model=m, max_tokens=max_tokens, messages=msgs).content[0].text
        except ServiceDown:
            raise
        except Exception as e:
            last = e
            print(f'[daily] {m} failed: {e!r}')
    raise last


def _json(text):
    t = (text or '').strip()
    if '```' in t:
        t = t.split('```json')[1] if '```json' in t else t.split('```')[1]
        t = t.split('```')[0]
    try:
        return json.loads(t.strip())
    except ValueError:
        m = re.search(r'\{.*\}', t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


_hits = {}
_hits_lock = threading.Lock()


def _allowed(key, limit, window):
    t = time.time()
    with _hits_lock:
        q = _hits.setdefault(key, deque())
        while q and t - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True


def _clip(v, n):
    return ' '.join(str(v or '').split())[:n]


# ── Today's fit ───────────────────────────────────────────────────────────────
CATEGORIES = ['Top', 'Bottom', 'Dress', 'Outerwear', 'Shoes', 'Bag', 'Accessory', 'Ethnic', 'Other']


def _daily_prompt(body, closet, recent):
    weather = body.get('weather') or {}
    w = ''
    if weather.get('summary') or weather.get('temp_c') is not None:
        bits = [_clip(weather.get('summary'), 40)]
        if weather.get('temp_c') is not None:
            bits.append(f"{weather.get('temp_c')}°C now")
        if weather.get('max_c') is not None and weather.get('min_c') is not None:
            bits.append(f"high {weather.get('max_c')}°C, low {weather.get('min_c')}°C")
        if weather.get('rain_mm'):
            bits.append(f"{weather.get('rain_mm')} mm of rain expected")
        w = ', '.join(b for b in bits if b)
    elif body.get('feel'):
        w = _clip(body.get('feel'), 40)
    city = _clip(body.get('city'), 60)
    plans = _clip(body.get('plans'), 200) or 'an ordinary day'
    gender = 'man' if body.get('gender') == 'men' else 'woman'
    lines = '\n'.join(f"{c['id']}: {c['name']}" + (f" ({c['category']})" if c['category'] else '') for c in closet)
    avoid = '\n'.join('- ' + ' + '.join(r) for r in recent) if recent else ''
    day = datetime.now(IST).strftime('%A %d %B')
    profile = _clip(body.get('style_profile'), 300)
    muse = _clip(body.get('celeb'), 60)
    return f"""You are Ella, the stylist from E&L: sharp, warm, a little Gossip Girl. Every morning you pick the user's outfit from clothes they already own.

Today: {day}{f' in {city}' if city else ''}.
Weather: {w or 'unknown, so pick something that works for most weather'}.
Their plans today: {plans}.
Styling a {gender}.{f' Style profile: {profile}.' if profile else ''}{f' Their style muse: {muse}.' if muse and muse.lower() != 'no specific muse' else ''}

Their closet (id: item):
{lines}

Build today's outfit ONLY from these closet items, by id: 3 to 6 items that make one complete, weather-right outfit for their plans (top and bottom or a dress, shoes if they have any, then a layer, bag or accessory if it helps).
{f'Recently worn combinations to avoid repeating:{chr(10)}{avoid}' if avoid else ''}
If something essential is missing from the closet (for example no shoes at all, or no layer on a cold day), suggest ONE piece to buy in "missing"; otherwise set "missing" to null.

Return ONLY valid JSON:
{{
  "look_name": "a 2-4 word name for today's look",
  "ella_note": "2 short sentences in Ella's voice: why this works for today's weather and plans",
  "items": [{{"id": "closet id", "how": "one short line on how to wear it (tuck, roll, layer...)"}}],
  "missing": {{"category": "...", "name": "the piece to buy", "why": "one line"}} or null,
  "weather_tip": "one short line about the weather (umbrella, layer, sunglasses...)"
}}"""


@bp.post('/api/daily-fit')
def daily_fit():
    body = request.get_json(silent=True) or {}
    closet, seen = [], set()
    for c in (body.get('closet') or [])[:120]:
        if not isinstance(c, dict):
            continue
        cid = _clip(c.get('id'), 16)
        name = _clip(c.get('name'), 60)
        if not cid or not name or cid in seen:
            continue
        seen.add(cid)
        cat = _clip(c.get('category'), 20)
        closet.append({'id': cid, 'name': name, 'category': cat if cat in CATEGORIES else ''})
    if len(closet) < 3:
        return jsonify({'error': 'Add at least 3 things you own to your closet first.'}), 400
    recent = []
    names = {c['id']: c['name'] for c in closet}
    for combo in (body.get('recent') or [])[:5]:
        if isinstance(combo, list):
            recent.append([names[i] for i in combo if i in names][:6])
    recent = [r for r in recent if r]

    email, who = identity()
    ticket = None
    free_key = 'daily:' + who
    try:
        from pro import _bump, payments_live
        first = _bump(free_key, today(), +1) == 1 if payments_live() else True
    except Exception as e:
        print('[daily] free-fit check skipped:', e)
        first = True
    if not first:                                   # another option today counts as one of the free looks
        ticket = reserve_look()
        if not ticket['ok']:
            return jsonify({'error': 'limit', 'pro': ticket['pro'], 'limit': ticket['limit'],
                            'signed_in': ticket['signed_in']}), 402
    try:
        out = _json(_ask(_daily_prompt(body, closet, recent), 900))
        if not isinstance(out, dict):
            raise ValueError('no JSON')
        by_id = {c['id']: c for c in closet}
        items = []
        for it in out.get('items') or []:
            if isinstance(it, dict) and str(it.get('id')) in by_id and str(it.get('id')) not in [i['id'] for i in items]:
                c = by_id[str(it['id'])]
                items.append({'id': c['id'], 'name': c['name'], 'category': c['category'] or 'Piece',
                              'how': _clip(it.get('how'), 160)})
        if len(items) < 2:
            raise ValueError('too few closet pieces')
        missing = out.get('missing') if isinstance(out.get('missing'), dict) and out['missing'].get('name') else None
        if missing:
            missing = {k: _clip(missing.get(k), 120) for k in ('category', 'name', 'why')}
        look = {'look_name': _clip(out.get('look_name'), 60) or "Today's fit",
                'ella_note': _clip(out.get('ella_note'), 400), 'weather_tip': _clip(out.get('weather_tip'), 160),
                'closet_items': items, 'missing': missing,
                'items': [{'category': i['category'], 'name': i['name'], 'brand': 'From my closet', 'price': '',
                           'store': '', 'description': i['how']} for i in items]}
        from growth import sign_look, track
        sign_look(look, {'occasion': _clip(body.get('plans'), 100) or 'today', 'stylist': body.get('stylist'),
                         'gender': body.get('gender')}, kind='daily')
        track('daily_fit', who)
        look['free_today'] = bool(first)
        return jsonify(look)
    except Exception as e:
        print('[daily] fit failed:', repr(e))
        if ticket:
            refund_look(ticket)
        elif first:
            try:
                from pro import _bump
                _bump(free_key, today(), -1)
            except Exception:
                pass
        from alerts import ServiceDown
        msg = str(e) if isinstance(e, ServiceDown) else 'Ella hit a snag picking today’s fit. Try again.'
        return jsonify({'error': msg}), 502


@bp.post('/api/closet/label')
def closet_label():
    body = request.get_json(silent=True) or {}
    img = str(body.get('image') or '')
    m = re.match(r'^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$', img)
    if not m or len(img) > 400_000:
        return jsonify({'error': 'Send a small JPEG.'}), 400
    ip = client_ip()
    if not owner_request() and not _allowed('lbl:' + ip, 40, 600):
        return jsonify({'error': 'slow down'}), 429
    try:
        base64.b64decode(m.group(2), validate=True)
    except Exception:
        return jsonify({'error': 'bad image'}), 400
    prompt = ('Name this clothing item for a wardrobe app. Return ONLY JSON: {"name": "3-6 words: colour, fabric or detail, '
              'then the item, like \\"black satin slip skirt\\" or \\"white linen shirt\\"", "category": one of '
              + json.dumps(CATEGORIES) + '}. If it is not clothing, shoes, a bag or an accessory, return {"name": "", "category": "Other"}.')
    content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/' + m.group(1), 'data': m.group(2)}},
               {'type': 'text', 'text': prompt}]
    try:
        out = _json(_ask(prompt, 120, content=content)) or {}
    except Exception as e:
        print('[daily] label failed:', repr(e))
        return jsonify({'error': 'Could not name it. Type a name instead.'}), 502
    cat = out.get('category') if out.get('category') in CATEGORIES else 'Other'
    return jsonify({'name': _clip(out.get('name'), 60), 'category': cat})


# ── Weather (MET Norway: free, including for commercial apps, with credit) ──────
_weather_cache = {}
SYMBOLS = [('thunder', 'Thunderstorms', '⛈'), ('heavysnow', 'Heavy snow', '❄️'), ('snow', 'Snow', '🌨'),
           ('sleet', 'Sleet', '🌨'), ('heavyrain', 'Heavy rain', '🌧'), ('lightrain', 'Light rain', '🌦'),
           ('rainshowers', 'Showers', '🌦'), ('rain', 'Rain', '🌧'), ('fog', 'Fog', '🌫'), ('partlycloudy', 'Partly cloudy', '⛅'),
           ('cloudy', 'Cloudy', '☁️'), ('fair', 'Mostly sunny', '🌤'), ('clearsky', 'Clear', '☀️')]


def describe_symbol(code):
    code = (code or '').split('_')[0]
    for key, words, icon in SYMBOLS:
        if code.startswith(key) or key in code:
            return words, icon
    return 'Mixed', '🌤'


def parse_met(data):
    series = ((data or {}).get('properties') or {}).get('timeseries') or []
    if not series:
        return None
    first = series[0]['data']
    temp = first['instant']['details'].get('air_temperature')
    nxt = first.get('next_1_hours') or first.get('next_6_hours') or first.get('next_12_hours') or {}
    words, icon = describe_symbol((nxt.get('summary') or {}).get('symbol_code'))
    temps, rain = [], 0.0
    for s in series[:13]:                                   # the next 12 hours
        t = s['data']['instant']['details'].get('air_temperature')
        if t is not None:
            temps.append(t)
        r = ((s['data'].get('next_1_hours') or {}).get('details') or {}).get('precipitation_amount')
        if r:
            rain += r
    return {'temp_c': round(temp) if temp is not None else None, 'min_c': round(min(temps)) if temps else None,
            'max_c': round(max(temps)) if temps else None, 'summary': words, 'icon': icon,
            'rain_mm': round(rain, 1), 'source': 'MET Norway'}


@bp.get('/api/weather')
def weather():
    try:
        lat = round(float(request.args.get('lat', '')), 1)       # about 10 km: enough for weather, not an address
        lon = round(float(request.args.get('lon', '')), 1)
    except ValueError:
        return jsonify({'error': 'bad location'}), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({'error': 'bad location'}), 400
    if not _allowed('wx:' + client_ip(), 30, 600):
        return jsonify({'error': 'slow down'}), 429
    key = (lat, lon)
    hit = _weather_cache.get(key)
    if hit and time.time() - hit[0] < 1800:
        return jsonify(hit[1])
    contact = env('CONTACT_EMAIL') or env('PUBLIC_URL') or 'https://fashiongpt-f0vi.onrender.com'
    try:
        r = req.get('https://api.met.no/weatherapi/locationforecast/2.0/compact', params={'lat': lat, 'lon': lon},
                    headers={'User-Agent': f'EandL-stylist/1.0 {contact}'}, timeout=8)
        out = parse_met(r.json()) if r.status_code == 200 else None
    except Exception as e:
        print('[daily] weather failed:', e)
        out = None
    if not out:
        return jsonify({'error': 'weather unavailable'}), 502
    _weather_cache[key] = (time.time(), out)
    if len(_weather_cache) > 2000:
        _weather_cache.clear()
    return jsonify(out)


# ── Characters from the series, as muses ──────────────────────────────────────
def characters():
    try:
        out = json.loads(kv_get('characters') or '[]')
    except Exception:
        return []
    return [c for c in out if isinstance(c, dict) and c.get('name')]


def find_character(name):
    n = _clip(name, 60).lower()
    if not n:
        return None
    for c in characters():
        if c['name'].lower() == n:
            return c
    return None


@bp.get('/api/characters')
def list_characters():
    return jsonify({'series': SERIES, 'characters': [{'name': c['name'], 'style': c.get('style', '')} for c in characters()]})


# ── Reels ─────────────────────────────────────────────────────────────────────
QUERIES = {
    'in': ['zara try on haul', 'myntra haul', 'h&m try on haul india', 'ajio haul', 'mango try on haul',
           'outfit ideas india', 'wedding guest outfit ideas india', 'get ready with me outfit'],
    'intl': ['zara try on haul', 'aritzia try on haul', 'mango try on haul', 'h&m try on haul', 'street style outfits',
             'outfit ideas', 'get ready with me outfit', 'revolve try on haul'],
}
BLOCK = re.compile(r'lingerie|bikini|swim ?suit|underwear|\bbra\b|see.?through|transparent|nude|braless|thong', re.I)
YT_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
IG_URL = re.compile(r'^https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(reel|reels|p)/([A-Za-z0-9_-]{5,40})/?')
YT_URL = re.compile(r'(?:youtube\.com/(?:shorts/|watch\?v=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})')
_yt_lock = threading.Lock()


def _yt_search(q, region):
    key = env('YOUTUBE_API_KEY')
    r = req.get('https://www.googleapis.com/youtube/v3/search', timeout=10, params={
        'part': 'snippet', 'type': 'video', 'videoDuration': 'short', 'videoEmbeddable': 'true', 'maxResults': 25,
        'q': q, 'key': key, 'safeSearch': 'strict', 'relevanceLanguage': 'en',
        'regionCode': 'IN' if region == 'in' else 'US', 'order': 'relevance'})
    if r.status_code != 200:
        raise RuntimeError(f'YouTube {r.status_code}: {r.text[:160]}')
    out = []
    for it in r.json().get('items') or []:
        vid = (it.get('id') or {}).get('videoId') or ''
        sn = it.get('snippet') or {}
        title = html.unescape(sn.get('title') or '')
        if not YT_ID.match(vid) or BLOCK.search(title + ' ' + (sn.get('description') or '')):
            continue
        out.append({'kind': 'yt', 'id': vid, 'title': title[:140], 'channel': html.unescape(sn.get('channelTitle') or '')[:60],
                    'thumb': f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'})
    return out


def _yt_cached(q, region, allow_fetch):
    k = f'reels:yt:{region}:{q}'
    try:
        hit = json.loads(kv_get(k) or 'null')
    except Exception:
        hit = None
    fresh = hit and now() - hit.get('at', 0) < 12 * 3600
    if (not fresh) and allow_fetch and env('YOUTUBE_API_KEY'):
        try:
            items = _yt_search(q, region)
            hit = {'at': now(), 'items': items}
            kv_set(k, json.dumps(hit, ensure_ascii=False))
            return items, True
        except Exception as e:
            print('[daily] YouTube search failed:', e)
    return (hit or {}).get('items') or [], False


def reel_picks():
    try:
        out = json.loads(kv_get('reels:picks') or '[]')
    except Exception:
        out = []
    return [p for p in out if isinstance(p, dict) and p.get('id')]


@bp.get('/api/reels')
def reels():
    region = 'in' if request.args.get('region') == 'in' else 'intl'
    picks = [dict(p, pick=True) for p in reel_picks()]
    lists, fetched = [], False
    with _yt_lock:                                         # at most one YouTube search per request
        for q in QUERIES[region]:
            items, did = _yt_cached(q, region, allow_fetch=not fetched)
            fetched = fetched or did
            lists.append(items)
    rnd = random.Random(today() + region)
    for l in lists:
        rnd.shuffle(l)
    feed, seen = [], {p['id'] for p in picks}
    for row in range(25):
        for l in lists:
            if row < len(l) and l[row]['id'] not in seen:
                seen.add(l[row]['id'])
                feed.append(l[row])
    return jsonify({'items': (picks + feed)[:80], 'youtube': bool(env('YOUTUBE_API_KEY'))})


SHOT = re.compile(r'^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$')


def reel_for_look(reel):
    """What Ella should look at for "Get this look": the thumbnail of a YouTube video (by its id only), the
    owner's own caption for an Instagram pick, or a screenshot the person uploaded (sent to Claude inline, never
    stored). Nothing the browser sends is fetched as a URL."""
    if not isinstance(reel, dict):
        return None
    if reel.get('kind') == 'link':      # an Instagram, TikTok or Pinterest post: its own picture, fetched from that app
        from identify import IMAGE_HOSTS, _get, _jpeg, picture_from_link
        url, caption, where = picture_from_link(str(reel.get('link') or '')[:500])
        raw = _get(url, IMAGE_HOSTS, limit=8_000_000, accept='image/*')[0] if url else None
        jpeg = _jpeg(raw, 1280)[1] if raw else None
        if not jpeg:
            return None
        return {'image': {'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(jpeg).decode()},
                'title': '', 'channel': where, 'caption': _clip(caption, 300), 'shot': True}
    if reel.get('kind') == 'shot':
        img = str(reel.get('image') or '')
        m = SHOT.match(img)
        if not m or len(img) > 2_000_000:
            return None
        try:
            base64.b64decode(m.group(2), validate=True)
        except Exception:
            return None
        return {'image': {'type': 'base64', 'media_type': 'image/' + m.group(1), 'data': m.group(2)},
                'title': '', 'channel': '', 'shot': True}
    rid = str(reel.get('id') or '')
    for p in reel_picks():
        if p['id'] == rid:
            shot = _pick_shot(rid)
            if shot:                                  # the owner's screenshot of the outfit: Ella sees the real thing
                return {'image': shot, 'title': p.get('caption') or '', 'channel': '', 'pick_shot': True}
            if p.get('kind') == 'yt':
                return {'image': f'https://i.ytimg.com/vi/{rid}/hqdefault.jpg', 'title': p.get('caption') or '',
                        'channel': ''}
            return {'image': None, 'title': p.get('caption') or 'an Instagram fashion reel', 'channel': ''}
    if YT_ID.match(rid):
        return {'image': f'https://i.ytimg.com/vi/{rid}/hqdefault.jpg', 'title': _clip(reel.get('title'), 140),
                'channel': _clip(reel.get('channel'), 60)}
    return None


def _pick_shot(rid):
    try:
        raw = kv_get('reel:shot:' + rid)
    except Exception:
        return None
    m = SHOT.match(raw or '')
    return {'type': 'base64', 'media_type': 'image/' + m.group(1), 'data': m.group(2)} if m else None


def _save_pick_shot(rid, img):
    """The owner's screenshot of a picked reel's outfit, kept small (the page shrinks it first)."""
    m = SHOT.match(str(img or ''))
    if not m or len(img) > 900_000:
        return False
    try:
        base64.b64decode(m.group(2), validate=True)
    except Exception:
        return False
    kv_set('reel:shot:' + rid, img)
    return True


# ── Owner tools ───────────────────────────────────────────────────────────────
@bp.get('/api/owner/content')
def owner_content():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    from growth import challenge_theme
    picks = [dict(p, has_shot=bool(kv_get('reel:shot:' + p['id']))) for p in reel_picks()]
    return jsonify({'characters': characters(), 'reels': picks, 'youtube': bool(env('YOUTUBE_API_KEY')),
                    'challenge': challenge_theme(), 'series': SERIES})


@bp.post('/api/owner/characters')
def owner_characters():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    out = []
    for c in ((request.get_json(silent=True) or {}).get('characters') or [])[:16]:
        if isinstance(c, dict) and _clip(c.get('name'), 40):
            out.append({'name': _clip(c.get('name'), 40), 'style': _clip(c.get('style'), 300)})
    kv_set('characters', json.dumps(out, ensure_ascii=False))
    return owner_content()


@bp.post('/api/owner/reels')
def owner_reels():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    picks = reel_picks()
    if body.get('shot_for'):                          # add or replace the screenshot of a pick already in the feed
        if not any(p['id'] == body['shot_for'] for p in picks) or not _save_pick_shot(body['shot_for'], body.get('image')):
            return jsonify({'error': 'Use a JPEG or PNG screenshot.'}), 400
        return owner_content()
    if body.get('remove'):
        picks = [p for p in picks if p['id'] != body['remove']]
        kv_set('reel:shot:' + str(body['remove'])[:60], '')
    url = str(body.get('url') or '').strip()
    if url:
        caption = _clip(body.get('caption'), 160)
        m, y = IG_URL.match(url), YT_URL.search(url)
        if m:
            kind = 'p' if m.group(1) == 'p' else 'reel'
            picks.insert(0, {'kind': 'ig', 'id': m.group(2), 'url': f'https://www.instagram.com/{kind}/{m.group(2)}/',
                             'caption': caption, 'added_at': now()})
        elif y:
            picks.insert(0, {'kind': 'yt', 'id': y.group(1), 'title': caption, 'caption': caption, 'channel': '',
                             'thumb': f'https://i.ytimg.com/vi/{y.group(1)}/hqdefault.jpg', 'added_at': now()})
        else:
            return jsonify({'error': 'Paste an Instagram reel or YouTube link.'}), 400
        seen, dedup = set(), []
        for p in picks:
            if p['id'] not in seen:
                seen.add(p['id'])
                dedup.append(p)
        picks = dedup[:60]
        if body.get('image') and not _save_pick_shot(picks[0]['id'], body['image']):
            return jsonify({'error': 'Use a JPEG or PNG screenshot.'}), 400
    kv_set('reels:picks', json.dumps(picks, ensure_ascii=False))
    return owner_content()
