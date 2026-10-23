from flask import Flask, request, jsonify, send_from_directory
import anthropic
import json
import os
import requests as req
from datetime import datetime

app = Flask(__name__, static_folder='.', static_url_path='')
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

RATINGS_FILE = 'ratings.json'
STATS_FILE = 'stats.json'
CLOSET_FILE = 'closet.json'


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json_file(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def append_json_line(path, data):
    try:
        with open(path, 'a') as f:
            f.write(json.dumps(data) + '\n')
    except Exception:
        pass


def increment_stat(key):
    stats = load_json_file(STATS_FILE, {})
    stats[key] = stats.get(key, 0) + 1
    save_json_file(STATS_FILE, stats)


def get_budget_brands(gender, budget):
    budget_brands_women = {
        'everyday': 'Zara, ASOS, Mango, H&M, Topshop, Myntra, AJIO, Nykaa Fashion',
        'midrange': 'COS, Reformation, & Other Stories, Massimo Dutti, Reiss, Banana Republic, Vero Moda, AND',
        'luxury': 'Chanel, Dior, Prada, Gucci, Saint Laurent, Bottega Veneta, Versace, Valentino, Loro Piana'
    }
    budget_brands_men = {
        'everyday': 'Zara Man, H&M Men, ASOS Menswear, Mango Man, Uniqlo, Myntra Men, AJIO Men',
        'midrange': 'Massimo Dutti, COS Men, Reiss, Banana Republic, Selected Homme, Jack & Jones Premium',
        'luxury': 'Tom Ford, Brunello Cucinelli, Zegna, Canali, Dior Men, Saint Laurent Men, Prada Men'
    }
    brands = budget_brands_men if gender == 'men' else budget_brands_women
    return brands.get(budget, brands['everyday'])


def get_price_guide(budget, currency):
    price_ranges = {
        'everyday': {'INR': '₹800–₹3,500', 'USD': '$15–$60'},
        'midrange': {'INR': '₹3,500–₹15,000', 'USD': '$60–$250'},
        'luxury': {'INR': '₹40,000–₹2,00,000', 'USD': '$500–$3,000'}
    }
    return price_ranges.get(budget, price_ranges['everyday'])[currency]


def parse_claude_json(response_text):
    text = response_text.strip()
    if '```json' in text:
        text = text.split('```json')[1].split('```')[0].strip()
    elif '```' in text:
        text = text.split('```')[1].split('```')[0].strip()
    return json.loads(text)


# ── Keep-alive ping ───────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    """Keep-alive endpoint — set up cron-job.org to hit this every 10 mins to prevent Render sleep."""
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()}), 200


# ── Static routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Return live stats for the landing page counters."""
    stats = load_json_file(STATS_FILE, {})

    total_ratings = 0
    positive_ratings = 0
    try:
        if os.path.exists(RATINGS_FILE):
            with open(RATINGS_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        total_ratings += 1
                        if entry.get('rating') == 'up':
                            positive_ratings += 1
    except Exception:
        pass

    satisfaction = round((positive_ratings / total_ratings) * 100) if total_ratings > 0 else 0

    return jsonify({
        "looks_generated": stats.get("looks_generated", 0),
        "total_ratings": total_ratings,
        "satisfaction_rate": satisfaction,
        "display": f"{stats.get('looks_generated', 0)}+ looks styled"
    })


# ── Rating ────────────────────────────────────────────────────────────────────

@app.route('/api/rate', methods=['POST'])
def rate_look():
    """Save a thumbs up/down rating for a look."""
    try:
        data = request.json
        entry = {
            "look_name": data.get("look_name", ""),
            "rating": data.get("rating", ""),
            "occasion": data.get("occasion", ""),
            "celeb": data.get("celeb", ""),
            "era": data.get("era", ""),
            "timestamp": datetime.utcnow().isoformat()
        }
        append_json_line(RATINGS_FILE, entry)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Image search ──────────────────────────────────────────────────────────────

@app.route('/api/image-search', methods=['POST'])
def image_search():
    """Search Google Custom Search for a product image."""
    try:
        data = request.json
        query = data.get('query', '')
        api_key = os.environ.get('GOOGLE_API_KEY')
        cx = os.environ.get('GOOGLE_CX')

        if not api_key or not cx:
            return jsonify({'image_url': None, 'error': 'Google API not configured'})

        response = req.get(
            'https://www.googleapis.com/customsearch/v1',
            params={
                'key': api_key,
                'cx': cx,
                'q': query + ' fashion product',
                'searchType': 'image',
                'num': 1,
                'imgSize': 'medium',
                'safe': 'active'
            },
            timeout=5
        )
        items = response.json().get('items', [])
        if items:
            return jsonify({'image_url': items[0]['link']})
        return jsonify({'image_url': None})
    except Exception as e:
        return jsonify({'image_url': None, 'error': str(e)})


# ── Outfit ────────────────────────────────────────────────────────────────────

@app.route('/api/outfit', methods=['POST'])
def get_outfit():
    data = request.json
    occasion = data.get('occasion', '')
    celebrity = data.get('celebrity', 'No specific muse')
    era = data.get('era', 'Clean Girl Aesthetic')
    mode = data.get('mode', 'discover')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    gender = data.get('gender', 'women')
    style_profile = data.get('style_profile', '')
    closet_context = data.get('closet_context', '')
    recent_look_names = data.get('recent_look_names', [])
    body_profile = data.get('body_profile', '')

    brands = get_budget_brands(gender, budget)
    price_guide = get_price_guide(budget, currency)

    # Track usage
    increment_stat("looks_generated")

    closet_instruction = ''
    if mode == 'closet' and closet_context:
        closet_instruction = f"\n\nIMPORTANT: Style using items from the user's closet. {closet_context}"

    style_instruction = ''
    if style_profile:
        style_instruction = f"\n\nUser style profile: {style_profile} — factor this into your choices for fit, color, and silhouette."

    body_instruction = ''
    if body_profile:
        body_instruction = f"\n\nBody/size profile: {body_profile} — recommend fits, cuts, and silhouettes that flatter this body type. Be specific about what to look for (e.g. high-rise for longer legs, wrap styles for waist definition)."

    no_repeat_instruction = ''
    if recent_look_names:
        names_list = ', '.join(recent_look_names[-5:])
        no_repeat_instruction = f"\n\nNO-REPEAT RADAR: The user recently wore looks called: {names_list}. Do NOT repeat the same vibe, color palette, or silhouette. Make this look feel fresh and different."

    # ── Dupe mode ──
    if mode == 'dupe':
        inspo_look = data.get('inspo_look', '')
        dupe_prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

The user wants an affordable DUPE for this celebrity/editorial look: "{inspo_look}"

Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide})
Styling for: {gender}

Deconstruct the original look and recreate its essence — the silhouette, color story, and vibe — at {budget} budget using {brands}. Make it wearable for real life, not just editorial.

Return ONLY valid JSON:
{{
  "look_name": "a name for this dupe look",
  "ella_note": "Ella's 2-sentence note — name the original inspiration and what makes this dupe work. Gossip Girl energy.",
  "items": [
    {{
      "category": "category name",
      "name": "specific product name",
      "brand": "exact brand name",
      "price": "price with currency symbol",
      "description": "one sentence on why this dupes the original piece"
    }}
  ],
  "styling_notes": {{
    "accessories": "accessory suggestions that complete the dupe",
    "hair": "hair direction",
    "makeup": "makeup direction",
    "fragrance": "a fragrance that fits the vibe"
  }}
}}

Include 5-7 items. Be specific — real product names."""
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": dupe_prompt}]
            )
            return jsonify(parse_claude_json(message.content[0].text))
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Complete mode ──
    if mode == 'complete':
        anchor_item = data.get('anchor_item', '')
        complete_prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

The user has this item and wants a complete outfit built around it: "{anchor_item}"

Occasion: {occasion or 'versatile everyday wear'}
Style muse: {celebrity}
Era: {era}
Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide})
Styling for: {gender}
{style_instruction}{body_instruction}

Build a complete outfit that makes this anchor piece shine. Do NOT include the anchor item itself in the items array — only the supporting pieces. Pick colors, textures, and silhouettes that elevate the anchor.

Return ONLY valid JSON:
{{
  "look_name": "a name for this complete look",
  "ella_note": "Ella's 2-sentence note — reference the anchor item specifically and how you styled around it. Gossip Girl energy.",
  "items": [
    {{
      "category": "category name",
      "name": "specific product name",
      "brand": "exact brand name",
      "price": "price with currency symbol",
      "description": "one sentence on how this piece complements the anchor item"
    }}
  ],
  "styling_notes": {{
    "accessories": "accessory suggestions",
    "hair": "hair direction",
    "makeup": "makeup direction",
    "fragrance": "a fragrance that completes the look"
  }}
}}

Include 4-6 supporting items."""
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": complete_prompt}]
            )
            return jsonify(parse_claude_json(message.content[0].text))
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Gender-specific prompt instructions ──
    if gender == 'couple':
        gender_instruction = """You are styling a COUPLE — provide TWO coordinated looks: one for her, one for him. They should complement each other (matching accent colors, complementary formality, cohesive vibe) without being matchy-matchy.

Structure the items array with items for BOTH looks. Prefix each item's category with "Her — " or "Him — " (e.g. "Her — Dress", "Him — Blazer"). Include 5-6 items per person (10-12 total).

For styling_notes, give coordinated advice: shared fragrance family, complementary hair/grooming directions, how the looks will photograph together."""
        gender_instruction_2 = "couple (her + him coordinated looks)"
    elif gender == 'men':
        gender_instruction = """You are styling a MAN. Focus on menswear — tailoring, silhouettes, and fits appropriate for the occasion. Think: collar vs no collar, trouser break, jacket lapel width, shoe formality.

For men, the categories are: Shirts / Trousers / Suit / Blazer / Outerwear / Shoes / Belt / Watch / Accessories.
styling_notes should cover: grooming (hair + beard direction), watch/accessory choices, fragrance, and one fit tip (e.g. sleeve length, trouser break)."""
        gender_instruction_2 = "man"
    else:
        gender_instruction = "You are styling a WOMAN."
        gender_instruction_2 = "woman"

    # Content Creator mode
    photo_tip_schema = ''
    photo_tip_instruction = ''
    if occasion == 'Content Creator':
        photo_tip_instruction = '\n\nCONTENT CREATOR MODE: This outfit will be photographed/filmed for social media. Think about how it will look on camera — contrast, texture pop, color saturation, and silhouette clarity.'
        photo_tip_schema = ',\n  "photo_tip": "one sharp sentence on how to shoot or film this look — lighting, angle, backdrop color that makes it pop on Instagram/Reels"'

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist — sharp, warm, and obsessively precise. You know luxury fashion inside out but you never make someone feel less than.

{gender_instruction}

Occasion: {occasion}
Styling for: {gender_instruction_2}
Style muse: {celebrity}
Aesthetic era: {era}
Mode: {mode}
Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide})
{style_instruction}{body_instruction}{closet_instruction}{no_repeat_instruction}{photo_tip_instruction}

Build a complete head-to-toe outfit. Return ONLY valid JSON in exactly this structure:

{{
  "look_name": "a poetic 3-5 word name for this look",
  "ella_note": "Ella's opening 2-sentence note — sharp, warm, slightly Gossip Girl. MUST name the specific occasion AND the city (if provided) AND one specific detail about the muse's style applied here. No filler, no generic lines.",
  "items": [
    {{
      "category": "category name (see gender-specific guidance above)",
      "name": "specific product name",
      "brand": "exact brand name (from the budget tier brands)",
      "price": "price with currency symbol e.g. ₹2,499 or $45",
      "description": "one precise sentence on why this piece works for this specific occasion and muse"
    }}
  ],
  "styling_notes": {{
    "accessories": "specific accessory suggestions",
    "hair": "hair direction for this look",
    "makeup": "makeup or grooming direction",
    "fragrance": "a fragrance that completes this look"
  }}{photo_tip_schema}
}}

Include 6-8 items (or 10-12 for couples). Be specific — real product names, real brand names. Prices must be realistic for the {budget} tier in {currency}. Factor in the city/weather/dress code context from the occasion string."""

    try:
        max_tokens = 3500 if gender == 'couple' else 2000
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify(parse_claude_json(message.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Plan My Day ───────────────────────────────────────────────────────────────

@app.route('/api/plan-day', methods=['POST'])
def plan_day():
    data = request.json
    schedule = data.get('schedule', '')
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    style_profile = data.get('style_profile', '')
    celebrity = data.get('celebrity', 'No specific muse')

    brands = get_budget_brands(gender, budget)
    price_guide = get_price_guide(budget, currency)
    increment_stat("day_plans")

    style_instruction = f"\nStyle muse: {celebrity}" if celebrity != 'No specific muse' else ''
    if style_profile:
        style_instruction += f"\nUser style profile: {style_profile}"

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

The user has a full day ahead and needs outfits that work together — pieces they can mix, layer, or swap for a seamless transition.

Day schedule: "{schedule}"

Gender: {gender}
Budget: {budget} ({brands})
Currency: {currency} (price range: {price_guide})
{style_instruction}

Create exactly 3 looks — one per key part of the day. Looks should share a color story or base pieces for easy transitions (e.g., same trousers styled two ways, a blazer that works for two occasions).

Return ONLY valid JSON:
{{
  "day_title": "a chic title for this day's looks",
  "looks": [
    {{
      "slot": "time/occasion label (e.g. Morning · Office)",
      "look_name": "3-5 word look name",
      "ella_note": "one sentence on this look's vibe",
      "items": [
        {{
          "category": "category",
          "name": "product name",
          "brand": "brand name",
          "price": "price",
          "description": "one sentence"
        }}
      ],
      "transition_tip": "one sentence on how to transition FROM this look to the next"
    }}
  ]
}}

Each look: 4-5 items. The third look has no transition_tip (end of day). Cohesive color story across all three."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify(parse_claude_json(message.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Re-style ──────────────────────────────────────────────────────────────────

@app.route('/api/restyle', methods=['POST'])
def restyle():
    data = request.json
    look = data.get('look', {})
    new_occasion = data.get('new_occasion', '')
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')

    brands = get_budget_brands(gender, budget)
    price_guide = get_price_guide(budget, currency)
    increment_stat("restyles")

    original_items = ', '.join([f"{i.get('name')} by {i.get('brand')}" for i in look.get('items', [])])
    original_name = look.get('look_name', 'your saved look')

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

The user saved a look called "{original_name}" with these pieces: {original_items}

Now they want to RE-STYLE it for: {new_occasion}

Budget: {budget} ({brands})
Currency: {currency} (price range: {price_guide})
Gender: {gender}

Keep as many original pieces as you can. Swap out what doesn't work and add new pieces to make it work for the new occasion. Be clever — Ella knows how to make a brunch blazer work for a cocktail hour.

Return ONLY valid JSON in the standard outfit format:
{{
  "look_name": "new name for this restyled look",
  "ella_note": "2-sentence Ella note — acknowledge the original look and explain the restyle. Gossip Girl energy.",
  "items": [
    {{
      "category": "category",
      "name": "product name",
      "brand": "brand",
      "price": "price",
      "description": "one sentence — note if this is KEPT from original or NEW"
    }}
  ],
  "styling_notes": {{
    "accessories": "...",
    "hair": "...",
    "makeup": "...",
    "fragrance": "..."
  }}
}}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify(parse_claude_json(message.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Brand Match ───────────────────────────────────────────────────────────────

@app.route('/api/brand-match', methods=['POST'])
def brand_match():
    data = request.json
    style_profile = data.get('style_profile', '')
    quiz = data.get('quiz', {})
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    celeb = data.get('celeb', '')
    era = data.get('era', '')
    increment_stat("brand_matches")

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

Based on this user's style profile, recommend brands they should know and shop.

Style profile: {style_profile}
Gender: {gender}
Budget tier: {budget}
Currency preference: {currency}
Style muse: {celeb or 'None'}
Aesthetic era: {era or 'None'}
Quiz answers: Body type: {quiz.get('body', '')}, Skin tone: {quiz.get('skin', '')}, Lifestyle: {quiz.get('lifestyle', '')}, Palette: {', '.join(quiz.get('palette', []))}

Give 6 brand recommendations across these categories: 2 everyday finds, 2 investment pieces brands, 1 under-the-radar label, 1 online platform. For each, explain in 1-2 sentences WHY it fits this specific profile.

Return ONLY valid JSON:
{{
  "intro": "Ella's 1-sentence intro — personalized to their profile",
  "brands": [
    {{
      "name": "Brand Name",
      "tier": "everyday | investment | under-the-radar | platform",
      "why": "1-2 sentences on why this brand fits their profile",
      "signature": "what this brand is known for (e.g. 'impeccable tailoring, neutral palette')",
      "shop_hint": "where to find them in India or online"
    }}
  ]
}}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify(parse_claude_json(message.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Closet ────────────────────────────────────────────────────────────────────

@app.route('/api/closet/save', methods=['POST'])
def save_closet():
    """Save a closet item — persists to server so it survives browser clears."""
    try:
        data = request.json
        existing = load_json_file(CLOSET_FILE, [])
        # Avoid duplicates by name
        existing = [i for i in existing if i.get('name') != data.get('name')]
        existing.append(data)
        save_json_file(CLOSET_FILE, existing)
        return jsonify({"success": True, "count": len(existing)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/closet/load', methods=['GET'])
def load_closet():
    """Load all saved closet items from server."""
    try:
        return jsonify(load_json_file(CLOSET_FILE, []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/closet/delete', methods=['POST'])
def delete_closet_item():
    """Delete a single closet item by name."""
    try:
        data = request.json
        name = data.get('name', '')
        existing = load_json_file(CLOSET_FILE, [])
        existing = [i for i in existing if i.get('name') != name]
        save_json_file(CLOSET_FILE, existing)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5001)
