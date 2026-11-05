# Photos to PDF (Windows)

A small offline Windows app that turns phone photos of documents into PDFs,
with searchable text if you want it. It can also scan a folder of mixed
photos, pick out the documents, and group multi-page ones into a single PDF. It was built for a non-technical user,
it is not part of the LifeOS stack, and it runs nothing on LifeOS hosts.

End-user instructions: [`HOW_TO_USE.txt`](HOW_TO_USE.txt). The installer
ships this file too.

## What it does

| Step | Tool |
|---|---|
| Opens JPEG / PNG / HEIC / WebP / TIFF / BMP / GIF, applies the phone's EXIF rotation, flattens transparency, can convert to greyscale with lighting flattened (background normalisation), caps the long edge at 3508 px (A4 at 300 dpi), and sets the DPI so pages come out roughly A4 | Pillow + pillow-heif |
| Fixes sideways or upside-down pages using orientation detection (`--psm 0`) | Tesseract |
| Builds each page as a searchable PDF (the image plus an invisible text layer) | Tesseract `pdf` renderer |
| Builds an image-only page when OCR is off or fails, with no further re-encoding | img2pdf |
| Joins the pages into one PDF | pypdf |

## Folder mode (find + group documents)

`photo2pdf/detect.py` handles detection and grouping, `photo2pdf/review.py` the review screen.

- **Detection:** each photo is greyscaled, lighting-flattened, scaled to a
  1600 px long edge, and OCR'd with `tesseract --psm 1` (layout analysis
  plus orientation detection, so sideways pages still read). It counts as a
  document if it has at least `min_words` (default 12) words with
  confidence >= 60, at least 3 characters, and mostly letters or digits.
  Several Tesseract processes run in parallel, one thread each. Word counts
  are cached per file (path, size, mtime) in
  `%LOCALAPPDATA%\Photo2PDF\scan-cache.json`.
- **Grouping:** capture time comes from EXIF `DateTimeOriginal`, then a
  timestamp in the filename (IMG_/PXL_/Screenshot_/WhatsApp patterns),
  then file mtime. Document photos in time order are split into separate
  documents when the gap exceeds `gap_seconds` (default 180) or a
  non-document photo sits between them.
- **Review:** the user can reorder pages, split, join, drop a page or a
  group, or promote an excluded photo. Excluded photos are listed with the
  highest word counts first, so the most likely misses are at the top. Only
  the top 60 are shown.
- **Output:** `<folder>\PDFs\Document NN - YYYY-MM-DD HH.MM (N pages).pdf`.
  Source photos are never moved or deleted.
- Advanced settings are in `settings.json` under `"scan"`: `min_words`,
  `gap_seconds`, `recursive`. They aren't in the GUI.

**Why there's no LLM or vision model:** whether a photo is a document comes
down to whether it holds a lot of legible text, and the bundled OCR engine
measures that directly. A local model (for example a CLIP-style zero-shot
classifier) would add roughly 100-400 MB plus an inference runtime
[VERIFY: size of the specific model if one is ever added], for a question
OCR already answers. Grouping is decided by capture time, which a model
wouldn't improve on. A model would be worth revisiting for semantic
grouping (say, telling apart two different letters photographed a minute
apart) or for naming files from their content.

**Accuracy is not measured.** The thresholds were checked against 17 local
images: generated text pages (upright, 90° and 180°), one real unevenly lit
page photo, and 13 natural photos. All 17 were classified correctly, and
flattening the lighting raised the real photo's count from 8 words to 36.
That is a smoke test, not a benchmark. Expect misses on very sparse
documents (a receipt with a few large numbers, an ID card) and false hits on
text-heavy non-documents (screenshots, posters). The review screen is how
those get fixed.

Behaviour:
- Pages are ordered by natural filename sort, never by selection order,
  because Windows doesn't pass Send-To or drag-and-drop arguments in a
  reliable order.
- Output goes next to the photos. If that folder isn't writable (for example
  a phone or a read-only share), it goes to `Documents\Photos to PDF`.
- Existing files are never overwritten; a second save gets ` (2)` added to
  its name.
- A page that fails produces a warning. It doesn't abort the batch.
- Settings live in `%APPDATA%\Photo2PDF\settings.json` and the log in
  `log.txt` next to it.

Known limits:
- No perspective or dewarp correction. A page photographed at an angle
  stays angled.
- Every photo is re-encoded once as JPEG at quality 85, after rotation and
  downscaling. Phone photos are already lossy JPEG or HEIC, so this costs
  a little quality but cuts file size a lot.
- OCR language is English only (`eng` plus `osd` bundled). To add another
  language, keep its `.traineddata` in the "Stage Tesseract" workflow step
  and set `"language": "eng+deu"` in `settings.json`. The GUI has no
  language picker.
- The exe is unsigned, so SmartScreen warns on first run.
- Review-screen thumbnails show photos as stored. A sideways page looks
  sideways in the review but is saved upright.
- Send To with a very large selection can hit the Windows command-line
  length limit. Dragging the folder instead avoids it.

## Ways in

- Explorer: right-click → **Send to → Photos to PDF**. The installer creates
  the shortcut in `%APPDATA%\Microsoft\Windows\SendTo`.
- Drag photos onto the desktop shortcut or into the window to get one PDF,
  or one per photo.
- Drag a single folder, Send-To a folder, or click "Find the documents in a
  folder of photos…" for folder mode.
- Start menu → click the box → file picker.

## Build

There's no local Windows build. Building happens in CI:
[`.github/workflows/photo2pdf-windows.yml`](../../.github/workflows/photo2pdf-windows.yml).

1. Run pytest on Linux, using the system tesseract.
2. Install Tesseract on the Windows runner (`choco install tesseract`). Keep
   only `eng` and `osd` plus `configs/`, then run pytest again against that
   staged copy.
3. Build a PyInstaller one-folder app with Tesseract bundled in `_internal\tesseract`.
4. Run `Photo2PDF.exe --selftest`, which OCRs a generated sideways page and
   checks that the text comes back, then scans a generated folder (two
   documents separated by a non-document image) and checks the grouping.
5. Build the Inno Setup installer: per-user, no admin, with Start menu,
   desktop and Send-To shortcuts.
6. Install silently, check the Send-To shortcut exists, then run
   `--selftest` on the installed copy.
7. Upload two artifacts: `PhotosToPDF-Setup-<ver>` and `PhotosToPDF-portable-<ver>`.

Artifacts need a GitHub login to download and expire after the repo's
retention period (90 days by default). Download one and send her the
installer `.exe`.

## Develop / test on Linux

```bash
cd tools/photo2pdf
pip install -r requirements-build.txt     # plus: apt install tesseract-ocr tesseract-ocr-osd python3-tk
pytest -q tests
python -m photo2pdf                       # GUI
python -m photo2pdf --selftest /tmp/st.pdf
```

Bump `__version__` in `photo2pdf/__init__.py` to change the installer version.

## Offline guarantee

The app makes no network calls. `tests/test_offline.py` fails if any module
in `photo2pdf/` imports socket, urllib, http, requests, and similar. The
only network use is at build time in CI (pip, choco, and possibly the
tessdata download).

## Third-party licences (bundled in the installer)

Tesseract (Apache-2.0), Pillow (MIT-CMU), pillow-heif (BSD-3, bundles
libheif LGPL-3), img2pdf (LGPL-3), pikepdf (MPL-2.0), pypdf (BSD-3),
tkinterdnd2 (MIT), Python (PSF). [VERIFY: exact licence of each pinned
version before any wider distribution than one friend]
