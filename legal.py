"""The six pages Razorpay checks before it switches on live payments:
pricing, terms, privacy, cancellation & refunds, shipping/delivery and contact.

Your details come from Render's environment so they're set in one place:
  LEGAL_NAME       your full legal name (or your registered business name)
  CONTACT_EMAIL    an inbox you actually read
  CONTACT_PHONE    a phone number for customers
  CONTACT_ADDRESS  a postal address (city, state, PIN at minimum)

These are a solid starting point, not legal advice. Have someone qualified read them
before you depend on them.
"""
import html
import os

from flask import Blueprint, Response

from pro import FREE_PER_DAY, PRICES, PRO_PER_DAY, intl_prices

legal = Blueprint('legal', __name__)
UPDATED = '4 November 2026'


def _v(name, fallback):
    return html.escape((os.environ.get(name) or '').strip() or fallback)


def _ctx():
    return {
        'brand': 'E&amp;L',
        'owner': _v('LEGAL_NAME', '[LEGAL_NAME not set]'),
        'email': _v('CONTACT_EMAIL', '[CONTACT_EMAIL not set]'),
        'phone': _v('CONTACT_PHONE', '[CONTACT_PHONE not set]'),
        'address': _v('CONTACT_ADDRESS', '[CONTACT_ADDRESS not set]'),
        'site': 'fashiongpt-f0vi.onrender.com',
        'm': f"{PRICES['monthly']:,}", 'y': f"{PRICES['yearly']:,}",
        'um': f"{intl_prices()['monthly']:.2f}", 'uy': f"{intl_prices()['yearly']:.2f}",
        'free': FREE_PER_DAY, 'cap': PRO_PER_DAY,
    }


PAGES = {
    'pricing': ('Pricing', """
<p>{brand} has two plans. You can use it from anywhere in the world.</p>
<table>
  <tr><th>Plan</th><th>In India</th><th>Outside India</th><th>What you get</th></tr>
  <tr><td>Free</td><td>₹0</td><td>$0</td><td>{free} looks a day from Ella and Léon, voice styling, Closet, Plan My Day, Dupe, Complete and Brand Match.</td></tr>
  <tr><td>Pro, monthly</td><td>₹{m} a month</td><td>US${um} a month</td><td>Unlimited looks every day (fair use: up to {cap} a day), plus everything in Free.</td></tr>
  <tr><td>Pro, yearly</td><td>₹{y} a year</td><td>US${uy} a year</td><td>The same as monthly Pro, billed once a year.</td></tr>
</table>
<p>Indian prices include all applicable taxes. Outside India, sales tax or VAT is added at checkout where your country requires it, and the price may be shown in your local currency.</p>
<p>Pro renews automatically at the end of each billing period until you cancel. You can cancel at any time from the Pro screen in the app; you keep Pro until the end of the period you've paid for. See <a href="/refunds">Cancellation &amp; Refunds</a>.</p>
<p>In India, payments are processed by Razorpay (UPI, debit or credit card, or net banking where your bank supports recurring payments). Outside India, our online reseller Dodo Payments is the merchant of record for your order and accepts cards and popular wallets.</p>
"""),
    'terms': ('Terms &amp; Conditions', """
<p>These terms apply when you use {brand} at {site} (the "Service"), run by {owner} ("we", "us"). By using the Service you agree to them.</p>
<h2>The Service</h2>
<p>{brand} gives AI-generated outfit suggestions from two stylist characters, Ella and Léon. Suggestions are for inspiration only. Product names, brands, prices, images and links come from third-party sources and can be wrong, out of date or unavailable. We don't sell, stock or ship any clothing, and we aren't responsible for purchases you make from other websites.</p>
<h2>Accounts</h2>
<p>You sign in with your Google account. You're responsible for activity under your account. You must be 18 or older to buy Pro.</p>
<h2>Pro subscriptions</h2>
<ul>
  <li>Pro costs ₹{m} a month or ₹{y} a year in India, and US${um} a month or US${uy} a year elsewhere (plus any local tax), as shown on the <a href="/pricing">Pricing</a> page when you subscribe.</li>
  <li>In India, your subscription renews automatically through a payment mandate you approve with Razorpay, and your bank or UPI app notifies you before each renewal charge. Outside India, Dodo Payments sells Pro to you as merchant of record and charges your saved payment method on each renewal date; their buyer terms also apply to that purchase.</li>
  <li>You can cancel at any time; Pro stays active until the end of the period you've paid for, and no further charges are made.</li>
  <li>"Unlimited" looks are subject to fair use of up to {cap} looks a day per account, to keep the Service running for everyone. Automated or scripted use isn't allowed.</li>
  <li>If we change the price, we'll tell you before your next renewal. You can cancel before it applies.</li>
</ul>
<h2>Acceptable use</h2>
<p>Don't misuse the Service: no attempts to break, overload or reverse-engineer it, no unlawful or abusive content, and no reselling access.</p>
<h2>Content</h2>
<p>The {brand} name, the Ella and Léon characters, and the app's design belong to us or our licensors. You may share looks the Service creates for you for personal, non-commercial use.</p>
<h2>Liability</h2>
<p>The Service is provided "as is". To the extent the law allows, we aren't liable for indirect or consequential losses. Our total liability to you is limited to the amount you paid us in the 3 months before the claim.</p>
<h2>Changes and termination</h2>
<p>We may update these terms; we'll post the new version here with a new date. We may suspend accounts that break these terms. If we close the Service, we'll refund any unused paid time.</p>
<h2>Law</h2>
<p>These terms are governed by the laws of India. Questions: {email}.</p>
"""),
    'privacy': ('Privacy Policy', """
<p>This policy explains what {brand} ({site}), run by {owner}, collects and why. We follow India's Digital Personal Data Protection Act, 2023.</p>
<h2>What we collect</h2>
<ul>
  <li><b>Google sign-in:</b> your name, email address and profile picture, used to run your account and Pro subscription.</li>
  <li><b>Styling details:</b> your quiz answers, occasion, budget and any text or voice request you send, used only to create your looks. Voice recordings are sent for transcription and aren't stored by us. Saved looks and closet photos stay in your browser on your device.</li>
  <li><b>Usage:</b> how many looks you've made today, to apply the daily limit.</li>
  <li><b>Payments:</b> handled by Razorpay (in India) or Dodo Payments (outside India). We receive your subscription and payment IDs and their status; we never see or store your card, UPI or bank details. Dodo Payments also receives your billing country to work out local tax.</li>
  <li><b>Analytics:</b> Google Analytics records anonymous visit statistics.</li>
</ul>
<h2>Who processes it for us</h2>
<p>Anthropic (AI that writes the looks), ElevenLabs (stylist voices, and turning your voice requests into text), Google (sign-in, product image search, analytics), Razorpay and Dodo Payments (payments), Render (hosting) and Neon (database). They process data only to provide their service to us.</p>
<h2>Cookies</h2>
<p>We use one essential cookie to keep you signed in and to count free looks, and analytics cookies from Google Analytics.</p>
<h2>How long we keep it</h2>
<p>Account and subscription records are kept while your account is active and for as long as tax and payment laws require afterwards. Daily look counts are deleted after 7 days.</p>
<h2>Your rights</h2>
<p>You can ask us to show, correct or delete your personal data, or withdraw consent, by emailing {email}. We reply within 7 days. {brand} is meant for people aged 18 and over.</p>
<h2>Contact</h2>
<p>{owner}, {address}. Email {email}.</p>
"""),
    'refunds': ('Cancellation &amp; Refunds', """
<h2>Cancelling</h2>
<p>You can cancel Pro at any time: open the app, tap <b>✦ Pro</b>, then <b>Cancel renewal</b>. You can also email {email}. Cancelling stops all future charges. You keep Pro until the end of the period you've already paid for.</p>
<h2>Refunds</h2>
<p>Because Pro starts working the moment you pay, we don't give refunds for partly used billing periods. We will refund you in full if:</p>
<ul>
  <li>you were charged twice for the same period;</li>
  <li>you were charged after you cancelled; or</li>
  <li>Pro didn't work because of a fault on our side that we couldn't fix within 7 days.</li>
</ul>
<p>Email {email} within 7 days of the charge with the email you sign in with and the payment ID from your receipt. Approved refunds are sent to your original payment method within 5–7 business days. Outside India, refunds are issued by our reseller Dodo Payments, who may also refund you directly under their own buyer terms.</p>
"""),
    'shipping': ('Shipping &amp; Delivery', """
<p>{brand} is a digital service. Nothing physical is shipped.</p>
<p>Pro is delivered instantly online: it's switched on for the Google account you're signed in with as soon as your payment is confirmed, usually within a minute. If it isn't active within 24 hours, email {email} and we'll sort it out.</p>
<p>Clothing and accessories suggested in your looks are sold and shipped by third-party stores, not by us. Their own shipping and return policies apply.</p>
"""),
    'contact': ('Contact Us', """
<p>We're happy to help with your account, Pro subscription, refunds or anything else.</p>
<dl>
  <dt>Name</dt><dd>{owner}</dd>
  <dt>Email</dt><dd>{email}</dd>
  <dt>Phone</dt><dd>{phone}</dd>
  <dt>Address</dt><dd>{address}</dd>
</dl>
<p>We reply within 2 business days.</p>
"""),
}

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · E&amp;L</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400..700;1,6..96,400..700&family=Jost:wght@400;500&display=swap">
<style>
  :root {{ --bg:#F7F0E3; --card:#FBF6EC; --ink:#1E1712; --fg2:#4E4134; --gold:#7E5F27; --line:#E2D5BF; color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  html {{ overflow-x: hidden; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.65 'Jost', 'Futura', system-ui, sans-serif;
         padding: 40px max(16px, env(safe-area-inset-left)) 64px max(16px, env(safe-area-inset-right)); }}
  main {{ max-width: 680px; margin: 0 auto; background: var(--card); border: 1px solid var(--line);
          padding: clamp(22px, 5vw, 44px); }}
  .mark {{ font: 500 12px 'Jost', sans-serif; letter-spacing: .3em; text-transform: uppercase; color: var(--gold); text-decoration: none; }}
  h1 {{ font: 400 clamp(32px, 7vw, 44px)/1.1 'Bodoni Moda', Didot, Georgia, serif; margin: 10px 0 4px; text-wrap: balance; }}
  .updated {{ color: var(--fg2); font-size: 13px; margin: 0 0 22px; }}
  h2 {{ font: 500 20px/1.3 'Bodoni Moda', Didot, Georgia, serif; margin: 28px 0 6px; }}
  p, li, dd {{ color: var(--fg2); }}
  a {{ color: var(--gold); }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 15px; }}
  th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ font-weight: 500; color: var(--ink); }}
  .tbl {{ overflow-x: auto; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; }}
  dt {{ font-weight: 500; }} dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; }}
  nav {{ max-width: 680px; margin: 22px auto 0; display: flex; flex-wrap: wrap; gap: 6px 16px; justify-content: center; font-size: 13px; }}
  nav a {{ color: var(--fg2); }}
</style></head>
<body>
<main>
  <a class="mark" href="/">E&amp;L</a>
  <h1>{title}</h1>
  <p class="updated">Last updated {updated}</p>
  {body}
</main>
<nav>
  <a href="/">Back to E&amp;L</a><a href="/pricing">Pricing</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a>
  <a href="/refunds">Cancellation &amp; Refunds</a><a href="/shipping">Shipping &amp; Delivery</a><a href="/contact">Contact</a>
</nav>
</body></html>"""


def _render(slug):
    title, body = PAGES[slug]
    body = body.format(**_ctx()).replace('<table>', '<div class="tbl"><table>').replace('</table>', '</table></div>')
    return Response(TEMPLATE.format(title=title, updated=UPDATED, body=body), mimetype='text/html')


for _slug in PAGES:
    legal.add_url_rule(f'/{_slug}', f'page_{_slug}', (lambda s=_slug: _render(s)))
