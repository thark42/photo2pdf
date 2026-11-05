import numpy as np
import pytest

from photo2pdf import geometry as g
from synth import camera_corners, page, photo_of

CASES = [dict(), dict(dist=420), dict(tilt_x=40, tilt_y=-15, roll=-20),
         dict(tilt_x=10, tilt_y=5, roll=3, dist=400), dict(tilt_x=35, roll=95, dist=600),
         dict(tilt_x=-30, tilt_y=25, roll=170, dist=450), dict(tilt_x=50, dist=380)]


@pytest.mark.parametrize("i,kw", list(enumerate(CASES)))
def test_page_found_and_aspect_restored(i, kw):
    corners = camera_corners(**kw)
    img = np.asarray(photo_of(page(), corners, seed=i))
    q = g.find_page_quad(img)
    assert q is not None
    err = max(min(np.hypot(*(p - np.array(c))) for c in corners) for p in q)
    assert err < 15, err  # px in a 2400x1800 frame
    out, _ = g.straighten(img)
    ratio = max(out.shape[:2]) / min(out.shape[:2])
    assert abs(ratio - 297 / 210) < 0.03


def test_tight_crop_left_alone():
    img = np.asarray(page())
    assert g.find_page_quad(img) is None


def test_plain_photo_no_quad_no_skew():
    rng = np.random.default_rng(1)
    img = (rng.random((900, 1200, 3)) * 255).astype(np.uint8)
    out, info = g.straighten(img)
    assert info["quad"] is None and info["skew"] == 0.0


@pytest.mark.parametrize("angle", [-8, -3, 2.5, 6])
def test_deskew(angle):
    rotated = g.rotate_bound(np.asarray(page()), angle)
    assert g.estimate_skew(rotated) == pytest.approx(-angle, abs=0.3)
    assert g.estimate_skew(np.asarray(page())) == 0.0


def test_true_aspect_front_on():
    q = np.array([[100, 100], [310, 100], [310, 397], [100, 397]], dtype=np.float32)
    assert g.true_aspect(q, (1000, 800)) == pytest.approx(210 / 297, abs=0.01)
