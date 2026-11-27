"""Карточки даров Кунегунды. Если в assets/gifts/<key>.png лежит своя картинка, берется она,
иначе карточка рисуется здесь."""
import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONT_SERIF = os.path.join(ASSETS, "fonts", "DejaVuSerif-Bold.ttf")
FONT_SANS = os.path.join(ASSETS, "fonts", "DejaVuSans-Bold.ttf")
W, H = 800, 1000
GOLD = (255, 210, 90)
GOLD_DARK = (170, 115, 25)


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _bg(c1, c2):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * k) for a, b in zip(c1, c2)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W / 2 - 300, 420 - 300, W / 2 + 300, 420 + 300], fill=(255, 220, 150, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    img.paste(glow, (0, 0), glow)
    return img.convert("RGBA")


def _star(d, x, y, r, col):
    d.polygon([(x, y - r), (x + r * .25, y - r * .25), (x + r, y), (x + r * .25, y + r * .25),
               (x, y + r), (x - r * .25, y + r * .25), (x - r, y), (x - r * .25, y - r * .25)], fill=col)


# --- рисунки артефактов (центр 400, 420) ---
def _clover(d):
    cx, cy = 400, 400
    d.line([(cx, cy + 20), (cx + 40, cy + 230)], fill=(30, 120, 55), width=16)
    for a in range(4):
        ang = a * math.pi / 2 + math.pi / 4
        x, y = cx + 85 * math.cos(ang), cy + 85 * math.sin(ang)
        for dx, dy in [(-38, 0), (38, 0)]:
            rx = x + dx * math.cos(ang + math.pi / 2)
            ry = y + dx * math.sin(ang + math.pi / 2)
            d.ellipse([rx - 62, ry - 62, rx + 62, ry + 62], fill=(60, 190, 90), outline=(20, 110, 50), width=5)
    d.ellipse([cx - 25, cy - 25, cx + 25, cy + 25], fill=(40, 150, 70))


def _crown(d):
    pts = [(220, 470), (200, 280), (300, 380), (400, 230), (500, 380), (600, 280), (580, 470)]
    d.polygon(pts, fill=GOLD, outline=GOLD_DARK)
    d.rounded_rectangle([210, 460, 590, 540], radius=14, fill=GOLD, outline=GOLD_DARK, width=5)
    for x, col in [(270, (220, 30, 60)), (400, (60, 120, 255)), (530, (40, 190, 110))]:
        d.ellipse([x - 26, 474, x + 26, 526], fill=col, outline=GOLD_DARK, width=3)
    for x, y in [(200, 280), (400, 230), (600, 280)]:
        d.ellipse([x - 20, y - 20, x + 20, y + 20], fill=(255, 245, 200), outline=GOLD_DARK, width=3)


def _mirror(d):
    d.ellipse([255, 200, 545, 560], fill=GOLD, outline=GOLD_DARK, width=6)
    d.ellipse([285, 230, 515, 530], fill=(170, 210, 255))
    d.polygon([(330, 270), (370, 260), (300, 420), (290, 380)], fill=(235, 245, 255))
    d.rounded_rectangle([380, 555, 420, 700], radius=12, fill=GOLD, outline=GOLD_DARK, width=4)
    d.ellipse([355, 690, 445, 730], fill=GOLD, outline=GOLD_DARK, width=4)


def _ball(d):
    d.polygon([(300, 600), (500, 600), (540, 680), (260, 680)], fill=(120, 70, 40), outline=(70, 40, 20))
    d.ellipse([240, 220, 560, 600], fill=(150, 110, 230), outline=(230, 210, 255), width=6)
    d.ellipse([290, 260, 400, 360], fill=(220, 200, 255))
    for x, y, r in [(430, 420, 18), (360, 480, 10), (470, 330, 8)]:
        _star(d, x, y, r, (255, 250, 210))


def _cup(d):
    d.ellipse([200, 610, 600, 690], fill=(235, 235, 245), outline=(150, 150, 170), width=4)
    d.polygon([(250, 380), (550, 380), (510, 630), (290, 630)], fill=(245, 240, 250), outline=(150, 150, 170))
    d.ellipse([250, 350, 550, 410], fill=(90, 50, 25), outline=(150, 150, 170), width=4)
    d.arc([520, 420, 640, 560], -80, 80, fill=(150, 150, 170), width=22)
    for i in range(3):
        x = 330 + i * 70
        d.arc([x, 210, x + 50, 330], 200, 340, fill=(255, 255, 255), width=8)


def _button(d):
    d.ellipse([230, 250, 570, 590], fill=(60, 40, 70), outline=(140, 80, 160), width=10)
    d.ellipse([270, 290, 530, 550], outline=(110, 60, 130), width=6)
    for x, y in [(355, 375), (445, 375), (355, 465), (445, 465)]:
        d.ellipse([x - 24, y - 24, x + 24, y + 24], fill=(25, 15, 30))
    d.line([(355, 375), (445, 465)], fill=(200, 40, 60), width=8)
    d.line([(445, 375), (355, 465)], fill=(200, 40, 60), width=8)


def _horseshoe(d):
    d.arc([230, 230, 570, 600], 0, 180, fill=GOLD, width=70)
    d.rectangle([230, 300, 300, 420], fill=GOLD)
    d.rectangle([500, 300, 570, 420], fill=GOLD)
    for x, y in [(265, 340), (265, 390), (535, 340), (535, 390), (300, 500), (500, 500), (400, 560)]:
        d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=GOLD_DARK)


def _feather(d):
    d.line([(300, 650), (520, 230)], fill=(230, 230, 230), width=8)
    for i in range(16):
        t = i / 15
        x, y = 300 + 220 * t, 650 - 420 * t
        col = [(255, 90, 80), (255, 190, 60), (60, 200, 120), (70, 160, 255)][i % 4]
        d.polygon([(x, y), (x - 110 + 60 * t, y - 40), (x - 20, y - 50)], fill=col)
        d.polygon([(x, y), (x + 80 - 40 * t, y + 20), (x + 30, y - 40)], fill=col)


DRAWERS = {"clover": _clover, "crown": _crown, "mirror": _mirror, "ball": _ball,
           "cup": _cup, "button": _button, "horseshoe": _horseshoe, "feather": _feather}
COLORS = {"clover": ((15, 60, 35), (5, 20, 15)), "crown": ((70, 20, 70), (20, 5, 30)),
          "mirror": ((20, 40, 80), (5, 10, 30)), "ball": ((45, 20, 90), (10, 5, 35)),
          "cup": ((70, 40, 25), (25, 12, 8)), "button": ((40, 10, 40), (10, 0, 12)),
          "horseshoe": ((80, 50, 10), (25, 15, 5)), "feather": ((15, 60, 70), (5, 20, 25))}


def card_bytes(key, title, subtitle):
    own = os.path.join(ASSETS, "gifts", f"{key}.png")
    if os.path.exists(own):
        return open(own, "rb").read()
    img = _bg(*COLORS.get(key, ((40, 20, 60), (10, 5, 20))))
    d = ImageDraw.Draw(img)
    rnd = random.Random(key)
    for _ in range(40):
        x, y = rnd.randint(20, W - 20), rnd.randint(20, H - 20)
        if (180 < x < 620 and 170 < y < 740) or y > 750 or y < 110:
            continue
        _star(d, x, y, rnd.randint(4, 13), (255, 240, 200))
    # рамка карты таро
    d.rounded_rectangle([24, 24, W - 24, H - 24], radius=30, outline=GOLD, width=6)
    d.rounded_rectangle([42, 42, W - 42, H - 42], radius=22, outline=GOLD_DARK, width=2)
    tf = _font(FONT_SANS, 30)
    top = "✦ ДАР КУНЕГУНДЫ ✦".replace("✦", "*")
    tw = d.textlength(top, font=tf)
    d.text((W / 2 - tw / 2, 70), top, font=tf, fill=GOLD)
    DRAWERS.get(key, _ball)(d)
    size = 64
    f = _font(FONT_SERIF, size)
    while d.textlength(title, font=f) > W - 120 and size > 30:
        size -= 2
        f = _font(FONT_SERIF, size)
    w = d.textlength(title, font=f)
    d.text((W / 2 - w / 2 + 3, 773), title, font=f, fill=(30, 10, 10))
    d.text((W / 2 - w / 2, 770), title, font=f, fill=GOLD)
    sf = _font(FONT_SANS, 28)
    words, lines, line = subtitle.split(), [], ""
    for wd in words:
        if d.textlength((line + " " + wd).strip(), font=sf) > W - 140:
            lines.append(line)
            line = wd
        else:
            line = (line + " " + wd).strip()
    lines.append(line)
    for i, ln in enumerate(lines[:3]):
        lw = d.textlength(ln, font=sf)
        d.text((W / 2 - lw / 2, 860 + i * 36), ln, font=sf, fill=(240, 225, 255))
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
