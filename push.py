"""Home Screen app (PWA) and push notifications.

  /manifest.webmanifest, /sw.js, /icon-*.png   what a phone needs to "Add to Home Screen"
  /api/push/key                               the public VAPID key the browser subscribes with
  /api/push/subscribe, /api/push/unsubscribe  one row per phone or browser (el_push)
  /api/cron/tick                              hit once an hour by a free cron service (cron-job.org) with the
                                              header  X-Cron-Key: <CRON_SECRET>. It sends each phone its morning
                                              note at its own local time: today's fit, Sunday's Edit, or a reminder
                                              for a wedding function the person planned.

Web Push is encrypted end to end (RFC 8291, aes128gcm) and signed with a VAPID key (RFC 8292). The key pair is
made once and kept in the database (el_kv), so there's nothing to set up; VAPID_PRIVATE_KEY in Render overrides it.
On iPhone, notifications only work once E&L is added to the Home Screen (iOS 16.4 and later).
"""
import base64
import hashlib
import hmac
import json
import os
import random
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests as req
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Blueprint, Response, jsonify, request

from pro import db, env, identity, kv_get, kv_set, now

bp = Blueprint('push', __name__)


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def unb64u(s):
    s = str(s or '')
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


# ── VAPID keys ────────────────────────────────────────────────────────────────
_vapid = {}
_vapid_lock = threading.Lock()


def _load_private(text):
    text = text.strip()
    if text.startswith('-----BEGIN'):
        return serialization.load_pem_private_key(text.encode(), password=None)
    return ec.derive_private_key(int.from_bytes(unb64u(text), 'big'), ec.SECP256R1())


def vapid_key():
    with _vapid_lock:
        if 'key' in _vapid:
            return _vapid['key']
        text = env('VAPID_PRIVATE_KEY') or kv_get('vapid:private')
        if text:
            key = _load_private(text)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            raw = key.private_numbers().private_value.to_bytes(32, 'big')
            kv_set('vapid:private', b64u(raw))
            key = _load_private(kv_get('vapid:private'))       # read back, so two workers end up with one key
        _vapid['key'] = key
        return key


def public_key_b64():
    pub = vapid_key().public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(pub)


def vapid_header(endpoint):
    u = urlparse(endpoint)
    contact = env('VAPID_SUBJECT') or ('mailto:' + env('CONTACT_EMAIL') if env('CONTACT_EMAIL') else
                                       (env('PUBLIC_URL') or 'https://fashiongpt-f0vi.onrender.com'))
    header = b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    claims = b64u(json.dumps({'aud': f'{u.scheme}://{u.netloc}', 'exp': now() + 12 * 3600, 'sub': contact},
                             separators=(',', ':')).encode())
    signing = f'{header}.{claims}'.encode()
    r, s = decode_dss_signature(vapid_key().sign(signing, ec.ECDSA(hashes.SHA256())))
    jwt = f'{header}.{claims}.{b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}'
    return f'vapid t={jwt}, k={public_key_b64()}'


# ── Encryption (RFC 8291) ─────────────────────────────────────────────────────
def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b'\x01', hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh, auth, _salt=None, _key=None):
    ua_pub = unb64u(p256dh)
    auth = unb64u(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    as_priv = _key or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = as_priv.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, secret, b'WebPush: info\x00' + ua_pub + as_pub, 32)
    salt = _salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
    body = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    return salt + struct.pack('!I', 4096) + bytes([len(as_pub)]) + as_pub + body


PUSH_HOSTS = ('fcm.googleapis.com', 'android.googleapis.com', 'updates.push.services.mozilla.com',
              'push.services.mozilla.com', 'web.push.apple.com', '.push.apple.com', '.notify.windows.com')


def allowed_endpoint(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or '').lower()
    return u.scheme == 'https' and any(host == h or (h.startswith('.') and host.endswith(h)) for h in PUSH_HOSTS)


def send(row, message):
    """Send one notification. Returns True if the push service took it; forgets phones that unsubscribed."""
    endpoint, sub = row['endpoint'], row['sub']
    if not allowed_endpoint(endpoint):
        return False
    try:
        keys = sub.get('keys') or {}
        body = encrypt(json.dumps(message, ensure_ascii=False).encode(), keys['p256dh'], keys['auth'])
        r = req.post(endpoint, data=body, timeout=10, headers={
            'Authorization': vapid_header(endpoint), 'Content-Encoding': 'aes128gcm',
            'Content-Type': 'application/octet-stream', 'TTL': str(message.get('ttl', 6 * 3600)),
            'Urgency': 'normal', 'Topic': (message.get('tag') or 'el')[:32].replace(' ', '-')})
    except Exception as e:
        print('[push] send failed:', repr(e)[:200])
        db('UPDATE el_push SET fails = fails + 1 WHERE endpoint = %s', (endpoint,))
        return False
    if r.status_code in (404, 410):
        db('DELETE FROM el_push WHERE endpoint = %s', (endpoint,))
        return False
    if r.status_code >= 400:
        print('[push]', r.status_code, r.text[:200])
        db('UPDATE el_push SET fails = fails + 1 WHERE endpoint = %s', (endpoint,))
        db('DELETE FROM el_push WHERE endpoint = %s AND fails > 20', (endpoint,))
        return False
    try:
        from growth import track
        track('push_sent')
    except Exception:
        pass
    return True


PUSH_COLS = ['endpoint', 'who', 'sub', 'tz', 'hour', 'last_day', 'reminders', 'prefs']


def _rows(where='', params=()):
    out = []
    for r in db(f"SELECT {', '.join(PUSH_COLS)} FROM el_push {where}", params, many=True) or []:
        d = dict(zip(PUSH_COLS, r))
        for k, default in (('sub', {}), ('reminders', []), ('prefs', {})):
            try:
                d[k] = json.loads(d[k] or 'null') or default
            except ValueError:
                d[k] = default
        out.append(d)
    return out


def notify_who(who, message):
    """Push to every phone this person turned notifications on (in the background)."""
    rows = [r for r in _rows('WHERE who = %s', (who,)) if r['prefs'].get(message.get('kind', 'daily'), True)]
    if rows:
        threading.Thread(target=lambda: [send(r, message) for r in rows], daemon=True).start()
    return len(rows)


# ── Routes ────────────────────────────────────────────────────────────────────
@bp.get('/api/push/key')
def push_key():
    try:
        return jsonify({'key': public_key_b64()})
    except Exception as e:
        print('[push] no key:', e)
        return jsonify({'error': 'notifications are not available right now'}), 503


def _clean_reminders(items):
    out = []
    for r in (items or [])[:30]:
        if not isinstance(r, dict):
            continue
        try:
            d = datetime.strptime(str(r.get('date'))[:10], '%Y-%m-%d').date().isoformat()
        except ValueError:
            continue
        label = ' '.join(str(r.get('label') or '').split())[:40]
        if label:
            out.append({'date': d, 'label': label})
    return out


@bp.post('/api/push/subscribe')
def subscribe():
    body = request.get_json(silent=True) or {}
    sub = body.get('subscription') or {}
    endpoint = str(sub.get('endpoint') or '')
    keys = sub.get('keys') or {}
    if not allowed_endpoint(endpoint) or not keys.get('p256dh') or not keys.get('auth') or len(endpoint) > 1000:
        return jsonify({'error': 'That browser can’t get notifications.'}), 400
    try:
        tz = max(-720, min(840, int(body.get('tz', 330))))
        hour = max(5, min(11, int(body.get('hour', 8))))
    except (TypeError, ValueError):
        tz, hour = 330, 8
    prefs = {k: bool((body.get('prefs') or {}).get(k, True)) for k in ('daily', 'edit', 'votes', 'reminders')}
    clean_sub = {'endpoint': endpoint, 'keys': {'p256dh': str(keys['p256dh'])[:200], 'auth': str(keys['auth'])[:100]}}
    who = identity()[1]
    db('INSERT INTO el_push (endpoint, who, sub, tz, hour, reminders, prefs, created_at) '
       'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (endpoint) DO UPDATE SET who = excluded.who, sub = excluded.sub, '
       'tz = excluded.tz, hour = excluded.hour, reminders = excluded.reminders, prefs = excluded.prefs, fails = 0',
       (endpoint, who, json.dumps(clean_sub), tz, hour, json.dumps(_clean_reminders(body.get('reminders'))),
        json.dumps(prefs), now()))
    if body.get('welcome'):
        row = _rows('WHERE endpoint = %s', (endpoint,))
        if row:
            threading.Thread(target=send, args=(row[0], {
                'title': 'You’re on the list ✦', 'body': f'Ella will have your fit ready every morning at {hour}:00.',
                'url': '/?open=daily', 'tag': 'welcome'}), daemon=True).start()
    try:
        from growth import track
        track('push_on', who)
    except Exception:
        pass
    return jsonify({'ok': True, 'hour': hour})


@bp.post('/api/push/unsubscribe')
def unsubscribe():
    endpoint = str((request.get_json(silent=True) or {}).get('endpoint') or '')
    db('DELETE FROM el_push WHERE endpoint = %s', (endpoint,))
    return jsonify({'ok': True})


DAILY_LINES = [
    'Spotted: you, dressed by nine. Today’s fit is waiting ✦',
    'What are we wearing today? Ella already has ideas ✦',
    'Your closet called. Ella answered. Today’s fit is ready ✦',
    'Good morning. One tap and today’s outfit is sorted ✦',
    'Weather checked, closet checked. Your fit for today ✦',
]


def morning_message(row, local):
    """What this phone should get this morning, or None."""
    prefs = row['prefs']
    today_s = local.date().isoformat()
    tomorrow = (local.date() + timedelta(days=1)).isoformat()
    if prefs.get('reminders', True):
        for r in row['reminders']:
            if r['date'] == today_s:
                return {'title': f"It’s {r['label']} day ✦", 'body': 'Your look is planned. Tap to see it.',
                        'url': '/?open=wedding', 'tag': 'wedding'}
        for r in row['reminders']:
            if r['date'] == tomorrow:
                return {'title': f"Tomorrow: {r['label']}", 'body': 'Your look is planned. Check it tonight, not at 7 am.',
                        'url': '/?open=wedding', 'tag': 'wedding'}
    if local.weekday() == 6 and prefs.get('edit', True):
        return {'title': 'Ella’s Sunday Edit is out ✦', 'body': 'Five looks for the week ahead. Spotted: you, ahead of everyone.',
                'url': '/?open=edit', 'tag': 'edit'}
    if prefs.get('daily', True):
        return {'title': 'Today’s fit ✦', 'body': random.choice(DAILY_LINES), 'url': '/?open=daily', 'tag': 'daily'}
    return None


def run_tick():
    utc = datetime.now(timezone.utc)
    sent = 0
    for row in _rows():
        local = utc + timedelta(minutes=int(row['tz'] or 330))
        if local.hour != int(row['hour'] or 8) or row['last_day'] == local.date().isoformat():
            continue
        msg = morning_message(row, local)
        db('UPDATE el_push SET last_day = %s WHERE endpoint = %s', (local.date().isoformat(), row['endpoint']))
        if msg and send(row, msg):
            sent += 1
    # Sunday Edit: start making this week's editions early on Sunday, before anyone opens the app
    try:
        from daily import edit_sunday, start_edit
        ist = utc + timedelta(hours=5, minutes=30)
        if ist.weekday() == 6 and ist.hour >= 4:
            for g in ('women', 'men'):
                for r in ('in', 'intl'):
                    start_edit(edit_sunday(), g, r)
    except Exception as e:
        print('[push] edit warm-up skipped:', e)
    if random.random() < 0.05:
        from growth import cleanup
        cleanup()
    print(f'[push] tick: {sent} sent')
    return sent


_tick_lock = threading.Lock()


@bp.route('/api/cron/tick', methods=['GET', 'POST'])
def cron_tick():
    secret = env('CRON_SECRET')
    given = request.headers.get('X-Cron-Key') or request.args.get('key') or ''
    if not secret or not hmac.compare_digest(secret, given):
        return jsonify({'error': 'not found'}), 404
    if not _tick_lock.acquire(blocking=False):
        return jsonify({'ok': True, 'busy': True})

    def work():
        try:
            run_tick()
        except Exception as e:
            print('[push] tick failed:', repr(e))
        finally:
            _tick_lock.release()
    threading.Thread(target=work, daemon=True).start()
    return jsonify({'ok': True})


# ── Home Screen app ───────────────────────────────────────────────────────────
MANIFEST = {
    'name': 'E&L · Ella & Léon', 'short_name': 'E&L', 'id': '/', 'start_url': '/?source=homescreen', 'scope': '/',
    'display': 'standalone', 'background_color': '#F7F0E3', 'theme_color': '#1E1712',
    'description': 'Two AI stylists. Your fit for today, every look for every occasion.',
    'icons': [{'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
              {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
              {'src': '/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}],
    'shortcuts': [{'name': "Today's fit", 'url': '/?open=daily'}, {'name': 'Sunday Edit', 'url': '/?open=edit'},
                  {'name': 'Reels', 'url': '/?open=reels'}],
}

SW = """// E&L service worker: shows notifications and opens the right screen when one is tapped.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'E&L', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'E&L', {
    body: d.body || '', icon: '/icon-192.png', badge: '/badge-96.png', tag: d.tag || 'el', renotify: !!d.tag,
    data: { url: d.url || '/' }
  }));
});
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).origin === self.location.origin && url.indexOf('/l/') !== 0) {
        c.postMessage({ type: 'open', url: url });
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
"""


@bp.get('/manifest.webmanifest')
def manifest():
    return Response(json.dumps(MANIFEST, ensure_ascii=False), mimetype='application/manifest+json',
                    headers={'Cache-Control': 'public, max-age=3600'})


@bp.get('/sw.js')
def service_worker():
    return Response(SW, mimetype='application/javascript',
                    headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})


def _png(data):
    return Response(data, mimetype='image/png', headers={'Cache-Control': 'public, max-age=604800'})


@bp.get('/icon-192.png')
def icon192():
    import cards_art
    return _png(cards_art.icon(192))


@bp.get('/icon-512.png')
def icon512():
    import cards_art
    return _png(cards_art.icon(512))


@bp.get('/icon-maskable-512.png')
def icon_maskable():
    import cards_art
    return _png(cards_art.icon(512, maskable=True))


@bp.get('/apple-touch-icon.png')
def apple_icon():
    import cards_art
    return _png(cards_art.icon(180, maskable=True))


@bp.get('/badge-96.png')
def badge():
    import cards_art
    return _png(cards_art.badge(96))


@bp.get('/favicon.ico')
def favicon():
    import cards_art
    return _png(cards_art.icon(64, maskable=True))
