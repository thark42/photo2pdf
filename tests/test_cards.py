"""ID cards, passports, driving licences, insurance, bank and loyalty cards."""
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from pypdf import PdfReader

from photo2pdf import geometry as g
from photo2pdf.core import Options, PageSpec, convert, save_documents
from photo2pdf.detect import Plan, PhotoInfo, ScanSettings, scan_paths
from photo2pdf.naming import DocFields, extract_any, extract_card, parse_mrz, suggest_name
from synth import card, card_photo, page, photo_of, camera_corners, td3

needs_tess = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
TODAY = date(2026, 11, 5)
NONE = ([], [])

# ---- naming ---------------------------------------------------------------


def test_mrz_icao_specimen():
    # ICAO 9303 Part 4 specimen passport (fictional "Utopia").
    text = ("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
            "L898902C36UTO7408122F1204159ZE184226B<<<<<10")
    assert parse_mrz(text) == {"type": "Passport", "country": "UTO", "expiry": date(2012, 4, 15)}


def test_mrz_bad_check_digit_gives_no_date():
    text = ("P<NLDDEVRIES<<ANNA<<<<<<<<<<<<<<<<<<<<<<<<<<\n"
            "SPECI20142NLD8501019F3105018<<<<<<<<<<<<<<<0")   # expiry check digit wrong (8, should be 0)
    out = parse_mrz(text)
    assert out["type"] == "Passport" and out["country"] == "Netherlands" and "expiry" not in out


@pytest.mark.parametrize("text,expected", [
    ("Zilveren Kruis\nZorgpas\nPolisnummer 123\nGeldig tot 31-12-2026",
     "Health insurance card - Zilveren Kruis - valid until 2026-12-31.pdf"),
    ("RIJBEWIJS\nKoninkrijk der Nederlanden\n4b. 01.05.2031", "Driving licence - Netherlands - valid until 2031-05-01.pdf"),
    ("ING\nDebit Mastercard\nVALID THRU 08/29", "Bank card - ING - valid until 2029-08-31.pdf"),
    ("Albert Heijn\nBonuskaart", "Membership card - Albert Heijn.pdf"),
    ("EUROPEAN HEALTH INSURANCE CARD\nEuropese ziekteverzekeringskaart\nVervaldatum 01/03/2028",
     "European Health Insurance Card - valid until 2028-03-01.pdf"),
])
def test_card_names(text, expected):
    assert suggest_name(extract_card(text, TODAY, NONE), "x.pdf") == expected


def test_short_words_are_not_issuers():
    f = extract_card("Bonuskaart\nplus punten sparen\nding dong ns", TODAY, NONE)
    assert f.sender is None and f.doc_type == "Membership card"


def test_letter_mentioning_insurance_stays_a_letter():
    letter = ("Zilveren Kruis\nDatum 2 januari 2025\nWijziging zorgverzekering 2025\n" + "tekst " * 60)
    f = extract_any(letter, card_shape=False, today=TODAY, extra=NONE)
    assert f.kind is None and f.sender is None or f.kind is None


def test_no_names_or_numbers_in_card_file_names():
    text = ("PASPOORT\nDe Vries, Anna\nBSN 123456782\n"
            "P<NLDDEVRIES<<ANNA<<<<<<<<<<<<<<<<<<<<<<<<<<\nSPECI20142NLD8501019F3105010<<<<<<<<<<<<<<<0")
    name = suggest_name(extract_card(text, TODAY, NONE), "x.pdf")
    assert name == "Passport - Netherlands - valid until 2031-05-01.pdf"
    for secret in ("Vries", "Anna", "123456782", "SPECI2014"):
        assert secret not in name


def test_card_without_type_uses_fallback():
    assert suggest_name(DocFields(kind="card"), "Card 01.pdf") == "Card 01.pdf"

# ---- geometry ---------------------------------------------------------------


def test_card_photo_is_flagged_and_snapped_to_id1():
    img = np.asarray(card_photo(card(["Zorgpas", "Polisnummer 1"]), seed=2))
    out, info = g.straighten(img)
    assert info["card"]
    assert abs(max(out.shape[:2]) / min(out.shape[:2]) - g.ID1) < 0.02


def test_a4_page_is_not_a_card():
    img = np.asarray(photo_of(page(), camera_corners(), seed=1))
    _, info = g.straighten(img)
    assert info["quad"] is not None and not info["card"]

# ---- grouping -----------------------------------------------------------------

T0 = datetime(2025, 6, 1, 12, 0)


def ph(name, secs, kind="page", card_shape=False, words=60, **fields):
    p = PhotoInfo(Path(name), T0 + timedelta(seconds=secs), words=words,
                  is_document=(kind == "card" or words >= 12), card_shape=card_shape, kind=kind)
    if fields:
        p.fields = DocFields(kind="card" if kind == "card" else None, **fields)
    return p


def test_card_front_back_pairing_and_wallet_split():
    photos = [
        ph("zorg_front", 0, "card", True, 8, doc_type="Health insurance card", sender="Zilveren Kruis"),
        ph("zorg_back", 20, "page", True, 0),          # blank back: promoted to the card's back
        ph("bank_front", 40, "card", True, 6, doc_type="Bank card", sender="ING"),
        ph("bank_back", 55, "card", True, 3),
        ph("ah", 70, "card", True, 4, doc_type="Membership card", sender="Albert Heijn"),
        ph("letter", 90, "page", False, 120),
    ]
    plan = Plan.from_scan(photos, 180)
    assert [[p.path.name for p in grp] for grp in plan.groups] == [
        ["zorg_front", "zorg_back"], ["bank_front", "bank_back"], ["ah"], ["letter"]]
    paths = plan.output_paths("year")
    assert paths[0] == "Cards/Health insurance card - Zilveren Kruis.pdf"
    assert paths[2] == "Cards/Membership card - Albert Heijn.pdf"
    assert not paths[3].startswith("Cards/")
    assert plan.is_card(0) and not plan.is_card(3)

# ---- end to end -------------------------------------------------------------------


@needs_tess
def test_scan_name_and_save_cards_at_real_size(tmp_path):
    folder = tmp_path / "wallet"
    folder.mkdir()
    card_photo(card(["Zilveren Kruis", "Zorgpas", "Polisnummer 123456789", "Geldig tot 31-12-2026"]),
               seed=1).save(folder / "IMG_20250601_120000.jpg")
    card_photo(card(["", ""], bg=(200, 200, 200)), seed=2).save(folder / "IMG_20250601_120020.jpg")
    card_photo(card(["RIJBEWIJS", "Koninkrijk der Nederlanden", "1. DE VRIES", "4b. 01.05.2031"],
                    bg=(240, 225, 230)), seed=3).save(folder / "IMG_20250601_120040.jpg")
    photo_of(page(), camera_corners(dist=430), seed=4).save(folder / "IMG_20250601_120100.jpg")

    infos = scan_paths([folder], ScanSettings(workers=2), _tess(), opts=Options())
    plan = Plan.from_scan(infos, 180)
    assert [len(grp) for grp in plan.groups] == [2, 1, 1]
    names = plan.output_paths("year")
    assert names[0] == "Cards/Health insurance card - Zilveren Kruis - valid until 2026-12-31.pdf"
    assert names[1] == "Cards/Driving licence - Netherlands - valid until 2031-05-01.pdf"
    assert not names[2].startswith("Cards/")

    docs = [(names[i], [PageSpec(p.path, p.rotation, "card" if p.card_shape else "page")
                        for p in plan.groups[i]]) for i in plan.included_indices()]
    res = save_documents(docs, folder / "PDFs", Options())
    assert len(res.outputs) == 3 and not res.problems(), res.summary_lines()
    card_pdf = PdfReader(str(res.outputs[0]))
    w, h = (float(x) for x in card_pdf.pages[0].mediabox.upper_right)
    # 85.6 x 53.98 mm = 242.6 x 153.0 pt
    assert abs(max(w, h) - 242.6) < 3 and abs(min(w, h) - 153.0) < 3
    assert len(card_pdf.pages) == 2
    letter = PdfReader(str(res.outputs[2])).pages[0]
    assert float(letter.mediabox.height) > 800    # A4


@needs_tess
def test_quick_mode_names_a_single_card(tmp_path):
    card_photo(card(["ING", "Debit Mastercard", "VALID THRU 08/29"]), seed=5).save(tmp_path / "IMG_1.jpg")
    res = convert([tmp_path], Options(combine=True))
    assert [p.name for p in res.outputs] == ["Bank card - ING - valid until 2029-08-31.pdf"]


def _tess():
    from photo2pdf.core import Tesseract
    return Tesseract.find()


def test_unidentified_card_then_identified_card_are_separate():
    photos = [ph("rijb", 0, "card", True, 5),                      # read poorly, no type
              ph("ah", 30, "card", True, 4, doc_type="Membership card", sender="Albert Heijn")]
    plan = Plan.from_scan(photos, 180)
    assert [[p.path.name for p in grp] for grp in plan.groups] == [["rijb"], ["ah"]]


def test_old_settings_file_cannot_pin_internal_values(tmp_path, monkeypatch):
    import json
    from photo2pdf import app
    d = tmp_path / "_home" / "AppData" / "Roaming" / "Photo2PDF"
    d.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "_home" / ".config" / "Photo2PDF"
    cfg.mkdir(parents=True, exist_ok=True)
    for folder in (d, cfg):
        (folder / "settings.json").write_text(json.dumps({"language": "eng", "grayscale": True}))
    o = app.load_options()
    assert o.language == "nld+eng" and o.grayscale is True


def test_blank_card_back_is_not_falsely_deskewed():
    img = np.asarray(card_photo(card(["", ""], bg=(200, 200, 200)), seed=2))
    out, info = g.straighten(img)
    assert info["card"] and info["skew"] == 0.0
