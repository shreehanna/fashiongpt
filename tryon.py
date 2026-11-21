"""Try it on me: upload a photo of yourself and see the look on you (an AI picture, made by OpenAI's image model).

Who gets it: everyone 1 a day, Pro 5 a day, the owner as many as they like (TRYON_FREE_PER_DAY / TRYON_PRO_PER_DAY),
and never more than TRYON_DAILY_MAX for the whole site in a day, so a spike can't empty the OpenAI credits.
Each picture costs about $0.04 (gpt-image-2, medium); TRYON_MODEL and TRYON_QUALITY change that.

Privacy: the photo and the picture are only ever held in memory (the picture for at most 10 minutes, until the phone
collects it), never written to the database or disk. The person confirms it's them and they're 18 or over. No
try-on for swimwear or lingerie looks. OpenAI's own moderation also applies.

The picture takes 30-60 seconds, so POST /api/try-on starts it and the phone asks GET /api/try-on/<job> until it's
ready (no request is held open long enough for a proxy to cut it off).
"""
import base64
import hashlib
import io
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests as req
from flask import Blueprint, jsonify, request

from pro import db, identity, is_owner, is_pro, get_user, today, client_ip

bp = Blueprint('tryon', __name__)
API = 'https://api.openai.com/v1/images/edits'
FREE_PER_DAY = int(os.environ.get('TRYON_FREE_PER_DAY', 1))
PRO_PER_DAY = int(os.environ.get('TRYON_PRO_PER_DAY', 5))
ANON_PER_IP = int(os.environ.get('TRYON_ANON_PER_IP', 3))      # signed-out people on one network, per day
DAILY_MAX = int(os.environ.get('TRYON_DAILY_MAX', 60))         # the whole site, per day (~$2.50)
COST = float(os.environ.get('TRYON_COST', 0.05))               # an estimate per picture, for the balance in owner tools
MAX_PHOTO = 8 * 1024 * 1024
KEEP = 600                                                     # a finished picture waits at most 10 minutes
_jobs, _jobs_lock = {}, threading.Lock()
_pool = ThreadPoolExecutor(max_workers=3)
# Real photos of people: never in swimwear or underwear
NOT_FOR_TRYON = re.compile(r'\b(bikini|swim\w*|monokini|tankini|lingerie|bra|bralette|bras|underwear|briefs|boxers|thong|panties|'
                           r'negligee|bodystocking|nightie|bathing suit)\b', re.I)


def init():
    db('CREATE TABLE IF NOT EXISTS el_tryon (who TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (who, day))')


def ready():
    return bool(os.environ.get('OPENAI_API_KEY'))


def _bump(who, day, by):
    row = db('SELECT n FROM el_tryon WHERE who = %s AND day = %s', (who, day), one=True)
    if row:
        db('UPDATE el_tryon SET n = n + %s WHERE who = %s AND day = %s', (by, who, day))
    else:
        db('INSERT INTO el_tryon (who, day, n) VALUES (%s, %s, %s)', (who, day, max(0, by)))
    row = db('SELECT n FROM el_tryon WHERE who = %s AND day = %s', (who, day), one=True)
    return int(row[0]) if row else 0


def _used(who, day):
    row = db('SELECT n FROM el_tryon WHERE who = %s AND day = %s', (who, day), one=True)
    return int(row[0]) if row else 0


def reserve():
    """Count it up front (two quick taps can't both slip through); refunded if the picture isn't made."""
    email, who = identity()
    day = today()
    if is_owner(email):
        return {'ok': True, 'keys': [], 'left': None}
    pro = is_pro(get_user(email)) if email else False
    limit = PRO_PER_DAY if pro else FREE_PER_DAY
    keys = []
    if _bump('site', day, +1) > DAILY_MAX:
        _bump('site', day, -1)
        return {'ok': False, 'why': 'busy', 'pro': pro, 'signed_in': bool(email)}
    keys.append(('site', day))
    if _bump(who, day, +1) > limit:
        _bump(who, day, -1)
        refund({'keys': keys})
        return {'ok': False, 'why': 'limit', 'pro': pro, 'limit': limit, 'signed_in': bool(email)}
    keys.append((who, day))
    if not email:
        ip = 'ip:' + client_ip()
        if _bump(ip, day, +1) > ANON_PER_IP:
            _bump(ip, day, -1)
            refund({'keys': keys})
            return {'ok': False, 'why': 'limit', 'pro': False, 'limit': limit, 'signed_in': False}
        keys.append((ip, day))
    return {'ok': True, 'keys': keys, 'left': max(0, limit - _used(who, day))}


def refund(ticket):
    for who, day in ticket.get('keys', []):
        try:
            _bump(who, day, -1)
        except Exception as e:
            print('[tryon] refund failed:', e)


def left_today():
    email, who = identity()
    if is_owner(email):
        return None
    pro = is_pro(get_user(email)) if email else False
    return max(0, (PRO_PER_DAY if pro else FREE_PER_DAY) - _used(who, today()))


# ── pictures in ───────────────────────────────────────────────────────────────
def _jpeg_url(data, edge):
    """Any photo -> a data URL of a JPEG at most `edge` px, turned upright, with its EXIF (place, phone...) dropped.
    None if it isn't a picture."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im = ImageOps.exif_transpose(im).convert('RGB')
    except Exception:
        return None
    if min(im.size) < 160:
        return None
    im.thumbnail((edge, edge))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=88)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()


def _product_photo(url):
    """A piece's product photo, fetched here (so a shop that blocks hotlinking still works), or None."""
    import muses
    if not url or not muses._public_https(url):
        return None
    try:
        r = req.get(url, timeout=6, headers=muses._BROWSER, stream=True)
        if r.status_code != 200 or not (r.headers.get('Content-Type') or '').startswith('image/'):
            return None
        return _jpeg_url(r.raw.read(5 * 1024 * 1024, decode_content=True), 768)
    except Exception:
        return None


def prompt_for(items, photo_of=None):
    """photo_of: {index of a piece in items: its image number}. Each piece with a product photo is tied to that image by
    number, so the model never mixes them up, and the photo (what the person would actually buy) decides its colour,
    print and fabric; the words only say what the piece is."""
    photo_of = photo_of or {}
    lines = []
    for i, it in enumerate(items):
        name = ' '.join(str(it.get('real_name') or it.get('name') or '').split())[:120]
        brand = ' '.join(str(it.get('brand') or '').split())[:60]
        desc = ' '.join(str(it.get('description') or '').split())[:160]
        cat = ' '.join(str(it.get('category') or '').split())[:40]
        if not name:
            continue
        what = f"- {cat + ': ' if cat else ''}{name}{' by ' + brand if brand and brand != 'From my closet' else ''}"
        if i in photo_of:
            what += f" (exactly as shown in image {photo_of[i]})"
        elif desc:
            what += f" ({desc})"
        lines.append(what)
    photos = ("Each piece marked with an image number must look exactly like that product photo: same colour, print, "
              "fabric, sheerness, cut, length and details. The photo wins over any words. Do not recolour a piece to "
              "match the rest of the outfit, and do not swap a photographed piece for a different one. "
              if photo_of else '')
    return ("Image 1 is a photo of a real person. Make a realistic fashion photo of THIS SAME PERSON wearing the outfit "
            "below, full length or three-quarter length. " + photos +
            "Keep their face, identity, skin tone, hair, body shape, height and proportions exactly as in image 1: "
            "do not slim, reshape, beautify, age or change them in any way. Keep their pose if you can, and their "
            "background, or a plain softly lit backdrop. Fully dressed, tasteful and natural, like a good street-style "
            "photo. No text, no logos that aren't on the pieces, no watermark, no extra people.\n\nThe outfit:\n"
            + '\n'.join(lines[:10]))


# ── the picture ───────────────────────────────────────────────────────────────
def make_picture(photo_url, items, who):
    """Calls OpenAI. Returns (jpeg bytes, None) or (None, (kind, message))."""
    refs, photo_of = [], {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        wanted = [it.get('shop_image') or it.get('photo') for it in items][:6]
        for i, got in enumerate(pool.map(_product_photo, wanted)):
            if got and len(refs) < 4:
                refs.append(got)
                photo_of[i] = len(refs) + 1          # image 1 is the person; their pieces' photos follow
    print(f'[tryon] {len(refs)} of {len(items)} pieces have a product photo')
    body = {'model': os.environ.get('TRYON_MODEL') or 'gpt-image-2', 'prompt': prompt_for(items, photo_of),
            'images': [{'image_url': photo_url}] + [{'image_url': u} for u in refs],
            'size': '1024x1536', 'quality': os.environ.get('TRYON_QUALITY') or 'medium',
            'output_format': 'jpeg', 'output_compression': 85, 'moderation': 'auto', 'n': 1,
            'safety_identifier': hashlib.sha256(('el|' + who).encode()).hexdigest()[:40]}
    try:
        r = req.post(API, json=body, timeout=(10, 180),
                     headers={'Authorization': f"Bearer {os.environ.get('OPENAI_API_KEY')}", 'Content-Type': 'application/json'})
    except req.RequestException as e:
        print('[tryon] OpenAI unreachable:', repr(e)[:200])
        return None, ('failed', "Ella couldn't reach the picture studio. Try again in a minute.")
    if r.status_code == 200:
        try:
            pic = base64.b64decode(r.json()['data'][0]['b64_json'])
        except Exception:
            return None, ('failed', "The picture came back empty. Try again.")
        _spent()
        return pic, None
    try:
        err = (r.json() or {}).get('error') or {}
    except ValueError:
        err = {'message': r.text[:300]}
    code = str(err.get('code') or err.get('type') or '').lower()
    msg = str(err.get('message') or '')
    print('[tryon] OpenAI said', r.status_code, code, msg[:200])
    if r.status_code == 400 and ('moderation' in code or 'safety' in code or 'safety' in msg.lower()):
        return None, ('declined', "The picture service wouldn't make this one. Try a different photo: just you, "
                                  "well lit, standing, fully dressed.")
    if r.status_code in (401, 403) or r.status_code == 429 and code in ('insufficient_quota', 'billing_hard_limit_reached'):
        _alert(r.status_code, code, msg)
        return None, ('down', "Try-on is taking a short break. Please try again later.")
    return None, ('failed', "Ella couldn't make the picture this time. Try again in a minute.")


def _spent():
    try:
        import alerts
        if alerts._brain() == 'openai':
            alerts.later(alerts._spend, 'claude', COST)
    except Exception:
        pass


def _alert(status, code, msg):
    try:
        import alerts
        kind = 'key' if status in (401, 403) else 'out'
        text = ("OpenAI says the API credits have run out, so try-on pictures can't be made." if kind == 'out' else
                f"OpenAI refused the API key for try-on pictures ({status}). Check OPENAI_API_KEY in Render.")
        if alerts._brain() == 'openai':          # the styling row is OpenAI's: same account, same credits
            alerts.later(alerts.report, 'claude', kind, text)
    except Exception as e:
        print('[tryon] alert failed:', e)


def _run(job, photo_url, items, who, ticket):
    pic, problem = make_picture(photo_url, items, who)
    with _jobs_lock:
        j = _jobs.get(job)
        if j is None:
            return
        if pic:
            j.update(state='done', image='data:image/jpeg;base64,' + base64.b64encode(pic).decode(), at=time.time())
        else:
            refund(ticket)
            j.update(state='failed', kind=problem[0], error=problem[1], at=time.time())


def _tidy():
    t = time.time()
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if t - j['at'] > KEEP]:
            _jobs.pop(k, None)


@bp.get('/api/try-on/left')
def left():
    return jsonify({'ready': ready(), 'left': left_today(), 'free': FREE_PER_DAY, 'pro': PRO_PER_DAY})


@bp.post('/api/try-on')
def start():
    """multipart: photo (the person), look (JSON: {"items": [...]}), consent=1."""
    import json
    if not ready():
        return jsonify({'error': "Try-on isn't set up yet."}), 503
    if request.form.get('consent') != '1':
        return jsonify({'error': "Please confirm it's you and you're 18 or over."}), 400
    f = request.files.get('photo')
    data = f.read(MAX_PHOTO + 1) if f else b''
    if not data or len(data) > MAX_PHOTO:
        return jsonify({'error': 'Pick a photo under 8 MB.'}), 400
    try:
        look = json.loads(request.form.get('look') or '{}')
    except ValueError:
        look = {}
    items = [it for it in (look.get('items') or []) if isinstance(it, dict)][:12]
    if not items:
        return jsonify({'error': 'There is no look to try on.'}), 400
    words = ' '.join(f"{it.get('category', '')} {it.get('name', '')} {it.get('real_name', '')}" for it in items)
    if NOT_FOR_TRYON.search(words):
        return jsonify({'error': "Try-on isn't available for swimwear or lingerie looks.", 'kind': 'not_allowed'}), 400
    photo = _jpeg_url(data, 1536)
    if not photo:
        return jsonify({'error': "That file doesn't look like a photo. Try a JPG or PNG."}), 400
    ticket = reserve()
    if not ticket['ok']:
        if ticket['why'] == 'busy':
            return jsonify({'error': "Try-on is very busy today. Please come back tomorrow.", 'kind': 'busy'}), 429
        return jsonify({'error': 'That was your free try-on for today.' if not ticket['pro'] else "That's today's try-ons used up.",
                        'kind': 'limit', 'pro': ticket['pro'], 'signed_in': ticket['signed_in']}), 429
    _tidy()
    job = secrets.token_urlsafe(18)
    _, who = identity()
    with _jobs_lock:
        _jobs[job] = {'state': 'working', 'who': who, 'at': time.time()}
    try:
        from growth import track
        track('tryon', who)
    except Exception:
        pass
    _pool.submit(_run, job, photo, items, who, ticket)
    return jsonify({'job': job, 'left': ticket.get('left')})


@bp.get('/api/try-on/<job>')
def collect(job):
    """The picture, once (then it's gone), only to the person who asked for it."""
    _, who = identity()
    with _jobs_lock:
        j = _jobs.get(job)
        if not j or j['who'] != who:
            return jsonify({'error': 'not found'}), 404
        if j['state'] == 'working':
            return jsonify({'state': 'working'})
        _jobs.pop(job, None)
    if j['state'] == 'done':
        return jsonify({'state': 'done', 'image': j['image']})
    return jsonify({'state': 'failed', 'error': j['error'], 'kind': j['kind']})
