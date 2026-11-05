"""Stage 3 - document understanding: type, scope, locale, injection-like text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.models import PageText
from src.pipeline.normalization import detect_locale, normalize_text

WATERMARK_RE = re.compile(r"SYNTHETIC\s*DEMO\s*DATA", re.I)

_TYPES = [
    ("credit_note", re.compile(r"\b(credit\s*note|gutschrift)\b", re.I)),
    ("statement", re.compile(r"\b(statement\s*of\s*account|account\s*statement|kontoauszug)\b", re.I)),
    ("purchase_order", re.compile(r"^\s*purchase\s*order\s*$", re.I)),
    ("delivery_note", re.compile(r"\b(delivery\s*(note|challan)|lieferschein)\b", re.I)),
    ("invoice", re.compile(r"\b(tax\s*invoice|invoice|rechnung|bill\s*of\s*supply|facture)\b", re.I)),
]

INJECTION_PATTERNS = [
    re.compile(r"\b(ignore|disregard|bypass|skip)\b.{0,40}\b(instructions?|rules?|validations?|checks?|controls?|policy)\b", re.I),
    re.compile(r"\b(note|message|instructions?)\s+(to|for)\s+(the\s+)?(ai|llm|assistant|model|system|bot)\b", re.I),
    re.compile(r"\bmark\b.{0,30}\b(approved|auto[_\s-]?approve)\b", re.I),
    re.compile(r"\bAUTO[_\s-]?APPROVE\b"),
    re.compile(r"\byou\s+are\s+(an?|the)\s+(ai|assistant|model)\b", re.I),
]


@dataclass
class Understanding:
    doc_type: str
    in_scope: bool
    title: str | None
    locale: dict
    injection_hits: list[str] = field(default_factory=list)
    page_roles: dict[int, str] = field(default_factory=dict)
    reason: str = ""


def _title_lines(page: PageText) -> list[str]:
    top = [l for l in page.lines if l.bbox[1] < page.height * 0.30 and not WATERMARK_RE.search(l.text)]
    if not top:
        return []
    max_size = max(l.size for l in top)
    return [l.text for l in top if l.size >= max_size * 0.6]


def understand(pages: list[PageText]) -> Understanding:
    full = "\n".join(p.full_text for p in pages)
    loc = detect_locale(full).as_dict()
    titles = _title_lines(pages[0]) if pages else []
    doc_type = "unknown"
    for t, rx in _TYPES:  # title first
        if any(rx.search(normalize_text(x)) for x in titles):
            doc_type = t
            break
    if doc_type == "unknown":
        if re.search(r"invoice\s*(no|number|#|date)|rechnungs\s*(nummer|datum)", full, re.I):
            doc_type = "invoice"
        else:
            for t, rx in _TYPES:
                if t != "purchase_order" and rx.search(full):
                    doc_type = t
                    break
    roles = {}
    for p in pages:
        txt = p.full_text.lower()
        if re.search(r"terms\s*and\s*conditions|bank\s*details|bankverbindung", txt) and not re.search(r"\b(qty|menge|quantity)\b", txt):
            roles[p.page] = "payment details / terms"
        elif re.search(r"\b(qty|menge|quantity)\b", txt):
            roles[p.page] = "header + line items + totals"
        else:
            roles[p.page] = "other"
    hits = []
    for p in pages:
        for line in p.rows:
            t = normalize_text(line.text)
            if any(rx.search(t) for rx in INJECTION_PATTERNS):
                hits.append(f"p{p.page}: \"{t[:140]}\"")
    in_scope = doc_type == "invoice"
    reason = {"invoice": "Document is a vendor invoice.",
              "credit_note": "Document is a credit note - route to the credit-note process, not invoice approval.",
              "statement": "Document is a statement of account, not an invoice - nothing to approve.",
              "purchase_order": "Document is a purchase order, not an invoice.",
              "delivery_note": "Document is a delivery note, not an invoice.",
              "unknown": "Could not identify an invoice in this document."}[doc_type]
    return Understanding(doc_type=doc_type, in_scope=in_scope, title=titles[0] if titles else None, locale=loc,
                         injection_hits=hits, page_roles=roles, reason=reason)
