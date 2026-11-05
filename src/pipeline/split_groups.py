"""All-or-none PO-group reconciliation for ONE_TIME purchase orders.

Every non-rejected invoice that references the same exact ONE_TIME PO joins one open reconciliation
group. The group is accepted only when ALL members together match every PO line (quantity, unit
price), the aggregate subtotal, tax and grand total. Otherwise no member is accepted.
A single invoice that bills the whole PO is simply a complete group of one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.master_data import policy, tol
from src.models import PurchaseOrder
from src.pipeline.matching import consumed

Q = Decimal("0.01")


def reconcile(po: PurchaseOrder, members: list[dict], group_id: str) -> dict:
    """members: [{document_id, file_name, invoice_number, own_blocking, own_reasons, po_method, line_mapping, calc}]"""
    t = tol("po_group_abs")
    prev = consumed(po)
    discrepancies, still_needed = [], []
    lines = []
    by_code = {l.item_code: l for l in po.lines}
    agg_qty = {c: Decimal("0") for c in by_code}
    agg_val = {c: Decimal("0") for c in by_code}
    prices = {c: set() for c in by_code}
    by_inv = {c: {} for c in by_code}
    unexpected = []
    bundled = [m for m in members if (m.get("line_mapping") or {}).get("mode") == "BUNDLED"]
    for m in members:
        lm = m.get("line_mapping") or {"mode": "LINES", "mapped": [], "unmapped": []}
        for x in lm.get("mapped", []):
            c = x["item_code"]
            q = Decimal(x["quantity"] or "0")
            agg_qty[c] += q
            agg_val[c] += Decimal(x["net_line_total"] or "0")
            if x["unit_price"] is not None:
                prices[c].add(x["unit_price"])
            by_inv[c][m["invoice_number"] or m["file_name"]] = by_inv[c].get(m["invoice_number"] or m["file_name"], Decimal("0")) + q
        for u in lm.get("unmapped", []):
            unexpected.append({**u, "invoice": m["invoice_number"] or m["file_name"]})

    for c, pl in by_code.items():
        expected = pl.quantity - prev.get(c, Decimal("0"))
        inv_q = agg_qty[c]
        bad_prices = sorted(p for p in prices[c] if abs(Decimal(p) - pl.unit_price) > t)
        if bundled:
            status = "BUNDLED"
        elif bad_prices:
            status = "PRICE_MISMATCH"
            discrepancies.append(f"{c}: unit price {', '.join(bad_prices)} != PO {pl.unit_price}")
        elif inv_q > expected:
            status = "OVER"
            discrepancies.append(f"{c}: invoiced {inv_q} > expected {expected} {pl.uom}")
        elif inv_q < expected:
            status = "UNDER"
            still_needed.append(f"{c}: {expected - inv_q} {pl.uom} not yet invoiced (expected {expected}, invoiced {inv_q})")
        else:
            status = "MATCH"
        lines.append({"item_code": c, "description": pl.description, "uom": pl.uom, "ordered_qty": str(pl.quantity),
                      "previously_accepted_qty": str(prev.get(c, Decimal("0"))), "expected_qty": str(expected), "invoiced_qty": str(inv_q),
                      "po_unit_price": str(pl.unit_price), "invoice_unit_prices": sorted(prices[c]),
                      "expected_value": str((expected * pl.unit_price).quantize(Q)), "invoiced_value": str(agg_val[c].quantize(Q)),
                      "status": status, "by_invoice": {k: str(v) for k, v in by_inv[c].items()}})
    for u in unexpected:
        discrepancies.append(f"Unexpected item on {u['invoice']}: '{u['description']}' ({u.get('item_code') or 'no code'}) is not on {po.po_number}")

    exp_sub = sum((Decimal(l["expected_value"]) for l in lines), Decimal("0")).quantize(Q)
    exp_tax = (exp_sub * po.tax_rate).quantize(Q)
    inv_sub = sum((Decimal(m["calc"]["net_subtotal"] or "0") for m in members), Decimal("0")).quantize(Q)
    inv_tax = sum((Decimal(m["calc"]["tax_total"] or "0") for m in members), Decimal("0")).quantize(Q)
    inv_gt = sum((Decimal(m["calc"]["grand_total"] or "0") for m in members), Decimal("0")).quantize(Q)
    totals = {"expected_subtotal": str(exp_sub), "invoiced_subtotal": str(inv_sub), "expected_tax": str(exp_tax),
              "invoiced_tax": str(inv_tax), "expected_grand_total": str(exp_sub + exp_tax), "invoiced_grand_total": str(inv_gt),
              "currency": po.currency}
    tax_tol = t * max(1, len(members))
    if inv_sub > exp_sub + t:
        discrepancies.append(f"Aggregate subtotal {inv_sub} exceeds expected {exp_sub}")
    if not discrepancies and not still_needed:
        if abs(inv_sub - exp_sub) > t:
            discrepancies.append(f"Aggregate subtotal {inv_sub} != expected {exp_sub}")
        if abs(inv_tax - exp_tax) > tax_tol:
            discrepancies.append(f"Aggregate tax {inv_tax} != expected {exp_tax} ({po.tax_rate * 100:.0f}% of {exp_sub})")
        if abs(inv_gt - (exp_sub + exp_tax)) > tax_tol:
            discrepancies.append(f"Aggregate grand total {inv_gt} != expected {exp_sub + exp_tax}")
    elif still_needed and not discrepancies and inv_sub < exp_sub:
        still_needed.append(f"Amount still to be invoiced: {po.currency} {(exp_sub - inv_sub).quantize(Q)} before tax")

    inferred = [m for m in members if m.get("po_method") == "inferred"]
    if inferred and len(members) > 1:
        discrepancies.append("An invoice with an inferred (not printed) PO reference cannot join a split group: "
                             + ", ".join(m["file_name"] for m in inferred))
    blocking = [m for m in members if m.get("own_blocking")]
    if blocking:
        status = "BLOCKED"
    elif discrepancies:
        status = "DISCREPANCY"
    elif still_needed:
        status = "INCOMPLETE"
    else:
        status = "ACCEPTED"
    return {"group_id": group_id, "po_number": po.po_number, "vendor_id": po.vendor_id, "billing_type": po.billing_type,
            "status": status, "members": [m["document_id"] for m in members],
            "member_details": [{"document_id": m["document_id"], "file_name": m["file_name"], "invoice_number": m["invoice_number"],
                                "own_blocking": bool(m.get("own_blocking")), "own_reasons": m.get("own_reasons", []),
                                "grand_total": m["calc"]["grand_total"], "net_subtotal": m["calc"]["net_subtotal"],
                                "po_method": m.get("po_method")} for m in members],
            "blocking_members": [m["file_name"] for m in blocking],
            "lines": lines, "totals": totals, "discrepancies": discrepancies, "still_needed": still_needed,
            "tolerance": str(t), "policy_version": policy()["policy_version"],
            "reconciled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def consumption_rows(group: dict, members: list[dict]) -> list[dict]:
    """One ledger entry per invoice per PO line, created only for an ACCEPTED group (idempotent keys)."""
    out = []
    for m in members:
        lm = m.get("line_mapping") or {}
        if lm.get("mode") == "BUNDLED":
            for code in lm["bundled"]["po_lines"]:
                pl = next(l for l in group["lines"] if l["item_code"] == code)
                out.append({"po_number": group["po_number"], "item_code": code, "quantity": pl["expected_qty"],
                            "amount": pl["expected_value"], "group_id": group["group_id"], "document_id": m["document_id"],
                            "policy_version": group["policy_version"], "idempotency_key": f"{group['group_id']}:{m['document_id']}:{code}"})
            continue
        for x in lm.get("mapped", []):
            out.append({"po_number": group["po_number"], "item_code": x["item_code"], "quantity": x["quantity"],
                        "amount": x["net_line_total"], "group_id": group["group_id"], "document_id": m["document_id"],
                        "policy_version": group["policy_version"], "idempotency_key": f"{group['group_id']}:{m['document_id']}:{x['item_code']}"})
    return out
