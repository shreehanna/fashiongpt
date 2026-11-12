"""Growth: looks people share ("Rate my fit"), boards, the weekly challenge, and our own stats.

How a share works
  1. Every look Ella makes is signed by the server (sign_look), so only real E&L looks can get a public link.
  2. The app sends the signed look to /api/cards and gets back /l/<id>.
  3. /l/<id> is a page anyone can open without signing up: the look, three vote buttons (fire, cute, not for me)
     and "Get your own look". WhatsApp and iMessage show /l/<id>/preview.jpg; the app shares /l/<id>/story.png
     straight to an Instagram Story.
Every public page has a Report button; reports alert the owner and three hide the page until the owner checks it.

Stats are counted here too (track): who opened the app each day (as a one-way hash, never an email), and how
often people make looks, share, vote and come back. The owner sees them in Owner tools.
"""
import base64
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, redirect, request, session

from pro import IST, client_ip, db, env, get_user, identity, kv_get, kv_set, now, owner_request, today

bp = Blueprint('growth', __name__)
SERIES = 'The Social Representation'


# ── Stats ─────────────────────────────────────────────────────────────────────
CLIENT_EVENTS = {
    'open', 'standalone', 'install', 'share_open', 'share_story', 'share_copy', 'share_save', 'daily_open',
    'push_on', 'wedding_open', 'wedding_look', 'edit_open', 'edit_look', 'reels_open', 'reel_look', 'challenge_open',
    'challenge_enter', 'board_create', 'board_share', 'ref_open', 'paywall_limit', 'paywall_open', 'closet_add',
    'character_pick', 'can_open', 'can_try', 'tour_open', 'tour_done', 'tour_try'}
SERVER_EVENTS = {'signin', 'look', 'card_create', 'card_view', 'vote', 'cta', 'daily_fit', 'push_sent', 'report', 'identify'}
_event_budget = {}
_budget_lock = threading.Lock()


def hash_who(who):
    """A stable, one-way id for counting people without keeping who they are."""
    key = (env('SECRET_KEY') or 'el').encode()
    return hmac.new(key, (who or '').encode(), hashlib.sha256).hexdigest()[:16]


def track(event, who=None, n=1):
    if event not in CLIENT_EVENTS and event not in SERVER_EVENTS:
        return
    try:
        day = today()
        db('INSERT INTO el_stats (day, event, n) VALUES (%s, %s, %s) '
           'ON CONFLICT (day, event) DO UPDATE SET n = el_stats.n + excluded.n', (day, event, int(n)))
        if who:
            db('INSERT INTO el_active (who, day) VALUES (%s, %s) ON CONFLICT DO NOTHING', (hash_who(who), day))
    except Exception as e:
        print('[growth] stat skipped:', e)


def _within_budget(key, limit, window=86400):
    t = time.time()
    with _budget_lock:
        q = _event_budget.setdefault(key, deque())
        while q and t - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        if len(_event_budget) > 20000:            # keep memory small
            for k in list(_event_budget)[:5000]:
                _event_budget.pop(k, None)
        return True


@bp.post('/api/track')
def track_event():
    body = request.get_json(silent=True) or {}
    event = str(body.get('event') or '')
    if event not in CLIENT_EVENTS:
        return jsonify({'ok': False}), 400
    who = identity()[1]
    if _within_budget('t:' + who, 300):
        track(event, who)
    return jsonify({'ok': True})


# ── Signed looks ──────────────────────────────────────────────────────────────
ITEM_KEYS = ('category', 'name', 'brand', 'price', 'store', 'description')
NOTE_KEYS = ('accessories', 'hair', 'makeup', 'fragrance')
META_KEYS = ('occasion', 'muse', 'era', 'stylist', 'gender', 'kind')


def _s(v, n):
    return str(v if v is not None else '')[:n]


def card_payload(look, meta):
    items = [{k: _s(i.get(k), 200) for k in ITEM_KEYS}
             for i in (look.get('items') or [])[:14] if isinstance(i, dict) and i.get('name')]
    sn = look.get('styling_notes') if isinstance(look.get('styling_notes'), dict) else {}
    return {'look_name': _s(look.get('look_name'), 120), 'ella_note': _s(look.get('ella_note'), 700),
            'items': items, 'styling_notes': {k: _s(sn.get(k), 400) for k in NOTE_KEYS if sn.get(k)},
            'meta': {k: _s(meta.get(k), 120) for k in META_KEYS}}


def _sig(payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    key = (env('SECRET_KEY') or 'el-dev').encode()
    return hmac.new(key, b'look|' + raw, hashlib.sha256).hexdigest()[:40]


def sign_look(look, data=None, kind='look'):
    """Add card_meta and sig to a look Ella just made, so the app can share it later."""
    if not isinstance(look, dict):
        return look
    data = data or {}
    stylist = data.get('stylist') if data.get('stylist') in ('ella', 'leon') else 'ella'
    occasion = data.get('occasion') or data.get('new_occasion') or data.get('anchor_item') or ''
    if data.get('mode') == 'dupe':
        occasion = 'a dupe of ' + str(data.get('inspo_look') or 'a look')
    meta = {'occasion': _s(occasion, 120), 'muse': _s(data.get('celebrity') or data.get('celeb') or '', 120),
            'era': _s(data.get('era') or '', 120), 'stylist': stylist,
            'gender': _s(data.get('gender') or 'women', 12), 'kind': kind}
    if meta['muse'].lower() in ('no specific muse', 'none'):
        meta['muse'] = ''
    look['card_meta'] = card_payload(look, meta)['meta']
    look['sig'] = _sig(card_payload(look, look['card_meta']))
    return look


def verify_look(look):
    """The look's public content if the server really made it, else None."""
    if not isinstance(look, dict) or not isinstance(look.get('card_meta'), dict) or not isinstance(look.get('sig'), str):
        return None
    payload = card_payload(look, look['card_meta'])
    if not payload['items'] or not hmac.compare_digest(_sig(payload), look['sig'][:40]):
        return None
    return payload


# ── Shared looks: /l/<id> ─────────────────────────────────────────────────────
def home_url():
    home = env('PUBLIC_URL').rstrip('/') or request.host_url.rstrip('/')
    if env('RENDER') and home.startswith('http://'):
        home = 'https://' + home[len('http://'):]
    return home


def _card_id(sig, who):
    return base64.urlsafe_b64encode(hashlib.sha256(f'{sig}|{who}'.encode()).digest()).decode()[:10]


def _valid_id(cid):
    return isinstance(cid, str) and len(cid) == 10 and all(c.isalnum() or c in '-_' for c in cid)


CARD_COLS = ['id', 'who', 'created_at', 'kind', 'look', 'name', 'challenge', 'fire', 'cute', 'hmm', 'views',
             'reports', 'hidden', 'last_ping']


def get_card(cid):
    if not _valid_id(cid):
        return None
    row = db(f"SELECT {', '.join(CARD_COLS)} FROM el_cards WHERE id = %s", (cid,), one=True)
    if not row:
        return None
    c = dict(zip(CARD_COLS, row))
    try:
        c['look'] = json.loads(c['look'] or '{}')
    except ValueError:
        c['look'] = {}
    return c


def first_name():
    email = session.get('email')
    if not email:
        return ''
    try:
        u = get_user(email)
    except Exception:
        u = None
    name = ((u or {}).get('name') or '').strip().split(' ')[0]
    return name[:20]


def week_key(t=None):
    d = datetime.fromtimestamp(t or time.time(), IST).date()
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


@bp.post('/api/cards')
def create_card():
    body = request.get_json(silent=True) or {}
    payload = verify_look(body.get('look'))
    if not payload:
        return jsonify({'error': "This look was made before vote links existed, so it can't get one. "
                                 "Make a new look and share that."}), 400
    who = identity()[1]
    if not _within_budget('c:' + who, 80):
        return jsonify({'error': "That's a lot of sharing for one day. Try again tomorrow."}), 429
    cid = _card_id(body['look']['sig'], who)
    name = first_name() if body.get('show_name') else ''
    challenge = week_key() if body.get('challenge') else ''
    kind = payload['meta'].get('kind') or 'look'
    existing = db('SELECT challenge FROM el_cards WHERE id = %s', (cid,), one=True)
    if existing:
        db('UPDATE el_cards SET name = %s, challenge = %s WHERE id = %s',
           (name, challenge or existing[0] or '', cid))
    else:
        db('INSERT INTO el_cards (id, who, created_at, kind, look, name, challenge) VALUES (%s, %s, %s, %s, %s, %s, %s)',
           (cid, who, now(), kind, json.dumps(payload, ensure_ascii=False), name, challenge))
        track('card_create', who)
    url = f'{home_url()}/l/{cid}'
    return jsonify({'id': cid, 'url': url, 'story': f'/l/{cid}/story.png', 'challenge': challenge})


def _counts(c):
    return {'fire': int(c['fire']), 'cute': int(c['cute']), 'hmm': int(c['hmm'])}


@bp.get('/api/cards/mine')
def my_cards():
    who = identity()[1]
    rows = db('SELECT id, created_at, look, fire, cute, hmm, views, challenge, hidden FROM el_cards '
              'WHERE who = %s ORDER BY created_at DESC LIMIT 40', (who,), many=True) or []
    out = []
    for cid, at, look, fire, cute, hmm, views, ch, hidden in rows:
        try:
            name = json.loads(look or '{}').get('look_name') or 'My look'
        except ValueError:
            name = 'My look'
        out.append({'id': cid, 'created_at': at, 'look_name': name, 'fire': fire, 'cute': cute, 'hmm': hmm,
                    'views': views, 'challenge': ch, 'hidden': bool(hidden)})
    return jsonify({'cards': out})


@bp.delete('/api/cards/<cid>')
def delete_card(cid):
    """Take a shared look offline (only the person who shared it can)."""
    c = get_card(cid)
    if not c or c['who'] != identity()[1]:
        return jsonify({'error': 'not found'}), 404
    db('DELETE FROM el_votes WHERE card_id = %s', (cid,))
    db('DELETE FROM el_cards WHERE id = %s', (cid,))
    with _img_lock:
        _img_cache.pop(('s', cid), None)
        _img_cache.pop(('p', cid), None)
    return jsonify({'ok': True})


@bp.delete('/api/boards/<bid>')
def delete_board(bid):
    if not _valid_id(bid):
        return jsonify({'error': 'not found'}), 404
    row = db('SELECT who FROM el_boards WHERE id = %s', (bid,), one=True)
    if not row or row[0] != identity()[1]:
        return jsonify({'error': 'not found'}), 404
    db('DELETE FROM el_boards WHERE id = %s', (bid,))
    return jsonify({'ok': True})


@bp.get('/api/cards/<cid>')
def card_json(cid):
    c = get_card(cid)
    if not c or c['hidden']:
        return jsonify({'error': 'not found'}), 404
    mine = db('SELECT vote FROM el_votes WHERE card_id = %s AND voter = %s', (cid, identity()[1]), one=True)
    return jsonify({'id': cid, 'counts': _counts(c), 'views': c['views'], 'mine': mine[0] if mine else None,
                    'owner': c['who'] == identity()[1]})


VOTES = ('fire', 'cute', 'hmm')


@bp.post('/api/cards/<cid>/vote')
def vote(cid):
    c = get_card(cid)
    if not c or c['hidden']:
        return jsonify({'error': 'not found'}), 404
    v = (request.get_json(silent=True) or {}).get('vote')
    if v not in VOTES:
        return jsonify({'error': 'pick fire, cute or hmm'}), 400
    voter = identity()[1]
    ip = hash_who('ip:' + client_ip())
    if voter == c['who']:
        return jsonify({'error': "You can't vote on your own fit.", 'counts': _counts(c)}), 403
    if not _within_budget('v:' + ip, 60, 600):
        return jsonify({'error': 'Slow down a little.'}), 429
    had = db('SELECT vote FROM el_votes WHERE card_id = %s AND voter = %s', (cid, voter), one=True)
    if not had:
        same_ip = db('SELECT COUNT(*) FROM el_votes WHERE card_id = %s AND ip = %s', (cid, ip), one=True)
        if same_ip and int(same_ip[0]) >= 8:            # a whole hostel on one wifi is fine; a script isn't
            return jsonify({'error': 'Too many votes from this network.', 'counts': _counts(c)}), 429
        db('INSERT INTO el_votes (card_id, voter, vote, ip, at) VALUES (%s, %s, %s, %s, %s) '
           'ON CONFLICT (card_id, voter) DO UPDATE SET vote = excluded.vote, at = excluded.at', (cid, voter, v, ip, now()))
    else:
        db('UPDATE el_votes SET vote = %s, at = %s WHERE card_id = %s AND voter = %s', (v, now(), cid, voter))
    rows = db('SELECT vote, COUNT(*) FROM el_votes WHERE card_id = %s GROUP BY vote', (cid,), many=True) or []
    counts = {k: 0 for k in VOTES}
    counts.update({k: int(n) for k, n in rows if k in counts})
    db('UPDATE el_cards SET fire = %s, cute = %s, hmm = %s WHERE id = %s',
       (counts['fire'], counts['cute'], counts['hmm'], cid))
    if not had:
        track('vote', voter)
        _ping_owner(c, v, counts)
    return jsonify({'counts': counts, 'mine': v})


def _ping_owner(c, v, counts):
    """Tell the person who shared it, at most once an hour per look."""
    if c['last_ping'] and now() - int(c['last_ping']) < 3600:
        return
    db('UPDATE el_cards SET last_ping = %s WHERE id = %s', (now(), c['id']))
    name = (c['look'] or {}).get('look_name') or 'your fit'
    total = sum(counts.values())
    title = {'fire': '🔥 Someone rated your fit', 'cute': '😍 Someone thinks your fit is cute',
             'hmm': 'A new vote on your fit'}[v]
    try:
        from push import notify_who
        notify_who(c['who'], {'title': title, 'body': f'{name}: {total} vote{"s" if total != 1 else ""} so far. Tap to see.',
                              'url': f"/l/{c['id']}", 'tag': 'vote-' + c['id'], 'kind': 'votes'})
    except Exception as e:
        print('[growth] vote ping skipped:', e)


_reported = {}


@bp.post('/api/report/<kind>/<cid>')
def report(kind, cid):
    table = {'l': 'el_cards', 'b': 'el_boards'}.get(kind)
    if not table or not _valid_id(cid):
        return jsonify({'error': 'not found'}), 404
    key = (kind, cid, hash_who('ip:' + client_ip()))
    if key in _reported:
        return jsonify({'ok': True})
    if len(_reported) > 5000:
        _reported.clear()
    _reported[key] = time.time()
    reason = str((request.get_json(silent=True) or {}).get('reason') or '')[:300]
    row = db(f'SELECT reports FROM {table} WHERE id = %s', (cid,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    n = int(row[0]) + 1
    db(f'UPDATE {table} SET reports = %s, hidden = CASE WHEN %s >= 3 THEN 1 ELSE hidden END WHERE id = %s', (n, n, cid))
    track('report')
    try:
        from alerts import later, send
        later(send, f'E&L: a shared page was reported ({n})',
              f"{home_url()}/{kind}/{cid} was reported{': ' + reason if reason else ''}. "
              f"{'It is now hidden. ' if n >= 3 else ''}Check it in Owner tools.", n >= 3)
    except Exception as e:
        print('[growth] report alert failed:', e)
    return jsonify({'ok': True})


@bp.get('/l/<cid>/go')
def card_go(cid):
    if _valid_id(cid):
        track('cta', identity()[1])
        return redirect(f'/?ref={cid}', 302)
    return redirect('/', 302)


_img_cache = OrderedDict()
_img_lock = threading.Lock()


def _cached_image(key, make):
    with _img_lock:
        if key in _img_cache:
            _img_cache.move_to_end(key)
            return _img_cache[key]
    data = make()
    with _img_lock:
        _img_cache[key] = data
        while len(_img_cache) > 48:
            _img_cache.popitem(last=False)
    return data


def _art_card(c):
    look = c['look'] or {}
    return {'look_name': look.get('look_name'), 'ella_note': look.get('ella_note'), 'items': look.get('items'),
            'meta': look.get('meta') or {}}


def _link_text(cid):
    host = home_url().split('://', 1)[-1]
    return f'{host}/l/{cid}'


@bp.get('/l/<cid>/story.png')
def card_story(cid):
    c = get_card(cid)
    if not c or c['hidden']:
        return Response('not found', 404)
    import cards_art
    data = _cached_image(('s', cid), lambda: cards_art.story(_art_card(c), _link_text(cid)))
    return Response(data, mimetype='image/png', headers={'Cache-Control': 'public, max-age=86400'})


@bp.get('/l/<cid>/preview.jpg')
def card_preview(cid):
    c = get_card(cid)
    if not c or c['hidden']:
        return Response('not found', 404)
    import cards_art
    data = _cached_image(('p', cid), lambda: cards_art.preview(_art_card(c)))
    return Response(data, mimetype='image/jpeg', headers={'Cache-Control': 'public, max-age=86400'})


BOTS = ('bot', 'crawler', 'spider', 'facebookexternalhit', 'whatsapp', 'slack', 'telegram', 'discord', 'preview',
        'embedly', 'skype', 'linkedin')


def _is_bot():
    ua = (request.headers.get('User-Agent') or '').lower()
    return any(b in ua for b in BOTS)


e = html.escape


def _shop_link(it):
    from urllib.parse import quote_plus
    return 'https://www.google.com/search?q=' + quote_plus(f"{it.get('brand', '')} {it.get('name', '')} buy")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta property="og:type" content="website"><meta property="og:site_name" content="E&amp;L">
<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
{og_image}
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#F7F0E3">
<script>(function(){{var p='auto';try{{p=localStorage.getItem('el_theme')||'auto';}}catch(e){{}}var d=p==='dark'||(p==='auto'&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.setAttribute('data-theme',d?'dark':'light');}})();</script>
<link rel="icon" href="/icon-192.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400..700;1,6..96,400..700&family=Jost:wght@300;400;500;600&family=Pinyon+Script&display=swap">
<style>
:root {{ --bg:#F7F0E3; --card:#FBF6EC; --fg:#1E1712; --fg2:#4E4134; --fg3:#6A5A48; --gold:#9A7433; --gold-ink:#7E5F27;
  --line:rgba(154,116,51,.34); --serif:'Bodoni Moda',Didot,'Bodoni 72',Georgia,serif; --sans:'Jost',Futura,system-ui,sans-serif;
  --script:'Pinyon Script','Snell Roundhand',cursive; color-scheme: light; }}
:root[data-theme="dark"] {{ --bg:#14100C; --card:#1C1611; --fg:#F3E9D8; --fg2:#D9CAB0; --fg3:#B5A386; --gold:#D2AE6A;
  --gold-ink:#E3C88F; --line:rgba(210,174,106,.32); color-scheme: dark; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
[hidden] {{ display:none !important; }}
body {{ background:var(--bg); color:var(--fg); font:16px/1.6 var(--sans); padding-block:28px 56px;
  padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px)); }}
main {{ max-width:560px; margin:0 auto; }}
.mark {{ display:block; text-align:center; font:500 44px/1 var(--serif); letter-spacing:.02em; color:var(--fg); text-decoration:none; }}
.mark em {{ font-style:italic; font-weight:400; color:var(--gold); }}
.eyebrow {{ text-align:center; font:500 12px/1.4 var(--sans); letter-spacing:.32em; text-transform:uppercase; color:var(--gold-ink); margin:14px 0 22px; }}
.for {{ text-align:center; font-size:12px; letter-spacing:.16em; text-transform:uppercase; color:var(--fg2); }}
h1 {{ font:500 clamp(34px,9vw,52px)/1.05 var(--serif); text-align:center; margin:10px 0 16px; text-wrap:balance; }}
blockquote {{ font:italic 400 19px/1.5 var(--serif); color:var(--gold-ink); text-align:center; margin:0 auto 26px; max-width:30em; text-wrap:pretty; }}
.vote {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:20px 16px; text-align:center; margin-bottom:26px; }}
.vote h2 {{ font:500 22px/1.2 var(--serif); margin-bottom:14px; }}
.btns {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }}
.btns button {{ font:500 13px/1.2 var(--sans); letter-spacing:.06em; color:var(--fg); background:var(--bg); border:1px solid var(--line);
  border-radius:6px; padding:14px 6px 12px; cursor:pointer; display:flex; flex-direction:column; align-items:center; gap:6px; min-height:84px; }}
.btns button b {{ font-size:30px; line-height:1; font-weight:400; }}
.btns button.on {{ border-color:var(--fg); background:var(--fg); color:var(--bg); }}
.btns button:disabled {{ cursor:default; }}
.bars {{ margin-top:16px; display:grid; gap:8px; text-align:left; }}
.bar {{ display:grid; grid-template-columns:7.5em 1fr 2.5em; align-items:center; gap:10px; font-size:13px; color:var(--fg2); }}
.bar i {{ display:block; height:8px; border-radius:4px; background:rgba(154,116,51,.18); overflow:hidden; }}
.bar i span {{ display:block; height:100%; background:var(--gold); width:0; transition:width .5s ease; }}
.bar em {{ font-style:normal; text-align:right; font-variant-numeric:tabular-nums; }}
.msg {{ margin-top:12px; font-size:14px; color:var(--fg2); min-height:1.4em; }}
.pieces {{ list-style:none; display:grid; gap:10px; margin-bottom:24px; }}
.pieces li {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:14px 16px; display:grid; grid-template-columns:1fr auto; gap:2px 12px; }}
.pieces .cat {{ grid-column:1/-1; font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--fg3); }}
.pieces .nm {{ font:500 19px/1.25 var(--serif); min-width:0; }}
.pieces .pr {{ font-weight:500; color:var(--gold-ink); white-space:nowrap; }}
.pieces .br {{ font-size:14px; color:var(--fg2); }}
.pieces a {{ font-size:13px; color:var(--gold-ink); text-align:right; }}
.notes {{ font-size:15px; color:var(--fg2); display:grid; gap:6px; margin-bottom:28px; }}
.notes b {{ color:var(--fg); font-weight:500; }}
.cta {{ display:block; text-align:center; background:var(--fg); color:var(--bg); text-decoration:none; font:500 14px/1 var(--sans);
  letter-spacing:.18em; text-transform:uppercase; padding:19px 16px; border-radius:2px; }}
.small {{ text-align:center; font-size:13px; color:var(--fg3); margin-top:10px; }}
.sig {{ text-align:center; font:400 40px/1.2 var(--script); color:var(--gold); margin:30px 0 4px; }}
.share {{ display:flex; gap:8px; margin-top:12px; }}
.share input {{ flex:1; min-width:0; font:14px var(--sans); padding:10px 12px; border:1px solid var(--line); border-radius:2px; background:var(--bg); color:var(--fg); }}
.share button, .rep button {{ font:500 12px var(--sans); letter-spacing:.1em; text-transform:uppercase; padding:10px 14px; border:1px solid var(--fg); background:transparent; color:var(--fg); border-radius:2px; cursor:pointer; }}
footer {{ max-width:560px; margin:28px auto 0; text-align:center; font-size:13px; color:var(--fg3); display:grid; gap:8px; }}
footer a, .linkish {{ color:var(--fg3); }}
.linkish {{ background:none; border:0; font:inherit; text-decoration:underline; cursor:pointer; }}
.rep button.linkish {{ border:0; padding:0; font:13px var(--sans); letter-spacing:0; text-transform:none; color:var(--fg3); }}
.rep {{ display:flex; gap:8px; justify-content:center; flex-wrap:wrap; align-items:center; }}
.board-look {{ border-top:1px solid var(--line); padding-top:22px; margin-top:22px; }}
.board-look h2 {{ font:500 28px/1.1 var(--serif); text-align:center; margin-bottom:10px; }}
</style></head><body>
<main>
<a class="mark" href="{home_link}">E<em>&amp;</em>L</a>
{body}
</main>
<footer>
<div class="rep" id="rep"><button class="linkish" type="button" id="rep-btn">Report this page</button></div>
<div><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/">E&amp;L</a></div>
</footer>
<script>
(function () {{
  var box = document.getElementById('rep'), btn = document.getElementById('rep-btn');
  btn.onclick = function () {{
    box.innerHTML = '<span>Report this page to E&amp;L?</span> <button type="button" id="rep-yes">Yes, report</button> <button type="button" class="linkish" id="rep-no">Cancel</button>';
    document.getElementById('rep-no').onclick = function () {{ location.reload(); }};
    document.getElementById('rep-yes').onclick = function () {{
      fetch('{report_url}', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: '{{}}' }})
        .finally(function () {{ box.textContent = 'Thanks. We’ll take a look.'; }});
    }};
  }};
}})();
{script}
</script>
</body></html>"""


VOTE_SCRIPT = """
(function () {
  var id = %(id)s, owner = %(owner)s, mine = %(mine)s, counts = %(counts)s;
  var labels = { fire: 'Fire', cute: 'Cute', hmm: 'Not for me' };
  var btns = document.querySelectorAll('.btns button'), bars = document.getElementById('bars'), msg = document.getElementById('msg');
  function show() {
    var total = (counts.fire || 0) + (counts.cute || 0) + (counts.hmm || 0);
    bars.hidden = !(mine || owner);
    bars.innerHTML = '';
    ['fire', 'cute', 'hmm'].forEach(function (k) {
      var row = document.createElement('div'); row.className = 'bar';
      var name = document.createElement('span'); name.textContent = labels[k];
      var track = document.createElement('i'); var fill = document.createElement('span'); track.appendChild(fill);
      var n = document.createElement('em'); n.textContent = counts[k] || 0;
      row.appendChild(name); row.appendChild(track); row.appendChild(n); bars.appendChild(row);
      requestAnimationFrame(function () { fill.style.width = (total ? Math.round(100 * (counts[k] || 0) / total) : 0) + '%%'; });
    });
    btns.forEach(function (b) { b.classList.toggle('on', b.dataset.v === mine); b.setAttribute('aria-pressed', b.dataset.v === mine ? 'true' : 'false'); });
    if (owner) msg.textContent = total ? total + (total === 1 ? ' vote' : ' votes') + ' so far.' : 'No votes yet. Share the link and watch them come in.';
    else if (mine) msg.textContent = 'Thanks for voting. You can change it any time.';
  }
  btns.forEach(function (b) {
    if (owner) { b.disabled = true; return; }
    b.onclick = function () {
      mine = b.dataset.v; show();
      fetch('/api/cards/' + id + '/vote', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ vote: b.dataset.v }) })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (x) { if (x.d.counts) counts = x.d.counts; if (!x.ok) { msg.textContent = x.d.error || 'That vote didn\\u2019t go through.'; } show(); })
        .catch(function () { msg.textContent = 'No connection. Try again.'; });
    };
  });
  var copy = document.getElementById('copy');
  if (copy) copy.onclick = function () {
    var input = document.getElementById('link');
    try { navigator.clipboard.writeText(input.value).then(function () { copy.textContent = 'Copied'; }, function () { input.select(); }); }
    catch (e) { input.select(); }
  };
  show();
})();
"""


def _pieces_html(items, shop_base=None):
    """The pieces of a shared look. "Find it" opens the product page itself (found when it's clicked, like in the
    app), falling back to a Google search for the exact product name."""
    out = []
    for k, it in enumerate(items):
        href = f'{shop_base}/{k}' if shop_base else _shop_link(it)
        out.append(
            f"<li><span class=\"cat\">{e(it.get('category', ''))}</span><span class=\"nm\">{e(it.get('name', ''))}</span>"
            f"<span class=\"pr\">{e(it.get('price', ''))}</span><span class=\"br\">{e(it.get('brand', ''))}</span>"
            + (f"<a href=\"{e(href)}\" target=\"_blank\" rel=\"noopener nofollow\">Find it →</a>"
               if it.get('brand') and it.get('brand') != 'From my closet' else '<span></span>')
            + '</li>')
    return '\n'.join(out)


def _viewer_country():
    """The visitor's country from their browser language (en-GB -> GB), India if it doesn't say."""
    m = __import__('re').search(r'[a-z]{2}-([A-Z]{2})', request.headers.get('Accept-Language') or '')
    return m.group(1) if m else 'IN'


def _shop_redirect(it):
    """To the product page itself, else a Google search for its exact name (robots never trigger a search)."""
    brand, name = str(it.get('brand') or '')[:80], str(it.get('name') or '')[:160]
    if not name or _is_bot():
        return redirect(_shop_link(it), 302)
    try:
        import shop
        if not shop._provider() or (not owner_request() and not shop._allowed(client_ip())):
            return redirect(_shop_link(it), 302)
        r = shop.resolve(brand, name, _viewer_country(), str(it.get('store') or '')[:80]) or {}
        url = r.get('url') if str(r.get('url') or '').startswith('https://') else None
        return redirect(url or r.get('google') or _shop_link(it), 302)
    except Exception as ex:
        print('[growth] shop link failed:', repr(ex))
        return redirect(_shop_link(it), 302)


@bp.get('/l/<cid>/shop/<int:k>')
def card_shop(cid, k):
    c = get_card(cid)
    items = ((c or {}).get('look') or {}).get('items') or []
    if not c or c['hidden'] or not 0 <= k < len(items):
        return redirect('/', 302)
    return _shop_redirect(items[k])


@bp.get('/b/<bid>/shop/<int:j>/<int:k>')
def board_shop(bid, j, k):
    row = db('SELECT looks, hidden FROM el_boards WHERE id = %s', (bid,), one=True) if _valid_id(bid) else None
    try:
        looks = json.loads(row[0] or '[]') if row and not row[1] else []
        items = looks[j].get('items') or []
        return _shop_redirect(items[k])
    except (ValueError, IndexError, AttributeError, TypeError):
        return redirect('/', 302)


def _notes_html(sn):
    labels = {'accessories': 'Accessories', 'hair': 'Hair', 'makeup': 'Makeup', 'fragrance': 'Fragrance'}
    rows = [f'<p><b>{labels[k]}:</b> {e(v)}</p>' for k, v in (sn or {}).items() if k in labels and v]
    return f'<div class="notes">{"".join(rows)}</div>' if rows else ''


def _gone(what='look'):
    body = (f'<p class="eyebrow">Not here</p><h1>This {what} isn’t available</h1>'
            '<p class="small" style="margin-bottom:28px">It may have been removed.</p>'
            '<a class="cta" href="/">Get your own look ✦</a>')
    return Response(PAGE.format(title='E&amp;L', desc='AI stylists Ella and Léon.', og_image='', home_link='/',
                                body=body, report_url='/', script=''), status=404, mimetype='text/html')


@bp.get('/l/<cid>')
def card_page(cid):
    c = get_card(cid)
    if not c or c['hidden']:
        return _gone()
    look = c['look'] or {}
    meta = look.get('meta') or {}
    who = identity()[1]
    owner = who == c['who']
    if not owner and not _is_bot():
        db('UPDATE el_cards SET views = views + 1 WHERE id = %s', (cid,))
        track('card_view', who)
    mine = db('SELECT vote FROM el_votes WHERE card_id = %s AND voter = %s', (cid, who), one=True)
    stylist = 'Léon' if meta.get('stylist') == 'leon' else 'Ella'
    name = (c['name'] or '').strip()
    kind = meta.get('kind') or 'look'
    if owner:
        eyebrow = 'Your fit · live votes'
    elif name:
        eyebrow = f'Rate {name}’s fit'
    else:
        eyebrow = 'Rate this fit'
    sub = ("Today's fit, from my own closet" if kind == 'daily'
           else f"From {stylist}'s Sunday Edit" if kind == 'edit'
           else (f"For {meta.get('occasion')}" if meta.get('occasion') else ''))
    title_txt = f"Rate my fit: {look.get('look_name') or 'my look'}"
    desc = (f"{len(look.get('items') or [])} pieces styled by {stylist} on E&L. Tap to vote: fire, cute or not for me.")
    link = f'{home_url()}/l/{cid}'
    share = (f'<div class="share"><input id="link" readonly value="{e(link)}" aria-label="Your vote link">'
             f'<button type="button" id="copy">Copy</button></div>') if owner else ''
    challenge_line = ''
    if c['challenge']:
        theme = challenge_theme(c['challenge'])
        challenge_line = f'<p class="small">Entered in this week’s challenge: {e(theme["name"])}</p>'
    vote_head = 'Your votes' if owner else 'How\u2019s this fit?'
    body = f"""
<p class="eyebrow">{e(eyebrow)}</p>
{f'<p class="for">{e(sub)}</p>' if sub else ''}
<h1>{e(look.get('look_name') or 'My look')}</h1>
{f'<blockquote>“{e(look.get("ella_note"))}”</blockquote>' if look.get('ella_note') else ''}
<section class="vote" aria-labelledby="vote-h">
  <h2 id="vote-h">{vote_head}</h2>
  <div class="btns" role="group" aria-label="Vote">
    <button type="button" data-v="fire"><b>🔥</b>Fire</button>
    <button type="button" data-v="cute"><b>😍</b>Cute</button>
    <button type="button" data-v="hmm"><b>🤔</b>Not for me</button>
  </div>
  <div class="bars" id="bars" hidden></div>
  <p class="msg" id="msg" role="status"></p>
  {share}
  {challenge_line}
</section>
<ol class="pieces">{_pieces_html(look.get('items') or [], f'/l/{cid}/shop')}</ol>
{_notes_html(look.get('styling_notes'))}
<p class="sig">Styled by {stylist}</p>
<a class="cta" href="/l/{cid}/go">Get your own look ✦</a>
<p class="small">Free on E&amp;L. No sign-up needed.</p>"""
    script = VOTE_SCRIPT % {'id': json.dumps(cid), 'owner': 'true' if owner else 'false',
                            'mine': json.dumps(mine[0] if mine else None), 'counts': json.dumps(_counts(c))}
    og = (f'<meta property="og:image" content="{e(home_url())}/l/{cid}/preview.jpg">'
          '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">')
    resp = Response(PAGE.format(title=e(title_txt), desc=e(desc), og_image=og, home_link=f'/l/{cid}/go',
                                body=body, report_url=f'/api/report/l/{cid}', script=script), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ── Boards: /b/<id> ───────────────────────────────────────────────────────────
@bp.post('/api/boards')
def share_board():
    body = request.get_json(silent=True) or {}
    name = ' '.join(str(body.get('name') or '').split())[:40] or 'My board'
    looks = [p for p in (verify_look(l) for l in (body.get('looks') or [])[:24]) if p]
    if not looks:
        return jsonify({'error': 'Add a look made with the latest app to this board first, then share it.'}), 400
    who = identity()[1]
    if not _within_budget('b:' + who, 40):
        return jsonify({'error': 'Try again tomorrow.'}), 429
    bid = str(body.get('id') or '')
    row = db('SELECT who FROM el_boards WHERE id = %s', (bid,), one=True) if _valid_id(bid) else None
    if row and row[0] == who:
        db('UPDATE el_boards SET name = %s, looks = %s WHERE id = %s', (name, json.dumps(looks, ensure_ascii=False), bid))
    else:
        bid = secrets.token_urlsafe(8)[:10]
        db('INSERT INTO el_boards (id, who, name, looks, created_at) VALUES (%s, %s, %s, %s, %s)',
           (bid, who, name, json.dumps(looks, ensure_ascii=False), now()))
    skipped = len(body.get('looks') or []) - len(looks)
    return jsonify({'id': bid, 'url': f'{home_url()}/b/{bid}', 'skipped': max(0, skipped)})


@bp.get('/b/<bid>')
def board_page(bid):
    if not _valid_id(bid):
        return _gone('board')
    row = db('SELECT name, looks, hidden, who FROM el_boards WHERE id = %s', (bid,), one=True)
    if not row or row[2]:
        return _gone('board')
    name, looks_json, _, owner_who = row
    if owner_who != identity()[1] and not _is_bot():
        db('UPDATE el_boards SET views = views + 1 WHERE id = %s', (bid,))
    try:
        looks = json.loads(looks_json or '[]')
    except ValueError:
        looks = []
    parts = []
    for j, look in enumerate(looks):
        parts.append(f"""<section class="board-look"><h2>{e(look.get('look_name') or 'Look')}</h2>
{f'<blockquote>“{e(look.get("ella_note"))}”</blockquote>' if look.get('ella_note') else ''}
<ol class="pieces">{_pieces_html(look.get('items') or [], f'/b/{bid}/shop/{j}')}</ol></section>""")
    body = f"""<p class="eyebrow">A board on E&amp;L</p><h1>{e(name)}</h1>
<p class="small">{len(looks)} look{'s' if len(looks) != 1 else ''}, styled by Ella and Léon</p>
{''.join(parts)}
<p class="sig" style="margin-top:36px">Want looks like these?</p>
<a class="cta" href="/?ref=b_{bid}">Get your own look ✦</a>
<p class="small">Free on E&amp;L. No sign-up needed.</p>"""
    return Response(PAGE.format(title=e(f'{name} · E&L board'), desc=e(f'{len(looks)} looks styled on E&L.'),
                                og_image='', home_link='/', body=body, report_url=f'/api/report/b/{bid}', script=''),
                    mimetype='text/html')


# ── The weekly challenge ──────────────────────────────────────────────────────
THEMES = [
    {'name': 'Goth Friday', 'occasion': 'Friday night out', 'era': 'Goth',
     'blurb': 'Black lace, velvet and silver. Make it dark, make it romantic.'},
    {'name': 'Old Money Weekend', 'occasion': 'weekend brunch at the club', 'era': 'Quiet Luxury',
     'blurb': 'Cashmere, loafers, not a logo in sight.'},
    {'name': 'Y2K Throwback', 'occasion': 'birthday party', 'era': '2000s Y2K',
     'blurb': 'Low-rise, baby tees and butterfly clips. Go full 2003.'},
    {'name': 'Wedding Guest Glam', 'occasion': 'evening wedding reception (guest)', 'era': 'Upper East Side Glam',
     'blurb': 'Outshine everyone except the bride.'},
    {'name': 'Clean Girl Monday', 'occasion': 'college / campus', 'era': 'Clean Girl Aesthetic',
     'blurb': 'Slicked bun, gold hoops, effortless.'},
    {'name': 'Cottagecore Picnic', 'occasion': 'picnic in the park', 'era': 'Cottagecore',
     'blurb': 'Puff sleeves, gingham and a basket bag.'},
    {'name': 'Grunge Gig Night', 'occasion': 'live music gig', 'era': '1990s Grunge',
     'blurb': 'Flannel, band tees and smudged liner.'},
    {'name': 'Date Night in Red', 'occasion': 'date night, wearing red', 'era': 'Upper East Side Glam',
     'blurb': 'One colour. Maximum impact.'},
    {'name': 'Festive Sparkle', 'occasion': 'Indian festive party', 'era': 'Upper East Side Glam',
     'blurb': 'Sequins, mirror work and statement jhumkas.'},
    {'name': 'Airport Chic', 'occasion': 'travel / airport', 'era': 'Quiet Luxury',
     'blurb': 'Comfy enough for the flight, sharp enough for the photos.'},
]


def challenge_theme(week=None):
    week = week or week_key()
    try:
        custom = json.loads(kv_get('challenge:' + week) or 'null')
    except Exception:
        custom = None
    if isinstance(custom, dict) and custom.get('name'):
        return {'name': custom['name'][:40], 'occasion': (custom.get('occasion') or custom['name'])[:80],
                'era': (custom.get('era') or '')[:60], 'blurb': (custom.get('blurb') or '')[:140], 'custom': True}
    n = int(week.split('W')[-1]) if 'W' in week else 0
    return dict(THEMES[n % len(THEMES)])


def _week_end():
    t = datetime.now(IST)
    monday = (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((monday + timedelta(days=7)).timestamp())


def _entries(week, limit=30):
    rows = db('SELECT id, look, name, fire, cute, hmm, who FROM el_cards WHERE challenge = %s AND hidden = 0 '
              'ORDER BY (fire * 2 + cute) DESC, created_at ASC LIMIT %s', (week, limit), many=True) or []
    out = []
    for cid, look, name, fire, cute, hmm, who in rows:
        try:
            ln = json.loads(look or '{}').get('look_name') or 'A look'
        except ValueError:
            ln = 'A look'
        out.append({'id': cid, 'look_name': ln, 'name': name or '', 'fire': fire, 'cute': cute, 'hmm': hmm,
                    'score': fire * 2 + cute, '_who': who})
    return out


@bp.get('/api/challenge')
def challenge():
    week = week_key()
    me = identity()[1]
    entries = _entries(week)
    last_week = week_key(time.time() - 7 * 86400)
    prev = _entries(last_week, 1)
    clean = lambda rows: [{k: v for k, v in r.items() if k != '_who'} | {'mine': r['_who'] == me} for r in rows]
    return jsonify({'week': week, 'theme': challenge_theme(week), 'ends_at': _week_end(), 'entries': clean(entries),
                    'winner': clean(prev)[0] if prev and prev[0]['score'] > 0 else None,
                    'last_theme': challenge_theme(last_week)['name']})


# ── Owner tools ───────────────────────────────────────────────────────────────
def _owner_only():
    return owner_request()


@bp.get('/api/owner/stats')
def owner_stats():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    days = [(datetime.now(IST) - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(14)]
    cutoff = days[-1]
    events = {}
    for day, ev, n in db('SELECT day, event, n FROM el_stats WHERE day >= %s', (cutoff,), many=True) or []:
        events.setdefault(day, {})[ev] = int(n)
    first, seen = {}, {}
    for who, day in db('SELECT who, day FROM el_active', many=True) or []:
        seen.setdefault(day, set()).add(who)
        if who not in first or day < first[who]:
            first[who] = day
    out_days = []
    for d in days:
        active = seen.get(d, set())
        new = sum(1 for w in active if first.get(w) == d)
        out_days.append({'day': d, 'active': len(active), 'new': new, 'returning': len(active) - new,
                         'events': events.get(d, {})})

    def retention(offset, cohorts):
        kept = total = 0
        for back in cohorts:
            cday = (datetime.now(IST) - timedelta(days=back)).strftime('%Y-%m-%d')
            later_day = (datetime.now(IST) - timedelta(days=back - offset)).strftime('%Y-%m-%d')
            cohort = [w for w, f in first.items() if f == cday]
            total += len(cohort)
            kept += sum(1 for w in cohort if w in seen.get(later_day, set()))
        return {'kept': kept, 'of': total, 'pct': round(100 * kept / total) if total else None}

    cards = db('SELECT COUNT(*), COALESCE(SUM(fire + cute + hmm), 0), COALESCE(SUM(views), 0) FROM el_cards', one=True) or (0, 0, 0)
    try:
        push = db('SELECT COUNT(*) FROM el_push', one=True)[0]
    except Exception:
        push = 0
    users = db('SELECT COUNT(*) FROM el_users', one=True)[0]
    reported = db('SELECT id, look, reports, hidden FROM el_cards WHERE reports > 0 ORDER BY reports DESC LIMIT 20',
                  many=True) or []
    rep = []
    for cid, look, n, hidden in reported:
        try:
            ln = json.loads(look or '{}').get('look_name') or ''
        except ValueError:
            ln = ''
        rep.append({'id': cid, 'look_name': ln, 'reports': n, 'hidden': bool(hidden), 'kind': 'l'})
    for bid, name, n, hidden in db('SELECT id, name, reports, hidden FROM el_boards WHERE reports > 0 LIMIT 20', many=True) or []:
        rep.append({'id': bid, 'look_name': name, 'reports': n, 'hidden': bool(hidden), 'kind': 'b'})
    return jsonify({'days': out_days, 'd1': retention(1, range(2, 9)), 'd7': retention(7, range(8, 15)),
                    'totals': {'cards': int(cards[0]), 'votes': int(cards[1]), 'card_views': int(cards[2]),
                               'push': int(push), 'accounts': int(users), 'people': len(first)},
                    'reported': rep})


@bp.post('/api/owner/hide')
def owner_hide():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    table = {'l': 'el_cards', 'b': 'el_boards'}.get(body.get('kind'))
    cid = str(body.get('id') or '')
    if not table or not _valid_id(cid):
        return jsonify({'error': 'bad id'}), 400
    hide = 1 if body.get('hidden') else 0
    db(f'UPDATE {table} SET hidden = %s, reports = CASE WHEN %s = 0 THEN 0 ELSE reports END WHERE id = %s', (hide, hide, cid))
    return owner_stats()


@bp.post('/api/owner/challenge')
def owner_challenge():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    name = ' '.join(str(body.get('name') or '').split())[:40]
    if not name:
        kv_set('challenge:' + week_key(), 'null')
    else:
        kv_set('challenge:' + week_key(), json.dumps({
            'name': name, 'occasion': ' '.join(str(body.get('occasion') or '').split())[:80] or name,
            'era': ' '.join(str(body.get('era') or '').split())[:60],
            'blurb': ' '.join(str(body.get('blurb') or '').split())[:140]}))
    return jsonify({'theme': challenge_theme()})


def cleanup():
    """Now and then: old stats and votes that no longer matter."""
    try:
        cutoff = (datetime.now(IST) - timedelta(days=400)).strftime('%Y-%m-%d')
        db('DELETE FROM el_active WHERE day < %s', (cutoff,))
        db('DELETE FROM el_stats WHERE day < %s', (cutoff,))
    except Exception as ex:
        print('[growth] cleanup skipped:', ex)
