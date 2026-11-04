"""No look is ever given twice, to anyone, and a chosen muse is followed to the letter.

Every look Ella makes is written to a ledger (el_looks). Before a new look goes out it is compared
with what's already been given:
  * the same core outfit (the same clothing pieces, even in new colours or fabrics) can never come
    back: checked against every look ever stored for that gender;
  * a look that shares most of its pieces with a recent one counts as a repeat too
    (checked against the last few thousand looks).
A repeat is thrown away and Ella starts over (up to 4 tries) with the clash spelled out.
Ella also sees a list of looks already given (same occasion first) and a random direction to
start from, so the first try is almost always new.
"""
import hashlib
import json
import os
import random
import re
import secrets
import time
from types import SimpleNamespace

from pro import db, identity
from shop import TYPE_FAMILIES, _tokens

STYLE_MODEL = os.environ.get('STYLE_MODEL') or 'claude-sonnet-4-6'   # the model that writes looks
WINDOW = 3000            # recent looks checked for near-repeats
MAX_TRIES = 4
COLOURS = {
    'black', 'white', 'ivory', 'cream', 'beige', 'ecru', 'nude', 'tan', 'camel', 'brown', 'chocolate', 'mocha', 'taupe',
    'grey', 'gray', 'charcoal', 'silver', 'gold', 'golden', 'rose', 'champagne', 'red', 'scarlet', 'crimson', 'burgundy',
    'wine', 'maroon', 'oxblood', 'pink', 'blush', 'fuchsia', 'magenta', 'coral', 'peach', 'orange', 'rust', 'terracotta',
    'mustard', 'yellow', 'lemon', 'butter', 'green', 'emerald', 'olive', 'sage', 'mint', 'khaki', 'forest', 'teal',
    'turquoise', 'aqua', 'blue', 'navy', 'cobalt', 'royal', 'sky', 'powder', 'indigo', 'denim', 'purple', 'lilac',
    'lavender', 'violet', 'plum', 'mauve', 'metallic', 'multicolour', 'multicolor', 'pastel', 'neutral', 'jewel', 'dark',
    'light', 'pale', 'deep', 'bright', 'tone', 'tonal', 'print', 'printed', 'floral', 'stripe', 'striped', 'check'}
MATERIALS = {
    'satin', 'silk', 'linen', 'cotton', 'wool', 'merino', 'cashmere', 'leather', 'faux', 'suede', 'velvet', 'chiffon',
    'georgette', 'organza', 'lace', 'sequin', 'sequinned', 'sequined', 'crepe', 'jersey', 'tweed', 'twill', 'poplin',
    'chanderi', 'banarasi', 'brocade', 'mulmul', 'khadi', 'tulle', 'mesh', 'knitted', 'ribbed', 'rib', 'cable', 'patent',
    'canvas', 'nylon', 'polyester', 'viscose', 'modal', 'blend', 'stretch', 'raw', 'washed', 'organic', 'recycled'}
FILLER = {'classic', 'essential', 'signature', 'statement', 'chic', 'elegant', 'everyday', 'premium', 'luxe', 'luxury',
          'mini', 'piece', 'look', 'style', 'pair', 'detail', 'detailed', 'the', 'her', 'him'}
CLOTHING = [i for i, fam in enumerate(TYPE_FAMILIES) if fam & {
    'dress', 'shirt', 'top', 'trouser', 'jean', 'skirt', 'short', 'blazer', 'coat', 'sweater', 'kurta', 'saree', 'lehenga',
    'anarkali', 'sherwani', 'bandhgala', 'suit', 'waistcoat', 'coord', 'jumpsuit', 'polo'}]
ETHNIC_WORDS = re.compile(r'diwali|wedding|shaadi|sangeet|mehendi|mehndi|haldi|puja|pooja|eid|navratri|garba|karwa|karva|'
                          r'holi|onam|pongal|durga|lohri|baisakhi|rakhi|reception|engagement|ethnic|traditional', re.I)


# ── What a look is made of ────────────────────────────────────────────────────
def piece(category, name, brand=''):
    toks = _tokens(f'{category} {name}') - _tokens(brand)
    fam = next((i for i, f in enumerate(TYPE_FAMILIES) if toks & f), None)
    family_words = TYPE_FAMILIES[fam] if fam is not None else set()
    details = {t for t in toks if t not in family_words and t not in COLOURS and t not in MATERIALS
               and t not in FILLER and not re.match(r'^(her|him)$', t)}
    return fam, frozenset(details)


def pieces_of(items):
    return [piece(i.get('category', ''), i.get('name', ''), i.get('brand', '')) for i in items or [] if i.get('name')]


def _same(a, b):
    (fa, da), (fb, db_) = a, b
    if fa is None or fb is None:
        return bool(fa is None and fb is None and da and db_ and len(da & db_) / len(da | db_) >= 0.5)
    if fa != fb:
        return False
    if not da and not db_:
        return True
    return bool(da and db_) and len(da & db_) / len(da | db_) >= 1 / 3


def _match_count(new, old):
    left = list(old)
    hits = 0
    for p in new:
        for j, q in enumerate(left):
            if _same(p, q):
                hits += 1
                left.pop(j)
                break
    return hits


def core_of(pcs):
    return [p for p in pcs if p[0] in CLOTHING]


def core_key(pcs):
    core = sorted((p[0], tuple(sorted(p[1]))) for p in core_of(pcs))
    return hashlib.sha1(json.dumps(core).encode()).hexdigest() if core else ''


def is_repeat(new_pcs, old_pcs):
    """Same outfit again? Same core clothing (2+ pieces, colours don't matter), or most pieces shared."""
    if not new_pcs or not old_pcs:
        return False
    cn, co = core_of(new_pcs), core_of(old_pcs)
    if cn and len(cn) == len(co) and _match_count(cn, co) == len(cn):
        rest_new = [p for p in new_pcs if p not in cn]
        rest_old = [p for p in old_pcs if p not in co]
        if len(cn) >= 2 or (rest_new and _match_count(rest_new, rest_old) / max(len(rest_new), 1) >= 0.5):
            return True
    return _match_count(new_pcs, old_pcs) / max(len(new_pcs), len(old_pcs)) >= 0.6


def describe(items, limit=6):
    """'cowl-neck top + wide-leg trousers + belt' (no colours: a new colour isn't a new look)."""
    parts = []
    for i in (items or [])[:limit]:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", i.get('name', '')) if w.lower() not in COLOURS]
        parts.append(' '.join(words[:5]).lower())
    return ' + '.join(p for p in parts if p)


# ── The ledger ────────────────────────────────────────────────────────────────
def _occasion_words(text):
    return {w for w in re.findall(r'[a-z]+', (text or '').lower()) if len(w) > 2} - {'the', 'and', 'for', 'with', 'winter', 'summer', 'monsoon', 'spring', 'autumn'}


def _rows(gender):
    return db('SELECT occasion, muse, look_name, items FROM el_looks WHERE gender = %s ORDER BY created_at DESC LIMIT %s',
              (gender, WINDOW), many=True) or []


def find_repeat(look, gender, extra_given=()):
    """The earlier look this one repeats (as text), or None. Checks the whole history for the same core outfit."""
    items = look.get('items') or []
    new = pieces_of(items)
    if not new:
        return None
    for other in extra_given:                         # e.g. the other two looks in a Plan My Day
        if is_repeat(new, pieces_of(other.get('items'))):
            return describe(other.get('items'))
    key = core_key(new)
    if key and len(core_of(new)) >= 2:
        row = db('SELECT items FROM el_looks WHERE gender = %s AND core_key = %s LIMIT 1', (gender, key), one=True)
        if row:
            return describe(json.loads(row[0]))
    for occasion, muse, name, items_json in _rows(gender):
        old = json.loads(items_json or '[]')
        if is_repeat(new, pieces_of(old)):
            return describe(old)
    return None


def remember(look, gender, occasion, muse, who):
    items = [{'category': i.get('category', ''), 'name': i.get('name', ''), 'brand': i.get('brand', '')}
             for i in look.get('items') or [] if i.get('name')]
    if not items:
        return
    db('INSERT INTO el_looks (id, created_at, gender, occasion, muse, who, look_name, items, core_key) '
       'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
       (secrets.token_hex(8), int(time.time()), gender, (occasion or '')[:200], (muse or '')[:80], who,
        (look.get('look_name') or '')[:120], json.dumps(items), core_key(pieces_of(items))))


def avoid_list(gender, occasion, who, limit=30):
    """Looks already given, most relevant first: this occasion, this person, then everyone recently."""
    want = _occasion_words(occasion)
    scored = []
    for n, (occ, muse, name, items_json) in enumerate(_rows(gender)[:1500]):
        overlap = len(want & _occasion_words(occ)) / max(len(want), 1)
        scored.append((-(overlap * 2) + n / 1500, occ, json.loads(items_json or '[]')))
    scored.sort(key=lambda s: s[0])
    lines = []
    for _, occ, items in scored[:limit]:
        d = describe(items)
        if d:
            lines.append(f'- [{occ[:40]}] {d}')
    return lines


# ── What goes into the prompt ─────────────────────────────────────────────────
HEROES = {
    # Ella's starting points lean Upper East Side glam (see HOUSE_STYLE); still varied enough that no look repeats.
    'women': ['a sequin or beaded mini dress', 'a ruffle-trimmed chiffon dress', 'a satin slip dress', 'a corset top with a mini skirt',
              'a lace dress', 'a feather- or fringe-trimmed piece', 'a tweed jacket with a mini skirt', 'a bow-detail dress',
              'a draped one-shoulder dress', 'a metallic co-ord', 'a puff-sleeve or bubble-hem dress', 'a halter-neck dress',
              'an embellished playsuit', 'a velvet mini dress', 'a jacquard or brocade mini', 'a sheer layered chiffon dress'],
    'men': ['a suit', 'an overshirt', 'a statement jacket', 'a knit polo', 'pleated trousers', 'a bomber', 'a shacket',
            'a camp-collar shirt', 'denim', 'a waistcoat', 'a turtleneck', 'cargo or utility trousers'],
    'women_ethnic': ['a lehenga', 'a saree', 'an anarkali', 'a sharara set', 'a kurta set', 'a pre-draped saree', 'a co-ord kurta set',
                     'an indo-western gown', 'a cape-and-skirt set', 'a palazzo suit', 'a dhoti-pant set'],
    'men_ethnic': ['a sherwani', 'a bandhgala', 'a kurta-pyjama', 'a Nehru jacket set', 'an indo-western jacket', 'a pathani suit',
                   'a kurta with dhoti pants', 'a jodhpuri suit', 'an achkan'],
}
PALETTES = ['jewel tones', 'all-neutral tonal', 'monochrome head to toe', 'pastels', 'black and one bold colour', 'earth tones',
            'metallics', 'red and pink', 'blue and white', 'warm brights', 'cool greys and silver', 'chocolate and cream',
            'olive and gold', 'print-on-print', 'white on white', 'burgundy and camel']
WOMEN_PALETTES = ['champagne and gold', 'blush and rose gold', 'ivory and pearl', 'black and gold', 'one jewel tone with gold',
                  'red and pink', 'silver and ice blue', 'emerald and gold', 'burgundy and blush', 'monochrome head to toe',
                  'pastels with gold', 'nude and crystal', 'cobalt and silver', 'all-white with sparkle', 'lilac and silver']

# Ella dresses women like an Upper East Side party girl, not an office. Every look prompt gets this except
# Dupe (which must copy its inspiration) and men's looks.
HOUSE_STYLE = (
    "\n\nELLA'S HOUSE STYLE (Upper East Side glam). Unless the occasion needs something else, dress her like an "
    "Upper East Side party girl, not like she's going to the office:\n"
    "- One show-stopping, feminine hero piece. Think a champagne sequin-and-bead mini with cascading chiffon ruffles and a "
    "wrap V-neck; or a satin slip, lace, a corset with a mini skirt, feathers or fringe, bows, tweed with a mini skirt by day.\n"
    "- Flirty, leg-lengthening proportions: mini or above the knee for parties, dates, dinners and nights out.\n"
    "- Texture and light: sequins, beading, satin, chiffon, lace, metallics. Palettes like champagne, blush, ivory and gold, "
    "or one rich jewel tone.\n"
    "- Finish the fantasy: a quilted chain-strap mini bag (flower appliqué is very her), statement gold or floral earrings, "
    "fine stacked gold bangles, barely-there metallic strappy heels.\n"
    "- Beauty: glossy blown-out waves, bronzed glowing skin, a soft smoky eye, nude-pink gloss.\n"
    "- Never the hero piece: office tailoring, plain basics, all-beige minimalism, sensible flats.\n"
    "- Work, interviews, religious or formal daytime events: keep it appropriate (no minis or sequins) but feminine and "
    "polished, Blair Waldorf by day: tweed, pleats, bows, a headband, a pretty top-handle bag.\n"
    "- Indian festive occasions: the same glam in Indian wear: sequin, mirror or bead work, ruffled or pre-draped sarees, "
    "chiffon, blush and champagne, an embellished potli, statement jhumkas.\n"
    "The user's chosen muse and era come first; this decides everything they leave open.")
HOUSE_STYLE_COUPLE = (" This is for HER look. Style HIM to match her glamour, Chuck Bass to her Serena: a velvet or "
                      "satin-lapel jacket, an open collar or a loosened silk tie, polished loafers, one sharp accessory.")


def house_style(gender, mode=''):
    if mode == 'dupe' or gender == 'men':
        return ''
    return HOUSE_STYLE + (HOUSE_STYLE_COUPLE if gender == 'couple' else '')


def variation(gender, occasion):
    ethnic = bool(ETHNIC_WORDS.search(occasion or ''))
    pool = HEROES.get(f"{'men' if gender == 'men' else 'women'}{'_ethnic' if ethnic else ''}")
    if gender == 'couple':
        pool = HEROES['women_ethnic' if ethnic else 'women']
    palettes = PALETTES if gender == 'men' else WOMEN_PALETTES
    return (f"\nStarting point for this look (to keep every look new): build it around {random.choice(pool)} with a "
            f"{random.choice(palettes)} palette, unless the muse or occasion clearly calls for something else.")


def muse_rule(muse):
    if not muse or muse.strip().lower() in ('no specific muse', 'none', ''):
        return ''
    m = muse.strip()[:60]
    return (f"\n\nMUSE RULE (this comes first): dress the user exactly the way {m} really dresses. Base every piece on "
            f"{m}'s actual signature style: the silhouettes, colours, fabrics, labels, accessories, hair and makeup they're "
            f"known for in real red-carpet, street-style and off-duty looks. Every item must be something {m} would "
            f"genuinely wear. If the aesthetic era or anything else clashes with {m}'s style, {m} wins. At this budget and in "
            f"this country, use the closest pieces the shopper can buy. Channel a different real {m} outfit each time, "
            f"so the same muse never gives a repeat. In ella_note, name the real {m} look or signature you're channelling.")


def rules(gender, occasion, muse, who, mode=''):
    try:
        given = avoid_list(gender, occasion, who)
    except Exception as e:
        print('[looks] avoid list skipped:', e)
        given = []
    text = house_style(gender, mode) + muse_rule(muse)
    text += ("\n\nNEVER REPEAT A LOOK: every look must be new, for this person and for everyone else, even for the same "
             "occasion asked again. The same main pieces in a different colour or fabric still count as a repeat.")
    if given:
        text += " These have already been given, so don't recreate any of them or their main combination of pieces:\n" + '\n'.join(given)
    return text + variation(gender, occasion)


# ── One call that only ever returns a new look ────────────────────────────────
class RepeatError(Exception):
    pass


def _parse(text):
    t = text.strip()
    if '```json' in t:
        t = t.split('```json')[1].split('```')[0].strip()
    elif '```' in t:
        t = t.split('```')[1].split('```')[0].strip()
    try:
        return json.loads(t)
    except ValueError:
        return None


def _who_and_rules(data):
    gender = str(data.get('gender') or 'women')
    occasion = str(data.get('occasion') or data.get('schedule') or data.get('new_occasion') or data.get('anchor_item')
                   or data.get('inspo_look') or '')[:200]
    muse = str(data.get('celebrity') or data.get('celeb') or '')
    try:
        who = identity()[1]
    except Exception:
        who = 'unknown'
    return gender, occasion, muse, who, rules(gender, occasion, muse, who, str(data.get('mode') or ''))


def _clash(text, kind, gender, occasion, muse, who):
    """None if this answer can go out (and it's remembered); otherwise the earlier look it repeats."""
    parsed = _parse(text)
    if parsed is None:
        return None                             # let the caller report the parse error as before
    looks = parsed.get('looks') if kind == 'day' else [parsed]
    looks = [l for l in looks or [] if isinstance(l, dict)]
    try:
        for k, look in enumerate(looks):
            clash = find_repeat(look, gender, looks[:k])
            if clash:
                return clash
        for look in looks:
            remember(look, gender, occasion, muse, who)
    except Exception as e:                      # the ledger being down must never stop a look
        print('[looks] repeat check skipped:', e)
    return None


def _clash_note(clash):
    return (f"\n\nREPEAT BLOCKED: your last answer was too close to a look already given ({clash}). "
            f"Start again with different main pieces, a different silhouette and a different colour story.")


GAVE_UP = "Ella couldn't find a look that hasn't been given before. Change a detail and try again."


def create_unique(client, data, prompt, max_tokens, model=None, kind='look'):
    """Like client.messages.create, but the look (or each look of a day plan) is guaranteed new.
    Returns an object with .content[0].text, so the existing parsing code works unchanged."""
    gender, occasion, muse, who, extra = _who_and_rules(data)
    model = model or STYLE_MODEL
    clash_note = ''
    for attempt in range(MAX_TRIES):
        message = client.messages.create(model=model, max_tokens=max_tokens,
                                         messages=[{'role': 'user', 'content': prompt + extra + clash_note}])
        clash = _clash(message.content[0].text, kind, gender, occasion, muse, who)
        if not clash:
            return message
        print(f'[looks] try {attempt + 1}: repeat of "{clash}", restyling')
        clash_note = _clash_note(clash)
    raise RepeatError(GAVE_UP)


def stream_unique(client, data, prompt, max_tokens, model=None, kind='look'):
    """The same guarantee, written live: yields ('delta', text) while Ella writes, ('restart', clash) if
    what she wrote repeats an earlier look (she then starts over), and finally ('done', full_text)."""
    gender, occasion, muse, who, extra = _who_and_rules(data)
    model = model or STYLE_MODEL
    clash_note = ''
    for attempt in range(MAX_TRIES):
        chunks = []
        with client.messages.stream(model=model, max_tokens=max_tokens,
                                    messages=[{'role': 'user', 'content': prompt + extra + clash_note}]) as s:
            for text in s.text_stream:
                chunks.append(text)
                yield 'delta', text
        full = ''.join(chunks)
        clash = _clash(full, kind, gender, occasion, muse, who)
        if not clash:
            yield 'done', full
            return
        print(f'[looks] try {attempt + 1}: repeat of "{clash}", restyling')
        clash_note = _clash_note(clash)
        yield 'restart', clash
    raise RepeatError(GAVE_UP)
