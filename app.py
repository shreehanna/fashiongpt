from flask import Flask, request, jsonify, send_from_directory, Response, g, stream_with_context
import anthropic
import json
import hashlib
import base64
import os
import requests as req
from shop import country_code, find_image
from regions import brands_for, prompt_note
import re
from pro import owner_request, refund_look
from looks import create_unique, stream_unique, RepeatError, STYLE_MODEL   # every look checked against every look ever given
from alerts import watch_claude, voice_result, ServiceDown, bp as alerts_bp   # tells the owner when an API runs low
from growth import sign_look, track, bp as growth_bp   # shareable "Rate my fit" links, boards, challenge, stats
import daily                                            # today's fit, Sunday Edit, characters, reels
import muses                                            # real style files, so muses are dressed like themselves
from push import bp as push_bp                          # Home Screen app + morning notifications

# static_folder=None: only index.html is served, so app.py, pro.py and any data files can't be downloaded
app = Flask(__name__, static_folder=None)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
watch_claude(client)   # counts each call's cost and alerts the owner if credits run low or out


# ── Looks, written live ───────────────────────────────────────────────────────
# Ella takes 15-25 s to write a whole look. With "stream": true the page gets her writing live, one JSON
# object per line: the name and note as they grow, each piece field by field, and the full look last.
def _strip_fences(text):
    t = (text or '').strip()
    if '```json' in t:
        t = t.split('```json')[1].split('```')[0].strip()
    elif '```' in t:
        t = t.split('```')[1].split('```')[0].strip()
    return t


class LiveLook:
    """Reads Ella's answer while she writes it and says what changed: the name and her note as they
    grow, the piece she's on (field by field), and each piece once it's finished. Only the new part of
    the text is looked at each time, so it stays cheap on Render's small CPU."""
    FIELDS = ('category', 'name', 'brand', 'store', 'price', 'description')
    _KEY = re.compile(r'"(\w+)"\s*:\s*"')

    def __init__(self):
        self.text = ''
        self.vals = {'look_name': ('', False), 'ella_note': ('', False)}
        self.items_at = None        # where the items list starts
        self.pos = None             # scanning resumes here: between pieces, outside any text
        self.done_items = 0
        self.part = None            # the half-written piece last reported
        self.closed = False

    @staticmethod
    def _string_at(text, i):
        """(raw text of a JSON string starting at i, finished?)"""
        esc = False
        for j in range(i, len(text)):
            ch = text[j]
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                return text[i:j], True
        return text[i:], False

    @staticmethod
    def _decode(raw, finished):
        if not finished:
            if (len(raw) - len(raw.rstrip('\\'))) % 2:
                raw = raw[:-1]                       # an escape cut in half
            raw = re.sub(r'\\u[0-9a-fA-F]{0,3}$', '', raw)
        try:
            return json.loads('"' + raw + '"')
        except ValueError:
            return None

    def _fields(self, obj):
        out, i = {}, 0
        while True:
            m = self._KEY.search(obj, i)
            if not m:
                return out
            raw, fin = self._string_at(obj, m.end())
            val = self._decode(raw, fin)
            if m.group(1) in self.FIELDS and val is not None:
                out[m.group(1)] = val
            if not fin:
                return out
            i = m.end() + len(raw) + 1

    def feed(self, chunk):
        self.text += chunk
        text, out = self.text, []
        for key, kind in (('look_name', 'name'), ('ella_note', 'note')):
            old, fin = self.vals[key]
            if fin:
                continue
            m = re.search(r'"' + key + r'"\s*:\s*"', text)
            if not m:
                continue
            raw, fin = self._string_at(text, m.end())
            val = self._decode(raw, fin)
            if val is not None and (val != old or fin):
                self.vals[key] = (val, fin)
                out.append({'t': kind, key: val, 'done': fin})
        if self.closed:
            return out
        if self.items_at is None:
            m = re.search(r'"items"\s*:\s*\[', text)
            if not m:
                return out
            self.items_at = self.pos = m.end()
        depth, start, in_str, esc = 0, None, False, False
        for i in range(self.pos, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        item = json.loads(text[start:i + 1])
                    except ValueError:
                        item = None              # the full look at the end still has it
                    if isinstance(item, dict):
                        out.append({'t': 'item', 'i': self.done_items, 'item': item})
                    self.done_items += 1
                    self.part = None
                    self.pos = i + 1
                    start = None
            elif ch == ']' and depth == 0:
                self.closed = True
                return out
        if start is not None:                    # a piece she's in the middle of
            fields = self._fields(text[start:])
            if fields and fields != self.part:
                self.part = fields
                out.append({'t': 'item_part', 'i': self.done_items, 'item': fields})
        return out


QUICK_MODEL = os.environ.get('QUICK_MODEL') or 'claude-haiku-5-5'


def quick_create(prompt, max_tokens):
    """Small, simple jobs (like understanding a spoken request) on the fast model, so Talk starts the
    look sooner. Falls back to the styling model if the fast one isn't available."""
    try:
        return client.messages.create(model=QUICK_MODEL, max_tokens=max_tokens,
                                      messages=[{'role': 'user', 'content': prompt}])
    except ServiceDown:
        raise
    except Exception as e:
        print(f'[talk] {QUICK_MODEL} failed ({e!r}); using {STYLE_MODEL}')
        return client.messages.create(model=STYLE_MODEL, max_tokens=max_tokens,
                                      messages=[{'role': 'user', 'content': prompt}])


def _line(ev):
    return json.dumps(ev, ensure_ascii=False) + '\n'


def _finished(data, text):
    """The finished look, signed so it can be shared, and counted."""
    look = json.loads(_strip_fences(text))
    import muses
    bank = data.get('_muse_bank') or []
    real = next((o for o in bank if o.get('id') == look.get('outfit_id')), None) if isinstance(look, dict) else None
    try:
        if real:      # her exact pieces, by their real names, whatever the model wrote (before signing, so shares match)
            muses.apply_real(look, real, data.get('_muse_pieces') or [], str(data.get('_muse_key') or ''))
    except Exception as e:
        print('[look] could not match the real outfit:', repr(e))
    try:
        from pro import identity
        sign_look(look, data)
        track('look', identity()[1])
        if real:      # a recreation of a real outfit the muse wore: say which, and don't give it to this person again
            look['real_outfit'] = {'muse': str(data.get('_muse_key') or '')[:60], 'event': real['event'],
                                   'date': real['date'], 'url': real['url'], 'source': real['source']}
            muses.mark_used(identity()[1], data.get('_muse_key'), real['id'])
    except Exception as e:
        print('[look] could not sign the look:', e)
    return look


def look_response(data, prompt, max_tokens, image_url=None):
    """The look as one JSON reply, or written live when the page sends "stream": true."""
    if not data.get('stream'):
        message = create_unique(client, data, prompt, max_tokens, image_url=image_url)
        return jsonify(_finished(data, message.content[0].text))
    ticket = getattr(g, 'look_ticket', None)
    events = stream_unique(client, data, prompt, max_tokens, image_url=image_url)

    def gen():
        live = LiveLook()
        yield _line({'t': 'start'})
        try:
            for kind, payload in events:
                if kind == 'delta':
                    for ev in live.feed(payload):
                        yield _line(ev)
                elif kind == 'status':           # still reading up on the muse: say so
                    yield _line({'t': 'status', 'text': str(payload)[:140]})
                elif kind == 'restart':          # that was a repeat: Ella starts over
                    live = LiveLook()
                    yield _line({'t': 'restart'})
                else:
                    yield _line({'t': 'done', 'look': _finished(data, payload)})
                    return
            raise RuntimeError('the look ended early')
        except Exception as e:
            if ticket:
                refund_look(ticket)              # a look that failed doesn't use up a free look
            print('[look] live look failed:', repr(e))
            friendly = str(e) if isinstance(e, (ServiceDown, RepeatError)) else 'Ella hit a snag. Please try again.'
            yield _line({'t': 'error', 'error': friendly})

    resp = Response(stream_with_context(gen()), mimetype='application/x-ndjson')
    resp.headers['Cache-Control'] = 'no-cache, no-transform'
    resp.headers['X-Accel-Buffering'] = 'no'     # tell any proxy not to hold the parts back
    return resp

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/outfit', methods=['POST'])
def get_outfit():
    data = request.json
    occasion = data.get('occasion', '')
    celebrity = data.get('celebrity', 'No specific muse')
    era = data.get('era') or 'Upper East Side Glam'
    mode = data.get('mode', 'discover')
    budget = data.get('budget', 'everyday')
    currency = data.get('currency', 'INR')
    region_note = prompt_note(data)   # only what this shopper can buy in their country
    gender = data.get('gender', 'women')
    style_profile = data.get('style_profile', '')
    closet_context = data.get('closet_context', '')
    recent_look_names = data.get('recent_look_names', [])
    body_profile = data.get('body_profile', '')
    data['_character'] = daily.find_character(data.get('character'))   # only the owner's own list, never the page's text
    if data['_character']:
        celebrity = f"{data['_character']['name']} (from The Social Representation)"

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
    brands = brands_for(data, gender, budget) or budget_brands.get(budget, budget_brands['everyday'])

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

    # A whole wedding, planned function by function: no two functions get the same outfit
    plan_instruction = ''
    plan = data.get('plan') if isinstance(data.get('plan'), dict) else None
    if plan:
        clip = lambda v, n: ' '.join(str(v or '').split())[:n]
        fn, role, when, notes = clip(plan.get('function'), 40), clip(plan.get('role'), 40), clip(plan.get('date'), 24), clip(plan.get('notes'), 160)
        plan_instruction = (f"\n\nWEDDING SEASON PLAN: this look is for the {fn or 'function'}"
                            + (f" on {when}" if when else '') + " of a wedding the user is planning a whole set of outfits for"
                            + (f", as {role}" if role else '') + '.'
                            + (f" Notes for this function: {notes}." if notes else '')
                            + f" Make it exactly right for a {fn or 'wedding function'}: the right formality, the colours hosts expect "
                              "(yellow for a haldi, greens and brights for a mehendi, sparkle for a sangeet), and Indian or "
                              "Indo-western wear when the function calls for it.")
        others = [f"- {clip(o.get('function'), 40)}: {clip(o.get('summary'), 180)}"
                  for o in (plan.get('others') or [])[:8] if isinstance(o, dict) and o.get('summary')]
        if others:
            plan_instruction += (" Outfits already planned for the other functions (never repeat their hero piece, silhouette "
                                 "or main colour; the set should feel like one wardrobe story with its own colour for each "
                                 "function):\n" + '\n'.join(others))

    # Mode-specific prompt overrides
    if mode == 'reel':
        reel = daily.reel_for_look(data.get('reel'))
        if not reel:
            return jsonify({'error': 'Ella couldn\'t get the picture from that link (the post may be private). Upload a screenshot instead.'}), 400
        by = f" by {reel['channel']}" if reel['channel'] else ''
        if reel.get('pick_shot'):
            data['occasion'] = f"the look from a reel: {reel['title'] or 'a fashion reel'}"[:120]
            seen = f"The user loved the outfit in a fashion reel" + (f" (\"{reel['title']}\")" if reel['title'] else '') + ' and wants that look.'
            what = ("The picture is a screenshot of that reel: recreate the main outfit in it, piece by piece. "
                    "Ignore any text, usernames or app buttons.")
        elif reel.get('shot'):
            data['occasion'] = 'the look from a screenshot'
            seen = 'The user saw a look they love and took a screenshot of it (from Instagram, TikTok, Pinterest or similar).'
            if reel.get('caption'):
                seen += f" The post's caption (it may name or tag the brands): \"{reel['caption']}\""
            what = ("The picture is that screenshot: recreate the main outfit in it. If it shows several outfits, take the "
                    "most striking one. Ignore any text, usernames, captions or app buttons. If there is no outfit in it, "
                    "build a look from the style, colours and mood the picture suggests.")
        else:
            data['occasion'] = f"the look from a reel: {reel['title'] or 'a fashion video'}"[:120]
            seen = f"The user is watching a fashion video, \"{reel['title'] or 'a fashion reel'}\"{by}, and wants that look."
            what = ("The picture is the video's thumbnail: recreate the main outfit in it. If it shows several outfits, take "
                    "the most striking one; if it shows no outfit clearly, build the look the title describes."
                    if reel['image'] else "Recreate the kind of outfit this video is about.")
        reel_prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

{seen}
{what}

Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide}){region_note}
Styling for: {gender}
{style_instruction}{body_instruction}

Rebuild the outfit piece by piece with real products the shopper can buy: the same silhouettes, colours and vibe.

Return ONLY valid JSON:
{{
  "look_name": "a name for this look",
  "ella_note": "Ella's 2-sentence note: what makes the look in the video work and how this version gets it. Gossip Girl energy.",
  "items": [
    {{
      "category": "category name",
      "name": "specific product name",
      "brand": "exact brand name",
      "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
      "price": "price with currency symbol",
      "description": "one sentence on which part of the video's look this recreates"
    }}
  ],
  "styling_notes": {{
    "accessories": "accessory suggestions",
    "hair": "hair direction",
    "makeup": "makeup direction",
    "fragrance": "a fragrance that fits the vibe"
  }}
}}

Include 5-7 items."""
        try:
            return look_response(data, reel_prompt, 2000, image_url=reel['image'])
        except json.JSONDecodeError as e:
            return jsonify({"error": f"Parse error: {str(e)}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if mode == 'dupe':
        inspo_look = data.get('inspo_look', '')
        dupe_prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

The user wants an affordable DUPE for this celebrity/editorial look: "{inspo_look}"

Budget: {budget} ({brands})
Currency: {currency} (price range per piece: {price_guide}){region_note}
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
      "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
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
            return look_response(data, dupe_prompt, 2000)
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
Currency: {currency} (price range per piece: {price_guide}){region_note}
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
      "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
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
            return look_response(data, complete_prompt, 2000)
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
Currency: {currency} (price range per piece: {price_guide}){region_note}
{style_instruction}{body_instruction}{closet_instruction}{no_repeat_instruction}{photo_tip_instruction}{plan_instruction}

Build a complete head-to-toe outfit. Return ONLY valid JSON in exactly this structure:

{{
  "look_name": "a poetic 3-5 word name for this look",
  "ella_note": "Ella's opening 2-sentence note — sharp, warm, slightly Gossip Girl. MUST name the specific occasion AND the city (if provided) AND one specific detail about the muse's style applied here. No filler, no generic lines.",
  "items": [
    {{
      "category": "category name (see gender-specific guidance above)",
      "name": "specific product name",
      "brand": "exact brand name (the budget tier brands are examples, not a limit)",
      "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
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
        return look_response(data, prompt, max_tokens)

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
    region_note = prompt_note(data)   # only what this shopper can buy in their country
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
    brands = brands_for(data, gender, budget) or budget_brands.get(budget, budget_brands['everyday'])

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
Currency: {currency} (price range: {price_guide}){region_note}
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
          "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
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
        message = create_unique(client, data, prompt, 3000, kind='day')
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
    region_note = prompt_note(data)   # only what this shopper can buy in their country

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
    brands = brands_for(data, gender, budget) or budget_brands.get(budget, budget_brands['everyday'])

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
Currency: {currency} (price range: {price_guide}){region_note}
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
      "store": "website to buy it from in the shopper's country, as a bare domain like zara.com",
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
        return look_response(data, prompt, 2000)
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
    region_note = prompt_note(data)   # only what this shopper can buy in their country
    celeb = data.get('celeb', '')
    era = data.get('era', '')

    prompt = f"""You are Ella, a Gossip Girl-coded AI fashion stylist.

Based on this user's style profile, recommend brands they should know and shop.

Style profile: {style_profile}
Gender: {gender}
Budget tier: {budget}
Currency preference: {currency}{region_note}
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
            model=STYLE_MODEL,
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


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})

@app.route('/api/image-search', methods=['POST'])
def image_search():
    # Serper if SERPER_API_KEY is set, otherwise the old Google Custom Search keys (shop.py)
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()[:200]
    if not query:
        return jsonify({'image_url': None})
    return jsonify({'image_url': find_image(query + ' product', gl=country_code(data).lower())})


@app.route('/api/parse-voice', methods=['POST'])
def parse_voice():
    """Parse a free-form voice transcript into structured outfit request data."""
    try:
        data = request.json
        transcript = data.get('transcript', '')
        if not transcript.strip():
            return jsonify({'error': 'No transcript provided'}), 400

        prompt = f"""You are Ella, a fashion AI. A user just spoke this request to you:

"{transcript}"

Extract the following fields. If a field isn't mentioned, use the default shown.

Return ONLY valid JSON:
{{
  "occasion": "what occasion/event they described — be specific (e.g. 'beach birthday party', 'job interview at a startup', 'date night dinner'). Default: 'everyday casual'",
  "budget": "everyday | midrange | luxury — infer from words like 'affordable', 'designer', 'luxury', 'cheap', 'splurge'. Default: everyday",
  "gender": "women | men | couple — infer from pronouns or explicit mention. Default: women",
  "celeb": "any celebrity name mentioned as style muse. Default: ''",
  "city": "city or location mentioned. Default: ''",
  "season": "season mentioned (Summer, Winter, Monsoon, etc). Default: ''",
  "era": "the aesthetic: one of 'Clean Girl Aesthetic', 'Quiet Luxury', '2000s Y2K', 'Goth', 'Cottagecore', '1990s Grunge', 'Upper East Side Glam' if what they said matches one; their own words (a few words) for any other aesthetic they name; 'No specific aesthetic (dress me like my muse)' if they name a celebrity but no aesthetic. Otherwise 'Upper East Side Glam'",
  "ella_greeting": "Ella's short 1-sentence spoken response confirming she heard the request — warm, Gossip Girl-coded. Start with 'Got it —' or 'On it —' etc. Under 20 words."
}}"""

        message = quick_create(prompt, 500)
        response_text = message.content[0].text.strip()
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()
        parsed = json.loads(response_text)
        return jsonify(parsed)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Parse error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# (closet save/load endpoints removed: the app keeps closets in each person's browser,
#  and the old shared closet.json let anyone read everyone's uploads)

# ── Ella's voice ─────────────────────────────────────────────────────────────
# The ElevenLabs key lives in Render's environment (ELEVENLABS_API_KEY), never in index.html.
# Voice IDs aren't secret; ELLA_VOICE_ID / LEON_VOICE_ID on Render override these defaults.
VOICES = {
    'ella': {'env': 'ELLA_VOICE_ID', 'default': '2xzAoajJn3wvwyztUU52',
             'settings': {'stability': 0.35, 'similarity_boost': 0.9, 'style': 0.75, 'use_speaker_boost': True}},
    'leon': {'env': 'LEON_VOICE_ID', 'default': 'cIMwmTggaef4oj037F51',
             'settings': {'stability': 0.45, 'similarity_boost': 0.9, 'style': 0.55, 'use_speaker_boost': True}},
}
import time
from collections import deque

_tts_cache = {}            # short fixed lines ("Hi babe", loading lines) are only paid for once
# Flash costs half as many ElevenLabs credits per character as Multilingual v2 and starts speaking faster.
# For the richer (pricier) voice, set TTS_MODEL=eleven_multilingual_v2 in Render.
TTS_MODEL = os.environ.get('TTS_MODEL') or 'eleven_flash_v2_5'


def _saved_voice(key):
    """Short fixed lines are also kept in the database, so a restart or redeploy doesn't pay for them again."""
    try:
        from pro import kv_get
        v = kv_get(key)
        return base64.b64decode(v) if v else None
    except Exception:
        return None


def _save_voice(key, audio):
    try:
        from pro import kv_set
        kv_set(key, base64.b64encode(audio).decode())
    except Exception as e:
        print('[tts] could not keep the clip:', e)
_tts_hits = {}             # ip -> recent request times, to stop anyone burning your credits
TTS_MAX_CHARS = 400
TTS_PER_IP = 20            # requests per 10 minutes per visitor


def _tts_allowed(ip):
    now = time.time()
    q = _tts_hits.setdefault(ip, deque())
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= TTS_PER_IP:
        return False
    q.append(now)
    return True


@app.route('/api/tts', methods=['POST'])
def tts():
    key = os.environ.get('ELEVENLABS_API_KEY')
    body = request.get_json(silent=True) or {}
    who = body.get('stylist') if body.get('stylist') in VOICES else 'ella'
    voice = VOICES[who]
    voice_id = os.environ.get(voice['env']) or voice['default']
    if not key:
        return jsonify({'error': 'voice not configured'}), 503
    text = (body.get('text') or '').strip()[:TTS_MAX_CHARS]
    if not text:
        return jsonify({'error': 'no text'}), 400
    cache_key = f'{who}:{text}'
    if cache_key in _tts_cache:
        return Response(_tts_cache[cache_key], mimetype='audio/mpeg')
    short = len(text) <= 120
    db_key = 'tts:' + hashlib.sha1(f'{voice_id}|{TTS_MODEL}|{text}'.encode()).hexdigest()
    if short:
        audio = _saved_voice(db_key)
        if audio:
            if len(_tts_cache) < 64:
                _tts_cache[cache_key] = audio
            return Response(audio, mimetype='audio/mpeg')
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    if not owner_request() and not _tts_allowed(ip):
        return jsonify({'error': 'slow down'}), 429
    try:
        r = req.post(
            f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
            headers={'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'},
            json={
                'text': text,
                'model_id': TTS_MODEL,
                'voice_settings': voice['settings']
            },
            timeout=30
        )
    except req.RequestException:
        return jsonify({'error': 'voice service unreachable'}), 502
    voice_result(r)
    if r.status_code != 200:
        return jsonify({'error': 'voice service error', 'status': r.status_code}), 502
    if short:
        if len(_tts_cache) < 64:
            _tts_cache[cache_key] = r.content
        _save_voice(db_key, r.content)
    return Response(r.content, mimetype='audio/mpeg')


# ── Talk: speech to text ─────────────────────────────────────────────────────
# The phone records the whole request, then ElevenLabs Scribe transcribes it in one go.
# It copes with accents, Hinglish and brand names far better than the browser's built-in
# recogniser, and nothing gets "corrected" word by word while you talk.
_stt_hits = {}
STT_PER_IP = 15            # transcriptions per 10 minutes per visitor
STT_MAX_BYTES = 8 * 1024 * 1024


@app.route('/api/transcribe', methods=['POST'])
def transcribe():
    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        return jsonify({'error': 'voice not configured'}), 503
    audio = request.files.get('audio')
    if not audio:
        return jsonify({'error': 'no audio'}), 400
    data = audio.read(STT_MAX_BYTES + 1)
    if not data:
        return jsonify({'error': 'empty recording'}), 400
    if len(data) > STT_MAX_BYTES:
        return jsonify({'error': 'recording too long'}), 413
    ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
    now = time.time()
    q = _stt_hits.setdefault(ip, deque())
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= STT_PER_IP and not owner_request():
        return jsonify({'error': 'slow down'}), 429
    q.append(now)
    try:
        r = req.post(
            'https://api.elevenlabs.io/v1/speech-to-text',
            headers={'xi-api-key': key},
            data={'model_id': os.environ.get('STT_MODEL', 'scribe_v1'), 'tag_audio_events': 'false'},
            files={'file': (audio.filename or 'talk.webm', data, audio.mimetype or 'application/octet-stream')},
            timeout=60
        )
    except req.RequestException:
        return jsonify({'error': 'voice service unreachable'}), 502
    voice_result(r)
    if r.status_code != 200:
        print('[talk] transcription failed', r.status_code, r.text[:200])
        return jsonify({'error': 'transcription failed', 'status': r.status_code}), 502
    out = r.json()
    return jsonify({'text': (out.get('text') or '').strip(), 'language': out.get('language_code')})


# ── E&L Pro, policy pages and shop links ──
from pro import init_pro
from legal import legal
from shop import shop
init_pro(app, ['get_outfit', 'restyle'])
app.register_blueprint(legal)
app.register_blueprint(shop)   # /api/shop-links: real product pages for the Shop buttons
app.register_blueprint(alerts_bp)   # /api/owner/*: API status, balances, test alert (owner only)
app.register_blueprint(growth_bp)   # /l/<id> Rate my fit, /b/<id> boards, /api/challenge, /api/track, owner stats
daily.init(client, QUICK_MODEL, STYLE_MODEL)
app.register_blueprint(daily.bp)    # /api/daily-fit, /api/edit, /api/reels, /api/characters, /api/weather
app.register_blueprint(push_bp)     # /manifest.webmanifest, /sw.js, icons, /api/push/*, /api/cron/tick
muses.init(client, STYLE_MODEL)
app.register_blueprint(muses.bp)    # /api/muse/prepare, /api/owner/muse
import identify
identify.init(client, STYLE_MODEL)
app.register_blueprint(identify.bp)  # /api/identify: where each piece of a screenshot or post is from
import story
story.init(client, STYLE_MODEL)
app.register_blueprint(story.bp)     # /api/edit: Ella's Sunday Edit, the story of the week (and /api/owner/story)


if __name__ == '__main__':
    app.run(debug=True, port=5001)
