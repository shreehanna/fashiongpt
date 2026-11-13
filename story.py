"""Ella's Sunday Edit: one short story a week about the moment everyone's talking about, told through the outfit.

  "Spotted: Charles and Alexandra just welcomed their first baby, and her outfit ate." Then what she wore, who made
  it, and the pieces to shop.

Rules it keeps (the prompt asks for them, and the code checks what it can):
- Real news only: this week's reports (Serper news), and every story links to where it was reported.
- Public figures at public moments. Fashion commentary only: never bodies, rumours, allegations, children beyond
  the news itself, or private people.
- A designer or an exact piece is kept only if the reports name it (checked against their text): never made up.
- The owner publishes it (one tap in owner tools), unless they switched on publishing automatically on Sunday.
- The photo at the top is the report's own cover photo (the one a shared link previews with), shown from the
  publisher's site, credited and linked to the report. Never copied to our server; the owner can take it off.

One story a week for India and one for everywhere else (different stars trend). Ella can read it out: the audio is
made once per story and kept.

  /api/edit          the published story (this week's, or the last one)
  /api/edit/audio    Ella reading it
  /api/owner/story   owner: the drafts, write another, publish, automatic publishing on/off
"""
import base64
import html as htmllib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests as req
from flask import Blueprint, Response, jsonify, request

from pro import identity, kv_get, kv_set, owner_request

bp = Blueprint('story', __name__)
_ai = {}
IST = timezone(timedelta(hours=5, minutes=30))
REGIONS = ('in', 'intl')
_lock = threading.Lock()
_writing = set()
_audio_lock = threading.Lock()

NEWS_QUERIES = {
    'in': ['celebrity outfit this week', 'Bollywood star wore designer', 'celebrity look viral India', 'red carpet look India',
           'celebrity wedding outfit India', 'cricketer wife outfit', 'airport look celebrity'],
    'intl': ['celebrity outfit this week', 'celebrity wore designer', 'red carpet look', 'celebrity look goes viral',
             'royal outfit', 'celebrity baby announcement outfit', 'F1 paddock style'],
}
SKIP_TOPICS = re.compile(r'\b(died|death|dies|dead|funeral|passed away|killed|murder\w*|arrest\w*|charged|lawsuit|sued|'
                         r'court case|in court|affair|cheat\w*|divorc\w*|breakup|split up|rehab|overdose|assault\w*|abuse\w*|'
                         r'scandal\w*|leaked|cancer|hospitali[sz]ed|illness|surgery|election\w*|minister|politic\w*)\b', re.I)


def init(client, model):
    _ai['client'], _ai['model'] = client, model


def env(k):
    return (os.environ.get(k) or '').strip()


def now():
    return int(time.time())


def issue_date(t=None):
    """The Sunday this story is for: today if it's Sunday, else the coming Sunday (IST)."""
    d = datetime.fromtimestamp(t or time.time(), IST).date()
    return (d + timedelta(days=(6 - d.weekday()) % 7)).isoformat()


def _key(sunday, region):
    return f'story:{sunday}:{region}'


def _load(key):
    try:
        return json.loads(kv_get(key) or 'null')
    except Exception:
        return None


def published(region):
    key = kv_get(f'story:pub:{region}')
    return _load(key) if key else None


# ── This week's news ─────────────────────────────────────────────────────────
def _serper(kind, body):
    key = env('SERPER_API_KEY')
    if not key:
        return {}
    try:
        r = req.post(f'https://google.serper.dev/{kind}', json=body, timeout=12,
                     headers={'X-API-KEY': key, 'Content-Type': 'application/json'})
    except req.RequestException:
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


def news(region):
    """This week's reports: [{'title', 'snippet', 'url', 'source', 'date'}], newest stories first, no repeats."""
    from concurrent.futures import ThreadPoolExecutor
    gl = 'in' if region == 'in' else 'us'
    with ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(lambda q: _serper('news', {'q': q, 'gl': gl, 'tbs': 'qdr:w', 'num': 10}), NEWS_QUERIES[region]))
    out, seen = [], set()
    for res in results:
        for row in res.get('news') or []:
            url, title = str(row.get('link') or ''), ' '.join(str(row.get('title') or '').split())
            if not url.startswith('https://') or not title or url in seen:
                continue
            if SKIP_TOPICS.search(f"{title} {row.get('snippet') or ''}"):
                continue                    # tragedies, crimes, rumours, politics: never a fashion story
            seen.add(url)
            out.append({'title': title[:200], 'snippet': ' '.join(str(row.get('snippet') or '').split())[:300],
                        'url': url[:400], 'source': str(row.get('source') or '')[:60], 'date': str(row.get('date') or '')[:40]})
    return out[:60]


def _page(url):
    """The text of a report (read through Serper when the site blocks servers)."""
    import muses
    lines = muses._direct(url) or muses._scraped(url) or []
    keep = [l for l in lines if len(l) >= 40]
    return '\n'.join(keep)[:7000]


_META = re.compile(r'<meta\b[^>]*>', re.I)
_META_NAME = re.compile(r'(?:property|name)\s*=\s*["\']\s*(og:image(?::secure_url|:url)?|twitter:image(?::src)?)\s*["\']', re.I)
_META_CONTENT = re.compile(r'content\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)
_NOT_A_PHOTO = re.compile(r'(^|[/_.\-])(logos?|placeholder|default|fallback|favicon|sprite|icons?|blank)([/_.\-?]|$)|\.svg(\?|$)', re.I)


def _meta_image(page):
    """The page's cover photo as it tells link previews: og:image, else twitter:image."""
    found = {}
    for tag in _META.findall(page):
        n, c = _META_NAME.search(tag), _META_CONTENT.search(tag)
        if n and c:
            found.setdefault(n.group(1).lower().split(':')[0], (c.group(1) or c.group(2) or '').strip())
    return found.get('og') or found.get('twitter') or ''


def _clean_image(src, page_url):
    """A cover photo address we're willing to show: https, a real photo (not the site's logo), nothing odd in it."""
    from urllib.parse import urljoin
    src = urljoin(page_url, htmllib.unescape(src or '').strip())
    if not src.startswith('https://') or len(src) > 700 or re.search(r'[\s"\'<>\\]', src):
        return None
    return None if _NOT_A_PHOTO.search(src.split('//', 1)[1].split('/', 1)[-1]) else src


def _photo(sources):
    """The report's own cover photo for the top of the story: shown from the publisher's site (never copied to ours),
    credited, and linked to the report, the way a shared link shows its preview. None if no report has one."""
    import muses
    for s in sources:
        url = s['url']
        if not muses._public_https(url):
            continue
        try:
            r = req.get(url, timeout=6, headers=muses._BROWSER, stream=True)
            if r.status_code != 200 or 'html' not in (r.headers.get('Content-Type') or ''):
                continue
            head = r.raw.read(400_000, decode_content=True).decode(r.encoding or 'utf-8', 'replace')
        except Exception:
            continue
        img = _clean_image(_meta_image(head), url)
        if img:
            from urllib.parse import urlparse
            return {'url': img, 'credit': s.get('source') or re.sub(r'^www\.', '', urlparse(url).hostname or ''), 'link': url}
    return None


def _plain(text):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', htmllib.unescape(str(text or '')).lower()).split())


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


def _ask(prompt, max_tokens):
    msg = _ai['client'].messages.create(model=_ai['model'], max_tokens=max_tokens,
                                        messages=[{'role': 'user', 'content': prompt}])
    return _json(msg.content[0].text)


VOICE = ('Your voice: a Gossip Girl-style society columnist. Knowing, playful, a little wicked, warm underneath, never '
         'mean. You can open with "Spotted:". Sign off "XOXO, Ella". Never name any real TV show.')


def pick(rows, region, avoid=()):
    """The 3 best stories this week, best first: indices into rows."""
    listing = '\n'.join(f"[{i + 1}] {r['title']} ({r['source']}, {r['date']}): {r['snippet']}" for i, r in enumerate(rows))
    avoid_line = ('\nDon\'t pick these again (already used): ' + '; '.join(avoid[:8])) if avoid else ''
    out = _ask(
        f"You are Ella from E&L, choosing this week's Sunday Edit: one trending moment told through what someone wore.\n"
        f"Readers: {'India' if region == 'in' else 'the US, UK and UAE'}.\n\n"
        "Pick the 3 best stories from this week's news below, best first. The best ones:\n"
        "- are a moment people are actually talking about this week (a baby, an engagement, a wedding, a premiere, a "
        "big win, an award, a viral appearance, fashion week), with a public figure at a public moment;\n"
        "- have an outfit that genuinely ate, and the coverage says what it was (the designer or the pieces);\n"
        "- are happy news. Never tragedies, illness, crimes, court cases, breakups, rumours, politics, or anyone under 18 "
        "as the subject." + avoid_line + "\n\n"
        'Return ONLY valid JSON: {"picks": [3, 7, 12]}. If nothing fits, {"picks": []}.\n\nThis week\'s news:\n' + listing, 300)
    picks = []
    for p in out.get('picks') or []:
        try:
            i = int(p) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(rows) and i not in picks:
            picks.append(i)
    return picks[:3]


def write(row, region, related):
    """The story itself, from the chosen report (and others about the same moment). None if the reports don't say
    what the outfit was."""
    reports = [row] + [r for r in related if r['url'] != row['url']][:2]
    texts = []
    for i, r in enumerate(reports):
        body = _page(r['url']) or r['snippet']
        texts.append(f"[{i + 1}] {r['title']} ({r['source']}, {r['date']})\n{body}")
    source = _plain(' '.join(texts))
    out = _ask(
        "You are Ella from E&L, writing \"Ella's Sunday Edit\": one short story a week about the moment everyone's "
        f"talking about, told through the outfit. {VOICE}\n\n"
        "Write it ONLY from these reports:\n\n" + '\n\n'.join(texts) + "\n\n"
        "Rules:\n"
        "- Every fact (who, what happened, when, what they wore, who made it) comes from the reports. Never invent a "
        "designer, a quote or a detail. Where the reports say \"reportedly\", say it too.\n"
        "- The outfit is the star: what they wore, why it works, who made it. Be specific.\n"
        "- Fashion commentary only: nothing about bodies, weight, age or looks beyond the clothes; no rumours or "
        "allegations; about children, only the happy news itself; never name or describe private people.\n"
        "- People skim, so keep it short: a headline of at most 9 words, a one-sentence dek, then 3 short paragraphs of "
        "at most 35 words each. The last paragraph ends with \"XOXO, Ella\".\n"
        "- \"deets\": every piece of the outfit the reports describe. \"name\" is its exact product name if the reports "
        "give one, else plain words (\"ivory silk column gown\"); \"brand\" only as the reports name it, else \"\".\n"
        "- If the reports don't say what the outfit was, return {\"skip\": true}.\n\n"
        'Return ONLY valid JSON: {"headline": "", "dek": "", "person": "who wore it", "moment": "what happened, in 3-8 '
        'words", "body": ["", "", ""], "deets": [{"category": "Dress", "name": "", "brand": "", "description": "colour, '
        'fabric, cut", "exact": false}], "skip": false}', 1600)
    if not out or out.get('skip') or not out.get('headline') or not out.get('deets'):
        return None
    deets = []
    for d in (out.get('deets') or [])[:8]:
        if not isinstance(d, dict) or not (d.get('name') or d.get('description')):
            continue
        brand = str(d.get('brand') or '').strip()[:60]
        name = str(d.get('name') or '').strip().strip('"“”')[:90]
        exact = bool(d.get('exact')) and bool(name) and _plain(name) in source
        if brand and _plain(brand) not in source:
            brand, exact = '', False        # a designer the reports never named: dropped, never guessed
        deets.append({'category': str(d.get('category') or 'Piece')[:40], 'name': name or str(d.get('description'))[:90],
                      'brand': brand, 'description': str(d.get('description') or '')[:160], 'exact': exact})
    if not deets:
        return None
    body = [str(p).strip()[:400] for p in (out.get('body') or []) if str(p).strip()][:4]
    return {'headline': str(out['headline']).strip()[:90], 'dek': str(out.get('dek') or '').strip()[:200],
            'person': str(out.get('person') or '').strip()[:80], 'moment': str(out.get('moment') or '').strip()[:80],
            'body': body, 'deets': deets,
            'sources': [{'title': r['title'], 'url': r['url'], 'source': r['source'], 'date': r['date']} for r in reports]}


def make_draft(sunday, region):
    """Find this week's story and write it. Saved as a draft (or published at once if automatic publishing is on and
    it's already Sunday morning)."""
    key = _key(sunday, region)
    try:
        rows = news(region)
        if not rows:
            raise ValueError('no news came back (is SERPER_API_KEY set?)')
        old = _load(key) or {}
        avoid = [s for s in json.loads(kv_get(f'story:used:{region}') or '[]')][-8:]
        if old.get('moment'):
            avoid.append(f"{old.get('person')}: {old.get('moment')}")
        story = None
        for i in pick(rows, region, avoid):
            words = set(_plain(rows[i]['title']).split()) - {'the', 'a', 'in', 'and', 'of', 'her', 'his', 'at', 'to', 'for'}
            related = [r for r in rows if len(words & set(_plain(r['title']).split())) >= 3]
            story = write(rows[i], region, related)
            if story:
                break
        if not story:
            raise ValueError('no story this week had an outfit the reports describe')
        story['photo'] = _photo(story['sources'])
        story.update(sunday=sunday, region=region, made_at=now(), status='draft')
        kv_set(key, json.dumps(story, ensure_ascii=False))
        kv_set(key + ':failed', '')
        print(f"[story] draft ready {key}: {story['headline']}")
        _tell_owner(story)
        if auto_publish() and _sunday_morning():
            publish(sunday, region)
    except Exception as e:
        print(f'[story] {key} failed: {e!r}')
        kv_set(key + ':failed', json.dumps({'at': now(), 'why': str(e)[:200]}))
    finally:
        with _lock:
            _writing.discard(key)


def start(sunday, region, force=False):
    key = _key(sunday, region)
    with _lock:
        if key in _writing:
            return True
        if not force:
            if _load(key):
                return False
            try:
                failed = json.loads(kv_get(key + ':failed') or 'null')
            except ValueError:
                failed = None
            if failed and now() - int(failed.get('at', 0)) < 1800:     # wait 30 minutes before trying again
                return False
        _writing.add(key)
    threading.Thread(target=make_draft, args=(sunday, region), daemon=True).start()
    return True


def publish(sunday, region):
    key = _key(sunday, region)
    story = _load(key)
    if not story:
        return False
    story.update(status='published', published_at=now())
    kv_set(key, json.dumps(story, ensure_ascii=False))
    # its own copy: writing a different draft afterwards never changes what readers see until that one is published
    kv_set(key + ':live', json.dumps(story, ensure_ascii=False))
    kv_set(f'story:pub:{region}', key + ':live')
    used = json.loads(kv_get(f'story:used:{region}') or '[]')
    kv_set(f'story:used:{region}', json.dumps((used + [f"{story.get('person')}: {story.get('moment')}"])[-12:]))
    return True


def auto_publish():
    return kv_get('story:auto') == '1'


def _sunday_morning(t=None):
    d = datetime.fromtimestamp(t or time.time(), IST)
    return d.weekday() == 6 and d.hour >= 9


def _tell_owner(story):
    """A notification to the owner's phone: the draft is ready to read."""
    who = kv_get('owner:who')
    if not who or auto_publish():
        return
    try:
        from push import notify_who
        notify_who(who, {'title': 'Sunday story ready to review ✦', 'body': story['headline'], 'url': '/?open=owner',
                         'tag': 'story', 'kind': 'owner'})
    except Exception as e:
        print('[story] could not tell the owner:', e)


def tick():
    """Hourly (from /api/cron/tick): drafts from Saturday noon, and on Sunday morning automatic publishing if it's on."""
    d = datetime.now(IST)
    if not (d.weekday() == 5 and d.hour >= 12) and d.weekday() != 6:
        return
    sunday = issue_date()
    for region in REGIONS:
        story = _load(_key(sunday, region))
        if not story:
            start(sunday, region)
        elif story.get('status') == 'draft' and auto_publish() and _sunday_morning():
            publish(sunday, region)


# ── For everyone ─────────────────────────────────────────────────────────────
def _region():
    return 'in' if request.args.get('region') == 'in' else 'intl'


@bp.get('/api/edit')
def get_story():
    region = _region()
    story = published(region)
    return jsonify({'story': story, 'current': bool(story and story.get('sunday') == issue_date()), 'sunday': issue_date()})


@bp.get('/api/edit/audio')
def story_audio():
    """Ella reading this week's story. Made once per story, then kept."""
    story = published(_region())
    if not story:
        return jsonify({'error': 'no story yet'}), 404
    key = f"story:audio:{story['sunday']}:{story['region']}:{story.get('published_at', 0)}"
    with _audio_lock:
        saved = kv_get(key)
        if not saved:
            audio = _speak(story)
            if not audio:
                return jsonify({'error': 'Ella’s voice isn’t set up'}), 503
            saved = base64.b64encode(audio).decode()
            kv_set(key, saved)
    return Response(base64.b64decode(saved), mimetype='audio/mpeg', headers={'Cache-Control': 'public, max-age=3600'})


def _speak(story):
    key = env('ELEVENLABS_API_KEY')
    if not key:
        return None
    from app import TTS_MODEL, VOICES
    voice = VOICES['ella']
    text = f"{story['headline']}. " + ' '.join(story['body'])
    try:
        r = req.post(f"https://api.elevenlabs.io/v1/text-to-speech/{env(voice['env']) or voice['default']}", timeout=60,
                     headers={'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'},
                     json={'text': text[:1500], 'model_id': TTS_MODEL, 'voice_settings': voice['settings']})
    except req.RequestException as e:
        print('[story] voice failed:', e)
        return None
    return r.content if r.status_code == 200 else None


# ── Owner tools ──────────────────────────────────────────────────────────────
def _owner_view():
    sunday = issue_date()
    regions = {}
    for region in REGIONS:
        key = _key(sunday, region)
        try:
            failed = json.loads(kv_get(key + ':failed') or 'null')
        except ValueError:
            failed = None
        pub = published(region)
        regions[region] = {'draft': _load(key), 'writing': key in _writing, 'failed': (failed or {}).get('why'),
                           'live': {'headline': pub['headline'], 'sunday': pub['sunday'], 'photo': bool(pub.get('photo'))} if pub else None}
    return {'sunday': sunday, 'auto': auto_publish(), 'regions': regions}


@bp.get('/api/owner/story')
def owner_story():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    try:
        kv_set('owner:who', identity()[1])        # so a draft that's ready can ping the owner's phone
    except Exception:
        pass
    return jsonify(_owner_view())


@bp.post('/api/owner/story')
def owner_story_act():
    if not owner_request():
        return jsonify({'error': 'not found'}), 404
    body = request.get_json(silent=True) or {}
    sunday, region = issue_date(), ('in' if body.get('region') == 'in' else 'intl')
    if 'auto' in body:
        kv_set('story:auto', '1' if body.get('auto') else '')
    action = body.get('action')
    if action == 'write':
        start(sunday, region, force=True)
    elif action == 'publish' and not publish(sunday, region):
        return jsonify(dict(_owner_view(), error='No draft to publish yet.')), 400
    elif action == 'nophoto':
        # if a photographer or the publisher asks: the photo comes off (this week's draft, or the live story)
        draft_key, live_key = _key(sunday, region), kv_get(f'story:pub:{region}')
        draft = _load(draft_key)
        keys = [live_key] if body.get('which') == 'live' else [draft_key]
        if body.get('which') != 'live' and draft and draft.get('status') == 'published' and live_key == draft_key + ':live':
            keys.append(live_key)                  # the draft that's live: off both
        for key in keys:
            story = _load(key) if key else None
            if story and story.get('photo'):
                story['photo'] = None
                kv_set(key, json.dumps(story, ensure_ascii=False))
    return jsonify(_owner_view())
