"""Drive the real windows without a person. Skipped where Tk has no display (Linux CI);
runs on the Windows CI runner."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(monkeypatch):
    try:
        root = tk.Tk()
        root.destroy()
    except tk.TclError:
        pytest.skip("no display")
    from photo2pdf import app as appmod
    a = appmod.App([])
    a.root.withdraw()
    yield a
    try:
        a.root.destroy()
    except tk.TclError:
        pass
    # Free every Tk object now, on this (the Tk) thread, and give automatic GC back to the
    # tests that follow: App switches it off so collections only run on the Tk thread.
    import gc
    gc.collect()
    gc.enable()


def _plan_photos(n_docs=30):
    from photo2pdf.detect import PhotoInfo
    t0 = datetime(2025, 1, 1)
    out = []
    for d in range(n_docs):
        for p in range(2):
            ph = PhotoInfo(Path(f"C:/x/IMG_{d:03d}_{p}.jpg"), t0 + timedelta(hours=d, seconds=20 * p),
                           words=60, is_document=True)
            ph.thumb = Image.new("RGB", (113, 160), "white")
            out.append(ph)
    other = PhotoInfo(Path("C:/x/cat.jpg"), t0 + timedelta(days=9), words=0, is_document=False)
    other.thumb = Image.new("RGB", (160, 120), "orange")
    return out + [other]


def test_review_window_edit_undo_page_and_close(app, monkeypatch, tmp_path):
    from photo2pdf import review
    monkeypatch.setattr(review.ScanWindow, "_start_scan", lambda self: None)
    sw = review.ScanWindow(app, [tmp_path], app.opts, app.scan)
    sw._on_scanned(_plan_photos())
    sw.win.update()
    assert "30 new documents" in sw.title.cget("text")
    assert sw.b_save.cget("text") == "Save 30 PDFs"
    # Paging: 12 per page -> 3 pages
    assert len(sw.inner.winfo_children()) == review.DOCS_PER_PAGE
    sw._set_page(2)
    assert len(sw.inner.winfo_children()) == 30 - 2 * review.DOCS_PER_PAGE
    # Edit + undo
    sw._edit(sw.plan.split, 0, 1)
    assert len(sw.plan.groups) == 31 and str(sw.b_undo.cget("state")) == "normal"
    sw.undo()
    assert len(sw.plan.groups) == 30
    # Untick one document
    sw._edit(sw.plan.set_included, 0, False)
    assert sw.b_save.cget("text") == "Save 29 PDFs"
    # Rotating changes the displayed thumbnail (PhotoImage cache keyed by thumb object)
    before = sw._thumb(sw.plan.groups[1][0])
    sw._edit(sw.plan.rotate, 1, 0, 90)
    assert sw._thumb(sw.plan.groups[1][0]) is not before
    # Tabs
    sw._set_tab(review.TAB_OTHER)
    sw._set_tab(review.TAB_SAVED)
    assert sw.pending_work()            # 29 unsaved documents
    # Large preview loads the page in a worker and hands it back through a queue.
    from synth import page
    real = tmp_path / "IMG_real.jpg"
    page().save(real)
    sw.plan.groups[1][0].path = real
    pv = review.Preview(sw, 1, 0)
    for _ in range(100):
        sw.win.update()
        if pv.base is not None:
            break
        import time
        time.sleep(0.05)
    assert pv.base is not None
    pv.win.destroy()
    sw.close(force=True)
    assert app.scan_window is None or not sw.win.winfo_exists()


def test_app_runs_gc_only_on_tk_thread(app):
    import gc
    assert not gc.isenabled()          # collections are scheduled on the Tk loop instead


def test_main_window_settings_and_multi_folder_routing(app, monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(app, "open_folders", lambda paths: calls.setdefault("folders", paths))
    monkeypatch.setattr(app, "_start_quick", lambda files: calls.setdefault("quick", files))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    app.start([str(a), str(b)])                  # two folders -> organize mode, not one giant PDF
    assert calls["folders"] == [a, b]
    f = tmp_path / "x.jpg"
    Image.new("RGB", (10, 10)).save(f)
    app.start([str(f)])
    assert calls["quick"] == [str(f)]
    from photo2pdf.app import SettingsDialog
    d = SettingsDialog(app)
    d.v_org.set("One folder per sender")
    d.ok()
    assert app.opts.organize == "sender"
