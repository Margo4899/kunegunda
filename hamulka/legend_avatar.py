"""Открытка «Легенда хамства»: аватарка победителя из ВК в золотой рамке с короной."""
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONT_SERIF = os.path.join(ASSETS, "fonts", "DejaVuSerif-Bold.ttf")
FONT_SANS = os.path.join(ASSETS, "fonts", "DejaVuSans-Bold.ttf")

W, H = 900, 1150
GOLD = (255, 205, 70)
GOLD_DARK = (176, 118, 20)
GOLD_LIGHT = (255, 240, 170)
PURPLE_TOP = (52, 10, 78)
PURPLE_BOTTOM = (14, 2, 28)
RUBY = (220, 30, 60)
EMERALD = (30, 190, 120)
SAPPHIRE = (60, 120, 255)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _background():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(PURPLE_TOP, PURPLE_BOTTOM)))
    # золотые лучи из-за аватарки
    rays = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rays)
    cx, cy = W // 2, 520
    for i in range(24):
        a1 = i * 2 * math.pi / 24
        a2 = a1 + math.pi / 48
        r = 900
        rd.polygon([(cx, cy), (cx + r * math.cos(a1), cy + r * math.sin(a1)),
                    (cx + r * math.cos(a2), cy + r * math.sin(a2))], fill=(255, 200, 80, 38))
    rays = rays.filter(ImageFilter.GaussianBlur(3))
    img.paste(rays, (0, 0), rays)
    # свечение
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 330, cy - 330, cx + 330, cy + 330], fill=(255, 190, 60, 110))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img.paste(glow, (0, 0), glow)
    return img


def _sparkle(d, x, y, r, color=(255, 245, 200)):
    d.polygon([(x, y - r), (x + r * 0.22, y - r * 0.22), (x + r, y), (x + r * 0.22, y + r * 0.22),
               (x, y + r), (x - r * 0.22, y + r * 0.22), (x - r, y), (x - r * 0.22, y - r * 0.22)], fill=color)


def _pepper(layer, x, y, scale=1.0, angle=0):
    """Маленький перчик чили."""
    p = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(p)
    d.ellipse([22, 40, 92, 72], fill=(225, 30, 35))
    d.polygon([(88, 48), (112, 62), (88, 66)], fill=(225, 30, 35))
    d.ellipse([30, 44, 70, 54], fill=(255, 120, 110))
    d.rectangle([10, 50, 26, 60], fill=(40, 150, 50))
    d.line([(12, 55), (4, 40)], fill=(40, 150, 50), width=5)
    p = p.rotate(angle, expand=True, resample=Image.BICUBIC)
    p = p.resize((int(p.width * scale), int(p.height * scale)), Image.LANCZOS)
    layer.paste(p, (int(x - p.width / 2), int(y - p.height / 2)), p)


def _crown(size=360):
    """Золотая корона с камнями."""
    w, h = size, int(size * 0.72)
    c = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(c)
    base_top, base_bot = int(h * 0.62), int(h * 0.92)
    pts = [(int(w * 0.06), base_top), (int(w * 0.02), int(h * 0.22)), (int(w * 0.27), int(h * 0.46)),
           (int(w * 0.5), int(h * 0.04)), (int(w * 0.73), int(h * 0.46)), (int(w * 0.98), int(h * 0.22)),
           (int(w * 0.94), base_top)]
    d.polygon(pts, fill=GOLD, outline=GOLD_DARK)
    # блик
    d.polygon([(int(w * 0.5), int(h * 0.1)), (int(w * 0.56), int(h * 0.44)), (int(w * 0.5), int(h * 0.55)),
               (int(w * 0.45), int(h * 0.44))], fill=GOLD_LIGHT)
    d.rounded_rectangle([int(w * 0.04), base_top, int(w * 0.96), base_bot], radius=12, fill=GOLD, outline=GOLD_DARK, width=4)
    d.rectangle([int(w * 0.06), base_top + 6, int(w * 0.94), base_top + 14], fill=GOLD_LIGHT)
    # шарики на зубцах
    for px, py in [(0.02, 0.22), (0.5, 0.04), (0.98, 0.22)]:
        r = int(w * 0.045)
        cx, cy = int(w * px), int(h * py)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD_LIGHT, outline=GOLD_DARK, width=3)
    # камни на ободе
    gems = [RUBY, EMERALD, SAPPHIRE, EMERALD, RUBY]
    for i, col in enumerate(gems):
        cx = int(w * (0.18 + i * 0.16))
        cy = (base_top + base_bot) // 2
        r = int(h * 0.09) if i == 2 else int(h * 0.07)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col, outline=GOLD_DARK, width=3)
        d.ellipse([cx - r // 2, cy - r // 2, cx - r // 6, cy - r // 6], fill=(255, 255, 255))
    # камень в центре
    r = int(h * 0.08)
    cx, cy = int(w * 0.5), int(h * 0.36)
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=RUBY, outline=GOLD_DARK)
    shadow = c.split()[3].point(lambda a: 120 if a else 0)
    sh = Image.new("RGBA", c.size, (0, 0, 0, 0))
    sh.putalpha(shadow.filter(ImageFilter.GaussianBlur(8)))
    out = Image.new("RGBA", (w + 20, h + 20), (0, 0, 0, 0))
    out.paste(sh, (12, 14), sh)
    out.paste(c, (4, 4), c)
    return out


def _ribbon(d, cx, cy, width, height, text, font):
    left, right = cx - width // 2, cx + width // 2
    top, bot = cy - height // 2, cy + height // 2
    tail = 60
    d.polygon([(left - tail, top + 18), (left + 10, top + 18), (left + 10, bot + 18), (left - tail, bot + 18),
               (left - tail + 30, (top + bot) // 2 + 18)], fill=(120, 0, 30))
    d.polygon([(right + tail, top + 18), (right - 10, top + 18), (right - 10, bot + 18), (right + tail, bot + 18),
               (right + tail - 30, (top + bot) // 2 + 18)], fill=(120, 0, 30))
    d.rounded_rectangle([left, top, right, bot], radius=14, fill=(200, 20, 55), outline=GOLD, width=5)
    tw = d.textlength(text, font=font)
    d.text((cx - tw / 2 + 3, cy - font.size * 0.62 + 3), text, font=font, fill=(90, 0, 20))
    d.text((cx - tw / 2, cy - font.size * 0.62), text, font=font, fill=GOLD_LIGHT)


def _fit_font(d, text, path, start, max_width):
    size = start
    while size > 22:
        f = _font(path, size)
        if d.textlength(text, font=f) <= max_width:
            return f
        size -= 2
    return _font(path, size)


def make_legend_card(avatar_bytes, name, score, date_text=""):
    """PNG-байты открытки. avatar_bytes может быть None — тогда силуэт."""
    img = _background().convert("RGBA")
    d = ImageDraw.Draw(img)
    rnd = random.Random(name)

    # звездочки
    for _ in range(46):
        x, y = rnd.randint(20, W - 20), rnd.randint(20, H - 20)
        if 180 < x < 720 and 230 < y < 820:
            continue
        _sparkle(d, x, y, rnd.randint(5, 16), (255, 240, 190) if rnd.random() < 0.7 else (255, 170, 230))

    # аватарка в кружке
    cx, cy, R = W // 2, 520, 230
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        except Exception:
            av = None
    else:
        av = None
    if av is None:
        av = Image.new("RGB", (400, 400), (90, 60, 120))
        ad = ImageDraw.Draw(av)
        ad.ellipse([140, 70, 260, 190], fill=(200, 180, 220))
        ad.ellipse([80, 210, 320, 450], fill=(200, 180, 220))
    side = min(av.size)
    av = av.crop(((av.width - side) // 2, (av.height - side) // 2, (av.width + side) // 2, (av.height + side) // 2))
    av = av.resize((2 * R, 2 * R), Image.LANCZOS)
    mask = Image.new("L", (2 * R, 2 * R), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * R, 2 * R], fill=255)

    # золотые кольца
    for rr, col, wdt in [(R + 34, GOLD_DARK, 10), (R + 26, GOLD, 16), (R + 14, GOLD_LIGHT, 4), (R + 8, (120, 0, 30), 8)]:
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=col, width=wdt)
    img.paste(av, (cx - R, cy - R), mask)
    # камешки по кольцу
    for i in range(16):
        a = i * 2 * math.pi / 16
        gx, gy = cx + (R + 26) * math.cos(a), cy + (R + 26) * math.sin(a)
        col = [RUBY, SAPPHIRE, EMERALD, (255, 255, 255)][i % 4]
        d.ellipse([gx - 9, gy - 9, gx + 9, gy + 9], fill=col, outline=GOLD_DARK, width=2)

    # перчики по бокам
    _pepper(img, cx - R - 80, cy + 150, 1.0, 30)
    _pepper(img, cx + R + 80, cy + 150, 1.0, 150)
    _pepper(img, cx - R - 60, cy - 120, 0.75, -20)
    _pepper(img, cx + R + 60, cy - 120, 0.75, 200)

    # корона набекрень на аватарке
    crown = _crown(330).rotate(-12, expand=True, resample=Image.BICUBIC)
    img.paste(crown, (cx - crown.width // 2 + 70, cy - R - crown.height + 135), crown)

    # заголовок
    d = ImageDraw.Draw(img)
    title = "ЛЕГЕНДА ХАМСТВА"
    tf = _fit_font(d, title, FONT_SERIF, 86, W - 80)
    tw = d.textlength(title, font=tf)
    for dx, dy in [(4, 5), (2, 3)]:
        d.text((W / 2 - tw / 2 + dx, 26 + dy), title, font=tf, fill=(60, 0, 20))
    d.text((W / 2 - tw / 2, 26), title, font=tf, fill=GOLD)

    # лента с именем
    nf = _fit_font(d, name, FONT_SANS, 58, 600)
    _ribbon(d, W // 2, 865, 680, 104, name, nf)

    # подпись
    sub = f"первым в беседе — {score} баллов хамства"
    sf = _font(FONT_SANS, 34)
    sw = d.textlength(sub, font=sf)
    d.text((W / 2 - sw / 2, 960), sub, font=sf, fill=(255, 225, 160))
    if date_text:
        df = _font(FONT_SANS, 28)
        dw = d.textlength(date_text, font=df)
        d.text((W / 2 - dw / 2, 1010), date_text, font=df, fill=(200, 170, 230))
    foot = "💎 навеки в топе 💎".replace("💎", "◆")
    ff = _font(FONT_SERIF, 34)
    fw = d.textlength(foot, font=ff)
    d.text((W / 2 - fw / 2, 1065), foot, font=ff, fill=GOLD)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


if __name__ == "__main__":
    import sys
    src = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 else None
    open("/tmp/legend_card.png", "wb").write(make_legend_card(src, "Данил Колбасенко", 500, "26 ноября 2026"))
    print("ok")
