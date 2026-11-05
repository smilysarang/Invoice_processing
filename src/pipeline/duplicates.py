"""Stage 7b - layered duplicate detection (file hash, business key, reissue, credit-rebill, near, recurring)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from src.master_data import policy, purchase_orders
from src.models import ExtractedInvoice, ValidationFinding
from src.pipeline.normalization import norm_invoice_number


def _f(rule, sev, status, msg, **kw) -> ValidationFinding:
    return ValidationFinding(rule_id=rule, stage="duplicates", severity=sev, status=status, message=msg, **kw)


def _days(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except Exception:
        return None


def _ref(h: dict) -> str:
    return f"{h['invoice_number'] or h['file_name'] or '?'} ({'seed history ' + h['ref'] if h['source'] == 'seed' else 'processed ' + str(h['document_id'])})"


def check(sha256: str, inv: Optional[ExtractedInvoice], vendor_id: Optional[str], po_number: Optional[str],
          history: list[dict]) -> dict:
    cfg = policy()["duplicates"]
    findings: list[ValidationFinding] = []
    matches: list[dict] = []
    classification = "none"

    # 1. exact file bytes
    same_file = [h for h in history if h.get("file_sha256") and h["file_sha256"] == sha256]
    if same_file:
        h = same_file[0]
        matches.append({"type": "exact_file", "matched": _ref(h), "signals": ["identical SHA-256"], "status": h["status"]})
        findings.append(_f("DUP_EXACT_FILE", "ERROR", "FAIL", f"Byte-identical file already received: {_ref(h)} (status {h['status']}).",
                           evidence=sha256[:16] + "..."))
        classification = "exact"
    if inv is None:
        return {"classification": classification, "matches": matches, "findings": findings}

    num = norm_invoice_number(inv.val("invoice_number"))
    amt = inv.dec("grand_total")
    d = inv.val("invoice_date")
    po_type = purchase_orders()[po_number].billing_type if po_number in purchase_orders() else None
    vend_hist = [h for h in history if vendor_id and h.get("vendor_id") == vendor_id and h.get("doc_type") != "UNREADABLE"]
    invoices = [h for h in vend_hist if h["doc_type"] == "INVOICE"]
    credits = [h for h in vend_hist if h["doc_type"] == "CREDIT_NOTE"]

    # 2. same vendor + invoice number
    if num and classification != "exact":
        for h in invoices:
            if h["invoice_number_norm"] != num:
                continue
            same_amt = amt is not None and h["grand_total"] and Decimal(h["grand_total"]) == amt
            same_date = (not d) or (not h["invoice_date"]) or h["invoice_date"] == d
            if same_amt and same_date and h.get("status") not in ("CANCELLED",):
                matches.append({"type": "exact_business_key", "matched": _ref(h),
                                "signals": ["same vendor", "same invoice number", "same amount", "same date"], "status": h["status"]})
                findings.append(_f("DUP_EXACT_BUSINESS_KEY", "ERROR", "FAIL",
                                   f"Same vendor + invoice number {inv.val('invoice_number')} + amount {amt} + date already recorded: "
                                   f"{_ref(h)} (status {h['status']}). Different file bytes (re-scan / re-export)."))
                classification = "exact"
            else:
                sig = ["same vendor", "same invoice number"]
                if not same_amt:
                    sig.append(f"amount changed {h['grand_total']} -> {amt}")
                if not same_date:
                    sig.append(f"date changed {h['invoice_date']} -> {d}")
                matches.append({"type": "possible_reissue", "matched": _ref(h), "signals": sig, "status": h["status"]})
                findings.append(_f("DUP_POSSIBLE_REISSUE", "ERROR", "FAIL",
                                   f"Invoice number {inv.val('invoice_number')} was already received ({_ref(h)}) with different content: "
                                   f"{'; '.join(sig[2:])}. Treat as a corrected reissue - confirm which version is valid; both are linked."))
                classification = "possible_reissue" if classification == "none" else classification
            break

    # 3. explicit references to earlier documents (reissue / credit-and-rebill)
    for ref in inv.references:
        rn = norm_invoice_number(ref)
        orig = next((h for h in invoices if h["invoice_number_norm"] == rn), None)
        if not orig:
            continue
        credit = next((c for c in credits if norm_invoice_number(c.get("related_invoice")) == rn), None)
        if credit and orig["grand_total"] and credit["grand_total"] and Decimal(credit["grand_total"]) == -Decimal(orig["grand_total"]):
            matches.append({"type": "credit_rebill", "matched": _ref(orig),
                            "signals": [f"references {ref}", f"credit note {credit['invoice_number']} reverses it in full ({credit['grand_total']})"],
                            "status": orig["status"]})
            findings.append(_f("CREDIT_REBILL_EVIDENCED", "WARN", "WARN",
                               f"Re-bill of {orig['invoice_number']} ({orig['grand_total']}), fully reversed by credit note "
                               f"{credit['invoice_number']} - not a duplicate. Linked for audit."))
            if classification == "none":
                classification = "credit_rebill"
        else:
            matches.append({"type": "possible_reissue", "matched": _ref(orig), "signals": [f"references {ref}", "no reversing credit note found"],
                            "status": orig["status"]})
            findings.append(_f("DUP_POSSIBLE_REISSUE", "ERROR", "FAIL",
                               f"Invoice references earlier invoice {orig['invoice_number']} but no credit note reversing it was found."))
            if classification in ("none", "credit_rebill"):
                classification = "possible_reissue"

    # 4. near duplicates vs legitimate recurring billing
    if classification == "none" and amt is not None:
        for h in invoices:
            if h["invoice_number_norm"] == num or not h["grand_total"] or h.get("status") in ("CANCELLED",) or h["doc_type"] != "INVOICE":
                continue
            ha = Decimal(h["grand_total"])
            if ha == 0:
                continue
            close_amt = abs(ha - amt) / abs(ha) * 100 <= Decimal(str(cfg["near_amount_pct"]))
            gap = _days(d, h["invoice_date"])
            in_window = gap is not None and gap <= cfg["near_window_days"]
            same_po = (h.get("po_number") or None) == (po_number or None)
            num_sim = fuzz.ratio(num or "", h["invoice_number_norm"] or "")
            if not (close_amt and in_window and same_po):
                different_pos = bool(po_number and h.get("po_number") and not same_po)  # a different PO is a strong distinguishing signal
                if num_sim >= cfg["invoice_number_similarity"] and close_amt and not different_pos:
                    matches.append({"type": "near", "matched": _ref(h), "signals": [f"invoice number similarity {num_sim:.0f}", "similar amount"],
                                    "status": h["status"]})
                    findings.append(_f("DUP_NEAR", "ERROR", "FAIL", f"Look-alike of {_ref(h)}: invoice number similarity {num_sim:.0f}, similar amount."))
                    classification = "near"
                continue
            period, hp = inv.service_period, h.get("service_period")
            if po_type == "BLANKET" and period and hp and period.strip().lower() != hp.strip().lower():
                matches.append({"type": "recurring_pattern", "matched": _ref(h),
                                "signals": ["same vendor, PO and amount", f"different period: {hp} -> {period}", f"{gap} days apart", "blanket PO"],
                                "status": h["status"]})
                findings.append(_f("RECURRING_PATTERN", "WARN", "WARN",
                                   f"Same amount as {_ref(h)} but a different service period ({hp} -> {period}) on blanket PO {po_number}: "
                                   "legitimate recurring billing, not a duplicate."))
                classification = "recurring_pattern"
            else:
                matches.append({"type": "near", "matched": _ref(h), "signals": ["same vendor, PO and amount", f"{gap} days apart",
                                                                                 "no distinguishing period"], "status": h["status"]})
                findings.append(_f("DUP_NEAR", "ERROR", "FAIL",
                                   f"Same vendor, PO and amount as {_ref(h)} within {gap} days, with a different invoice number and no "
                                   "distinguishing service period - possible duplicate."))
                classification = "near"
                break
    if classification == "none":
        findings.append(_f("DUPLICATE_CHECK", "INFO", "PASS", f"No duplicate signals across {len(history)} historical record(s)."))
    return {"classification": classification, "matches": matches, "findings": findings}
