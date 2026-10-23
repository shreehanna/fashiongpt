from flask import Flask, request, jsonify, send_from_directory
import anthropic
import json
import os

app = Flask(__name__, static_folder='.', static_url_path='')
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

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

    # Budget brand guidance
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
    budget_brands = budget_brands_men if gender == 'men' else budget_brands_women

    currency_symbol = '₹' if currency == 'INR' else '$'
    price_ranges = {
        'everyday': {'INR': '₹800–₹3,500', 'USD': '$15–$60'},
        'midrange': {'INR': '₹3,500–₹15,000', 'USD': '$60–$250'},
        'luxury': {'INR': '₹40,000–₹2,00,000', 'USD': '$500–$3,000'}
    }

    price_guide = price_ranges.get(budget, price_ranges['everyday'])[currency]
    brands = budget_brands.get(budget, budget_brands['everyday'])

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

    # Mode-specific prompt overrides
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
            response_text = message.content[0].text.strip()
            if '```json' in response_text:
                response_text = response_text.split('```json')[1].split('```')[0].strip()
            elif '```' in response_text:
                response_text = response_text.split('```')[1].split('```')[0].strip()
            outfit_data = json.loads(response_text)
            return jsonify(outfit_data)
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

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
            response_text = message.content[0].text.strip()
            if '```json' in response_text:
                response_text = response_text.split('```json')[1].split('```')[0].strip()
            elif '```' in response_text:
                response_text = response_text.split('```')[1].split('```')[0].strip()
            outfit_data = json.loads(response_text)
            return jsonify(outfit_data)
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Gender-specific prompt instructions
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

    # Content Creator mode: add photo_tip to JSON schema
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

        response_text = message.content[0].text.strip()

        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()

        outfit_data = json.loads(response_text)
        return jsonify(outfit_data)

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/plan-day', methods=['POST'])
def plan_day():
    """Generate 3 coordinated looks for a full day's schedule."""
    data = request.json
    schedule = data.get('schedule', '')
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    style_profile = data.get('style_profile', '')
    celebrity = data.get('celebrity', 'No specific muse')

    budget_brands_women = {
        'everyday': 'Zara, ASOS, Mango, H&M, Myntra, AJIO',
        'midrange': 'COS, Reformation, & Other Stories, Massimo Dutti',
        'luxury': 'Chanel, Dior, Prada, Gucci, Saint Laurent'
    }
    budget_brands_men = {
        'everyday': 'Zara Man, H&M Men, ASOS Menswear, Uniqlo, Myntra Men',
        'midrange': 'Massimo Dutti, COS Men, Reiss, Banana Republic',
        'luxury': 'Tom Ford, Brunello Cucinelli, Zegna, Dior Men'
    }
    budget_brands = budget_brands_men if gender == 'men' else budget_brands_women
    brands = budget_brands.get(budget, budget_brands['everyday'])

    price_ranges = {
        'everyday': {'INR': '₹800–₹3,500', 'USD': '$15–$60'},
        'midrange': {'INR': '₹3,500–₹15,000', 'USD': '$60–$250'},
        'luxury': {'INR': '₹40,000–₹2,00,000', 'USD': '$500–$3,000'}
    }
    price_guide = price_ranges.get(budget, price_ranges['everyday'])[currency]

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
        response_text = message.content[0].text.strip()
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
        return jsonify(json.loads(response_text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/restyle', methods=['POST'])
def restyle():
    """Re-style a saved look for a new occasion."""
    data = request.json
    look = data.get('look', {})
    new_occasion = data.get('new_occasion', '')
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')

    original_items = ', '.join([f"{i.get('name')} by {i.get('brand')}" for i in look.get('items', [])])
    original_name = look.get('look_name', 'your saved look')

    budget_brands_women = {
        'everyday': 'Zara, ASOS, Mango, H&M, Myntra, AJIO',
        'midrange': 'COS, Reformation, & Other Stories, Massimo Dutti',
        'luxury': 'Chanel, Dior, Prada, Gucci, Saint Laurent'
    }
    budget_brands_men = {
        'everyday': 'Zara Man, H&M Men, ASOS Menswear, Uniqlo',
        'midrange': 'Massimo Dutti, COS Men, Reiss',
        'luxury': 'Tom Ford, Brunello Cucinelli, Dior Men'
    }
    budget_brands = budget_brands_men if gender == 'men' else budget_brands_women
    brands = budget_brands.get(budget, budget_brands['everyday'])

    price_ranges = {
        'everyday': {'INR': '₹800–₹3,500', 'USD': '$15–$60'},
        'midrange': {'INR': '₹3,500–₹15,000', 'USD': '$60–$250'},
        'luxury': {'INR': '₹40,000–₹2,00,000', 'USD': '$500–$3,000'}
    }
    price_guide = price_ranges.get(budget, price_ranges['everyday'])[currency]

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
        response_text = message.content[0].text.strip()
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
        return jsonify(json.loads(response_text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/brand-match', methods=['POST'])
def brand_match():
    """Match the user's style profile to brands worth exploring."""
    data = request.json
    style_profile = data.get('style_profile', '')
    quiz = data.get('quiz', {})
    gender = data.get('gender', 'women')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    celeb = data.get('celeb', '')
    era = data.get('era', '')

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
        response_text = message.content[0].text.strip()
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
        return jsonify(json.loads(response_text))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/closet/save', methods=['POST'])
def save_closet():
    try:
        data = request.json
        closet_path = 'closet.json'
        existing = []
        if os.path.exists(closet_path):
            with open(closet_path, 'r') as f:
                existing = json.load(f)
        existing.append(data)
        with open(closet_path, 'w') as f:
            json.dump(existing, f)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/closet/load', methods=['GET'])
def load_closet():
    try:
        closet_path = 'closet.json'
        if not os.path.exists(closet_path):
            return jsonify([])
        with open(closet_path, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001)
