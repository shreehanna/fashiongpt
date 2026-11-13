"""The cast of E&L's drama series "The Social Representation": each character's fashion identity, as the owner wrote
it in the character fashion map. Ella dresses the user the way each one dresses, adapted to the user's occasion.

The owner can still add characters (and a note on any of these) in owner tools; daily.characters() puts them together.
"""

CAST = [
    {'name': 'Sienna Whitmore', 'gender': 'women', 'title': 'The Effortless It-Girl',
     'aesthetic': 'Manhattan socialite; effortless luxury; feminine contemporary fashion; romantic eveningwear; relaxed designer dressing',
     'colours': 'champagne, ivory, black, pale gold, soft neutrals and warm metallics',
     'wardrobe': 'elegant fitted tops; champagne and satin pieces; mini skirts; designer-inspired dresses; tailored trousers; '
                 'feminine evening dresses; relaxed luxury daywear',
     'accessories': 'gold watches; delicate gold jewellery; quilted handbags; elegant evening clutches; minimal statement accessories',
     'footwear': 'knee-high boots; elegant heels; ballet flats; designer-inspired everyday shoes',
     'personality': 'Naturally fashionable without looking overly calculated. Feminine, polished and subtly glamorous.',
     'reference': 'The Champagne Return: a champagne/pale-gold gown, elegant heels, delicate jewellery and an evening clutch'},
    {'name': 'Vivienne Sinclair', 'gender': 'women', 'title': 'The Manhattan Fashion Heiress',
     'aesthetic': 'refined Upper East Side luxury; classic feminine tailoring; polished designer dressing; Blair-inspired '
                  'sophistication; structured silhouettes',
     'colours': 'black, ivory, cream, burgundy, navy and muted pastels',
     'wardrobe': 'structured dresses; tailored blazers; elegant skirts; fine knitwear; fitted tops; sophisticated coats; '
                 'coordinated designer-inspired sets',
     'accessories': 'signature headbands; pearl jewellery; diamond-inspired earrings; structured handbags; elegant watches',
     'footwear': 'classic pumps; elegant ankle boots; refined ballet flats; sophisticated heels',
     'personality': 'Impeccably coordinated. Every accessory looks intentional. She never looks messy or accidentally dressed.',
     'reference': 'The Black Swan: a structured black gown, diamond/pearl jewellery and polished hair'},
    {'name': 'Lea Beaumont', 'gender': 'women', 'title': 'The Parisian It-Girl',
     'aesthetic': 'Parisian chic; romantic luxury; feminine contemporary fashion; playful designer dressing; effortlessly '
                  'seductive styling without excessive exposure',
     'colours': 'rose, blush pink, champagne, cream, ivory, black and soft neutrals',
     'wardrobe': 'romantic mini dresses; elegant skirts; fitted tops; tailored jackets; feminine blouses; soft flowing dresses; '
                 'sophisticated eveningwear',
     'accessories': 'delicate necklaces; fine bracelets; small designer-inspired handbags; feminine earrings',
     'footwear': 'strappy heels; ballet flats; elegant boots; minimalist sandals',
     'personality': 'Playful, romantic and fashionable. Effortlessly put together, never too formal for ordinary occasions.',
     'reference': 'The Rose Affair: a blush-pink/rose-champagne gown, delicate jewellery and romantic hair'},
    {'name': 'Scarlett Voss', 'gender': 'women', 'title': 'The Modern Socialite',
     'aesthetic': 'contemporary Manhattan luxury; sophisticated statement dressing; feminine power dressing; polished evening fashion',
     'colours': 'emerald green, black, navy, ivory, burgundy and deep jewel tones',
     'wardrobe': 'tailored dresses; elegant statement tops; structured skirts; designer-inspired separates; sophisticated '
                 'evening dresses; tailored coats',
     'accessories': 'statement earrings; layered jewellery; elegant bracelets; structured handbags; refined watches',
     'footwear': 'pointed heels; elegant boots; classic pumps; refined sandals',
     'personality': 'Composed, confident and visually distinctive. Every outfit has one memorable statement element.',
     'reference': 'The Emerald Entrance: a deep emerald gown, statement earrings and sleek hair'},
    {'name': 'Adrian Wexford', 'gender': 'men', 'title': 'The Dark Luxury Heir',
     'aesthetic': 'European luxury menswear; dark academia; tailored sophistication; understated expensive dressing',
     'colours': 'black, charcoal, midnight navy, dark grey, white and espresso',
     'wardrobe': 'tailored suits; structured overcoats; fine-knit sweaters; tailored trousers; crisp shirts; sophisticated '
                 'jackets; elevated casualwear',
     'accessories': 'luxury watches; minimal silver jewellery; leather belts; structured leather bags',
     'footwear': 'leather loafers; Oxford shoes; Chelsea boots; minimal luxury sneakers',
     'personality': 'Refined, confident and slightly mysterious. Fitted through thoughtful tailoring, never flashy.',
     'reference': 'The Midnight Tux: a black tailored tuxedo, white shirt, black bow tie and a luxury watch'},
    {'name': 'Alexander Wetherby', 'gender': 'men', 'title': 'The Old-Money Athlete',
     'aesthetic': 'American old money; country-club luxury; Ivy League dressing; sporty sophistication; Ralph Lauren-inspired '
                  'classic menswear',
     'colours': 'navy, burgundy, cream, white, forest green, camel and grey',
     'wardrobe': 'polo shirts; cable-knit sweaters; Oxford shirts; tailored chinos; pleated trousers; quarter-zip sweaters; '
                 'classic blazers; relaxed luxury weekend clothing',
     'accessories': 'classic watches; leather belts; minimal bracelets; refined sunglasses',
     'footwear': 'loafers; clean leather sneakers; suede shoes; classic dress shoes',
     'personality': 'Naturally wealthy and athletic. Comfortable, classic and quietly expensive.',
     'reference': 'The Midnight Blue: a deep navy tuxedo, white shirt and black bow tie'},
    {'name': 'Charles Wetherby', 'gender': 'men', 'title': 'The Classic Heir',
     'aesthetic': 'traditional American luxury; formal old-money dressing; classic masculine tailoring; timeless sophistication',
     'colours': 'black, navy, charcoal, white, cream and dark burgundy',
     'wardrobe': 'tailored suits; crisp dress shirts; classic knitwear; structured blazers; formal trousers; refined outerwear',
     'accessories': 'elegant cufflinks; classic watches; leather belts; minimal accessories',
     'footwear': 'Oxford shoes; polished loafers; classic leather dress shoes',
     'personality': 'Traditional, composed and impeccably dressed. No overly experimental trends.',
     'reference': 'The Classic Heir: a black tuxedo, white shirt and elegant cufflinks'},
    {'name': 'Sebastien Montclair', 'gender': 'men', 'title': 'The European Luxury Man',
     'aesthetic': 'European old money; dark luxury; refined minimalism; contemporary formalwear',
     'colours': 'black, espresso, charcoal, deep navy, ivory and dark brown',
     'wardrobe': 'tailored jackets; fine-knit sweaters; premium shirts; structured coats; tailored trousers; minimalist luxury '
                 'basics; sophisticated eveningwear',
     'accessories': 'understated luxury watches; minimal silver jewellery; leather accessories',
     'footwear': 'Chelsea boots; leather loafers; Oxford shoes; minimal leather sneakers',
     'personality': 'Wealth shown through quality, construction and restraint. Never unnecessarily loud.'},
    {'name': 'Oliver Hayes', 'gender': 'men', 'title': 'The Brooklyn Intellectual',
     'aesthetic': 'downtown New York casual; indie-inspired minimalism; intellectual casualwear; understated everyday fashion',
     'colours': 'brown, charcoal, cream, white, faded blue, olive and muted neutrals',
     'wardrobe': 'corduroy jackets; plain T-shirts; relaxed sweaters; denim jackets; straight-leg jeans; casual button-down '
                 'shirts; overshirts; simple everyday layers',
     'accessories': 'canvas backpacks; simple watches; minimal jewellery; practical shoulder bags',
     'footwear': 'canvas sneakers; minimal leather sneakers; casual boots',
     'personality': 'Never tries to compete with the wealthy characters. Relaxed, practical and authentic.'},
    {'name': 'Ruby Hayes', 'gender': 'women', 'title': 'The Downtown Rebel',
     'aesthetic': 'downtown New York fashion; indie sleaze; alternative feminine dressing; youthful street style',
     'colours': 'black, grey, burgundy, faded denim, muted red and silver',
     'wardrobe': 'ruched tops; plaid skirts; relaxed graphic tees; layered tops; denim; oversized jackets; casual dresses; '
                 'alternative-inspired everyday pieces',
     'accessories': 'black headbands; layered necklaces; silver jewellery; shoulder bags; statement belts',
     'footwear': 'chunky boots; Converse-inspired sneakers; casual boots; platform shoes',
     'personality': 'Expressive and individualistic. Younger and more experimental than the polished Upper East Side wardrobe.'},
    {'name': 'Elliot', 'gender': 'men', 'title': 'The Young Heir',
     'aesthetic': 'youthful American preppy; relaxed old-money dressing; contemporary schoolboy luxury',
     'colours': 'cream, navy, grey, white, burgundy and forest green',
     'wardrobe': 'cable-knit cardigans; polo shirts; Oxford shirts; fine-knit sweaters; relaxed trousers; classic jeans; simple jackets',
     'accessories': 'minimal watches; simple backpacks; understated accessories',
     'footwear': 'classic sneakers; loafers; casual leather shoes',
     'personality': 'Youthful, polished and age-appropriate: a younger take on the Wetherby / Upper East Side preppy look.'},
]
_BY_NAME = {c['name'].lower(): c for c in CAST}


def find(name):
    return _BY_NAME.get(str(name or '').strip().lower())


def profile(c):
    """The character's whole fashion identity, for Ella's prompt."""
    parts = [f"{c['name']}: {c['title']}.", f"Aesthetic: {c['aesthetic']}.", f"Signature colours: {c['colours']}.",
             f"Wardrobe: {c['wardrobe']}.", f"Accessories: {c['accessories']}.", f"Footwear: {c['footwear']}.",
             f"Styling personality: {c['personality']}"]
    if c.get('reference'):
        parts.append(f"Their signature outfit in the series: {c['reference']}.")
    return ' '.join(parts)
