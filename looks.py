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

# The seven aesthetics on the picker. Each has a one-line definition for Ella and its own starting points,
# so the variety that stops repeats stays inside the aesthetic (a Goth look never drifts toward sequins).
AESTHETICS = {
    'clean girl': {
        'name': 'Clean Girl',
        'brief': 'polished minimalism: fitted basics, tailored trousers, slip skirts, crisp shirts and fine knits in neutral '
                 'tones; gold hoops, a sleek bun, dewy skin, brushed-up brows and lip oil. No loud prints or heavy embellishment.',
        'women': ['a ribbed tank with tailored trousers', 'a slip skirt with a fitted tee', 'a crisp white shirt', 'a monochrome knit set',
                  'a linen co-ord', 'a bodysuit with straight-leg jeans', 'a minimalist slip dress', 'an oversized blazer over a tank',
                  'a fine-gauge knit dress', 'a column skirt with a cropped knit'],
        'men': ['a fitted white tee with pleated trousers', 'a knit polo', 'a crisp Oxford shirt', 'a monochrome knit set',
                'a linen shirt with tailored shorts', 'a fine-gauge crewneck', 'a minimal overshirt'],
        'palettes': ['white and cream', 'beige and camel', 'soft grey and white', 'black and white', 'sage and cream',
                     'tonal taupe', 'butter yellow and cream', 'baby blue and white']},
    'quiet luxury': {
        'name': 'Quiet Luxury',
        'brief': 'old-money understatement: impeccable fabrics (cashmere, silk, fine wool, suede), perfect tailoring, no visible '
                 'logos, muted tones; loafers, a structured leather bag, fine gold jewellery, natural hair and makeup.',
        'women': ['a cashmere crewneck', 'a camel coat', 'tailored wide-leg trousers', 'a silk shirt', 'a knit polo',
                  'a merino turtleneck', 'a double-faced wool coat', 'a column maxi skirt', 'a suede jacket', 'a bias-cut silk skirt'],
        'men': ['a cashmere crewneck', 'a camel overcoat', 'a suede bomber', 'pleated wool trousers', 'a knit polo',
                'an unstructured blazer', 'a merino turtleneck'],
        'palettes': ['camel and cream', 'navy and ivory', 'chocolate and oatmeal', 'charcoal and white', 'olive and stone',
                     'tonal greige', 'black and camel']},
    'y2k': {
        'name': 'Y2K',
        'brief': 'early-2000s fun: low-rise everything, baby tees, butterfly and halter tops, velour, denim minis, metallics and '
                 'mesh; tinted sunglasses, butterfly clips, a shoulder bag, platform sandals, glossy lips.',
        'women': ['a low-rise mini skirt', 'a baby tee', 'a velour tracksuit', 'a butterfly top', 'a satin halter',
                  'low-rise flared jeans', 'a mesh top', 'a metallic mini', 'cargo pants with a crop top', 'a slip dress over a tee',
                  'a denim mini'],
        'men': ['baggy jeans with a graphic tee', 'a velour tracksuit', 'a track jacket', 'a mesh jersey', 'cargo pants',
                'a bowling shirt', 'a zip hoodie with a trucker cap'],
        'palettes': ['baby pink and silver', 'lilac and chrome', 'baby blue and white', 'hot pink and black', 'metallics',
                     'butter yellow and pink']},
    'goth': {
        'name': 'Goth',
        'brief': 'dark and romantic, Victorian or punk: black lace, velvet, leather, mesh and corsetry; silver jewellery and '
                 'chokers, platform or buckled boots, a dark lip and smoky eyes.',
        'women': ['a lace slip dress', 'a corset with a long skirt', 'a velvet maxi dress', 'a mesh top with a leather skirt',
                  'a long black coat', 'a ruffled Victorian blouse', 'platform boots with a tulle skirt', 'a leather mini',
                  'a fishnet layered look', 'a high-neck lace gown'],
        'men': ['a long black coat', 'a mesh top under a leather jacket', 'black skinny jeans with boots', 'a ruffled poet shirt',
                'a velvet blazer', 'black cargo pants with platform boots'],
        'palettes': ['all black', 'black and oxblood', 'black and deep purple', 'black and silver', 'black and blood red',
                     'charcoal and black']},
    'cottagecore': {
        'name': 'Cottagecore',
        'brief': 'romantic countryside: prairie and milkmaid dresses, puff sleeves, smocking, gingham, florals, eyelet, crochet '
                 'and embroidered knits; ribbons, a straw or basket bag, Mary Janes, soft natural makeup.',
        'women': ['a prairie dress', 'a puff-sleeve blouse with a midi skirt', 'a smocked sundress', 'a gingham dress',
                  'an embroidered cardigan', 'a linen pinafore', 'a floral milkmaid dress', 'a crochet top with a tiered skirt',
                  'a lace-collar dress', 'an eyelet midi'],
        'men': ['a linen shirt with suspenders', 'a cable-knit cardigan', 'corduroy trousers', 'a chore jacket',
                'a grandad-collar shirt', 'a knitted vest'],
        'palettes': ['sage and cream', 'floral pastels', 'butter yellow and white', 'dusty rose and ivory', 'red gingham and white',
                     'lavender and cream', 'earth tones']},
    'grunge': {
        'name': 'Grunge',
        'brief': '90s Seattle undone: plaid flannel, band tees, ripped jeans, slip dresses over tees, oversized knits and a '
                 'leather jacket; combat boots, layered silver, messy hair, smudged liner.',
        'women': ['a plaid flannel shirt', 'a slip dress with a cardigan', 'ripped jeans with a band tee', 'a leather jacket',
                  'combat boots with a mini skirt', 'an oversized knit', 'a babydoll dress with tights', 'a denim jacket over a slip',
                  'a mesh layered top', 'cargo pants with a thermal'],
        'men': ['a plaid flannel', 'ripped jeans with a band tee', 'a leather jacket', 'a thermal with cargo pants',
                'an oversized knit', 'a denim jacket', 'combat boots with straight jeans'],
        'palettes': ['black and plaid red', 'washed black and grey', 'olive and black', 'burgundy and black',
                     'faded denim and black', 'forest green and brown']},
}
_AESTHETIC_WORDS = [('clean girl', ('clean girl',)), ('quiet luxury', ('quiet luxury', 'old money')), ('y2k', ('y2k',)),
                    ('goth', ('goth',)), ('cottagecore', ('cottagecore', 'cottage core')), ('grunge', ('grunge',)),
                    ('ues', ('upper east side',))]


def aesthetic_key(era):
    """Which of the seven aesthetics was picked. Nothing picked (or Ella's automatic Indian festive) means
    Upper East Side, Ella's own. Anything else (an older saved look's era) returns None and leads on its own."""
    e = (era or '').lower()
    if 'no specific aesthetic' in e:
        return 'celeb'                     # "just dress me like my celeb"
    for key, words in _AESTHETIC_WORDS:
        if any(w in e for w in words):
            return key
    if not e.strip() or 'indian festive' in e:
        return 'ues'
    return None


def aesthetic_rule(era):
    key = aesthetic_key(era)
    a = AESTHETICS.get(key)
    if a:
        return (f"\n\nAESTHETIC: the user picked {a['name']}, so every piece, colour and accessory must read as {a['name']}: "
                f"{a['brief']}")
    if key == 'celeb':
        return ("\n\nAESTHETIC: none. The user wants to be dressed purely like their chosen muse, so the muse decides "
                "everything: silhouettes, colours, labels, hair and makeup.")
    if key is None and (era or '').strip():
        return (f"\n\nAESTHETIC: the user typed their own: {era.strip()}. Make every piece, colour and accessory read "
                f"as {era.strip()}, the way fashion people use the term.")
    return ''


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


def house_style(gender, mode='', budget='', country='', era=''):
    if mode in ('dupe', 'reel') or gender == 'men' or aesthetic_key(era) != 'ues':
        return ''
    return HOUSE_STYLE + (HOUSE_STYLE_COUPLE if gender == 'couple' else '') + signature_brands(budget, country)


def signature_brands(budget, country):
    """Ella's favourite luxury label, named outright so it actually turns up: only for luxury budgets and
    only where it delivers (Annie's Ibiza doesn't ship to India)."""
    from brand_atlas import ships_to
    if budget != 'luxury' or not ships_to("Annie's Ibiza", country):
        return ''
    return ("\n- Ella's favourite luxury label for this mood is Annie's Ibiza (anniesibiza.com): hand-beaded silk "
            "chiffon, ruffled tiers, Victorian and Ottoman-inspired embellishment, feather boas and statement "
            "accessories. When it suits the occasion, make the hero piece (or a statement accessory) Annie's Ibiza, "
            "with its store set to anniesibiza.com.")


def variation(gender, occasion, era='', muse=''):
    if aesthetic_key(era) == 'celeb':
        return (f"\nStarting point for this look (to keep every look new): pick pieces true to {muse}'s style that "
                f"aren't the most obvious ones, without leaving that style.")
    if aesthetic_key(era) is None and (era or '').strip():
        return (f"\nStarting point for this look (to keep every look new): build it around a key piece that isn't the "
                f"most obvious {era.strip()} choice, in colours true to {era.strip()}.")
    ethnic = bool(ETHNIC_WORDS.search(occasion or ''))
    side = 'men' if gender == 'men' else 'women'          # a couple's starting point is her look
    key = aesthetic_key(era)
    a = AESTHETICS.get(key)
    if ethnic:
        pool = HEROES[f'{side}_ethnic']
        palettes = a['palettes'] if a else (PALETTES if side == 'men' else WOMEN_PALETTES)
    elif a:
        pool, palettes = a[side], a['palettes']
    elif key == 'ues':
        pool, palettes = HEROES[side], (PALETTES if side == 'men' else WOMEN_PALETTES)
    else:
        pool, palettes = None, PALETTES
    piece = f"build it around {random.choice(pool)}" if pool else "build it around a key piece that isn't the most obvious choice"
    palette = random.choice(palettes)
    article = 'an' if palette[0] in 'aeiou' else 'a'
    return (f"\nStarting point for this look (to keep every look new): {piece} with {article} {palette} palette, "
            f"unless the muse or occasion clearly calls for something else.")


from muses import building, muse_data, muse_identity, outfit_line, piece_line, used, wait_for   # who the muse is, what they wear, what they've worn

# Every look: a product only gets a name if it really exists under it (a made-up name sends the shopper nowhere)
HONEST_NAMES = ("\n\nPRODUCT NAMES: give a piece its exact product name only when you're sure that product really exists "
                "under that name at that brand, and is sold now. Otherwise name it in plain words (\"Flat leather sandals in "
                "tan\"). Never make up product, line or collection names, and never borrow one from another brand (Maysale is "
                "Manolo Blahnik's, not Jimmy Choo's; Waverly isn't a Zimmermann gown): a wrong name can't be found in any "
                "store. The app looks every piece up in real shops and shows the real product's name.")


def real_pieces(m, pieces):
    """The other real pieces the muse has been seen in, exact names only (for shoes, bags and jewellery)."""
    named = [p for p in pieces or [] if p.get('name')][:24]
    if not named:
        return ''
    return (f"\nOTHER REAL PIECES {m} HAS BEEN SEEN IN (exact names, from fashion coverage):\n" +
            '\n'.join(f"- {p['category'] or 'Piece'}: {piece_line(p)}" + (f", worn at {p['event']}" if p.get('event') else '')
                      for p in named))


def real_outfit_rule(m, who, outfits, used_ids, occasion, look, file, pieces=()):
    """The muse has a bank of real outfits: the look IS one of them, her exact pieces (app.py makes sure of it)."""
    text = f"\n\nMUSE RULE (this comes first, above every other rule): the user wants to dress exactly like {m}."
    text += f" WHO THIS IS: {who}" if who else f" Be sure you have the right person: {m}, never someone with a similar name."
    text += (f"\nREAL OUTFITS {m} HAS ACTUALLY WORN (from fashion coverage, with the event and date):\n" +
             '\n'.join(outfit_line(o) for o in outfits[:20]))
    text += real_pieces(m, pieces)
    text += (f"\nTHIS LOOK MUST BE ONE OF THESE REAL OUTFITS, worn exactly as {m} wore it. Pick the one that best suits "
             f"\"{occasion or 'their day'}\": its formality, setting and season" + (f", and the {look} aesthetic they picked if one fits" if look else '') +
             ". If none is a perfect match, pick the closest (an evening outfit for any evening event, a daytime one for any day event).")
    fresh = [o['id'] for o in outfits if o['id'] not in used_ids]
    if used_ids and fresh:
        text += f" This person has already been given {', '.join(used_ids[-8:])}, so pick a different one if another suits the occasion."
    text += (f"\nTHE ITEMS ARE HER EXACT PIECES, one item per piece, in the outfit's order:"
             f"\n- \"brand\": the brand exactly as given."
             f"\n- \"name\": the product name in quotes above, word for word, with its colour (like \"Carousel Metallic Twist Gown in "
             f"Silver\"). When the product name isn't known, name the piece in plain words, like \"Silver metallic halter gown\". "
             f"NEVER make up a product, line or collection name."
             f"\n- \"price\": its real retail price in the shopper's currency, as best you know it."
             f"\n- \"description\": one sentence on how {m} wore it."
             f"\nUse the exact pieces even if they cost more than the shopper's budget: they asked to dress exactly like {m}. Never "
             f"swap a piece for a cheaper, similar or different one, never change a colour, and never restyle the outfit to suit a "
             f"theme or a house style."
             f"\nThen complete the look with what the coverage doesn't mention (shoes, bag, jewellery): first from the OTHER REAL "
             f"PIECES {m} has been seen in, if any suit, named the same way; otherwise in plain words, in {m}'s style. Use the "
             f"outfit's hair and makeup if given."
             f"\nAdd \"outfit_id\": \"<the id you picked, like o2>\" to the JSON. In ella_note, name the real event and date "
             f"(\"what {m} wore to ...\")."
             f"\nOnly if every one of these outfits is wrong for the occasion (for example a gym session when they're all evening "
             f"looks), set \"outfit_id\": null and build the look from the other real pieces {m} has worn and the style file below, "
             f"naming anything you don't have an exact name for in plain words, never a made-up one.")
    if file:
        text += f"\nSTYLE FILE for {m} (for anything the outfit doesn't cover):\n{file}"
    return text


def muse_rule(muse, era='', file=None, pieces=()):
    if not muse or muse.strip().lower() in ('no specific muse', 'none', ''):
        return ''
    m = muse.strip()[:60]
    who = muse_identity(m)
    key = aesthetic_key(era)
    a = AESTHETICS.get(key)
    look = a['name'] if a else ('' if key in ('celeb', 'ues') and 'upper east side' not in (era or '').lower()
                                else (era or '').strip()[:60])
    text = (f"\n\nMUSE RULE (this comes first): dress the user exactly the way {m} really dresses. It overrides every "
            f"other style rule.")
    if who:
        text += f" WHO THIS IS: {who}"
    else:
        text += f" Be sure you have the right person: style exactly {m}, never someone with a similar name."
    if file:
        text += (f"\nSTYLE FILE for {m}, written from real fashion coverage and photos of them. Trust it over your own "
                 f"memory:\n{file}\n"
                 f"Build the look from this file: its silhouettes, colours, fabrics, labels, shoes, bags, jewellery, hair "
                 f"and makeup. Respect its 'Never' line. If the file doesn't cover this occasion, dress {m} for it the way "
                 f"the file says they dress, never in a style the file doesn't show (for example, no dramatic couture if "
                 f"they dress understated).")
        labels = (f" When {m} has actually been seen in a label (from the file), use that label itself, not a lookalike, "
                  f"whenever the shopper can buy it in their country at this budget, even if it isn't in the brand list, "
                  f"with its store set to that label's own site; otherwise use the closest piece in the same spirit.")
    else:
        text += (f" Base every piece on {m}'s real, widely documented style: the silhouettes, colours, fabrics, accessories, "
                 f"hair and makeup they're known for. If you aren't sure how {m} dresses, keep to what is certain and "
                 f"keep it understated rather than inventing a dramatic look.")
        labels = (f" If you're certain {m} really wears a particular label for this kind of occasion, use that label "
                  f"itself, not a lookalike, whenever the shopper can buy it in their country at this budget, even if it "
                  f"isn't in the brand list, with its store set to that label's own site. Never call a label \"theirs\" "
                  f"unless you're certain.")
    if look:
        text += (f" The user also picked {look}, so dress them the way {m} would wear {look}, keeping {m}'s real "
                 f"signatures.")
    if real_pieces(m, pieces):
        text += real_pieces(m, pieces) + (f"\nUse these real pieces of {m}'s, by their exact names, wherever they suit the "
                                          f"occasion; anything else is named in plain words unless it's a real product.")
    return text + labels + (
        f" When a label {m} wears isn't sold in the shopper's country or is far over their budget, use the closest piece "
        f"they can buy instead."
        f" NEVER invent facts: no made-up appearances, events, outfits or brand loyalties. In ella_note, mention one real "
        f"signature of {m}'s style{' from the style file' if file else ''}, not a specific event unless you're certain it "
        f"happened. Vary the pieces from look to look, so the same muse never gives a repeat.")


def character_rule(char, era=''):
    """A muse from E&L's own drama series: Ella can't know them, so the owner describes their style."""
    name, style = char['name'][:40], (char.get('style') or '')[:300]
    key = aesthetic_key(era)
    a = AESTHETICS.get(key)
    look = a['name'] if a else ('' if key in ('celeb', 'ues') else (era or '').strip()[:60])
    text = (f"\n\nMUSE RULE (this comes first): the muse is {name}, a character from E&L's drama series "
            f"\"The Social Representation\". Their style: {style or 'polished, confident, unmistakably Upper East Side'}. "
            f"Dress the user exactly the way {name} dresses in the series: their silhouettes, colours, labels, "
            f"accessories, hair and makeup.")
    if look:
        text += f" The user also picked {look}, so dress them the way {name} would wear {look}."
    return text + f" In ella_note, mention {name} by name, the way a society columnist would."


def rules(gender, occasion, muse, who, mode='', budget='', country='', era='', character=None, info=None):
    try:
        given = avoid_list(gender, occasion, who)
    except Exception as e:
        print('[looks] avoid list skipped:', e)
        given = []
    era = re.sub(r'[\r\n"\u201c\u201d]+', ' ', era or '').strip()[:60]
    real_muse = bool(muse) and muse.strip().lower() not in ('no specific muse', 'none', '')
    if mode in ('dupe', 'reel') or (aesthetic_key(era) == 'celeb' and not real_muse and not character):
        era = ''                                   # Dupe copies its inspiration; "like my celeb" without a celeb = Ella's own
    info = info or {}
    file, outfits = info.get('text'), info.get('outfits') or []
    if outfits:
        # A real outfit the muse wore, recreated: no house style, no theme on top, no random starting piece,
        # and no "never repeat" (copying the real thing is the point; each person still gets a new one each time).
        key = aesthetic_key(era)
        a = AESTHETICS.get(key)
        look = a['name'] if a else ('' if key in ('celeb', 'ues') else era)
        try:
            used_ids = used(who, muse)
        except Exception:
            used_ids = []
        return real_outfit_rule(muse.strip()[:60], muse_identity(muse), outfits, used_ids, occasion, look, file,
                                info.get('pieces') or []) + HONEST_NAMES
    text = (house_style(gender, mode, budget, country, era) + aesthetic_rule(era) +
            (character_rule(character, era) if character else muse_rule(muse, era, file, info.get('pieces') or [])))
    if mode in COPY_MODES:
        # Dupe and Get this look copy something: no random starting piece or palette, no forcing it to be different
        return text + HONEST_NAMES + ("\n\nCOPY THE ORIGINAL: match it as closely as the shopper can buy it: the same hero piece, the same "
                       "colours, the same silhouette and the same overall vibe. Never swap the hero piece for something else "
                       "and never change the colour story. Fill in only what the original doesn't show (shoes, bag, jewellery) "
                       "in the same spirit.")
    text += ("\n\nNEVER REPEAT A LOOK: every look must be new, for this person and for everyone else, even for the same "
             "occasion asked again. The same main pieces in a different colour or fabric still count as a repeat.")
    if given:
        text += " These have already been given, so don't recreate any of them or their main combination of pieces:\n" + '\n'.join(given)
    return text + variation(gender, occasion, era, (muse or '').strip()[:60]) + HONEST_NAMES


COPY_MODES = ('dupe', 'reel')


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
    character = data.get('_character') if isinstance(data.get('_character'), dict) else None
    if character:
        muse = character['name']
    try:
        who = identity()[1]
    except Exception:
        who = 'unknown'
    info = {}
    real_muse = bool(muse) and muse.strip().lower() not in ('no specific muse', 'none', '')
    if real_muse and not character and str(data.get('mode') or '') not in COPY_MODES:
        try:
            info = muse_data(muse, wait=0 if data.get('_muse_waited') else 40)   # usually ready: it started at the muse screen
        except Exception as e:
            print('[looks] muse research skipped:', e)
    data['_muse_bank'] = info.get('outfits') or []
    data['_muse_pieces'] = info.get('pieces') or []
    data['_muse_key'] = muse
    return gender, occasion, muse, who, rules(gender, occasion, muse, who, str(data.get('mode') or ''),
                                              str(data.get('budget') or ''), str(data.get('country') or '')[:2],
                                              str(data.get('era') or '')[:80], character, info)


def plan_looks(data):
    """Looks already planned for other functions of the same wedding: this one must not repeat them."""
    out = []
    for l in (data.get('plan_looks') or [])[:10] if isinstance(data.get('plan_looks'), list) else []:
        if isinstance(l, dict) and isinstance(l.get('items'), list):
            items = [{'category': str(i.get('category') or '')[:60], 'name': str(i.get('name') or '')[:120],
                      'brand': str(i.get('brand') or '')[:60]} for i in l['items'][:14] if isinstance(i, dict)]
            if items:
                out.append({'items': items})
    return out


def _content(text, image):
    """The prompt, with a picture first if there is one: a URL (a YouTube thumbnail) or an inline screenshot."""
    if not image:
        return text
    source = image if isinstance(image, dict) else {'type': 'url', 'url': image}
    return [{'type': 'image', 'source': source}, {'type': 'text', 'text': text}]


def _image_failed(e):
    from alerts import ServiceDown
    return not isinstance(e, ServiceDown)


def _clash(text, kind, gender, occasion, muse, who, planned=(), check=True):
    """None if this answer can go out (and it's remembered); otherwise the earlier look it repeats.
    check=False (a dupe or a reel's look) skips the repeat check: copying the original is the point."""
    parsed = _parse(text)
    if parsed is None:
        return None                             # let the caller report the parse error as before
    looks = parsed.get('looks') if kind == 'day' else [parsed]
    looks = [l for l in looks or [] if isinstance(l, dict)]
    try:
        for k, look in enumerate(looks if check else []):
            clash = find_repeat(look, gender, list(planned) + looks[:k])
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


def create_unique(client, data, prompt, max_tokens, model=None, kind='look', image_url=None):
    """Like client.messages.create, but the look (or each look of a day plan) is guaranteed new.
    Returns an object with .content[0].text, so the existing parsing code works unchanged."""
    gender, occasion, muse, who, extra = _who_and_rules(data)
    planned = plan_looks(data)
    model = model or STYLE_MODEL
    clash_note = ''
    for attempt in range(MAX_TRIES):
        try:
            message = client.messages.create(model=model, max_tokens=max_tokens, messages=[
                {'role': 'user', 'content': _content(prompt + extra + clash_note, image_url)}])
        except Exception as e:
            if not (isinstance(image_url, str) and _image_failed(e)):
                raise
            print('[looks] the picture could not be used, styling from the text:', repr(e)[:160])
            image_url = None
            message = client.messages.create(model=model, max_tokens=max_tokens,
                                             messages=[{'role': 'user', 'content': prompt + extra + clash_note}])
        clash = _clash(message.content[0].text, kind, gender, occasion, muse, who, planned,
                       check=str(data.get('mode') or '') not in COPY_MODES and not data.get('_muse_bank'))
        if not clash:
            return message
        print(f'[looks] try {attempt + 1}: repeat of "{clash}", restyling')
        clash_note = _clash_note(clash)
    raise RepeatError(GAVE_UP)


MUSE_WAIT = 40      # seconds a live look waits for the muse's research (saying what's happening meanwhile)


def _muse_wait(data):
    """While the muse's real outfits are still being read up on: a line every few seconds saying so (the app shows
    it on the results page, so nobody stares at the loading screen)."""
    muse = str(data.get('celebrity') or data.get('celeb') or '').strip()
    if (not muse or muse.lower() in ('no specific muse', 'none') or isinstance(data.get('_character'), dict)
            or str(data.get('mode') or '') in COPY_MODES or not building(muse)):
        return
    m = muse[:60]
    lines = [f'Reading what {m} has really worn…', f'Finding the real {m} outfit that suits this…',
             'Getting the exact pieces and their names…', f'Still reading up on {m}: the first time takes a little longer…']
    start, k = time.time(), 0
    while time.time() - start < MUSE_WAIT and building(muse):
        yield lines[min(k, len(lines) - 1)]
        k += 1
        wait_for(muse, 4)
    data['_muse_waited'] = True


def stream_unique(client, data, prompt, max_tokens, model=None, kind='look', image_url=None):
    """The same guarantee, written live: yields ('status', text) while the muse is still being read up on,
    ('delta', text) while Ella writes, ('restart', clash) if what she wrote repeats an earlier look (she then
    starts over), and finally ('done', full_text)."""
    for line in _muse_wait(data):
        yield 'status', line
    gender, occasion, muse, who, extra = _who_and_rules(data)
    planned = plan_looks(data)
    model = model or STYLE_MODEL
    clash_note = ''
    for attempt in range(MAX_TRIES):
        chunks = []
        try:
            with client.messages.stream(model=model, max_tokens=max_tokens, messages=[
                    {'role': 'user', 'content': _content(prompt + extra + clash_note, image_url)}]) as s:
                for text in s.text_stream:
                    chunks.append(text)
                    yield 'delta', text
        except Exception as e:
            if chunks or not (isinstance(image_url, str) and _image_failed(e)):
                raise
            print('[looks] the picture could not be used, styling from the text:', repr(e)[:160])
            image_url = None
            with client.messages.stream(model=model, max_tokens=max_tokens, messages=[
                    {'role': 'user', 'content': prompt + extra + clash_note}]) as s:
                for text in s.text_stream:
                    chunks.append(text)
                    yield 'delta', text
        full = ''.join(chunks)
        clash = _clash(full, kind, gender, occasion, muse, who, planned,
                       check=str(data.get('mode') or '') not in COPY_MODES and not data.get('_muse_bank'))
        if not clash:
            yield 'done', full
            return
        print(f'[looks] try {attempt + 1}: repeat of "{clash}", restyling')
        clash_note = _clash_note(clash)
        yield 'restart', clash
    raise RepeatError(GAVE_UP)
