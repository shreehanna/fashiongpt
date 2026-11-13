"""Shop links and product photos for each item in a look, for any brand, in the shopper's country.

For every item Ella picks, this looks for the real product page, in this order:
  1. the store Ella says it's sold from in that country (e.g. net-a-porter.com, myntra.com)
  2. the brand's own website (known brands, or found by searching "<brand> official site",
     so Emilio Pucci, Roberto Cavalli, Revolve, Nike... all work without a list)
  3. the big stores that deliver in that country (regions.py)
and prefers the country's version of a page (zara.com/in/... for India, /us/ for the US).
If nothing is found, the item says so and tells the shopper which site to search.

Search provider, set in Render's environment:
  SERPER_API_KEY   serper.dev (Google results; 2,500 free searches, then about $1 per 1,000).
                   Recommended: Google's Custom Search API closes on 1 January 2027.
  GOOGLE_API_KEY + GOOGLE_CX   your existing Custom Search keys, used until then if Serper isn't set.
"""
import html as htmllib
import os
import re
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait
import threading
from urllib.parse import quote_plus, urlparse

import requests as req
from flask import Blueprint, jsonify, request

from alerts import search_result   # counts Serper credits; alerts the owner when they run low or out

import unicodedata

from brand_atlas import SITE_OVERRIDES
from pro import owner_request
from regions import retailers_for

shop = Blueprint('shop', __name__)

# brands whose website isn't simply brandname.com, plus each site's product-page shape
BRAND_SITES = [
    ('& other stories', 'stories.com', r'/product\.|/product/|\.\d{6,}'),
    ('massimo dutti', 'massimodutti.com', r'-l\d{6,}|/p/|\d{8}'),
    ('banana republic', 'bananarepublic.gap.com', r'product\.do|/product/'),
    ('saint laurent', 'ysl.com', r'\d{6,}|/product/'),
    ('ysl', 'ysl.com', r'\d{6,}|/product/'),
    ('tom ford', 'tomford.com', r'\.html|/product/'),
    ('jack & jones', 'jackjones.in', r'/p/|\d{6,}|\.html'),
    ('vero moda', 'veromoda.in', r'/p/|\d{6,}|\.html'),
    ('selected homme', 'selected.com', r'\.html|\d{6,}'),
    ('label ritu kumar', 'ritukumar.com', r'/products/|/product/'),
    ('ritu kumar', 'ritukumar.com', r'/products/|/product/'),
    ('nykaa fashion', 'nykaafashion.com', r'/p/\d+'),
    ('tata cliq luxury', 'luxury.tatacliq.com', r'/p-mp\d+'),
    ('tata cliq', 'tatacliq.com', r'/p-mp\d+'),
    ('marks & spencer', 'marksandspencer.com', r'/p/|\d{6,}'),
    ('m&s', 'marksandspencer.com', r'/p/|\d{6,}'),
    ('h&m', 'hm.com', r'productpage\.\d+'),
    ('net-a-porter', 'net-a-porter.com', r'/product/\d+'),
    ('net a porter', 'net-a-porter.com', r'/product/\d+'),
    ('mr porter', 'mrporter.com', r'/product/\d+'),
    ('saks', 'saksfifthavenue.com', r'/product/'),
    ('bergdorf', 'bergdorfgoodman.com', r'/p/|prod\d+'),
    ('reformation', 'thereformation.com', r'/products/'),
    ('w for woman', 'wforwoman.com', r'\.html|/p/|/products/'),
    ('the iconic', 'theiconic.com.au', r'\d{6,}\.html'),
    ('zara', 'zara.com', r'-p\d{6,}\.html'),
    ('asos', 'asos.com', r'/prd/\d+'),
    ('mango', 'mango.com', r'_\d{6,}|/p/'),
    ('cos', 'cos.com', r'/product\.|/product/|\.\d{6,}'),
    ('uniqlo', 'uniqlo.com', r'/products/'),
    ('myntra', 'myntra.com', r'/\d{5,}/buy'),
    ('ajio', 'ajio.com', r'/p/\d+'),
    ('farfetch', 'farfetch.com', r'-item-\d+'),
    ('mytheresa', 'mytheresa.com', r'-p\d{8}'),
    ('ssense', 'ssense.com', r'/product/'),
    ('revolve', 'revolve.com', r'/dp/'),
    ('nordstrom', 'nordstrom.com', r'/s/'),
    ('nike', 'nike.com', r'/t/'),
    ('gucci', 'gucci.com', r'/pr/|/p/'),
]
# product-page shapes that work across most stores (Shopify, Salesforce, big marketplaces, luxury houses)
GENERIC_PRODUCT = re.compile(
    r'/(p|pd|pr|prd|product|products|dp|item|buy|t|s|shop/product)/'
    r'|productpage|-item-\d+|-p\d{5,}|/p-mp\d+|prod\d{5,}|/\d{5,}/buy'
    r'|[-_/]\d{6,}(\.html)?($|[/?])'
    r'|/[^/?]*\d[^/?]*\.html($|\?)', re.I)
NOT_PRODUCT = re.compile(
    r'/search|[?&](q|query|searchterm|text|k)=|/category|/categories|/collections/?($|\?)|/c/|/blog'
    r'|/stories/|/editorial|/help|/customer-service|/store-locator|/careers', re.I)
NOT_BRAND_SITES = re.compile(
    r'(wikipedia|instagram|facebook|youtube|pinterest|linkedin|twitter|x\.com|tiktok|reddit|amazon|ebay'
    r'|flipkart|vogue|elle|harpersbazaar|glamour|businessoffashion|lyst|google)\.', re.I)

_cache = OrderedDict()
_cache_lock = __import__('threading').Lock()   # the server handles several requests at once
_domains = {}
_hits = {}
PER_IP = 200             # pieces searched per 10 minutes per visitor (pieces already found don't count)
MAX_SEARCHES = 7         # searches one piece may use (each is a Serper credit and a second or two)
RETRY_LEFT = 4           # a second wording on the same site only while this many searches are left for the other steps
DEADLINE = 25            # seconds /api/shop-links waits; slower pieces keep searching and are collected on the next call
MAX_ITEMS = 12


def _provider():
    if os.environ.get('SERPER_API_KEY'):
        return 'serper'
    if os.environ.get('GOOGLE_API_KEY') and os.environ.get('GOOGLE_CX'):
        return 'google'
    return None


_seen = threading.local()      # every result one product lookup saw (exact product names for the Google fallback)


def search_web(query, gl='in', num=10):
    """[(title, link)] for a Google web search in country `gl`, via Serper or the old Custom Search API.
    Inside one product lookup: at most MAX_SEARCHES searches, and a search that fails is remembered (so the app
    can say "couldn't check" instead of "not sold online")."""
    budget = getattr(_seen, 'budget', None)
    if budget is not None:
        if budget <= 0:
            return []
        _seen.budget = budget - 1
    out, worked = _search_web(query, gl, num)
    if not worked and getattr(_seen, 'results', None) is not None:
        _seen.failed = True
    seen = getattr(_seen, 'results', None)
    if seen is not None:
        seen.extend(out)
    return out


def _search_web(query, gl='in', num=10):
    """(results, whether the search itself worked)."""
    p = _provider()
    try:
        if p == 'serper':
            r = req.post('https://google.serper.dev/search', timeout=8,
                         headers={'X-API-KEY': os.environ['SERPER_API_KEY'], 'Content-Type': 'application/json'},
                         json={'q': query, 'gl': gl, 'num': num})
            search_result(r)
            if r.status_code != 200:        # credits used up, a refused key, Serper down
                print(f'[shop] search refused: {r.status_code} {r.text[:120]!r}')
                return [], False
            return [(o.get('title', ''), o.get('link', '')) for o in (r.json().get('organic') or [])], True
        if p == 'google':
            r = req.get('https://www.googleapis.com/customsearch/v1', timeout=8, params={
                'key': os.environ['GOOGLE_API_KEY'], 'cx': os.environ['GOOGLE_CX'],
                'q': query, 'num': min(num, 10), 'gl': gl})
            if r.status_code != 200:
                return [], False
            return [(o.get('title', ''), o.get('link', '')) for o in (r.json().get('items') or [])], True
    except (req.RequestException, ValueError) as e:
        print('[shop] search failed:', e)
        return [], False
    return [], False


def find_image(query, gl='in'):
    """One product photo URL, or None."""
    p = _provider()
    try:
        if p == 'serper':
            r = req.post('https://google.serper.dev/images', timeout=6,
                         headers={'X-API-KEY': os.environ['SERPER_API_KEY'], 'Content-Type': 'application/json'},
                         json={'q': query, 'gl': gl, 'num': 10})
            search_result(r)
            for im in r.json().get('images') or []:
                url = im.get('imageUrl') or ''
                if url.startswith('https://') and not url.lower().endswith('.svg'):
                    return url
            return None
        if p == 'google':
            r = req.get('https://www.googleapis.com/customsearch/v1', timeout=5, params={
                'key': os.environ['GOOGLE_API_KEY'], 'cx': os.environ['GOOGLE_CX'], 'q': query,
                'searchType': 'image', 'num': 1, 'imgSize': 'medium', 'safe': 'active', 'gl': gl})
            items = r.json().get('items') or []
            return items[0]['link'] if items else None
    except (req.RequestException, ValueError, KeyError) as e:
        print('[shop] image search failed:', e)
    return None


def _clean_title(title):
    # "LINEN SHIRT - Ecru | ZARA India" -> "LINEN SHIRT"; "Rust Anarkali Set - Biba" -> "Rust Anarkali Set"
    t = re.split(r'\s+[|–—]\s+|\s+-\s+(?:Buy|Shop|Online)\b', title or '')[0].strip()
    head, sep, tail = t.rpartition(' - ')
    if sep and head and len(tail.split()) <= 3:
        t = head.strip()
    return (t[:70] + '…') if len(t) > 71 else t


def product_name(title, link, brand=''):
    """The product's own name for the card: 'ZIMMERMANN Momentum corset floral-print linen and silk-...' ->
    'Momentum corset floral print linen and silk blend crepe de chine mini dress' (rebuilt from the web address
    when Google cut the title off), without the brand in front."""
    t = re.split(r'\s+[|–—]\s+|\s+-\s+(?:Buy|Shop|Online)\b', htmllib.unescape(title or ''))[0].strip()
    head, sep, tail = t.rpartition(' - ')
    if sep and head and len(tail.split()) <= 3:
        t = head.strip()
    cut = bool(re.search(r'(\.\.\.|…)\s*$', t))
    t = re.sub(r'[\s,;:-]*(\.\.\.|…)\s*$', '', t).strip()
    bw = len((brand or '').split())
    if brand and _norm(' '.join(t.split()[:bw])) == _norm(brand):
        t = ' '.join(t.split()[bw:])
    if cut:
        start = [w for w in re.findall(r'[a-z0-9]+', t.lower())][:3]
        for seg in sorted(urlparse(link or '').path.split('/'), key=lambda x: -x.count('-')):
            words = [w for w in re.split(r'[-_]+', seg.lower()) if w and not re.search(r'\d', w)]
            if start and words[:len(start)] == start and len(words) > len(start):
                t = ' '.join(words)
                t = t[:1].upper() + t[1:]
                break
    t = t.strip(' -–—:|,')
    return t if len(t) <= 90 else t[:88].rsplit(' ', 1)[0] + '…'


def _host(url):
    host = urlparse(url if '//' in url else '//' + url).netloc.lower().split(':')[0]
    return host[4:] if host.startswith('www.') else host


_LETTERS = str.maketrans({'ø': 'o', 'æ': 'ae', 'œ': 'oe', 'ß': 'ss', 'ł': 'l', 'đ': 'd', '°': ''})


def _ascii(text, german=False):
    t = (text or '').lower().translate(_LETTERS)
    if german:   # Glashütte -> glashuette, Söhne -> soehne
        t = t.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')
    return unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode()


def _keys(brand):
    """Every way a brand's name can appear in its web address: Hermès -> hermes, Dolce & Gabbana ->
    dolcegabbana / dolceandgabbana, Zadig & Voltaire -> zadigetvoltaire, A. Lange & Söhne -> alangesoehne."""
    keys = set()
    for german in (False, True):
        t = _ascii(brand, german)
        for amp in ('and', 'et', ''):
            k = re.sub(r'[^a-z0-9]', '', t.replace('&', amp))
            if k:
                keys.add(k)
    return keys


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', _ascii(text).replace('&', 'and'))


def _label_matches(label, keys, min_len=3):
    label = re.sub(r'[^a-z0-9]', '', label)
    return any(label == k or (len(label) >= min_len and label in k) or (len(k) >= min_len and k in label) for k in keys)


def _registrable(host):
    """'www2.hm.com' -> 'hm.com'; 'shop.mango.com' -> 'mango.com'; 'www.theiconic.com.au' -> 'theiconic.com.au'."""
    parts = host.split('.')
    if len(parts) >= 3 and parts[-2] in ('co', 'com', 'net', 'org', 'ac') and len(parts[-1]) == 2:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:])


def _core(domain):
    return _registrable(domain).split('.')[0]


def _region_hint(cc):
    cc = cc.lower()
    extra = {'gb': '|/uk/|en-uk|\\.co\\.uk', 'in': '|\\.in/|india', 'ae': '|uae', 'us': '|en-us|/en_us'}.get(cc, '')
    return re.compile(rf'(?:/|[-_.]){cc}(?:[/_-]|$)|[-_]{cc}\.html' + extra, re.I)


# ── Is this search result the same item? ─────────────────────────────────────
STOP = {'with', 'and', 'the', 'in', 'for', 'a', 'an', 'of', 'on', 'to', 'by', 'from', 'women', 'woman', 'womens', 'men', 'man',
        'mens', 'buy', 'online', 'shop', 'new', 'india', 'html', 'www', 'com', 'product', 'products', 'p', 'item', 'detail'}
SYN = {'trainer': 'sneaker', 'pant': 'trouser', 'tshirt': 'tee', 'kurti': 'kurta', 'sari': 'saree', 'jumper': 'sweater',
       'pullover': 'sweater', 'denim': 'jean', 'pump': 'heel', 'stiletto': 'heel', 'handbag': 'bag', 'eyewear': 'sunglass',
       'bootie': 'boot', 'playsuit': 'jumpsuit', 'romper': 'jumpsuit', 'camisole': 'cami', 'parfum': 'perfume', 'fragrance': 'perfume'}
# words that say what kind of thing it is; a result must be the same kind
TYPE_FAMILIES = [
    {'dress', 'gown'}, {'shirt', 'overshirt'}, {'top', 'blouse', 'tee', 'tank', 'cami', 'bodysuit', 'corset', 'bustier', 'crop'},
    {'trouser', 'chino', 'jogger', 'legging', 'palazzo', 'culotte'}, {'jean'}, {'skirt'}, {'short'},
    {'blazer', 'jacket', 'bomber', 'gilet'}, {'coat', 'trench', 'overcoat', 'parka', 'puffer'},
    {'sweater', 'cardigan', 'sweatshirt', 'hoodie', 'knit'}, {'kurta', 'tunic'}, {'saree'}, {'lehenga'}, {'anarkali'},
    {'dupatta', 'stole', 'scarf', 'shawl'}, {'sherwani'}, {'bandhgala', 'nehru'}, {'suit', 'tuxedo'}, {'waistcoat', 'vest'},
    {'coord', 'set'}, {'jumpsuit'}, {'sneaker', 'shoe'}, {'heel', 'sandal', 'slingback'}, {'boot'}, {'loafer', 'moccasin'},
    {'flat', 'ballerina', 'ballet'}, {'mule', 'clog', 'slide'}, {'juttis', 'jutti', 'mojari', 'kolhapuri'},
    {'bag', 'tote', 'clutch', 'pouch', 'crossbody', 'satchel', 'hobo', 'baguette', 'backpack'}, {'belt'}, {'watch'},
    {'earring', 'hoop', 'stud', 'jhumka'}, {'necklace', 'pendant', 'chain', 'choker'}, {'ring'},
    {'bracelet', 'bangle', 'cuff', 'kada'}, {'sunglass'}, {'cap', 'hat', 'beanie', 'beret'}, {'perfume', 'eau', 'edp', 'edt'},
    {'polo'}, {'bra', 'bralette', 'lingerie'},
]
# what a piece is made of: with what kind of piece it is, that's what a plain description gets searched by
MATERIALS = {'linen', 'silk', 'satin', 'leather', 'suede', 'cashmere', 'wool', 'cotton', 'jean', 'tweed', 'velvet', 'lace',
             'organza', 'crepe', 'chiffon', 'knit', 'crochet', 'sequin', 'mesh', 'poplin', 'jersey', 'corduroy', 'boucle',
             'raffia', 'canvas', 'nylon', 'tulle', 'georgette', 'chanderi', 'brocade', 'metallic', 'pearl', 'gold', 'silver',
             'diamond', 'straw', 'patent', 'shearling', 'merino', 'mohair', 'taffeta', 'lurex', 'gingham', 'seersucker'}
_TYPE_WORDS = set().union(*TYPE_FAMILIES)
_SLEEVES = re.compile(r'\b(short|long|cap|puff|half)[\s-]+sleeved?\b', re.I)


def key_words(name):
    """'Relaxed linen-blend short-sleeve overshirt in pale ecru' -> 'linen overshirt': what it's made of and what it
    is, the words a store's own product name will share."""
    keep, seen = [], set()
    for w in re.findall(r'[A-Za-z0-9]+', _SLEEVES.sub(' ', name or '')):
        st = _stem(_ascii(w))
        if (st in MATERIALS or st in _TYPE_WORDS) and st not in seen:
            seen.add(st)
            keep.append(w)
    return ' '.join(keep[:4])


def _descriptive(name):
    return len(re.findall(r'[A-Za-z0-9]+', name or '')) >= 5


MULTI_BRAND = {'myntra.com', 'ajio.com', 'nykaafashion.com', 'nykaa.com', 'tatacliq.com', 'azafashions.com', 'perniaspopupshop.com',
               'farfetch.com', 'net-a-porter.com', 'mrporter.com', 'mytheresa.com', 'ssense.com', 'revolve.com', 'nordstrom.com',
               'saksfifthavenue.com', 'shopbop.com', 'selfridges.com', 'harrods.com', 'asos.com', 'zalando.com', 'aboutyou.com',
               'namshi.com', 'ounass.ae', '6thstreet.com', 'levelshoes.com', 'theiconic.com.au', 'davidjones.com', 'myer.com.au',
               'zalora.sg', 'johnlewis.com', 'simons.ca', 'holtrenfrew.com', 'thebay.com', 'bergdorfgoodman.com',
               'neimanmarcus.com', 'luisaviaroma.com', 'matchesfashion.com', 'amazon.in', 'amazon.com', 'flipkart.com'}


def _stem(w):
    if len(w) > 4 and w.endswith('sses'):
        w = w[:-2]
    elif len(w) > 4 and w.endswith('ies'):
        w = w[:-3] + 'y'
    elif len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        w = w[:-1]
    return SYN.get(w, w)


def _tokens(text):
    t = _ascii(text)
    t = re.sub(r'\bco[\s-]?ords?\b', 'coord', t)
    t = re.sub(r'\bt[\s-]?shirts?\b', 'tee', t)
    t = re.sub(r'\bsun[\s-]?glasses\b', 'sunglass', t)
    return {_stem(w) for w in re.findall(r'[a-z0-9]+', t) if len(w) > 1 and w not in STOP and not w.isdigit()}


def _families(tokens):
    return [i for i, fam in enumerate(TYPE_FAMILIES) if tokens & fam]


# A different kind of thing even though the words overlap: a mini dress for a gown, a bag charm for a bag.
_LENGTH_FAMILY = next((i for i, f in enumerate(TYPE_FAMILIES) if 'dress' in f), None)
_MINI = {'mini'}
_LONGER = {'midi', 'maxi', 'gown', 'floor', 'column', 'long'}
_PARTS = re.compile(r'\b(charms?|key ?rings?|key ?chains?|key ?fobs?|bag ?straps?|shoulder ?straps?|watch ?straps?|pouch(?:es)?|wallets?'
                    r'|card ?holders?|cases?|dust ?bags?|stickers?|trinkets?|candles?|perfumes?|fragrances?|eau de|gift ?cards?'
                    r'|lipsticks?|refills?|shoe ?laces?|insoles?|hangers?|swatch(?:es)?)\b', re.I)
_COLOUR_GROUPS = [{'ivory', 'cream', 'ecru', 'white', 'offwhite', 'off'}, {'tan', 'camel', 'cognac', 'brown', 'chocolate', 'caramel'},
                  {'navy', 'blue', 'cobalt'}, {'champagne', 'gold', 'beige', 'nude', 'sand'}, {'silver', 'grey', 'gray', 'pewter'},
                  {'black', 'noir'}, {'pink', 'blush', 'rose'}, {'red', 'burgundy', 'scarlet', 'wine'}, {'green', 'olive', 'sage', 'emerald'},
                  {'yellow', 'mustard'}, {'orange', 'rust'}, {'lilac', 'purple', 'lavender'}]


def _colours(text):
    words = set(re.findall(r'[a-z]+', (text or '').lower()))
    return {i for i, g in enumerate(_COLOUR_GROUPS) if g & words}


def _conflicts(name, text):
    """Why a result is a different piece despite sharing words (a mini dress for a gown, a key ring for a bag)."""
    want = _tokens(_SLEEVES.sub(' ', name or ''))
    got = _tokens(_SLEEVES.sub(' ', text or ''))
    if _LENGTH_FAMILY is not None and TYPE_FAMILIES[_LENGTH_FAMILY] & want:
        if (want & _MINI and got & _LONGER - {'long'}) or (want & _LONGER and got & _MINI):
            return 'length'
    if _PARTS.search(text or '') and not _PARTS.search(name or ''):
        return 'part'
    return None


def relevance(name, brand, title, link, multi_brand=False):
    """0..1 for how well a result matches the item, or None if it's clearly a different thing."""
    want = _tokens(_SLEEVES.sub(' ', name or '')) - _tokens(brand)     # "short-sleeve" doesn't make it shorts
    got = _tokens(title) | _tokens(urlparse(link).path.replace('-', ' ').replace('_', ' ').replace('/', ' '))
    fams = _families(want)
    if fams and not any(TYPE_FAMILIES[i] & got for i in fams):
        return None                      # asked for a co-ord set, this is a shirt
    path_words = urlparse(link).path.replace('-', ' ').replace('_', ' ').replace('/', ' ')
    if _conflicts(name, f'{title} {path_words}'):
        return None                      # a mini dress for a gown, a bag charm for a bag
    if multi_brand and brand:
        bt = {w for w in _tokens(brand) if len(w) > 2} or _tokens(brand)
        if bt and not (bt & got) and _norm(brand) not in _norm(title + link):
            return None                  # on Myntra/Farfetch, it must be the right brand
    if not want:
        return 0.5
    overlap = len(want & got) / len(want)
    if overlap >= 0.33 or (len(want) <= 2 and overlap > 0):
        return overlap
    # A plain description ("relaxed linen-blend overshirt in pale ecru") rarely shares the store's own words: the same
    # kind of piece, of that brand, in the same material is a real product that fits it (ranked below closer matches).
    # The kind of piece must match; then at least half of what it's made of.
    core = _tokens(key_words(name)) - _tokens(brand)
    mats = {w for w in core if w in MATERIALS}
    kinds = core - mats
    if fams and len(want) >= 4 and kinds:
        kind_hit = any(w in got or any(w in f and f & got for f in TYPE_FAMILIES) for w in kinds)
        score = (kind_hit + len(mats & got)) / (1 + len(mats))
        want_c, got_c = _colours(name), _colours(f'{title} {path_words}')
        if want_c and got_c and not want_c & got_c:
            return None                  # only a loose match, and another colour: not this piece
        if kind_hit and score >= 0.5:
            return 0.1 + 0.2 * score
    return None


def _is_product_url(link, pattern=None):
    host = _host(link)
    path = link.split(host, 1)[-1]
    if NOT_PRODUCT.search(link):
        return False
    if pattern:                          # a site we know: only its real product-page shape counts
        return bool(re.search(pattern, path, re.I))
    return bool(GENERIC_PRODUCT.search(path))


def _rank(results, name, brand, domain=None, pattern=None, cc='in', trace=None):
    """Product pages from these results, best match first."""
    hint = _region_hint(cc)
    ranked = []
    for pos, (title, link) in enumerate(results):
        why = None
        host = _host(link) if link.startswith('http') else ''
        if not host:
            why = 'not a web page'
        elif domain and not (host == domain or host.endswith('.' + domain)):
            why = 'other site'
        elif not _is_product_url(link, pattern):
            why = 'not a product page'
        else:
            multi = _registrable(host) in MULTI_BRAND and _norm(brand) not in _norm(_registrable(host))
            score = relevance(name, brand, title, link, multi)
            if score is None:
                why = 'different item' + (' or brand' if multi else '')
            else:
                total = score + (0.15 if hint.search(link) else 0) + max(0, 0.05 - pos * 0.005)
                ranked.append((total, title, link))
                why = f'match {score:.2f}'
        if trace is not None:
            trace.append({'title': title, 'link': link, 'result': why})
    ranked.sort(key=lambda r: -r[0])
    return [(t, l) for _, t, l in ranked]


# ── Does the page still exist? (sold-out products often redirect to a category or the home page) ──
UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/126.0.0.0 Mobile Safari/537.36')


def _meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)', html, re.I) or \
        re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\']', html, re.I)
    return m.group(1).strip() if m else None


def _safe_host(host):
    return bool(host) and '.' in host and not re.match(r'^[\d.]+$|^\[|localhost|\.local$|\.internal$', host)


def check_page(url, pattern=None, cc='in'):
    """{'state': 'ok'|'dead'|'unknown', ...}. Shops that block robots answer 403: that's 'unknown', not dead."""
    if not url.startswith('https://') or not _safe_host(_host(url)):
        return {'state': 'dead'}
    try:
        r = req.get(url, timeout=6, allow_redirects=True, stream=True, headers={
            'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': f'en-{cc.upper()},en;q=0.9'})
        status, final = r.status_code, r.url
        if status in (404, 410):
            return {'state': 'dead', 'status': status}
        if status != 200:
            return {'state': 'unknown', 'status': status}
        fpath = urlparse(final).path.rstrip('/')
        if final.split('?')[0].rstrip('/') != url.split('?')[0].rstrip('/') and (not fpath or not _is_product_url(final, pattern)):
            return {'state': 'dead', 'status': status, 'final': final}
        html, size = '', 0
        for chunk in r.iter_content(65536, decode_unicode=False):
            html += chunk.decode('utf-8', 'ignore')
            size += len(chunk)
            if size > 600000:
                break
        r.close()
        avail = [a.lower() for a in re.findall(r'"availability"\s*:\s*"(?:https?://schema\.org/)?([A-Za-z]+)"', html)]
        og_avail = (_meta(html, 'product:availability') or _meta(html, 'og:availability') or '').lower()
        sold_out = (bool(avail) and not any(a in ('instock', 'limitedavailability', 'preorder', 'onlineonly') for a in avail)) \
            or og_avail in ('out of stock', 'oos', 'outofstock')
        image = _meta(html, 'og:image') or _meta(html, 'twitter:image')
        return {'state': 'ok', 'final': final, 'title': _meta(html, 'og:title'),
                'image': image if image and image.startswith('https://') else None, 'sold_out': sold_out}
    except (req.RequestException, UnicodeError) as e:
        return {'state': 'unknown', 'error': type(e).__name__}


def _choose(ranked, pattern, cc, trace=None, budget=3):
    """First ranked page that still exists and is in stock; else the best one that might be."""
    fallback = None
    for title, link in ranked[:budget]:
        c = check_page(link, pattern, cc)
        if trace is not None:
            trace.append({'check': link, **{k: v for k, v in c.items() if k != 'image'}})
        if c['state'] == 'dead':
            continue
        found = {'url': c.get('final') or link, 'title': _clean_title(c.get('title') or title), 'raw': c.get('title') or title}
        if c.get('image'):
            found['image'] = c['image']
        if c['state'] == 'ok' and c.get('sold_out'):
            found['sold_out'] = True
            fallback = fallback or found
            continue
        return found
    return fallback


_OVERRIDES = {_norm(k): v for k, v in SITE_OVERRIDES.items()}
_SUFFIXES = {'india', 'in', 'man', 'men', 'mens', 'women', 'womens', 'woman', 'kids', 'home', 'design', 'us', 'usa', 'uk', 'online', 'store'}


def known_site(name):
    """(domain, product-page pattern) for brands we know, else None.
    'Zara India' and 'H&M Men' match Zara and H&M; 'Raw Mango' does not match Mango."""
    n = _norm(name)
    if n in _OVERRIDES:
        domain = _OVERRIDES[n]
        pattern = next((p for kw, d, p in BRAND_SITES if d == domain), None)
        return domain, pattern
    b = _ascii(name)
    for kw, domain, pattern in BRAND_SITES:
        m = re.match(r'\s*' + re.escape(kw) + r'(?=[^a-z0-9]|$)(.*)$', b)
        if m and set(re.findall(r'[a-z0-9]+', m.group(1))) <= _SUFFIXES:
            return domain, pattern
    return None


def official_domain(brand, gl):
    """Find a brand's own website by searching for it once (cached): works for any brand."""
    key = _norm(brand)
    if len(key) < 2:
        return None
    if key in _domains:
        return _domains[key]
    keys = _keys(brand)
    found = None
    for _, link in search_web(f'{brand} official website', gl, num=8):
        host = _host(link)
        if not host or NOT_BRAND_SITES.search(host):
            continue
        registrable = _registrable(host)
        if _label_matches(registrable.split('.')[0], keys):
            found = registrable
            break
        # the brand lives on a parent company's site: allensolly.abfrl.in, oldnavy.gap.com, gshock.casio.com
        subs = host[: -len(registrable)].strip('.').split('.') if host != registrable else []
        if any(_label_matches(lab, keys, min_len=4) for lab in subs if lab not in ('www', 'www2', 'shop', 'store', 'usa', 'us', 'uk', 'eu', 'en', 'in')):
            found = host
            break
    _domains[key] = found
    if len(_domains) > 3000:
        _domains.clear()
    return found


def _store_domain(store):
    s = (store or '').strip().lower()
    if '.' in s and ' ' not in s:
        host = _host(s)
        return _registrable(host) if host and '.' in host else None
    return None


def _site_search(domain, pattern, name, brand, gl, retry=True, trace=None):
    multi = domain in MULTI_BRAND and _norm(brand) not in _norm(domain)
    q = f'site:{domain} {brand + " " if multi else ""}{name}'
    step = [] if trace is not None else None
    ranked = _rank(search_web(q, gl), name, brand, domain, pattern, gl, step)
    if trace is not None:
        trace.append({'query': q, 'results': step})
    left = getattr(_seen, 'budget', None)
    if not ranked and (retry or _descriptive(name)) and (left is None or left > RETRY_LEFT):   # try the key words only
        words = key_words(name) if _descriptive(name) else ''
        words = words or ' '.join(re.findall(r'[A-Za-z0-9]+', name)[:4])
        if words and words.lower() != name.lower():
            q = f'site:{domain} {brand + " " if multi else ""}{words}'
            step = [] if trace is not None else None
            ranked = _rank(search_web(q, gl), name, brand, domain, pattern, gl, step)
            if trace is not None:
                trace.append({'query': q, 'results': step})
    return _choose(ranked, pattern, gl, trace) if ranked else None


CACHE_SECONDS = 12 * 3600


# not shops: never a "direct link"
NOT_SHOPS = re.compile(r'(wikipedia|instagram|facebook|youtube|pinterest|linkedin|twitter|x\.com|tiktok|reddit|vogue|elle'
                       r'|harpersbazaar|glamour|businessoffashion|whowhatwear|google|lyst|quora|medium)\.', re.I)
_COLOUR_WORDS = re.compile(r'\b(black|white|ivory|cream|ecru|beige|tan|camel|brown|chocolate|grey|gray|silver|gold|champagne|'
                           r'pink|blush|red|burgundy|navy|blue|green|olive|sage|emerald|yellow|orange|lilac|purple|nude)\b', re.I)


def _branded(brand, title, link):
    """The brand's name is on the result (its title or its web address)."""
    t, u = _norm(title), _norm(urlparse(link).netloc + urlparse(link).path)
    return any(len(k) >= 3 and (k in t or k in u) for k in _keys(brand))


def _open_search(brand, name, gl, trace=None):
    """Anywhere on the web that sells it: a product page from any shop, with the brand's name on it."""
    if not brand:
        return None
    q = f'{brand} {(key_words(name) if _descriptive(name) else "") or name}'
    results = [(t, l) for t, l in search_web(q, gl) if l.startswith('https://') and not NOT_SHOPS.search(_host(l))
               and _branded(brand, t, l)]
    step = [] if trace is not None else None
    ranked = _rank(results, name, brand, cc=gl, trace=step)
    if trace is not None:
        trace.append({'query': q, 'anywhere': True, 'results': step})
    return _choose(ranked, None, gl, trace) if ranked else None


def _same_kind(brand, name, gl, home, trace=None):
    """Nothing sold under Ella's description: the closest real piece from that brand, the same kind of thing
    (a top for a top), the same colour first."""
    kinds = [w for w in key_words(name).split() if _stem(_ascii(w)) in _TYPE_WORDS]
    if not brand or not kinds:
        return None
    kind = kinds[-1]
    fam = set(_families({_stem(_ascii(kind))}))
    q = f'site:{home} {kind}' if home else f'{brand} {kind}'
    pattern = (known_site(brand) or (None, None))[1] if home else None
    colours = {c.lower() for c in _COLOUR_WORDS.findall(name)}
    ranked = []
    for pos, (t, l) in enumerate(search_web(q, gl)):
        host = _host(l) if l.startswith('https://') else ''
        path_words = urlparse(l).path.replace('-', ' ').replace('_', ' ').replace('/', ' ')
        same_site = bool(home) and (host == home or host.endswith('.' + home))
        want_c, got_c = _colours(name), _colours(f'{t} {path_words}')
        if (host and not NOT_SHOPS.search(host) and _is_product_url(l, pattern if same_site else None)
                and set(_families(_tokens(t) | _tokens(path_words))) & fam and (same_site or _branded(brand, t, l))
                and not _conflicts(name, f'{t} {path_words}') and not (want_c and got_c and not want_c & got_c)):
            ranked.append((-(len(colours & {c.lower() for c in _COLOUR_WORDS.findall(t + ' ' + path_words)})), pos, t, l))
        if trace is not None:
            trace.append({'closest_kind': kind, 'title': t, 'link': l})
    ranked.sort()
    found = _choose([(t, l) for _, _, t, l in ranked], pattern, gl, trace) if ranked else None
    if found:
        found['closest'] = True
    return found


_EDITORIAL = re.compile(r'(wikipedia|instagram|facebook|youtube|linkedin|twitter|x\.com|tiktok|reddit|quora|medium|vogue|elle'
                        r'|harpersbazaar|glamour|businessoffashion|whowhatwear|hellomagazine|marieclaire)\.', re.I)


def exact_name(title, brand):
    """'THE ROW Faustino silk satin top | NET-A-PORTER' -> 'Faustino silk satin top' (the product's own name)."""
    t = _clean_title(title).rstrip('…').strip()
    t = re.sub(r'\s+(?:by|from)\s+' + re.escape(brand) + r'\b.*$', '', t, flags=re.I) if brand else t
    words, bw = t.split(), len(brand.split())
    if brand and _norm(' '.join(words[:bw])) == _norm(brand):
        t = ' '.join(words[bw:])
    t = t.strip(' -–—:|,')
    return t if len(t) >= 4 and len(t.split()) >= 2 else ''


def _harvest(results, brand, name):
    """A real product's exact name from what the searches saw, even where no product page could be linked
    (a page that's gone, a shop that blocks us, a listing on Pinterest or Lyst)."""
    best = None
    for t, l in results:
        host = _host(l) if l.startswith('http') else ''
        if not t or not host or _EDITORIAL.search(host) or NOT_PRODUCT.search(l) or not _branded(brand, t, l):
            continue
        score = relevance(name, brand, t, l)
        n = exact_name(t, brand)
        if score is not None and n and (best is None or score > best[0]):
            best = (score, n)
    return best[1] if best else ''


def google_link(brand, name):
    return 'https://www.google.com/search?q=' + quote_plus(f'{brand} {name}'.strip())


def _cache_key(brand, name, country, store, closest):
    return f'{(country or "IN").lower()}|{(brand or "").strip().lower()}|{(name or "").strip().lower()}|{(store or "").lower()}|{int(bool(closest))}'


def _cached(key):
    with _cache_lock:
        hit = _cache.get(key)
    return bool(hit) and time.time() - hit[0] < CACHE_SECONDS


def resolve(brand, name, country='IN', store=None, trace=None, closest=True):
    _seen.results, _seen.budget, _seen.failed = [], MAX_SEARCHES, False
    try:
        return _resolve(brand, name, country, store, trace, closest)
    finally:
        _seen.results = _seen.budget = None


def _resolve(brand, name, country='IN', store=None, trace=None, closest=True):
    """{'url', 'title', ['image', 'sold_out', 'closest']}: the product page itself (in `country`), else the closest real
    piece of the same kind from that brand (closest: True; never for a muse's own piece).
    If no product page can be linked: {'url': None, 'name': the real product's exact name if any search saw one,
    'google': a Google search for brand + that exact name, 'site', 'site_url'}."""
    brand = (brand or '').strip()
    name = (name or '').strip()
    if not name:
        return None
    gl = (country or 'IN').lower()
    key = _cache_key(brand, name, country, store, closest)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and trace is None and time.time() - hit[0] < CACHE_SECONDS:
            _cache.move_to_end(key)
            return hit[1]

    tried, found, home = set(), None, None
    candidates = []
    sd = _store_domain(store)
    if sd:
        candidates.append((sd, (known_site(sd) or (None, None))[1]))
    elif store and known_site(store):
        candidates.append(known_site(store))
    ks = known_site(brand)
    if ks:
        candidates.append(ks)
    elif brand:
        od = official_domain(brand, gl)
        if od:
            candidates.append((od, (known_site(od) or (None, None))[1]))
    if trace is not None:
        trace.append({'sites_to_try': [c[0] for c in candidates]})
    for i, (domain, pattern) in enumerate(candidates):
        if domain in tried:
            continue
        tried.add(domain)
        if domain not in MULTI_BRAND:
            home = home or domain
        found = _site_search(domain, pattern, name, brand, gl, retry=(i == len(candidates) - 1), trace=trace)
        if found:
            break
    if not found:   # the big stores that deliver in this country, brand required
        stores = [d for d in retailers_for(country) if d not in tried][:8]
        if stores:
            q = f'{brand} {(key_words(name) if _descriptive(name) else "") or name} (' + ' OR '.join(f'site:{d}' for d in stores) + ')'
            step = [] if trace is not None else None
            ranked = _rank(search_web(q, gl), name, brand, cc=gl, trace=step)
            if trace is not None:
                trace.append({'query': q, 'results': step})
            found = _choose(ranked, None, gl, trace) if ranked else None
    if not found:   # any shop on the web that sells it under the brand's name
        found = _open_search(brand, name, gl, trace)
    if not found and closest:   # nothing sold as Ella described it: the closest real piece from that brand
        found = _same_kind(brand, name, gl, home, trace)
    if not found:   # nothing to link: the exact name, for a Google search (a muse's own piece keeps its own name)
        site = home or sd
        exact = _harvest(getattr(_seen, 'results', None) or [], brand, name) if closest else ''
        found = {'url': None, 'name': exact, 'google': google_link(brand, exact or name),
                 'site': site, 'site_url': f'https://{"www." if site and site.count(".") == 1 else ""}{site}' if site else None}
        if getattr(_seen, 'failed', False) and not exact:
            found['unavailable'] = True     # the shops couldn't be searched: not "not sold online", and not cached
        if trace is not None:
            trace.append({'no_product_page': True, 'exact_name': exact, 'search_failed': bool(getattr(_seen, 'failed', False))})
    if found.get('url'):            # the card shows the product's own full name
        found['title'] = product_name(found.pop('raw', '') or found.get('title') or '', found['url'], brand) or found.get('title')
    found.pop('raw', None)
    if found.get('unavailable'):    # try the shops again next time
        return found
    with _cache_lock:
        _cache[key] = (time.time(), found)
        _cache.move_to_end(key)
        while len(_cache) > 2000:
            _cache.popitem(last=False)
    return found


# ── A dupe, for a piece that can't be bought (sold out, not sold online, or only a Google search) ──
# High-street shops with product pages the search knows, that deliver to that country.
DUPE_STORES = {'IN': ['zara.com', 'hm.com', 'mango.com', 'myntra.com', 'ajio.com']}
DUPE_STORES_ELSEWHERE = ['zara.com', 'hm.com', 'mango.com', 'asos.com']


def dupe_words(name, description=''):
    """('Carousel Metallic Twist Gown', 'silver metallic twist-front gown') -> 'silver Metallic Gown': its colour, what
    it's made of and what it is. The kind of piece comes from its name; colour and fabric from its description if the
    name hasn't any (so "how to wear it" words in a description never change what's searched for)."""
    words = key_words(name)
    have = {_stem(_ascii(w)) for w in words.split()}
    if not have & MATERIALS:
        fabric = next((w for w in key_words(description).split() if _stem(_ascii(w)) in MATERIALS), '')
        words = f'{fabric} {words}'.strip()
    colour = next((c for c in _COLOUR_WORDS.findall(f'{name} {description}') if c.lower() not in words.lower()), '')
    return ' '.join(f'{colour.lower()} {words}'.split()) if have & _TYPE_WORDS else ''


def _dupe_key(name, country, description=''):
    return f'dupe|{(country or "IN").lower()}|{dupe_words(name, description).lower()}'


def dupe(name, country='IN', description='', trace=None):
    _seen.results, _seen.budget, _seen.failed = [], MAX_SEARCHES, False
    try:
        return _dupe(name, country, description, trace)
    finally:
        _seen.results = _seen.budget = None


def _dupe(name, country='IN', description='', trace=None):
    """{'url', 'title', 'image', 'store', 'dupe': True}: the same kind of piece, same colour first, from a high-street
    shop that delivers to `country`. {'url': None, 'none': True} if there isn't one."""
    words = dupe_words(name, description)
    if not words:
        return {'url': None, 'none': True}
    gl = (country or 'IN').lower()
    key = _dupe_key(name, country, description)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and trace is None and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    stores = DUPE_STORES.get((country or 'IN').upper(), DUPE_STORES_ELSEWHERE)
    found = None
    plain = ' '.join(_COLOUR_WORDS.sub(' ', words).split())
    for q in dict.fromkeys([words, plain]):                     # with the colour, then without it
        if not q:
            continue
        results = []
        for t, l in search_web(f'{q} (' + ' OR '.join(f'site:{d}' for d in stores) + ')', gl):
            host = _registrable(_host(l)) if l.startswith('https://') else ''
            if host in stores and _is_product_url(l, (known_site(host) or (None, None))[1]):
                results.append((t, l))
        step = [] if trace is not None else None
        ranked = _rank(results, words, '', cc=gl, trace=step)
        if trace is not None:
            trace.append({'dupe_query': q, 'results': step})
        found = _choose(ranked, None, gl, trace) if ranked else None
        if found and not found.get('sold_out'):
            break
        found = None
    if found:
        found['title'] = product_name(found.pop('raw', '') or found.get('title') or '', found['url'], '') or found.get('title')
        found.update(dupe=True, store=_registrable(_host(found['url'])))
    else:
        found = {'url': None, 'none': True}
    found.pop('raw', None)
    if getattr(_seen, 'failed', False) and not found.get('url'):
        return {'url': None, 'unavailable': True}     # the shops couldn't be searched: ask again later
    with _cache_lock:
        _cache[key] = (time.time(), found)
        _cache.move_to_end(key)
        while len(_cache) > 2000:
            _cache.popitem(last=False)
    return found


def _allowed(ip, n=1):
    """n more searches for this visitor within PER_IP per 10 minutes?"""
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) + n > PER_IP:
        return False
    q.extend([now] * n)
    return True


_runner = ThreadPoolExecutor(max_workers=12)     # product lookups outlive the request that started them
_inflight = {}
_inflight_lock = threading.RLock()


def _lookup(key, args):
    """The running (or new) lookup for one piece: asking twice never searches twice."""
    with _inflight_lock:
        f = _inflight.get(key)
        if f is None:
            f = _runner.submit((lambda: dupe(args[1], args[2], args[6])) if args[5] else (lambda: resolve(*args[:4], closest=args[4])))
            _inflight[key] = f
            f.add_done_callback(lambda _f: _forget(key, _f))
        return f


def _forget(key, f):
    with _inflight_lock:
        if _inflight.get(key) is f:
            del _inflight[key]


def country_code(body):
    cc = str(body.get('country') or '').upper()[:2]
    if len(cc) == 2 and cc.isalpha():
        return cc
    return 'US' if body.get('currency') == 'USD' else 'IN'


@shop.post('/api/shop-links')
def shop_links():
    body = request.get_json(silent=True) or {}
    items = (body.get('items') or [])[:MAX_ITEMS]
    if not items or not _provider():
        return jsonify({'links': [None] * len(items)})
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    cc = country_code(body)
    # a piece with "dupe": an affordable piece like it, from the high street (for one that can't be bought)
    args = [(str(it.get('brand', ''))[:80], str(it.get('name', ''))[:160], cc, str(it.get('store', ''))[:80],
             not it.get('worn'), bool(it.get('dupe')), str(it.get('description', ''))[:300]) for it in items]
    keys = [_dupe_key(a[1], cc, a[6]) if a[5] else _cache_key(*a[:5]) for a in args]
    with _inflight_lock:
        new = sum(1 for k in set(keys) if k not in _inflight and not _cached(k))
    if new and not owner_request() and not _allowed(ip, new):
        return jsonify({'links': [{'unavailable': True, 'google': google_link(a[0], a[1])} for a in args],
                        'error': 'slow down'}), 429
    # Answer within DEADLINE: a piece still searching comes back as {'pending': True} and keeps going here,
    # so the app's next call collects it (from the cache, or by waiting on the same search).
    jobs = [_lookup(k, a) for k, a in zip(keys, args)]
    wait(jobs, timeout=DEADLINE)
    links = []
    for f, a in zip(jobs, args):
        if not f.done():
            links.append({'pending': True})
        elif f.exception():
            print('[shop] lookup failed:', repr(f.exception()))
            links.append({'unavailable': True} if a[5] else {'unavailable': True, 'google': google_link(a[0], a[1])})
        else:
            links.append(f.result())
    return jsonify({'links': links})


@shop.get('/api/shop-check')
def shop_check():
    """Open /api/shop-check?brand=Zara&name=linen%20shirt&country=IN in a browser to see every step."""
    provider = _provider()
    args = request.args
    if not args.get('name'):
        return jsonify({'search': provider or 'none: set SERPER_API_KEY on Render, so Shop links can find products',
                        'try': '/api/shop-check?brand=Zara&name=linen%20blend%20shirt&country=IN&store=zara.com'})
    if not provider:
        return jsonify({'search': 'none: set SERPER_API_KEY on Render'}), 503
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    if not owner_request() and not _allowed(ip):
        return jsonify({'error': 'slow down'}), 429
    trace = []
    if args.get('dupe'):        # /api/shop-check?dupe=1&name=silver%20metallic%20gown&country=IN
        return jsonify({'search': provider, 'result': dupe(args.get('name', '')[:160], country_code(args), args.get('description', '')[:300], trace),
                        'steps': trace})
    result = resolve(args.get('brand', '')[:80], args.get('name', '')[:160], country_code(args), args.get('store', '')[:80], trace)
    return jsonify({'search': provider, 'result': result, 'steps': trace})
