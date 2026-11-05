"""Receipts, screenshots, tickets, notes, labels, business cards, envelopes, signs, duplicates."""
import shutil
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter
from pypdf import PdfReader

from photo2pdf import classify as cls
from photo2pdf import naming
from photo2pdf.core import Options, PageSpec, convert, save_documents
from photo2pdf.detect import Plan, ScanSettings, scan_paths
from synth import (camera_corners, card, card_photo, envelope, font, page, photo_of, receipt,
                   receipt_photo, screenshot, scribbles, tax_letter, type_plate)

needs_tess = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
TODAY = date(2026, 11, 5)

# ---- classification rules on hand-made features (fast, no OCR) -----------------------


def F(**kw):
    base = {"words": 0, "quad": False, "card_shape": False, "screenshot": False, "aspect": 1.4,
            "ink": 0.0, "sharpness": 500.0, "mean_conf": 90.0, "median_h_ratio": 0.01,
            "lines": 10, "long_lines": 8, "qr": False, "barcode": False}
    base.update(kw)
    return base


def test_rules():
    c = cls.classify
    assert c(F(screenshot=True, words=3), "Instagram likes", 12).reason == "Screenshot"
    assert c(F(screenshot=True, words=10), "ING Betaling geslaagd € 25,00", 12).kind == "confirmation"
    assert c(F(screenshot=True, words=8, qr=True), "KLM Boarding pass Gate D7", 12).kind == "ticket"
    assert c(F(quad=True, aspect=2.7, words=20), "TOTAAL 12,16 PINNEN 12,16 BTW", 12).kind == "receipt"
    assert c(F(words=20, barcode=True), "PostNL pakket zending track & trace", 12).kind == "ticket"
    assert c(F(words=10, long_lines=0), "Port betaald Retouradres Postbus 12 1234 AB", 12).reason == "Envelope"
    assert c(F(words=12), "BOSCH Type WAN28K40 220-240V 50Hz CE", 12).kind == "label"
    assert c(F(words=23, median_h_ratio=0.06, long_lines=0), "CAFE koffie thee " * 5, 12).reason == "Sign or poster"
    assert c(F(words=60, quad=True), "Belastingdienst " * 60, 12).kind == "page"
    assert c(F(quad=True, ink=0.05), "", 12).kind == "note"
    assert c(F(quad=True, sharpness=5), "", 12).kind == "page"          # blurred sheet: kept, flagged
    assert c(F(), "", 12).is_document is False                           # plain photo


def test_hard_to_read():
    assert cls.hard_to_read(F(sharpness=10))
    assert cls.hard_to_read(F(words=30, mean_conf=40))
    assert not cls.hard_to_read(F(words=30))


@pytest.mark.parametrize("fields,expected", [
    (naming.extract_receipt("ALBERT HEIJN\nTOTAAL 12,16\nPINNEN 12,16\n02-11-24 14:32", TODAY),
     "Receipt - Albert Heijn - 2024-11-02 - EUR 12.16.pdf"),
    (naming.extract_receipt("MEDIA MARKT Amsterdam\nTotaal 349,00\n2 november 2024", TODAY),
     "Receipt - MediaMarkt - 2024-11-02 - EUR 349.00.pdf"),
    (naming.extract_confirmation("ING\nBetaling geslaagd\n€ 25,00 overgemaakt\n1 februari 2025", TODAY),
     "Payment confirmation - ING - 2025-02-01 - EUR 25.00.pdf"),
    (naming.extract_confirmation("Booking.com\nJe boeking is bevestigd\n12 augustus 2025", TODAY),
     "Booking confirmation - Booking.com - 2025-08-12.pdf"),
    (naming.extract_ticket("KLM\nBOARDING PASS\nGate D7\n14 July 2026", TODAY), "Boarding pass - KLM - 2026-07-14.pdf"),
    (naming.extract_ticket("PostNL\nJe pakket\nTrack & Trace", TODAY), "Parcel label - PostNL.pdf"),
    (naming.extract_label("BOSCH\nType WAN28K40/01\n220-240V 50Hz"), "Product label - Bosch - WAN28K40-01.pdf"),
])
def test_kind_names(fields, expected):
    assert naming.suggest_name(fields, "fallback.pdf") == expected


def test_receipt_total_when_ocr_splits_the_columns():
    # Label and amount in separate OCR lines (wide gap on the till slip).
    text = "ALBERT HEIJN\nBANANEN\n1,99\nTOTAAL\nPINNEN\n12,16\n12,16\n02-11-2024 14:32"
    f = naming.extract_receipt(text, TODAY)
    assert f.extra.get("total") == "12.16"


def test_business_card_uses_domain_not_person():
    f = naming.business_card_fields("Jan Bakker\nBakker Bouw BV\n06 12345678\njan@bakkerbouw.nl")
    assert naming.suggest_name(f, "x.pdf") == "Business card - bakkerbouw.nl.pdf"
    g = naming.business_card_fields("Jan Bakker\n06 12345678\njan.bakker@gmail.com")
    assert naming.suggest_name(g, "x.pdf") == "Business card.pdf"     # personal mail domain: no name

# ---- end to end -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    d = tmp_path_factory.mktemp("archive")
    photo_of(tax_letter(), camera_corners(dist=430), seed=1).save(d / "IMG_20250301_100000.jpg")
    photo_of(tax_letter(), camera_corners(dist=440, tilt_x=20, roll=3), seed=2).save(d / "IMG_20250301_100100.jpg")
    photo_of(tax_letter(), camera_corners(dist=430), seed=3).filter(ImageFilter.GaussianBlur(6)).save(
        d / "IMG_20250301_100200.jpg")
    photo_of(page(), camera_corners(dist=430), seed=4).save(d / "IMG_20250302_090000.jpg")
    receipt_photo(seed=5).save(d / "IMG_20250303_120000.jpg")
    screenshot(["ING", "Betaling geslaagd", "Je hebt € 25,00 overgemaakt", "aan J. Jansen",
                "1 februari 2025"]).save(d / "Screenshot_20250304-101500.png")
    screenshot(["Instagram", "Nice photo!", "12 likes"]).save(d / "Screenshot_20250304-101600.png")
    screenshot(["KLM", "Boarding pass", "Flight KL1234  Gate D7", "Date 14 July 2026", "Seat 23A"],
               qr="M1TEST/PASSENGER").save(d / "Screenshot_20250304-101700.png")
    photo_of(scribbles(), camera_corners(dist=430), seed=6).save(d / "IMG_20250305_080000.jpg")
    menu = Image.new("RGB", (2400, 1800), (240, 235, 220))
    dr = ImageDraw.Draw(menu)
    for i, ln in enumerate(["CAFE DE HOEK", "Koffie 2,80  Thee 2,50", "Appeltaart met room 4,25",
                            "Tosti ham kaas 4,50", "Soep van de dag 5,95", "Open dagelijks vanaf negen uur"]):
        dr.text((150, 100 + i * 260), ln, fill=(30, 30, 30), font=font(140))
    menu.save(d / "IMG_20250306_190000.jpg")
    photo_of(envelope(), camera_corners(page_mm=(220, 110), dist=380), seed=7).save(d / "IMG_20250307_110000.jpg")
    type_plate().save(d / "IMG_20250308_150000.jpg")
    card_photo(card(["Bakker Bouw BV", "Jan Bakker", "Projectleider", "06 12345678", "jan@bakkerbouw.nl"]),
               seed=8).save(d / "IMG_20250309_160000.jpg")
    return d


@needs_tess
def test_archive_end_to_end(archive):
    from photo2pdf.core import Tesseract
    infos = scan_paths([archive], ScanSettings(workers=3), Tesseract.find(), opts=Options())
    plan = Plan.from_scan(infos, 180)
    assert sorted(plan.output_paths("year")) == sorted([
        "2025/2025-03-14 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf",
        "Undated/Document 02 - 2025-03-02 09.00 (1 page).pdf",
        "Receipts/2024/Receipt - Albert Heijn - 2024-11-02 - EUR 12.16.pdf",
        "Confirmations/2025/Payment confirmation - ING - 2025-02-01 - EUR 25.00.pdf",
        "Tickets/2026/Boarding pass - KLM - 2026-07-14.pdf",
        "Notes/2025/Note - 2025-03-05 08.00.pdf",
        "Product labels/Product label - Bosch - WAN28K40-01.pdf",
        "Cards/Business card - bakkerbouw.nl.pdf",
    ])
    reasons = {o.path.name: o.reason for o in plan.others}
    assert reasons["IMG_20250301_100100.jpg"].startswith("Duplicate of IMG_20250301_100000.jpg")
    assert reasons["IMG_20250301_100200.jpg"].startswith("Duplicate of IMG_20250301_100000.jpg")
    assert reasons["Screenshot_20250304-101600.png"] == "Screenshot"
    assert reasons["IMG_20250306_190000.jpg"] == "Sign or poster"
    assert reasons["IMG_20250307_110000.jpg"] == "Envelope"


@needs_tess
def test_receipt_saved_at_till_roll_width(tmp_path):
    receipt_photo(seed=5).save(tmp_path / "IMG_1.jpg")
    res = save_documents([("r.pdf", [PageSpec(tmp_path / "IMG_1.jpg", 0, "receipt")])], tmp_path / "out", Options())
    w, h = (float(x) for x in PdfReader(str(res.outputs[0])).pages[0].mediabox.upper_right)
    assert abs(min(w, h) - 226.8) < 3          # 80 mm = 226.8 pt


def test_screenshots_are_never_warped(tmp_path):
    shot = screenshot(["Booking.com", "Je boeking is bevestigd"])
    shot.save(tmp_path / "Screenshot_20250101-120000.png")
    res = convert([tmp_path], Options(ocr=False))
    pg = PdfReader(str(res.outputs[0])).pages[0]
    w, h = float(pg.mediabox.width), float(pg.mediabox.height)
    assert abs(h / w - 2340 / 1080) < 0.01      # full screen kept, aspect unchanged


@needs_tess
def test_receipt_photo_total_from_real_ocr(tmp_path):
    """Same OCR path as the folder scan, on the synthetic receipt photo. On failure the
    message shows what Tesseract read (synthetic text, nothing private)."""
    import tempfile

    from photo2pdf import detect
    from photo2pdf.core import Tesseract, flatten_lighting, load_page
    from photo2pdf.naming import text_from_tsv

    src = tmp_path / "r.jpg"
    receipt_photo(seed=5).save(src)
    im = load_page(src, Options(), max_edge=2000)
    gray = flatten_lighting(detect._resize_long(im.convert("L"), detect.DETECT_LONG_EDGE))
    tess = Tesseract.find()
    with tempfile.TemporaryDirectory() as td:
        gray.save(Path(td) / "d.png")
        r = tess._run(["d.png", "stdout", "-l", "nld+eng", "--psm", "1", "tsv"], cwd=Path(td), timeout=180,
                      threads=1)
    text = text_from_tsv(r.stdout or "")
    total = naming.extract_receipt(text, TODAY).extra.get("total") or naming.receipt_total_by_row(r.stdout)
    assert total == "12.16", f"OCR text was:\n{text}\n\nby row:\n{naming.rows_from_tsv(r.stdout)}"


def _tsv(words):
    """TSV rows (level 5) for (block, left, top, text) words, height 30."""
    head = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [f"5\t1\t{b}\t1\t{i}\t1\t{x}\t{y}\t60\t30\t95\t{t}" for i, (b, x, y, t) in enumerate(words)]
    return "\n".join([head] + rows)


def test_receipt_total_when_amounts_come_back_as_a_separate_block():
    # What Tesseract returned on Windows: all labels in one block, all amounts in a later one.
    tsv = _tsv([(1, 30, 40, "ALBERT"), (1, 120, 40, "HEIJN"), (1, 30, 100, "BANANEN"),
                (1, 30, 160, "SUBTOTAAL"), (1, 30, 220, "TOTAAL"), (1, 30, 280, "PINNEN"),
                (1, 30, 340, "BTW"), (1, 90, 340, "9%"),
                (2, 400, 102, "1,99"), (2, 400, 158, "12,16"), (2, 400, 221, "12,16"),
                (2, 400, 281, "12,16"), (2, 400, 339, "1,00")])
    assert naming.extract_receipt(naming.text_from_tsv(tsv), TODAY).extra.get("total") is None
    assert naming.rows_from_tsv(tsv).splitlines()[3] == "TOTAAL 12,16"
    assert naming.receipt_total_by_row(tsv) == "12.16"
