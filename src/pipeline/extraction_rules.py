"""Stage 4 (deterministic extractor) - label dictionary + table parser over native/OCR text.

Every value carries its evidence: raw text, page, bounding box, method and confidence.
Missing values are returned as null with a reason - nothing is invented.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from src.models import ExtractedInvoice, FieldEvidence, InvoiceLine, PageText, TextLine
from src.pipeline.normalization import (detect_currency, norm_account, normalize_text, parse_amount, parse_date)
from src.pipeline.understanding import WATERMARK_RE, Understanding


def L(*phrases: str) -> str:
    """Space-tolerant label alternation (OCR often drops spaces)."""
    alts = []
    for p in phrases:
        alts.append(r"\s*".join(re.escape(w) for w in p.split()))
    return r"(?:" + "|".join(alts) + r")"


ID = r"([A-Z0-9][A-Z0-9/\-_]*\d[A-Z0-9/\-_]*)"
DATE = (r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}\.?\s*[A-Za-zäÄ]{3,9}\.?,?\s*\d{4}"
        r"|[A-Za-z]{3,9}\.?\s*\d{1,2},?\s*\d{4})")
AMT = r"(?:\d{1,3}(?:[.,]\d{2,3})+(?:[.,]\d{2})?|\d+[.,]\d{2})(?![\d])"
AMT_C = r"((?:INR|EUR|USD|GBP|Rs\.?|€|\$)?\s*-?" + AMT + r"(?:\s*(?:EUR|INR|USD|€))?)"

LABELS = {
    "invoice_number": L("invoice number", "invoice no", "invoice #", "inv no", "bill no", "rechnungsnummer", "rechnungsnr", "rechnung nr"),
    "invoice_date": L("invoice date", "date of issue", "bill date", "rechnungsdatum") + r"|(?<![A-Za-z])(?<!due\s)(?<!order\s)(?:date|datum)(?![a-z])",
    "due_date": L("due date", "payment due", "zahlbar bis", "fällig am", "faellig am"),
    "po_number": L("po number", "po no", "po ref", "po #", "purchase order no", "purchase order", "your po", "ihre bestellung", "bestellnummer", "bestell nr"),
    "currency": L("currency", "währung", "waehrung"),
    "subtotal": L("subtotal", "sub total", "taxable value", "zwischensumme", "nettobetrag", "net amount"),
    "discount_total": L("discount", "rabatt"),
    "shipping_total": L("shipping", "freight charges", "delivery charges", "versandkosten"),
    "supplier_tax_id": L("gstin", "ust-idnr.", "ust-idnr", "ust-id", "vat no", "vat id", "tax id", "ein"),
    "supplier_bank_account": L("account no", "a/c no", "account number", "iban", "kontonummer"),
    "payment_terms": L("payment terms", "terms", "zahlungsbedingungen"),
}
GRAND_TOTAL_LABELS = [L("grand total"), L("total amount due", "amount payable", "total payable"), L("total due"),
                      L("gesamtbetrag", "rechnungsbetrag", "invoice total"), r"total\s*\(incl", r"(?<![a-z])(?<!sub\s)(?<!sub)total(?![a-z])"]
TAX_ROW = re.compile(r"(?<![a-z])(cgst|sgst|igst|utgst|gst|vat|mwst|ust|sales\s*tax|tax)(?![a-z])", re.I)
FREE_TEXT = {
    "service_period": L("service period", "billing period", "leistungszeitraum", "period"),
    "order_reference": L("order ref", "your ref", "reference", "ihr zeichen"),
}
REF_RES = [re.compile(r"(?:re-?issue\s+of|replaces|supersedes|in\s+lieu\s+of)\s+(?:invoice\s+)?(?:no\.?\s*)?([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", re.I),
           re.compile(r"credit\s*note\s*(?:no\.?\s*)?([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", re.I)]
TABLE_HEADER = (re.compile(r"description|particulars|bezeichnung|item", re.I), re.compile(r"qty|quantity|menge", re.I),
                re.compile(r"amount|gesamt|total", re.I))
TABLE_END = re.compile(r"subtotal|sub\s*total|zwischensumme|grand\s*total|gesamtbetrag|total|taxable|cgst|sgst|igst|mwst|amount\s*in\s*words", re.I)
ROW_RE = re.compile(r"^(?P<head>.*\S)\s+(?P<qty>\d+(?:[.,]\d+)?)\.?\s*(?P<uom>[A-Za-z]{2,6})?\.?\s+(?P<price>" + AMT + r")\s+(?P<amount>" + AMT + r")\s*$")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")


def _ev(field, value, raw, line: TextLine, notes=None) -> FieldEvidence:
    return FieldEvidence(field=field, value=value, raw_text=raw, source_page=line.page, bbox=line.bbox, method=line.method,
                         confidence=line.confidence, notes=notes or [])


def _rows(pages: list[PageText]) -> list[TextLine]:
    return [r for p in pages for r in p.rows if not WATERMARK_RE.search(r.text)]


def _segments(pages: list[PageText]) -> list[TextLine]:
    return [s for p in pages for s in p.lines if not WATERMARK_RE.search(s.text)]


def _find_labeled(rows, label_rx: str, value_rx: str, first=True):
    rx = re.compile(r"(?:" + label_rx + r")\s*[:#.]?\s*[:#]?\s*" + value_rx, re.I)
    hits = []
    for r in rows:
        t = normalize_text(r.text)
        m = rx.search(t)
        if m:
            hits.append((r, m))
            if first:
                break
    return hits


def extract(pages: list[PageText], und: Understanding) -> tuple[ExtractedInvoice, list[str]]:
    notes: list[str] = []
    loc = und.locale
    ns, order = loc.get("number_style"), loc.get("date_order")
    rows = _rows(pages)
    segs = _segments(pages)
    inv = ExtractedInvoice(doc_type=und.doc_type, locale=loc, injection_hits=und.injection_hits, extractor="rules")
    F = inv.fields

    # ---------------- supplier name: largest text in the top band of page 1 that is not a title
    p1 = [s for s in segs if s.page == 1 and s.bbox[1] < pages[0].height * 0.3]
    cands = [s for s in p1 if not re.search(r"\b(tax\s*)?invoice\b|rechnung|statement|credit\s*note|revised", s.text, re.I)]
    if cands:
        best = sorted(cands, key=lambda s: (-round(s.size, 0), s.bbox[1]))[0]
        F["supplier_name"] = _ev("supplier_name", normalize_text(best.text), best.text, best, ["largest letterhead text on page 1"])
    else:
        F["supplier_name"] = FieldEvidence(field="supplier_name", missing_reason="no letterhead text found")

    # ---------------- simple labelled IDs / dates
    hits = _find_labeled(rows, LABELS["invoice_number"], ID, first=False)
    vals = {re.sub(r"[^A-Z0-9]", "", m.group(1).upper()) for _, m in hits}
    if hits:
        r, m = hits[0]
        F["invoice_number"] = _ev("invoice_number", m.group(1).upper(), m.group(0), r)
        if len(vals) > 1:
            F["invoice_number"].notes.append(f"MULTIPLE invoice numbers printed: {sorted(vals)}")
    else:
        F["invoice_number"] = FieldEvidence(field="invoice_number", missing_reason="no invoice-number label found on any page")

    for fld in ("invoice_date", "due_date"):
        hits = _find_labeled(rows, LABELS[fld], DATE)
        if fld == "invoice_date":  # never take a due date as the invoice date
            hits = [h for h in _find_labeled(rows, LABELS[fld], DATE, first=False)
                    if not re.search(r"due|zahlbar|fällig|order", h[1].group(0), re.I)][:1]
        if hits:
            r, m = hits[0]
            p = parse_date(m.group(1), order)
            ev = _ev(fld, p.value, m.group(0), r, [p.note] if p.note else [])
            if p.ambiguous:
                ev.missing_reason = "AMBIGUOUS_DATE"
            F[fld] = ev
        else:
            F[fld] = FieldEvidence(field=fld, missing_reason="not printed")

    hits = _find_labeled(rows, LABELS["po_number"], ID)
    if hits:
        r, m = hits[0]
        F["po_number"] = _ev("po_number", m.group(1).upper(), m.group(0), r)
    else:
        implied = [(r, m) for r in rows for m in [re.search(r"\b(PO[-\s]?\d{3,})\b", normalize_text(r.text))] if m]
        if implied:
            r, m = implied[0]
            F["po_number"] = _ev("po_number", m.group(1).upper().replace(" ", "-"), m.group(0), r, ["PO pattern found without a label"])
        else:
            F["po_number"] = FieldEvidence(field="po_number", missing_reason="no PO reference printed")

    # ---------------- currency
    hits = _find_labeled(rows, LABELS["currency"], r"([A-Z]{3})")
    if hits:
        r, m = hits[0]
        F["currency"] = _ev("currency", m.group(1).upper(), m.group(0), r, ["currency label"])
    else:
        full = "\n".join(p.full_text for p in pages)
        c = detect_currency(WATERMARK_RE.sub("", full))
        src = next((r for r in rows if re.search(r"\b(INR|EUR|USD)\b|€|\$|Rs\.", r.text)), rows[0] if rows else None)
        if c.value and src:
            F["currency"] = _ev("currency", c.value, src.text, src, [c.note])
        else:
            F["currency"] = FieldEvidence(field="currency", missing_reason="AMBIGUOUS_CURRENCY" if c.ambiguous else "no currency evidence",
                                          notes=[c.note] if c.note else [])

    # ---------------- tax ID (first in reading order = supplier header) and bank account
    hits = _find_labeled(rows, LABELS["supplier_tax_id"], r"([A-Z0-9][A-Z0-9\-]{6,})")
    if hits:
        r, m = hits[0]
        F["supplier_tax_id"] = _ev("supplier_tax_id", m.group(1).upper(), m.group(0), r)
    hits = _find_labeled(rows, LABELS["supplier_bank_account"], r"([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?|\d{6,20})")
    if hits:
        r, m = hits[0]
        F["supplier_bank_account"] = _ev("supplier_bank_account", norm_account(m.group(1)), m.group(0), r, ["sensitive - masked in UI"])

    # ---------------- money: subtotal / discount / shipping / tax / grand total
    def amount_from_row(r: TextLine, after: int = 0):
        t = normalize_text(r.text)[after:]
        ms = list(re.finditer(AMT_C, t))
        if not ms:
            return None, None
        raw = ms[-1].group(1)
        return parse_amount(raw, ns), raw

    used_rows: set[int] = set()

    def labelled_amount(fld, label_rx):
        rx = re.compile(label_rx, re.I)
        for r in rows:
            t = normalize_text(r.text)
            m = rx.search(t)
            if m:
                p, raw = amount_from_row(r, m.end())
                if p is not None:
                    used_rows.add(id(r))
                    ev = _ev(fld, p.value, t, r, [p.note] if p.note else [])
                    if p.ambiguous:
                        ev.missing_reason = "AMBIGUOUS_NUMBER"
                    return ev
        return None

    gt = None
    for lab in GRAND_TOTAL_LABELS:
        gt = labelled_amount("grand_total", lab)
        if gt:
            break
    F["grand_total"] = gt or FieldEvidence(field="grand_total", missing_reason="no total label with an amount found")
    for fld in ("subtotal", "discount_total", "shipping_total"):
        ev = labelled_amount(fld, LABELS[fld])
        if ev:
            F[fld] = ev

    tax_sum, tax_raw, tax_rows, rate_pct = Decimal("0"), [], [], Decimal("0")
    for r in rows:
        if id(r) in used_rows:
            continue
        t = normalize_text(r.text)
        if re.search(r"gstin|ust-?id|vat\s*(no|id)|tax\s*id|\bein\b", t, re.I) or not TAX_ROW.search(t):
            continue
        p, raw = amount_from_row(r)
        if p is None or p.value is None:
            continue
        if re.search(r"included|inclusive|incl\.?\s*in", t, re.I):
            inv.tax_inclusive = True
        tax_sum += Decimal(p.value)
        tax_raw.append(t)
        tax_rows.append(r)
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", t)
        if m:
            rate_pct += Decimal(m.group(1).replace(",", "."))
    if tax_rows:
        F["tax_total"] = FieldEvidence(field="tax_total", value=str(tax_sum.quantize(Decimal("0.01"))), raw_text=" | ".join(tax_raw),
                                       source_page=tax_rows[0].page, bbox=tax_rows[0].bbox, method=tax_rows[0].method,
                                       confidence=min([x.confidence for x in tax_rows if x.confidence is not None], default=None),
                                       notes=[f"sum of {len(tax_rows)} tax line(s)"] + (["tax included in prices"] if inv.tax_inclusive else []))
        inv.tax_rate_printed = str(rate_pct) if rate_pct else None
    else:
        F["tax_total"] = FieldEvidence(field="tax_total", missing_reason="no tax line printed")
    if any(re.search(r"inclusive\s*of\s*(gst|vat|tax)|prices?\s*include", normalize_text(r.text), re.I) for r in rows):
        inv.tax_inclusive = True

    # ---------------- free text
    for fld, lab in FREE_TEXT.items():
        rx = re.compile(r"^\s*(?:" + lab + r")\s*[:#]?\s*(.+)$", re.I)
        for s in segs:
            m = rx.search(normalize_text(s.text))
            if m:
                if fld == "service_period":
                    inv.service_period = m.group(1).strip()
                else:
                    inv.order_reference_text = m.group(1).strip()
                break
    hits = [(s, m) for s in segs for m in [re.search(r"^\s*(?:" + LABELS["payment_terms"] + r")\s*[:]?\s*(.+)$", normalize_text(s.text), re.I)] if m]
    if hits:
        s, m = hits[0]
        F["payment_terms"] = _ev("payment_terms", m.group(1).strip(), m.group(0), s)
    full_norm = normalize_text(" ".join(r.text for r in rows))
    for rx in REF_RES:
        for m in rx.finditer(full_norm):
            ref = m.group(1).rstrip(".,;").upper()
            if ref not in inv.references:
                inv.references.append(ref)

    # ---------------- line items
    inv.lines = _parse_lines(pages, ns, notes)
    return inv, notes


def _parse_lines(pages: list[PageText], ns, notes) -> list[InvoiceLine]:
    out: list[InvoiceLine] = []
    for p in pages:
        rows = [r for r in p.rows if not WATERMARK_RE.search(r.text)]
        start = None
        for i, r in enumerate(rows):
            t = r.text
            if all(rx.search(t) for rx in TABLE_HEADER):
                start = i
                break
        if start is None:
            continue
        for r in rows[start + 1:]:
            t = normalize_text(r.text)
            m = ROW_RE.match(t)
            if not m:
                if TABLE_END.search(t):
                    break
                if out and out[-1].source_page == p.page and not re.search(r"\d[.,]\d{2}", t):
                    out[-1].description = (out[-1].description + " " + t).strip()   # wrapped description
                    out[-1].raw_text += " / " + t
                continue
            head = re.sub(r"^\d{1,3}\s+(?=[A-Z])", "", m.group("head"))
            toks = head.split()
            code = toks[0] if toks and CODE_RE.match(toks[0]) else None
            desc = " ".join(toks[1:]) if code else head
            q, pr, am = parse_amount(m.group("qty"), "dot_decimal" if "." in m.group("qty") and ns != "comma_decimal" else ns), \
                parse_amount(m.group("price"), ns), parse_amount(m.group("amount"), ns)
            qty = q.value
            if qty is not None:
                qd = Decimal(qty)
                qty = str(qd.normalize()) if qd == qd.to_integral() else str(qd)
                if qd == qd.to_integral():
                    qty = str(int(qd))
            out.append(InvoiceLine(line_no=len(out) + 1, description=desc, item_code=code, quantity=qty, uom=m.group("uom"),
                                   unit_price=pr.value, line_total=am.value, raw_text=t, source_page=p.page, bbox=r.bbox,
                                   method=r.method))
    if not out:
        notes.append("no line-item table recognised")
    return out
