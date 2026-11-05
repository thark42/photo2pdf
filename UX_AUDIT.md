# UX / usability audit: Photos to PDF v0.4.0

Date: 2026-11-05. Audited before the first test with the real user.

**Method.** I walked through every screen and flow as the target user would:
a non-technical person on a Windows laptop, clearing an archive of
photographed Dutch and English government letters. I checked each step
against the code (`app.py`, `review.py`, `core.py`, `detect.py`), rendered
the screens under Xvfb, and timed the review screen with a synthetic plan
of 20 and 200 documents. I applied standard heuristics: visibility of
status, user control and undo, error prevention, recognition over recall,
plain-language errors, and platform conventions.

**Limits.**
- The screens were rendered by Linux Tk, not Windows. The Windows "vista"
  ttk theme, fonts and DPI scaling will look different.
- No real user and no real archive were involved.
- Anything marked *(verify on Windows)* is inferred, not observed.

Severity:
- **B** = blocker for the test (likely to derail it or lose work)
- **H** = high (will confuse or cost real time)
- **M** = medium
- **L** = low / polish

---

## Blockers: fix before she touches it

### B1. The review window's Save button can be off-screen on a typical laptop
- **Evidence:** the window has a fixed `geometry("980x720")`
  (`review.py:41`). I rendered it on a 1366×697 screen, which is 768 minus
  the taskbar and title bar. The bottom row with "Save N PDFs" and "Close"
  was cut in half.
- **Why it's worse on Windows:** a 1366×768 or 1920×1080 laptop at 150%
  scaling is common, and that leaves less room still.
- **Fix:** size the window to the screen (`winfo_screenheight()` minus a
  margin), or open it maximised. Put the Save and Close bar above the
  scroll area, or pin it so it is always visible.

### B2. Closing the review window throws away all review work without asking
- **Evidence:** `ScanWindow.close()` (`review.py:371`) sets cancel and
  destroys the window. The same happens via "Close", the window's X button,
  or closing the main window, which destroys the review window with it.
  Scan results are cached, but her edits (renames, rotations, splits) are
  not.
- **Fix:**
  - Ask "You haven't saved yet. Close anyway?" when there are unsaved
    edits or unsaved documents.
  - Ask before closing the main window while a scan or save is running.

### B3. Saving twice, or re-running a folder, creates duplicate PDFs
- **Evidence:**
  - After "Stop" during a save, or after a finished save, the Save button
    is enabled again and `save()` re-writes every document.
  - `unique_path` then adds " (2)". Earlier GUI runs left
    `... 2025.pdf` and `... 2025 (2).pdf` side by side.
  - Re-scanning the same folder next week, which is how an archive gets
    worked through, regenerates every PDF already made.
- **Why it matters:** for an archive processed over several sessions, this
  is the most likely way she ends up with a mess.
- **Fix:**
  - Record which source photos went into which saved PDF, in the cache
    or a small `PDFs/.photo2pdf.json`.
  - Mark already-converted documents in the review ("already saved")
    and leave them unticked by default.
  - After a stopped save, Save should only write what's left.

### B4. Dropping two or more folders silently makes one giant combined PDF
- **Evidence:** `App.start()` (`app.py:266`) only switches to folder mode
  when exactly one path is a folder. With two folders, `convert()` expands
  both and merges every photo in them, holiday pictures included, into one
  PDF.
- **Fix:** if any dropped item is a folder, use folder mode (scan all the
  folders), or ask which she meant.

---

## High

### H1. Language: decided, the UI stays English
**Decision (2026-11-05, operator):** keep the application in English. OCR,
naming and document handling stay Dutch + English. When observing the
test, separate hesitation caused by wording from hesitation caused by the
interaction itself.

### H2. Page actions are hidden behind a left-click on a small thumbnail
- **Evidence:** rotate, move, split and remove only appear in a popup menu
  on left-click (`review.py:288`). The only cue is a three-line hint
  paragraph at the top, which users skip.
- **Platform convention:** on Windows, right-click is the usual place for a
  context menu, and nothing happens on right-click here.
- **Fix:**
  - Show small visible buttons on or under each thumbnail (↺ ↻ ✕).
  - Add a "split here" control between pages.
  - Bind right-click to the same menu.
  - Cut the hint to one line.

### H3. Thumbnails are too small to judge, and "Open photo" shows the wrong image
- **Size:** thumbnails are 160 px on the long side, too small to tell
  whether a page is upright, readable, or the right letter.
- **"Open photo":** it opens the *original* photo in an external viewer,
  not the straightened and rotated page that will be saved. That
  contradicts what the thumbnail shows.
- **Fix:** click (or double-click) a thumbnail to open a large in-app
  preview of the processed page, with the rotate, remove and split buttons
  next to it.

### H4. No undo in the review screen
- **Evidence:**
  - "Not a document" moves the whole group to "Other photos".
  - Adding the pages back makes each one its own one-page document, so
    the original grouping and order are lost.
  - "Remove this page" works the same way.
- **Fix:** an Undo button (Ctrl+Z) over a short history of `Plan`
  snapshots. Every edit already goes through `_edit()`, so this is a
  contained change.

### H5. Long scans give no time estimate and no reassurance
- **Evidence:** progress reads "Checked 37 of 2140: IMG_….jpg". At roughly
  1–2 s per photo per core, a large archive takes tens of minutes to hours.
- **Fix:**
  - Show the estimated time remaining.
  - Say plainly: "You can stop at any time; photos already checked are
    remembered and the next run continues where you left off". That is
    already true because of the cache, but she has no way to know.

### H6. Error and warning text is technical, and every batch asks for a report
- **Wording:** warnings such as `text recognition failed (…)` or
  `could not read image (cannot identify image file …)` are shown raw.
- **Nagging:** every batch with any warning ends in a Yes/No "Make a
  problem report about this?" (`offer_report`). That asks too often for
  routine cases like one unreadable photo, and trains her to click "No".
- **Fix:**
  - Rewrite the messages in plain language, for example "1 photo could
    not be opened and was skipped: IMG_123.jpg".
  - Offer a report only for real errors (exceptions, crashes). Show
    skips and warnings in a "Details" list.

### H7. Folder-mode saving fails with a raw error when the folder isn't writable
- **Evidence:** quick mode falls back to `Documents\Photos to PDF`, but
  folder mode calls `out_dir.mkdir()`. On a read-only location such as a
  phone mounted over MTP or a protected share, that raises and shows
  "Something went wrong: [Errno 13] …".
- **Fix:** run the same writability check before saving and offer the
  Documents fallback or "Change…".

---

## Medium

### M1. The main window mixes two modes and their options
- **Competing entry points:** the large drop area (quick mode) and the
  folder button compete for attention.
- **Options shown for the wrong mode:** "Put all photos into one PDF" and
  "Make a separate PDF for each photo" do nothing in folder mode, and
  "include subfolders" does nothing in quick mode.
- **Fix:** two clear, equal choices, for example "Make a PDF from some
  photos" and "Sort out a folder of photos", each showing only its own
  options. Move "searchable", "black and white" and "straighten" to a
  collapsed "More options" (the defaults are right).

### M2. A second scan or conversion can be started while one is running
- **Evidence:** the main window's `busy` flag isn't set during a folder
  scan, so a second scan window or a quick conversion can be started.
- **Effects:**
  - Two Tesseract pools compete for the CPU.
  - `bind_all`/`unbind_all` on the mouse wheel is global: closing one
    review window kills scrolling in the other.
- **Fix:**
  - Disable the start actions while a scan or save is running.
  - Bind the mouse wheel to the canvas instead of with `bind_all`.

### M3. Quick mode has no Stop button
A large selection can't be cancelled except by killing the app. Add Stop,
as in folder mode.

### M4. The review screen doesn't scale to a large archive
- **Density:** each document takes about 270 px of height. With 200
  documents that is roughly 100 screens of scrolling.
- **Redraw time:** measured at 0.07 s for 20 documents and 0.48 s for 200,
  so roughly 2–3 s per edit at 1000 documents (extrapolated).
- **Fix:**
  - A compact list view (one row per document: first-page thumbnail,
    name, pages, date), opening a document to edit its pages.
  - Or paging, e.g. 25 documents at a time.
  - Redraw only the document that changed.

### M5. "Other photos" shows only the first 60
Excluded photos are sorted by word count, so likely misses come first. But
in a big archive, a missed document with 0 words (badly blurred) beyond
position 60 can't be added at all. Add paging or "Show more".

### M6. Ticking "Make text searchable" off also turns off naming in quick mode, without saying so
The checkbox label doesn't mention naming. Either keep OCR for naming
regardless, or say "(also needed for automatic names)".

### M7. Per-photo mode doesn't use content naming
Combined mode names PDFs from their content, but "separate PDF for each
photo" keeps the photo's file name. The inconsistency will look like a
bug. Apply naming in both modes, with the photo name as the fallback.

### M8. The window will probably be blurry on scaled Windows displays *(verify on Windows)*
The app doesn't declare DPI awareness: there is no
`SetProcessDpiAwareness` call or manifest entry. Windows then
bitmap-stretches Tk windows at 125–150% scaling, which makes them blurry.
**Fix:** call `ctypes.windll.shcore.SetProcessDpiAwareness(1)` before
creating the Tk root, then re-check B1 sizing with real scaling.

---

## Low / polish

- **L1. Keyboard access:** thumbnails and the drop area can't be focused,
  so page actions need a mouse. This matters less for this user, but it
  blocks anyone on a keyboard.
- **L2. Text contrast:** grey `#777` text on white is 4.48:1, just below
  the WCAG AA 4.5:1 threshold for normal text. Use `#666` or darker.
- **L3. Status line width:** the main window's status line wraps at a fixed
  420 px, which cuts long paths awkwardly. Wrap to the window width.
- **L4. Disappearing feedback:** the "Saved …" message and buttons
  disappear on the next drop. Keep a short "Recent" list, and offer
  "Open PDF" for the combined case.
- **L5. Explorer pop-ups:** Explorer opens automatically after every
  quick-mode save. That's good the first time and noisy for repeated
  batches. Make it optional, remembered in settings.
- **L6. File name checking:** characters that aren't allowed are dropped
  silently, which is fine, but nothing warns when an edited name
  duplicates another (it gets " (2)"). Show a small inline note.
- **L7. Review title:** the review window title shows only the folder's
  base name. Use the full path, shortened.

---

## What already works well

- **Three ways in:** Send To, drag onto the shortcut, or the window. That
  matches how people already handle photos on Windows.
- **Safe with files:** originals are never moved or deleted, and nothing
  is ever overwritten.
- **Easy to undo a mistake:** every automatic decision (grouping,
  rotation, name) can be changed before saving.
- **Clear reporting:** the report dialog says exactly what it collects,
  and makes leaving out the recognised names a visible choice.
- **Remembers its work:** stopping a scan keeps partial results, and the
  cache makes re-runs fast.

---

## Recommended order before the test

1. B1–B4 (layout, data loss, duplicates, multi-folder drop). Roughly half
   a day.
2. H2–H4 (visible page controls, large preview, undo).
3. H5–H7 and M8, after a first look on a real Windows laptop.

Everything else can wait for her feedback. The diagnostic log already
records every review correction, which is better evidence than guesses for
M-level work.

## Suggested test script (30–45 min, observe, don't help)

1. **Install** from the `.exe` she receives, the way she would actually
   get it (email or download). Note her reaction to SmartScreen.
2. **Quick mode:** "Here are 3 photos of one letter. Make one PDF of
   them." Watch whether she finds drag-and-drop, Send To, or the click
   area.
3. **Folder mode:** "This folder has a mix of photos. Make PDFs of just the
   letters." Use 30–50 of her real photos, including at least one angled,
   one sideways, one two-page letter, and two letters taken within a
   minute of each other.
4. **Review tasks:**
   - fix a wrongly rotated page
   - split two letters that were merged
   - rename one PDF
   - add a missed document
5. **Find the results:** "Where are your PDFs now? Open the tax one."
6. **Report:** "Something looked wrong. Tell the developer." Watch whether
   she finds Help → Report.
7. **Afterwards:** ask for the report zip anyway. Its `review ` lines show
   every correction she made.

**Measure:**
- task completion without help
- time on step 3
- number of corrections in the review
- every point where she hesitates for more than about 5 seconds
- whether any PDF ends up with a wrong name, page or orientation
