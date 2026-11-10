"""Image -> PDF conversion logic. No GUI code here so it is testable on Linux.

Pipeline per photo:
  1. Pillow opens it (JPEG/PNG/HEIC/...), applies the EXIF rotation the phone
     recorded, flattens transparency, optionally converts to greyscale, caps
     the resolution, and writes a temporary JPEG with a DPI that makes the
     page roughly A4-sized.
  2. If OCR is on and Tesseract is available: Tesseract's orientation
     detection fixes sideways/upside-down pages, then Tesseract renders a
     one-page PDF (image + invisible text layer).
     Otherwise img2pdf wraps the JPEG into a PDF page without re-encoding.
  3. pypdf joins the pages when "one PDF for all photos" is selected.

A page that fails OCR falls back to an image-only page instead of aborting
the whole batch; the failure is reported as a warning.
"""

from __future__ import annotations

import logging
import os
import re
import time
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".tif", ".tiff", ".bmp", ".gif",
}

# A4 in inches. Used only to pick a DPI so pages open at a sensible size.
A4_W_IN = 8.27
A4_H_IN = 11.69
# ISO/IEC 7810 ID-1 card in inches (85.60 x 53.98 mm): cards come out at real size.
CARD_W_IN = 85.60 / 25.4
CARD_H_IN = 53.98 / 25.4
RECEIPT_W_IN = 80 / 25.4   # common till-roll width

ProgressFn = Callable[[int, int, str], None]


@dataclass
class Options:
    combine: bool = True          # one PDF for all photos vs one per photo
    ocr: bool = True              # searchable text layer
    grayscale: bool = False       # smaller files
    auto_rotate: bool = True      # 0/90/180/270 orientation fix (needs OCR engine)
    straighten: bool = True       # perspective correction + deskew (OpenCV)
    language: str = "nld+eng"     # Tesseract language code(s)
    auto_name: bool = True        # name PDFs from their content (date, sender, type)
    organize: str = "year"        # folder mode: subfolders "none" | "year" | "sender" | "sender_year"
    max_long_edge: int = 3508     # px; 3508 = A4 long edge at 300 dpi
    jpeg_quality: int = 85
    thumb_size: str = "Medium"    # review screen: "Small" | "Medium" | "Large"


@dataclass
class PageSpec:
    """A page to render. rotation=None means detect automatically; an int is
    the clockwise turn to apply after straightening (from the review screen)."""

    path: Path
    rotation: Optional[int] = None
    kind: Optional[str] = None   # "card" / "receipt" -> real size; "screenshot" -> never warped;
                                 # "page" -> A4; None -> decided from the photo


@dataclass
class Result:
    """Outcome of a run, kept structured so the UI can explain it in plain words."""

    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # other notes, already plain language
    skipped: list[str] = field(default_factory=list)    # photo names that could not be opened
    no_text: list[str] = field(default_factory=list)    # saved, but without searchable text
    errors: list[str] = field(default_factory=list)     # unexpected failures (worth a report)
    stopped: bool = False
    saved: list[tuple[int, Path]] = field(default_factory=list)  # folder mode: (document index, pdf)

    def problems(self) -> bool:
        return bool(self.warnings or self.skipped or self.no_text or self.errors)

    def summary_lines(self) -> list[str]:
        """Short plain-language lines for the end-of-run message."""
        lines = []
        n = len(self.outputs)
        if n:
            lines.append(f"Saved {n} PDF{'s' if n != 1 else ''}.")
        elif not self.stopped:
            lines.append("Nothing was saved.")
        if self.stopped:
            lines.append("You stopped before the end. Everything finished so far has been kept.")
        if self.skipped:
            k = len(self.skipped)
            lines.append(f"{k} photo{'s' if k != 1 else ''} could not be opened and "
                         f"{'were' if k != 1 else 'was'} left out.")
        if self.no_text:
            k = len(self.no_text)
            lines.append(f"{k} page{'s' if k != 1 else ''} could not be read, so "
                         f"{'they are' if k != 1 else 'it is'} saved but not searchable.")
        lines.extend(self.warnings)
        if self.errors:
            lines.append("Something unexpected went wrong. A problem report would help fix it.")
        return lines

    def detail_lines(self) -> list[str]:
        out = []
        out += [f"Could not open: {n}" for n in self.skipped]
        out += [f"Not searchable: {n}" for n in self.no_text]
        out += [f"Error: {e}" for e in self.errors]
        return out


def is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=folder, prefix=".photo2pdf-", delete=True)
        probe.close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def natural_key(p: Path) -> list:
    """Sort 'IMG_2.jpg' before 'IMG_10.jpg'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def collect_images(paths: Iterable[str | os.PathLike]) -> list[Path]:
    """Expand folders (one level), keep supported images, natural-sort, dedupe.

    Windows does not pass dropped / Send-To files in a reliable order, so
    order is always by filename. Phone cameras name files by timestamp or
    counter, which makes filename order the capture order.
    """
    found: dict[Path, None] = {}
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in p.iterdir():
                if child.is_file() and child.suffix.lower() in IMAGE_EXTS:
                    found[child.resolve()] = None
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            found[p.resolve()] = None
    return sorted(found, key=natural_key)


def unique_path(path: Path) -> Path:
    """Never overwrite: 'Scan.pdf' -> 'Scan (2).pdf' -> 'Scan (3).pdf'."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def fallback_dir() -> Path:
    return Path.home() / "Documents" / "Photos to PDF"


def output_dir_for(folder: Path) -> Path:
    """The photos' own folder if writable, else Documents/Photos to PDF.

    Covers photos opened straight from a phone, a camera card, or a
    read-only share.
    """
    try:
        probe = tempfile.NamedTemporaryFile(dir=folder, prefix=".photo2pdf-", delete=True)
        probe.close()
        return folder
    except OSError:
        d = fallback_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d


def combined_output_path(images: list[Path], now: Optional[datetime] = None) -> Path:
    now = now or datetime.now()
    name = f"Scan {now:%Y-%m-%d %H.%M}.pdf"
    return unique_path(output_dir_for(images[0].parent) / name)


# --------------------------------------------------------------------------
# Tesseract discovery + invocation
# --------------------------------------------------------------------------

def _bundle_roots() -> list[Path]:
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).parent)
    roots.append(Path(__file__).resolve().parent.parent)
    return roots


@dataclass
class Tesseract:
    exe: Path
    tessdata: Optional[Path] = None

    @classmethod
    def find(cls) -> Optional["Tesseract"]:
        override = os.environ.get("PHOTO2PDF_TESSERACT")
        if override and Path(override).is_file():
            return cls(Path(override), _tessdata_next_to(Path(override)))
        exe_name = "tesseract.exe" if os.name == "nt" else "tesseract"
        for root in _bundle_roots():
            cand = root / "tesseract" / exe_name
            if cand.is_file():
                return cls(cand, _tessdata_next_to(cand))
        on_path = shutil.which("tesseract")
        if on_path:
            return cls(Path(on_path), None)  # system install knows its tessdata
        return None

    def _run(self, args: list[str], cwd: Path, timeout: int = 300,
             threads: Optional[int] = None) -> subprocess.CompletedProcess:
        cmd = [str(self.exe), *args]
        if self.tessdata:
            cmd += ["--tessdata-dir", str(self.tessdata)]
        kwargs = {}
        if os.name == "nt":
            # Stop a console window flashing up for every page.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        env = dict(os.environ, OMP_THREAD_LIMIT=str(threads or 4))
        # Tesseract writes UTF-8. Without an explicit encoding Windows decodes with the ANSI
        # code page (cp1252): bytes such as 0x9d (from a curly quote) raise in the reader
        # thread and the whole OCR output is lost. Decode as UTF-8 and never fail on a byte.
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, **kwargs,
        )

    def languages(self) -> set[str]:
        try:
            r = self._run(["--list-langs"], cwd=Path.cwd(), timeout=30)
        except (OSError, subprocess.SubprocessError):
            return set()
        lines = (r.stdout or "").splitlines()
        return {ln.strip() for ln in lines[1:] if ln.strip()}

    def osd(self, image: Path) -> Optional[tuple[int, float]]:
        """(clockwise degrees to turn upright, confidence) from Tesseract OSD, or None."""
        try:
            r = self._run([image.name, "stdout", "--psm", "0"], cwd=image.parent, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        out = (r.stdout or "") + (r.stderr or "")
        rot = re.search(r"Rotate:\s*(\d+)", out)
        conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
        if r.returncode != 0 or not rot or not conf:
            return None
        return int(rot.group(1)) % 360, float(conf.group(1))

    def detect_rotation(self, image: Path, min_confidence: float = 2.0) -> int:
        """OSD-only rotation (clockwise degrees); 0 when unsure. See find_rotation for the robust path."""
        res = self.osd(image)
        return res[0] if res and res[1] >= min_confidence else 0

    def word_count(self, image: Path, language: str, psm: int = 3, threads: Optional[int] = None) -> int:
        try:
            r = self._run([image.name, "stdout", "-l", language, "--psm", str(psm), "tsv"],
                          cwd=image.parent, timeout=120, threads=threads)
        except (OSError, subprocess.SubprocessError):
            return 0
        return count_words(r.stdout or "")

    def render_pdf(self, image: Path, language: str) -> Path:
        """One-page searchable PDF next to `image`. Raises RuntimeError on failure."""
        base = image.with_suffix("")
        r = self._run([image.name, base.name, "-l", language, "pdf"], cwd=image.parent)
        pdf = base.with_suffix(".pdf")
        if r.returncode != 0 or not pdf.is_file():
            msg = (r.stderr or "").strip().splitlines()
            raise RuntimeError(msg[-1] if msg else f"tesseract exit {r.returncode}")
        return pdf


def _tessdata_next_to(exe: Path) -> Optional[Path]:
    d = exe.parent / "tessdata"
    return d if d.is_dir() else None


# --------------------------------------------------------------------------
# Page preparation
# --------------------------------------------------------------------------

_heif_registered = False


def _open_image(src: Path):
    global _heif_registered
    from PIL import Image

    if not _heif_registered:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass  # HEIC unsupported; other formats still work
        _heif_registered = True
    return Image.open(src)


def flatten_lighting(im):
    """Even out shadows and uneven lighting on a greyscale page photo.

    Divides each pixel by a heavily blurred copy of the image (an estimate of
    the paper's local brightness), so paper goes near-white and ink stays
    dark. Standard background-normalisation trick; makes phone photos look
    closer to a scan and noticeably helps Tesseract on unevenly lit pages.
    """
    from PIL import ImageFilter, ImageMath

    im = im.convert("L")
    bg = im.filter(ImageFilter.GaussianBlur(radius=max(im.size) / 40))
    return ImageMath.lambda_eval(
        lambda a: a["float"](a["x"]) * 250 / (a["float"](a["b"]) + 1), x=im, b=bg
    ).convert("L")


_WORD_OK = re.compile(r"^[^\W_]{3,}$|^[^\W_][\w'’.,:/\-]{2,}$", re.UNICODE)


def count_words(tsv: str, min_conf: float = 60.0) -> int:
    """Confident, word-like tokens in Tesseract TSV output (>=3 chars, mostly alphanumeric)."""
    n = 0
    for line in tsv.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        text = cols[11].strip()
        if conf < min_conf or len(text) < 3:
            continue
        alnum = sum(ch.isalnum() for ch in text)
        if alnum / len(text) >= 0.7 and _WORD_OK.match(text):
            n += 1
    return n


def _resize_long(im, long_edge: int):
    from PIL import Image

    scale = long_edge / max(im.size)
    if abs(scale - 1) < 1e-3:
        return im
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def load_page(src: Path, opts: Options, max_edge: int = 5000):
    """Open a photo upright per EXIF, flatten alpha, cap size, straighten. Returns RGB PIL image."""
    from PIL import Image, ImageOps

    with _open_image(src) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im = im.convert("RGB")
    if max(im.size) > max_edge:
        im = _resize_long(im, max_edge)
    if opts.straighten:
        try:
            import numpy as np

            from photo2pdf.geometry import straighten

            t = time.perf_counter()
            arr, info = straighten(np.asarray(im))
            im = Image.fromarray(arr)
            im.info["photo2pdf_card"] = bool(info.get("card"))
            im.info["photo2pdf_quad"] = info.get("quad") is not None
            logging.info("straighten %s: page_found=%s skew=%.1f out=%dx%d (%.2fs)", src.name,
                         info["quad"] is not None, info["skew"], im.width, im.height,
                         time.perf_counter() - t)
        except ImportError:
            logging.warning("OpenCV not available; straightening skipped")
    return im


def rotate_cw(im, degrees: int):
    """Rotate a PIL image clockwise by a multiple of 90 (PIL rotates counter-clockwise)."""
    degrees %= 360
    return im.rotate(-degrees, expand=True) if degrees else im


def find_rotation(im, tess: Tesseract, language: str, work: Path,
                  osd_confident: float = 3.0, threads: Optional[int] = None,
                  prefer_landscape: bool = False) -> int:
    """Clockwise degrees (0/90/180/270) that make the page upright.

    1. Tesseract OSD on a cleaned 1600 px greyscale copy; accepted when its
       confidence is >= `osd_confident` (heuristic threshold).
    2. Otherwise OCR the copy at all four rotations and keep the one with the
       most confident words, if it clearly wins (>= 5 words and 1.3x the
       runner-up). Text read the wrong way up produces few confident words.
    3. Otherwise a weak OSD answer, else 0.
    """
    small = flatten_lighting(_resize_long(im.convert("L"), 1600))
    probe = work / "orient.png"
    small.save(probe)
    osd = tess.osd(probe)
    if osd and osd[1] >= osd_confident:
        logging.info("rotation=%d via OSD (confidence %.2f)", osd[0], osd[1])
        return osd[0]
    counts = {}
    for deg in (0, 90, 180, 270):
        rotate_cw(small, deg).save(probe)
        counts[deg] = tess.word_count(probe, language, psm=3, threads=threads)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    (best, n1), (_, n2) = ranked[0], ranked[1]
    if n1 >= 5 and n1 >= 1.3 * max(n2, 1):
        logging.info("rotation=%d via word vote %s (OSD said %s)", best, counts, osd)
        return best
    if n1 >= 5:
        # Close vote: break the tie with a weak OSD answer, then (cards) with landscape.
        contenders = [d for d, c in ranked if c * 1.3 >= n1]
        if osd and osd[0] in contenders:
            logging.info("rotation=%d via close vote %s + OSD %s", osd[0], counts, osd)
            return osd[0]
        keep = [d for d in contenders if d in (0, 180)] if (prefer_landscape and im.width >= im.height) else []
        if len(keep) == 1:
            logging.info("rotation=%d via close vote %s + landscape", keep[0], counts)
            return keep[0]
        if 0 in contenders:
            # Upright reads as well as anything else: leave it. A weak OSD answer the vote
            # has already ruled out (e.g. 180 with 9 words vs 31) must not turn the page.
            logging.info("rotation=0 via close vote %s (OSD %s not among the leaders)", counts, osd)
            return 0
    if osd and osd[1] >= 0.5:
        logging.info("rotation=%d via weak OSD %s; vote inconclusive %s", osd[0], osd, counts)
        return osd[0]
    logging.info("rotation=0 (undecided: OSD %s, vote %s)", osd, counts)
    return 0


def finish_page(im, opts: Options, rotation: int, dst: Path, kind: Optional[str] = None) -> Path:
    """Rotate, optionally clean to greyscale, cap resolution, write JPEG with a DPI that gives
    an A4-sized page, or a real-size card page for ID / bank / insurance / loyalty cards."""
    card = kind == "card" or (kind is None and im.info.get("photo2pdf_card", False))
    w0, h0 = im.size
    receipt = kind == "receipt" or (kind is None and im.info.get("photo2pdf_quad", False)
                                    and max(w0, h0) / max(min(w0, h0), 1) >= 2.0)
    im = rotate_cw(im, rotation)
    im = im.convert("L" if opts.grayscale else "RGB")
    if max(im.size) > opts.max_long_edge:
        im = _resize_long(im, opts.max_long_edge)
    if opts.grayscale:
        im = flatten_lighting(im)
    if card:
        w_in, h_in = (CARD_H_IN, CARD_W_IN) if im.height >= im.width else (CARD_W_IN, CARD_H_IN)
    elif receipt:
        short_px = min(im.width, im.height)
        dpi = max(short_px / RECEIPT_W_IN, 1.0)
        im.save(dst, "JPEG", quality=opts.jpeg_quality, dpi=(dpi, dpi), optimize=True)
        return dst
    else:
        w_in, h_in = (A4_W_IN, A4_H_IN) if im.height >= im.width else (A4_H_IN, A4_W_IN)
    dpi = max(im.width / w_in, im.height / h_in, 1.0)
    im.save(dst, "JPEG", quality=opts.jpeg_quality, dpi=(dpi, dpi), optimize=True)
    return dst


def prepare_page(src: Path, dst: Path, opts: Options, extra_rotation: int = 0) -> Path:
    """Write a normalised JPEG for one photo (no automatic orientation). Returns dst."""
    return finish_page(load_page(src, opts), opts, extra_rotation, dst)


def _image_only_pdf(jpg: Path) -> Path:
    import img2pdf

    out = jpg.with_suffix(".pdf")
    out.write_bytes(img2pdf.convert(str(jpg)))
    return out


def _merge(pages: list[Path], out: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for p in pages:
        writer.append(str(p))
    tmp = out.with_name(out.name + ".part")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    writer.close()
    os.replace(tmp, out)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _render_pages(
    images: list, work: Path, opts: Options, tess: Optional[Tesseract],
    result: Result, progress: ProgressFn, start: int, total: int, cancel=None,
) -> list[tuple[Path, Path]]:
    """One single-page PDF per image (Path or PageSpec) in `work`. Unreadable images are listed in result.skipped."""
    pages: list[tuple[Path, Path]] = []
    for k, item in enumerate(images):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            break
        spec = item if isinstance(item, PageSpec) else PageSpec(Path(item))
        src = spec.path
        n = start + k
        progress(n, total, f"Page {n + 1} of {total}: {src.name}")
        t0 = time.perf_counter()
        try:
            size_kb = src.stat().st_size // 1024
        except OSError:
            size_kb = -1
        logging.info("page %d/%d: %s (%d KB) rotation_given=%s", n + 1, total, src.name, size_kb,
                     spec.rotation)
        stem = f"p{n:05d}"  # ASCII temp names: Tesseract is fragile with non-ASCII paths
        try:
            screenshot = spec.kind == "screenshot"
            if spec.kind is None:
                from photo2pdf.features import is_screenshot

                with _open_image(src) as raw:
                    screenshot = is_screenshot(src, raw, raw.size)
            page_opts = opts
            if screenshot:
                from dataclasses import replace as _replace

                page_opts = _replace(opts, straighten=False)   # screen captures are already flat
            im = load_page(src, page_opts)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            logging.exception("could not read %s", src.name)
            result.skipped.append(src.name)
            continue
        rotation = spec.rotation
        if rotation is None:
            rotation = 0
            if tess and opts.auto_rotate and not screenshot:
                try:
                    rotation = find_rotation(im, tess, opts.language, work)
                except Exception as exc:  # noqa: BLE001
                    logging.exception("rotation check failed for %s", src.name)
        jpg = finish_page(im, opts, rotation, work / f"{stem}.jpg",
                          kind=None if spec.kind == "screenshot" else spec.kind)
        if tess and opts.ocr:
            try:
                pdf = tess.render_pdf(jpg, opts.language)
            except Exception as exc:  # noqa: BLE001
                logging.exception("OCR failed for %s", src.name)
                result.no_text.append(src.name)
                pdf = _image_only_pdf(jpg)
        else:
            pdf = _image_only_pdf(jpg)
        logging.info("page %d done: rotation=%d ocr=%s (%.2fs)", n + 1, rotation,
                     bool(tess and opts.ocr), time.perf_counter() - t0)
        pages.append((src, pdf))
    return pages


def _content_name(page_pdfs: list[Path]) -> Optional[str]:
    """Name from the OCR text layer of the first pages, or None if nothing clear was found."""
    from pypdf import PdfReader

    from photo2pdf.naming import extract_any, merge, suggest_name

    fields = []
    for pdf in page_pdfs[:3]:
        try:
            page = PdfReader(str(pdf)).pages[0]
            text = page.extract_text() or ""
            # A real-size card page is about 243 x 153 pt; A4 is 595 x 842.
            small = max(float(page.mediabox.width), float(page.mediabox.height)) < 400
            fields.append(extract_any(text, card_shape=small))
        except Exception:  # noqa: BLE001
            continue
    f = merge(fields)
    from photo2pdf import diag

    name = suggest_name(f, "") or None if not f.empty() else None
    diag.fields("quick-mode name fields %s -> %r", f.to_json(), name)
    return name


def _resolve_tess(opts: Options, tesseract: Optional[Tesseract], result: Result) -> Optional[Tesseract]:
    want = opts.ocr or opts.auto_rotate
    tess = tesseract if tesseract is not None else (Tesseract.find() if want else None)
    if opts.ocr and tess is None:
        result.warnings.append("The text reader is missing, so the PDFs are not searchable.")
    return tess


def convert(
    inputs: Iterable[str | os.PathLike],
    opts: Options,
    progress: Optional[ProgressFn] = None,
    tesseract: Optional[Tesseract] = None,
    cancel=None,
) -> Result:
    """Convert dropped/selected photos: one combined PDF, or one PDF per photo."""
    progress = progress or (lambda done, total, msg: None)
    result = Result()
    images = collect_images(inputs)
    if not images:
        result.warnings.append("No photos were found. The app reads JPG, PNG, HEIC, WebP, TIFF, BMP and GIF.")
        return result
    tess = _resolve_tess(opts, tesseract, result)

    total = len(images)
    with tempfile.TemporaryDirectory(prefix="photo2pdf-") as td:
        pages = _render_pages(images, Path(td), opts, tess, result, progress, 0, total, cancel)
        if not pages or result.stopped:
            # A stopped quick conversion saves nothing: half a document is worse than none.
            return result
        progress(total, total, "Saving…")
        if opts.combine:
            out = combined_output_path([s for s, _ in pages])
            if opts.auto_name and tess and opts.ocr:
                named = _content_name([p for _, p in pages])
                if named:
                    out = unique_path(out.parent / named)
            _merge([p for _, p in pages], out)
            result.outputs.append(out)
        else:
            for src, pdf in pages:
                name = f"{src.stem}.pdf"
                if opts.auto_name and tess and opts.ocr:
                    name = _content_name([pdf]) or name
                out = unique_path(output_dir_for(src.parent) / name)
                shutil.copyfile(pdf, out)
                result.outputs.append(out)

    progress(total, total, "Done")
    return result


def save_documents(
    documents: list[tuple[str, list]],
    out_dir: Path,
    opts: Options,
    progress: Optional[ProgressFn] = None,
    tesseract: Optional[Tesseract] = None,
    cancel=None,
) -> Result:
    """Folder mode: write each (relative file name, ordered Paths or PageSpecs) as one PDF under out_dir.

    The name may include a subfolder ("2025/…pdf"). Page order is kept exactly
    as given (the review screen decides it). Each saved document is recorded
    in the registry so it isn't saved again next time.
    """
    from photo2pdf import registry

    progress = progress or (lambda done, total, msg: None)
    result = Result()
    tess = _resolve_tess(opts, tesseract, result)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = sum(len(imgs) for _, imgs in documents)
    done = 0
    with tempfile.TemporaryDirectory(prefix="photo2pdf-") as td:
        for d, (name, imgs) in enumerate(documents):
            if cancel is not None and cancel.is_set():
                result.stopped = True
                break
            work = Path(td) / f"d{d:04d}"
            work.mkdir()
            pages = _render_pages(imgs, work, opts, tess, result, progress, done, total, cancel)
            done += len(imgs)
            if result.stopped:
                break  # don't write a half-rendered document
            if not pages:
                continue
            out = unique_path(out_dir / name)
            out.parent.mkdir(parents=True, exist_ok=True)
            _merge([p for _, p in pages], out)
            result.outputs.append(out)
            result.saved.append((d, out))
            registry.record([s for s, _ in pages], out)
            from photo2pdf import diag

            diag.fields("saved document %d: %r (%d pages)", d + 1, out.name, len(pages))
    progress(total, total, "Done")
    return result
