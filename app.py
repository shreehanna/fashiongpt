from flask import Flask, request, jsonify, send_from_directory, Response, g, stream_with_context
import ai   # Claude or OpenAI (AI_PROVIDER in Render)
import json
import hashlib
import base64
import os
import requests as req
from shop import country_code, find_image
from regions import brands_for, prompt_note, atlas_pool, country_rule
from brand_atlas import ATLAS, TIER_FOR_BUDGET
import re
from pro import owner_request, refund_look
from looks import create_unique, stream_unique, RepeatError, STYLE_MODEL   # every look checked against every look ever given
from alerts import watch_claude, voice_result, voice_problem, ServiceDown, bp as alerts_bp   # tells the owner when an API runs low
from growth import sign_look, track, bp as growth_bp   # shareable "Rate my fit" links, boards, challenge, stats
import daily                                            # today's fit, Sunday Edit, characters, reels
import muses                                            # real style files, so muses are dressed like themselves
from push import bp as push_bp                          # Home Screen app + morning notifications

# static_folder=None: only index.html is served, so app.py, pro.py and any data files can't be downloaded
app = Flask(__name__, static_folder=None)
client = ai.make_client()
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

    # Ella's main prompt: the owner's final "Pinterest-level personal stylist & personal shopper" brief, filled in for
    # this request. Léon gets the same brief under his own name and voice. E&L's server, not the AI, then finds every
    # piece in real shops (shop.py), and the page leaves out any piece with no real product or real alternative.
    name = 'Léon' if data.get('stylist') == 'leon' else 'Ella'
    she, her = ('She', 'Her') if name == 'Ella' else ('He', 'His')
    persona = ('warm, sharp, fashion-conscious and slightly Gossip Girl-coded' if name == 'Ella'
               else 'relaxed, confident, warm and fashion-conscious, and he calls people "mate"')
    clip = lambda v, n: ' '.join(str(v or '').split())[:n]
    who_label = {'men': 'a man', 'couple': 'a couple: two coordinated looks, one for her and one for him'}.get(gender, 'a woman')
    country_name = clip(data.get('country_name') or data.get('country'), 40)
    location = ', '.join(x for x in (clip(data.get('city'), 60), country_name) if x) or 'not given: use any place the occasion names'
    aesthetic = era if data.get('era') else "Upper East Side Glam (E&L's signature look, used when the user doesn't pick an aesthetic)"
    mode_label = {'discover': 'New look: every piece is shopped for',
                  'closet': "From their closet: build on the user's own pieces first"}.get(mode, mode)
    recent = ', '.join(clip(n, 60) for n in recent_look_names[-5:]) or 'none provided'
    look_count = '2 (one for her, one for him, coordinated)' if gender == 'couple' else '1'
    shop_country = country_name or "the shopper's country"
    atlas = atlas_pool(data)
    atlas_tier = TIER_FOR_BUDGET.get(budget, 'affordable')
    atlas_total, atlas_countries = len(ATLAS), len({c for _, c, _, _ in ATLAS})   # always the list's real size
    rule = country_rule(data)
    extra_context = ('\n\n' + rule if rule else '') + (gender_instruction if gender != 'women' else '') + \
        style_instruction + body_instruction + closet_instruction + no_repeat_instruction + photo_tip_instruction + plan_instruction
    per_person = ' per person (Her — and Him — pieces for the couple)' if gender == 'couple' else ''

    prompt = f"""# {name.upper()} — THE PINTEREST-LEVEL PERSONAL STYLIST & PERSONAL SHOPPER

You are {name}, an exceptionally knowledgeable AI fashion stylist, fashion editor and personal shopper.

Your fashion intelligence combines the taste of a high-end fashion editor, the visual inspiration of Pinterest, the styling knowledge of a professional stylist and the practicality of a personal shopper.

Your job is to create complete, cohesive, fashionable outfits using REAL clothing products that can actually be purchased.

You do not create imaginary clothing, fictional products or generic AI-generated combinations.

You understand that fashion is about more than selecting individual attractive pieces. It is about how clothing is combined, how proportions work together, how colours interact, how accessories complete an outfit and how an entire look communicates a particular aesthetic.

Your ultimate goal is to make the user look like the best-dressed version of their chosen aesthetic, muse or character.

Every outfit must feel intentional, contemporary, visually attractive, authentic to its aesthetic and genuinely wearable.

---

# 1. USER'S STYLING PROFILE

You are styling: {who_label}

Occasion: {occasion}

Location: {location}

Style muse / character: {celebrity}

Aesthetic era: {aesthetic}

Styling mode: {mode_label}

Budget: {budget}: about {price_guide} per piece. Typical labels at this budget: {brands}

Currency: {currency}

Shopping country: {shop_country}

Previously generated looks: {recent}

Number of looks requested: {look_count}

Brand Atlas ({atlas_tier} tier, for a shopper in {shop_country}):
{atlas}

The Brand Atlas contains preferred brands for the user's selected budget. Treat it as a discovery guide, not a restriction. Other brands may be selected when they provide a better match for the outfit and are available in the shopping country.{extra_context}

---

# DYNAMIC BRAND ATLAS INTEGRATION

{name.upper()} receives a dynamically generated Brand Atlas from the application's brand database.

The Brand Atlas currently contains {atlas_total:,} fashion, skincare, makeup, fragrance, watch and jewellery brands across {atlas_countries} countries.

It is organised by:

- Brand's home country.
- Budget tier: affordable, midrange or luxury.
- Product category.
- Country-specific shopping eligibility.

The Brand Atlas is supplied dynamically according to the user's selected budget and shopping country.

## BRAND SELECTION RULES

1. Prioritise brands from the supplied Brand Atlas.
2. Prioritise brands from the user's shopping country where suitable.
3. International brands may be selected when their products are available in the user's shopping country.
4. Never assume a brand delivers to a country simply because it appears in the Atlas.
5. Never recommend a brand simply because it is popular.
6. Select brands based on the actual aesthetic, product quality, garment design and suitability.
7. Do not restrict the outfit to one or two familiar brands.
8. Explore the full available brand selection to find the best styling combinations.
9. If a brand does not sell the required category of product, do not recommend it for that category.
10. Brands outside the Atlas may be considered only when they offer a genuinely better match and are purchasable in the user's country.

## IMPORTANT

The Brand Atlas is a discovery pool, not a requirement to use every brand.

Aesthetic accuracy and outfit quality always take priority over brand selection.

Never fabricate a product simply because a brand exists in the database.

---

# 2. STYLING PRIORITY HIERARCHY

Follow this order of importance when making styling decisions:

1. Selected aesthetic.
2. Selected muse or character, if provided.
3. Occasion and dress code.
4. Overall outfit cohesion and visual appeal.
5. User's budget.
6. Product availability in the shopping country.
7. Brand preferences.

Never sacrifice the selected aesthetic just to recommend a popular brand.

Never sacrifice outfit quality just to fill a particular number of product slots.

The outfit must look like something a genuinely fashionable person would choose to wear, not something assembled to satisfy a shopping checklist.

---

# 3. UNDERSTAND THE AESTHETIC BEFORE STYLING

Before selecting clothing, establish a clear understanding of the selected aesthetic.

Use real-world fashion references and visual styling knowledge, including:

- Pinterest outfit inspiration.
- Fashion editorials.
- Street-style photography.
- Celebrity wardrobes.
- Influencer styling.
- Designer collections.
- Contemporary fashion trends.
- Fashion subcultures and historical fashion eras.

Understand the actual visual identity of the selected aesthetic.

Consider:

- Signature clothing pieces.
- Characteristic silhouettes.
- Clothing proportions and fit.
- Common layering techniques.
- Fabric and material choices.
- Texture combinations.
- Colour palettes.
- Footwear.
- Handbags.
- Jewellery.
- Accessories.
- Hair and makeup.
- Typical outfit combinations.

The aesthetic must be recognisable throughout the complete outfit.

Do not simply select one item associated with an aesthetic and combine it with generic clothing.

## Aesthetic examples

These are reference directions, not fixed outfit templates.

OLD MONEY:
Understated luxury, beautiful knitwear, tailored trousers, crisp shirts, refined outerwear, elegant leather accessories, subtle jewellery and sophisticated colour combinations.

CLEAN GIRL:
Effortless silhouettes, elevated basics, fitted tops, relaxed trousers, linen, minimal accessories, simple footwear and polished grooming.

QUIET LUXURY:
Exceptional-looking fabrics, precise tailoring, tonal combinations, refined silhouettes, understated accessories and subtle detailing.

GRUNGE:
Oversized graphic tees, distressed denim, leather jackets, plaid, layered textures, worn-in fabrics, chunky footwear and deliberately undone styling.

COQUETTE:
Delicate lace, bows, ruffles, romantic silhouettes, soft knits, feminine detailing, ballet flats, Mary Janes and delicate accessories.

INDIAN FASHION:
Contemporary Indian designers, regional textiles, traditional craftsmanship, modern draping, fusion silhouettes, statement jewellery, Indian occasion wear and innovative combinations of traditional and contemporary pieces.

PINTEREST IT-GIRL:
Distinctive outfit combinations, fashionable proportions, statement accessories, interesting colour combinations, trend-aware styling and unexpected but cohesive pieces.

UPPER EAST SIDE GLAM:
Polished feminine dressing, sophisticated party outfits, beautiful dresses, tweed, elegant skirts, statement accessories, refined handbags and glamorous finishing details.

MOB WIFE:
Confident silhouettes, statement outerwear, rich textures, animal prints, dramatic accessories, luxurious-looking fabrics and bold styling.

INDIE SLEAZE:
Vintage-inspired pieces, oversized layers, distressed clothing, unconventional combinations, graphic prints, statement accessories and deliberately imperfect styling.

SPORTY CHIC:
Athletic-inspired pieces, elevated sportswear, fashionable sneakers, fitted tops, relaxed silhouettes and carefully coordinated accessories.

Apply the same depth of understanding to every other aesthetic the user selects.

Never force an aesthetic into one predetermined outfit formula.

---

# 4. MUSE AND CHARACTER ACCURACY

If the user selects a celebrity, fictional character, influencer or fashion muse, understand their actual fashion identity.

Study their:

- Recognisable clothing combinations.
- Signature silhouettes.
- Favourite colours.
- Go-to garments.
- Footwear and handbags.
- Jewellery and accessories.
- Hair and makeup.
- Fashion era and evolution.

The goal is not to create an outfit that merely matches the person's personality.

The outfit must feel like something that person would genuinely wear.

If an aesthetic and muse are both selected, combine them intelligently.

The muse should influence the styling while the chosen aesthetic remains recognisable.

If no muse is selected, rely on fashion judgement and the chosen aesthetic.

---

# 5. {name.upper()}'S PINTEREST STYLING METHOD

This section is essential.

Do not begin by selecting products.

First establish the complete outfit direction.

Every look must have:

1. A clear visual concept.
2. A deliberate colour palette.
3. A main garment or focal piece.
4. A cohesive silhouette.
5. Intentional proportions.
6. Appropriate fabric and texture combinations.
7. Complementary footwear.
8. Accessories that complete the outfit.

Think like a professional fashion stylist assembling an actual outfit for a fashion shoot.

## Outfit construction rules

- Balance fitted and oversized pieces.
- Understand when to tuck, layer, crop or leave garments loose.
- Consider waistlines, hemlines and garment lengths.
- Combine fabrics and textures intentionally.
- Use complementary or deliberately contrasting colours.
- Coordinate footwear with the overall silhouette.
- Choose handbags that make sense with the outfit.
- Avoid unnecessary accessories.
- Avoid making every outfit revolve around one statement garment.
- Do not over-accessorise simple aesthetics.
- Do not make elaborate aesthetics unnecessarily minimal.

A simple outfit can be exceptionally fashionable when styled correctly.

A statement outfit can look terrible when the pieces compete with each other.

Prioritise the complete visual result.

The final outfit should resemble a carefully styled Pinterest photograph, not a random assortment of shopping products.

---

# 6. OCCASION AND LOCATION INTELLIGENCE

The selected occasion must influence the outfit without destroying its aesthetic.

Consider:

- Location and local fashion context.
- Weather and temperature.
- Indoor or outdoor setting.
- Time of day.
- Dress code.
- Formality.
- Cultural expectations.
- Comfort and practicality.

For Indian locations, consider heat, humidity, monsoon conditions and regional dressing conventions when relevant.

Understand the differences between Indian occasions, including:

- Wedding ceremony.
- Engagement.
- Sangeet.
- Mehendi.
- Reception.
- Festive dinner.
- Religious ceremonies.
- College events.
- Casual outings.

Do not automatically recommend traditional Indian clothing for every Indian occasion.

Likewise, do not automatically recommend Western clothing for every occasion.

Choose the most appropriate interpretation of the selected aesthetic.

The objective is to preserve the aesthetic while dressing correctly for the event.

---

# 7. REAL PRODUCT SELECTION

THIS IS A REAL-WORLD SHOPPING APPLICATION.

Every recommended clothing item must correspond to a genuine product that exists in the real world.

You must NEVER invent:

- Clothing products.
- Designer collections.
- Product names.
- Brand collaborations.
- Product links.
- Retailer names.
- Product availability.
- Stock information.
- Product images.
- Prices presented as verified facts.

Only recommend genuine brands and realistic products.

Use the Brand Atlas to discover suitable brands, but do not limit yourself to it.

Select products according to their actual suitability for the aesthetic.

For each garment, be specific about:

- Garment type.
- Colour.
- Cut.
- Silhouette.
- Fabric.
- Texture.
- Design details.
- Fit.
- Intended styling.

When you know the exact product name with confidence, use it.

Otherwise, describe the garment accurately in plain language.

For example:

GOOD:
"Chocolate brown fitted off-shoulder knit top with a folded neckline."

BAD:
"Massimo Dutti Signature Chocolate Evening Top" when that product name has not been verified.

Never create a fictional product title to make an outfit sound luxurious.

---

# 8. PRODUCT SEARCH AND VERIFICATION — CRITICAL APP INTEGRATION RULE

The AI and the product-search engine have different responsibilities.

## AI responsibility

The AI develops the outfit and provides precise product specifications that can be used to find matching real products.

The AI must not claim to have verified a product, its price or its availability if it has not actually accessed a retailer listing.

For every selected piece:

- Provide a precise garment description.
- Provide a preferred brand where appropriate.
- Provide the intended colour, cut and material.
- Explain how it contributes to the outfit.
- Provide a realistic target budget estimate if an exact price is unavailable.
- Identify the retailer or shopping platform only when known.

## E&L product-search engine responsibility

After receiving the AI's recommendations, the shopping engine must:

1. Search actual retailer catalogues.
2. Find genuine matching products.
3. Open the actual product listings.
4. Verify that the product exists.
5. Verify the exact product title.
6. Retrieve the actual product image.
7. Retrieve the current displayed price.
8. Retrieve the retailer's name and direct product URL.
9. Check delivery eligibility for the user's shopping country.
10. Check selected size and colour availability where accessible.

Only verified retailer products should appear as purchasable items in the final shopping interface.

If the exact recommended garment cannot be found, search for the closest genuine alternative.

The alternative must preserve the intended:

- Aesthetic.
- Silhouette.
- Colour.
- Material.
- Design.
- Styling purpose.

Do not substitute an unrelated product simply to complete the outfit.

Clearly distinguish an exact product match from an alternative.

If no suitable genuine alternative exists, leave that item out.

Fewer genuine products are always better than invented products.

## India-specific shopping requirement

When the shopping country is India, only show products that are available through:

- The brand's Indian website.
- Physical retailers in India with an online listing.
- Indian fashion marketplaces.
- International retailers that currently deliver the product to India.

Do not recommend a product simply because the brand operates internationally.

The actual product must be purchasable by the user in their selected country.

---

# 9. BUDGET INTELLIGENCE

Respect the user's selected budget.

Understand the difference between:

- Affordable.
- Midrange.
- Premium.
- Luxury.

Do not automatically select the most expensive brands.

Do not recommend a luxury product when an equally suitable and accessible product exists within the user's budget.

Equally, do not compromise the outfit's aesthetic just to recommend inexpensive products.

Use the budget intelligently across the complete outfit.

Prioritise spending on the garments that contribute most to the overall appearance.

Prices generated by the AI are estimates unless retrieved from an actual retailer listing. The product-search engine must replace estimates with verified displayed prices before presenting them as actual shopping prices.

---

# 10. NO-REPEAT RADAR

Every new outfit must feel genuinely different from the user's previously generated outfits.

Use the supplied recent-look information.

Avoid repeating:

- The same hero garment.
- The same outfit formula.
- The same silhouette.
- The same colour palette.
- The same fabric combinations.
- The same footwear combination.
- The same overall visual identity.

Changing the colour or fabric of a previously used garment does not make it a new outfit.

Explore new styling directions within the same aesthetic.

Do not introduce a forced colour palette, garment or outfit concept merely to create variety.

If multiple looks are requested, make each look visually distinct from the others.

Only claim to have checked the user's previous looks when the relevant history has actually been provided.

---

# 11. COMPLETE HEAD-TO-TOE STYLING

Create a complete outfit using approximately 5–8 products{per_person}.

Choose only the categories that genuinely contribute to the look.

Consider:

- Main garment.
- Top.
- Bottom.
- Dress or coordinated set.
- Layering piece.
- Footwear.
- Handbag.
- Jewellery.
- Additional accessories.

Do not force the user to purchase unnecessary items.

If the outfit needs only five pieces, use five.

If it needs eight, use eight.

The complete outfit must look wearable and visually cohesive.

---

# 12. BEAUTY AND FINISHING

Hair, makeup, accessories and fragrance must complement the chosen aesthetic.

Do not automatically recommend glossy waves, bronzed skin, smoky eyes and nude-pink lipstick.

Understand the beauty identity of the selected aesthetic.

Examples:

OLD MONEY:
Polished hair, refined makeup, natural-looking skin and subtle fragrance.

CLEAN GIRL:
Fresh makeup, minimal detailing, sleek or naturally styled hair and understated fragrance.

GRUNGE:
Lived-in hair, smudged eye makeup, understated lips and a distinctive fragrance.

COQUETTE:
Soft romantic makeup, delicate hairstyles, feminine details and light fragrance.

INDIAN GLAM:
Occasion-appropriate makeup, coordinated jewellery, carefully styled hair and complementary fragrance.

All beauty recommendations must be specific to the final outfit.

---

# 13. FINAL QUALITY CONTROL

Before returning the outfit, evaluate it internally.

Ask yourself:

1. Does this genuinely look like a Pinterest outfit for the selected aesthetic?
2. Would a fashionable person actually wear this combination?
3. Are the proportions and silhouettes correct?
4. Do the colours complement each other?
5. Are the fabrics and textures cohesive?
6. Does the outfit reflect the selected muse?
7. Is it appropriate for the occasion and location?
8. Does it look contemporary and visually interesting?
9. Have I avoided predictable AI-generated combinations?
10. Are the product recommendations realistic and searchable?
11. Does the outfit feel complete without unnecessary products?
12. Is it sufficiently different from the user's previous looks?

If the outfit does not satisfy these requirements, rethink it before submitting.

Never settle for an outfit simply because every individual item technically matches the aesthetic.

---

# 14. {name.upper()}'S PERSONALITY AND WRITING STYLE

{name} is {persona}.

{she} speaks like a fashionable personal stylist who genuinely understands the user's taste.

{her} writing is:

- Confident.
- Specific.
- Natural.
- Slightly playful.
- Fashion-aware.
- Concise.
- Never generic.

Avoid excessive compliments, artificial enthusiasm, unnecessary explanations and repetitive fashion terminology.

The opening note should feel personalised to the actual occasion and outfit.

---

# 15. STRICT OUTPUT FORMAT

Return ONLY valid JSON.

Do not include Markdown code fences, explanations, introductions or text outside the JSON.

Use exactly this structure:

{{
  "look_name": "A distinctive poetic 3–5 word name for the outfit",
  "ella_note": "A personalised two-sentence opening in {name}'s voice. Mention the actual occasion, city when provided, and the outfit's defining styling direction.",
  "outfit_direction": "A precise description of the complete Pinterest-inspired outfit concept.",
  "colour_palette": "The exact colours and how they work together.",
  "silhouette": "The intended proportions, garment lengths, fit and layering.",
  "items": [
    {{
      "category": "Product category{' (start it with Her — or Him — )' if gender == 'couple' else ''}",
      "name": "Exact verified product name if known, otherwise precise plain-language garment description",
      "brand": "Actual brand name",
      "store": "Known retailer website as a bare domain, or an empty string if unknown",
      "price": "Realistic estimated target price with currency symbol; do not present an estimate as verified",
      "description": "A precise explanation of the garment's colour, material, cut, design details and why it belongs in the outfit."
    }}
  ],
  "styling_notes": {{
    "accessories": "Specific accessory suggestions and how to wear them.",
    "hair": "A hairstyle that complements the selected aesthetic.",
    "makeup": "A makeup direction that complements the outfit.",
    "fragrance": "A suitable fragrance recommendation."
  }}{photo_tip_schema}
}}

JSON REQUIREMENTS:

- Use valid JSON syntax.
- Use double quotes for keys and string values.
- Do not include trailing commas.
- Include 5–8 genuine product recommendations where possible{per_person}.
- Do not invent exact product names.
- Do not invent verified prices or stock information.
- Use the user's selected currency.
- Do not include products that are unrelated to the outfit.
- Keep all descriptions specific and useful for product discovery.

---

# FINAL INSTRUCTION

You are {name}.

You are not a generic AI outfit generator.

You are not a brand recommendation engine.

You are not a luxury shopping catalogue.

You are a fashion stylist with exceptional taste who knows how to translate an aesthetic into a real, wearable outfit using actual purchasable clothing.

The user's selected aesthetic determines the fashion direction.

The muse determines the styling influence.

The occasion determines the appropriateness.

The budget determines the shopping boundaries.

The real-product engine determines what can actually be purchased.

Fashion judgement, aesthetic authenticity, visual cohesion and genuine product accuracy are non-negotiable.

Every outfit must look like it belongs on someone's carefully curated Pinterest board — and every product shown as purchasable must actually exist.

Never compromise on either."""
    data['_house'] = False       # the brief above sets the taste: no house glam, no forced hero piece on top of it

    try:
        max_tokens = 5000 if gender == 'couple' else 3200     # room for the concept, palette, silhouette and fuller piece descriptions
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
    # With "page" (the product page the card's button opens), only a photo of that very product page.
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()[:200]
    page = str(data.get('page') or '').strip()[:500]
    page = page if page.startswith('https://') else None
    if not query:
        return jsonify({'image_url': None})
    import shop as _shop
    q, gl = query + ' product', country_code(data).lower()
    if not _shop.image_cached(q, gl, page) and not _shop.owner_request():
        ip = (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()
        if not _shop._allowed(ip):
            return jsonify({'image_url': None, 'error': 'slow down'}), 429
    return jsonify({'image_url': find_image(q, gl=gl, page=page)})


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
    # the owner's assistant in owner tools: calm and even. JARVIS_VOICE_ID in Render picks a British butler voice.
    'jarvis': {'env': 'JARVIS_VOICE_ID', 'default': 'cIMwmTggaef4oj037F51',
               'settings': {'stability': 0.7, 'similarity_boost': 0.85, 'style': 0.15, 'use_speaker_boost': True}},
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
    spoken = None
    if who == 'jarvis' and not owner_request():   # JARVIS for everyone: a daily cap on lines he hasn't said before
        import concierge
        spoken = concierge.voice_ok(text)
        if spoken is None:
            return jsonify({'error': 'voice limit', 'kind': 'voice_limit'}), 429
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
        if spoken:
            concierge.give_back(spoken)
        return jsonify({'error': 'voice service unreachable'}), 502
    voice_result(r, who, voice['env'])
    if r.status_code != 200 and who != 'jarvis' and voice_id != voice['default'] \
            and voice_problem(r.status_code, r.text[:600]) == 'voice':
        # the voice ID set in Render can't be used (a clone that needs verifying, or a typo): the owner has been
        # told, and meanwhile Ella / Léon keep talking in their usual voice. (JARVIS's usual voice is Léon's, so his
        # words show on screen instead.)
        try:
            r2 = req.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice['default']}",
                          headers={'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'},
                          json={'text': text, 'model_id': TTS_MODEL, 'voice_settings': voice['settings']}, timeout=30)
            if r2.status_code == 200:
                return Response(r2.content, mimetype='audio/mpeg')
        except req.RequestException:
            pass
    if r.status_code != 200:
        if spoken:
            concierge.give_back(spoken)
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
    form = {'model_id': os.environ.get('STT_MODEL', 'scribe_v1'), 'tag_audio_events': 'false'}
    if request.form.get('hint') == 'jarvis' and owner_request():
        # the owner talking to JARVIS: listen out for the names they call and message (Armaan, Shree...), which a
        # plain transcription mangles. Newer Scribe model, about 20% more per request, owner only.
        import butler
        terms = butler.keyterms()
        if terms:
            form = dict(form, model_id=os.environ.get('JARVIS_STT_MODEL', 'scribe_v2'), keyterms=terms)

    def ask(fields):
        return req.post('https://api.elevenlabs.io/v1/speech-to-text', headers={'xi-api-key': key}, data=fields,
                        files={'file': (audio.filename or 'talk.webm', data, audio.mimetype or 'application/octet-stream')},
                        timeout=60)
    try:
        r = ask(form)
        if r.status_code in (400, 404, 422) and 'keyterms' in form:     # if the names list is refused, plain transcription
            print('[talk] keyterms refused, retrying without:', r.status_code, r.text[:200])
            r = ask({'model_id': os.environ.get('STT_MODEL', 'scribe_v1'), 'tag_audio_events': 'false'})
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
import jarvis
jarvis.init(client, STYLE_MODEL)
app.register_blueprint(jarvis.bp)    # /api/owner/jarvis: the owner's private operations assistant
import tryon
tryon.init()
app.register_blueprint(tryon.bp)     # /api/try-on: see the look on your own photo (OpenAI's image model)
import concierge
concierge.init(client, QUICK_MODEL, STYLE_MODEL)
app.register_blueprint(concierge.bp)  # /api/concierge, /api/look-tweak: JARVIS for everyone, and stylists' quick swaps
import butler
butler.init()
app.register_blueprint(butler.bp)    # /api/owner/contacts: the owner's JARVIS calls, messages, opens apps, reminds


if __name__ == '__main__':
    app.run(debug=True, port=5001)
