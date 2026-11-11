"""Tax reconciliation against the PO - item by item.

Every invoiced item is mapped to a PO line, and each PO line carries its own tax rate (GST depends on the item's
HSN/SAC code, VAT on the product category). So the tax an invoice SHOULD carry is known per item:

    expected tax of an item = its net amount x its PO line rate

The invoice is then checked in whichever form it states its tax:

1. tax printed per item        -> each item's rate = its PO line rate, each item's tax = net x rate
2. tax summarised with rates   -> the printed tax lines (CGST 2.5%, SGST 9%, cess, IGST ...) are ASSIGNED to the
                                  items' rate slabs: the lines in a slab must add up to its rate and to its expected
                                  tax. Assignment is driven by the items, so two slabs with the same taxable amount
                                  are never confused, and nothing assumes "half each".
3. only a total tax amount     -> total = sum over items of net x rate
4. no tax stated at all        -> invoice total vs the PO value INCLUDING tax for the invoiced items:
                                  equal -> tax treated as included, passed with a visible warning;
                                  equal to the PO value BEFORE tax -> tax not charged (held);
                                  neither -> held.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional

from src.master_data import tol
from src.models import ExtractedInvoice, PurchaseOrder, ValidationFinding

Q = Decimal("0.01")
EPS = Decimal("0.0001")


def _f(rule, sev, status, msg) -> ValidationFinding:
    return ValidationFinding(rule_id=rule, stage="vendor_po", severity=sev, status=status, message=msg)


def pct(r: Decimal) -> str:
    s = f"{(r * 100).normalize():f}"
    return (s.rstrip("0").rstrip(".") if "." in s else s) + "%"


def _d(x) -> Optional[Decimal]:
    return Decimal(str(x)) if x not in (None, "") else None


# ------------------------------------------------------------------ what each item should carry
def item_rows(inv: ExtractedInvoice, po: PurchaseOrder, lm: Optional[dict], net_total: Optional[Decimal] = None) -> list[dict]:
    """One row per invoiced item: its PO line rate, its net amount on the invoice and its net value at PO prices."""
    lm = lm or {}
    if lm.get("mode") == "BUNDLED":
        # one bundled line for the whole PO: each PO line's share of the invoice's net amount
        net = net_total if net_total is not None else po.subtotal
        return [{"label": pl.item_code, "rate": po.line_rate(pl), "line": None, "po_net": pl.line_total,
                 "net": (net * pl.line_total / po.subtotal).quantize(Q) if po.subtotal else pl.line_total} for pl in po.lines]
    lines = {l.line_no: l for l in inv.lines}
    rows = []
    for m in lm.get("mapped", []):
        pl = next(l for l in po.lines if l.item_code == m["item_code"])
        po_net = (Decimal(m["quantity"]) * pl.unit_price).quantize(Q) if m.get("quantity") else None
        net = _d(m.get("net_line_total"))
        rows.append({"label": m["item_code"], "rate": po.line_rate(pl), "line": lines.get(m["line_no"]),
                     "net": net if net is not None else po_net, "po_net": po_net})
    return rows


def by_rate(rows: list[dict], key: str = "net") -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for x in rows:
        if x[key] is not None:
            out[x["rate"]] = out.get(x["rate"], Decimal("0")) + x[key]
    return out


def _basis(bases: dict[Decimal, Decimal]) -> str:
    return " + ".join(f"{pct(r)} of {b.quantize(Q)}" for r, b in sorted(bases.items()))


# ------------------------------------------------------------------ summary tax lines -> rate slabs
def assign_components(components: list[dict], expected: dict[Decimal, Decimal], tol_amt: Decimal) -> Optional[dict]:
    """Assign each printed tax line to one rate slab so that, per slab, the rates add up to the slab rate and the amounts
    add up to the slab's expected tax. Returns {rate: [component, ...]} or None if no consistent assignment exists."""
    comps = []
    for c in components:
        r, a = _d(c.get("rate")), _d(c.get("amount"))
        if r is not None and r > 0:
            comps.append({**c, "_r": r / 100, "_a": a})
    slabs = sorted(r for r in expected if r > 0)
    if not comps:   # only 0% lines printed (exempt / zero-rated / export): fine only if no item should carry tax
        return {r: [] for r in slabs} if all(expected[r] <= tol_amt for r in slabs) else None
    if len(comps) > 12:
        return None
    acc: dict[Decimal, list] = {r: [] for r in slabs}

    def complete() -> bool:
        for r in slabs:
            parts = acc[r]
            if not parts:
                if expected[r] > tol_amt:
                    return False
                continue
            if abs(sum(p["_r"] for p in parts) - r) > EPS:
                return False
            if all(p["_a"] is not None for p in parts) and abs(sum(p["_a"] for p in parts) - expected[r]) > tol_amt:
                return False
        return True

    def rec(i: int) -> bool:
        if i == len(comps):
            return complete()
        for r in slabs:
            if sum(p["_r"] for p in acc[r]) + comps[i]["_r"] > r + EPS:
                continue
            acc[r].append(comps[i])
            if rec(i + 1):
                return True
            acc[r].pop()
        return False

    return {r: list(v) for r, v in acc.items()} if rec(0) else None


def _comp_text(c: dict) -> str:
    return f"{c.get('label') or 'Tax'} {pct(_d(c['rate']) / 100)}" + (f" {_d(c['amount']).quantize(Q)}" if _d(c.get("amount")) is not None else "")


# ------------------------------------------------------------------ checks
def check(inv: ExtractedInvoice, po: PurchaseOrder, lm: Optional[dict], calc: dict) -> list[ValidationFinding]:
    """Tax checks for an invoice whose tax is printed (per item, as summary lines, or as a total)."""
    if calc.get("tax_derived") or calc.get("tax_not_charged"):
        return []                                   # no tax printed: handled by resolve_missing_tax
    rows = item_rows(inv, po, lm, _d(calc.get("net_subtotal")))
    if not rows:
        return []                                   # nothing maps to the PO (flagged as unexpected items elsewhere)
    t = tol("tax_abs")
    bases = by_rate(rows)
    expected = {r: (b * r).quantize(Q) for r, b in bases.items()}
    exp_total = sum(expected.values(), Decimal("0"))
    out: list[ValidationFinding] = []

    # 1. tax printed per item
    per_item = [x for x in rows if x["line"] is not None and (x["line"].tax_rate or x["line"].tax_amount)]
    if per_item:
        rate_bad, amt_bad, ok = [], [], []
        for x in per_item:
            ln, r = x["line"], x["rate"]
            exp_l = (x["net"] * r).quantize(Q) if x["net"] is not None else None
            pr, pa = (_d(ln.tax_rate) / 100 if ln.tax_rate else None), _d(ln.tax_amount)
            if pr is not None and abs(pr - r) > EPS:
                rate_bad.append(f"{x['label']} charged {pct(pr)}, PO line rate {pct(r)}")
            elif pa is not None and exp_l is not None and abs(pa - exp_l) > t:
                amt_bad.append(f"{x['label']} tax {pa} != {pct(r)} of {x['net']} = {exp_l}")
            else:
                ok.append(f"{x['label']} {pct(r)} = {pa if pa is not None else exp_l}")
        if rate_bad:
            out.append(_f("TAX_RATE_MISMATCH", "ERROR", "FAIL", "Tax rate per item differs from the PO line rate: " + "; ".join(rate_bad) + "."))
        if amt_bad:
            out.append(_f("TAX_RATE_CHECK", "ERROR", "FAIL", "Tax amount per item is wrong: " + "; ".join(amt_bad) + "."))
        if not rate_bad and not amt_bad:
            out.append(_f("TAX_LINE_CHECK", "INFO", "PASS", f"Tax per item matches the PO line rates: {'; '.join(ok)}."))
        printed_sum = sum((_d(x["line"].tax_amount) for x in per_item if x["line"].tax_amount), Decimal("0"))
        if inv.dec("tax_total") is not None and all(x["line"].tax_amount for x in per_item) and abs(printed_sum - inv.dec("tax_total")) > t:
            out.append(_f("TAX_TOTAL_CONSISTENCY", "ERROR", "FAIL",
                          f"Per-item taxes add up to {printed_sum.quantize(Q)} but the invoice states total tax {inv.dec('tax_total')}."))

    # 2. tax summarised with rates (CGST/SGST/IGST/cess/VAT lines)
    elif inv.tax_components:
        asg = assign_components(inv.tax_components, expected, t)
        if asg is not None and not any(asg.values()):
            out.append(_f("TAX_RATE_PRINTED", "INFO", "PASS",
                          f"Printed tax {', '.join(_comp_text(c) for c in inv.tax_components)} matches the items' PO line rate(s) "
                          f"({' / '.join(pct(r) for r in sorted(bases))})."))
        elif asg:
            parts = []
            for r in sorted(asg):
                items = [x["label"] for x in rows if x["rate"] == r]
                comps = asg[r]
                if comps:
                    parts.append(f"{' + '.join(_comp_text(c) for c in comps)}"
                                 + (f" = {pct(r)}" if len(comps) > 1 else "") + f" on {', '.join(items)} ({bases[r].quantize(Q)})")
            out.append(_f("TAX_RATE_PRINTED", "INFO", "PASS", "Printed tax matches the items' PO line rates: " + "; ".join(parts) + "."))
        else:
            out.append(_f("TAX_RATE_MISMATCH", "ERROR", "FAIL",
                          f"Printed tax lines ({', '.join(_comp_text(c) for c in inv.tax_components)}) do not match the tax the items should "
                          f"carry at their PO line rates: " + "; ".join(f"{pct(r)} of {bases[r].quantize(Q)} = {expected[r]}" for r in sorted(expected)) + "."))
    elif inv.tax_rate_printed and not po.mixed_rates:
        printed = Decimal(inv.tax_rate_printed) / 100
        if abs(printed - po.rates[0]) > EPS:
            out.append(_f("TAX_RATE_MISMATCH", "ERROR", "FAIL", f"Printed tax rate {pct(printed)} != PO tax rate {pct(po.rates[0])}."))

    # 3. total tax amount (always, when printed)
    printed_total = inv.dec("tax_total")
    if printed_total is not None:
        ok = abs(printed_total - exp_total) <= t
        how = "" if (per_item or inv.tax_components) else " (no tax rate printed - expected tax worked out per item from the PO line rates)"
        # if per-item tax amounts were already reported as TAX_RATE_CHECK, report the total under its own name
        rule = "TAX_TOTAL_CHECK" if any(f.rule_id == "TAX_RATE_CHECK" for f in out) else "TAX_RATE_CHECK"
        out.append(_f(rule, "ERROR" if not ok else "INFO", "FAIL" if not ok else "PASS",
                      f"Tax {printed_total} {'=' if ok else '!='} expected {_basis(bases)} = {exp_total}{how}."))
    return out


def resolve_missing_tax(inv: ExtractedInvoice, po: PurchaseOrder, lm: dict, calc: dict,
                        remap: Callable[[], dict]) -> tuple[dict, list[ValidationFinding]]:
    """No tax amount anywhere on the invoice: decide from the PO whether tax is embedded in the prices or not charged."""
    gt = inv.dec("grand_total")
    rows = item_rows(inv, po, lm)
    if gt is None or not rows:
        return lm, []
    po_bases = by_rate(rows, "po_net")
    exp_net = sum(po_bases.values(), Decimal("0")).quantize(Q)
    exp_tax = sum(((b * r).quantize(Q) for r, b in po_bases.items()), Decimal("0"))
    exp_gross = exp_net + exp_tax
    basis = _basis(po_bases)
    t = max(tol("tax_abs"), tol("invoice_arithmetic_abs"))
    if exp_tax == 0:
        return lm, [_f("TAX_RATE_CHECK", "INFO", "PASS", "No tax printed and none expected: the PO lines for these items carry 0% tax.")]

    embedded, not_charged = abs(gt - exp_gross) <= t, abs(gt - exp_net) <= t
    if embedded or (inv.tax_inclusive and not not_charged):
        inv.tax_inclusive = True
        if lm.get("mode") == "LINES":
            lines = {l.line_no: l for l in inv.lines}
            for m in lm["mapped"]:                    # back out each item's tax at ITS PO line rate
                ln, r = lines[m["line_no"]], po.rate_for(m["item_code"])
                if ln.unit_price:
                    ln.net_unit_price = str((Decimal(ln.unit_price) / (1 + r)).quantize(Q))
                if ln.line_total:
                    ln.net_line_total = str((Decimal(ln.line_total) / (1 + r)).quantize(Q))
            lm = remap()
            net = sum((Decimal(m["net_line_total"]) for m in lm["mapped"] if m["net_line_total"]), Decimal("0")).quantize(Q)
        else:
            net = (gt / (1 + po.effective_rate)).quantize(Q)
        tax = (gt - net).quantize(Q)
        calc.update(net_subtotal=str(net), net_lines_sum=str(net), tax_total=str(tax), tax_derived=True, tax_inclusive=True,
                    effective_tax_rate=str((tax / net).quantize(Decimal("0.0001"))) if net else None)
        findings = [_f("TAX_NOT_STATED", "WARN", "WARN",
                       f"Warning: the invoice does not state any tax. Its total {gt} is treated as including tax "
                       f"(net {net} + tax {tax}). Ask the vendor for a tax invoice that shows the tax - it is needed to claim input tax credit.")]
        if embedded:
            findings.append(_f("TAX_EMBEDDED_TOTAL_MATCH", "INFO", "PASS",
                               f"Invoice total {gt} = PO value incl. tax for the invoiced items: net {exp_net} + tax {exp_tax} ({basis}) = {exp_gross}."))
        else:
            findings.append(_f("TAX_TOTAL_VS_PO", "ERROR", "FAIL",
                               f"Prices are stated as tax-inclusive, but the total {gt} != PO value incl. tax {exp_gross} for the invoiced items "
                               f"(net {exp_net} + tax {exp_tax}: {basis})."))
        return lm, findings
    if not_charged:
        calc.update(tax_total="0.00", tax_not_charged=True)
        return lm, [_f("TAX_NOT_CHARGED", "ERROR", "FAIL",
                       f"No tax on the invoice, and its total {gt} equals the PO value BEFORE tax ({exp_net}): tax was not charged although "
                       f"the PO lines carry tax. Expected tax {exp_tax} ({basis}), total {exp_gross}.")]
    return lm, [_f("TAX_NOT_STATED", "WARN", "WARN", "Warning: the invoice does not state any tax."),
                _f("TAX_TOTAL_VS_PO", "ERROR", "FAIL",
                   f"No tax is printed and the total {gt} matches neither the PO value before tax ({exp_net}) nor including tax "
                   f"({exp_gross}: {basis}) for the invoiced items.")]
