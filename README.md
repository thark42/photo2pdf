# Photos to PDF (Windows)

A small offline Windows app that turns phone photos of documents into
searchable PDFs. It is built for a non-technical user with an existing
archive of photographed tax and government letters, in Dutch and English.
It is not part of the LifeOS stack and runs nothing on LifeOS hosts.

End-user instructions: [`HOW_TO_USE.txt`](HOW_TO_USE.txt). The installer
ships this file too.

## Two modes

- **Quick mode.** Selected photos become one combined PDF, or one PDF per
  photo. The combined PDF is named from its content when that is clear.
- **Folder mode.** Point the app at a folder of mixed photos, optionally
  including subfolders. It:
  1. finds which photos are documents
  2. straightens them and turns them upright
  3. groups multi-page letters
  4. suggests a name for each PDF from the letter itself
  5. shows a review screen, then saves to `<folder>\PDFs`

## Page pipeline (both modes)

| Step | How | Module |
|---|---|---|
| Open the photo upright per its EXIF tag; supports HEIC | Pillow + pillow-heif | `core.load_page` |
| Perspective correction: find the sheet's 4 corners, recover its true aspect ratio, warp it to a rectangle | OpenCV | `geometry` |
| Deskew any remaining tilt up to ±10° | OpenCV | `geometry.estimate_skew` |
| Orientation 0/90/180/270 | Tesseract | `core.find_rotation` |
| Optional black and white with lighting flattened | Pillow | `core.flatten_lighting` |
| Searchable PDF page (image plus invisible text), Dutch + English | Tesseract `-l nld+eng pdf` | `core` |
| Image-only fallback when OCR fails | img2pdf | `core` |
| Merge pages | pypdf | `core` |

### Straightening (`geometry.py`)

- **Finding the page:** two detectors run on a 1000 px copy: edges (Canny,
  then contours, then a 4-point polygon) and brightness (Otsu threshold on
  the bright paper region). The candidate is accepted only if:
  - it is convex
  - it covers 8–97% of the frame
  - its corner angles are within 45–135°
  - its inside is clearly brighter than its surroundings

  When no candidate passes (a tight crop, an existing scan, a non-document)
  the photo is left unwarped.
- **Aspect ratio:** recovered with the Zhang & He (2007) whiteboard-scanning
  method, which assumes a camera centred on the image with square pixels.
  The result snaps to A4 or US Letter when within 6%. Without this step, a
  page shot at an angle comes out squashed (1.29 instead of 1.41 on the
  test images).
- **Deskew:** the projection profile of the text is scored over ±10° and the
  sharpest angle wins. It is applied only when the gain over 0° is ≥ 10% and
  the ink density looks like text.
- **Not handled:**
  - curved pages (open books)
  - folds or crumples
  - several pages in one photo
  - a page cut off by the frame edge, which can't be detected as a
    4-corner shape and is left as is

### Orientation (`core.find_rotation`)

1. Run Tesseract OSD on a cleaned 1600 px copy. Accept its answer if its
   confidence is ≥ 3.
2. Otherwise OCR the copy at all four rotations and keep the one with the
   most confident words, if it clearly wins (≥ 5 words and ≥ 1.3× the
   runner-up).
3. Otherwise use a weak OSD answer, or else 0°.

The thresholds are heuristics, not values documented by Tesseract. In the
review screen every page has Rotate left / right / upside down, so a wrong
call can be fixed before saving.

## Folder mode details (`detect.py`, `review.py`)

- **Is it a document?** The photo is straightened, lighting-flattened,
  greyscaled at 1600 px, then OCR'd with `tesseract -l nld+eng --psm 1`. It
  is a document if it has at least 12 words with confidence ≥ 60, at least
  3 characters, and mostly letters or digits. Several Tesseract processes
  run in parallel, one thread each.
- **Grouping:**
  - Photos are put in time order, using EXIF `DateTimeOriginal`, then a
    timestamp in the file name (IMG_/PXL_/Screenshot_/WhatsApp patterns),
    then the file's modified time.
  - A new document starts when:
    - the gap between photos exceeds 180 s
    - a non-document photo sits in between
    - the subfolder changes
    - a page shows a different letterhead sender or document date than the
      pages before it
- **Cache:** `%LOCALAPPDATA%\Photo2PDF\scan-cache.json` holds word count,
  rotation and the extracted name fields per file (keyed by path, size,
  mtime, language and the straighten setting). It never stores the full
  OCR text. Delete the file to clear it.
- **Review screen:**
  - pages are shown straightened and upright
  - each page can be rotated, moved, split into a new document, or removed
  - documents can be joined
  - a missed photo can be promoted to a document
  - each PDF's file name is editable
- **Advanced settings:** in `%APPDATA%\Photo2PDF\settings.json` under
  `"scan"`: `min_words`, `gap_seconds`, `recursive`. The GUI only has the
  subfolders checkbox.

## Naming from content (`naming.py`)

Rule-based, Dutch + English. Format:

    <YYYY-MM-DD> <Sender> - <Document type> [<tax year>].pdf
    2025-03-14 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf

- **Date:**
  1. A date right after a label (`Datum`, `Dagtekening`, `Verzenddatum`,
     `Date`, `Date of issue`).
  2. Otherwise the first date in the top 40% of the text.

  Formats: `14 maart 2025`, `14 mrt. 2025`, `March 14, 2025`, `2025-03-14`,
  and numeric dates read day-first (`14-03-2025`). Years outside 1990 to
  next year are ignored.
- **Sender:**
  - Dutch: Belastingdienst, Toeslagen, Douane, Gemeente X, Waterschap X,
    Hoogheemraadschap X, UWV, SVB, DUO, CJIB, CAK, RDW, IND, KVK, ABP,
    Pensioenfonds X
  - English: HMRC, IRS
- **Document type:** for example voorlopige/definitieve aanslag
  inkomstenbelasting, aanslag gemeentelijke belastingen, WOZ-beschikking,
  motorrijtuigenbelasting, waterschapsbelasting, (definitieve berekening)
  zorg-/huur-/kinderopvangtoeslag, uitnodiging aangifte, jaaropgaaf,
  aanmaning, betalingsherinnering, dwangbevel, kwijtschelding, uitspraak op
  bezwaar, beschikking, betalingsregeling, factuur, Self Assessment, P60.
  The tax year is appended when found.
- **Where it searches:** the top third of the text first, because generic
  footer words ("bezwaar maken", "beschikking") appear on almost every
  letter.
- **When it names:** only when at least two of date, sender and type are
  found. Otherwise it falls back to
  `Document NN - <photo date> (N pages).pdf`.
- **Adding rules:** extra rules go in `%APPDATA%\Photo2PDF\naming.json`,
  with no rebuild needed. They are tried before the built-in ones:

  ```json
  {"senders": [["zilveren\\s+kruis", "Zilveren Kruis"]],
   "doc_types": [["\\bpolis\\b", "Polis"]]}
  ```

**Why there's no LLM or vision model:** everything here is either geometry
(OpenCV), OCR (Tesseract), or pattern matching over the OCR text. The
targets are formulaic government letters, and the user asked for names only
"where easy and clear", which rules do predictably and explain easily. A
small local LLM (around 1–2 GB plus a runtime) [VERIFY: size and quality of
a specific model if one is evaluated] could name arbitrary documents better,
but it would roughly triple the install size and make names less
predictable. It's worth evaluating only if the rules miss too often on her
real archive.

## Accuracy: what has and hasn't been measured

Everything below was run on synthetic or sample images. Nothing has been
run on a real archive yet.

- **Perspective:** 7 simulated camera poses. They project an A4 page
  through a pinhole camera with tilt up to 50° and any roll, onto a
  textured background with a lighting gradient and noise. In all 7 the page
  was found, corners came within 15 px in a 2400×1800 frame (4–7 px
  typical), and the aspect ratio was within 0.03 of A4.
- **Deskew:** within 0.3° for tilts of −8° to +6°. No false rotation on a
  noise image or an already-straight page.
- **Orientation:** 0/90/180 on generated pages. An angled, upside-down
  photo of a fictional Dutch tax letter was straightened, turned upright
  and named correctly end to end.
- **Detection:** 17 images (generated pages, one real unevenly lit page
  photo, 13 natural photos), all classified correctly.
- **Naming:** unit tests on fictional Dutch and English letter texts.

Not measured:
- real phone photos: motion blur, glare, hands in frame, cut-off pages
- real Belastingdienst layouts
- photos of screens
- handwritten documents

Expect misses. The review screen is where they get fixed.

## Diagnostics and problem reports (`diag.py`)

- **Rolling log:** `%LOCALAPPDATA%\Photo2PDF\logs\photo2pdf.log`, 1 MB per
  file, 5 old files kept (about 6 MB maximum). Native crashes from OpenCV,
  Tk and similar go to `crash.log` via `faulthandler`.
- **What each session records:**
  - app, OS, Python, library and Tesseract versions, bundled languages,
    settings
  - per page: file name, size, timings, whether the page was found and the
    skew, the orientation decision and how it was reached (OSD confidence
    or the word-vote counts), word count, document yes/no, OCR fallbacks
  - folder scan totals and the shape of the plan
  - **every review-screen correction** (rotate, move, split, join, remove,
    add as document, rename), with what the app had decided. This is the
    signal for tuning thresholds and naming rules.
  - all errors with tracebacks, including uncaught exceptions in worker
    threads and Tk callbacks
- **Privacy:**
  - the photos, PDFs and OCR text are never logged
  - the home folder becomes `~` and the Windows user name `<user>`
  - lines derived from document content (recognised date, sender and type,
    and names the user types) are tagged `FIELDS`
- **Report:** Help → "Report a problem or idea…", or offered automatically
  after warnings, errors or a crash. It writes
  `Desktop\Photos to PDF report <date>.zip` containing:
  - `message.txt` (her description)
  - `system.json`
  - `logs/*`
  - `settings.json`
  - `naming.json`

  A checkbox (on by default) controls whether `FIELDS` lines are included.
  Unticking it replaces them with `FIELDS [left out]` and leaves out
  `naming.json`. Nothing is sent anywhere; she emails the zip.
- **Reading a report:** grep for `review ` to see corrections, `rotation=`
  for orientation decisions, `straighten` for perspective, and `ERROR` or
  `CRITICAL` for failures.

## Behaviour

- Quick mode orders pages by natural filename sort, because Windows
  doesn't pass Send-To or drag-and-drop arguments in a reliable order.
- Existing files are never overwritten; a second save gets ` (2)` added to
  its name. Source photos are never moved or deleted.
- If the photo folder isn't writable, quick mode saves to
  `Documents\Photos to PDF`.
- A page that fails produces a warning. It doesn't abort the batch.
- Each photo is re-encoded once as JPEG (quality 85, long edge ≤ 3508 px)
  after straightening and rotating.
- Settings are in `%APPDATA%\Photo2PDF\settings.json`. Logs are described
  under Diagnostics.
- The exe is unsigned, so SmartScreen warns on first run.

## Ways in

- Explorer: right-click photos → **Send to → Photos to PDF** for quick mode.
  Right-click a folder → Send to opens folder mode.
- Drag photos onto the desktop shortcut or into the window for quick mode.
  Drag a single folder for folder mode.
- In the window: click the drop area, or click "Find the documents in a
  folder of photos…".

## Build

Builds run in CI only:
[`.github/workflows/photo2pdf-windows.yml`](../../.github/workflows/photo2pdf-windows.yml).

1. Run pytest on Linux, using the system tesseract with eng, nld and osd.
2. Install Tesseract on the Windows runner (`choco install tesseract`). Keep
   only `eng`, `nld` and `osd` (downloading any missing ones from
   `tesseract-ocr/tessdata_fast`) plus `configs/`, then run pytest again
   against that copy.
3. Build a PyInstaller one-folder app with Tesseract in `_internal\tesseract`.
4. Run `Photo2PDF.exe --selftest`. It covers:
   - OCR of a sideways page
   - folder grouping
   - building a problem-report zip
   - an angled, upside-down Dutch letter, which must be straightened,
     turned upright and named correctly
5. Build the Inno Setup installer: per-user, no admin, with Start menu,
   desktop and Send-To shortcuts.
6. Install silently, check the Send-To shortcut exists, then run
   `--selftest` on the installed copy.
7. Upload two artifacts: `PhotosToPDF-Setup-<ver>` and `PhotosToPDF-portable-<ver>`.

Artifacts need a GitHub login to download and expire after the repo's
retention period (90 days by default).

## Develop / test on Linux

```bash
cd tools/photo2pdf
pip install -r requirements-build.txt
# apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-nld tesseract-ocr-osd python3-tk
pytest -q tests
python -m photo2pdf                       # GUI
python -m photo2pdf --selftest /tmp/st.pdf
```

`tests/synth.py` generates the synthetic phone photos: a camera-projected
page over a textured background, and a fictional tax letter.

## Offline guarantee

The app makes no network calls. `tests/test_offline.py` fails if any module
in `photo2pdf/` imports socket, urllib, http, requests, and similar. The
only network use is at build time in CI (pip, choco, and the tessdata
download).

## Third-party licences (bundled in the installer)

Tesseract and its tessdata (Apache-2.0), OpenCV (Apache-2.0), NumPy
(BSD-3), Pillow (MIT-CMU), pillow-heif (BSD-3, bundles libheif LGPL-3),
img2pdf (LGPL-3), pikepdf (MPL-2.0), pypdf (BSD-3), tkinterdnd2 (MIT),
Python (PSF). [VERIFY: exact licence of each pinned version, and whether
the opencv-python-headless Windows wheel bundles any LGPL components,
before any wider distribution than one friend]
