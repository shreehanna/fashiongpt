"""JARVIS's personal side, for the owner only: call or message people, open apps, reminders, notes, live search.

A website can't place a call or send a text by itself: iOS (and Android) always ask you first. So JARVIS does
everything up to that point (finds the number, writes the message) and hands over a one-tap card that opens the
Phone, WhatsApp, Messages or the app with it all filled in. Phone numbers stay in E&L, owner-only, and are never sent
to the AI: it only sees names, and the number is looked up here after it picks one.

  GET  /api/owner/contacts              JARVIS's contacts
  POST /api/owner/contacts              add or update one: {name, phone, app}
  POST /api/owner/contacts/delete       {name}
  POST /api/owner/contacts/import       a .vcf file from iPhone Contacts (share a contact, or export from iCloud.com)
  Reminders are pushed to the owner's phones when due: checked every 30 s while E&L is awake, and on every
  /api/cron/tick (set cron-job.org to every 5 minutes so they're never more than a few minutes late).
"""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests as req
from flask import Blueprint, jsonify, request

from pro import db, kv_get, kv_set, owner_request, env

bp = Blueprint('butler', __name__)
IST = timezone(timedelta(hours=5, minutes=30))
MAX_CONTACTS, MAX_NOTES = 3000, 200
APPS = ('whatsapp', 'sms')
CALLS = ('phone', 'facetime', 'facetime_audio')
_lock = threading.Lock()


def init():
    db('CREATE TABLE IF NOT EXISTS el_reminders (id TEXT PRIMARY KEY, due INTEGER NOT NULL, text TEXT NOT NULL, '
       'sent INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL)')
    threading.Thread(target=_loop, name='el-reminders', daemon=True).start()


def _clip(v, n):
    return ' '.join(str(v or '').split())[:n]


# ── contacts ──────────────────────────────────────────────────────────────────
def phone_clean(raw):
    """'+91 98765 43210', '098765-43210', '9876543210' -> '+919876543210' (a 10-digit number is taken as Indian)."""
    s = str(raw or '').strip()
    plus = s.startswith('+') or s.startswith('00')
    digits = re.sub(r'\D', '', s)
    if s.startswith('00'):
        digits = digits[2:]
    if not plus:
        if len(digits) == 11 and digits.startswith('0'):
            digits = digits[1:]
        if len(digits) == 10:
            digits = '91' + digits
    return '+' + digits if 8 <= len(digits) <= 15 else ''


def contacts():
    try:
        out = json.loads(kv_get('jarvis:contacts') or '[]')
        return [c for c in out if isinstance(c, dict) and c.get('name') and c.get('phone')]
    except ValueError:
        return []


def _save_contacts(rows):
    rows = sorted(rows, key=lambda c: c['name'].lower())[:MAX_CONTACTS]
    kv_set('jarvis:contacts', json.dumps(rows, ensure_ascii=False))
    return rows


def add_contact(name, phone, app='whatsapp'):
    name, phone = _clip(name, 60), phone_clean(phone)
    if not name or not phone:
        return None
    rows = [c for c in contacts() if c['name'].lower() != name.lower()]
    rows.append({'name': name, 'phone': phone, 'app': app if app in APPS else 'whatsapp'})
    _save_contacts(rows)
    return rows[-1]


def _norm(s):
    return re.sub(r'[^a-z0-9 ]', '', str(s or '').lower()).strip()


def find(name):
    """The contact JARVIS means: ([match], []) or ([], [candidates]) when it's not clear, or ([], [])."""
    q = _norm(name)
    if not q:
        return [], []
    rows = contacts()
    exact = [c for c in rows if _norm(c['name']) == q]
    if len(exact) == 1:
        return exact, []
    if len(exact) > 1:
        return [], exact
    starts = [c for c in rows if _norm(c['name']).startswith(q + ' ') or _norm(c['name']).startswith(q)]
    if len(starts) == 1:
        return starts, []
    words = [c for c in rows if q in _norm(c['name']).split()]
    pool = starts or words
    if len(pool) == 1:
        return pool, []
    return [], pool[:5]


def names_for(said, limit=150):
    """The names JARVIS may use: all of them if there aren't many, else just the ones that came up."""
    rows = contacts()
    if len(rows) <= limit:
        return [c['name'] for c in rows], 0
    words = set(_norm(said).split())
    hit = [c['name'] for c in rows if words & set(_norm(c['name']).split())]
    return hit[:40], len(rows) - len(hit[:40])


def parse_vcf(text):
    """Names and phone numbers from a .vcf file (one or many cards). Mobile numbers win over others."""
    text = re.sub(r'\r?\n[ \t]', '', str(text or ''))           # unfold long lines
    out = []
    for card in re.findall(r'BEGIN:VCARD(.*?)END:VCARD', text, re.S | re.I):
        fn, n, tels = '', '', []
        for line in card.splitlines():
            key, _, val = line.partition(':')
            k = key.upper()
            if k == 'FN' or k.startswith('FN;'):
                fn = val
            elif k == 'N' or k.startswith('N;'):
                parts = val.split(';')
                n = ' '.join(p for p in (parts[1] if len(parts) > 1 else '', parts[0]) if p)
            elif 'TEL' in k.split(';')[0].split('.')[-1]:
                tels.append((0 if re.search(r'CELL|MOBILE|IPHONE', k) else 1, val))
        name = _clip((fn or n).replace('\\,', ',').replace('\\;', ';'), 60)
        tels.sort(key=lambda t: t[0])
        phone = next((phone_clean(v) for _, v in tels if phone_clean(v)), '')
        if name and phone:
            out.append({'name': name, 'phone': phone, 'app': 'whatsapp'})
    return out


@bp.get('/api/owner/contacts')
def list_contacts():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    return jsonify({'contacts': contacts()})


@bp.post('/api/owner/contacts')
def save_contact():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    b = request.get_json(silent=True) or {}
    c = add_contact(b.get('name'), b.get('phone'), b.get('app'))
    if not c:
        return jsonify({'error': 'A name and a full phone number, please.'}), 400
    return jsonify({'ok': True, 'contact': c, 'contacts': contacts()})


@bp.post('/api/owner/contacts/delete')
def delete_contact():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    name = _clip((request.get_json(silent=True) or {}).get('name'), 60).lower()
    rows = _save_contacts([c for c in contacts() if c['name'].lower() != name])
    return jsonify({'ok': True, 'contacts': rows})


@bp.post('/api/owner/contacts/import')
def import_contacts():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    f = request.files.get('file')
    raw = f.read(4 * 1024 * 1024 + 1) if f else (request.get_data() or b'')
    if len(raw) > 4 * 1024 * 1024:
        return jsonify({'error': 'That file is too big (4 MB at most).'}), 400
    found = parse_vcf(raw.decode('utf-8', 'replace'))
    if not found:
        return jsonify({'error': "No names with phone numbers in that file. Export it from Contacts as a vCard (.vcf)."}), 400
    have = {c['name'].lower(): c for c in contacts()}
    for c in found:
        if c['name'].lower() in have:
            c['app'] = have[c['name'].lower()].get('app', 'whatsapp')
        have[c['name'].lower()] = c
    rows = _save_contacts(list(have.values()))
    return jsonify({'ok': True, 'added': len(found), 'contacts': rows})


# ── notes ─────────────────────────────────────────────────────────────────────
def notes():
    try:
        return [n for n in json.loads(kv_get('jarvis:notes') or '[]') if isinstance(n, dict) and n.get('text')]
    except ValueError:
        return []


def add_note(text):
    text = _clip(text, 500)
    if not text:
        return None
    rows = ([{'text': text, 'at': int(time.time())}] + notes())[:MAX_NOTES]
    kv_set('jarvis:notes', json.dumps(rows, ensure_ascii=False))
    return rows[0]


def forget_note(n):
    rows = notes()
    if not (1 <= n <= len(rows)):
        return None
    gone = rows.pop(n - 1)
    kv_set('jarvis:notes', json.dumps(rows, ensure_ascii=False))
    return gone


# ── reminders ─────────────────────────────────────────────────────────────────
def parse_when(s, now=None):
    """'2026-11-18 18:00' (IST, from JARVIS) -> unix time, or None if it isn't a sensible future time."""
    m = re.match(r'\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})', str(s or ''))
    if not m:
        return None
    try:
        t = datetime(*map(int, m.groups()), tzinfo=IST).timestamp()
    except ValueError:
        return None
    now = now or time.time()
    return int(t) if now - 60 <= t <= now + 366 * 86400 else None


def remind(when, text):
    due = parse_when(when)
    text = _clip(text, 200)
    if not due or not text:
        return None
    rid = secrets.token_hex(3)
    db('INSERT INTO el_reminders (id, due, text, sent, created) VALUES (%s, %s, %s, 0, %s)', (rid, max(due, int(time.time())), text, int(time.time())))
    return {'id': rid, 'due': due, 'text': text}


def upcoming(limit=20):
    rows = db('SELECT id, due, text FROM el_reminders WHERE sent = 0 ORDER BY due LIMIT %s', (limit,), many=True) or []
    return [{'id': r[0], 'when_ist': datetime.fromtimestamp(r[1], IST).strftime('%a %d %b, %I:%M %p'), 'text': r[2]} for r in rows]


def cancel(rid):
    rid = _clip(rid, 12)
    if not db('SELECT id FROM el_reminders WHERE id = %s AND sent = 0', (rid,), one=True):
        return False
    db('DELETE FROM el_reminders WHERE id = %s', (rid,))
    return True


def due_now():
    """Push every reminder that's due to the owner's phones. Safe to call from anywhere, as often as you like."""
    if not _lock.acquire(blocking=False):
        return 0
    try:
        rows = db('SELECT id, text FROM el_reminders WHERE sent = 0 AND due <= %s ORDER BY due', (int(time.time()),), many=True) or []
        if not rows:
            return 0
        import push
        for rid, text in rows:
            db('UPDATE el_reminders SET sent = 1 WHERE id = %s', (rid,))
            push.notify_owner({'title': 'JARVIS: reminder', 'body': text, 'tag': 'reminder-' + rid,
                               'url': '/?open=owner', 'ttl': 6 * 3600})
        db('DELETE FROM el_reminders WHERE sent = 1 AND due < %s', (int(time.time()) - 7 * 86400,))
        return len(rows)
    except Exception as e:
        print('[butler] reminders failed:', repr(e)[:200])
        return 0
    finally:
        _lock.release()


def _loop():
    while True:
        time.sleep(30)
        due_now()


# ── live search (Serper) ──────────────────────────────────────────────────────
def search(q):
    """A few fresh results for JARVIS to answer from, or '' if search isn't set up or failed."""
    key = env('SERPER_API_KEY')
    q = _clip(q, 160)
    if not key or not q:
        return ''
    try:
        r = req.post('https://google.serper.dev/search', json={'q': q, 'gl': 'in', 'hl': 'en', 'num': 6}, timeout=10,
                     headers={'X-API-KEY': key, 'Content-Type': 'application/json'})
        if r.status_code != 200:
            return ''
        d = r.json()
    except Exception:
        return ''
    lines = []
    box = d.get('answerBox') or {}
    if box.get('answer') or box.get('snippet'):
        lines.append(f"Answer box: {_clip(box.get('answer') or box.get('snippet'), 300)} ({_clip(box.get('link') or box.get('title'), 90)})")
    kg = d.get('knowledgeGraph') or {}
    if kg.get('description'):
        lines.append(f"About {_clip(kg.get('title'), 60)}: {_clip(kg.get('description'), 300)}")
    for o in (d.get('organic') or [])[:6]:
        lines.append(f"- {_clip(o.get('title'), 110)} ({_clip(o.get('link'), 90)}): {_clip(o.get('snippet'), 260)}"
                     + (f" [{o['date']}]" if o.get('date') else ''))
    return '\n'.join(lines)


# ── what JARVIS can do with a tap ─────────────────────────────────────────────
OPEN = {
    'instagram': ('Open Instagram', lambda q: 'instagram://app'),
    'instagram_reels': ('Open Instagram (Reels is the ▶ tab)', lambda q: 'instagram://app'),
    'youtube': ('Open YouTube', lambda q: 'https://www.youtube.com/results?search_query=' + quote(q) if q else 'https://www.youtube.com/'),
    'spotify': ('Open Spotify', lambda q: 'https://open.spotify.com/search/' + quote(q) if q else 'https://open.spotify.com/'),
    'maps': ('Directions in Google Maps', lambda q: 'https://www.google.com/maps/dir/?api=1&destination=' + quote(q) if q else 'https://www.google.com/maps'),
    'google': ('Search Google', lambda q: 'https://www.google.com/search?q=' + quote(q) if q else 'https://www.google.com/'),
    'whatsapp': ('Open WhatsApp', lambda q: 'whatsapp://app'),
    'uber': ('Open Uber', lambda q: 'https://m.uber.com/ul/?action=setPickup&pickup=my_location' + ('&dropoff[formatted_address]=' + quote(q) if q else '')),
    'swiggy': ('Open Swiggy', lambda q: 'https://www.swiggy.com/'),
    'zomato': ('Open Zomato', lambda q: 'https://www.zomato.com/'),
    'myntra': ('Open Myntra', lambda q: 'https://www.myntra.com/' + quote(re.sub(r'\s+', '-', q.strip().lower())) if q else 'https://www.myntra.com/'),
    'amazon': ('Open Amazon', lambda q: 'https://www.amazon.in/s?k=' + quote(q) if q else 'https://www.amazon.in/'),
    'netflix': ('Open Netflix', lambda q: 'https://www.netflix.com/'),
    'gmail': ('Open Gmail', lambda q: 'https://mail.google.com/'),
    'el_reels': ("Open E&L's Reels", lambda q: '/?open=reels'),
    'el_daily': ("Open today's fit", lambda q: '/?open=daily'),
    'el_edit': ('Open the Sunday Edit', lambda q: '/?open=edit'),
}
_ACT = re.compile(r'\[\[\s*action:\s*(call|message|open|remind|note|forget_note|cancel_reminder|save_contact|search)\b\s*(.*?)\]\]\s*$', re.I | re.S)


def split(text):
    """(reply without the line, {'type', 'args'}) for JARVIS's personal actions, or (text, None)."""
    text = (text or '').rstrip()
    m = _ACT.search(text)
    if not m:
        return text, None
    args = [a.strip() for a in re.split(r'\s*\|\s*', m.group(2).strip())]
    return text[:m.start()].rstrip(), {'type': m.group(1).lower(), 'args': args}


def _digits(s):
    return re.sub(r'\D', '', str(s or ''))


def handle(act, a):
    """Carry out a personal action. Returns {'card': {...} or None, 'say': override or None, 'search': query or None}."""
    kind, args = act['type'], [x for x in act['args']]
    first = args[0] if args else ''
    if kind == 'search':
        q = ' '.join(x for x in args if x)
        return {'search': q} if q else {'say': f"Search for what, {a}?"}
    if kind in ('call', 'message'):
        if len(_digits(first)) >= 7 and phone_clean(first):     # a number said out loud
            who = {'name': phone_clean(first), 'phone': phone_clean(first), 'app': 'whatsapp'}
        else:
            hit, maybe = find(first)
            if maybe:
                return {'say': f"Which one, {a}: " + ', '.join(c['name'] for c in maybe[:-1]) + f" or {maybe[-1]['name']}?"
                        if len(maybe) > 1 else f"Did you mean {maybe[0]['name']}, {a}?"}
            if not hit:
                return {'say': f"I don't have a number for {first or 'them'}, {a}. Tell me \"save {first or 'their'} number\" and "
                               "the number, or add them under Contacts in owner tools."}
            who = hit[0]
        if kind == 'call':
            how = (args[1].lower() if len(args) > 1 else 'phone')
            how = how if how in CALLS else 'phone'
            scheme = {'phone': 'tel:', 'facetime': 'facetime:', 'facetime_audio': 'facetime-audio:'}[how]
            label = {'phone': 'Call', 'facetime': 'FaceTime', 'facetime_audio': 'FaceTime audio'}[how]
            return {'card': {'type': 'call', 'name': who['name'], 'label': f"📞 {label} {who['name']}", 'href': scheme + who['phone']}}
        named = len(args) > 1 and args[1].lower() in APPS
        app = args[1].lower() if named else who.get('app', 'whatsapp')
        msg = _clip('|'.join(args[2:] if named else args[1:]), 1000)
        if app == 'sms':
            href = f"sms:{who['phone']}" + (f"&body={quote(msg)}" if msg else '')
            label = f"💬 Text {who['name']} in Messages"
        else:
            href = f"https://wa.me/{_digits(who['phone'])}" + (f"?text={quote(msg)}" if msg else '')
            label = f"💬 WhatsApp {who['name']}"
        return {'card': {'type': 'message', 'name': who['name'], 'app': app, 'text': msg, 'label': label, 'href': href}}
    if kind == 'open':
        target = first.lower().replace(' ', '_')
        if target not in OPEN:
            return {'say': f"I can't open that one for you, {a}."}
        label, url = OPEN[target]
        q = _clip(' '.join(args[1:]), 160)
        return {'card': {'type': 'open', 'label': label, 'href': url(q), 'target': target}}
    if kind == 'remind':
        r = remind(first, ' '.join(args[1:]))
        if not r:
            return {'say': f"I couldn't set that reminder, {a}: I need a time that's still to come. When should I remind you?"}
        return {'done': True}
    if kind == 'note':
        return {'done': True} if add_note(' '.join(x for x in args if x)) else {'say': f"Note what, {a}?"}
    if kind == 'forget_note':
        try:
            gone = forget_note(int(_digits(first) or 0))
        except ValueError:
            gone = None
        return {'done': True} if gone else {'say': f"There's no note with that number, {a}."}
    if kind == 'cancel_reminder':
        return {'done': True} if cancel(first) else {'say': f"I can't find that reminder, {a}."}
    if kind == 'save_contact':
        c = add_contact(first, args[1] if len(args) > 1 else '', args[2].lower() if len(args) > 2 else 'whatsapp')
        return {'done': True} if c else {'say': f"I need a name and a full phone number for that, {a}."}
    return {}


def context(said):
    """What JARVIS knows about the owner's own things (names only, never numbers)."""
    names, more = names_for(said)
    return {'contacts': names, 'more_contacts_not_listed': more,
            'notes_newest_first': [f"{i}. {n['text']}" for i, n in enumerate(notes()[:30], 1)],
            'reminders_upcoming': upcoming()}


PERSONA = """
YOUR PERSONAL SIDE
You're also the owner's personal assistant, not only E&L's: answer anything (general knowledge, study help, maths,
plans, writing, advice) like a brilliant all-round assistant, in the same voice. You can also do these, with ONE line
at the very end of your reply, exactly in this form (pick at most one per reply):
- [[action: call NAME]] (or "| facetime" / "| facetime_audio"): NAME exactly as in CONTACTS, or a number they said.
- [[action: message NAME | whatsapp | TEXT]] (or "| sms" for Messages/iMessage). WhatsApp unless they say SMS,
  iMessage or Messages. TEXT is the message itself, written as the owner would text it: first person, casual, their
  words tidied up, never signed by you, never calling anyone "{a}".
- [[action: open TARGET | SEARCH]]: TARGET one of instagram, instagram_reels, youtube, spotify, maps, google,
  whatsapp, uber, swiggy, zomato, myntra, amazon, netflix, gmail, el_reels, el_daily, el_edit; SEARCH optional
  (a song, video, place or thing). "Open reels" means instagram_reels unless they say E&L's reels.
- [[action: remind YYYY-MM-DD HH:MM | TEXT]]: a time in IST worked out from now_ist ("in 20 minutes", "at 6",
  "tomorrow morning" = 8:00). It arrives as a notification on their phone.
- [[action: note | TEXT]], [[action: forget_note N]], [[action: cancel_reminder ID]] (from the lists below).
- [[action: save_contact NAME | NUMBER | whatsapp]] when they tell you someone's number.
- [[action: search | QUERY]]: whenever the answer depends on anything current or that you might not know (news,
  scores, prices, weather outside Chennai, opening hours, anything recent). Reply with ONLY that line; you'll be given
  fresh results and then answer from them, naming the source in a word or two.
HOW THESE WORK, be honest about it: you can't place calls or send messages yourself. A card appears and one tap opens
the Phone, WhatsApp or Messages with everything filled in; the owner taps call or send. So say "Ready, {a}. Tap to
call." or "It's written, {a}. Tap to send.", never "calling now" or "sent". Reminders, notes and saved contacts happen
straight away ("Done, {a}." is right). If someone isn't in CONTACTS, don't guess a number: say so. You can't read their
messages, calls, calendar, photos or notifications, or control the phone's settings, music volume or smart devices."""
