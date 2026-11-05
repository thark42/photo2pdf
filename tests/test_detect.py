import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from photo2pdf.core import Options, save_documents
from photo2pdf.detect import (
    Plan, PhotoInfo, ScanSettings, count_words, group_documents, scan_folder, time_from_name,
)
from test_core import text_page

needs_tess = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
T0 = datetime(2026, 3, 1, 10, 0, 0)


def ph(name, secs, doc=True, words=None):
    return PhotoInfo(Path(name), T0 + timedelta(seconds=secs), words=50 if words is None else words,
                     is_document=doc)


def names(groups):
    return [[p.path.name for p in g] for g in groups]


@pytest.mark.parametrize("name,expected", [
    ("IMG_20241105_143210.jpg", datetime(2024, 11, 5, 14, 32, 10)),
    ("PXL_20241105_143210123.jpg", datetime(2024, 11, 5, 14, 32, 10)),
    ("Screenshot_20241105-143210.png", datetime(2024, 11, 5, 14, 32, 10)),
    ("WhatsApp Image 2024-11-05 at 14.32.10.jpeg", datetime(2024, 11, 5, 14, 32, 10)),
    ("IMG_1234.JPG", None),
    ("IMG_20241399_999999.jpg", None),  # invalid date
])
def test_time_from_name(name, expected):
    assert time_from_name(name) == expected


def test_count_words_filters_noise():
    hdr = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [("Invoice", 95), ("total", 80), ("ab", 99), ("|||", 90), ("Payment", 30), ("2026-03-01", 88)]
    tsv = "\n".join([hdr] + [f"5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t{c}\t{t}" for t, c in rows])
    assert count_words(tsv) == 3  # Invoice, total, 2026-03-01


def test_group_by_time_gap_and_separators():
    photos = [
        ph("a1", 0), ph("a2", 30), ph("a3", 70),        # doc A
        ph("b1", 1000), ph("b2", 1010),                 # doc B (big gap before)
        ph("cat", 1020, doc=False),                     # separator
        ph("c1", 1030),                                 # doc C
    ]
    assert names(group_documents(photos, 180)) == [["a1", "a2", "a3"], ["b1", "b2"], ["c1"]]


def test_plan_edits():
    photos = [ph("a1", 0), ph("a2", 10), ph("a3", 20), ph("x", 25, doc=False, words=5),
              ph("b1", 30), ph("dog", 40, doc=False, words=0)]
    plan = Plan.from_scan(photos, 180)
    assert names(plan.groups) == [["a1", "a2", "a3"], ["b1"]]
    assert [p.path.name for p in plan.others] == ["x", "dog"]  # near-miss first

    plan.split(0, 2)
    assert names(plan.groups) == [["a1", "a2"], ["a3"], ["b1"]]
    plan.join_previous(2)
    assert names(plan.groups) == [["a1", "a2"], ["a3", "b1"]]
    plan.move(0, 0, +1)
    assert names(plan.groups)[0] == ["a2", "a1"]
    plan.remove(1, 0)
    assert names(plan.groups) == [["a2", "a1"], ["b1"]]
    assert plan.others[0].path.name == "a3"
    plan.add(plan.others.index(next(p for p in plan.others if p.path.name == "x")))
    assert names(plan.groups) == [["a2", "a1"], ["x"], ["b1"]]  # placed by time
    plan.remove(1, 0)  # removing last page drops the group
    assert len(plan.groups) == 2
    out = plan.output_names()
    assert out[0] == "Document 01 - 2026-03-01 10.00 (2 pages).pdf"
    assert out[1].endswith("(1 page).pdf")


def _photo(path: Path, when: datetime):
    im = Image.new("RGB", (900, 700))
    d = ImageDraw.Draw(im)
    for y in range(0, 700, 7):
        d.line([(0, y), (900, y)], fill=((y * 3) % 255, (y * 7) % 255, 120))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    im.save(path, exif=exif)


@needs_tess
def test_scan_folder_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = tmp_path / "camera"
    folder.mkdir()
    # Doc A: 2 pages a minute apart (one sideways); then a photo; doc B much later.
    text_page(folder / "IMG_20260301_100000.jpg")
    text_page(folder / "IMG_20260301_100100.jpg", rotate_ccw=90)
    _photo(folder / "IMG_20260301_100130.jpg", datetime(2026, 3, 1, 10, 1, 30))
    text_page(folder / "IMG_20260301_120000.jpg")

    infos = scan_folder(folder, ScanSettings(workers=2), _tess())
    assert [i.is_document for i in infos] == [True, True, False, True]
    plan = Plan.from_scan(infos, 180)
    assert [len(g) for g in plan.groups] == [2, 1]

    # Second scan hits the cache and gives the same answer.
    again = scan_folder(folder, ScanSettings(workers=2), _tess())
    assert [i.words for i in again] == [i.words for i in infos]

    out_dir = folder / "PDFs"
    docs = [(n, [p.path for p in g]) for n, g in zip(plan.output_names(), plan.groups)]
    res = save_documents(docs, out_dir, Options(ocr=True))
    assert not res.warnings, res.warnings
    assert [len(PdfReader(str(p)).pages) for p in res.outputs] == [2, 1]
    assert "brown fox" in PdfReader(str(res.outputs[0])).pages[1].extract_text().lower()


def _tess():
    from photo2pdf.core import Tesseract
    return Tesseract.find()
