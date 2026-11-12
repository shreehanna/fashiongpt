"""Where's it from: a screenshot, or a link to a post (Instagram, TikTok, Pinterest, YouTube), and every piece of the
outfit in it found in a real shop.

1. The picture: the screenshot itself, or the post's own picture, fetched from that app (only those apps' own
   addresses are ever fetched: Instagram through its public embed page, TikTok through its public oEmbed).
2. Claude looks at it: every piece, where it sits in the picture, and its brand only when a logo, a design the brand
   is known for, or the caption says so.
3. Each piece is cut out and searched with Google Lens (through Serper): shops selling that exact thing, with their
   own name, price and photo.
4. Nothing on Lens: the product search (shop.resolve), else a Google search for it.

The cut-outs are put up at an unguessable address for 15 minutes so Lens can fetch them, then dropped. Nothing is
stored except the finished answer for that picture (so the same screenshot doesn't cost twice).
"""
import base64
import hashlib
import html as htmllib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import requests as req
from flask import Blueprint, Response, abort, jsonify, request

from pro import client_ip, kv_get, kv_set, metered, owner_request

bp = Blueprint('identify', __name__)
_ai = {}

MAX_PIECES = 8
HOSTED_SECONDS = 15 * 60
CACHE_SECONDS = 7 * 86400
PER_IP_HOUR = 20                    # pictures per visitor per hour (each uses Lens searches)
BROWSER = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/126.0.0.0 Safari/537.36')
FB_BOT = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'
SHOT = re.compile(r'^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$')

INSTA = re.compile(r'^https?://(?:www\.|m\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,40})', re.I)
TIKTOK = re.compile(r'^https?://(?:(?:www|m|vm|vt)\.)?tiktok\.com/', re.I)
PIN = re.compile(r'^https?://(?:(?:[a-z]{2,3}|www)\.)?pinterest\.[a-z.]{2,6}/pin/|^https?://pin\.it/', re.I)
YT = re.compile(r'(?:youtube\.com/(?:shorts/|watch\?(?:.*&)?v=|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})', re.I)

# the only places a link can make this server fetch anything
PAGE_HOSTS = ('instagram.com', 'tiktok.com', 'pinterest.com', 'pinterest.co.uk', 'pinterest.ca', 'pinterest.com.au',
              'pinterest.fr', 'pinterest.de', 'pinterest.es', 'pinterest.it', 'pin.it', 'in.pinterest.com')
IMAGE_HOSTS = ('cdninstagram.com', 'fbcdn.net', 'tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com', 'ibyteimg.com',
               'muscdn.com', 'pinimg.com', 'ytimg.com')


def init(client, model):
    _ai['client'], _ai['model'] = client, model


def env(k):
    return (os.environ.get(k) or '').strip()


# ── Getting the picture ──────────────────────────────────────────────────────
def _allowed_host(url, hosts):
    try:
        u = urlparse(url)
        h = (u.hostname or '').lower()
        if u.scheme != 'https' or u.port not in (None, 443) or not any(h == d or h.endswith('.' + d) for d in hosts):
            return False
        return all(ipaddress.ip_address(i[4][0]).is_global for i in socket.getaddrinfo(h, None))
    except Exception:
        return False


def _get(url, hosts, ua=BROWSER, limit=2_000_000, accept=None):
    """GET from an allowed host only, following redirects only to allowed hosts. (bytes, content type) or (None, '')."""
    for _ in range(4):
        if not _allowed_host(url, hosts):
            return None, ''
        try:
            r = req.get(url, timeout=10, allow_redirects=False, stream=True,
                        headers={'User-Agent': ua, 'Accept-Language': 'en-GB,en;q=0.9', **({'Accept': accept} if accept else {})})
        except req.RequestException:
            return None, ''
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('Location'):
            loc = r.headers['Location']
            url = loc if loc.startswith('http') else f'https://{urlparse(url).hostname}{loc}'
            continue
        if r.status_code != 200:
            return None, ''
        body = r.raw.read(limit + 1, decode_content=True)
        return (body if len(body) <= limit else None), (r.headers.get('Content-Type') or '')
    return None, ''


def _meta(html_text, prop):
    m = (re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]*)"', html_text) or
         re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{prop}"', html_text))
    return htmllib.unescape(m.group(1)) if m else ''


def _text(fragment):
    return ' '.join(htmllib.unescape(re.sub(r'<[^>]+>', ' ', fragment or '')).split())


def _instagram(kind, code):
    """(picture url, caption) from Instagram's public embed page, else the post's own preview tags."""
    for url, ua in ((f'https://www.instagram.com/p/{code}/embed/captioned/', BROWSER),
                    (f'https://www.instagram.com/{"reel" if kind.startswith("reel") else "p"}/{code}/', FB_BOT)):
        raw, _ = _get(url, ('instagram.com',), ua=ua)
        if not raw:
            continue
        page = raw.decode('utf-8', 'replace')
        img = ''
        m = re.search(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', page) or re.search(r'src="([^"]+)"[^>]*class="EmbeddedMediaImage"', page)
        if m:
            img = htmllib.unescape(m.group(1))
        if not img:
            m = re.search(r'"display_url"\s*:\s*"([^"]+)"', page)
            img = m.group(1).encode().decode('unicode_escape').replace('\\/', '/') if m else ''
        img = img or _meta(page, 'og:image')
        cap = ''
        m = re.search(r'<div class="Caption">(.*?)</div>', page, re.S)
        if m:
            cap = _text(re.sub(r'<a class="CaptionUsername".*?</a>', ' ', m.group(1), flags=re.S))
        cap = cap or _meta(page, 'og:description') or _meta(page, 'og:title')
        if img:
            return img, cap[:700]
    return None, ''


def _tiktok(link):
    raw, _ = _get('https://www.tiktok.com/oembed?url=' + quote(link, safe=''), ('tiktok.com',))
    try:
        d = json.loads(raw or b'{}')
    except ValueError:
        return None, ''
    return d.get('thumbnail_url'), f"{d.get('title') or ''} (by {d.get('author_name') or 'someone'})"[:700]


def _pinterest(link):
    raw, _ = _get(link, PAGE_HOSTS)
    if not raw:
        return None, ''
    page = raw.decode('utf-8', 'replace')
    return _meta(page, 'og:image') or None, (_meta(page, 'og:description') or _meta(page, 'og:title'))[:700]


def picture_from_link(link):
    """(picture url, caption, where it's from) for a post link, or (None, '', reason)."""
    link = (link or '').strip()
    m = INSTA.match(link)
    if m:
        img, cap = _instagram(m.group(1).lower(), m.group(2))
        return (img, cap, 'Instagram') if img else (None, '', 'instagram')
    if TIKTOK.match(link):
        img, cap = _tiktok(link)
        return (img, cap, 'TikTok') if img else (None, '', 'tiktok')
    if PIN.match(link):
        img, cap = _pinterest(link)
        return (img, cap, 'Pinterest') if img else (None, '', 'pinterest')
    m = YT.search(link)
    if m:
        return f'https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg', '', 'YouTube'
    return None, '', 'unsupported'


def _jpeg(raw, side=1600):
    """Any picture as a JPEG at most `side` pixels wide or tall (a PIL image and its bytes), or (None, None)."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert('RGB')
    except Exception:
        return None, None
    im.thumbnail((side, side))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=88)
    return im, out.getvalue()


# ── The cut-outs Lens looks at ───────────────────────────────────────────────
_hosted = {}
_hosted_lock = threading.Lock()


def _host_image(jpeg, base):
    """The cut-out at {base}/lens/<unguessable>.jpg for 15 minutes. base: this app's public address (worked out in
    the request: the searches run in other threads)."""
    token = secrets.token_hex(16)
    with _hosted_lock:
        now = time.time()
        for k in [k for k, (t, _) in _hosted.items() if t < now]:
            del _hosted[k]
        while len(_hosted) > 300:
            del _hosted[next(iter(_hosted))]
        _hosted[token] = (now + HOSTED_SECONDS, jpeg)
    return f'{base}/lens/{token}.jpg'


@bp.get('/lens/<token>.jpg')
def hosted(token):
    with _hosted_lock:
        hit = _hosted.get(token)
    if not hit or hit[0] < time.time():
        abort(404)
    return Response(hit[1], mimetype='image/jpeg', headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})


def _crop(im, box):
    """The piece cut out of the picture, with a little room around it. box: [left, top, right, bottom] in 0-1000."""
    try:
        l, t, r, b = [max(0.0, min(1000.0, float(v))) / 1000 for v in box]
    except (TypeError, ValueError):
        return None
    if r - l < 0.03 or b - t < 0.03:
        return None
    pw, ph = (r - l) * 0.1, (b - t) * 0.1
    w, h = im.size
    c = im.crop((int(max(0, l - pw) * w), int(max(0, t - ph) * h), int(min(1, r + pw) * w), int(min(1, b + ph) * h)))
    if min(c.size) < 40:
        return None
    out = io.BytesIO()
    c.save(out, 'JPEG', quality=88)
    return out.getvalue()


def _thumb(jpeg, side=360):
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg))
    im.thumbnail((side, side))
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=80)
    return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()


# ── Claude: what's in the picture ────────────────────────────────────────────
def _json(text):
    t = (text or '').strip()
    if '```' in t:
        t = t.split('```json')[1] if '```json' in t else t.split('```')[1]
        t = t.split('```')[0]
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r'\{.*\}', t, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            return {}


def read_outfit(jpeg, caption=''):
    """[{'category', 'description', 'brand', 'product', 'box', 'why_brand'}] for every piece, the clearest first."""
    prompt = (
        "Look at this outfit picture" + (f". Its caption: \"{caption}\"" if caption else '') + ".\n"
        f"List every clothing item and accessory the main person is wearing (at most {MAX_PIECES}, the most visible "
        "first; skip anything you can't see clearly). For each piece:\n"
        "- \"category\": like Dress, Blazer, Trousers, Shoes, Bag, Sunglasses, Earrings\n"
        "- \"description\": colour, fabric, pattern and cut in plain words, the way a shop would list it "
        "(\"ivory linen wide-leg trousers\")\n"
        "- \"brand\": ONLY if you can see its logo or monogram, it's a design that brand is famous for, or the caption "
        "names or tags it (like @zimmermann); otherwise \"\"\n"
        "- \"product\": its exact product name ONLY if you're certain (like \"Le Bambino\"); otherwise \"\"\n"
        "- \"why_brand\": what tells you the brand (\"the caption tags @zimmermann\", \"interlocking CC logo\"), or \"\"\n"
        "- \"box\": where the piece is in the picture: [left, top, right, bottom], each from 0 to 1000\n"
        'Return ONLY valid JSON: {"items": [...]}. If there\'s no outfit in the picture, return {"items": []}.')
    msg = _ai['client'].messages.create(
        model=_ai['model'], max_tokens=1800,
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(jpeg).decode()}},
            {'type': 'text', 'text': prompt}]}])
    out = []
    for it in (_json(msg.content[0].text).get('items') or [])[:MAX_PIECES]:
        if isinstance(it, dict) and it.get('description'):
            out.append({'category': str(it.get('category') or 'Piece')[:40], 'description': str(it['description'])[:140],
                        'brand': str(it.get('brand') or '')[:60], 'product': str(it.get('product') or '')[:80],
                        'why_brand': str(it.get('why_brand') or '')[:120], 'box': it.get('box')})
    return out


# ── Google Lens: the shops selling that exact piece ──────────────────────────
def _lens(image_url, gl):
    key = env('SERPER_API_KEY')
    if not key:
        return None
    try:
        r = req.post('https://google.serper.dev/lens', timeout=25, json={'url': image_url, 'gl': gl, 'hl': 'en'},
                     headers={'X-API-KEY': key, 'Content-Type': 'application/json'})
    except req.RequestException as e:
        print('[identify] lens failed:', e)
        return None
    try:
        from alerts import search_result
        search_result(r)
    except Exception:
        pass
    if r.status_code != 200:
        print(f'[identify] lens refused: {r.status_code} {r.text[:120]!r}')
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    rows = []
    for k in ('organic', 'visualMatches', 'visual_matches', 'products', 'shopping', 'images'):
        for o in data.get(k) or []:
            if isinstance(o, dict) and (o.get('link') or o.get('url')):
                rows.append({'title': str(o.get('title') or ''), 'link': str(o.get('link') or o.get('url')),
                             'source': str(o.get('source') or ''), 'price': str(o.get('price') or ''),
                             'image': str(o.get('imageUrl') or o.get('thumbnailUrl') or o.get('image') or '')})
    return rows


def _rank(rows, piece):
    """Lens results that could be this piece, best first: [(row, branded, agree)]. A product page from a shop, the
    same kind of thing, its brand first when the brand is known."""
    import shop as S
    want = f"{piece['category']} {piece['description']}"
    kinds = set(S._families(S._tokens(want)))
    brand_keys = S._keys(piece['brand']) if piece['brand'] else []
    scored = []
    for pos, row in enumerate(rows):
        link, title = row['link'], row['title']
        host = S._host(link) if link.startswith('https://') else ''
        if not host or S.NOT_SHOPS.search(host) or S._EDITORIAL.search(host) or S.NOT_PRODUCT.search(link):
            continue
        path_words = urlparse(link).path.replace('-', ' ').replace('_', ' ').replace('/', ' ')
        got = S._tokens(f'{title} {path_words}')
        if kinds and not set(S._families(got)) & kinds:
            continue                                    # a bag for the dress
        if S._conflicts(want, f'{title} {path_words}'):
            continue                                    # a mini dress for the gown, a charm for the bag
        if not (row['price'] or S._is_product_url(link, None)):
            continue                                    # not a shop's product page
        branded = bool(brand_keys) and any(len(k) >= 3 and k in S._norm(f'{title} {link}') for k in brand_keys)
        scored.append((-(2 * branded + bool(row['price'])), pos, row, branded))
    scored.sort(key=lambda x: (x[0], x[1]))
    out = []
    for i, (_, _, row, branded) in enumerate(scored):
        name = S._tokens(row['title'])
        # several shops showing the same product: it's that exact piece
        agree = sum(1 for j, (_, _, r, _) in enumerate(scored[:6]) if j != i and name
                    and len(name & S._tokens(r['title'])) / len(name) >= 0.6)
        out.append((row, branded, agree))
    return out


def find_piece(piece, im, gl, base):
    """Where one piece is from: {'name', 'brand', 'url', 'source', 'price', 'image', 'match', 'google', 'crop'}.
    match: 'exact' (its brand, or several shops showing the same product), 'similar', or 'search' (no shop page)."""
    import shop as S
    out = {'category': piece['category'], 'description': piece['description'], 'brand': piece['brand'],
           'why_brand': piece['why_brand'], 'name': '', 'url': '', 'source': '', 'price': '', 'image': '',
           'match': 'search', 'crop': '', 'sold_out': False}
    crop = _crop(im, piece.get('box')) if piece.get('box') else None
    if crop:
        out['crop'] = _thumb(crop)
        rows = _lens(_host_image(crop, base), gl) or []
        for row, branded, agree in _rank(rows, piece)[:3]:
            c = S.check_page(row['link'], None, gl)
            if c['state'] == 'dead':
                continue                                # a page that's gone: the next shop
            img = row['image'] if row['image'].startswith('https://') else (c.get('image') or '')
            out.update(name=S.product_name(row['title'], row['link'], piece['brand']) or row['title'][:90],
                       url=c.get('final') or row['link'], source=row['source'] or S._host(row['link']),
                       price=row['price'], image=img, sold_out=bool(c.get('sold_out')),
                       match='exact' if (branded or agree >= 1) else 'similar')
            break
    if not out['url'] and piece['brand']:            # nothing on Lens: the product search for that brand
        r = S.resolve(piece['brand'], (piece['product'] + ' ' + piece['description']).strip(), gl.upper()) or {}
        if r.get('url'):
            out.update(name=r.get('title') or piece['description'], url=r['url'], source=S._host(r['url']),
                       image=r.get('image') or '', match='similar')
        elif r.get('name'):
            out['name'] = r['name']
    q = f"{piece['brand']} {out['name'] or piece['product'] or piece['description']}".strip()
    out['google'] = 'https://www.google.com/search?q=' + quote(q) + ('' if out['url'] else '&tbm=shop')
    return out


# ── The endpoint ─────────────────────────────────────────────────────────────
_hits = {}


def _allowed(ip):
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= PER_IP_HOUR:
        return False
    q.append(now)
    if len(_hits) > 5000:
        _hits.clear()
    return True


WHY_NOT = {
    'instagram': "Instagram didn't let Ella see that post (it may be private, or Instagram is blocking us right now). "
                 "Take a screenshot of the outfit and upload it instead.",
    'tiktok': "TikTok didn't share that video's picture. Take a screenshot of the outfit and upload it instead.",
    'pinterest': "Pinterest didn't share that pin's picture. Take a screenshot and upload it instead.",
    'unsupported': 'Paste an Instagram, TikTok, Pinterest or YouTube link, or upload a screenshot.',
}


@bp.post('/api/identify')
@metered
def identify():
    """{'pieces': [...], 'source': 'Instagram'|'Screenshot'|..., 'caption'} for a screenshot or a post link."""
    if not _ai.get('client'):
        return jsonify({'error': 'Ella is offline right now.'}), 503
    if not owner_request() and not _allowed(client_ip()):
        return jsonify({'error': 'That’s a lot of outfits! Try again in a little while.'}), 429
    body = request.get_json(silent=True) or {}
    from shop import country_code
    gl = country_code(body).lower()
    caption, where = '', 'Screenshot'
    shot = str(body.get('image') or '')
    if shot:
        m = SHOT.match(shot)
        if not m or len(shot) > 6_000_000:
            return jsonify({'error': 'That picture didn’t come through. Try a smaller screenshot.'}), 400
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return jsonify({'error': 'That picture didn’t come through. Try again.'}), 400
    else:
        img_url, caption, where = picture_from_link(str(body.get('link') or '')[:500])
        if not img_url:
            return jsonify({'error': WHY_NOT.get(where, WHY_NOT['unsupported']), 'need_screenshot': True}), 422
        raw, ctype = _get(img_url, IMAGE_HOSTS, limit=8_000_000, accept='image/*')
        if not raw:
            return jsonify({'error': WHY_NOT.get(where.lower(), WHY_NOT['unsupported']), 'need_screenshot': True}), 422
    im, jpeg = _jpeg(raw)
    if not jpeg:
        return jsonify({'error': 'That isn’t a picture Ella can read. Try a screenshot.'}), 400
    # the same picture with the same caption (its brand tags change the answer), for the same country
    key = 'identify:' + hashlib.sha256(jpeg + gl.encode() + b'|' + caption.encode()).hexdigest()[:40]
    try:
        hit = json.loads(kv_get(key) or 'null')
    except Exception:
        hit = None
    if hit and time.time() - hit.get('at', 0) < CACHE_SECONDS:
        return jsonify(dict(hit['answer'], source=where))
    try:
        pieces = read_outfit(jpeg, caption)
    except Exception as e:
        print('[identify] reading the picture failed:', repr(e))
        return jsonify({'error': 'Ella couldn’t look at that picture just now. Try again in a minute.'}), 503
    if not pieces:
        return jsonify({'error': 'Ella couldn’t see an outfit in that picture. Try a clearer screenshot of the person.'}), 422
    from growth import home_url
    base = home_url()
    with ThreadPoolExecutor(max_workers=min(MAX_PIECES, len(pieces))) as pool:
        found = list(pool.map(lambda p: find_piece(p, im, gl, base), pieces))
    answer = {'pieces': found, 'caption': caption[:300]}
    try:
        kv_set(key, json.dumps({'at': time.time(), 'answer': answer}))
    except Exception:
        pass
    try:
        from growth import track
        from pro import identity
        track('identify', identity()[1])
    except Exception:
        pass
    return jsonify(dict(answer, source=where))
