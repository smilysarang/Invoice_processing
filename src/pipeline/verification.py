"""Stage 6 - verification & evidence grounding (deterministic, Decimal arithmetic)."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from src.master_data import policy, tol
from src.models import CRITICAL_FIELDS, ExtractedInvoice, PageText, ValidationFinding
from src.pipeline.normalization import normalize_text

Q = Decimal("0.01")


def _f(rule, sev, status, msg, **kw) -> ValidationFinding:
    return ValidationFinding(rule_id=rule, stage="verification", severity=sev, status=status, message=msg, **kw)


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", normalize_text(s)).lower()


def ground(inv: ExtractedInvoice, pages: list[PageText]) -> None:
    """Mark each field grounded only if its raw text can be found in the page text it claims to come from."""
    by_page = {p.page: _squash(p.full_text) for p in pages}
    all_text = _squash("\n".join(p.full_text for p in pages))
    for f in inv.fields.values():
        if not f.raw_text:
            continue
        hay = by_page.get(f.source_page or 0, all_text) or all_text
        needle = _squash(f.raw_text)
        if not needle:
            continue
        if f.field == "tax_total":
            parts = [_squash(x) for x in f.raw_text.split("|")]
            score = 100.0 if all(p in all_text for p in parts) else fuzz.partial_ratio(needle, all_text)
        else:
            score = 100.0 if needle in hay else float(fuzz.partial_ratio(needle, hay))
        f.grounding_score = round(score, 1)
        f.grounded = score >= policy()["confidence"]["grounding_min_score"]
        base = f.confidence if f.confidence is not None else (1.0 if f.method == "native_text" else 0.9)
        f.confidence = round(base * (score / 100.0), 3)


def compute(inv: ExtractedInvoice, po_tax_rate: Optional[Decimal] = None) -> dict:
    """Derive tax-exclusive (net) values used for PO comparison. Returns a calc dict (strings).

    po_tax_rate is only used when prices include tax but no tax amount is printed (PO candidate scoring);
    the binding per-item treatment of that case is in tax.resolve_missing_tax."""
    gt, st, tax = inv.dec("grand_total"), inv.dec("subtotal"), inv.dec("tax_total") or Decimal("0")
    disc, ship = inv.dec("discount_total") or Decimal("0"), inv.dec("shipping_total") or Decimal("0")
    rate, derived = None, False
    if inv.tax_inclusive and gt is not None and tax:
        net_total = gt - tax
        rate = (tax / net_total) if net_total else None
    elif inv.tax_inclusive and gt is not None and po_tax_rate:
        # prices include tax but no tax amount is printed: back out the tax at the PO's rate (flagged by the caller)
        rate = po_tax_rate
        tax = (gt - (st if st is not None else gt / (1 + rate))).quantize(Q)
        derived = True
    for ln in inv.lines:
        up, lt = (Decimal(ln.unit_price) if ln.unit_price else None), (Decimal(ln.line_total) if ln.line_total else None)
        if inv.tax_inclusive and rate:
            ln.net_unit_price = str((up / (1 + rate)).quantize(Q)) if up is not None else None
            ln.net_line_total = str((lt / (1 + rate)).quantize(Q)) if lt is not None else None
        else:
            ln.net_unit_price, ln.net_line_total = ln.unit_price, ln.line_total
    lines_sum = sum((Decimal(l.line_total) for l in inv.lines if l.line_total), Decimal("0"))
    net_lines = sum((Decimal(l.net_line_total) for l in inv.lines if l.net_line_total), Decimal("0"))
    if st is not None:
        net_sub = st
    elif inv.tax_inclusive and gt is not None:
        net_sub = gt - tax
    else:
        net_sub = net_lines if inv.lines else None
    return {"lines_sum": str(lines_sum.quantize(Q)), "net_lines_sum": str(net_lines.quantize(Q)),
            "net_subtotal": str(net_sub.quantize(Q)) if net_sub is not None else None,
            "tax_total": str(tax.quantize(Q)), "discount": str(disc), "shipping": str(ship),
            "grand_total": str(gt) if gt is not None else None,
            "effective_tax_rate": str((tax / net_sub).quantize(Decimal("0.0001"))) if net_sub else None,
            "tax_inclusive": inv.tax_inclusive, "tax_derived": derived}


def verify(inv: ExtractedInvoice, pages: list[PageText]) -> tuple[list[ValidationFinding], dict]:
    out: list[ValidationFinding] = []
    pol = policy()
    ground(inv, pages)

    # requiredness / ambiguity (ambiguity gets its own reason code so the reviewer knows what to resolve)
    missing, ambiguous = [], []
    for name in pol["required_fields"]:
        f = inv.fields.get(name)
        if f is None or f.value is None:
            reason = f.missing_reason if f else "not extracted"
            (ambiguous if reason and reason.startswith("AMBIGUOUS") else missing).append((name, reason, f))
    for name, reason, f in ambiguous:
        out.append(_f(reason, "ERROR", "FAIL", f"{name} is ambiguous: {'; '.join(f.notes) if f else ''}. Not guessed - needs confirmation.",
                      actual=f.raw_text if f else None))
    if missing:
        out.append(_f("MISSING_REQUIRED_FIELD", "ERROR", "FAIL",
                      "Missing required field(s): " + ", ".join(f"{n} ({r})" for n, r, _ in missing) + ". Values were not invented."))
    else:
        out.append(_f("MISSING_REQUIRED_FIELD", "INFO", "PASS", "All required header fields present."))
    for name in ("due_date",):
        f = inv.fields.get(name)
        if f and f.missing_reason == "AMBIGUOUS_DATE":
            out.append(_f("AMBIGUOUS_DATE", "ERROR", "FAIL", f"{name} '{f.raw_text}' is ambiguous (day/month order)."))
    inv_no = inv.fields.get("invoice_number")
    if inv_no and any("MULTIPLE" in n for n in inv_no.notes):
        out.append(_f("MULTIPLE_DOCUMENTS", "ERROR", "FAIL", "More than one invoice number printed - file may contain several invoices; split before processing."))

    # grounding
    ungrounded = [f.field for f in inv.fields.values() if f.value is not None and f.grounded is False]
    crit = [x for x in ungrounded if x in CRITICAL_FIELDS]
    if crit:
        out.append(_f("EVIDENCE_GROUNDING", "ERROR", "FAIL", f"Critical field(s) not found in the document text: {crit}."))
    elif ungrounded:
        out.append(_f("EVIDENCE_GROUNDING", "WARN", "WARN", f"Non-critical field(s) weakly grounded: {ungrounded}."))
    else:
        out.append(_f("EVIDENCE_GROUNDING", "INFO", "PASS", "Every extracted value is traceable to the page text."))

    # OCR confidence on critical fields
    thr = pol["confidence"]["ocr_min_field_confidence"]
    low = [f"{f.field} ({f.confidence})" for f in inv.fields.values()
           if f.method == "ocr" and f.field in CRITICAL_FIELDS and f.value is not None and (f.confidence or 0) < thr]
    if low:
        out.append(_f("LOW_OCR_CONFIDENCE", "ERROR", "FAIL", f"OCR confidence below {thr} on: {', '.join(low)}."))
    elif any(f.method == "ocr" for f in inv.fields.values()):
        out.append(_f("LOW_OCR_CONFIDENCE", "INFO", "PASS", f"OCR confidence on critical fields >= {thr}."))

    # dates
    d, due = inv.val("invoice_date"), inv.val("due_date")
    if d and due:
        ok = d <= due
        out.append(_f("DATE_ORDER", "ERROR" if not ok else "INFO", "PASS" if ok else "FAIL",
                      f"Invoice date {d} {'<=' if ok else '>'} due date {due}."))

    # arithmetic
    calc = compute(inv)
    tol_abs = tol("invoice_arithmetic_abs")
    bad = []
    for ln in inv.lines:
        if ln.quantity and ln.unit_price and ln.line_total:
            exp = (Decimal(ln.quantity) * Decimal(ln.unit_price)).quantize(Q)
            if abs(exp - Decimal(ln.line_total)) > tol_abs:
                bad.append(f"line {ln.line_no}: {ln.quantity} x {ln.unit_price} = {exp}, printed {ln.line_total}")
    if not inv.lines:
        out.append(_f("LINE_ITEMS_PRESENT", "WARN", "WARN", "No itemised lines recognised; only totals can be compared."))
    elif bad:
        out.append(_f("LINE_ARITHMETIC", "ERROR", "FAIL", "Line arithmetic does not hold: " + "; ".join(bad)))
    else:
        out.append(_f("LINE_ARITHMETIC", "INFO", "PASS", f"{len(inv.lines)} line(s): quantity x unit price = line total."))

    gt = inv.dec("grand_total")
    lines_sum = Decimal(calc["lines_sum"])
    if inv.lines and gt is not None:
        target = gt if inv.tax_inclusive else (inv.dec("subtotal") if inv.dec("subtotal") is not None else None)
        if target is not None:
            ok = abs(lines_sum - target) <= tol_abs
            out.append(_f("LINES_SUM_RECONCILIATION", "ERROR" if not ok else "INFO", "PASS" if ok else "FAIL",
                          f"Sum of lines {lines_sum} {'=' if ok else '!='} {'grand total (tax-inclusive)' if inv.tax_inclusive else 'subtotal'} {target}.",
                          expected=str(target), actual=str(lines_sum)))
    # an invoice with no tax amount at all is resolved against the PO in matching (src/pipeline/tax.py)
    if gt is not None and calc["net_subtotal"] is not None:
        st, printed_tax = inv.dec("subtotal"), inv.dec("tax_total")
        if inv.tax_inclusive and st is not None and printed_tax is not None:
            # a separate net subtotal is printed: it must really add up with the included tax
            exp = st - Decimal(calc["discount"]) + Decimal(calc["shipping"]) + printed_tax
            ok = abs(exp - gt) <= tol_abs
            msg = (f"Tax-inclusive invoice: net subtotal {st} - discount {calc['discount']} + shipping {calc['shipping']} + included tax "
                   f"{printed_tax} = {exp.quantize(Q)} {'=' if ok else '!='} grand total {gt}.")
        elif inv.tax_inclusive:
            ok = True   # nothing independent to reconcile: net is derived as grand total - included tax
            msg = (f"Tax-inclusive invoice without a separate net subtotal: net derived as grand total {gt} - included tax "
                   f"{calc['tax_total'] if printed_tax is not None else '(not printed)'}.")
        else:
            exp = Decimal(calc["net_subtotal"]) - Decimal(calc["discount"]) + Decimal(calc["shipping"]) + Decimal(calc["tax_total"])
            ok = abs(exp - gt) <= tol_abs
            msg = (f"Subtotal {calc['net_subtotal']} - discount {calc['discount']} + shipping {calc['shipping']} + tax {calc['tax_total']}"
                   f" = {exp.quantize(Q)} {'=' if ok else '!='} grand total {gt}.")
        out.append(_f("TOTAL_RECONCILIATION", "ERROR" if not ok else "INFO", "PASS" if ok else "FAIL", msg))
    return out, calc
