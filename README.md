# Photos to PDF (Windows)

A small offline Windows app that turns phone photos of documents into
searchable PDFs. It is built for a non-technical user with an existing
archive of photographed tax and government letters, in Dutch and English.
It makes no network connections.

**Download:** the installer is attached to each
[release](../../releases/latest). Licence: MIT ([`LICENSE`](LICENSE)).

End-user instructions: [`HOW_TO_USE.txt`](HOW_TO_USE.txt). The installer
ships this file too.

## Two modes

- **Quick mode.** Selected photos become one combined PDF, or one PDF per
  photo. The combined PDF is named from its content when that is clear.
- **Folder mode** ("Organize a folder of photos", the main action). Point
  the app at one or more folders of mixed photos; subfolders are included
  by default, and hidden/system folders are skipped. It:
  1. finds which photos are documents
  2. straightens them and turns them upright
  3. groups multi-page letters
  4. suggests a name for each PDF from the letter itself
  5. shows a review screen, then saves to `<folder>\PDFs\<year>\…`
     (or per sender, sender then year, or flat)
  6. remembers what was saved, so running it again on the same folder
     only offers new documents

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
- **Review screen** (`review.py`):
  - **Tabs:** "New documents", "Already saved", "Not documents". Each is
    paged (12 documents or 36 photos per page), so a large archive stays
    responsive.
  - **Per document:**
    - a "Save this document" tick-box, so a document can be skipped
    - an editable name, and the target folder shown next to it
    - "Join with document before" and "Not a document"
  - **Per page:**
    - visible buttons to turn left, turn right, start a new document
      here, or remove the page
    - right-click for the full menu
    - click for a large preview of the processed page, exactly as it will
      be saved
  - **Undo** (button or Ctrl+Z) covers every edit since the last save.
  - **Action bar:** packed first, so Save and Close stay visible on a
    1366×768 screen. The window is sized to fit the screen.
  - **Before saving:** if the save folder isn't writable, it offers
    `Documents\Photos to PDF` instead.
  - **Closing safely:** closing with unsaved documents or edits asks
    first. So does closing the main window during a scan or save.
- **Saved-documents record** (`registry.py`):
  `%LOCALAPPDATA%\Photo2PDF\saved.json` maps each photo (path, size,
  mtime) to the PDF it went into.
  - Documents whose photos are all recorded, and whose PDF still exists,
    start unticked under "Already saved".
  - Deleting a PDF makes its photos new again.
  - A stopped save never writes a half document; Save afterwards only
    writes what's left.
- **Long runs:** the window shows an estimate of the time remaining and
  keeps Windows from sleeping (`SetThreadExecutionState`).
- **Settings dialog:** how PDFs are arranged, whether to include
  subfolders, one PDF vs one per photo (quick mode), searchable,
  straighten, and black and white. "Restore recommended" resets them.
  `min_words` and `gap_seconds` stay in `settings.json` under `"scan"`.

## What else it recognises (`features.py`, `classify.py`)

Each photo is measured once:
- OCR word count and confidence, plus text height relative to the image
- how many lines of text have 5 or more words
- whether a sheet outline was found, and its proportions
- ink density
- sharpness (variance of the Laplacian)
- QR code and barcode presence (OpenCV `QRCodeDetector` and
  `barcode.BarcodeDetector`)
- whether it looks like a screenshot: file name, PNG without camera data,
  screen proportions
- an image fingerprint (a 256-bit dHash of the upright page) and a text
  fingerprint (a 32-value MinHash of the word set, which can't be turned
  back into the text)

`classify.py` then applies rules in order:

| Type | Recognised by | Folder | Name |
|---|---|---|---|
| Screenshot, payment or booking confirmation | screenshot + payment/booking words | `Confirmations/<year>` | `Payment confirmation - ING - 2025-02-01 - EUR 25.00` |
| Other screenshots | screenshot | not saved ("Screenshot") | |
| Card | ID-1 outline (see Cards) | `Cards` | |
| Business card | card outline + phone/e-mail/web, no card type | `Cards` | `Business card - <e-mail or web domain>` (never the person's name; personal mail domains are left out) |
| Receipt | outline ≥ 2:1 + till words, or ≥ 5 till signals | `Receipts/<year>` | `Receipt - Albert Heijn - 2024-11-02 - EUR 12.16` (page 80 mm wide) |
| Ticket / boarding pass / parcel label | QR code or barcode + ticket words | `Tickets/<year>` | `Boarding pass - KLM - 2026-07-14` (barcode contents never used) |
| Envelope | postal markings / postcode, few words, no text lines | not saved ("Envelope") | |
| Product type plate | no sheet + ≥ 2 of model/type, serial, V/Hz/W/IP, CE | `Product labels` | `Product label - Bosch - WAN28K40-01` (serial numbers never used) |
| Sign, poster, menu | no sheet + large text (median height > 3% of image) + few long lines | not saved ("Sign or poster") | |
| Letter / form | ≥ 12 confident words | `<year>` / sender | as before |
| Blurred sheet | sheet found but sharpness < 60 | kept as a page, flagged "hard to read" | |
| Handwritten note / whiteboard | sheet found, ink 1–20%, little printed text | `Notes/<year>` | `Note - <photo date>` (handwriting isn't readable by Tesseract, so these aren't searchable) |

**Duplicates.** Document photos of the same thing are grouped. Two photos
are duplicates when:
- their image fingerprints differ by 40 bits or fewer out of 256, and
- where both have text, their text fingerprints match at least 60%

The text check keeps two pages of the same form apart. The copy with more
readable words, then the sharper one, is kept. The rest go under "Not
documents" as "Duplicate of …" and can be added back. Nothing is deleted.

**Hard to read.** A document with sharpness below 60, or mean OCR
confidence below 55, gets a warning in the review screen: "retake if you
still have the paper".

**Not implemented:** meter readings (dials and displays aren't reliably
readable by OCR).

**Calibration.** All thresholds were checked on synthetic images only. The
images covered:
- a letter, a re-shot copy of it and a blurred copy
- a different letter
- a receipt
- a payment screenshot, an Instagram screenshot and a QR boarding pass
- a handwritten note
- a menu board
- an envelope
- a type plate
- a business card
- a natural photo

All 14 came out right. Each photo's measurements are logged (`features
<file>: …`) so they can be tuned against real archives.

## Cards: ID, passport, driving licence, insurance, bank, loyalty

Card handling is in `geometry.py`, `detect.py` and `naming.py`.

- **Recognising a card.** A card can be recognised two ways:
  - **By its shape.** The straightened outline has ISO/IEC 7810 ID-1
    proportions (85.60 × 53.98 mm, ±7%), and at least 2 readable words
    are found.
  - **By its text.** If the outline wasn't found, a card keyword or a
    passport/ID machine-readable zone, plus fewer than 40 words, also
    counts. The 40-word limit stops a short insurer letter from being
    taken for a card.
- **Reading a small card.** Cards with little text are turned upright
  before being read again. Upside-down and sideways cards otherwise read
  as noise. Ties in the orientation vote are broken by Tesseract's
  orientation estimate first, then by a landscape preference.
- **Front and back.** A card-shaped photo with little or no text, taken
  within 120 s directly after a card that was recognised on its own
  evidence, becomes that card's back.
- **When a new card starts.** A card has at most 2 sides. A new card
  starts when:
  - a recognised card type differs from the card so far
  - a card follows a letter, or a letter follows a card

  So a wallet photographed card after card comes out as separate cards.
- **Output.** Card PDFs have real-size pages (85.6 × 54 mm), but only
  when the card's outline was found. They are filed in `Cards/` in every
  arrangement except "all in one folder".
- **Name format.** `<Card type> - <Issuer> - valid until <YYYY-MM-DD>.pdf`,
  for example:
  - `Health insurance card - Zilveren Kruis - valid until 2026-12-31.pdf`
  - `Passport - Netherlands - valid until 2031-05-01.pdf`
  - `Bank card - ING - valid until 2029-08-31.pdf`
  - `Membership card - Albert Heijn.pdf`

  Without a recognised type, the name falls back to
  `Card NN - <photo date> (front and back).pdf`.
- **Card types:**
  - passport
  - ID card
  - driving licence
  - residence permit
  - EHIC/GHIC
  - health insurance card
  - OV-chipkaart
  - Museumkaart
  - library card
  - bank card
  - membership / loyalty card
- **Issuers:**
  - Dutch health insurers
  - Dutch banks
  - common Dutch shops and memberships
  - for government IDs, the issuing country, taken from the
    machine-readable zone or from "Koninkrijk der Nederlanden"
- **Short brand names.** Names that are also ordinary words or first
  names (ING, NS, PLUS, AH, CZ, …) are matched case-sensitively.
- **Expiry date.** Taken from:
  - a verified machine-readable zone: the date is only accepted when its
    ICAO 9303 check digit matches, which rejects OCR misreads
  - otherwise a labelled date: "geldig tot", "vervaldatum", "valid until",
    "expiry", "4b." on driving licences
  - `MM/YY` on bank cards, read as the end of that month

**Privacy.** The holder's name, document number, BSN and card number are
read by OCR but never put in file names or logged. The card type, issuer
and expiry date are logged only as `FIELDS` lines, which the problem
report can leave out.

**Accuracy is not measured on real cards.** The tests and the self-test
use generated card images. They cover:
- an insurance card front and blank back
- a driving licence photographed upside down
- a passport with a valid machine-readable zone
- a bank card
- a loyalty card
- the official ICAO specimen machine-readable zone

Real cards add holograms, glare, embossed digits, busy backgrounds, and
the OCR-B font in the machine-readable zone, which Tesseract's default
models are not trained on. Expect more misses than with letters.

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
- **Cards:** see the Cards section above.
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
  doesn't pass Send-To or drag-and-drop arguments in a reliable order. It
  has a Stop button. A stopped quick conversion saves nothing, because
  half a document is worse than none.
- Both modes name PDFs from their content when that is clear, including
  one-PDF-per-photo. Otherwise they keep the photo name.
- End-of-run messages are plain language ("1 photo could not be opened
  and was left out"), with a "Show details" list. A "Report this
  problem" button appears only for unexpected errors.
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

- **Main window:** two buttons, "Organize a folder of photos" (primary)
  and "Make one PDF from a few photos". Settings and Help are at the
  bottom and in the menus.
- **Drag and drop:** onto the window or the desktop shortcut. Anything that
  includes a folder (one or several) opens folder mode. Only plain photos
  use quick mode.
- **Explorer:** select photos or folders, right-click → **Send to →
  Photos to PDF**. The same routing applies.
- **One scan window at a time:** starting another action while one is open
  brings it to the front.
- **Display:** the app declares DPI awareness so Windows doesn't
  bitmap-stretch it at 125–150% scaling, and thumbnails and icons scale
  with the DPI. This hasn't been checked on a real scaled display yet.

## Build

Builds run in CI only: [`.github/workflows/build.yml`](.github/workflows/build.yml).

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
8. On a pushed tag `v<ver>` (must equal `photo2pdf.__version__`), publish a
   GitHub Release with the installer and a portable zip attached, using
   [`RELEASE_NOTES.md`](RELEASE_NOTES.md) as the text.

## Develop / test on Linux

```bash
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

The build runs [`packaging/collect_licenses.py`](packaging/collect_licenses.py),
which copies the licence files of every bundled component into a `licenses`
folder next to `Photo2PDF.exe`, with an `INDEX.txt` listing name, version and
declared licence. It takes them from the installed packages, so they match
the pinned versions, and fails the build if a component has none. Components:
Python, Tcl/Tk, Tesseract and its `tessdata_fast` language data, OpenCV,
NumPy, Pillow, pillow-heif (with libheif), img2pdf, pikepdf, pypdf, lxml,
tkinterdnd2.
