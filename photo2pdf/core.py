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

import os
import re
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

ProgressFn = Callable[[int, int, str], None]


@dataclass
class Options:
    combine: bool = True          # one PDF for all photos vs one per photo
    ocr: bool = True              # searchable text layer
    grayscale: bool = False       # smaller files
    auto_rotate: bool = True      # Tesseract orientation detection (needs OCR engine)
    language: str = "eng"         # Tesseract language code(s), e.g. "eng+deu"
    max_long_edge: int = 3508     # px; 3508 = A4 long edge at 300 dpi
    jpeg_quality: int = 85


@dataclass
class Result:
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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

    def _run(self, args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
        cmd = [str(self.exe), *args]
        if self.tessdata:
            cmd += ["--tessdata-dir", str(self.tessdata)]
        kwargs = {}
        if os.name == "nt":
            # Stop a console window flashing up for every page.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        env = dict(os.environ, OMP_THREAD_LIMIT=os.environ.get("OMP_THREAD_LIMIT", "4"))
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env, **kwargs,
        )

    def languages(self) -> set[str]:
        try:
            r = self._run(["--list-langs"], cwd=Path.cwd(), timeout=30)
        except (OSError, subprocess.SubprocessError):
            return set()
        lines = (r.stdout or "").splitlines()
        return {ln.strip() for ln in lines[1:] if ln.strip()}

    def detect_rotation(self, image: Path, min_confidence: float = 2.0) -> int:
        """Degrees Tesseract says the page must be turned clockwise (0/90/180/270).

        Returns 0 when detection fails or is unsure (blank or sparse pages).
        The confidence threshold is a heuristic, not a Tesseract-documented value.
        """
        try:
            r = self._run([image.name, "stdout", "--psm", "0"], cwd=image.parent, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return 0
        out = (r.stdout or "") + (r.stderr or "")
        rot = re.search(r"Rotate:\s*(\d+)", out)
        conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
        if r.returncode != 0 or not rot or not conf:
            return 0
        if float(conf.group(1)) < min_confidence:
            return 0
        return int(rot.group(1)) % 360

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


def prepare_page(src: Path, dst: Path, opts: Options, extra_rotation: int = 0) -> Path:
    """Write a normalised JPEG for one photo. Returns dst."""
    from PIL import Image, ImageOps

    with _open_image(src) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        if extra_rotation:
            # Tesseract reports clockwise degrees; PIL rotates counter-clockwise.
            im = im.rotate(-extra_rotation, expand=True)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im = im.convert("L" if opts.grayscale else "RGB")
        long_edge = max(im.size)
        if long_edge > opts.max_long_edge:
            scale = opts.max_long_edge / long_edge
            im = im.resize(
                (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                Image.LANCZOS,
            )
        w_in, h_in = (A4_W_IN, A4_H_IN) if im.height >= im.width else (A4_H_IN, A4_W_IN)
        dpi = max(im.width / w_in, im.height / h_in, 1.0)
        im.save(dst, "JPEG", quality=opts.jpeg_quality, dpi=(dpi, dpi), optimize=True)
    return dst


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

def convert(
    inputs: Iterable[str | os.PathLike],
    opts: Options,
    progress: Optional[ProgressFn] = None,
    tesseract: Optional[Tesseract] = None,
) -> Result:
    progress = progress or (lambda done, total, msg: None)
    result = Result()
    images = collect_images(inputs)
    if not images:
        result.warnings.append("No photos found. Supported: " + ", ".join(sorted(IMAGE_EXTS)))
        return result

    tess = tesseract if tesseract is not None else (Tesseract.find() if opts.ocr else None)
    if opts.ocr and tess is None:
        result.warnings.append("Text recognition engine not found; PDFs are not searchable.")

    total = len(images)
    with tempfile.TemporaryDirectory(prefix="photo2pdf-") as td:
        work = Path(td)
        page_pdfs: list[tuple[Path, Path]] = []  # (source image, page pdf)
        for i, src in enumerate(images):
            progress(i, total, f"Page {i + 1} of {total}: {src.name}")
            stem = f"p{i:04d}"  # ASCII temp names: Tesseract is fragile with non-ASCII paths
            try:
                jpg = prepare_page(src, work / f"{stem}.jpg", opts)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                result.warnings.append(f"Skipped {src.name}: could not read image ({exc})")
                continue

            if tess and opts.ocr:
                if opts.auto_rotate:
                    rot = tess.detect_rotation(jpg)
                    if rot:
                        prepare_page(src, jpg, opts, extra_rotation=rot)
                try:
                    pdf = tess.render_pdf(jpg, opts.language)
                except Exception as exc:  # noqa: BLE001
                    result.warnings.append(f"{src.name}: text recognition failed ({exc}); saved without text")
                    pdf = _image_only_pdf(jpg)
            else:
                pdf = _image_only_pdf(jpg)
            page_pdfs.append((src, pdf))

        if not page_pdfs:
            return result

        progress(total, total, "Saving…")
        if opts.combine:
            out = combined_output_path([s for s, _ in page_pdfs])
            _merge([p for _, p in page_pdfs], out)
            result.outputs.append(out)
        else:
            for src, pdf in page_pdfs:
                out = unique_path(output_dir_for(src.parent) / f"{src.stem}.pdf")
                shutil.copyfile(pdf, out)
                result.outputs.append(out)

    progress(total, total, "Done")
    return result
