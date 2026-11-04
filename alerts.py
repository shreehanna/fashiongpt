"""Tells the owner when a paid API is running low or has run out, so it can be topped up in time.

What it watches
  Claude (styling)     out of credit / key rejected: straight from Anthropic's error.
                       Running low: Anthropic doesn't tell apps the balance, so after each top-up the
                       owner types the new balance into the app (Pro sheet, owner view) and E&L
                       subtracts an estimate of every look's cost from it.
  ElevenLabs (voice)   out / key rejected: from ElevenLabs' error. Running low: asks ElevenLabs how many
                       characters are left, at most every few hours.
  Serper (shop links)  out / key rejected: from Serper's error. Running low: same as Claude
                       (you type the credits you have, every search uses one).

Where alerts go (Render -> Environment; set one or both)
  NTFY_TOPIC                   a push on your phone through the free ntfy app. Use a long, hard-to-guess
                               topic name (anyone who knows it can read the alerts).
  RESEND_API_KEY + ALERT_EMAIL an email through resend.com. Render's free plan blocks normal email
                               (SMTP), so it has to go through an email API. Without your own domain,
                               Resend only delivers to the address you signed up to Resend with.
  ALERT_LOW_PERCENT            warn when this much is left (default 20)
The owner also sees a banner on the site while anything is low or out.
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import requests as req
from flask import Blueprint, jsonify, request

IST = timezone(timedelta(hours=5, minutes=30))
REMIND_EVERY = 12 * 3600          # repeat an unresolved alert twice a day, not on every request
VOICE_CHECK_EVERY = 6 * 3600
# USD per million tokens (input, output). An estimate; override with CLAUDE_PRICE_IN / CLAUDE_PRICE_OUT.
CLAUDE_PRICES = {'haiku': (1.0, 5.0), 'sonnet': (3.0, 15.0), 'opus': (5.0, 25.0)}

SERVICES = {
    'claude': {'name': 'Claude', 'what': 'styling', 'unit': 'usd', 'env': 'ANTHROPIC_API_KEY',
               'link': 'https://console.anthropic.com/settings/billing'},
    'voice': {'name': 'ElevenLabs', 'what': 'voice', 'unit': 'chars', 'env': 'ELEVENLABS_API_KEY',
              'link': 'https://elevenlabs.io/app/subscription'},
    'search': {'name': 'Serper', 'what': 'shop links', 'unit': 'credits', 'env': 'SERPER_API_KEY',
               'link': 'https://serper.dev/'},
}
SEVERITY = {'ok': 0, 'off': 0, 'low': 1, 'out': 2, 'key': 2}

bp = Blueprint('alerts', __name__)


def env(k):
    return (os.environ.get(k) or '').strip()


def low_percent():
    try:
        return min(90.0, max(1.0, float(env('ALERT_LOW_PERCENT') or 20)))
    except ValueError:
        return 20.0


# ── Background worker: alerts never slow a look down or break it ─────────────
_jobs = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def _run():
    while True:
        fn, args = _jobs.get()
        try:
            fn(*args)
        except Exception as e:
            print('[alerts]', getattr(fn, '__name__', 'job'), 'failed:', e)
        finally:
            _jobs.task_done()


def later(fn, *args):
    global _worker
    if _worker is None or not _worker.is_alive():
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, name='el-alerts', daemon=True)
                _worker.start()
    _jobs.put((fn, args))


def flush():
    """Wait for queued alert work (tests, and the owner's status view)."""
    _jobs.join()


# ── Stored state (el_kv) ──────────────────────────────────────────────────────
def _get(k):
    from pro import kv_get
    v = kv_get(k)
    return json.loads(v) if v else None


def _set(k, v):
    from pro import kv_set
    kv_set(k, json.dumps(v))


def _fmt(svc, amount):
    unit = SERVICES[svc]['unit']
    if unit == 'usd':
        return f'${amount:,.2f}'
    return f'{int(round(amount)):,} {"characters" if unit == "chars" else "credits"}'


# ── Sending ───────────────────────────────────────────────────────────────────
def channels():
    out = []
    if env('NTFY_TOPIC'):
        out.append('phone')
    if env('RESEND_API_KEY') and env('ALERT_EMAIL'):
        out.append('email')
    return out


def send(title, text, link=None, urgent=True):
    """Push + email to whatever is set up. Returns {'phone': True/False, 'email': ...}."""
    sent = {}
    full = text + (f'\n\nTop up: {link}' if link else '')
    if env('NTFY_TOPIC'):
        try:
            body = {'topic': env('NTFY_TOPIC'), 'title': title, 'message': full,
                    'priority': 4 if urgent else 3, 'tags': ['warning' if urgent else 'white_check_mark']}
            if link:
                body['click'] = link
            r = req.post(env('NTFY_SERVER') or 'https://ntfy.sh', json=body, timeout=10)
            sent['phone'] = r.status_code < 300
            if not sent['phone']:
                print('[alerts] ntfy said', r.status_code, r.text[:200])
        except req.RequestException as e:
            sent['phone'] = False
            print('[alerts] ntfy unreachable:', e)
    if env('RESEND_API_KEY') and env('ALERT_EMAIL'):
        try:
            r = req.post('https://api.resend.com/emails', timeout=10,
                         headers={'Authorization': 'Bearer ' + env('RESEND_API_KEY')},
                         json={'from': env('ALERT_FROM') or 'E&L Alerts <onboarding@resend.dev>',
                               'to': [e.strip() for e in env('ALERT_EMAIL').split(',') if e.strip()],
                               'subject': title, 'text': full})
            sent['email'] = r.status_code < 300
            if not sent['email']:
                print('[alerts] Resend said', r.status_code, r.text[:200])
        except req.RequestException as e:
            sent['email'] = False
            print('[alerts] Resend unreachable:', e)
    if not sent:
        print('[alerts] (no alert channel set up)', title, '-', text)
    return sent


def _title(svc, level):
    s = SERVICES[svc]
    return {
        'low': f"E&L: {s['name']} ({s['what']}) is running low",
        'out': f"E&L: {s['name']} has run out. {s['what'].capitalize()} is down",
        'key': f"E&L: {s['name']} rejected the API key. {s['what'].capitalize()} is down",
        'ok': f"E&L: {s['name']} is working again",
    }[level]


def report(svc, level, text=''):
    """Record a service's state; alert on a change, and again every 12 hours while it isn't fixed."""
    cur = _get('alert:' + svc) or {'level': 'ok'}
    t = time.time()
    if level == 'ok':
        if cur.get('level', 'ok') == 'ok':
            return
        _set('alert:' + svc, {'level': 'ok', 'text': '', 'at': t, 'sent': t})
        send(_title(svc, 'ok'), 'Back to normal, nothing to do.', urgent=False)
        return
    changed = cur.get('level') != level
    state = {'level': level, 'text': text, 'at': t if changed else cur.get('at', t), 'sent': cur.get('sent', 0)}
    if changed or t - float(cur.get('sent') or 0) > REMIND_EVERY:
        state['sent'] = t
        _set('alert:' + svc, state)
        send(_title(svc, level), text, SERVICES[svc]['link'])
    elif text != cur.get('text'):
        _set('alert:' + svc, state)          # keep the numbers fresh without sending again


def _resolved(svc):
    """A call just worked, so 'out' / 'key rejected' is over (a 'low' warning stays until topped up)."""
    cur = _get('alert:' + svc)
    if cur and cur.get('level') in ('out', 'key'):
        report(svc, 'ok')


# ── Balances you type in (Claude, Serper) ─────────────────────────────────────
def _spend(svc, amount):
    b = _get('bal:' + svc) or {'balance': None, 'spent': 0.0}
    b['spent'] = float(b.get('spent') or 0) + amount
    _set('bal:' + svc, b)
    _resolved(svc)
    bal = b.get('balance')
    if not bal:
        return
    left = bal - b['spent']
    if left <= bal * low_percent() / 100:
        have = (f"By E&L's estimate the {_fmt(svc, bal)} you added is about used up" if left < bal * 0.01 else
                f"About {_fmt(svc, left)} left of the {_fmt(svc, bal)} you added (E&L's estimate)")
        report(svc, 'low', have + ". Top up soon, then type the new balance into E&L.")


def set_balance(svc, amount):
    _set('bal:' + svc, {'balance': float(amount), 'spent': 0.0, 'set_at': time.time()})
    cur = _get('alert:' + svc)
    if cur and cur.get('level') in ('low', 'out'):
        report(svc, 'ok')


# ── Claude ────────────────────────────────────────────────────────────────────
class ServiceDown(Exception):
    """Raised instead of the raw API error, so visitors only ever see a friendly line."""


def claude_problem(e):
    status = getattr(e, 'status_code', None)
    text = str(e).lower()
    body = getattr(e, 'body', None)
    etype = ''
    if isinstance(body, dict):
        etype = str((body.get('error') or {}).get('type') or '').lower()
    if status == 402 or etype in ('billing_error', 'payment_required') or any(w in text for w in (
            'credit balance', 'purchase credits', 'plans & billing', 'billing', 'spend limit', 'usage limit')):
        return 'out'
    if status in (401, 403) or etype in ('authentication_error', 'permission_error'):
        return 'key'
    return None


def _claude_cost(model, usage):
    pin, pout = next((v for k, v in CLAUDE_PRICES.items() if k in (model or '').lower()), CLAUDE_PRICES['sonnet'])
    pin = float(env('CLAUDE_PRICE_IN') or pin)
    pout = float(env('CLAUDE_PRICE_OUT') or pout)
    tin = (getattr(usage, 'input_tokens', 0) or 0) \
        + 1.25 * (getattr(usage, 'cache_creation_input_tokens', 0) or 0) \
        + 0.1 * (getattr(usage, 'cache_read_input_tokens', 0) or 0)
    return (tin * pin + (getattr(usage, 'output_tokens', 0) or 0) * pout) / 1e6


def watch_claude(client):
    """Wrap client.messages.create: count what each call costs, and catch running out."""
    original = client.messages.create
    if getattr(original, '_el_watched', False):
        return

    def create(*a, **kw):
        try:
            msg = original(*a, **kw)
        except Exception as e:
            kind = claude_problem(e)
            if not kind:
                raise
            text = ("Anthropic says the credit balance is too low, so no looks can be made. Add credits "
                    "(or switch on auto-reload) in the Claude Console." if kind == 'out' else
                    f"Anthropic refused the API key ({getattr(e, 'status_code', '?')}). Check ANTHROPIC_API_KEY "
                    "in Render: was the key deleted or changed?")
            later(report, 'claude', kind, text)
            raise ServiceDown("Ella's taking a short break. Please try again in a little while.") from e
        usage = getattr(msg, 'usage', None)
        if usage is not None:
            later(_spend, 'claude', _claude_cost(kw.get('model', ''), usage))
        else:
            later(_resolved, 'claude')
        return msg

    create._el_watched = True
    client.messages.create = create


# ── ElevenLabs ────────────────────────────────────────────────────────────────
def voice_problem(status, text):
    t = (text or '').lower()
    if status == 402 or 'quota_exceeded' in t or 'exceeds your quota' in t or \
            ('credit' in t and ('insufficient' in t or 'not enough' in t or 'exceed' in t)):
        return 'out'
    if status in (401, 403) and 'missing_permissions' not in t:
        return 'key'
    return None


def voice_result(r):
    """Call after every ElevenLabs request with its response."""
    try:
        if r.status_code == 200:
            later(_voice_ok)
            return
        kind = voice_problem(r.status_code, r.text[:600])
        if kind:
            text = ("ElevenLabs says the character quota is used up, so Ella and Léon can't speak or listen."
                    if kind == 'out' else
                    f"ElevenLabs refused the request ({r.status_code}): {r.text[:160]}. Check ELEVENLABS_API_KEY "
                    "in Render, or whether ElevenLabs paused the account.")
            later(report, 'voice', kind, text)
    except Exception as e:
        print('[alerts] voice_result failed:', e)


def _voice_ok():
    _resolved('voice')
    check_voice()


def check_voice(force=False):
    """Ask ElevenLabs how many characters are left (at most every 6 hours unless forced)."""
    key = env('ELEVENLABS_API_KEY')
    if not key:
        return
    last = float(_get('voice:checked') or 0)
    if not force and time.time() - last < VOICE_CHECK_EVERY:
        return
    _set('voice:checked', time.time())
    r = req.get('https://api.elevenlabs.io/v1/user/subscription', headers={'xi-api-key': key}, timeout=8)
    if r.status_code != 200:
        # A restricted key without "User: read" can't see this; out/key problems still show up on real calls.
        _set('voice:left', {'unknown': True, 'why': f'ElevenLabs said {r.status_code}'})
        return
    d = r.json()
    limit = int(d.get('character_limit') or 0)
    used = int(d.get('character_count') or 0)
    reset = d.get('next_character_count_reset_unix')
    left = max(0, limit - used)
    _set('voice:left', {'left': left, 'limit': limit, 'reset': reset})
    if not limit:
        return
    when = ''
    if reset:
        when = ' It refills on ' + datetime.fromtimestamp(int(reset), IST).strftime('%-d %b') + '.'
    if left <= 0:
        report('voice', 'out', f'All {limit:,} characters for this period are used.{when}')
    elif left <= limit * low_percent() / 100:
        report('voice', 'low', f'{left:,} of {limit:,} characters left.{when}')
    else:
        report('voice', 'ok')


# ── Serper ────────────────────────────────────────────────────────────────────
def search_problem(status, text):
    t = (text or '').lower()
    if 'credit' in t or status == 402:
        return 'out'
    if status in (401, 403):
        return 'key'
    return None


def search_result(r):
    """Call after every Serper request with its response (each one uses a credit)."""
    try:
        if r.status_code == 200:
            later(_spend, 'search', 1.0)
            return
        kind = search_problem(r.status_code, r.text[:400])
        if kind:
            text = ("Serper says the search credits are used up, so Shop links and product photos can't "
                    "find anything." if kind == 'out' else
                    f"Serper refused the API key ({r.status_code}). Check SERPER_API_KEY in Render.")
            later(report, 'search', kind, text)
    except Exception as e:
        print('[alerts] search_result failed:', e)


# ── What the owner sees ───────────────────────────────────────────────────────
def status():
    out = []
    for svc, s in SERVICES.items():
        row = {'id': svc, 'name': s['name'], 'what': s['what'], 'unit': s['unit'], 'link': s['link'],
               'level': 'ok', 'text': '', 'left': None, 'balance': None}
        if not env(s['env']):
            row.update(level='off', text='Not set up')
            out.append(row)
            continue
        a = _get('alert:' + svc) or {}
        row['level'] = a.get('level') or 'ok'
        row['text'] = a.get('text') or ''
        if svc in ('claude', 'search'):
            b = _get('bal:' + svc) or {}
            if b.get('balance'):
                row['balance'] = b['balance']
                row['left'] = max(0.0, b['balance'] - float(b.get('spent') or 0))
            row['spent'] = float(b.get('spent') or 0)
        else:
            v = _get('voice:left') or {}
            if 'left' in v:
                row['left'], row['balance'], row['reset'] = v['left'], v['limit'], v.get('reset')
            elif v.get('unknown'):
                row['note'] = "Can't read the quota with this key (give it User: read access)."
        out.append(row)
    return out


def owner_payload():
    """Added to /api/me for the owner. Also nudges a fresh ElevenLabs check now and then."""
    try:
        later(check_voice)
        return {'health': status(), 'alert_channels': channels()}
    except Exception as e:
        print('[alerts] status failed:', e)
        return {'health': [], 'alert_channels': channels()}


def _owner_only():
    from pro import owner_request
    return owner_request()


@bp.get('/api/owner/status')
def owner_status():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    try:
        check_voice(force=True)
    except Exception as e:
        print('[alerts] voice check failed:', e)
    return jsonify({'health': status(), 'alert_channels': channels()})


@bp.post('/api/owner/balance')
def owner_balance():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    svc = body.get('service')
    try:
        amount = float(str(body.get('balance', '')).replace('$', '').replace(',', '').strip())
    except ValueError:
        return jsonify({'error': 'Type a number, like 10 or 2500.'}), 400
    if svc not in ('claude', 'search') or not (0 < amount < 1e7):
        return jsonify({'error': 'Type a number, like 10 or 2500.'}), 400
    set_balance(svc, amount)
    return jsonify({'health': status(), 'alert_channels': channels()})


@bp.post('/api/owner/test-alert')
def owner_test_alert():
    if not _owner_only():
        return jsonify({'error': 'not found'}), 404
    if not channels():
        return jsonify({'error': 'No alert channel is set up yet. Add NTFY_TOPIC, or RESEND_API_KEY and '
                                 'ALERT_EMAIL, in Render.'}), 400
    sent = send('E&L: test alert', "If you're reading this, API alerts reach you. Nothing to do.", urgent=False)
    return jsonify({'sent': sent})
