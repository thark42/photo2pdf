import shutil
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from photo2pdf.core import (
    Options, Tesseract, collect_images, combined_output_path, convert,
    prepare_page, unique_path,
)

HAS_TESS = shutil.which("tesseract") is not None
needs_tess = pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")


def _font(size=48):
    for name in ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def text_page(path: Path, rotate_ccw: int = 0, size=(1700, 2200), mode="RGB", exif_orientation=None):
    im = Image.new(mode, size, "white")
    d = ImageDraw.Draw(im)
    for i in range(12):
        d.text((120, 150 + i * 80), f"Line {i} the quick brown fox jumps", fill="black", font=_font())
    if rotate_ccw:
        im = im.rotate(rotate_ccw, expand=True)
    kw = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kw["exif"] = exif
    im.save(path, **kw)
    return path


def test_collect_images_natural_sort_filters_and_expands_dirs(tmp_path):
    for n in ("IMG_10.jpg", "IMG_2.JPG", "IMG_1.png", "notes.txt"):
        (tmp_path / n).write_bytes(b"x")
    got = collect_images([tmp_path, tmp_path / "IMG_2.JPG"])  # duplicate removed
    assert [p.name for p in got] == ["IMG_1.png", "IMG_2.JPG", "IMG_10.jpg"]


def test_unique_path_never_overwrites(tmp_path):
    p = tmp_path / "Scan.pdf"
    assert unique_path(p) == p
    p.write_bytes(b"")
    assert unique_path(p).name == "Scan (2).pdf"
    (tmp_path / "Scan (2).pdf").write_bytes(b"")
    assert unique_path(p).name == "Scan (3).pdf"


def test_combined_output_path_name(tmp_path):
    out = combined_output_path([tmp_path / "a.jpg"], now=datetime(2026, 1, 2, 3, 4))
    assert out == tmp_path / "Scan 2026-01-02 03.04.pdf"


def test_prepare_page_applies_exif_and_caps_size(tmp_path):
    # Landscape pixels + EXIF orientation 6 (rotate 90 CW) => portrait page.
    src = text_page(tmp_path / "in.jpg", size=(6000, 4000), exif_orientation=6)
    dst = prepare_page(src, tmp_path / "out.jpg", Options(max_long_edge=3000))
    with Image.open(dst) as im:
        assert im.height > im.width
        assert max(im.size) == 3000
        dpi = im.info["dpi"][0]
        assert 250 < dpi < 300  # ~A4-sized page


def test_prepare_page_flattens_transparency_and_grayscale(tmp_path):
    src = tmp_path / "a.png"
    Image.new("RGBA", (400, 600), (0, 0, 0, 0)).save(src)
    dst = prepare_page(src, tmp_path / "o.jpg", Options(grayscale=True))
    with Image.open(dst) as im:
        assert im.mode == "L"
        assert im.getpixel((10, 10)) > 240  # transparent -> white


def test_convert_without_ocr_combined_and_separate(tmp_path):
    for i in range(3):
        text_page(tmp_path / f"IMG_{i}.jpg")
    res = convert([tmp_path], Options(ocr=False, combine=True))
    assert len(res.outputs) == 1 and not res.warnings
    assert len(PdfReader(str(res.outputs[0])).pages) == 3

    res = convert([tmp_path], Options(ocr=False, combine=False))
    assert sorted(p.name for p in res.outputs) == ["IMG_0.pdf", "IMG_1.pdf", "IMG_2.pdf"]


def test_convert_reports_bad_image_and_continues(tmp_path):
    text_page(tmp_path / "good.jpg")
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    res = convert([tmp_path], Options(ocr=False))
    assert len(res.outputs) == 1
    assert any("bad.jpg" in w for w in res.warnings)


def test_convert_no_images(tmp_path):
    res = convert([tmp_path], Options(ocr=False))
    assert not res.outputs and res.warnings


def test_missing_tesseract_falls_back_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(Tesseract, "find", classmethod(lambda cls: None))
    text_page(tmp_path / "a.jpg")
    res = convert([tmp_path], Options(ocr=True))
    assert len(res.outputs) == 1
    assert any("not searchable" in w for w in res.warnings)


@needs_tess
def test_ocr_produces_searchable_text_and_fixes_rotation(tmp_path):
    text_page(tmp_path / "IMG_1.jpg")
    text_page(tmp_path / "IMG_2.jpg", rotate_ccw=90)   # sideways
    text_page(tmp_path / "IMG_3.jpg", rotate_ccw=180)  # upside down
    res = convert([tmp_path], Options(ocr=True, combine=True))
    assert not res.warnings, res.warnings
    pages = PdfReader(str(res.outputs[0])).pages
    assert len(pages) == 3
    for page in pages:
        assert "brown fox" in (page.extract_text() or "").lower()
        assert page.mediabox.height > page.mediabox.width  # upright portrait


@needs_tess
def test_detect_rotation(tmp_path):
    tess = Tesseract.find()
    up = prepare_page(text_page(tmp_path / "a.jpg"), tmp_path / "a2.jpg", Options())
    side = prepare_page(text_page(tmp_path / "b.jpg", rotate_ccw=90), tmp_path / "b2.jpg", Options())
    assert tess.detect_rotation(up) == 0
    assert tess.detect_rotation(side) == 90


@needs_tess
def test_bad_language_falls_back_to_image_only(tmp_path):
    text_page(tmp_path / "a.jpg")
    res = convert([tmp_path], Options(ocr=True, language="zzz"))
    assert len(res.outputs) == 1
    assert any("text recognition failed" in w for w in res.warnings)


def test_heic_input(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    src = tmp_path / "IMG_0001.HEIC"
    try:
        Image.new("RGB", (800, 1000), "white").save(src, format="HEIF")
    except Exception as exc:  # encoder may be missing on some builds
        pytest.skip(f"no HEIF encoder: {exc}")
    res = convert([src], Options(ocr=False))
    assert len(res.outputs) == 1 and not res.warnings
