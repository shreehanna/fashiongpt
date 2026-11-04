"""E&L Pro: Google sign-in, worldwide subscriptions and the daily look limit.

Two payment providers, chosen by where the customer pays from:
  India              Razorpay       ₹199 / ₹1,999, UPI Autopay, cards, net banking (about 3.5% in fees)
  everywhere else    Dodo Payments  USD (shown in local currency), cards, Apple/Google Pay, PayPal.
                                    Dodo is the merchant of record, so it collects and pays foreign
                                    sales tax / VAT for you and pays out to your Indian bank.

Plug it in from app.py with:  from pro import init_pro;  init_pro(app, ['get_outfit', 'restyle'])

Everything secret lives in Render's environment, never in index.html.
Needed for any payments:
  SECRET_KEY               long random string that signs the login cookie
  DATABASE_URL             Postgres connection string (Neon's free tier works)
  GOOGLE_CLIENT_ID         OAuth "Web application" client ID from Google Cloud
India (Razorpay):
  RAZORPAY_KEY_ID          rzp_test_... while testing, rzp_live_... after KYC
  RAZORPAY_KEY_SECRET
  RAZORPAY_WEBHOOK_SECRET  the secret you type when you add the webhook in Razorpay
Rest of the world (Dodo Payments):
  DODO_API_KEY
  DODO_WEBHOOK_SECRET      starts with whsec_
  DODO_PRODUCT_MONTHLY     product IDs of the two subscription products you create in Dodo
  DODO_PRODUCT_YEARLY
  DODO_MODE                test (default) or live
Optional:
  INTL_PRICE_MONTHLY (4.99)  INTL_PRICE_YEARLY (49.99)   shown in the app; must match Dodo
  RAZORPAY_PLAN_MONTHLY / RAZORPAY_PLAN_YEARLY            created automatically if not set
  FREE_LOOKS_PER_DAY (3)  PRO_LOOKS_PER_DAY (50, a fair-use ceiling against scripts)
  OWNER_EMAILS             the owner's Google account(s), comma-separated: everything unlocked, never pays,
                           no daily limits. Kept in Render rather than in the code so it isn't public.

Until the core three plus at least one provider are set, nothing here blocks anyone:
the app behaves exactly as before and the Pro sheet says payments aren't open yet.
"""
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests as req
from flask import Blueprint, jsonify, make_response, request, session

PRICES = {'monthly': 199, 'yearly': 1999}            # rupees (Razorpay)
CYCLES = {'monthly': 60, 'yearly': 5}                 # Razorpay billing cycles per subscription (5 years)
PERIOD_DAYS = {'monthly': 31, 'yearly': 366}
GRACE = 2 * 86400                                     # keep Pro 2 days past a renewal date while the bank retries
FREE_PER_DAY = int(os.environ.get('FREE_LOOKS_PER_DAY', 3))
PRO_PER_DAY = int(os.environ.get('PRO_LOOKS_PER_DAY', 50))
ANON_PER_IP_PER_DAY = 40                              # backstop for people clearing cookies in a loop
IST = timezone(timedelta(hours=5, minutes=30))
RZP_API = 'https://api.razorpay.com/v1'

bp = Blueprint('pro', __name__)


def env(name):
    return (os.environ.get(name) or '').strip()


def intl_prices():
    def num(name, default):
        try:
            return float(env(name) or default)
        except ValueError:
            return default
    return {'monthly': num('INTL_PRICE_MONTHLY', 4.99), 'yearly': num('INTL_PRICE_YEARLY', 49.99)}


def core_ready():
    return all(env(k) for k in ('SECRET_KEY', 'GOOGLE_CLIENT_ID')) and \
        (bool(env('DATABASE_URL')) or env('EL_ALLOW_SQLITE') == '1')


def razorpay_ready():
    return core_ready() and all(env(k) for k in ('RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET', 'RAZORPAY_WEBHOOK_SECRET'))


def dodo_ready():
    return core_ready() and all(env(k) for k in ('DODO_API_KEY', 'DODO_WEBHOOK_SECRET',
                                                 'DODO_PRODUCT_MONTHLY', 'DODO_PRODUCT_YEARLY'))


def payments_live():
    return razorpay_ready() or dodo_ready()


def test_mode():
    return (razorpay_ready() and env('RAZORPAY_KEY_ID').startswith('rzp_test_')) or \
        (dodo_ready() and (env('DODO_MODE') or 'test') != 'live')


def now():
    return int(time.time())


def today():
    return datetime.now(IST).strftime('%Y-%m-%d')


def client_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()


def to_ts(value):
    """Unix seconds from a Unix number or an ISO 8601 string (Dodo sends ISO dates)."""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp())
    except ValueError:
        return None


# ── Database: Postgres in production, SQLite for local runs ───────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS el_users (
         email TEXT PRIMARY KEY, google_sub TEXT, name TEXT, picture TEXT,
         created_at BIGINT, pro_until BIGINT NOT NULL DEFAULT 0, plan TEXT,
         sub_id TEXT, sub_status TEXT, sub_provider TEXT, cancelling INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS el_usage (
         who TEXT NOT NULL, day TEXT NOT NULL, looks INTEGER NOT NULL DEFAULT 0,
         PRIMARY KEY (who, day))""",
    """CREATE TABLE IF NOT EXISTS el_subs (
         sub_id TEXT PRIMARY KEY, email TEXT NOT NULL, plan TEXT, status TEXT, provider TEXT,
         created_at BIGINT, current_end BIGINT)""",
    "CREATE TABLE IF NOT EXISTS el_kv (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS el_events (id TEXT PRIMARY KEY, at BIGINT)",
]
_local = threading.local()
_schema_ready = False
_schema_lock = threading.Lock()


def _connect():
    url = env('DATABASE_URL')
    if url:
        import psycopg
        return psycopg.connect(url, autocommit=True, connect_timeout=10), 'pg'
    con = sqlite3.connect(env('EL_SQLITE_PATH') or 'el_local.db', isolation_level=None,
                          check_same_thread=False)
    return con, 'lite'


def _conn():
    c = getattr(_local, 'con', None)
    if c is None:
        _local.con = c = _connect()
    return c


def db(sql, params=(), one=False, many=False):
    """Run one statement. Reconnects once if the connection went stale (Neon sleeps when idle)."""
    global _schema_ready
    for attempt in (0, 1):
        con, kind = _conn()
        try:
            if not _schema_ready:
                with _schema_lock:
                    if not _schema_ready:
                        for s in SCHEMA:
                            con.execute(s)
                        _schema_ready = True
            q = sql if kind == 'pg' else sql.replace('%s', '?')
            cur = con.execute(q, params)
            if one:
                return cur.fetchone()
            if many:
                return cur.fetchall()
            return None
        except Exception:
            _local.con = None
            try:
                con.close()
            except Exception:
                pass
            if attempt:
                raise


def kv_get(k):
    row = db('SELECT v FROM el_kv WHERE k = %s', (k,), one=True)
    return row[0] if row else None


def kv_set(k, v):
    db('INSERT INTO el_kv (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = excluded.v', (k, v))


USER_COLS = ['email', 'name', 'picture', 'pro_until', 'plan', 'sub_id', 'sub_status', 'sub_provider', 'cancelling']


def get_user(email):
    row = db(f"SELECT {', '.join(USER_COLS)} FROM el_users WHERE email = %s", (email,), one=True)
    return dict(zip(USER_COLS, row)) if row else None


def grant(email, until, plan=None, sub_id=None, status=None, provider=None):
    """Extend Pro to `until` (never shortens it: a cancelled plan keeps its paid days)."""
    u = get_user(email)
    if not u:
        return
    db('UPDATE el_users SET pro_until = %s, plan = COALESCE(%s, plan), sub_id = COALESCE(%s, sub_id), '
       'sub_status = COALESCE(%s, sub_status), sub_provider = COALESCE(%s, sub_provider) WHERE email = %s',
       (max(int(u['pro_until'] or 0), int(until)), plan, sub_id, status, provider, email))


def seen_event(event_id):
    return bool(event_id) and bool(db('SELECT 1 FROM el_events WHERE id = %s', (event_id,), one=True))


def remember_event(event_id):
    if event_id:
        db('INSERT INTO el_events (id, at) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING', (event_id, now()))


# ── Who is asking ─────────────────────────────────────────────────────────────
def identity():
    email = session.get('email')
    if email:
        return email, 'u:' + email
    if 'anon' not in session:
        session['anon'] = secrets.token_urlsafe(12)
        session.permanent = True
    return None, 'a:' + session['anon']


def owner_emails():
    return {e.strip().lower() for e in env('OWNER_EMAILS').split(',') if e.strip()}


def is_owner(email):
    return bool(email) and email.lower() in owner_emails()


def owner_request():
    """True when the owner is signed in on this browser (Google-verified email in the login cookie)."""
    try:
        return is_owner(session.get('email'))
    except RuntimeError:     # outside a request
        return False


def is_pro(user):
    if user and is_owner(user.get('email')):
        return True
    return bool(user) and int(user.get('pro_until') or 0) > now()


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not payments_live():
            return jsonify({'error': 'payments are not switched on yet'}), 503
        if not session.get('email') or not get_user(session['email']):
            return jsonify({'error': 'sign in first'}), 401
        return view(*a, **kw)
    return wrapper


# ── Daily look limit (only enforced once payments are live) ───────────────────
def _bump(who, day, delta):
    if delta > 0:
        row = db('INSERT INTO el_usage (who, day, looks) VALUES (%s, %s, 1) '
                 'ON CONFLICT (who, day) DO UPDATE SET looks = el_usage.looks + 1 RETURNING looks',
                 (who, day), one=True)
        return int(row[0])
    db('UPDATE el_usage SET looks = looks - 1 WHERE who = %s AND day = %s AND looks > 0', (who, day))
    return None


def _looks_used(who, day):
    row = db('SELECT looks FROM el_usage WHERE who = %s AND day = %s', (who, day), one=True)
    return int(row[0]) if row else 0


def reserve_look():
    """Count the look up front so two quick taps can't both slip under the limit; refunded if it fails."""
    if not payments_live():
        return {'ok': True}
    try:
        email, who = identity()
        if is_owner(email):
            return {'ok': True}          # the owner is never counted or capped
        day = today()
        user = get_user(email) if email else None
        pro = is_pro(user)
        limit = PRO_PER_DAY if pro else FREE_PER_DAY
        if _bump(who, day, +1) > limit:
            _bump(who, day, -1)
            return {'ok': False, 'pro': pro, 'limit': limit, 'signed_in': bool(email)}
        ticket = {'ok': True, 'keys': [(who, day)]}
        if not email:
            ip_key = 'ip:' + client_ip()
            if _bump(ip_key, day, +1) > ANON_PER_IP_PER_DAY:
                _bump(ip_key, day, -1)
                _bump(who, day, -1)
                return {'ok': False, 'pro': False, 'limit': limit, 'signed_in': False}
            ticket['keys'].append((ip_key, day))
        if random.random() < 0.02:   # tidy up old counters now and then
            cutoff = (datetime.now(IST) - timedelta(days=7)).strftime('%Y-%m-%d')
            db('DELETE FROM el_usage WHERE day < %s', (cutoff,))
        return ticket
    except Exception as e:           # a database hiccup should never stop someone getting a look
        print('[pro] usage check skipped:', e)
        return {'ok': True}


def refund_look(ticket):
    for who, day in ticket.get('keys', []):
        try:
            _bump(who, day, -1)
        except Exception as e:
            print('[pro] refund failed:', e)


def metered(view):
    @wraps(view)
    def wrapper(*a, **kw):
        ticket = reserve_look()
        if not ticket['ok']:
            return jsonify({'error': 'limit', 'pro': ticket['pro'], 'limit': ticket['limit'],
                            'signed_in': ticket['signed_in']}), 402
        try:
            resp = make_response(view(*a, **kw))
        except Exception:
            refund_look(ticket)
            raise
        if resp.status_code >= 400:
            refund_look(ticket)
        return resp
    return wrapper


# ── Providers ─────────────────────────────────────────────────────────────────
class ProviderError(Exception):
    pass


def _call(method, url, **kw):
    r = req.request(method, url, timeout=20, **kw)
    if r.status_code >= 400:
        try:
            err = r.json()
            msg = (err.get('error') or {}).get('description') if isinstance(err.get('error'), dict) else err.get('message')
        except ValueError:
            msg = r.text[:200]
        raise ProviderError(f'{r.status_code}: {msg}')
    return r.json() if r.content else {}


def rzp(method, path, **kw):
    return _call(method, RZP_API + path, auth=(env('RAZORPAY_KEY_ID'), env('RAZORPAY_KEY_SECRET')), **kw)


def dodo(method, path, **kw):
    base = 'https://live.dodopayments.com' if env('DODO_MODE') == 'live' else 'https://test.dodopayments.com'
    return _call(method, base + path, headers={'Authorization': 'Bearer ' + env('DODO_API_KEY')}, **kw)


def razorpay_plan_id(plan):
    """Use RAZORPAY_PLAN_MONTHLY/YEARLY if set, otherwise create the plan once and remember it.
    Test and live mode have separate plans, so the key remembers which mode it belongs to."""
    override = env('RAZORPAY_PLAN_' + plan.upper())
    if override:
        return override
    mode = 'test' if env('RAZORPAY_KEY_ID').startswith('rzp_test_') else 'live'
    key = f'plan:{mode}:{plan}:{PRICES[plan]}'
    pid = kv_get(key)
    if pid:
        return pid
    created = rzp('POST', '/plans', json={
        'period': plan, 'interval': 1,
        'item': {'name': f"E&L Pro ({'Monthly' if plan == 'monthly' else 'Yearly'})",
                 'amount': PRICES[plan] * 100, 'currency': 'INR',
                 'description': 'Unlimited looks from Ella and Léon'},
    })
    kv_set(key, created['id'])
    return created['id']


def hmac_hex(secret, message):
    return hmac.new(secret.encode(), message if isinstance(message, bytes) else message.encode(),
                    hashlib.sha256).hexdigest()


def dodo_signature_ok(raw, headers):
    """Standard Webhooks: base64 HMAC-SHA256 of "{id}.{timestamp}.{body}" with the decoded whsec_ key."""
    wid, ts, sigs = headers.get('webhook-id'), headers.get('webhook-timestamp'), headers.get('webhook-signature', '')
    if not (wid and ts and sigs):
        return False
    try:
        if abs(now() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    secret = env('DODO_WEBHOOK_SECRET')
    key = base64.b64decode(secret[6:] if secret.startswith('whsec_') else secret)
    expected = base64.b64encode(hmac.new(key, f'{wid}.{ts}.'.encode() + raw, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(',', 1)[1])
               for part in sigs.split() if part.startswith('v1,'))


def dodo_plan(product_id):
    return {env('DODO_PRODUCT_MONTHLY'): 'monthly', env('DODO_PRODUCT_YEARLY'): 'yearly'}.get(product_id)


def apply_dodo_subscription(data, email_hint=None):
    """Bring our records in line with a Dodo subscription object (from a webhook or a fetch)."""
    sub_id = data.get('subscription_id')
    meta = data.get('metadata') or {}
    email = (meta.get('email') or email_hint or (data.get('customer') or {}).get('email') or '').lower()
    if not sub_id or not email or not get_user(email):
        return None
    plan = meta.get('plan') or dodo_plan(data.get('product_id')) or 'monthly'
    status = data.get('status') or ''
    end = to_ts(data.get('next_billing_date'))
    db('INSERT INTO el_subs (sub_id, email, plan, status, provider, created_at, current_end) '
       "VALUES (%s, %s, %s, %s, 'dodo', %s, %s) ON CONFLICT (sub_id) DO UPDATE SET "
       'status = excluded.status, current_end = COALESCE(excluded.current_end, el_subs.current_end)',
       (sub_id, email, plan, status, now(), end))
    if status == 'active':
        grant(email, (end or now() + PERIOD_DAYS[plan] * 86400) + GRACE,
              plan=plan, sub_id=sub_id, status='active', provider='dodo')
        db('UPDATE el_users SET cancelling = %s WHERE email = %s AND sub_id = %s',
           (1 if data.get('cancel_at_next_billing_date') else 0, email, sub_id))
    else:
        db('UPDATE el_users SET sub_status = %s WHERE email = %s AND sub_id = %s', (status, email, sub_id))
        if status in ('cancelled', 'expired'):
            db('UPDATE el_users SET cancelling = 1 WHERE email = %s AND sub_id = %s', (email, sub_id))
    return email


# ── Routes ────────────────────────────────────────────────────────────────────
def me_payload():
    live = payments_live()
    out = {'payments_live': live, 'test_mode': live and test_mode(),
           'regions': {'in': razorpay_ready(), 'intl': dodo_ready()},
           'google_client_id': env('GOOGLE_CLIENT_ID') or None,
           'prices': PRICES, 'intl_prices': intl_prices(), 'free_per_day': FREE_PER_DAY,
           'signed_in': False, 'pro': False, 'looks_left': None}
    if not live:
        return out
    email, who = identity()
    user = get_user(email) if email else None
    if email and not user:
        session.pop('email', None)
        email, who = identity()
    pro = is_pro(user)
    out.update(signed_in=bool(user), pro=pro)
    if user and is_owner(user['email']):
        out.update(owner=True, email=user['email'], name=user['name'], picture=user['picture'], renews=False)
        return out
    if user:
        end = None
        if pro and user['sub_id']:
            row = db('SELECT current_end FROM el_subs WHERE sub_id = %s', (user['sub_id'],), one=True)
            end = int(row[0]) if row and row[0] else None
        out.update(email=user['email'], name=user['name'], picture=user['picture'], plan=user['plan'],
                   provider=user['sub_provider'], pro_until=int(user['pro_until'] or 0),
                   period_end=end or (int(user['pro_until'] or 0) or None),
                   renews=pro and user['sub_status'] in ('active', 'authenticated') and not user['cancelling'])
    if not pro:
        out['looks_left'] = max(0, FREE_PER_DAY - _looks_used(who, today()))
    return out


@bp.get('/api/me')
def me():
    try:
        return jsonify(me_payload())
    except Exception as e:
        print('[pro] /api/me failed:', e)
        return jsonify({'payments_live': False, 'error': 'unavailable'})


@bp.post('/api/auth/google')
def auth_google():
    if not payments_live():
        return jsonify({'error': 'sign-in is not switched on yet'}), 503
    token = (request.get_json(silent=True) or {}).get('credential')
    if not token:
        return jsonify({'error': 'missing credential'}), 400
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        info = id_token.verify_oauth2_token(token, google_requests.Request(), env('GOOGLE_CLIENT_ID'))
    except ValueError:
        return jsonify({'error': 'Google sign-in could not be verified'}), 401
    if not info.get('email_verified'):
        return jsonify({'error': 'Google says this email is not verified'}), 401
    email = info['email'].lower()
    db('INSERT INTO el_users (email, google_sub, name, picture, created_at) VALUES (%s, %s, %s, %s, %s) '
       'ON CONFLICT (email) DO UPDATE SET google_sub = excluded.google_sub, name = excluded.name, '
       'picture = excluded.picture',
       (email, info.get('sub'), info.get('name') or info.get('given_name') or '', info.get('picture') or '', now()))
    session['email'] = email
    session.permanent = True
    return jsonify(me_payload())


@bp.post('/api/auth/logout')
def logout():
    session.pop('email', None)
    return jsonify(me_payload())


@bp.post('/api/pro/subscribe')
@login_required
def subscribe():
    body = request.get_json(silent=True) or {}
    plan = body.get('plan')
    region = 'intl' if body.get('region') == 'intl' else 'in'
    if plan not in PRICES:
        return jsonify({'error': 'unknown plan'}), 400
    if region == 'in' and not razorpay_ready():
        region = 'intl'
    if region == 'intl' and not dodo_ready():
        return jsonify({'error': 'International payments are not open yet.'}), 503
    user = get_user(session['email'])
    if is_owner(user['email']):
        return jsonify({'error': "You're the owner, so everything's already unlocked."}), 409
    if is_pro(user) and user['sub_status'] in ('active', 'authenticated') and not user['cancelling']:
        return jsonify({'error': 'You already have Pro.'}), 409
    try:
        if region == 'in':
            sub = rzp('POST', '/subscriptions', json={
                'plan_id': razorpay_plan_id(plan), 'total_count': CYCLES[plan], 'quantity': 1,
                'customer_notify': 1, 'notes': {'email': user['email'], 'plan': plan},
            })
            db("INSERT INTO el_subs (sub_id, email, plan, status, provider, created_at) "
               "VALUES (%s, %s, %s, %s, 'razorpay', %s)",
               (sub['id'], user['email'], plan, sub.get('status', 'created'), now()))
            return jsonify({'provider': 'razorpay', 'subscription_id': sub['id'], 'key_id': env('RAZORPAY_KEY_ID'),
                            'description': f"Pro · ₹{PRICES[plan]:,}/{'month' if plan == 'monthly' else 'year'}",
                            'name': user['name'], 'email': user['email']})
        home = env('PUBLIC_URL').rstrip('/') or request.host_url.rstrip('/')
        if os.environ.get('RENDER') and home.startswith('http://'):
            home = 'https://' + home[len('http://'):]   # Render terminates https in front of the app
        session_ = dodo('POST', '/checkouts', json={
            'product_cart': [{'product_id': env('DODO_PRODUCT_' + plan.upper()), 'quantity': 1}],
            'customer': {'email': user['email'], 'name': user['name'] or user['email'].split('@')[0]},
            'metadata': {'email': user['email'], 'plan': plan},
            'return_url': home + '/?pro=welcome',
            'cancel_url': home + '/?pro=cancelled',
        })
        return jsonify({'provider': 'dodo', 'checkout_url': session_['checkout_url']})
    except ProviderError as e:
        print('[pro] checkout start failed:', e)
        return jsonify({'error': 'Could not start checkout. Please try again.'}), 502


@bp.post('/api/pro/verify')
@login_required
def verify():
    """Razorpay's checkout hands back a signed payment; check the signature, then switch Pro on."""
    body = request.get_json(silent=True) or {}
    pay_id = body.get('razorpay_payment_id') or ''
    sub_id = body.get('razorpay_subscription_id') or ''
    signature = body.get('razorpay_signature') or ''
    row = db("SELECT plan FROM el_subs WHERE sub_id = %s AND email = %s AND provider = 'razorpay'",
             (sub_id, session['email']), one=True)
    if not row:
        return jsonify({'error': 'unknown subscription'}), 404
    expected = hmac_hex(env('RAZORPAY_KEY_SECRET'), f'{pay_id}|{sub_id}')
    if not (pay_id and hmac.compare_digest(expected, signature)):
        return jsonify({'error': 'payment could not be verified'}), 400
    plan = row[0]
    # Pro starts right away; the webhook then pins the exact renewal date.
    db('UPDATE el_users SET cancelling = 0 WHERE email = %s', (session['email'],))
    grant(session['email'], now() + PERIOD_DAYS[plan] * 86400, plan=plan, sub_id=sub_id,
          status='active', provider='razorpay')
    db('UPDATE el_subs SET status = %s WHERE sub_id = %s', ('active', sub_id))
    try:
        s = rzp('GET', f'/subscriptions/{sub_id}')
        if s.get('current_end'):
            db('UPDATE el_subs SET current_end = %s WHERE sub_id = %s', (s['current_end'], sub_id))
    except ProviderError as e:
        print('[pro] fetch after verify failed:', e)
    return jsonify(me_payload())


@bp.post('/api/pro/dodo-return')
@login_required
def dodo_return():
    """Dodo sends people back with ?subscription_id=...; ask Dodo directly whether it's really active."""
    sub_id = (request.get_json(silent=True) or {}).get('subscription_id') or ''
    if not sub_id.replace('_', '').isalnum() or not dodo_ready():
        return jsonify(me_payload())
    try:
        data = dodo('GET', f'/subscriptions/{sub_id}')
    except ProviderError as e:
        print('[pro] dodo fetch failed:', e)
        return jsonify(me_payload())
    owner = ((data.get('metadata') or {}).get('email') or (data.get('customer') or {}).get('email') or '').lower()
    if owner == session['email']:
        apply_dodo_subscription(data)
    return jsonify(me_payload())


@bp.post('/api/pro/cancel')
@login_required
def cancel():
    user = get_user(session['email'])
    if not user['sub_id']:
        return jsonify({'error': 'no subscription to cancel'}), 404
    try:
        if user['sub_provider'] == 'dodo':
            dodo('PATCH', f"/subscriptions/{user['sub_id']}", json={'cancel_at_next_billing_date': True})
        else:
            try:
                rzp('POST', f"/subscriptions/{user['sub_id']}/cancel", json={'cancel_at_cycle_end': 1})
            except ProviderError:   # some mandates can't be scheduled; cancel now, the paid days are kept anyway
                rzp('POST', f"/subscriptions/{user['sub_id']}/cancel", json={'cancel_at_cycle_end': 0})
    except ProviderError as e:
        print('[pro] cancel failed:', e)
        return jsonify({'error': 'Could not cancel right now. Please try again.'}), 502
    db('UPDATE el_users SET cancelling = 1 WHERE email = %s', (user['email'],))
    return jsonify(me_payload())


@bp.post('/api/razorpay/webhook')
def razorpay_webhook():
    secret = env('RAZORPAY_WEBHOOK_SECRET')
    raw = request.get_data()
    sig = request.headers.get('X-Razorpay-Signature', '')
    if not secret or not sig or not hmac.compare_digest(hmac_hex(secret, raw), sig):
        return jsonify({'error': 'bad signature'}), 400
    event_id = request.headers.get('X-Razorpay-Event-Id')
    if seen_event(event_id):
        return jsonify({'ok': True, 'duplicate': True})
    handle_razorpay_event(json.loads(raw or b'{}'))
    remember_event(event_id)   # only after it was handled, so a failed attempt gets retried
    return jsonify({'ok': True})


def handle_razorpay_event(evt):
    sub = ((evt.get('payload') or {}).get('subscription') or {}).get('entity') or {}
    sub_id = sub.get('id')
    if not sub_id:
        return
    row = db('SELECT email, plan FROM el_subs WHERE sub_id = %s', (sub_id,), one=True)
    email = row[0] if row else ((sub.get('notes') or {}).get('email') or '').lower()
    plan = row[1] if row else (sub.get('notes') or {}).get('plan')
    if not email or not get_user(email):
        return
    status, end = sub.get('status'), sub.get('current_end')
    db('UPDATE el_subs SET status = %s, current_end = COALESCE(%s, current_end) WHERE sub_id = %s',
       (status, end, sub_id))
    event = evt.get('event', '')
    if event in ('subscription.activated', 'subscription.charged', 'subscription.resumed') and end:
        grant(email, int(end) + GRACE, plan=plan, sub_id=sub_id, status=status, provider='razorpay')
    else:
        u = get_user(email)
        if u and u['sub_id'] == sub_id:
            db('UPDATE el_users SET sub_status = %s WHERE email = %s', (status, email))
    if event in ('subscription.cancelled', 'subscription.completed'):
        db('UPDATE el_users SET cancelling = 1 WHERE email = %s AND sub_id = %s', (email, sub_id))


@bp.post('/api/dodo/webhook')
def dodo_webhook():
    raw = request.get_data()
    if not env('DODO_WEBHOOK_SECRET') or not dodo_signature_ok(raw, request.headers):
        return jsonify({'error': 'bad signature'}), 400
    event_id = 'dodo:' + request.headers.get('webhook-id', '')
    if seen_event(event_id):
        return jsonify({'ok': True, 'duplicate': True})
    evt = json.loads(raw or b'{}')
    data = evt.get('data') or {}
    if str(evt.get('type', '')).startswith('subscription.') and data.get('subscription_id'):
        if evt['type'] in ('subscription.cancelled', 'subscription.expired') and not data.get('status'):
            data['status'] = evt['type'].split('.')[1]
        apply_dodo_subscription(data)
    remember_event(event_id)
    return jsonify({'ok': True})


def init_pro(app, metered_endpoints=()):
    """Register the routes, set up the login cookie, and meter the look-making endpoints."""
    app.secret_key = env('SECRET_KEY') or app.secret_key or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),   # Render sets RENDER=true; https only there
        PERMANENT_SESSION_LIFETIME=timedelta(days=60),
    )
    app.register_blueprint(bp)
    for name in metered_endpoints:
        app.view_functions[name] = metered(app.view_functions[name])
    print('[pro] payments:', 'India on' if razorpay_ready() else 'India off', '·',
          'international on' if dodo_ready() else 'international off')
