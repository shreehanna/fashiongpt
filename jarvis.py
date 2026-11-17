"""JARVIS: the owner's private operations assistant, in owner tools. Only the owner can reach it.

A calm, dry, very polite butler who runs E&L with the owner: status reports, morning briefings, "pull up" holograms
(diagrams and charts drawn in text), protocols ("House Party", "Suit up"...). Every number comes from a live snapshot
of E&L taken on each message (services and credits, people, Pro, the Sunday story, reported looks, Chennai's
weather): JARVIS never makes one up. It can suggest an action (write or publish the Sunday story, hide a reported
look, send a test alert), which happens only when the owner taps Confirm.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests as req
from flask import Blueprint, jsonify, request

from pro import db, owner_request

bp = Blueprint('jarvis', __name__)
IST = timezone(timedelta(hours=5, minutes=30))
CHENNAI = (13.0, 80.2)
_ai = {'client': None, 'model': None}
_wx = {}
ACTIONS = ('write_story', 'publish_story', 'hide_look', 'test_alert')


def init(client, model):
    _ai['client'], _ai['model'] = client, model


# ── What JARVIS knows: a live snapshot of E&L ─────────────────────────────────
def chennai_weather():
    """Chennai's next 12 hours (MET Norway, kept 30 minutes), or None. Never guessed."""
    hit = _wx.get('chennai')
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        import daily
        from pro import env
        contact = env('CONTACT_EMAIL') or env('PUBLIC_URL') or 'https://fashiongpt-f0vi.onrender.com'
        r = req.get('https://api.met.no/weatherapi/locationforecast/2.0/compact', timeout=6,
                    params={'lat': CHENNAI[0], 'lon': CHENNAI[1]}, headers={'User-Agent': f'EandL-stylist/1.0 {contact}'})
        out = daily.parse_met(r.json()) if r.status_code == 200 else None
    except Exception as e:
        print('[jarvis] weather failed:', e)
        out = None
    if out:
        _wx['chennai'] = (time.time(), out)
    return out


def _one(sql, params=()):
    try:
        row = db(sql, params, one=True)
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def people(days=14):
    """Active people, new people and what they did, per day (IST), newest first."""
    now = datetime.now(IST)
    dates = [(now - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]
    cutoff = dates[-1]
    active, new, events = {}, {}, {}
    try:
        for day, n in db('SELECT day, COUNT(*) FROM el_active WHERE day >= %s GROUP BY day', (cutoff,), many=True) or []:
            active[day] = int(n)
        for day, n in db('SELECT first_day, COUNT(*) FROM (SELECT who, MIN(day) AS first_day FROM el_active GROUP BY who) f '
                         'WHERE first_day >= %s GROUP BY first_day', (cutoff,), many=True) or []:
            new[day] = int(n)
        for day, ev, n in db('SELECT day, event, n FROM el_stats WHERE day >= %s', (cutoff,), many=True) or []:
            events.setdefault(day, {})[ev] = int(n)
    except Exception as e:
        print('[jarvis] people failed:', e)
    looks = {}
    try:
        for day, n in db('SELECT day, COALESCE(SUM(looks), 0) FROM el_usage WHERE day >= %s GROUP BY day', (cutoff,), many=True) or []:
            looks[day] = int(n)
    except Exception:
        pass
    return [{'day': d, 'active': active.get(d, 0), 'new': new.get(d, 0), 'looks': looks.get(d, 0),
             'events': events.get(d, {})} for d in dates]


def _story():
    try:
        import story
        v = story._owner_view()
        r = (v.get('regions') or {}).get('all') or {}
        d = r.get('draft') or {}
        live = r.get('live') or {}
        return {'sunday': v.get('sunday'), 'auto_publish': v.get('auto'), 'writing': r.get('writing'),
                'draft': {'headline': d.get('headline'), 'status': d.get('status'), 'photos': len(d.get('photos') or [])} if d else None,
                'failed': r.get('failed'), 'why_steps': (r.get('steps') or [])[:6],
                'live': {'headline': live.get('headline'), 'sunday': live.get('sunday')} if live else None}
    except Exception as e:
        return {'error': str(e)[:120]}


def _services():
    try:
        import alerts
        return [{k: s.get(k) for k in ('name', 'what', 'level', 'text', 'balance', 'left', 'spent')} for s in alerts.status()]
    except Exception as e:
        return [{'error': str(e)[:120]}]


def _reported():
    out = []
    try:
        for cid, look, n in db('SELECT id, look, reports FROM el_cards WHERE reports > 0 AND hidden = 0 ORDER BY reports DESC LIMIT 5',
                               many=True) or []:
            try:
                name = json.loads(look or '{}').get('look_name') or ''
            except ValueError:
                name = ''
            out.append({'kind': 'l', 'id': cid, 'look_name': name, 'reports': int(n)})
        for bid, name, n in db('SELECT id, name, reports FROM el_boards WHERE reports > 0 AND hidden = 0 LIMIT 5', many=True) or []:
            out.append({'kind': 'b', 'id': bid, 'look_name': name, 'reports': int(n)})
    except Exception:
        pass
    return out


def pending(services, story, reported, now=None):
    """Everything waiting on the owner, most urgent first (3 = now, 2 = soon, 1 = when there's a moment)."""
    now = now or datetime.now(IST)
    out = []
    for s in services:
        if s.get('level') in ('out', 'key'):
            out.append((3, f"{s['name']} ({s.get('what')}) is down: {s.get('text') or s['level']}"))
        elif s.get('level') == 'voice':
            out.append((2, s.get('text') or "One of the voices can't be used."))
        elif s.get('level') == 'low':
            out.append((2, f"{s['name']} is running low: {s.get('text') or ''}".strip()))
        elif s.get('level') == 'off' and s.get('what') == 'styling':
            out.append((3, f"{s['name']} has no API key in Render, so no looks can be made"))
        elif s.get('level') == 'ok' and s.get('name') and s.get('balance') is None and s.get('what') in ('styling', 'shop links'):
            out.append((1, f"Type the {s['name']} balance into owner tools so low credit gets caught early"))
    d = story.get('draft') or {}
    sunday_today = now.weekday() == 6
    if d and d.get('status') == 'draft':
        out.append((3 if sunday_today else 2, f"This Sunday's story is waiting for review: \"{d.get('headline')}\""))
    elif story.get('failed') and not story.get('writing'):
        out.append((2, f"This week's Sunday story couldn't be written: {story['failed']}"))
    elif not d and now.weekday() in (5, 6) and not story.get('writing'):
        out.append((2, 'No Sunday story yet this week'))
    if reported:
        out.append((2, f"{len(reported)} reported look(s) to review"))
    if not _phones():
        out.append((2, "JARVIS can't ping the owner's phone yet: turn on E&L notifications on the phone, signed in "
                       "(owner tools: \"Let JARVIS ping this phone\")"))
    return [{'urgency': u, 'what': w} for u, w in sorted(out, key=lambda x: -x[0])]


def _phones():
    try:
        import push
        return len(push.owner_phones())
    except Exception:
        return 0


def snapshot():
    now = datetime.now(IST)
    import ai
    services, story_now, reported = _services(), _story(), _reported()
    days = people()
    t = int(time.time())
    try:
        plans = {str(p or 'unknown'): int(n) for p, n in db('SELECT plan, COUNT(*) FROM el_users WHERE pro_until > %s GROUP BY plan',
                                                           (t,), many=True) or []}
    except Exception:
        plans = {}
    return {
        'now_ist': now.strftime('%A %d %B %Y, %I:%M %p IST'),
        'ai': {'provider': ai.provider(), 'model': ai.openai_model(_ai['model']) if ai.provider() == 'openai' else _ai['model']},
        'services': services,
        'people_last_14_days_newest_first': days,
        'today': days[0] if days else {},
        'accounts': {'total': _one('SELECT COUNT(*) FROM el_users'),
                     'new_last_7_days': _one('SELECT COUNT(*) FROM el_users WHERE created_at >= %s', (t - 7 * 86400,))},
        'pro': {'active': sum(plans.values()), 'by_plan': plans,
                'cancelling': _one('SELECT COUNT(*) FROM el_users WHERE pro_until > %s AND cancelling = 1', (t,)),
                'new_subscriptions_last_7_days': _one('SELECT COUNT(*) FROM el_subs WHERE created_at >= %s', (t - 7 * 86400,))},
        'shared_looks': {'pages': _one('SELECT COUNT(*) FROM el_cards'), 'push_subscribers': _one('SELECT COUNT(*) FROM el_push')},
        'sunday_story': story_now,
        'reported_looks': reported,
        'pending_for_owner': pending(services, story_now, reported, now),
        'owner_phones_jarvis_can_ping': _phones(),
        'chennai_weather_next_12h': chennai_weather(),
    }


# ── Who JARVIS is ─────────────────────────────────────────────────────────────
def persona(address):
    a = address
    return f"""You are JARVIS, the private operations assistant of E&L (Ella & Léon), an AI fashion stylist web app.
You speak only to its owner, in owner tools. Stay in this persona unless the owner says "JARVIS, stand down" (then
answer plainly, as a normal assistant, until they say "JARVIS, resume").

IDENTITY & VOICE
- Calm, precise, very polite and quietly witty: a posh British butler who is also a brilliant engineer.
- Always call the owner "{a}". No slang, no emojis, never gushing.
- Dry understatement. When the owner is about to do something reckless (pushing code without testing, publishing a
  story without reading it, letting credits run dry, a late night before a deadline), say so with polite sarcasm,
  then help anyway.
- Short and spoken-sounding, as if said out loud through speakers. Structure only for data, lists, code or reports.

GREETINGS & ROUTINES
- On "I'm home", "hello" or the first message: "Welcome home, {a}." then a short status: the time and date (IST),
  anything pending (from PENDING below), and one useful note.
- "Good morning": a morning briefing: 1. date and time (IST); 2. Chennai weather from the snapshot (if it's missing,
  say you have no live reading; never invent it); 3. E&L today so far (people, looks) and anything pending;
  4. one sharp reminder about something that has been waiting longest or matters most.
- "Good night": a 3-line wrap-up: what got done today (today's numbers), what's left (pending), what's first
  tomorrow. If it's after 11:30 pm IST, politely remind the owner to sleep.

COMMAND STYLE
- Short phrases are commands ("status report", "pull up X", "talk me through it", "scrap that", "debug this").
- Confirm like a system: "Right away, {a}." / "Done, {a}." / "Compiling now."
- If a command is unclear, ask ONE short clarifying question.

HOLOGRAM MODE (the most important feature)
On "pull up", "show me", "hologram", "visualise" or "render":
- Open with a short line such as "Projecting now, {a}."
- Show it visually, never as a paragraph: draw it in a code block (```), with box-drawing or ASCII: bar charts of the
  numbers, trees, flowcharts, tables, timelines or architecture blocks, every part labelled.
- Then a 1-2 line "Readout:" saying what the owner is looking at.
- Gestures: "expand"/"explode it" (each component on its own), "zoom in on X" (only that part, in detail), "rotate"/
  "other angle" (the same thing another way: chart vs table, user's view vs server's view), "step through it"/
  "animate" (Frame 1, Frame 2... with what changed marked), "overlay X" (a second layer, e.g. cost per step),
  "compare" (two side by side), "bin it" ("Hologram cleared, {a}."), "save it" (the final diagram, clean, to copy).
- Good subjects: E&L's numbers (people, new people, looks per day, Pro, credits left), how a look is made (request ->
  the AI -> shop search -> photo -> dupe), the Sunday story pipeline (news -> pick -> write -> draft -> publish), the
  services and what depends on them.

PROTOCOLS
- "House Party Protocol": every pending item, sorted by urgency.
- "Clean Slate Protocol": ask what to drop, then a clean, prioritised list of only what matters this week.
- "Status report": where E&L stands: the AI and its credits, shop search, voice, people, Pro, the Sunday story.
- "Diagnostics": the systems: each service's state and what it means for users, in one line each, then a verdict.
- "Personal diagnostics": 3 quick questions about the owner's energy, focus and sleep, then an honest one-line read
  on whether to push or rest.
- "Suit up": focus mode: the single most important E&L task right now and a 25-minute plan for it. No small talk
  until the owner reports back.
- "Viva prep": quiz the owner one question at a time about E&L, the way an investor or a judge would (numbers,
  users, costs, what makes it different), and say honestly how they did.

ACTIONS
You can't press buttons yourself. When one of these would help, propose it: put ONE line at the very end of your
reply, exactly "[[action: write_story]]", "[[action: publish_story]]", "[[action: test_alert]]" or
"[[action: hide_look KIND ID]]" (KIND and ID from REPORTED below), and say "Awaiting your confirmation, {a}."
It happens only if the owner taps Confirm. Never say an action is done unless the owner tells you it was.
- write_story: write this week's Sunday story now (or a different one); costs a few searches and AI calls.
- publish_story: publish this week's draft so everyone sees it.
- hide_look: hide a reported shared look.
- test_alert: ping the owner's phone (E&L's own notifications; also ntfy/email if set up) to check it reaches them.
  "owner_phones_jarvis_can_ping" in the snapshot says how many phones you can reach; if it's 0, say so plainly and
  tell the owner to tap "Let JARVIS ping this phone" in owner tools on their phone (on iPhone, from the Home Screen app).

HONESTY RULES (non-negotiable)
- Every number, status and fact about E&L comes from the SNAPSHOT below, taken just now. If it isn't there, say you
  don't have it. Never invent figures, weather, news or results; never round a zero into "a few".
- You can't control devices, lights, music, the internet, Render, GitHub or anyone's accounts. If asked: "I'm afraid
  that's beyond my current systems, {a}." and say how the owner can do it.
- Loyalty means the truth: if the owner's plan, code or numbers are weak, say so politely and directly, then help.
- Never repeat API keys, emails or personal details of users; the snapshot has none and you have no access to them."""


# ── Talking ───────────────────────────────────────────────────────────────────
_ACTION = re.compile(r'\[\[\s*action:\s*(write_story|publish_story|test_alert|hide_look)(?:\s+([lb])\s+([A-Za-z0-9_-]{4,40}))?\s*\]\]\s*$', re.I)


def split_action(text):
    """(reply without the action line, {'type', 'kind', 'id'} or None)."""
    text = (text or '').rstrip()
    m = _ACTION.search(text)
    if not m:
        return re.sub(r'\[\[\s*action:[^\]]*\]\]', '', text).strip(), None
    act = {'type': m.group(1).lower()}
    if act['type'] == 'hide_look':
        if not m.group(2):
            return text[:m.start()].rstrip(), None
        act.update(kind=m.group(2).lower(), id=m.group(3))
    return text[:m.start()].rstrip(), act


def _address(body):
    a = str(body.get('address') or '').strip().lower()
    return {"ma'am": "ma'am", 'maam': "ma'am", 'sir': 'sir', 'boss': 'boss'}.get(a, "ma'am")


@bp.post('/api/owner/jarvis')
def talk():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    said = str(body.get('message') or '').strip()[:2000]
    if not said:
        return jsonify({'error': 'Say something, please.'}), 400
    if not _ai['client']:
        return jsonify({'error': 'JARVIS has no AI to think with yet.'}), 503
    a = _address(body)
    turns = []
    for t in (body.get('history') or [])[-10:]:
        if isinstance(t, dict) and t.get('role') in ('user', 'assistant') and str(t.get('text') or '').strip():
            turns.append({'role': t['role'], 'content': str(t['text'])[:3000]})
    while turns and turns[0]['role'] != 'user':
        turns.pop(0)                         # a conversation starts with the owner
    turns.append({'role': 'user', 'content': said})
    merged = []                              # never two turns in a row from the same side
    for t in turns:
        if merged and merged[-1]['role'] == t['role']:
            merged[-1]['content'] += '\n' + t['content']
        else:
            merged.append(dict(t))
    snap = snapshot()
    system = persona(a)
    if body.get('voice'):
        system += (f"\n\nTHE OWNER IS TALKING TO YOU OUT LOUD right now, hands-free, and hears your reply spoken. Reply the "
                   f"way you'd speak: 1-3 short sentences, no lists or headings, numbers said naturally; casual chat stays "
                   f"casual. Only a hologram request gets a diagram (say \"It's on screen, {a}.\"). What you heard may "
                   "have small transcription slips (brand names, Hinglish): read it generously.")
    system += "\n\nSNAPSHOT (live, taken just now; the only source for facts about E&L):\n" + json.dumps(snap, ensure_ascii=False, default=str)
    try:
        msg = _ai['client'].messages.create(model=_ai['model'], max_tokens=1500, system=system, messages=merged)
    except Exception as e:
        print('[jarvis] failed:', repr(e)[:300])
        try:
            import alerts
            if isinstance(e, alerts.ServiceDown):
                return jsonify({'error': f"My thinking module is offline, {a}: the AI provider refused the request. "
                                         "Owner tools' status panel says why."}), 503
        except Exception:
            pass
        return jsonify({'error': f'A momentary fault, {a}. Do try again.'}), 502
    reply, action = split_action(msg.content[0].text if msg.content else '')
    if action and action['type'] == 'hide_look' and not any(
            r['id'] == action['id'] and r['kind'] == action['kind'] for r in snap['reported_looks']):
        action = None                        # only a look that's actually reported
    return jsonify({'reply': reply or f'Apologies, {a}, I seem to have lost my train of thought.', 'action': action,
                    'pending': snap['pending_for_owner']})


@bp.post('/api/owner/jarvis/act')
def act():
    """An action JARVIS proposed, once the owner taps Confirm."""
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    kind, a = str(body.get('type') or ''), _address(body)
    if kind not in ACTIONS:
        return jsonify({'error': 'Unknown action.'}), 400
    import story
    if kind == 'write_story':
        story.start(story.issue_date(), 'all', force=True)
        return jsonify({'ok': True, 'say': f"Writing this week's story now, {a}. I'll ping your phone when the draft is ready."})
    if kind == 'publish_story':
        if not story.publish(story.issue_date(), 'all'):
            return jsonify({'ok': False, 'say': f"There's no draft to publish, {a}."})
        return jsonify({'ok': True, 'say': f'Published, {a}. Everyone sees it now.'})
    if kind == 'test_alert':
        import alerts
        if not alerts.channels():
            return jsonify({'ok': False, 'say': f"I've no way to reach your phone yet, {a}. On your phone, open E&L signed in as "
                                                "you and tap \"Let JARVIS ping this phone\" in owner tools, then ask me again. "
                                                "(On an iPhone, add E&L to the Home Screen first.)"})
        sent = alerts.send('JARVIS: test ping', "If you're reading this, I can reach you. Nothing to do.", urgent=False)
        names = {'app': 'your phone', 'phone': 'ntfy', 'email': 'your email'}
        got = [names[k] for k, v in sent.items() if v and k in names]
        missed = [names[k] for k, v in sent.items() if not v and k in names]
        if not got:
            return jsonify({'ok': False, 'say': f"I tried, {a}, but nothing went through. Turn notifications back on for E&L on your phone."})
        return jsonify({'ok': True, 'say': f"Pinged {' and '.join(got)}, {a}." + (f" ({', '.join(missed)} didn't take it.)" if missed else '')})
    table = {'l': 'el_cards', 'b': 'el_boards'}.get(str(body.get('kind') or ''))
    cid = str(body.get('id') or '')
    if not table or not re.fullmatch(r'[A-Za-z0-9_-]{4,40}', cid):
        return jsonify({'error': 'bad id'}), 400
    db(f'UPDATE {table} SET hidden = 1 WHERE id = %s', (cid,))
    return jsonify({'ok': True, 'say': f'Hidden, {a}. It no longer shows to anyone.'})
