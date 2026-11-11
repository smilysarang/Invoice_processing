"""Agent 4 - Decision Explanation. Explains the FINAL policy result; cannot change it."""
from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel

from src.agents.base import Agent
from src.agents.providers.base import ProviderError

LABELS = {
    "ALL_REQUIRED_CHECKS_PASSED": "all required checks passed",
    "BANK_MISMATCH_HIGH_RISK": "bank account differs from the verified vendor record",
    "DUP_EXACT_FILE": "the exact same file was already received",
    "DUP_EXACT_BUSINESS_KEY": "the same invoice (vendor, number, amount, date) was already recorded",
    "DUP_POSSIBLE_REISSUE": "possible corrected reissue of an earlier invoice",
    "DUP_NEAR": "looks like an earlier invoice",
    "RECURRING_PATTERN": "recurring billing for a new period (not a duplicate)",
    "CREDIT_REBILL_EVIDENCED": "re-bill after a full credit note (not a duplicate)",
    "PO_INFERRED": "PO number not printed - inferred from strong evidence",
    "PO_AMBIGUOUS": "PO number not printed and several POs fit",
    "PO_NO_CANDIDATE": "no matching PO found",
    "PO_WEAK_MATCH": "only a weak PO match",
    "PO_NOT_FOUND": "PO number not in the procurement system",
    "PO_VENDOR_MISMATCH": "PO belongs to a different vendor",
    "AMBIGUOUS_DATE": "the date could be read two ways",
    "AMBIGUOUS_NUMBER": "an amount format is ambiguous",
    "AMBIGUOUS_CURRENCY": "the currency is ambiguous",
    "MISSING_REQUIRED_FIELD": "a required field is missing",
    "VENDOR_NOT_APPROVED": "vendor is not active/approved",
    "VENDOR_NOT_FOUND": "vendor not in the vendor master",
    "PROMPT_INJECTION_SUSPECTED": "the document contains instructions aimed at automation",
    "GROUP_DISCREPANCY": "the split invoices for this PO do not add up to the PO",
    "GROUP_INCOMPLETE": "not all split invoices for this PO have arrived yet",
    "GROUP_BLOCKED_BY_MEMBER": "another invoice in the same PO group failed its checks",
    "SPLIT_GROUP_RECONCILED": "all split invoices together match the PO exactly",
    "BUNDLED_TOTAL_MATCH": "bundled invoice matches the PO total",
    "DOC_OUT_OF_SCOPE": "the document is not an invoice",
    "INTAKE_CORRUPT": "the file is damaged",
    "INTAKE_FILE_TYPE": "the file is not really a PDF",
    "TAX_RATE_CHECK": "tax does not match the PO tax rate",
    "TAX_RATE_MISMATCH": "the invoice's tax rate differs from the PO's tax rate",
    "TAX_NOT_STATED": "the invoice does not state the tax (treated as included in the total)",
    "TAX_NOT_CHARGED": "no tax was charged although the PO items carry tax",
    "TAX_TOTAL_VS_PO": "the invoice total does not match the PO value for the invoiced items",
    "TAX_TOTAL_CHECK": "the total tax does not match the tax expected for the items",
    "TAX_TOTAL_CONSISTENCY": "the per-item taxes do not add up to the stated total tax",
    "TOTAL_RECONCILIATION": "the invoice totals do not add up",
    "LINES_SUM_RECONCILIATION": "the line items do not add up to the invoice total",
    "EXTRACTION_DISAGREEMENT": "the two independent readings of the document disagree",
    "AGENT_RISK_ESCALATION": "an AI agent raised an additional risk",
    "AGENT_POLICY_CONFLICT": "AI agent and rules disagree on the PO",
    "AGENT_UNAVAILABLE": "a live AI agent call failed",
    "REPROCESSED_AFTER_TECHNICAL_FAILURE": "re-processed after an earlier technical failure (not a duplicate)",
    "OCR_UNAVAILABLE": "the scanned page could not be read (OCR not installed)",
}
HEADLINES = {"AUTO_APPROVE": "Ready for the next approval/payment step",
             "HOLD_FOR_REVIEW": "Held for human review",
             "PENDING_SPLIT_RECONCILIATION": "Waiting for the rest of the split invoices",
             "REJECT_DUPLICATE": "Rejected as a duplicate",
             "REJECT_INVALID": "Rejected - not a processable invoice"}


class ExplanationOut(BaseModel):
    decision_echo: str
    headline: str
    explanation: str
    reviewer_checklist: list[str]
    vendor_message_draft: Optional[str] = None


SYSTEM = """You are the Decision Explanation Agent in an accounts-payable pipeline.
The decision has ALREADY been made by a deterministic policy engine. You must not change, soften or contradict it.
Write for a non-technical AP reviewer:
- headline: one short line.
- explanation: 3-5 plain-language sentences citing the specific evidence (amounts, PO numbers, fields). Never hide a failed check.
- Mention EVERY warning you are given in the explanation, even when the decision is AUTO_APPROVE
  (e.g. "Warning: the invoice does not state the tax ...").
- reviewer_checklist: concrete next steps.
- vendor_message_draft: ONLY if the vendor must supply or correct something; a short polite draft (it is never sent automatically).
  For bank-detail changes, do NOT ask the vendor to confirm by replying; say the company will call the contact already on file.
- decision_echo: must be exactly the decision string you were given.
State that no payment has been made. Never write "approved for payment" or "paid": AUTO_APPROVE only means the invoice is
ready for the company's next approval/payment step. Do not include full bank account numbers."""


def template(ctx) -> dict:
    d = ctx["decision"]
    dec, reasons = d["decision"], d["reason_codes"]
    fails = [f for f in ctx["findings"] if f.get("status") == "FAIL" and f.get("severity") == "ERROR"]
    warns = [f for f in ctx["findings"] if f.get("status") == "WARN" or f.get("notable")]
    warns.sort(key=lambda f: f.get("rule_id") != "TAX_NOT_STATED")     # the tax warning is always shown first
    passes = [f for f in ctx["findings"] if f.get("status") == "PASS"]
    if dec == "AUTO_APPROVE":
        why = [f"{len(passes)} checks passed."] + [w["message"] if w["message"].startswith("Warning") else f"Note: {w['message']}" for w in warns[:3]]
        text = ("Every required field is grounded in the document, the vendor is approved, the PO match is unambiguous, amounts "
                "and tax reconcile and no duplicate or bank-detail risk was found. " + " ".join(why) + " No payment has been initiated.")
    else:
        top = "; ".join(LABELS.get(r, r) for r in reasons[:3])
        text = f"Decision: {dec} because {top}. " + " ".join(f"- {f['message']}" for f in fails[:4]) + " No payment has been initiated."
    checklist = [d["next_action"]] if dec != "AUTO_APPROVE" else ["Forward to the next approval step per delegation of authority."]
    if any(f.get("rule_id") == "TAX_NOT_STATED" for f in ctx["findings"]):
        checklist.append("Warning: tax is not stated on the invoice - request a tax invoice showing the tax (needed for input tax credit).")
    if "GROUP_DISCREPANCY" in reasons or "GROUP_INCOMPLETE" in reasons:
        checklist.append("Open the PO Groups page to see ordered vs invoiced quantities per line.")
    if "BANK_MISMATCH_HIGH_RISK" in reasons:
        checklist += ["Call the vendor on the registered number (not the one on the invoice).", "Do not edit the vendor master from this invoice."]
    vm = None
    inv_no = ctx.get("invoice_number") or "your recent invoice"
    if any(r in reasons for r in ("MISSING_REQUIRED_FIELD", "AMBIGUOUS_DATE", "AMBIGUOUS_NUMBER", "AMBIGUOUS_CURRENCY", "PO_AMBIGUOUS",
                                  "PO_NO_CANDIDATE", "GROUP_DISCREPANCY", "DUP_POSSIBLE_REISSUE", "TOTAL_RECONCILIATION", "TAX_RATE_CHECK",
                                  "TAX_RATE_MISMATCH", "TAX_NOT_CHARGED", "TAX_TOTAL_VS_PO", "TAX_TOTAL_CONSISTENCY", "TAX_TOTAL_CHECK")):
        asks = []
        if "MISSING_REQUIRED_FIELD" in reasons:
            asks.append("a corrected invoice that shows the invoice number")
        if "AMBIGUOUS_DATE" in reasons:
            asks.append("confirmation of the invoice date (please state the month in words)")
        if "PO_AMBIGUOUS" in reasons or "PO_NO_CANDIDATE" in reasons:
            asks.append("the purchase order number this invoice relates to")
        if "GROUP_DISCREPANCY" in reasons:
            asks.append("a corrected invoice or credit note so the invoices for " + (d.get("selected_po") or "the PO") + " match the ordered quantities and prices")
        if "DUP_POSSIBLE_REISSUE" in reasons:
            asks.append("confirmation of which version of this invoice is valid (and a credit note for the other)")
        if any(r in reasons for r in ("TOTAL_RECONCILIATION", "TAX_RATE_CHECK", "TAX_RATE_MISMATCH", "TAX_NOT_CHARGED",
                                      "TAX_TOTAL_VS_PO", "TAX_TOTAL_CONSISTENCY", "TAX_TOTAL_CHECK")):
            asks.append("a corrected invoice - the totals/tax do not reconcile")
        vm = (f"Dear {ctx.get('vendor_name') or 'Vendor'},\n\nThank you for invoice {inv_no}. Before we can process it we need "
              + "; ".join(asks) + ".\n\nKind regards,\nAccounts Payable (draft - not sent)")
    return {"decision_echo": dec, "headline": HEADLINES[dec], "explanation": text, "reviewer_checklist": checklist,
            "vendor_message_draft": vm, "source": "template (deterministic)"}


class ExplanationAgent(Agent):
    name = "DecisionExplanationAgent"
    system = SYSTEM
    schema = ExplanationOut

    def build_input(self, ctx):
        d = ctx["decision"]
        payload = {"decision": d["decision"], "reason_codes": d["reason_codes"], "next_action": d["next_action"],
                   "invoice_number": ctx.get("invoice_number"), "vendor": ctx.get("vendor_name"), "po": d.get("selected_po"),
                   "amounts": ctx.get("amounts"),
                   "failed_checks": [f["message"] for f in ctx["findings"] if f.get("status") == "FAIL" and f.get("severity") == "ERROR"][:8],
                   "warnings": [f["message"] for f in ctx["findings"] if f.get("status") == "WARN"][:5],
                   "passed_checks": [f["rule_id"] for f in ctx["findings"] if f.get("status") == "PASS"],
                   "po_group": ctx.get("group_summary")}
        return "Explain this final decision.\n" + json.dumps(payload, default=str, separators=(",", ":")), []

    def deterministic(self, ctx):
        return template(ctx)

    def postprocess(self, parsed: ExplanationOut, ctx):
        dec = ctx["decision"]["decision"]
        out = parsed.model_dump()
        out["source"] = "live LLM"
        if re.search(r"approved for payment|\bpaid\b|payment (has been )?(made|released|processed)", parsed.headline, re.I):
            out["headline"] = HEADLINES[dec]          # headline must not overstate what happened
        if parsed.decision_echo.strip() != dec:
            t = template(ctx)
            t["source"] = f"template - LLM explanation rejected (it said '{parsed.decision_echo}', policy decided '{dec}')"
            t["llm_output"] = out
            return t
        return out

    def on_error(self, ctx, err: ProviderError):
        t = template(ctx)
        t["source"] = f"template - live explanation agent failed ({err.kind}); decision unaffected"
        t["error"] = f"{err.kind}: {err.message}"
        return t
