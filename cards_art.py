"""Pictures the server draws itself: the 9:16 Story card and the link preview for a shared look,
and the app icons for the Home Screen. Drawn from the look's text only (never from uploaded images),
in the app's own fonts (fonts/, SIL Open Font License)."""
import io
import os
import unicodedata
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, 'fonts')

CREAM = (247, 240, 227)
PAPER = (251, 246, 236)
INK = (30, 23, 18)
FG2 = (78, 65, 52)
FG3 = (106, 90, 72)
GOLD = (154, 116, 51)
GOLD_INK = (126, 95, 39)
HAIR = (226, 213, 191)


@lru_cache(maxsize=64)
def _font(name, size, weight=None, opsz=None):
    f = ImageFont.truetype(os.path.join(FONTS, name), size)
    try:
        if name.startswith('BodoniModa'):
            f.set_variation_by_axes([weight or 400, opsz or min(96, max(6, size // 2))])
        elif name == 'Jost.ttf':
            f.set_variation_by_axes([weight or 400])
    except Exception:
        pass                       # an older FreeType: the default weight is fine
    return f


def serif(size, weight=500, opsz=None):
    return _font('BodoniModa.ttf', size, weight, opsz)


def serif_i(size, weight=400, opsz=None):
    return _font('BodoniModa-Italic.ttf', size, weight, opsz)


def sans(size, weight=400):
    return _font('Jost.ttf', size, weight)


def script(size):
    return _font('PinyonScript-Regular.ttf', size)


def rupee(size):
    return _font('Rupee.ttf', size)


STAR = '✦'


def clean(text, limit=400):
    """Text that our fonts can draw: no emoji or symbols they lack (the rupee sign and ✦ are drawn separately)."""
    out = []
    for ch in str(text or '')[:limit]:
        if ch in '₹' + STAR:
            out.append(ch)
        elif ch in '\r\n\t':
            out.append(' ')
        elif unicodedata.category(ch) in ('So', 'Sk', 'Cs', 'Co', 'Cn', 'Cc', 'Cf'):
            continue
        else:
            out.append(ch)
    return ' '.join(''.join(out).split())


def _runs(text, font):
    """Split into (piece, font) runs so ₹ comes from the small rupee font."""
    runs, cur = [], ''
    for ch in text:
        if ch == '₹':
            if cur:
                runs.append((cur, font))
                cur = ''
            runs.append((ch, rupee(int(font.size * 0.95))))
        elif ch == STAR:
            if cur:
                runs.append((cur, font))
                cur = ''
            runs.append((ch, None))
        else:
            cur += ch
    if cur:
        runs.append((cur, font))
    return runs


def width(text, font, spacing=0):
    w = 0
    for piece, f in _runs(text, font):
        if f is None:
            w += font.size * 0.8
        else:
            w += f.getlength(piece) + spacing * len(piece)
    return w


def star(draw, cx, cy, r, fill):
    """A four-pointed sparkle, the app's ✦."""
    k = r * 0.28
    draw.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                  (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def text(draw, xy, s, font, fill, spacing=0, anchor_y='top'):
    x, y = xy
    asc = font.getmetrics()[0]
    for piece, f in _runs(s, font):
        if f is None:
            star(draw, x + font.size * 0.4, y + asc * 0.62, font.size * 0.32, fill)
            x += font.size * 0.8
            continue
        dy = asc - f.getmetrics()[0]
        if spacing:
            for ch in piece:
                draw.text((x, y + dy), ch, font=f, fill=fill)
                x += f.getlength(ch) + spacing
        else:
            draw.text((x, y + dy), piece, font=f, fill=fill)
            x += f.getlength(piece)
    return x


def centred(draw, cx, y, s, font, fill, spacing=0):
    text(draw, (cx - width(s, font, spacing) / 2, y), s, font, fill, spacing)


def wrap(s, font, max_w, max_lines):
    words, lines, cur = s.split(), [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if width(trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and width(last + '…', font) > max_w:
            last = last[:-1].rstrip()
        lines[-1] = last.rstrip(',;:—-') + '…'
    return [shorten(l, font, max_w) for l in lines]


def shorten(s, font, max_w):
    if width(s, font) <= max_w:
        return s
    while s and width(s + '…', font) > max_w:
        s = s[:-1]
    return s.rstrip() + '…'


def line_h(font, k=1.18):
    a, d = font.getmetrics()
    return int((a + d) * k)


def mark(draw, cx, y, size, ink=INK, gold=GOLD):
    """E&L, with the ampersand in gold italic like the app's title."""
    e, amp = serif(size, 500, 96), serif_i(size, 400, 96)
    parts = [('E', e, ink), ('&', amp, gold), ('L', e, ink)]
    gap = size * 0.04
    total = sum(f.getlength(p) for p, f, _ in parts) + gap * 2
    x = cx - total / 2
    for p, f, c in parts:
        draw.text((x, y), p, font=f, fill=c)
        x += f.getlength(p) + gap


def _png(img, fmt='PNG'):
    buf = io.BytesIO()
    if fmt == 'JPEG':
        img.convert('RGB').save(buf, 'JPEG', quality=88, optimize=True)
    else:
        img.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def _card_bits(card):
    meta = card.get('meta') or {}
    kind = meta.get('kind') or 'look'
    who = 'Léon' if meta.get('stylist') == 'leon' else 'Ella'
    if kind == 'daily':
        eyebrow = "TODAY'S FIT"
    elif kind == 'edit':
        eyebrow = f"{who.upper()}'S SUNDAY EDIT"
    else:
        occ = clean(meta.get('occasion'), 80)
        eyebrow = ('FOR ' + occ.upper()) if occ else 'STYLED ON E&L'
    items = [i for i in card.get('items') or [] if isinstance(i, dict) and i.get('name')]
    return eyebrow, who, items


def story(card, link_text):
    """1080×1920 Instagram Story card."""
    W, H = 1080, 1920
    img = Image.new('RGB', (W, H), CREAM)
    d = ImageDraw.Draw(img)
    d.rectangle([44, 44, W - 45, H - 45], outline=GOLD, width=3)
    d.rectangle([58, 58, W - 59, H - 59], outline=HAIR, width=1)
    cx = W // 2
    eyebrow, who, items = _card_bits(card)

    mark(d, cx, 120, 132)
    star(d, cx - 250, 205, 10, GOLD)
    star(d, cx + 250, 205, 10, GOLD)
    centred(d, cx, 318, 'RATE MY FIT', sans(30, 500), GOLD_INK, spacing=9)

    y = 410
    centred(d, cx, y, shorten(clean(eyebrow, 90), sans(26, 500), 820), sans(26, 500), FG2, spacing=4)
    y += 70
    title_f = serif(88, 500, 96)
    for line in wrap(clean(card.get('look_name'), 120) or 'My look', title_f, 860, 3):
        centred(d, cx, y, line, title_f, INK)
        y += line_h(title_f, 1.05)
    y += 26
    note = clean(card.get('ella_note'), 600)
    if note:
        note_f = serif_i(36, 400, 28)
        for line in wrap('“' + note + '”', note_f, 840, 3):
            centred(d, cx, y, line, note_f, GOLD_INK)
            y += line_h(note_f, 1.2)
        y += 10
    # the pieces, as many as fit above the footer
    footer_top = H - 470
    y += 18
    d.line([120, y, W - 120, y], fill=HAIR, width=2)
    star(d, cx, y, 12, GOLD)
    y += 38
    name_f, sub_f = serif(38, 500, 36), sans(26, 400)
    row = line_h(name_f, 1.0) + line_h(sub_f, 1.0) + 22
    shown = 0
    for it in items:
        if y + row > footer_top or shown >= 6:
            break
        text(d, (120, y), shorten(clean(it.get('name'), 120), name_f, 840), name_f, INK)
        y += line_h(name_f, 1.0)
        sub = ' · '.join(p for p in (clean(it.get('category'), 30).upper(), clean(it.get('brand'), 60),
                                     clean(it.get('price'), 30)) if p)
        text(d, (120, y), shorten(sub, sub_f, 840), sub_f, FG2)
        y += line_h(sub_f, 1.0) + 22
        shown += 1
    if len(items) > shown:
        text(d, (120, y - 6), f'+ {len(items) - shown} more on the link', sans(26, 400), FG3)

    # footer: the vote prompt, Ella's signature and the link
    fy = H - 440
    d.rounded_rectangle([150, fy, W - 150, fy + 130], radius=65, fill=INK)
    centred(d, cx, fy + 26, 'TAP THE LINK', sans(30, 500), PAPER, spacing=8)
    centred(d, cx, fy + 74, 'and rate my fit', serif_i(34, 400, 28), (226, 196, 140))
    sig = f'Styled by {who}'
    sf = script(76)
    sw = sf.getlength(sig)
    d.text((cx - sw / 2 - 20, fy + 150), sig, font=sf, fill=GOLD)
    star(d, cx + sw / 2 + 14, fy + 205, 16, GOLD)
    centred(d, cx, H - 128, shorten(link_text, sans(28, 400), 860), sans(28, 400), FG2)
    return _png(img)


def preview(card):
    """1200×630 picture shown when the link is pasted in WhatsApp, iMessage, X..."""
    W, H = 1200, 630
    img = Image.new('RGB', (W, H), CREAM)
    d = ImageDraw.Draw(img)
    d.rectangle([24, 24, W - 25, H - 25], outline=GOLD, width=3)
    eyebrow, who, items = _card_bits(card)
    x0 = 72
    mark(d, x0 + 70, 58, 58)
    text(d, (x0 + 164, 84), 'RATE MY FIT', sans(22, 500), GOLD_INK, spacing=6)
    y = 170
    text(d, (x0, y), shorten(clean(eyebrow, 90), sans(20, 500), 520), sans(20, 500), FG2, spacing=3)
    y += 44
    tf = serif(54, 500, 72)
    for line in wrap(clean(card.get('look_name'), 120) or 'My look', tf, 540, 3):
        text(d, (x0, y), line, tf, INK)
        y += line_h(tf, 1.04)
    note = clean(card.get('ella_note'), 300)
    if note:
        y += 14
        nf = serif_i(26, 400, 24)
        room = max(0, min(3, int((H - 112 - y) // line_h(nf, 1.2))))
        for line in (wrap('“' + note + '”', nf, 540, room) if room else []):
            text(d, (x0, y), line, nf, GOLD_INK)
            y += line_h(nf, 1.2)
    text(d, (x0, H - 92), f'Styled by {who}', script(44), GOLD)
    d.line([660, 70, 660, H - 70], fill=HAIR, width=2)
    star(d, 660, H // 2, 10, GOLD)
    py = 92
    nf2, bf = serif(30, 500, 28), sans(21, 400)
    for it in items[:6]:
        if py + line_h(nf2, 1.0) + line_h(bf, 1.0) > H - 124:
            break
        text(d, (700, py), shorten(clean(it.get('name'), 100), nf2, 440), nf2, INK)
        py += line_h(nf2, 1.0)
        sub = ' · '.join(p for p in (clean(it.get('brand'), 50), clean(it.get('price'), 24)) if p)
        text(d, (700, py), shorten(sub, bf, 440), bf, FG2)
        py += line_h(bf, 1.0) + 18
    d.rounded_rectangle([700, H - 104, 1130, H - 52], radius=26, fill=INK)
    t = 'TAP TO VOTE'
    tw = width(t, sans(22, 500), 5)
    text(d, (915 - tw / 2, H - 92), t, sans(22, 500), PAPER, spacing=5)
    return _png(img, 'JPEG')


@lru_cache(maxsize=8)
def icon(size, maskable=False, light=False):
    """Home Screen icon: E&L in cream and gold on ink (or ink on cream)."""
    bg, fg = (CREAM, INK) if light else (INK, CREAM)
    img = Image.new('RGB', (size, size), bg)
    d = ImageDraw.Draw(img)
    s = size * (0.30 if maskable else 0.36)
    f = serif(int(s), 500, 96)
    amp = serif_i(int(s), 400, 96)
    gap = s * 0.03
    total = f.getlength('E') + amp.getlength('&') + f.getlength('L') + gap * 2
    x = (size - total) / 2
    asc, desc = f.getmetrics()
    y = (size - (asc + desc)) / 2 - s * 0.04
    for p, ft, c in (('E', f, fg), ('&', amp, (201, 162, 90)), ('L', f, fg)):
        d.text((x, y), p, font=ft, fill=c)
        x += ft.getlength(p) + gap
    if not maskable:
        inset = size * 0.07
        d.rectangle([inset, inset, size - inset, size - inset], outline=(154, 116, 51), width=max(2, size // 128))
    return _png(img)


@lru_cache(maxsize=2)
def badge(size=96):
    """Android's small status-bar icon: only the shape counts, so white on transparent."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = serif(int(size * 0.5), 700, 96)
    t = 'E&L'
    w = f.getlength(t)
    asc, desc = f.getmetrics()
    d.text(((size - w) / 2, (size - asc - desc) / 2), t, font=f, fill=(255, 255, 255, 255))
    return _png(img)
