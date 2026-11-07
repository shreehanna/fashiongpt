"""Where the shopper is, and what they can actually buy there.

The front page asks for the visitor's location (or lets them pick a country) and every
request carries `country` (ISO code) and `country_name`. Ella then only suggests brands and
stores that sell and deliver there, and shop.py looks for the product on that country's site.
The brand lists are examples per budget tier, not a whitelist: Ella can pick any brand or
store as long as it ships to that country.
"""

from brand_atlas import COUNTRY_NAMES, pool_for

REGIONS = {
    'IN': {
        'everyday': {
            'women': 'Zara India, H&M India, Mango India, Uniqlo India, Myntra, AJIO, Nykaa Fashion, Westside, Only, Vero Moda, Libas, FabIndia, Biba, W, Global Desi',
            'men': 'Zara India, H&M India, Uniqlo India, Myntra, AJIO, Snitch, Westside, Jack & Jones, Levi\'s India, Nike India, Bonkers Corner',
        },
        'midrange': {
            'women': 'Massimo Dutti India, Marks & Spencer India, Tommy Hilfiger India, Label Ritu Kumar, Anita Dongre, Nicobar, AND, Charles & Keith India, Aza Fashions, Pernia\'s Pop-Up Shop, Ogaan',
            'men': 'Massimo Dutti India, Tommy Hilfiger India, Marks & Spencer India, Rare Rabbit, Andamen, Raymond, Manyavar, Nicobar, Hugo Boss India',
        },
        'luxury': {
            'women': 'Sabyasachi, Manish Malhotra, Tarun Tahiliani, Anamika Khanna, Gucci, Dior, Louis Vuitton, Prada, Valentino, Jimmy Choo, Tata CLiQ Luxury, Farfetch, Net-a-Porter, Mytheresa (all deliver to India)',
            'men': 'Tarun Tahiliani, Kunal Rawal, Sabyasachi, Gucci, Dior, Louis Vuitton, Canali, Tata CLiQ Luxury, Farfetch, Mr Porter (deliver to India)',
        },
        'retailers': ['myntra.com', 'ajio.com', 'nykaafashion.com', 'tatacliq.com', 'azafashions.com',
                      'perniaspopupshop.com', 'farfetch.com', 'net-a-porter.com'],
    },
    'US': {
        'everyday': {
            'women': 'Zara, H&M, Uniqlo, Gap, Madewell, J.Crew, Abercrombie & Fitch, Aritzia, Old Navy, Free People, Nike',
            'men': 'Zara, H&M, Uniqlo, Gap, J.Crew, Abercrombie & Fitch, Todd Snyder, Nike, Levi\'s',
        },
        'midrange': {
            'women': 'Reformation, COS, & Other Stories, Aritzia, Everlane, Banana Republic, Vince, Sézane, Revolve, Anthropologie',
            'men': 'COS, Banana Republic, Buck Mason, Theory, Vince, Todd Snyder, Reiss, Club Monaco',
        },
        'luxury': {
            'women': 'Chanel, Prada, Gucci, Saint Laurent, Bottega Veneta, Emilio Pucci, Roberto Cavalli, Khaite, The Row, Net-a-Porter, Saks Fifth Avenue, Bergdorf Goodman, Nordstrom',
            'men': 'Prada, Gucci, Saint Laurent, Tom Ford, Brunello Cucinelli, Zegna, Mr Porter, Saks Fifth Avenue, SSENSE',
        },
        'retailers': ['nordstrom.com', 'revolve.com', 'saksfifthavenue.com', 'net-a-porter.com', 'ssense.com',
                      'shopbop.com', 'farfetch.com', 'mrporter.com'],
    },
    'GB': {
        'everyday': 'Zara, H&M, Uniqlo, ASOS, Marks & Spencer, Next, Arket, Mango, Nike',
        'midrange': 'COS, & Other Stories, Reiss, Whistles, ME+EM, Massimo Dutti, Sézane, Ted Baker',
        'luxury': 'Chanel, Prada, Gucci, Burberry, Alexander McQueen, Emilio Pucci, Roberto Cavalli, Selfridges, Harrods, Net-a-Porter, Mr Porter',
        'retailers': ['asos.com', 'selfridges.com', 'harrods.com', 'net-a-porter.com', 'mrporter.com', 'farfetch.com', 'johnlewis.com'],
    },
    'AE': {
        'everyday': 'Zara, H&M, Mango, Uniqlo, Namshi, 6thStreet, Nike',
        'midrange': 'Massimo Dutti, COS, Reiss, Ted Baker, Namshi, Ounass',
        'luxury': 'Chanel, Dior, Gucci, Prada, Valentino, Emilio Pucci, Roberto Cavalli, Ounass, Level Shoes, Net-a-Porter, Farfetch',
        'retailers': ['namshi.com', 'ounass.ae', '6thstreet.com', 'levelshoes.com', 'net-a-porter.com', 'farfetch.com'],
    },
    'CA': {
        'everyday': 'Zara, H&M, Uniqlo, Aritzia, Gap, Roots, Nike',
        'midrange': 'COS, & Other Stories, Aritzia, Reformation, Club Monaco, Simons',
        'luxury': 'Chanel, Prada, Gucci, Saint Laurent, Holt Renfrew, SSENSE, Net-a-Porter',
        'retailers': ['ssense.com', 'simons.ca', 'holtrenfrew.com', 'thebay.com', 'net-a-porter.com', 'farfetch.com'],
    },
    'AU': {
        'everyday': 'Zara, H&M, Uniqlo, Country Road, Cotton On, The Iconic, Nike',
        'midrange': 'COS, Sportscraft, Witchery, Aje, Camilla and Marc, Reformation',
        'luxury': 'Zimmermann, Chanel, Prada, Gucci, David Jones, Net-a-Porter, Mytheresa',
        'retailers': ['theiconic.com.au', 'davidjones.com', 'myer.com.au', 'net-a-porter.com', 'farfetch.com'],
    },
    'SG': {
        'everyday': 'Zara, H&M, Uniqlo, Love Bonito, Zalora, Cotton On, Nike',
        'midrange': 'COS, Massimo Dutti, Charles & Keith, Pedro, Reformation',
        'luxury': 'Chanel, Dior, Prada, Gucci, Net-a-Porter, Mytheresa, Farfetch',
        'retailers': ['zalora.sg', 'net-a-porter.com', 'mytheresa.com', 'farfetch.com'],
    },
    'EU': {
        'everyday': 'Zara, H&M, Mango, Uniqlo, Arket, Zalando, About You, Nike',
        'midrange': 'COS, & Other Stories, Massimo Dutti, Sézane, Rouje, Claudie Pierlot, Sandro, Maje',
        'luxury': 'Chanel, Prada, Gucci, Saint Laurent, Emilio Pucci, Roberto Cavalli, Mytheresa, Net-a-Porter, Farfetch',
        'retailers': ['zalando.com', 'aboutyou.com', 'mytheresa.com', 'net-a-porter.com', 'farfetch.com'],
    },
    'WORLD': {
        'everyday': 'Zara, H&M, Mango, Uniqlo, ASOS, Nike',
        'midrange': 'COS, & Other Stories, Massimo Dutti, Reformation, Sézane',
        'luxury': 'Chanel, Prada, Gucci, Emilio Pucci, Roberto Cavalli, Net-a-Porter, Farfetch, Mytheresa',
        'retailers': ['farfetch.com', 'net-a-porter.com', 'mytheresa.com', 'asos.com'],
    },
}
EU_CODES = {'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE', 'IT', 'LV',
            'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE', 'CH', 'NO'}


def group(code):
    code = (code or '').upper()
    if code in REGIONS:
        return code
    if code in EU_CODES:
        return 'EU'
    if code in ('NZ',):
        return 'AU'
    if code in ('SA', 'QA', 'KW', 'BH', 'OM'):
        return 'AE'
    return 'WORLD'


def country_of(data):
    """(code, name) from a request body; None if the app didn't send one (older page)."""
    code = str((data or {}).get('country') or '').upper()[:2]
    name = str((data or {}).get('country_name') or '').strip()[:60]
    if len(code) != 2 or not code.isalpha():
        return None
    return code, (name or code)


def brands_for(data, gender, budget):
    c = country_of(data)
    if not c:
        return None
    tiers = REGIONS[group(c[0])]
    tier = tiers.get(budget) or tiers['everyday']
    if isinstance(tier, dict):
        tier = tier['men'] if gender == 'men' else (tier['women'] + '; for him: ' + tier['men'] if gender == 'couple' else tier['women'])
    return tier


def retailers_for(code):
    return REGIONS[group(code)]['retailers']


def prompt_note(data):
    """A paragraph for every styling prompt: the Brand Atlas pool for this budget, and only what this
    person can buy and get delivered in their country."""
    budget = str((data or {}).get('budget') or 'everyday')
    c = country_of(data)
    pool = pool_for(budget, c[0] if c else None)
    atlas = ("\nBrand Atlas brands for this budget. Pick mainly from these; use another brand only if it suits "
             "the look better or a chosen muse really wears it:\n" + pool + "\n")
    if not c:
        return atlas
    code, name = c
    return atlas + (
        f"Shopper's country: {name} ({code}). Only choose pieces from brands or stores that sell and deliver to "
        f"{name} right now: the brand's own {name} website, its stores there, or a retailer that ships there "
        f"(for example Revolve, Net-a-Porter, Farfetch or Nike where they deliver). Leave out anything that isn't "
        f"sold in {name}, even if it's in the atlas. For each item, set \"store\" to the website it can be bought "
        f"from in {name}, as a bare domain (e.g. zara.com, prada.com, net-a-porter.com, myntra.com).")
