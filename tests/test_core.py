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


_VARIANT_WORDS = ["alpha", "harbour", "window", "garden", "lantern", "meadow", "copper", "silver",
                  "orchard", "pillow", "thunder", "violet", "maple", "anchor", "barley", "candle"]


def text_page(path: Path, rotate_ccw: int = 0, size=(1700, 2200), mode="RGB", exif_orientation=None,
              variant=None):
    """A clean text page. Pages written to different file names get different words by default,
    so they are distinct pages rather than duplicates of one another."""
    im = Image.new(mode, size, "white")
    d = ImageDraw.Draw(im)
    v = variant if variant is not None else sum(map(ord, Path(path).name))
    extra = " ".join(_VARIANT_WORDS[(v + k * 5) % len(_VARIANT_WORDS)] for k in range(3))
    for i in range(12):
        d.text((120, 150 + i * 80), f"Line {i} the quick brown fox {extra if i % 2 else 'jumps'}",
               fill="black", font=_font())
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
    assert res.skipped == ["bad.jpg"]
    assert "1 photo could not be opened and was left out." in res.summary_lines()


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
    assert res.no_text == ["a.jpg"]
    assert any("saved but not searchable" in ln for ln in res.summary_lines())


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


def test_iphone_style_heic_orientation_and_capture_time(tmp_path):
    """iPhones store HEIC pixels sideways plus an orientation tag, and the capture time only
    in EXIF. The page must come out the way it was held, with the EXIF time."""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    from photo2pdf.core import _open_image, load_page
    from photo2pdf.detect import _exif_time

    portrait = Image.new("RGB", (600, 900), "white")
    ImageDraw.Draw(portrait).rectangle([0, 0, 599, 99], fill="black")   # marks the top edge
    exif = Image.Exif()
    exif[0x0112] = 6
    exif.get_ifd(0x8769)[0x9003] = "2024:06:01 09:30:00"
    src = tmp_path / "IMG_4821.HEIC"
    try:
        portrait.rotate(90, expand=True).save(src, format="HEIF", exif=exif.tobytes(), quality=95)
    except Exception as exc:  # encoder may be missing on some builds
        pytest.skip(f"no HEIF encoder: {exc}")

    with _open_image(src) as raw:
        assert _exif_time(raw) == datetime(2024, 6, 1, 9, 30)
    im = load_page(src, Options(straighten=False), max_edge=2000)
    assert im.size == (600, 900)                       # portrait, as held
    top = im.convert("L").crop((0, 0, 600, 80))
    assert top.getextrema()[1] < 80                    # black band is at the top, not rotated twice


def test_tesseract_output_is_decoded_as_utf8(monkeypatch):
    """Windows otherwise decodes with cp1252 and loses the whole output on bytes like 0x9d."""
    import subprocess as sp
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(sp, "run", fake_run)
    Tesseract(Path("tesseract"))._run(["--version"], cwd=Path("."))
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"


def test_weak_osd_outside_vote_leaders_does_not_rotate(tmp_path, monkeypatch):
    from photo2pdf import core
    state = {"deg": 0}
    orig = core.rotate_cw

    def spy(im, deg):
        state["deg"] = deg % 360
        return orig(im, deg)
    monkeypatch.setattr(core, "rotate_cw", spy)

    class FakeTess:
        def osd(self, p):
            return (180, 0.58)          # weak guess

        def word_count(self, p, lang, psm=3, threads=None):
            return {0: 31, 90: 31, 180: 9, 270: 8}[state["deg"]]
    assert core.find_rotation(Image.new("RGB", (400, 1000), "white"), FakeTess(), "eng", tmp_path) == 0
