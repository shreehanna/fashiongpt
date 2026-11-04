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
import os
import re
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests as req
from flask import Blueprint, jsonify, request

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
_domains = {}
_hits = {}
PER_IP = 40              # look lookups per 10 minutes per visitor
MAX_ITEMS = 12


def _provider():
    if os.environ.get('SERPER_API_KEY'):
        return 'serper'
    if os.environ.get('GOOGLE_API_KEY') and os.environ.get('GOOGLE_CX'):
        return 'google'
    return None


def search_web(query, gl='in', num=10):
    """[(title, link)] for a Google web search in country `gl`, via Serper or the old Custom Search API."""
    p = _provider()
    try:
        if p == 'serper':
            r = req.post('https://google.serper.dev/search', timeout=8,
                         headers={'X-API-KEY': os.environ['SERPER_API_KEY'], 'Content-Type': 'application/json'},
                         json={'q': query, 'gl': gl, 'num': num})
            return [(o.get('title', ''), o.get('link', '')) for o in (r.json().get('organic') or [])]
        if p == 'google':
            r = req.get('https://www.googleapis.com/customsearch/v1', timeout=8, params={
                'key': os.environ['GOOGLE_API_KEY'], 'cx': os.environ['GOOGLE_CX'],
                'q': query, 'num': min(num, 10), 'gl': gl})
            return [(o.get('title', ''), o.get('link', '')) for o in (r.json().get('items') or [])]
    except (req.RequestException, ValueError) as e:
        print('[shop] search failed:', e)
    return []


def find_image(query, gl='in'):
    """One product photo URL, or None."""
    p = _provider()
    try:
        if p == 'serper':
            r = req.post('https://google.serper.dev/images', timeout=6,
                         headers={'X-API-KEY': os.environ['SERPER_API_KEY'], 'Content-Type': 'application/json'},
                         json={'q': query, 'gl': gl, 'num': 10})
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


def _host(url):
    host = urlparse(url if '//' in url else '//' + url).netloc.lower().split(':')[0]
    return host[4:] if host.startswith('www.') else host


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower().replace('&', 'and'))


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


def _pick(results, domain=None, pattern=None, cc='in'):
    picks = []
    for title, link in results:
        if not link.startswith('http') or NOT_PRODUCT.search(link):
            continue
        host = _host(link)
        if domain and not (host == domain or host.endswith('.' + domain)):
            continue
        path = link.split(host, 1)[-1]
        if (pattern and re.search(pattern, path, re.I)) or GENERIC_PRODUCT.search(path):
            picks.append((title, link))
    if not picks:
        return None
    hint = _region_hint(cc)
    for title, link in picks:      # e.g. zara.com/in/... for India over zara.com/us/...
        if hint.search(link):
            return {'url': link, 'title': _clean_title(title)}
    title, link = picks[0]
    return {'url': link, 'title': _clean_title(title)}


def known_site(name):
    b = (name or '').lower()
    for kw, domain, pattern in BRAND_SITES:
        if re.search(r'(^|[^a-z0-9])' + re.escape(kw) + r'([^a-z0-9]|$)', b):
            return domain, pattern
    return None


def official_domain(brand, gl):
    """Find a brand's own website by searching for it once (cached): works for any brand."""
    key = _norm(brand)
    if len(key) < 2:
        return None
    if key in _domains:
        return _domains[key]
    found = None
    for _, link in search_web(f'{brand} official website', gl, num=8):
        host = _host(link)
        if not host or NOT_BRAND_SITES.search(host):
            continue
        core = _core(host)
        if len(core) >= 3 and (core in key or key in core):
            found = _registrable(host)
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


def _site_search(domain, pattern, name, gl, retry=True):
    found = _pick(search_web(f'site:{domain} {name}', gl), domain, pattern, gl)
    if not found and retry:   # the brand's own wording often differs: try the key words only
        words = ' '.join(re.findall(r'[A-Za-z0-9]+', name)[:4])
        if words and words.lower() != name.lower():
            found = _pick(search_web(f'site:{domain} {words}', gl), domain, pattern, gl)
    return found


def resolve(brand, name, country='IN', store=None):
    """{'url', 'title'} for the closest real product page in `country`,
    or {'url': None, 'site', 'site_url'} saying where to search by hand."""
    brand = (brand or '').strip()
    name = (name or '').strip()
    if not name:
        return None
    gl = (country or 'IN').lower()
    key = f'{gl}|{brand.lower()}|{name.lower()}|{(store or "").lower()}'
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

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
    for i, (domain, pattern) in enumerate(candidates):
        if domain in tried:
            continue
        tried.add(domain)
        home = home or domain
        found = _site_search(domain, pattern, name, gl, retry=(i == len(candidates) - 1))
        if found:
            break
    if not found:   # the big stores that deliver in this country
        stores = [d for d in retailers_for(country) if d not in tried][:8]
        if stores:
            sites = ' OR '.join(f'site:{d}' for d in stores)
            found = _pick(search_web(f'{brand} {name} ({sites})', gl), cc=gl)
    if not found:
        site = home or sd
        found = {'url': None, 'site': site, 'site_url': f'https://{"www." if site and site.count(".") == 1 else ""}{site}' if site else None}
    _cache[key] = found
    if len(_cache) > 2000:
        _cache.popitem(last=False)
    return found


def _allowed(ip):
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= PER_IP:
        return False
    q.append(now)
    return True


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
    if not _allowed(ip):
        return jsonify({'links': [None] * len(items), 'error': 'slow down'}), 429
    cc = country_code(body)
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
        links = list(pool.map(lambda it: resolve(str(it.get('brand', ''))[:80], str(it.get('name', ''))[:160], cc,
                                                 str(it.get('store', ''))[:80]), items))
    return jsonify({'links': links})
