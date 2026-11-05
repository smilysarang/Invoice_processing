"""Typed data contracts (schema version 1.0.0).

Money is carried as Decimal internally and serialised as strings, never floats.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"

Severity = Literal["ERROR", "WARN", "INFO"]
CheckStatus = Literal["PASS", "FAIL", "WARN", "INFO"]
Decision = Literal[
    "AUTO_APPROVE",
    "HOLD_FOR_REVIEW",
    "PENDING_SPLIT_RECONCILIATION",
    "REJECT_DUPLICATE",
    "REJECT_INVALID",
]
Method = Literal["native_text", "ocr", "llm", "human"]

CRITICAL_FIELDS = {"invoice_number", "invoice_date", "po_number", "grand_total", "currency", "supplier_bank_account"}


class TextLine(BaseModel):
    page: int
    text: str
    bbox: list[float]
    method: Method
    confidence: Optional[float] = None
    size: float = 0.0


class PageText(BaseModel):
    page: int
    method: Method
    lines: list[TextLine]          # gap-split segments (header fields, names)
    rows: list[TextLine] = Field(default_factory=list)  # full y-lines (table rows)
    full_text: str
    quality: float
    ocr_confidence: Optional[float] = None
    width: float
    height: float
    note: Optional[str] = None


class FieldEvidence(BaseModel):
    field: str
    value: Optional[str] = None          # normalised value (ISO date, Decimal string, code...)
    raw_text: Optional[str] = None       # exactly as printed / read
    source_page: Optional[int] = None
    bbox: Optional[list[float]] = None
    method: Optional[Method] = None
    confidence: Optional[float] = None
    grounded: Optional[bool] = None
    grounding_score: Optional[float] = None
    missing_reason: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class InvoiceLine(BaseModel):
    line_no: int
    description: str
    item_code: Optional[str] = None
    quantity: Optional[str] = None
    uom: Optional[str] = None
    unit_price: Optional[str] = None      # as printed (normalised Decimal string)
    line_total: Optional[str] = None
    net_unit_price: Optional[str] = None  # tax-exclusive unit price used for PO comparison
    net_line_total: Optional[str] = None
    raw_text: str = ""
    source_page: Optional[int] = None
    bbox: Optional[list[float]] = None
    method: Optional[Method] = None


class ExtractedInvoice(BaseModel):
    doc_type: str = "unknown"
    fields: dict[str, FieldEvidence] = Field(default_factory=dict)
    lines: list[InvoiceLine] = Field(default_factory=list)
    tax_inclusive: bool = False
    tax_rate_printed: Optional[str] = None
    locale: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)      # other doc numbers referenced (reissue/credit note)
    service_period: Optional[str] = None
    order_reference_text: Optional[str] = None
    injection_hits: list[str] = Field(default_factory=list)
    extractor: str = "rules"

    def val(self, name: str) -> Optional[str]:
        f = self.fields.get(name)
        return f.value if f else None

    def dec(self, name: str) -> Optional[Decimal]:
        v = self.val(name)
        return Decimal(v) if v not in (None, "") else None


class ValidationFinding(BaseModel):
    rule_id: str
    stage: str
    severity: Severity
    status: CheckStatus
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    evidence: Optional[str] = None


class Vendor(BaseModel):
    vendor_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    status: str
    approved: bool
    tax_id: Optional[str] = None
    country: Optional[str] = None
    allowed_currencies: list[str] = Field(default_factory=list)
    bank_account: Optional[str] = None
    contact_ref: Optional[str] = None


class POLine(BaseModel):
    po_number: str
    line_no: int
    item_code: str
    description: str
    uom: str
    quantity: Decimal
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    currency: str
    status: str
    po_date: str
    tax_rate: Decimal
    billing_type: Literal["ONE_TIME", "BLANKET"]
    description: str
    allow_bundled_invoice: bool = False
    lines: list[POLine] = Field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((l.line_total for l in self.lines), Decimal("0")).quantize(Decimal("0.01"))

    @property
    def tax_total(self) -> Decimal:
        return (self.subtotal * self.tax_rate).quantize(Decimal("0.01"))

    @property
    def grand_total(self) -> Decimal:
        return self.subtotal + self.tax_total


class AgentTrace(BaseModel):
    agent: str
    mode: Literal["live_llm", "deterministic_demo"]
    status: Literal["completed", "error", "skipped"]
    provider: Optional[str] = None
    model: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    latency_ms: Optional[int] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    request_id: Optional[str] = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    input_summary: Optional[str] = None
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    retries: int = 0


class DecisionResult(BaseModel):
    decision: Decision
    reason_codes: list[str]
    warnings: list[str] = Field(default_factory=list)
    next_action: str
    policy_version: str
    selected_vendor: Optional[str] = None
    selected_po: Optional[str] = None
    group_id: Optional[str] = None
    agent_conflicts: list[str] = Field(default_factory=list)


class StageEvent(BaseModel):
    document_id: Optional[str]
    file_name: Optional[str]
    stage: str
    status: Literal["running", "pass", "warn", "fail", "info"]
    message: str
    ts: str
    details: dict[str, Any] = Field(default_factory=dict)
