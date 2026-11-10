"""Page numbers as grouping evidence; duplicates (retakes) never cut a letter in two."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photo2pdf.detect import Plan, PhotoInfo, group_documents, mark_duplicates
from photo2pdf.features import minhash
from photo2pdf.naming import DocFields, page_number

T0 = datetime(2025, 3, 1, 10, 0)
TEXT = "geachte heer mevrouw hierbij ontvangt u de voorlopige aanslag inkomstenbelasting voor het jaar"


def ph(name, secs, page=None, total=None, words=60, text=None, sender=None, date=None, dhash=0, **kw):
    p = PhotoInfo(Path(name), T0 + timedelta(seconds=secs), words=words, is_document=True,
                  page_no=page, page_total=total, dhash=dhash, **kw)
    p.minhash = minhash(text or f"{name} {TEXT} unique{name}") if words >= 4 else []
    if sender or date:
        p.fields = DocFields(sender=sender, date=date)
    return p


def names(groups):
    return [[p.path.name for p in g] for g in groups]


@pytest.mark.parametrize("text,expected", [
    ("Belastingdienst\nbody\nPagina 2 van 3", (2, 3)),
    ("Header\nbody text\nPage 5 of 17", (5, 17)),
    ("x\nblad 2/4", (2, 4)),
    ("Our ref 123\nbody\n3/5", (3, 5)),
    ("body\n- 2 -", (2, None)),
    ("body\nPagina 4", (4, None)),
    ("U betaalt in 2 van 3 termijnen\nmet vriendelijke groet", None),   # not a page number
    ("Datum 12/03/2024\nbody", None),
    ("page 9 of 3", None),                                             # impossible
])
def test_page_number(text, expected):
    assert page_number(text) == expected


def test_numbered_pages_join_across_long_pauses_and_changing_dates():
    # 17 pages photographed over 40 minutes; page 5 mentions another date in its text.
    photos = [ph(f"p{n}", n * 150, page=n, total=17, date=(datetime(2024, 1, n).date() if n == 5 else None))
              for n in range(1, 18)]
    assert names(group_documents(photos, 180)) == [[f"p{n}" for n in range(1, 18)]]


def test_page_one_starts_a_new_document_even_seconds_later():
    photos = [ph("a1", 0, 1, 2), ph("a2", 20, 2, 2), ph("b1", 40, 1, 3), ph("b2", 60, 2, 3)]
    assert names(group_documents(photos, 180)) == [["a1", "a2"], ["b1", "b2"]]


def test_complete_document_ends_and_restart_splits():
    photos = [ph("a1", 0, 1, 2), ph("a2", 20, 2, 2), ph("x", 40), ph("c3", 60, 3, 4), ph("c2", 80, 2, 4)]
    g = names(group_documents(photos, 180))
    assert g[0] == ["a1", "a2"]           # 2 of 2: complete, nothing more joins
    assert ["c2"] in g                    # page 2 after page 3: a restart, not a continuation


def test_unnumbered_pages_keep_the_old_rules():
    photos = [ph("a", 0), ph("b", 30), ph("c", 1000)]
    assert names(group_documents(photos, 180)) == [["a", "b"], ["c"]]


def test_non_document_between_numbered_pages_does_not_split_them():
    other = PhotoInfo(Path("cat.jpg"), T0 + timedelta(seconds=50), words=0, is_document=False)
    photos = [ph("p1", 0, 1, 3), ph("p2", 30, 2, 3), other, ph("p3", 70, 3, 3)]
    assert names(group_documents(photos, 180)) == [["p1", "p2", "p3"]]


def test_retake_does_not_cut_the_letter():
    # Page 2 photographed twice (second one blurred, so very different picture and few words).
    p1 = ph("p1", 0, 1, 3, dhash=0)
    p2 = ph("p2", 30, 2, 3, dhash=0, text="tweede pagina " + TEXT)
    p2b = ph("p2b", 50, 2, 3, dhash=(1 << 200) - 1, words=2, hard_to_read=True)
    p3 = ph("p3", 80, 3, 3, dhash=0)
    plan = Plan.from_scan([p1, p2, p2b, p3], 180)
    assert names(plan.groups) == [["p1", "p2", "p3"]]
    assert p2b.duplicate_of == p2.path and "Duplicate of p2" in p2b.reason


def test_retake_from_another_angle_is_a_duplicate_by_its_words():
    a = ph("a", 0, text=TEXT, dhash=0)
    b = ph("b", 90, text=TEXT, dhash=(1 << 256) - 1, words=55)      # same words, different picture
    mark_duplicates([a, b])
    assert b.duplicate_of == a.path and not b.is_document


def test_pages_with_different_numbers_are_never_duplicates():
    a = ph("a", 0, 1, 2, text=TEXT, dhash=0)
    b = ph("b", 30, 2, 2, text=TEXT, dhash=0)                        # same form layout and boilerplate
    mark_duplicates([a, b])
    assert a.is_document and b.is_document


def test_same_words_far_apart_in_time_need_the_same_picture():
    a = ph("a", 0, text=TEXT, dhash=0)
    b = ph("b", 86400, text=TEXT, dhash=(1 << 256) - 1)              # next day: likely a new copy
    mark_duplicates([a, b])
    assert b.is_document
