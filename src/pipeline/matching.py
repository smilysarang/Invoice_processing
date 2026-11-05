"""Stage 7a - vendor master checks, PO matching hierarchy, invoice-line to PO-line mapping."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from src import storage
from src.master_data import policy, purchase_orders, tol, vendors
from src.models import ExtractedInvoice, PurchaseOrder, ValidationFinding, Vendor
from src.pipeline.normalization import mask_account, norm_account, normalize_text, parse_date

Q = Decimal("0.01")


def _f(rule, sev, status, msg, stage="vendor_po", **kw) -> ValidationFinding:
    return ValidationFinding(rule_id=rule, stage=stage, severity=sev, status=status, message=msg, **kw)


def _norm_id(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ------------------------------------------------------------------ vendor
def resolve_vendor(inv: ExtractedInvoice) -> dict:
    vs = vendors()
    findings: list[ValidationFinding] = []
    name, tax_id = inv.val("supplier_name"), inv.val("supplier_tax_id")
    by_tax = next((v for v in vs.values() if tax_id and _norm_id(v.tax_id) == _norm_id(tax_id)), None)
    best, best_score = None, 0.0
    if name:
        for v in vs.values():
            s = max(fuzz.token_set_ratio(normalize_text(name).lower(), n.lower()) for n in [v.name] + v.aliases)
            if s > best_score:
                best, best_score = v, s
    by_name = best if best_score >= 88 else None
    vendor, method = None, None
    if by_tax and by_name and by_tax.vendor_id != by_name.vendor_id:
        findings.append(_f("VENDOR_IDENTITY_CONFLICT", "ERROR", "FAIL",
                           f"Tax ID {tax_id} belongs to {by_tax.name} but the letterhead reads '{name}' ({by_name.name})."))
        vendor, method = by_tax, "tax_id (conflicting name)"
    elif by_tax:
        vendor, method = by_tax, "tax_id exact" + (" + name" if by_name else "")
    elif by_name:
        vendor, method = by_name, f"name fuzzy {best_score:.0f}"
        if tax_id and by_name.tax_id and _norm_id(tax_id) != _norm_id(by_name.tax_id):
            findings.append(_f("VENDOR_TAX_ID_MISMATCH", "ERROR", "FAIL",
                               f"Printed tax ID {tax_id} differs from vendor master {by_name.tax_id}."))
    if not vendor:
        findings.append(_f("VENDOR_NOT_FOUND", "ERROR", "FAIL", f"No approved vendor matches '{name}' / tax ID {tax_id}."))
        return {"vendor_id": None, "vendor": None, "method": None, "score": best_score, "findings": findings}
    findings.append(_f("VENDOR_MATCH", "INFO", "PASS", f"Matched vendor master {vendor.vendor_id} {vendor.name} by {method}."))
    if not (vendor.approved and vendor.status == "ACTIVE"):
        findings.append(_f("VENDOR_NOT_APPROVED", "ERROR", "FAIL",
                           f"Vendor {vendor.vendor_id} is {vendor.status} / approved={vendor.approved}: {vendor.contact_ref}."))
    else:
        findings.append(_f("VENDOR_NOT_APPROVED", "INFO", "PASS", "Vendor is approved and active."))
    cur = inv.val("currency")
    if cur and vendor.allowed_currencies and cur not in vendor.allowed_currencies:
        findings.append(_f("VENDOR_CURRENCY_NOT_ALLOWED", "ERROR", "FAIL", f"{cur} is not an allowed currency for {vendor.name} ({vendor.allowed_currencies})."))
    acct = inv.val("supplier_bank_account")
    if acct:
        if vendor.bank_account and norm_account(acct) == norm_account(vendor.bank_account):
            findings.append(_f("BANK_DETAILS_MATCH", "INFO", "PASS",
                               f"Invoice bank account {mask_account(acct)} matches the verified vendor master.", stage="risk"))
        else:
            findings.append(_f("BANK_MISMATCH_HIGH_RISK", "ERROR", "FAIL",
                               f"Bank account on invoice {mask_account(acct)} differs from verified vendor master {mask_account(vendor.bank_account)}. "
                               f"Do not update master data from an invoice - verify independently via registered contact: {vendor.contact_ref}.",
                               stage="risk", expected=mask_account(vendor.bank_account), actual=mask_account(acct)))
    else:
        findings.append(_f("BANK_DETAILS_MATCH", "INFO", "PASS", "No bank details printed; payment would use the verified vendor master.", stage="risk"))
    return {"vendor_id": vendor.vendor_id, "vendor": vendor.model_dump(), "method": method, "score": best_score, "findings": findings}


# ------------------------------------------------------------------ PO consumption helpers
def consumed(po: PurchaseOrder) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in storage.consumption(po.po_number):
        out[r["item_code"]] = out.get(r["item_code"], Decimal("0")) + Decimal(r["quantity"])
    return out


def remaining(po: PurchaseOrder) -> dict[str, Decimal]:
    c = consumed(po)
    return {l.item_code: l.quantity - c.get(l.item_code, Decimal("0")) for l in po.lines}


def remaining_value(po: PurchaseOrder) -> Decimal:
    rem = remaining(po)
    return sum((rem[l.item_code] * l.unit_price for l in po.lines), Decimal("0")).quantize(Q)


# ------------------------------------------------------------------ line mapping
def map_lines(inv: ExtractedInvoice, po: PurchaseOrder) -> dict:
    """Map each invoice line to a PO line (item code first, then strong unique description match)."""
    codes = {l.item_code: l for l in po.lines}
    mapped, unmapped = [], []
    if po.allow_bundled_invoice and len(inv.lines) == 1 and len(po.lines) > 1 and inv.lines[0].item_code not in codes:
        ln = inv.lines[0]
        return {"mode": "BUNDLED", "mapped": [], "unmapped": [],
                "bundled": {"description": ln.description, "net_total": ln.net_line_total, "po_lines": [l.item_code for l in po.lines]}}
    for ln in inv.lines:
        target, how = None, None
        if ln.item_code and ln.item_code in codes:
            target, how = codes[ln.item_code], "item code"
        else:
            scored = sorted(((fuzz.token_set_ratio(ln.description.lower(), l.description.lower()), l) for l in po.lines),
                            key=lambda x: -x[0])
            if scored and scored[0][0] >= 85 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 10):
                target, how = scored[0][1], f"description {scored[0][0]:.0f}"
        if not target:
            unmapped.append({"line_no": ln.line_no, "description": ln.description, "item_code": ln.item_code,
                             "net_line_total": ln.net_line_total})
            continue
        price_ok = ln.net_unit_price is not None and abs(Decimal(ln.net_unit_price) - target.unit_price) <= \
            target.unit_price * tol("line_unit_price_pct") / 100 + Decimal("0.005")
        mapped.append({"line_no": ln.line_no, "po_line_no": target.line_no, "item_code": target.item_code, "match_by": how,
                       "quantity": ln.quantity, "uom": ln.uom, "po_uom": target.uom, "unit_price": ln.net_unit_price,
                       "po_unit_price": str(target.unit_price), "price_ok": price_ok, "net_line_total": ln.net_line_total})
    return {"mode": "LINES", "mapped": mapped, "unmapped": unmapped}


# ------------------------------------------------------------------ PO matching
def _candidate_score(inv: ExtractedInvoice, calc: dict, po: PurchaseOrder) -> dict:
    m = map_lines(inv, po)
    rem = remaining(po)
    n = max(1, len(inv.lines))
    feats = {}
    feats["item_codes"] = len([x for x in m["mapped"] if x["match_by"] == "item code"]) / n
    feats["descriptions"] = (sum(max(fuzz.token_set_ratio(l.description.lower(), p.description.lower()) for p in po.lines)
                                 for l in inv.lines) / n / 100) if inv.lines and po.lines else 0
    feats["unit_prices"] = len([x for x in m["mapped"] if x["price_ok"]]) / n
    feats["qty_within_remaining"] = len([x for x in m["mapped"] if x["quantity"] and Decimal(x["quantity"]) <= rem.get(x["item_code"], 0)]) / n
    net = Decimal(calc["net_subtotal"]) if calc.get("net_subtotal") else None
    rv = remaining_value(po)
    feats["amount"] = 1.0 if net is not None and abs(net - rv) <= Q else (0.5 if net is not None and rv and abs(net - rv) / rv <= Decimal("0.05") else 0.0)
    d = inv.val("invoice_date")
    feats["date_after_po"] = 1.0 if d and d >= po.po_date else 0.0
    weights = {"item_codes": 0.35, "descriptions": 0.15, "unit_prices": 0.2, "qty_within_remaining": 0.1, "amount": 0.15, "date_after_po": 0.05}
    score = sum(feats[k] * w for k, w in weights.items())
    bonus = None
    ref = inv.order_reference_text or ""
    if ref:
        p = parse_date(ref)
        if p.value and p.value == po.po_date:
            score += 0.10
            bonus = f"order date '{ref}' = PO date {po.po_date}"
    return {"po_number": po.po_number, "score": round(min(score, 1.0), 3), "features": {k: round(float(v), 2) for k, v in feats.items()},
            "bonus": bonus, "po_date": po.po_date, "description": po.description, "remaining_value": str(rv),
            "billing_type": po.billing_type}


def match_po(inv: ExtractedInvoice, vendor_id: Optional[str], calc: dict) -> dict:
    pos = purchase_orders()
    cfg = policy()["po_inference"]
    findings: list[ValidationFinding] = []
    printed = inv.val("po_number")
    result = {"printed_po": printed, "selected_po": None, "method": None, "ambiguous": False, "candidates": [], "findings": findings,
              "line_mapping": None}
    if printed:
        po = next((p for p in pos.values() if _norm_id(p.po_number) == _norm_id(printed)), None)
        if not po:
            findings.append(_f("PO_NOT_FOUND", "ERROR", "FAIL", f"PO '{printed}' does not exist in the procurement system."))
            return result
        if vendor_id and po.vendor_id != vendor_id:
            findings.append(_f("PO_VENDOR_MISMATCH", "ERROR", "FAIL",
                               f"{po.po_number} belongs to vendor {po.vendor_id}, but this invoice is from {vendor_id}. Not accepted against this PO."))
            return result
        cur = inv.val("currency")
        if cur and cur != po.currency:
            findings.append(_f("PO_CURRENCY_MISMATCH", "ERROR", "FAIL", f"Invoice currency {cur} != PO currency {po.currency}; no FX rate is inferred."))
        if po.status != "OPEN":
            findings.append(_f("PO_CLOSED", "ERROR", "FAIL", f"{po.po_number} status is {po.status}."))
        findings.append(_f("PO_MATCH", "INFO", "PASS", f"Exact PO reference {po.po_number} belongs to the matched vendor."))
        result.update(selected_po=po.po_number, method="exact_reference")
    else:
        if not vendor_id:
            findings.append(_f("PO_NO_CANDIDATE", "ERROR", "FAIL", "No PO printed and vendor unknown - cannot search candidates."))
            return result
        cands = [_candidate_score(inv, calc, p) for p in pos.values()
                 if p.vendor_id == vendor_id and p.status == "OPEN" and (not inv.val("currency") or p.currency == inv.val("currency"))]
        cands.sort(key=lambda c: -c["score"])
        for c in cands:
            c["plausible"] = c["score"] >= cfg["plausible_score"]
        result["candidates"] = cands
        plausible = [c for c in cands if c["plausible"]]
        top = cands[0] if cands else None
        close = [c for c in plausible if top and top["score"] - c["score"] <= cfg["ambiguity_margin"]]
        if not plausible:
            findings.append(_f("PO_NO_CANDIDATE", "ERROR", "FAIL", f"No PO printed and no open PO of this vendor is a plausible match ({len(cands)} checked)."))
        elif len(close) > 1:
            result["ambiguous"] = True
            findings.append(_f("PO_AMBIGUOUS", "ERROR", "FAIL",
                               f"No PO printed; {len(close)} open POs match equally well ({', '.join(c['po_number'] for c in close)}). Not guessing."))
        elif top["score"] >= cfg["strong_score"]:
            result.update(selected_po=top["po_number"], method="inferred")
            findings.append(_f("PO_INFERRED", "WARN", "WARN",
                               f"No PO printed; inferred {top['po_number']} (score {top['score']}) - next best "
                               f"{cands[1]['po_number'] + ' ' + str(cands[1]['score']) if len(cands) > 1 else 'none'}."))
        else:
            findings.append(_f("PO_WEAK_MATCH", "ERROR", "FAIL",
                               f"No PO printed; best candidate {top['po_number']} scores only {top['score']} (< {cfg['strong_score']})."))
    if result["selected_po"]:
        po = pos[result["selected_po"]]
        lm = map_lines(inv, po)
        result["line_mapping"] = lm
        result["billing_type"] = po.billing_type
        findings.extend(line_findings(inv, po, lm, calc))
    return result


def line_findings(inv: ExtractedInvoice, po: PurchaseOrder, lm: dict, calc: dict) -> list[ValidationFinding]:
    """Per-invoice PO checks. For ONE_TIME POs the binding quantity/price checks happen at PO-group level."""
    out = []
    group_level = po.billing_type == "ONE_TIME"
    sev = "INFO" if group_level else "ERROR"
    if lm["mode"] == "BUNDLED":
        exp = po.subtotal
        net = Decimal(calc["net_subtotal"]) if calc.get("net_subtotal") else None
        ok = net is not None and abs(net - exp) <= tol("po_group_abs")
        out.append(_f("BUNDLED_TOTAL_MATCH" if ok else "BUNDLED_TOTAL_MISMATCH", "WARN" if ok else "ERROR", "WARN" if ok else "FAIL",
                      f"Bundled invoice (1 line) vs {len(po.lines)}-line PO {po.po_number}: net {net} {'=' if ok else '!='} PO {exp}. "
                      "PO allows bundled billing; line-level evidence not available."))
    if lm["unmapped"]:
        out.append(_f("UNEXPECTED_ITEM", "ERROR", "FAIL",
                      f"{len(lm['unmapped'])} invoice line(s) do not map to any line of {po.po_number}: "
                      + "; ".join(u["description"] for u in lm["unmapped"])))
    bad = [m for m in lm["mapped"] if not m["price_ok"]]
    if bad:
        out.append(_f("LINE_PRICE_MISMATCH", sev, "FAIL" if not group_level else "INFO",
                      "Unit price differs from PO: " + "; ".join(f"{m['item_code']} {m['unit_price']} vs PO {m['po_unit_price']}" for m in bad)
                      + (" (evaluated at PO-group level)" if group_level else "")))
    elif lm["mapped"]:
        out.append(_f("LINE_PRICE_MATCH", "INFO", "PASS", f"{len(lm['mapped'])} line(s) mapped to {po.po_number} at PO unit prices."))
    # tax rate vs PO
    net = Decimal(calc["net_subtotal"]) if calc.get("net_subtotal") else None
    if net is not None:
        exp_tax = (net * po.tax_rate).quantize(Q)
        tax = Decimal(calc["tax_total"])
        ok = abs(exp_tax - tax) <= tol("tax_abs")
        out.append(_f("TAX_RATE_CHECK", "ERROR" if not ok else "INFO", "PASS" if ok else "FAIL",
                      f"Tax {tax} vs PO rate {po.tax_rate * 100:.0f}% of {net} = {exp_tax}."))
    if po.billing_type == "BLANKET":
        rem = remaining(po)
        over = [m for m in lm["mapped"] if m["quantity"] and Decimal(m["quantity"]) > rem.get(m["item_code"], 0)]
        if over:
            out.append(_f("BLANKET_EXCEEDS_REMAINING", "ERROR", "FAIL",
                          "Quantity exceeds remaining blanket PO balance: " + "; ".join(f"{m['item_code']} {m['quantity']} > {rem[m['item_code']]}" for m in over)))
        elif lm["mapped"]:
            out.append(_f("BLANKET_WITHIN_REMAINING", "INFO", "PASS",
                          "Blanket PO: " + "; ".join(f"{m['item_code']} {m['quantity']} of remaining {rem[m['item_code']]}" for m in lm["mapped"])))
    return out
