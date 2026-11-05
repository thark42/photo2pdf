"""Each native (OpenCV / Pillow) feature call on its own, in the main thread, before the big
scans. If one of them crashes the interpreter on a platform, the last test name printed in a
verbose run names it."""
import pytest
from PIL import Image, ImageDraw

from photo2pdf import features as ft
from synth import screenshot, receipt, page


@pytest.fixture(scope="module")
def imgs():
    return {"page": page(), "receipt": receipt(), "shot": screenshot(["KLM", "Boarding pass"], qr="X1"),
            "blank": Image.new("RGB", (800, 600), "white")}


@pytest.mark.parametrize("name", ["page", "receipt", "shot", "blank"])
def test_sharpness(imgs, name):
    assert ft.sharpness(imgs[name]) >= 0


@pytest.mark.parametrize("name", ["page", "receipt", "shot", "blank"])
def test_ink(imgs, name):
    assert 0 <= ft.ink_density(imgs[name].convert("L")) <= 1


@pytest.mark.parametrize("name", ["page", "receipt", "shot", "blank"])
def test_dhash(imgs, name):
    assert ft.dhash(imgs[name]) >= 0


def test_qr_only(imgs):
    import cv2
    import numpy as np
    ok, pts = cv2.QRCodeDetector().detect(np.asarray(imgs["shot"]))
    assert ok


def test_barcode_only(imgs):
    import cv2
    import numpy as np
    res = cv2.barcode.BarcodeDetector().detect(np.asarray(imgs["page"]))
    assert res[0] in (True, False)


@pytest.mark.parametrize("name", ["page", "receipt", "shot", "blank"])
def test_find_codes(imgs, name):
    assert set(ft.find_codes(imgs[name])) == {"qr", "barcode"}


def test_png_exif_and_screenshot(tmp_path, imgs):
    p = tmp_path / "Screenshot_20250101-000000.png"
    imgs["shot"].save(p)
    with Image.open(p) as raw:
        assert ft.is_screenshot(p, raw, raw.size)
        raw.getexif()
