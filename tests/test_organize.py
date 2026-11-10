"""Plan editing extras, the saved-documents registry, and organized output."""
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from photo2pdf import registry
from photo2pdf.core import Options, PageSpec, convert, save_documents
from photo2pdf.detect import Plan, PhotoInfo, ScanSettings, scan_paths
from photo2pdf.naming import DocFields
from test_core import text_page

needs_tess = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
T0 = datetime(2025, 3, 1, 10, 0)


def ph(name, secs, **fields):
    p = PhotoInfo(Path(name), T0 + timedelta(seconds=secs), words=50, is_document=True)
    p.thumb = Image.new("RGB", (10, 20))
    if fields:
        p.fields = DocFields(**fields)
    return p


def test_undo_restores_structure_rotation_names_and_thumb():
    a1, a2, b1 = ph("a1", 0), ph("a2", 10), ph("b1", 999)
    plan = Plan.from_scan([a1, a2, b1], 180)
    thumb_before = a1.thumb
    for edit in (lambda: plan.rotate(0, 0, 90), lambda: plan.set_name(0, "Mine"),
                 lambda: plan.join_previous(1), lambda: plan.remove_group(0)):
        plan.checkpoint()
        edit()
    assert plan.groups == [] and a1.rotation == 90
    for _ in range(4):
        plan.undo()
    assert [[p.path.name for p in g] for g in plan.groups] == [["a1", "a2"], ["b1"]]
    assert a1.rotation == 0 and a1.thumb is thumb_before
    assert plan.custom_names == {} and not plan.can_undo()


def test_already_saved_documents_start_unticked(tmp_path):
    a1, b1 = ph("a1", 0), ph("b1", 999)
    a1.saved_as = tmp_path / "x.pdf"
    plan = Plan.from_scan([a1, b1], 180)
    assert plan.is_saved(0) and not plan.is_included(0)
    assert plan.included_indices() == [1]
    plan.set_included(0, True)
    assert plan.included_indices() == [0, 1]
    plan.mark_saved(1, tmp_path / "y.pdf")
    assert plan.is_saved(1) and not plan.is_included(1)


@pytest.mark.parametrize("mode,expected", [
    ("none", ["2025-03-14 Belastingdienst - Aanslag.pdf", "Document 02 - 2025-03-01 10.16 (1 page).pdf"]),
    ("year", ["2025/2025-03-14 Belastingdienst - Aanslag.pdf",
              "Undated/Document 02 - 2025-03-01 10.16 (1 page).pdf"]),
    ("sender", ["Belastingdienst/2025-03-14 Belastingdienst - Aanslag.pdf",
                "Other/Document 02 - 2025-03-01 10.16 (1 page).pdf"]),
    ("sender_year", ["Belastingdienst/2025/2025-03-14 Belastingdienst - Aanslag.pdf",
                     "Other/Undated/Document 02 - 2025-03-01 10.16 (1 page).pdf"]),
])
def test_output_paths_by_organize_mode(mode, expected):
    a = ph("a", 0, date=date(2025, 3, 14), sender="Belastingdienst", doc_type="Aanslag")
    b = ph("b", 999)
    assert Plan.from_scan([a, b], 180).output_paths(mode) == expected


def test_registry_roundtrip_and_deleted_pdf(tmp_path):
    photo = tmp_path / "IMG_1.jpg"
    Image.new("RGB", (10, 10)).save(photo)
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF")
    registry.record([photo], pdf)
    assert registry.lookup([photo]) == {photo: pdf}
    pdf.unlink()
    assert registry.lookup([photo]) == {}   # PDF gone -> photo counts as not saved
    pdf.write_bytes(b"%PDF")
    Image.new("RGB", (12, 10)).save(photo)  # photo replaced -> new key
    assert registry.lookup([photo]) == {}


def test_save_documents_organizes_records_and_rescan_marks_saved(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    text_page(folder / "IMG_20250301_100000.jpg")
    text_page(folder / "IMG_20250301_100020.jpg")
    docs = [("2025/Letter.pdf", [PageSpec(folder / "IMG_20250301_100000.jpg", 0),
                                 PageSpec(folder / "IMG_20250301_100020.jpg", 0)])]
    res = save_documents(docs, folder / "PDFs", Options(ocr=False))
    out = folder / "PDFs" / "2025" / "Letter.pdf"
    assert res.outputs == [out] and res.saved == [(0, out)]
    assert len(PdfReader(str(out)).pages) == 2
    saved = registry.lookup(sorted(folder.glob("*.jpg")))
    assert set(saved.values()) == {out}


def test_save_documents_stop_writes_no_partial_document(tmp_path):
    for i in range(3):
        text_page(tmp_path / f"IMG_{i}.jpg")
    cancel = threading.Event()
    calls = []

    def progress(d, t, m):
        calls.append(d)
        if d == 0:
            cancel.set()   # stop while page 1 of document A renders; page 2 never starts
    docs = [("A.pdf", [tmp_path / "IMG_0.jpg", tmp_path / "IMG_1.jpg"]), ("B.pdf", [tmp_path / "IMG_2.jpg"])]
    res = save_documents(docs, tmp_path / "out", Options(ocr=False), progress=progress, cancel=cancel)
    assert res.stopped and res.outputs == []
    assert "You stopped before the end" in " ".join(res.summary_lines())


def test_quick_convert_stop_saves_nothing(tmp_path):
    for i in range(3):
        text_page(tmp_path / f"IMG_{i}.jpg")
    cancel = threading.Event()
    cancel.set()
    res = convert([tmp_path], Options(ocr=False), cancel=cancel)
    assert res.stopped and not res.outputs and not list(tmp_path.glob("*.pdf"))


@needs_tess
def test_scan_paths_multiple_folders_and_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    text_page(a / "IMG_20250301_100000.jpg")
    text_page(b / "IMG_20250302_100000.jpg")
    loose = tmp_path / "IMG_20250303_100000.jpg"
    text_page(loose)
    infos = scan_paths([a, b, loose, a], ScanSettings(workers=2), _tess(), opts=Options())
    assert sorted(i.path.name for i in infos) == [
        "IMG_20250301_100000.jpg", "IMG_20250302_100000.jpg", "IMG_20250303_100000.jpg"]
    assert all(i.is_document and i.saved_as is None for i in infos)


@needs_tess
def test_per_photo_mode_uses_content_names(tmp_path):
    from synth import tax_letter
    tax_letter().save(tmp_path / "IMG_1.jpg")
    text_page(tmp_path / "IMG_2.jpg")
    res = convert([tmp_path], Options(combine=False))
    assert sorted(p.name for p in res.outputs) == [
        "2025-03-14 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf", "IMG_2.pdf"]


def _tess():
    from photo2pdf.core import Tesseract
    return Tesseract.find()


@needs_tess
def test_rescan_after_save_shows_documents_as_saved_not_new(tmp_path):
    folder = tmp_path / "archive"
    folder.mkdir()
    text_page(folder / "IMG_20250301_100000.jpg")
    settings = ScanSettings(workers=2)
    infos = scan_paths([folder], settings, _tess(), opts=Options())
    plan = Plan.from_scan(infos, 180)
    docs = [(p, [PageSpec(x.path, x.rotation) for x in plan.groups[g]])
            for g, p in zip(plan.included_indices(), plan.output_paths("year"))]
    save_documents(docs, folder / "PDFs", Options(ocr=False))

    again = Plan.from_scan(scan_paths([folder], settings, _tess(), opts=Options()), 180)
    assert again.is_saved(0) and again.included_indices() == []   # nothing to save twice


def test_non_document_thumbs_are_compact_and_decode_on_demand():
    import io
    p = PhotoInfo(Path("cat.jpg"), T0, words=0, is_document=False)
    buf = io.BytesIO()
    Image.new("RGB", (160, 120), "orange").save(buf, "JPEG")
    p.thumb_jpeg = buf.getvalue()
    assert p.thumb is None
    assert p.get_thumb().size == (160, 120)
    plan = Plan.from_scan([p], 180)
    plan.add(0)
    plan.rotate(0, 0, 90)          # promoted photo can be turned
    assert plan.groups[0][0].display_thumb().size == (120, 160)
    assert plan.groups[0][0].get_thumb().size == (160, 120)   # stored copy untouched


def test_walk_skips_hidden_and_system_folders(tmp_path):
    from photo2pdf.detect import list_folder
    for d in ("ok", ".hidden", "AppData", "Windows", "$Recycle.Bin", "ok/deeper"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (5, 5)).save(tmp_path / d / "a.jpg")
    got = sorted(str(p.relative_to(tmp_path)).replace("\\", "/") for p in list_folder(tmp_path, True))
    assert got == ["ok/a.jpg", "ok/deeper/a.jpg"]
