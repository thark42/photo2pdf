from datetime import date

from photo2pdf.naming import DocFields, extract, find_dates, merge, pick_date, sanitize, suggest_name

TODAY = date(2026, 11, 5)
NONE = ([], [])


def test_dates_nl_en_formats():
    t = "op 3 mrt. 2024 en 14 maart 2025, of March 2, 2023, 2022-01-31 en 05-06-2021; 31-02-2020 is fout"
    assert [d for _, d in find_dates(t, TODAY)] == [
        date(2024, 3, 3), date(2025, 3, 14), date(2023, 3, 2), date(2022, 1, 31), date(2021, 6, 5)]


def test_labelled_date_wins_over_earlier_date():
    t = "Geboren 01-01-1980\nUw brief van 2 januari 2025\nDagtekening: 14 maart 2025\n"
    assert pick_date(t, TODAY) == date(2025, 3, 14)


def test_future_and_ancient_years_ignored():
    assert pick_date("Datum 01-01-2031 en 01-01-1950", TODAY) is None


def test_belastingdienst_voorlopige_aanslag():
    text = ("Belastingdienst\nDatum 14 maart 2025\nVoorlopige aanslag inkomstenbelasting 2025\n"
            "Geachte heer, mevrouw,\n...\nBent u het niet eens met deze beschikking? Dan kunt u bezwaar maken.")
    f = extract(text, TODAY, NONE)
    assert (f.date, f.sender, f.doc_type, f.year) == (
        date(2025, 3, 14), "Belastingdienst", "Voorlopige aanslag inkomstenbelasting", 2025)
    assert suggest_name(f, "x.pdf") == \
        "2025-03-14 Belastingdienst - Voorlopige aanslag inkomstenbelasting 2025.pdf"


def test_toeslagen_and_gemeente_and_woz():
    f = extract("Belastingdienst/Toeslagen\nDefinitieve berekening zorgtoeslag 2023\nDatum 2 april 2024",
                TODAY, NONE)
    assert f.sender == "Toeslagen" and f.doc_type == "Definitieve berekening Zorgtoeslag"
    g = extract("Gemeente Utrecht\nWOZ-beschikking en aanslag gemeentelijke belastingen\n28 februari 2025",
                TODAY, NONE)
    assert g.sender == "Gemeente Utrecht" and g.doc_type == "Aanslag gemeentelijke belastingen"


def test_footer_words_do_not_decide_type():
    body = "UWV\nDatum 1 mei 2025\nUw uitkering\n" + "tekst " * 200 + "\nbezwaar maken? herinnering"
    f = extract(body, TODAY, NONE)
    assert f.sender == "UWV" and f.doc_type is None


def test_english_hmrc():
    f = extract("HM Revenue & Customs\nDate of issue: 6 April 2025\nSelf Assessment tax year 2024-25",
                TODAY, NONE)
    assert f.sender == "HMRC" and f.doc_type == "Self Assessment" and f.date == date(2025, 4, 6)
    assert f.year == 2024


def test_nothing_found_uses_fallback():
    assert suggest_name(extract("random words here", TODAY, NONE), "Document 01.pdf") == "Document 01.pdf"


def test_single_weak_field_uses_fallback():
    f = extract("Dear customer, thank you for paying the invoice.", TODAY, NONE)
    assert f.doc_type == "Invoice"
    assert suggest_name(f, "Document 02.pdf") == "Document 02.pdf"


def test_merge_and_sanitize():
    m = merge([DocFields(sender="UWV"), DocFields(date=date(2025, 1, 2), sender="SVB")])
    assert m.sender == "UWV" and m.date == date(2025, 1, 2)
    assert sanitize('a/b:c*"d"?  ') == "abcd"


def test_user_rules_take_priority():
    from photo2pdf.naming import Rule
    f = extract("Zilveren Kruis\nPolis 2025", TODAY, ([Rule("zilveren kruis", "Zilveren Kruis")],
                                                      [Rule(r"\bpolis\b", "Polis")]))
    assert f.sender == "Zilveren Kruis" and f.doc_type == "Polis"
