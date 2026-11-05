"""Straighten phone photos of pages: perspective correction + deskew.

1. find_page_quad: locate the sheet of paper as a 4-corner shape.
   Two independent detectors run on a 1000 px copy and the better candidate
   wins:
     * edges: Canny -> dilate -> contours -> 4-point polygon approximation
     * brightness: Otsu threshold (paper is usually the brightest large
       region) -> largest blob -> polygon approximation, then min-area
       rectangle as a fallback
   A candidate must be convex, cover 8-97% of the frame, and have corner
   angles within 45-135 degrees. When the page already fills the frame (a
   tight crop or an existing scan) nothing is found and the image is left
   alone.
2. warp_to_quad: a perspective transform maps the four corners to a
   rectangle. Output aspect comes from the averaged side lengths and snaps
   to A4 / US Letter when it is within 6%.
3. estimate_skew / deskew: corrects small leftover rotation (up to +-10
   degrees) by picking the angle that maximises the variance of the
   horizontal projection profile of the text (text lines line up as sharp
   peaks). It is only applied when the gain over 0 degrees is clear, so
   pages without text are not rotated by noise.

Not handled: curved pages (open books), folds, multiple pages in one photo.
0/90/180/270 orientation is handled separately with Tesseract (see core).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

WORK_EDGE = 1000
INSET_PX = 5  # at WORK_EDGE scale
A4 = 297 / 210
LETTER = 11 / 8.5


def _cv2():
    import cv2  # lazy: heavy import, only needed when straightening

    return cv2


def _order(pts: np.ndarray) -> np.ndarray:
    """Return corners as top-left, top-right, bottom-right, bottom-left."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
                    dtype=np.float32)


def _angles_ok(q: np.ndarray, lo: float = 45, hi: float = 135) -> bool:
    for i in range(4):
        a, b, c = q[i - 1], q[i], q[(i + 1) % 4]
        v1, v2 = a - b, c - b
        cos = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
        ang = np.degrees(np.arccos(np.clip(cos, -1, 1)))
        if not lo <= ang <= hi:
            return False
    return True


def _valid(q: Optional[np.ndarray], area_img: float) -> bool:
    if q is None:
        return False
    cv2 = _cv2()
    if not cv2.isContourConvex(q.reshape(4, 1, 2).astype(np.int32)):
        return False
    frac = cv2.contourArea(q) / area_img
    return 0.08 <= frac <= 0.97 and _angles_ok(q)


def _quad_from_contour(c, allow_rect_fallback: bool) -> Optional[np.ndarray]:
    cv2 = _cv2()
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.03, 0.05):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            return _order(approx)
    if allow_rect_fallback:
        return _order(cv2.boxPoints(cv2.minAreaRect(hull)))
    return None


def _edge_candidates(gray: np.ndarray) -> list[np.ndarray]:
    cv2 = _cv2()
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    med = float(np.median(blur))
    edges = cv2.Canny(blur, int(max(0, 0.66 * med)), int(min(255, 1.33 * med)))
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:5]
    return [q for q in (_quad_from_contour(c, False) for c in cnts) if q is not None]


def _bright_candidates(gray: np.ndarray) -> list[np.ndarray]:
    cv2 = _cv2()
    blur = cv2.GaussianBlur(gray, (9, 9), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = np.ones((15, 15), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k)  # fill in the text
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, k)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:2]
    return [q for q in (_quad_from_contour(c, True) for c in cnts) if q is not None]


def find_page_quad(rgb: np.ndarray) -> Optional[np.ndarray]:
    """Corners (TL, TR, BR, BL) of the page in `rgb` pixel coordinates, or None."""
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    scale = WORK_EDGE / max(h, w)
    small = cv2.resize(rgb, (max(1, round(w * scale)), max(1, round(h * scale))),
                       interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    area = float(gray.shape[0] * gray.shape[1])

    best, best_score = None, 0.0
    for q in _edge_candidates(gray) + _bright_candidates(gray):
        if not _valid(q, area):
            continue
        # Prefer the larger shape whose inside is brighter than its surroundings.
        mask = np.zeros(gray.shape, np.uint8)
        cv2.fillConvexPoly(mask, q.astype(np.int32), 255)
        inside = float(gray[mask > 0].mean())
        outside = float(gray[mask == 0].mean()) if (mask == 0).any() else inside
        contrast = max(inside - outside, 0.0)
        score = cv2.contourArea(q) / area * (1.0 + contrast / 64.0)
        if contrast < 8:  # page not distinguishable from background
            continue
        if score > best_score:
            best, best_score = q, score
    if best is None:
        return None
    # Edge dilation pushes contours a few px outward; pull corners back in so
    # no background strip is left along the page edge.
    c = best.mean(axis=0)
    v = c - best
    best = best + v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9) * INSET_PX
    return best / scale


def true_aspect(q: np.ndarray, image_size: tuple[int, int]) -> Optional[float]:
    """Real width/height of the photographed rectangle, undoing perspective.

    Zhang & He, "Whiteboard scanning and image enhancement", Digital Signal
    Processing 17 (2007) 414-432
    (https://www.microsoft.com/en-us/research/?p=326696): assumes
    square pixels and the principal point at the image centre, and solves
    for the focal length from the quad. Returns None when the geometry is
    degenerate or inconsistent; the caller then uses side lengths.
    """
    w_img, h_img = image_size
    u0, v0 = w_img / 2.0, h_img / 2.0
    tl, tr, br, bl = [np.array([p[0], p[1], 1.0]) for p in q]
    m1, m2, m3, m4 = tl, tr, bl, br
    den2 = np.dot(np.cross(m2, m4), m3)
    den3 = np.dot(np.cross(m3, m4), m2)
    if abs(den2) < 1e-9 or abs(den3) < 1e-9:
        return None
    k2 = np.dot(np.cross(m1, m4), m3) / den2
    k3 = np.dot(np.cross(m1, m4), m2) / den3
    n2 = k2 * m2 - m1
    n3 = k3 * m3 - m1
    if abs(n2[2] * n3[2]) < 1e-9:
        # Viewed straight on: no perspective to undo.
        return float(np.hypot(n2[0], n2[1]) / np.hypot(n3[0], n3[1]))
    f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 * u0)
           + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 * v0)) \
        / (n2[2] * n3[2])
    if f2 <= 0:
        return None
    f = np.sqrt(f2)
    a_inv = np.linalg.inv(np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]]))
    b = a_inv.T @ a_inv
    num, den = n2 @ b @ n2, n3 @ b @ n3
    if num <= 0 or den <= 0:
        return None
    r = float(np.sqrt(num / den))
    return r if 0.2 < r < 5 else None


def _target_size(q: np.ndarray, image_size: Optional[tuple[int, int]] = None) -> tuple[int, int]:
    tl, tr, br, bl = q
    w = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    h = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    aspect = true_aspect(q, image_size) if image_size else None
    if aspect is not None:
        # Keep the larger measured side as resolution, fix the other from the true aspect.
        if w >= h * aspect:
            h = w / aspect
        else:
            w = h * aspect
    long_, short = max(w, h), max(min(w, h), 1.0)
    ratio = long_ / short
    for std in (A4, LETTER):
        if abs(ratio - std) / std <= 0.06:
            short = long_ / std
            break
    return (round(long_), round(short)) if w >= h else (round(short), round(long_))


def warp_to_quad(rgb: np.ndarray, q: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    tw, th = _target_size(q, (rgb.shape[1], rgb.shape[0]))
    dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(q.astype(np.float32), dst)
    return cv2.warpPerspective(rgb, m, (tw, th), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _profile_score(binary: np.ndarray, angle: float) -> float:
    cv2 = _cv2()
    h, w = binary.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(binary, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    # Score only the centre so the empty corners introduced by rotating don't count.
    ch, cw = int(h * 0.15), int(w * 0.15)
    rows = rot[ch:h - ch, cw:w - cw].sum(axis=1, dtype=np.float64)
    return float(np.var(rows))


def estimate_skew(rgb_or_gray: np.ndarray, limit: float = 10.0) -> float:
    """Degrees to rotate counter-clockwise (OpenCV/PIL convention) to level the text; 0 if unsure."""
    cv2 = _cv2()
    img = rgb_or_gray
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    h, w = img.shape
    scale = 800 / max(h, w)
    small = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                       interpolation=cv2.INTER_AREA)
    binary = cv2.adaptiveThreshold(small, 1, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    ink = float(binary.mean())
    if not 0.005 <= ink <= 0.25:  # blank, or not text-like (photo, noise)
        return 0.0
    base = _profile_score(binary, 0.0)
    coarse = np.arange(-limit, limit + 0.01, 0.5)
    scores = [_profile_score(binary, a) for a in coarse]
    best = float(coarse[int(np.argmax(scores))])
    fine = np.clip(np.arange(best - 0.5, best + 0.51, 0.1), -limit, limit)
    fscores = [_profile_score(binary, a) for a in fine]
    best = float(fine[int(np.argmax(fscores))])
    if abs(best) < 0.3 or max(fscores) < base * 1.10:
        return 0.0
    return round(best, 1)


def rotate_bound(rgb: np.ndarray, angle: float) -> np.ndarray:
    """Rotate counter-clockwise by `angle` degrees, enlarging the canvas, edges replicated."""
    cv2 = _cv2()
    h, w = rgb.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(rgb, m, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def straighten(rgb: np.ndarray, perspective: bool = True, deskew: bool = True) -> tuple[np.ndarray, dict]:
    """Apply perspective correction then deskew. Returns (image, info dict for logging/tests)."""
    info: dict = {"quad": None, "skew": 0.0}
    out = rgb
    if perspective:
        q = find_page_quad(rgb)
        if q is not None:
            out = warp_to_quad(rgb, q)
            info["quad"] = q
    if deskew:
        ang = estimate_skew(out)
        if ang:
            out = rotate_bound(out, ang)
            info["skew"] = ang
    return out, info
