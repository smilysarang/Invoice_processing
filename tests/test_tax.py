"""Tax handling, item by item against PO line rates (src/pipeline/tax.py).

PO-2001: one rate - 18% GST. Monitor 6,000.00 net.
PO-1003: mixed rates - tea 450.00 and coffee 700.00 at 5%, kettle 1,500.00 at 18%.
"""
from src.models import ExtractedInvoice, FieldEvidence, InvoiceLine
from src.pipeline import matching, policy_engine, verification


def make(lines, incl=False, rate_printed=None, components=None, po_number="PO-2001", **fields):
    inv = ExtractedInvoice(doc_type="invoice", extractor="rules", tax_inclusive=incl, tax_rate_printed=rate_printed,
                           tax_components=components or [])
    for k, v in {"po_number": po_number, "currency": "INR", "invoice_date": "2026-10-20", **fields}.items():
        if v is not None:
            inv.fields[k] = FieldEvidence(field=k, value=v, raw_text=v)
    inv.lines = []
    for i, ln in enumerate(lines, 1):
        c, d, q, p, t, *tax = ln
        inv.lines.append(InvoiceLine(line_no=i, description=d, item_code=c, quantity=q, unit_price=p, line_total=t,
                                     tax_rate=tax[0] if tax else None, tax_amount=tax[1] if len(tax) > 1 else None))
    return inv


def run(inv):
    found, calc = verification.verify(inv, [])
    pm = matching.match_po(inv, "V001", calc)
    return {f.rule_id: f for f in found + pm["findings"]}, calc, pm


def tax_fails(f):
    return sorted(k for k, x in f.items() if x.status == "FAIL" and any(s in k for s in ("TAX", "PRICE", "TOTAL", "LINE")))


def comp(label, rate, amount):
    return {"label": label, "rate": rate, "amount": amount}


MON = ("APX-MON-27", "27-inch IPS Monitor")
TEA, COF, KTL = ("PAN-TEA-1K", "Assam tea, 1 kg pack"), ("PAN-COF-1K", "Filter coffee powder, 1 kg pack"), ("APP-KTL-17", "Electric kettle 1.7 L")
NET_1003 = [(*TEA, "40", "450.00", "18000.00"), (*COF, "30", "700.00", "21000.00"), (*KTL, "6", "1500.00", "9000.00")]
GROSS_1003 = [(*TEA, "40", "472.50", "18900.00"), (*COF, "30", "735.00", "22050.00"), (*KTL, "6", "1770.00", "10620.00")]
GST_1003 = [comp("CGST", "2.5", "975.00"), comp("SGST", "2.5", "975.00"), comp("CGST", "9", "810.00"), comp("SGST", "9", "810.00")]


# ------------------------------------------------------------------ the PO side
def test_po_model_has_per_line_rates():
    from src.master_data import purchase_orders
    po = purchase_orders()["PO-1003"]
    assert po.mixed_rates and [str(r) for r in po.rates] == ["0.05", "0.18"]
    assert po.subtotal == 48000 and po.tax_total == 3570 and po.grand_total == 51570


# ------------------------------------------------------------------ 2. tax summarised with rates
def test_summary_tax_lines_assigned_to_item_slabs():
    f, _, _ = run(make(NET_1003, components=GST_1003, po_number="PO-1003",
                       subtotal="48000.00", tax_total="3570.00", grand_total="51570.00"))
    assert not tax_fails(f)
    assert "CGST 2.5% 975.00 + SGST 2.5% 975.00 = 5% on PAN-TEA-1K, PAN-COF-1K (39000.00)" in f["TAX_RATE_PRINTED"].message
    assert "5% of 39000.00 + 18% of 9000.00 = 3570.00" in f["TAX_RATE_CHECK"].message


def test_two_rates_on_the_same_taxable_amount_are_not_confused():
    # tea 20 x 450 = 9,000 at 5% and kettle 6 x 1,500 = 9,000 at 18%: identical bases. Grouping by base would merge them
    # into one 23% slab; assigning to the items' slabs does not.
    lines = [(*TEA, "20", "450.00", "9000.00"), (*KTL, "6", "1500.00", "9000.00")]
    gst = [comp("CGST", "2.5", "225.00"), comp("SGST", "2.5", "225.00"), comp("CGST", "9", "810.00"), comp("SGST", "9", "810.00")]
    f, _, _ = run(make(lines, components=gst, po_number="PO-1003", subtotal="18000.00", tax_total="2070.00", grand_total="20070.00"))
    assert not tax_fails(f) and f["TAX_RATE_PRINTED"].status == "PASS"


def test_unequal_split_that_adds_up_is_accepted():
    # 2% + 3% = 5% on the 5% items (e.g. tax + cess); nothing assumes 'half each'
    gst = [comp("GST", "2", "780.00"), comp("Cess", "3", "1170.00"), comp("CGST", "9", "810.00"), comp("SGST", "9", "810.00")]
    f, _, _ = run(make(NET_1003, components=gst, po_number="PO-1003", subtotal="48000.00", tax_total="3570.00", grand_total="51570.00"))
    assert not tax_fails(f) and "GST 2% 780.00 + Cess 3% 1170.00 = 5%" in f["TAX_RATE_PRINTED"].message


def test_lone_cgst_and_mismatched_pairs_are_flagged():
    lone = [comp("CGST", "2.5", "975.00"), comp("SGST", "2.5", "975.00"), comp("CGST", "9", "810.00")]
    f, _, _ = run(make(NET_1003, components=lone, po_number="PO-1003", subtotal="48000.00", tax_total="2760.00", grand_total="50760.00"))
    assert {"TAX_RATE_MISMATCH", "TAX_RATE_CHECK"} <= set(tax_fails(f))
    pair = [comp("CGST", "2.5", "975.00"), comp("SGST", "9", "810.00")]
    f, _, _ = run(make(NET_1003, components=pair, po_number="PO-1003", subtotal="48000.00", tax_total="1785.00", grand_total="49785.00"))
    assert "TAX_RATE_MISMATCH" in tax_fails(f)


def test_wrong_rate_on_one_item_is_flagged():
    # the kettle charged at 5% too: 5% of 48,000 = 2,400 instead of 3,570
    gst = [comp("CGST", "2.5", "1200.00"), comp("SGST", "2.5", "1200.00")]
    f, _, _ = run(make(NET_1003, components=gst, po_number="PO-1003", subtotal="48000.00", tax_total="2400.00", grand_total="50400.00"))
    assert {"TAX_RATE_MISMATCH", "TAX_RATE_CHECK"} <= set(tax_fails(f))


def test_rate_not_on_po_and_right_label_wrong_amount():
    f, _, _ = run(make([(*MON, "6", "6000.00", "36000.00")], components=[comp("CGST", "6", "2160.00"), comp("SGST", "6", "2160.00")],
                       subtotal="36000.00", tax_total="4320.00", grand_total="40320.00"))
    assert {"TAX_RATE_MISMATCH", "TAX_RATE_CHECK"} <= set(tax_fails(f))
    f, _, _ = run(make([(*MON, "6", "6000.00", "36000.00")], components=[comp("CGST", "9", "2160.00"), comp("SGST", "9", "2160.00")],
                       subtotal="36000.00", tax_total="4320.00", grand_total="40320.00"))
    assert "TAX_RATE_CHECK" in tax_fails(f)


# ------------------------------------------------------------------ 1. tax printed per item
def test_tax_printed_per_item():
    ok = [(*TEA, "40", "450.00", "18000.00", "5", "900.00"), (*COF, "30", "700.00", "21000.00", "5", "1050.00"),
          (*KTL, "6", "1500.00", "9000.00", "18", "1620.00")]
    f, _, _ = run(make(ok, po_number="PO-1003", subtotal="48000.00", tax_total="3570.00", grand_total="51570.00"))
    assert not tax_fails(f) and f["TAX_LINE_CHECK"].status == "PASS"
    wrong_rate = ok[:2] + [(*KTL, "6", "1500.00", "9000.00", "12", "1080.00")]
    f, _, _ = run(make(wrong_rate, po_number="PO-1003", subtotal="48000.00", tax_total="3030.00", grand_total="51030.00"))
    assert "TAX_RATE_MISMATCH" in tax_fails(f) and "APP-KTL-17 charged 12%, PO line rate 18%" in f["TAX_RATE_MISMATCH"].message
    wrong_amount = ok[:2] + [(*KTL, "6", "1500.00", "9000.00", "18", "1500.00")]
    f, _, _ = run(make(wrong_amount, po_number="PO-1003", subtotal="48000.00", tax_total="3450.00", grand_total="51450.00"))
    assert "TAX_RATE_CHECK" in tax_fails(f)
    f, _, _ = run(make(ok, po_number="PO-1003", subtotal="48000.00", tax_total="3600.00", grand_total="51600.00"))
    assert "TAX_TOTAL_CONSISTENCY" in tax_fails(f)


# ------------------------------------------------------------------ 3. only a total tax amount
def test_only_total_tax_amount_is_checked_against_item_rates():
    f, _, _ = run(make(NET_1003, po_number="PO-1003", subtotal="48000.00", tax_total="3570.00", grand_total="51570.00"))
    assert not tax_fails(f) and "no tax rate printed" in f["TAX_RATE_CHECK"].message
    f, _, _ = run(make(NET_1003, po_number="PO-1003", subtotal="48000.00", tax_total="8640.00", grand_total="56640.00"))  # 18% on all
    assert "TAX_RATE_CHECK" in tax_fails(f)


# ------------------------------------------------------------------ tax-inclusive with a printed tax amount
def test_inclusive_prices_netted_per_item_rate():
    f, _, pm = run(make(GROSS_1003, incl=True, po_number="PO-1003", tax_total="3570.00", grand_total="51570.00"))
    prices = {m["item_code"]: m["unit_price"] for m in pm["line_mapping"]["mapped"]}
    assert prices == {"PAN-TEA-1K": "450.00", "PAN-COF-1K": "700.00", "APP-KTL-17": "1500.00"} and not tax_fails(f)


def test_inclusive_with_printed_subtotal_is_really_reconciled():
    f, _, _ = run(make([(*MON, "6", "7166.67", "43000.00")], incl=True, subtotal="36000.00", tax_total="6480.00", grand_total="43000.00"))
    assert f["TOTAL_RECONCILIATION"].status == "FAIL"


# ------------------------------------------------------------------ 4. no tax stated at all
def test_tax_not_stated_total_equals_po_value_incl_tax_passes_with_warning():
    for po_number, lines, total in (("PO-2001", [(*MON, "6", "7080.00", "42480.00")], "42480.00"),
                                    ("PO-1003", GROSS_1003, "51570.00")):
        for incl in (True, False):          # with or without 'prices include GST' wording
            inv = make(lines, incl=incl, po_number=po_number, grand_total=total)
            f, calc, pm = run(inv)
            assert not tax_fails(f), (po_number, incl, tax_fails(f))
            assert f["TAX_NOT_STATED"].status == "WARN" and f["TAX_EMBEDDED_TOTAL_MATCH"].status == "PASS"
            assert calc["tax_derived"] and inv.tax_inclusive
            assert all(m["price_ok"] for m in pm["line_mapping"]["mapped"])
    assert calc["net_subtotal"] == "48000.00" and calc["tax_total"] == "3570.00"


def test_tax_not_charged_is_held():
    f, _, _ = run(make([(*MON, "6", "6000.00", "36000.00")], grand_total="36000.00"))
    assert "TAX_NOT_CHARGED" in tax_fails(f) and "Expected tax 6480.00" in f["TAX_NOT_CHARGED"].message


def test_tax_not_stated_and_total_matches_nothing_is_held():
    f, _, _ = run(make([(*MON, "6", "6600.00", "39600.00")], grand_total="39600.00"))
    assert "TAX_TOTAL_VS_PO" in tax_fails(f) and f["TAX_NOT_STATED"].status == "WARN"


def test_tax_warning_appears_in_the_explanation():
    from src.agents.explanation_agent import template
    f, _, _ = run(make([(*MON, "6", "7080.00", "42480.00")], grand_total="42480.00"))
    findings = [x.model_dump() for x in f.values() if x.rule_id in ("TAX_NOT_STATED", "TAX_EMBEDDED_TOTAL_MATCH", "LINE_PRICE_MATCH")]
    d = policy_engine.decide(findings).model_dump()
    assert d["decision"] == "AUTO_APPROVE" and "TAX_NOT_STATED" in d["reason_codes"]
    ex = template({"decision": d, "findings": findings})
    assert "does not state any tax" in ex["explanation"] and any("tax is not stated" in c for c in ex["reviewer_checklist"])
