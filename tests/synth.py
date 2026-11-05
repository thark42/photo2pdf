"""Synthetic 'phone photo of a page' generator for tests."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

LINES_EN = ["Dear customer, thank you for your payment of the invoice.",
            "Please find the statement for this month attached below."]
LINES_NL = ["Geachte klant, hartelijk dank voor uw betaling van de factuur.",
            "Hierbij ontvangt u het overzicht van deze maand als bijlage."]


def font(size=34):
    for name in ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def page(w=1240, h=1754, lang="mixed", lines=22) -> Image.Image:
    im = Image.new("RGB", (w, h), (250, 250, 246))
    d = ImageDraw.Draw(im)
    f = font()
    src = {"en": LINES_EN, "nl": LINES_NL}.get(lang, LINES_EN + LINES_NL)
    for i in range(lines):
        d.text((90, 120 + i * 68), src[i % len(src)], fill=(20, 20, 20), font=f)
    return im


def photo_of(pg: Image.Image, corners, size=(2400, 1800), seed=0) -> Image.Image:
    """Project `pg` onto `corners` (TL,TR,BR,BL in output px) over a textured background."""
    import cv2

    rng = np.random.default_rng(seed)
    W, H = size
    yy, xx = np.mgrid[0:H, 0:W]
    bg = (90 + 30 * np.sin(xx / 37.0) + 15 * np.sin(yy / 11.0) + rng.normal(0, 8, (H, W)))
    bg = np.stack([bg * 0.9, bg * 0.7, bg * 0.5], axis=-1).clip(0, 255).astype(np.uint8)
    src = np.array(pg)
    h, w = src.shape[:2]
    m = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [w, 0], [w, h], [0, h]]), np.float32(corners))
    warped = cv2.warpPerspective(src, m, (W, H))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), m, (W, H))
    out = np.where(mask[..., None] > 0, warped, bg).astype(np.float32)
    shade = (0.75 + 0.25 * (xx / W))[..., None]  # lighting gradient
    out = (out * shade + rng.normal(0, 3, out.shape)).clip(0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    return Image.fromarray(out)


def camera_corners(size=(2400, 1800), page_mm=(210, 297), tilt_x=30, tilt_y=12, roll=8,
                   dist=520, focal=2000, shift=(0, 0)):
    """Project a real-size page through a pinhole camera (principal point = centre).

    Returns TL,TR,BR,BL pixel corners. Angles in degrees; distances in mm.
    """
    W, H = size
    pw, ph = page_mm
    pts = np.array([[-pw / 2, -ph / 2, 0], [pw / 2, -ph / 2, 0], [pw / 2, ph / 2, 0], [-pw / 2, ph / 2, 0]])
    ax, ay, az = np.radians([tilt_x, tilt_y, roll])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    cam = pts @ (rz @ ry @ rx).T + np.array([shift[0], shift[1], dist])
    u = focal * cam[:, 0] / cam[:, 2] + W / 2
    v = focal * cam[:, 1] / cam[:, 2] + H / 2
    return [tuple(map(float, p)) for p in np.stack([u, v], 1)]


# Portrait A4 page shot at an angle, landscape frame.
ANGLED = camera_corners()


def tax_letter(w=1240, h=1754) -> Image.Image:
    """Fictional Belastingdienst-style letter (layout invented for tests)."""
    im = Image.new("RGB", (w, h), (250, 250, 246))
    d = ImageDraw.Draw(im)
    big, f = font(48), font(30)
    d.text((90, 80), "Belastingdienst", fill=(0, 0, 0), font=big)
    y = 240
    for line in ["Datum 14 maart 2025", "Ons kenmerk 1234.56.789",
                 "", "Voorlopige aanslag inkomstenbelasting 2025", "",
                 "Geachte heer, mevrouw,", "Hierbij ontvangt u de voorlopige aanslag voor het jaar 2025.",
                 "Het bedrag dat u moet betalen staat hieronder vermeld.",
                 "U betaalt dit bedrag in maandelijkse termijnen.",
                 "Betaal op tijd, zodat u geen extra kosten hoeft te betalen.",
                 "Wij verwerken uw gegevens volgens de wet.",
                 "Bent u het niet eens met deze aanslag? Dan kunt u bezwaar maken.",
                 "Met vriendelijke groet,", "de inspecteur"]:
        d.text((90, y), line, fill=(15, 15, 15), font=f)
        y += 62
    return im


def mono(size=40):
    for name in ("DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "consola.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def card(lines, w=1012, h=638, bg=(235, 242, 250), mrz=None, big_first=True) -> Image.Image:
    """An ID-1 card (85.6 x 54 mm at ~300 dpi) with text lines; optional MRZ lines at the bottom."""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    y = 50
    for i, ln in enumerate(lines):
        f = font(52 if (i == 0 and big_first) else 34)
        d.text((60, y), ln, fill=(20, 20, 40), font=f)
        y += 70 if i == 0 else 52
    if mrz:
        f = mono(34 if len(mrz[0]) > 32 else 40)
        yy = h - 60 * len(mrz) - 20
        for ln in mrz:
            d.text((30, yy), ln, fill=(10, 10, 10), font=f)
            yy += 60
    return im


def mrz_check(field: str) -> str:
    total = 0
    for i, ch in enumerate(field):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else 0)
        total += v * (7, 3, 1)[i % 3]
    return str(total % 10)


def td3(country="NLD", surname="DEVRIES", given="ANNA", number="SPECI2014", birth="850101",
        sex="F", expiry="310501") -> list[str]:
    """Fictional passport MRZ (ICAO 9303 TD3) with valid check digits."""
    l1 = f"P<{country}{surname}<<{given}".ljust(44, "<")[:44]
    l2 = (number + mrz_check(number) + country + birth + mrz_check(birth) + sex + expiry + mrz_check(expiry))
    l2 = l2.ljust(42, "<") + "<" + "0"
    return [l1, l2[:44]]


def card_photo(img: Image.Image, seed=0, **pose) -> Image.Image:
    """A card on a table, shot at an angle (camera model as for pages, card-sized)."""
    kw = dict(page_mm=(85.6, 53.98), dist=190, focal=2000, tilt_x=25, tilt_y=8, roll=5)
    kw.update(pose)
    w, h = img.size
    if w < h:
        kw["page_mm"] = (53.98, 85.6)
    return photo_of(img, camera_corners(**kw), seed=seed)


def receipt(lines=None, w=560, h=1500) -> Image.Image:
    """Till receipt: narrow, long, small monospace text."""
    lines = lines or ["ALBERT HEIJN", "Filiaal 1234 Amsterdam", "", "BANANEN          1,99", "MELK HALFVOL     1,19",
                      "BROOD VOLKOREN   2,49", "KAAS JONG BELEGEN 6,49", "", "SUBTOTAAL       12,16",
                      "TOTAAL          12,16", "PINNEN          12,16", "BTW 9%           1,00", "",
                      "02-11-2024 14:32  Kassa 3", "Bedankt en tot ziens"]
    im = Image.new("RGB", (w, h), (248, 247, 242))
    d = ImageDraw.Draw(im)
    f = mono(30)
    y = 40
    for ln in lines:
        d.text((30, y), ln, fill=(30, 30, 30), font=f)
        y += 46
    return im


def receipt_photo(img=None, seed=0, **pose):
    img = img or receipt()
    kw = dict(page_mm=(80, 214), dist=330, focal=2000, tilt_x=18, tilt_y=6, roll=4)
    kw.update(pose)
    return photo_of(img, camera_corners(**kw), seed=seed)


def screenshot(lines, w=1080, h=2340, qr=None) -> Image.Image:
    """Phone screenshot (19.5:9) with app-like text; optional QR code."""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 120], fill=(255, 102, 0))
    y = 220
    for i, ln in enumerate(lines):
        d.text((70, y), ln, fill=(20, 20, 20), font=font(64 if i == 0 else 46))
        y += 110 if i == 0 else 80
    if qr:
        import cv2
        import numpy as np

        code = cv2.QRCodeEncoder.create().encode(qr)
        code = cv2.resize(code, (code.shape[1] * 14, code.shape[0] * 14), interpolation=cv2.INTER_NEAREST)
        im.paste(Image.fromarray(np.asarray(code)).convert("RGB"), ((w - code.shape[1]) // 2, y + 60))
    return im


def scribbles(w=1240, h=1754, seed=0) -> Image.Image:
    """A handwritten-looking note: wavy pen strokes in lines, no printed text."""
    import random

    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), (250, 250, 246))
    d = ImageDraw.Draw(im)
    for row in range(14):
        y0 = 150 + row * 95
        x = 100
        while x < w - 150:
            seg = rnd.randint(40, 160)
            pts = [(x + t, y0 + 14 * __import__("math").sin((x + t) / rnd.uniform(8, 14)) + rnd.uniform(-4, 4))
                   for t in range(0, seg, 4)]
            d.line(pts, fill=(25, 35, 110), width=4)
            x += seg + rnd.randint(20, 45)
    return im


def poster() -> Image.Image:
    """A sign / menu board photographed straight on: big text, no paper sheet."""
    im = Image.new("RGB", (2400, 1800), (40, 90, 60))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(["CAFE DE HOEK", "Koffie 2,80", "Thee 2,50", "Appeltaart 4,25",
                            "Tosti kaas 4,50", "Open dagelijks", "Welkom binnen"]):
        d.text((200, 120 + i * 230), ln, fill=(250, 250, 240), font=font(150))
    return im


def envelope(w=1100, h=550) -> Image.Image:
    im = Image.new("RGB", (w, h), (245, 240, 225))
    d = ImageDraw.Draw(im)
    d.text((40, 30), "Retouradres: Postbus 1234, 3500 AB Utrecht", fill=(40, 40, 40), font=font(24))
    d.rectangle([w - 230, 30, w - 40, 140], outline=(40, 40, 40), width=3)
    d.text((w - 215, 70), "PORT BETAALD", fill=(40, 40, 40), font=font(22))
    for i, ln in enumerate(["Mevrouw A. de Vries", "Dorpsstraat 12", "1234 AB  Ergenshuizen"]):
        d.text((450, 260 + i * 50), ln, fill=(20, 20, 20), font=font(34))
    return im


def type_plate() -> Image.Image:
    im = Image.new("RGB", (1600, 1000), (200, 200, 205))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(["BOSCH", "Type WAN28K40/01", "E-Nr WAN28K40NL  FD 9904", "220-240V 50Hz 2300W",
                            "Made in Germany   CE   IPX4"]):
        d.text((120, 120 + i * 150), ln, fill=(20, 20, 20), font=font(80 if i == 0 else 60))
    return im
