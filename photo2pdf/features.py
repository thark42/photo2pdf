"""Measurements taken from each photo, used by classify.py.

Nothing here decides anything; it only measures. All values are cheap
(a few tens of milliseconds on a 1600 px image) except OCR, which detect.py
already runs.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np

SCREENSHOT_NAME = re.compile(r"screenshot|schermafbeelding|screen ?shot|bildschirmfoto|capture d.?[ée]cran", re.I)
# Portrait phone screens are long and thin; photos from phone cameras are 4:3 or 16:9.
_PHONE_SCREEN_ASPECTS = (2.0, 2.056, 2.111, 2.164, 2.167, 2.222, 2.4)


def ocr_stats(tsv: str, min_conf: float = 60.0) -> dict:
    """Confidence and text-size statistics from Tesseract TSV (word rows only)."""
    confs, heights, lines = [], [], {}
    page_h = 0
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12:
            continue
        if c[0] == "1":
            try:
                page_h = int(c[9])
            except ValueError:
                pass
        if c[0] != "5" or not c[11].strip():
            continue
        try:
            conf, h = float(c[10]), int(c[9])
        except ValueError:
            continue
        if conf < 0:
            continue
        confs.append(conf)
        if conf >= min_conf:
            heights.append(h)
            key = (c[1], c[2], c[3], c[4])
            lines[key] = lines.get(key, 0) + 1
    words_per_line = sorted(lines.values())
    return {
        "mean_conf": float(np.mean(confs)) if confs else 0.0,
        "median_h_ratio": (float(np.median(heights)) / page_h) if heights and page_h else 0.0,
        "lines": len(lines),
        "long_lines": sum(1 for n in words_per_line if n >= 5),
    }


def sharpness(gray) -> float:
    """Variance of the Laplacian on a 1000 px copy. Low = blurred or out of focus."""
    import cv2

    a = np.asarray(gray.convert("L"))
    s = 1000 / max(a.shape)
    if abs(s - 1) > 0.01:
        a = cv2.resize(a, (max(1, int(a.shape[1] * s)), max(1, int(a.shape[0] * s))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(a, cv2.CV_64F).var())


def ink_density(gray) -> float:
    """Fraction of dark strokes on a light background (handwriting, print)."""
    import cv2

    a = np.asarray(gray.convert("L"))
    s = 800 / max(a.shape)
    a = cv2.resize(a, (max(1, int(a.shape[1] * s)), max(1, int(a.shape[0] * s))), interpolation=cv2.INTER_AREA)
    b = cv2.adaptiveThreshold(a, 1, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15)
    return float(b.mean())


def dhash(gray, size: int = 16) -> int:
    """Difference hash (size*size bits) of an upright page: near-identical images differ in few bits."""
    from PIL import Image

    im = gray.convert("L").resize((size + 1, size), Image.LANCZOS)
    a = np.asarray(im, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def minhash(text: str, k: int = 32) -> list[int]:
    """Small MinHash signature of the word set. Not reversible to the text, so it can be cached."""
    words = {w.lower() for w in re.findall(r"[^\W\d_]{3,}", text or "")}
    if not words:
        return []
    sig = []
    for i in range(k):
        salt = str(i).encode()
        sig.append(min(int.from_bytes(hashlib.blake2b(salt + w.encode(), digest_size=8).digest(), "big")
                       for w in words))
    return sig


def minhash_similarity(a: list[int], b: list[int]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


_codes_lock = threading.Lock()


def find_codes(rgb) -> dict:
    """Whether a QR code or a 1D barcode is visible (positions/contents are not kept).

    Serialised: OpenCV's QR/barcode detectors are not used concurrently from
    the scan's worker threads (a native crash was seen on Windows CI when they were).
    """
    with _codes_lock:
        return _find_codes(rgb)


def _find_codes(rgb) -> dict:
    import cv2

    a = np.asarray(rgb.convert("RGB"))
    s = 1400 / max(a.shape[:2])
    if s < 1:
        a = cv2.resize(a, (int(a.shape[1] * s), int(a.shape[0] * s)), interpolation=cv2.INTER_AREA)
    out = {"qr": False, "barcode": False}
    try:
        ok, pts = cv2.QRCodeDetector().detect(a)
        out["qr"] = bool(ok and pts is not None)
    except Exception:  # noqa: BLE001
        pass
    try:
        res = cv2.barcode.BarcodeDetector().detect(a)   # (found, corner points)
        ok, pts = res[0], res[1]
        out["barcode"] = bool(ok and pts is not None and len(pts))
    except Exception:  # noqa: BLE001
        pass
    return out


def is_screenshot(path: Path, raw, size: tuple[int, int]) -> bool:
    """Screen captures: a screenshot-like file name, or a PNG without camera data in screen proportions."""
    if SCREENSHOT_NAME.search(path.name):
        return True
    if path.suffix.lower() != ".png":
        return False
    try:
        exif = raw.getexif()
        if exif.get(0x010F) or exif.get(0x0110):   # camera Make / Model
            return False
    except Exception:  # noqa: BLE001
        pass
    w, h = size
    long_, short = max(w, h), max(min(w, h), 1)
    return any(abs(long_ / short - a) < 0.02 for a in _PHONE_SCREEN_ASPECTS) or (w, h) in (
        (1920, 1080), (2560, 1440), (1366, 768), (1440, 900), (2880, 1800), (1280, 800))


def page_aspect(im) -> float:
    w, h = im.size
    return max(w, h) / max(min(w, h), 1)


def first(it, default: Optional[object] = None):
    return next(iter(it), default)
