import logging
import os
import threading
import zipfile
from pathlib import Path

import pytest

from photo2pdf import diag


@pytest.fixture
def logenv(tmp_path, monkeypatch):
    home = tmp_path / "Anna"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "annadevries")
    monkeypatch.setenv("USERNAME", "annadevries")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Windows paths come from these; point them into the test home too.
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    root = logging.getLogger()
    saved = root.handlers[:]
    for h in saved:
        root.removeHandler(h)
    path = diag.setup()
    yield home, path
    for h in root.handlers[:]:
        h.close()
        root.removeHandler(h)
    # diag.setup() points native-crash tracebacks at crash.log; give them back to stderr so a
    # crash later in the test run is visible in CI output.
    import faulthandler
    import sys
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    for h in saved:
        root.addHandler(h)


def test_log_location_and_redaction(logenv):
    home, path = logenv
    expected = (home / "AppData" / "Local" if os.name == "nt" else home / ".cache") / "Photo2PDF" / "logs"
    assert path == expected / "photo2pdf.log"
    logging.info("opened %s for annadevries", home / "Pictures" / "IMG_1.jpg")
    try:
        raise ValueError(f"bad file {home}/x.jpg")
    except ValueError:
        logging.exception("boom")
    text = path.read_text("utf-8")
    assert os.path.join("~", "Pictures", "IMG_1.jpg") in text and "for <user>" in text
    assert str(home) not in text and "annadevries" not in text
    assert "ValueError: bad file ~/x.jpg" in text


def test_rolls_over(logenv, monkeypatch):
    _, path = logenv
    handler = next(h for h in logging.getLogger().handlers if getattr(h, "_photo2pdf", False))
    handler.maxBytes = 2000
    for i in range(200):
        logging.info("line %d %s", i, "x" * 50)
    files = sorted(p.name for p in path.parent.glob("photo2pdf.log*"))
    assert "photo2pdf.log.1" in files and len(files) <= 1 + diag.BACKUPS


def test_thread_exceptions_are_logged(logenv):
    _, path = logenv

    def bad():
        raise RuntimeError("worker died")

    t = threading.Thread(target=bad, name="worker")
    t.start()
    t.join()
    assert "worker died" in path.read_text("utf-8")


def test_report_contents_and_field_exclusion(logenv, tmp_path):
    home, _ = logenv
    logging.info("scan IMG_1.jpg: words=80 document=True")
    diag.fields("scan IMG_1.jpg fields %s", {"sender": "Belastingdienst"})
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with_fields = diag.build_report("Page 2 came out sideways", True, out_dir=out_dir)
    z = zipfile.ZipFile(with_fields)
    names = z.namelist()
    assert {"message.txt", "system.json", "report-info.txt", "logs/photo2pdf.log"} <= set(names)
    log = z.read("logs/photo2pdf.log").decode()
    assert "Belastingdienst" in log and "words=80" in log
    assert z.read("message.txt").decode().startswith("Page 2 came out sideways")

    no_fields = diag.build_report("again", False, out_dir=out_dir)
    assert no_fields != with_fields  # same minute: never overwrites
    log2 = zipfile.ZipFile(no_fields).read("logs/photo2pdf.log").decode()
    assert "Belastingdienst" not in log2 and "FIELDS [left out]" in log2
    assert "words=80" in log2
