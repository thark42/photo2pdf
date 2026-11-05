# Photos to PDF (Windows)

A small offline Windows app that turns phone photos of documents into PDFs,
with searchable text if you want it. It was built for a non-technical user,
it is not part of the LifeOS stack, and it runs nothing on LifeOS hosts.

End-user instructions: [`HOW_TO_USE.txt`](HOW_TO_USE.txt). The installer
ships this file too.

## What it does

| Step | Tool |
|---|---|
| Opens JPEG / PNG / HEIC / WebP / TIFF / BMP / GIF, applies the phone's EXIF rotation, flattens transparency, can convert to greyscale, caps the long edge at 3508 px (A4 at 300 dpi), and sets the DPI so pages come out roughly A4 | Pillow + pillow-heif |
| Fixes sideways or upside-down pages using orientation detection (`--psm 0`) | Tesseract |
| Builds each page as a searchable PDF (the image plus an invisible text layer) | Tesseract `pdf` renderer |
| Builds an image-only page when OCR is off or fails, with no further re-encoding | img2pdf |
| Joins the pages into one PDF | pypdf |

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
- Send To with a very large selection can hit the Windows command-line
  length limit. Dragging the folder instead avoids it.

## Ways in

- Explorer: right-click → **Send to → Photos to PDF**. The installer creates
  the shortcut in `%APPDATA%\Microsoft\Windows\SendTo`.
- Drag photos or a folder onto the desktop shortcut or into the window.
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
   checks that the text comes back.
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

## Third-party licences (bundled in the installer)

Tesseract (Apache-2.0), Pillow (MIT-CMU), pillow-heif (BSD-3, bundles
libheif LGPL-3), img2pdf (LGPL-3), pikepdf (MPL-2.0), pypdf (BSD-3),
tkinterdnd2 (MIT), Python (PSF). [VERIFY: exact licence of each pinned
version before any wider distribution than one friend]
