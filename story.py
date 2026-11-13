"""Ella's Sunday Edit: one short story a week about the moment everyone's talking about, told through the outfit.

  "Spotted: Charles and Alexandra just welcomed their first baby, and her outfit ate." Then what she wore, who made
  it, and the pieces to shop.

Two kinds of story: a moment (a public figure at something everyone's talking about; if those reports don't say what
they wore, Ella reads this week's coverage of their looks, or writes about the style they're in right now and says so)
or a trend (a look several stars are wearing this week, like socks with heels).

Rules it keeps (the prompt asks for them, and the code checks what it can):
- Real news only: this week's reports (Serper news), and every story links to where it was reported.
- Public figures at public moments, happy news only (no deaths, crimes, cheating, breakups, politics). A new romance
  only as the reports tell it. Fashion commentary only: never bodies, allegations, children beyond the news itself;
  private people (a non-famous partner) are never named.
- A designer or an exact piece is kept only if the reports name it (checked against their text): never made up.
- The owner publishes it (one tap in owner tools), unless they switched on publishing automatically on Sunday.
- The photos are the reports' own photos of the star (the cover photo, and pictures whose caption names them), shown
  from the publisher's site, each credited and linked to its report. Never copied to our server; the owner can take
  any of them off.

One story a week for everyone, from the world's news and India's. Ella can read it out: the audio is made once per
story and kept.

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
REGIONS = ('all',)                         # one story for everyone, India and everywhere else
_lock = threading.Lock()
_writing = set()
_audio_lock = threading.Lock()

# (search, Google country): the world's news and India's, so a Bollywood moment can be the story of the week too
NEWS_QUERIES = [('celebrity outfit this week', 'us'), ('celebrity wore designer', 'us'), ('red carpet look', 'us'),
                ('celebrity look goes viral', 'us'), ('celebrity style trend', 'us'), ('celebrities spotted this week', 'us'),
                ('royal outfit', 'us'), ('F1 paddock style', 'us'),
                ('Bollywood star wore designer', 'in'), ('celebrity look viral India', 'in'), ('Bollywood fashion trend', 'in'),
                ('Bollywood celebrity spotted', 'in'), ('celebrity wedding outfit India', 'in'), ('airport look celebrity', 'in')]
# Checked against the headline only: a happy story whose summary mentions an old breakup or a charity night for a
# cause is still a happy story, and fashion slang ("she killed it", "a scandalous sheer dress") isn't news of a death
# or a scandal. Ella's own rules catch the rest.
SKIP_TOPICS = re.compile(r'\b(died|death|dies at|funeral|passed away|murder\w*|arrest\w*|charged with|lawsuit|sued|'
                         r'court|affair|cheat\w*|divorc\w*|rehab|overdose|assault\w*|abuse\w*|scandal|scandals|leaked|'
                         r'cancer|hospitali[sz]ed|illness|surgery|election\w*|minister|politic\w*)\b', re.I)


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


def _rows(res, seen, stats=None, gl='us'):
    out = []
    for row in res.get('news') or []:
        url, title = str(row.get('link') or ''), ' '.join(str(row.get('title') or '').split())
        if not url.startswith('https://') or not title or url in seen:
            continue
        if SKIP_TOPICS.search(title):
            if stats is not None:
                stats.setdefault('skipped', set()).add(url)
            continue                        # tragedies, crimes, cheating, politics: never a fashion story
        seen.add(url)
        out.append({'title': title[:200], 'snippet': ' '.join(str(row.get('snippet') or '').split())[:300],
                    'url': url[:400], 'source': str(row.get('source') or '')[:60], 'date': str(row.get('date') or '')[:40],
                    'gl': gl})
    return out


def news(region='all', stats=None):
    """This week's reports: [{'title', 'snippet', 'url', 'source', 'date', 'gl'}], no repeats."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda qg: _serper('news', {'q': qg[0], 'gl': qg[1], 'tbs': 'qdr:w', 'num': 10}), NEWS_QUERIES))
    out, seen = [], set()
    for res, (_, gl) in zip(results, NEWS_QUERIES):
        out += _rows(res, seen, stats, gl)
    return out[:90]


def fashion_coverage(who, kind, gl, have):
    """This week's reports on what they've been wearing (or on the trend): the trending story is often about the moment,
    and the outfit is covered somewhere else."""
    words = [w for w in _plain(who).split() if len(w) > 2]
    if not words:
        return []
    q = f'{who} outfit' if kind == 'moment' else f'{who} trend celebrities'
    res = _serper('news', {'q': q, 'gl': gl, 'tbs': 'qdr:w', 'num': 10})
    key = words[-1] if kind == 'moment' else None      # a surname for a person; any of the words for a trend
    out = []
    for r in _rows(res, set(have), gl=gl):
        text = _plain(f"{r['title']} {r['snippet']}")
        if (key and key in text.split()) or (not key and any(w in text.split() for w in words)):
            out.append(r)
    return out[:2]


def _page(url):
    """What a report says: the start of it (what happened), then every line about what someone wore. Read through
    Serper when the site blocks servers. '' if it can't be read."""
    import muses
    lines = muses._direct(url) or muses._scraped(url) or []
    head = '\n'.join(l for l in lines if len(l) >= 40)[:3500]
    wore, seen = [], set(head.split('\n'))
    for l in lines:
        if len(l) >= 15 and l not in seen and muses._WORE.search(l):
            seen.add(l)
            wore.append(l)
    return (head + '\n' + '\n'.join(wore)).strip()[:7000]


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


_IMG_TAG = re.compile(r'<(?:img|source)\b[^>]*>', re.I)
_NOT_ARTICLE_PHOTO = re.compile(r'(^|[/_.\-])(avatars?|authors?|profile|headshots?|byline|ads?|advert\w*|pixel|tracking|badge|'
                                r'emoji|button|banner|newsletter|promo|spinner|loading|lazy)([/_.\-?]|$)|\.gif(\?|$)', re.I)


def _attr(tag, name):
    m = re.search(r'\b' + name + r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag, re.I)
    return htmllib.unescape((m.group(1) if m.group(1) is not None else m.group(2)) if m else '').strip()


def _biggest(srcset):
    """The largest picture in a srcset ("a.jpg 320w, b.jpg 1200w")."""
    best, best_w = '', -1
    for part in re.split(r',\s+(?=https?:|/)', srcset or ''):
        bits = part.strip().split()
        if not bits:
            continue
        w = int(re.sub(r'\D', '', bits[1]) or 0) if len(bits) > 1 and bits[1].lower().endswith('w') else 0
        if w > best_w:
            best, best_w = bits[0], w
    return best


def _article_images(page, page_url, names):
    """The photos in a report that show the star: the cover photo, then every picture whose caption or file name has
    their name (so "more stories" pictures of other people never come along)."""
    out = []
    cover = _clean_image(_meta_image(page), page_url)
    if cover:
        out.append(cover)
    for tag in _IMG_TAG.findall(page):
        src = (_attr(tag, 'data-src') or _attr(tag, 'data-lazy-src') or _attr(tag, 'data-original')
               or _biggest(_attr(tag, 'data-srcset') or _attr(tag, 'srcset')) or _attr(tag, 'src'))
        if not src or src.startswith('data:'):
            continue
        try:
            w, h = int(_attr(tag, 'width') or 0), int(_attr(tag, 'height') or 0)
        except ValueError:
            w = h = 0
        if (w and w < 300) or (h and h < 300):
            continue                        # icons and thumbnails
        img = _clean_image(src, page_url)
        if not img or _NOT_ARTICLE_PHOTO.search(img.split('//', 1)[1].split('/', 1)[-1]):
            continue
        about = _plain(_attr(tag, 'alt') + ' ' + _attr(tag, 'title') + ' ' + img.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' '))
        if names and any(n in about.split() for n in names):
            out.append(img)
    return out


def _same_picture(a, b):
    k = lambda u: re.sub(r'(-\d+x\d+)?(\.\w+)?([?#].*)?$', '', u.lower())
    return k(a) == k(b)


def _photos(sources, person=''):
    """The reports' own photos of the star (at most 6): shown from the publisher's site (never copied to ours),
    each credited and linked to its report."""
    import muses
    from urllib.parse import urlparse
    names = [w for w in _plain(person).split() if len(w) >= 3 and w not in ('and', 'the', 'her', 'his')]
    out = []
    for s in sources:
        url = s['url']
        if len(out) >= 6 or not muses._public_https(url):
            continue
        try:
            r = req.get(url, timeout=6, headers=muses._BROWSER, stream=True)
            if r.status_code != 200 or 'html' not in (r.headers.get('Content-Type') or ''):
                continue
            page = r.raw.read(1_500_000, decode_content=True).decode(r.encoding or 'utf-8', 'replace')
        except Exception:
            continue
        credit = s.get('source') or re.sub(r'^www\.', '', urlparse(url).hostname or '')
        n = 0
        for img in _article_images(page, url, names):
            if n >= 4 or len(out) >= 6:
                break
            if not any(_same_picture(img, o['url']) for o in out):
                out.append({'url': img, 'credit': credit, 'link': url})
                n += 1
    return out


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
    """The 5 best stories this week, best first: [(index into rows, 'moment' or 'trend', who or what)]."""
    listing = '\n'.join(f"[{i + 1}] {r['title']} ({r['source']}, {r['date']}): {r['snippet']}" for i, r in enumerate(rows))
    avoid_line = ('\nDon\'t pick these again (already used): ' + '; '.join(avoid[:8])) if avoid else ''
    out = _ask(
        f"You are Ella from E&L, choosing this week's Sunday Edit: one trending story told through what people wore.\n"
        "Readers: everyone, in India, the US, the UK and the UAE, so pick what people everywhere are talking about (a "
        "Bollywood moment counts when it's big).\n\n"
        "Pick the 5 best stories from this week's news below, best first. Two kinds work:\n"
        "- a MOMENT: a public figure at a moment people are actually talking about this week (a baby, a new romance, an "
        "engagement, a wedding, a premiere, a big win, an award, a viral appearance, fashion week). The headline doesn't "
        "need to mention clothes: Ella finds out what they wore.\n"
        "- a TREND: a look that several stars are wearing this week (like socks with heels), named in the coverage.\n"
        "Happy news only. Never tragedies, illness, crimes, court cases, cheating, a breakup or divorce as the story, "
        "politics, or anyone under 18 as the subject. A new romance is fine as the reports tell it." + avoid_line + "\n\n"
        'Return ONLY valid JSON: {"picks": [{"n": 3, "kind": "moment", "who": "the public figure\'s full name"}, '
        '{"n": 7, "kind": "trend", "who": "the trend in 2-4 words"}]}. If nothing fits, {"picks": []}.\n\n'
        "This week's news:\n" + listing, 500)
    picks, used = [], set()
    for p in out.get('picks') or []:
        n, kind, who = (p.get('n'), p.get('kind'), p.get('who')) if isinstance(p, dict) else (p, 'moment', '')
        try:
            i = int(n) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(rows) and i not in used:
            used.add(i)
            picks.append((i, 'trend' if kind == 'trend' else 'moment', ' '.join(str(who or '').split())[:60]))
    return picks[:5]


def write(row, region, related, kind='moment', who='', notes=None):
    """The story itself, from the chosen report, others about the same moment, and this week's coverage of what they
    wore. None if no report says what anyone wore (and notes say why)."""
    notes = [] if notes is None else notes
    reports = [row] + [r for r in related if r['url'] != row['url']][:1]
    if who:
        reports += fashion_coverage(who, kind, row.get('gl') or 'us', [r['url'] for r in reports])
    texts, unread = [], []
    for i, r in enumerate(reports):
        body = _page(r['url'])
        if not body:
            unread.append(r['source'] or 'a report')
        texts.append(f"[{i + 1}] {r['title']} ({r['source']}, {r['date']})\n{body or r['snippet']}")
    source = _plain(' '.join(texts))
    star = ("- The outfit is the star: what they wore, why it works, who made it. Be specific. If the reports describe "
            "what they wore at this moment, that's the look. If they don't, but they describe what this person has been "
            "wearing this week or the style they're in right now (an era, signature pieces), write about that and say "
            "so honestly (like \"No word on the game-night fit, but her cowgirl era? Thriving.\"). Never invent an outfit.\n"
            if kind == 'moment' else
            "- The trend is the star: what the look is, which stars wore it and how (only as the reports say), and how "
            "to wear it. \"person\" is the stars, like \"Dua Lipa and Lily James\"; \"moment\" is the trend.\n")
    out = _ask(
        "You are Ella from E&L, writing \"Ella's Sunday Edit\": one short story a week about what everyone's talking "
        f"about, told through the outfit. {VOICE}\n\n"
        "Write it ONLY from these reports:\n\n" + '\n\n'.join(texts) + "\n\n"
        "Rules:\n"
        "- Every fact (who, what happened, when, what they wore, who made it) comes from the reports. Never invent a "
        "designer, a quote or a detail. Where the reports say \"reportedly\" or \"seemingly\", say it too.\n" + star +
        "- Fashion commentary only: nothing about bodies, weight, age or looks beyond the clothes and hair; no allegations "
        "or anything that could embarrass someone; a new romance only as the reports tell it; about children, only the "
        "happy news itself; never name or describe private people (anyone not famous in their own right, like a "
        "non-famous partner or friend): call them only by their part in the story (\"her new cowboy\").\n"
        "- People skim, so keep it short: a headline of at most 9 words, a one-sentence dek, then 3 short paragraphs of "
        "at most 35 words each. The last paragraph ends with \"XOXO, Ella\".\n"
        "- \"deets\": every piece the reports describe (the outfit, or the trend's pieces). \"name\" is its exact product "
        "name if the reports give one, else plain words (\"ivory silk column gown\"); \"brand\" only as the reports name "
        "it, else \"\".\n"
        "- If no report says what anyone wore (not even their style right now, or the trend's pieces), return "
        "{\"skip\": true, \"why\": \"one short line\"}.\n\n"
        'Return ONLY valid JSON: {"headline": "", "dek": "", "person": "who wore it", "moment": "what happened, in 3-8 '
        'words", "body": ["", "", ""], "deets": [{"category": "Dress", "name": "", "brand": "", "description": "colour, '
        'fabric, cut", "exact": false}], "skip": false}', 1600)
    read = f"read {len(reports) - len(unread)} of {len(reports)} reports" + (f" ({', '.join(unread)} blocked)" if unread else '')
    if not out or out.get('skip') or not out.get('headline') or not out.get('deets'):
        notes.append(f"{who or row['title'][:60]} ({kind}): {read}; Ella: {str((out or {}).get('why') or 'no outfit described')[:120]}")
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
        notes.append(f"{who or row['title'][:60]} ({kind}): {read}; Ella: no pieces described")
        return None
    body = [str(p).strip()[:400] for p in (out.get('body') or []) if str(p).strip()][:4]
    return {'headline': str(out['headline']).strip()[:90], 'dek': str(out.get('dek') or '').strip()[:200],
            'person': str(out.get('person') or '').strip()[:80], 'moment': str(out.get('moment') or '').strip()[:80],
            'kind': kind, 'body': body, 'deets': deets,
            'sources': [{'title': r['title'], 'url': r['url'], 'source': r['source'], 'date': r['date']} for r in reports]}


def make_draft(sunday, region):
    """Find this week's story and write it. Saved as a draft (or published at once if automatic publishing is on and
    it's already Sunday morning)."""
    key = _key(sunday, region)
    steps = []                              # what happened, for owner tools when there's no story
    try:
        stats = {}
        rows = news(region, stats)
        steps.append(f"{len(rows)} stories in this week's news" + (f" ({len(stats['skipped'])} left out: deaths, crimes, cheating, politics…)"
                                                                     if stats.get('skipped') else ''))
        if not rows:
            raise ValueError('no news came back (is SERPER_API_KEY set?)')
        old = _load(key) or {}
        avoid = [s for s in json.loads(kv_get(f'story:used:{region}') or '[]')][-8:]
        if old.get('moment'):
            avoid.append(f"{old.get('person')}: {old.get('moment')}")
        story, picks = None, pick(rows, region, avoid)
        steps.append(f"Ella's picks: {'; '.join(dict.fromkeys(who or rows[i]['title'][:50] for i, _, who in picks)) or 'none of them fit'}")
        tried = set()
        for i, kind, who in picks:
            if who and (kind, who.lower()) in tried:
                continue                    # two reports about the same moment: one try is enough
            tried.add((kind, who.lower()))
            words = set(_plain(rows[i]['title']).split()) - {'the', 'a', 'in', 'and', 'of', 'her', 'his', 'at', 'to', 'for'}
            related = [r for r in rows if len(words & set(_plain(r['title']).split())) >= 3]
            story = write(rows[i], region, related, kind, who, steps)
            if story:
                break
        if not story:
            raise ValueError('none of this week\'s stories said what anyone wore' if picks else 'nothing in this week\'s news fit')
        story['photos'] = _photos(story['sources'], story.get('person') or '')
        story.update(sunday=sunday, region=region, made_at=now(), status='draft')
        kv_set(key, json.dumps(story, ensure_ascii=False))
        kv_set(key + ':failed', '')
        print(f"[story] draft ready {key}: {story['headline']}")
        _tell_owner(story)
        if auto_publish() and _sunday_morning():
            publish(sunday, region)
    except Exception as e:
        print(f'[story] {key} failed: {e!r}', steps)
        kv_set(key + ':failed', json.dumps({'at': now(), 'why': str(e)[:200], 'steps': [s[:300] for s in steps[:8]]}))
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
    return 'all'                            # one story for everyone


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
                           'steps': (failed or {}).get('steps') or [],
                           'live': {'headline': pub['headline'], 'sunday': pub['sunday'],
                                    'photo': bool(pub.get('photos') or pub.get('photo'))} if pub else None}
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
    sunday, region = issue_date(), 'all'
    if 'auto' in body:
        kv_set('story:auto', '1' if body.get('auto') else '')
    action = body.get('action')
    if action == 'write':
        start(sunday, region, force=True)
    elif action == 'publish' and not publish(sunday, region):
        return jsonify(dict(_owner_view(), error='No draft to publish yet.')), 400
    elif action == 'nophoto':
        # if a photographer or the publisher asks: a photo (or all of them) comes off this week's draft or the live story
        draft_key, live_key = _key(sunday, region), kv_get(f'story:pub:{region}')
        draft = _load(draft_key)
        keys = [live_key] if body.get('which') == 'live' else [draft_key]
        if body.get('which') != 'live' and draft and draft.get('status') == 'published' and live_key == draft_key + ':live':
            keys.append(live_key)                  # the draft that's live: off both
        for key in keys:
            story = _load(key) if key else None
            if not story:
                continue
            one = str(body.get('url') or '')
            photos = story.get('photos') or ([story['photo']] if story.get('photo') else [])
            story['photos'] = [p for p in photos if one and p.get('url') != one]
            story['photo'] = None
            kv_set(key, json.dumps(story, ensure_ascii=False))
    return jsonify(_owner_view())
