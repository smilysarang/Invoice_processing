from src.pipeline.normalization import detect_currency, detect_locale, normalize_text, parse_amount, parse_date
from src.pipeline.text_acquisition import text_quality


def test_indian_lakh_grouping():
    assert parse_amount("INR 1,18,000.00").value == "118000.00"
    assert parse_amount("5,00,000.00").value == "500000.00"


def test_german_decimal_comma():
    assert parse_amount("4.203,48 EUR", "comma_decimal").value == "4203.48"
    assert parse_amount("249,90", "comma_decimal").value == "249.90"
    assert parse_amount("1.234", "comma_decimal").value == "1234.00"


def test_ambiguous_numbers_are_not_guessed():
    p = parse_amount("1,234")
    assert p.value is None and p.ambiguous
    p = parse_amount("1.234")
    assert p.value is None and p.ambiguous


def test_non_western_digits():
    assert normalize_text("१,२३४.५०") == "1,234.50"            # Devanagari
    assert parse_amount("١٬٢٣٤٫٥٠").value == "1234.50"           # Arabic-Indic with Arabic separators
    assert parse_amount("₹ ५,००,०००.००").value == "500000.00"


def test_dates():
    assert parse_date("14/10/2026").value == "2026-10-14"           # day > 12 -> unambiguous
    p = parse_date("07/10/2026")
    assert p.value is None and p.ambiguous                          # no locale evidence -> hold
    assert parse_date("07/10/2026", "DMY").value == "2026-10-07"
    assert parse_date("28 Oct 2026").value == "2026-10-28"
    assert parse_date("31. Oktober 2026").value == "2026-10-31"
    assert parse_date("31.10.2026").value == "2026-10-31"


def test_locale_detection_german():
    loc = detect_locale("Rechnung Rechnungsdatum: 31.10.2026 Zwischensumme 4.203,48 Gesamtbetrag 4.203,48 EUR")
    assert loc.language == "de" and loc.number_style == "comma_decimal" and loc.date_order == "DMY"


def test_currency():
    assert detect_currency("Total € 12,00").value == "EUR"
    p = detect_currency("Total $4,250.00")
    assert p.value is None and p.ambiguous
    assert detect_currency("Total USD 4,250.00").value == "USD"


def test_garbled_text_layer_scores_low():
    assert text_quality("Invoice No: APX-INV-1042 Invoice Date: 14/10/2026 Grand Total 1,18,000.00") > 0.9
    assert text_quality("�� ���  ####$$$$%%%%^^^^&&&&****") < 0.5
