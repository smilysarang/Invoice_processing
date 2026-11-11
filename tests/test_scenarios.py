"""End-to-end scenario tests through the real pipeline (deterministic mode, offline)."""
from decimal import Decimal

from src.orchestrator import process_batch
from tests.conftest import load

ORDER = ["happy_itemized", "happy_bundled", "happy_scanned", "ec1a", "ec1b", "ec2", "ec3", "ec4a", "ec4b", "split_ok", "split_bad",
         "bonus_injection", "missing", "mixed_gst", "tax_per_item", "tax_not_stated", "invalid"]


def run(*names, **kw):
    return {d["file_name"]: d for d in process_batch(load(*names), **kw)["documents"]}


def test_all_scenarios_match_expected(fresh_db, scenarios, expected):
    got = {}
    for sid in ORDER:
        got.update(run(*scenarios[sid]["files"]))
    for f, e in expected.items():
        d = got[f]["decision"]
        assert d["decision"] == e["decision"], (f, d)
        for r in e.get("reasons_include", []):
            assert r in d["reason_codes"], (f, d["reason_codes"])


def test_evidence_contract_on_happy_path(fresh_db):
    d = run("01_happy_apex_itemized.pdf")["01_happy_apex_itemized.pdf"]
    f = d["invoice"]["fields"]
    for k in ("invoice_number", "invoice_date", "po_number", "currency", "grand_total"):
        assert f[k]["value"] and f[k]["raw_text"] and f[k]["source_page"] and f[k]["bbox"] and f[k]["grounded"], k
    assert f["grand_total"]["value"] == "118000.00" and "1,18,000.00" in f["grand_total"]["raw_text"]
    assert f["supplier_bank_account"]["source_page"] == 2          # found on the continuation page


def test_scanned_invoice_uses_ocr(fresh_db):
    d = run("03_happy_scanned_northstar.pdf")["03_happy_scanned_northstar.pdf"]
    assert d["pages_meta"][0]["method"] == "ocr"
    assert d["invoice"]["fields"]["invoice_number"]["method"] == "ocr"
    assert d["decision"]["decision"] == "AUTO_APPROVE"


def test_missing_ocr_is_graceful(fresh_db, monkeypatch):
    import src.orchestrator as orch
    import src.pipeline.text_acquisition as ta
    monkeypatch.setattr(ta, "ocr_available", lambda: (False, "OCR engine unavailable (test)"))
    monkeypatch.setattr(orch, "ocr_available", lambda: (False, "OCR engine unavailable (test)"))
    d = run("03_happy_scanned_northstar.pdf")["03_happy_scanned_northstar.pdf"]
    assert d["decision"]["decision"] == "HOLD_FOR_REVIEW" and "OCR_UNAVAILABLE" in d["decision"]["reason_codes"]


def test_missing_field_is_null_not_invented(fresh_db):
    d = run("24_missing_invoice_number.pdf")["24_missing_invoice_number.pdf"]
    f = d["invoice"]["fields"]["invoice_number"]
    assert f["value"] is None and f["missing_reason"]


def test_split_group_all_or_none_and_ledger(fresh_db):
    storage = fresh_db
    ok = run("14_split_ok_A.pdf", "15_split_ok_B.pdf", "16_split_ok_C.pdf")
    assert {d["decision"]["decision"] for d in ok.values()} == {"AUTO_APPROVE"}
    led = storage.consumption("PO-2001")
    assert sum(Decimal(r["amount"]) for r in led) == Decimal("100000.00")
    # re-uploading the same batch must not double-consume
    again = run("14_split_ok_A.pdf", "15_split_ok_B.pdf", "16_split_ok_C.pdf")
    assert {d["decision"]["decision"] for d in again.values()} == {"REJECT_DUPLICATE"}
    assert sum(Decimal(r["amount"]) for r in storage.consumption("PO-2001")) == Decimal("100000.00")


def test_failing_split_group_accepts_none_and_consumes_nothing(fresh_db):
    bad = run("17_split_bad_A.pdf", "18_split_bad_B.pdf", "19_split_bad_C.pdf")
    assert {d["decision"]["decision"] for d in bad.values()} == {"HOLD_FOR_REVIEW"}
    g = fresh_db.group_for_po("PO-2002")
    assert g["status"] == "DISCREPANCY"
    assert any("APX-KBM-01: unit price 2100.00" in x for x in g["discrepancies"])
    lines = {l["item_code"]: l for l in g["lines"]}
    assert lines["APX-MON-27"]["invoiced_qty"] == "9" and lines["APX-MON-27"]["expected_qty"] == "10"
    assert fresh_db.consumption("PO-2002") == []


def test_incomplete_group_completed_in_later_session(fresh_db):
    first = run("14_split_ok_A.pdf", "15_split_ok_B.pdf")
    assert {d["decision"]["decision"] for d in first.values()} == {"PENDING_SPLIT_RECONCILIATION"}
    assert fresh_db.consumption("PO-2001") == []
    run("16_split_ok_C.pdf")
    for d in first.values():
        assert fresh_db.current_decision(d["document_id"])["result"]["decision"] == "AUTO_APPROVE"
    assert fresh_db.group_for_po("PO-2001", open_only=False)["status"] == "ACCEPTED"


def test_inferred_po_policy_switch(fresh_db, monkeypatch):
    from src import master_data
    pol = dict(master_data.policy())
    pol["po_inference"] = dict(pol["po_inference"], allow_auto_approve_inferred_po=False)
    monkeypatch.setattr("src.pipeline.policy_engine.policy", lambda: pol)
    d = run("04_ec1a_no_po_single_candidate.pdf")["04_ec1a_no_po_single_candidate.pdf"]
    assert d["decision"]["decision"] == "HOLD_FOR_REVIEW" and "PO_INFERRED" in d["decision"]["reason_codes"]


def test_audit_trail_complete(fresh_db):
    d = run("11_ec3_bank_change.pdf")["11_ec3_bank_change.pdf"]
    stages = {e["stage"] for e in fresh_db.audit_for(d["document_id"])}
    for s in ("intake", "text", "understanding", "extraction", "normalization", "verification", "vendor_po", "duplicates_risk",
              "po_group", "decision", "explanation"):
        assert s in stages
    assert d["decision"]["policy_version"] == "1.0.0"
    assert {t["mode"] for t in fresh_db.traces(d["document_id"])} == {"deterministic_demo"}
    # bank details are masked in the record
    assert "77700099988811" not in str(d["normalization"])
