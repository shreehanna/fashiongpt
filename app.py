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
    style_profile = data.get('style_profile', '')
    closet_context = data.get('closet_context', '')

    # Budget brand guidance
    budget_brands = {
        'everyday': 'Zara, ASOS, Mango, H&M, Topshop, Myntra, AJIO, Nykaa Fashion',
        'midrange': 'COS, Reformation, & Other Stories, Massimo Dutti, Reiss, Banana Republic, Vero Moda, AND',
        'luxury': 'Chanel, Dior, Prada, Gucci, Saint Laurent, Bottega Veneta, Versace, Valentino, Loro Piana'
    }

    # Currency symbol
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

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist — sharp, warm, and obsessively precise. You know luxury fashion inside out but you never make someone feel less than.

Occasion: {occasion}
Style muse: {celebrity}
Aesthetic era: {era}
Mode: {mode}
Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide})
{style_instruction}{closet_instruction}

Build a complete head-to-toe outfit for this person. Return ONLY valid JSON in exactly this structure:

{{
  "look_name": "a poetic 3-5 word name for this look",
  "ella_note": "Ella's opening 2-sentence note — smart, warm, slightly Gossip Girl. Reference the occasion, the muse, and one specific styling insight. No filler.",
  "items": [
    {{
      "category": "Tops / Bottoms / Dress / Outerwear / Shoes / Bag / Accessories / Jewellery",
      "name": "specific product name",
      "brand": "exact brand name (from the budget tier brands)",
      "price": "price with currency symbol e.g. ₹2,499 or $45",
      "description": "one precise sentence on why this piece works for this specific occasion and muse"
    }}
  ],
  "styling_notes": {{
    "accessories": "specific accessory suggestions",
    "hair": "hair direction for this look",
    "makeup": "makeup direction",
    "fragrance": "a fragrance that completes this look"
  }}
}}

Include 6-8 items covering a complete outfit. Be specific — real product names, real brand names. Prices must be realistic for the {budget} tier in {currency}. Factor in the city/weather/dress code context from the occasion string."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text.strip()

        # Extract JSON
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
