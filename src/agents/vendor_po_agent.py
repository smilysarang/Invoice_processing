"""Agent 2 - Vendor & PO Matching. Reviews read-only vendor/PO tool results; can escalate, never select silently."""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel

from src.agents.base import UNTRUSTED_NOTE, Agent, finding


class POCandidateView(BaseModel):
    po_number: str
    plausible: bool
    rationale: str


class VendorPOAssessment(BaseModel):
    vendor_consistent: bool
    vendor_rationale: str
    candidates: list[POCandidateView]
    selected_po: Optional[str] = None
    ambiguous: bool
    questions_for_human: list[str]


SYSTEM = f"""You are the Vendor & PO Matching Agent in an accounts-payable pipeline.
You receive an extracted invoice summary plus READ-ONLY results of the tools lookup_vendor and search_po_candidates
(deterministic feature scores are included). Your job:
- Judge whether the supplier identity on the invoice is consistent with the vendor master record.
- Judge which PO candidates are plausible and why (item codes, descriptions, quantities, prices, amount, dates, references).
- Set selected_po ONLY if exactly one candidate is clearly supported by the evidence. If two or more remain plausible,
  set ambiguous=true, selected_po=null, and list the questions a human must answer.
- You cannot create or edit vendors or POs, change PO values, or waive tolerances. You do not approve payments.
{UNTRUSTED_NOTE}"""


def _summary(inv) -> dict:
    v = lambda k: inv.val(k)
    return {"supplier_name": v("supplier_name"), "supplier_tax_id": v("supplier_tax_id"), "invoice_number": v("invoice_number"),
            "invoice_date": v("invoice_date"), "po_number_printed": v("po_number"), "order_reference_text": inv.order_reference_text,
            "currency": v("currency"), "grand_total": v("grand_total"),
            "lines": [{"item_code": l.item_code, "description": l.description, "qty": l.quantity, "uom": l.uom,
                       "net_unit_price": l.net_unit_price} for l in inv.lines]}


class VendorPOAgent(Agent):
    name = "VendorPOMatchingAgent"
    system = SYSTEM
    schema = VendorPOAssessment

    def tool_calls(self, ctx):
        vm, pm = ctx["vendor_match"], ctx["po_match"]
        return [{"tool": "lookup_vendor", "args": {"name": ctx["inv"].val("supplier_name"), "tax_id": ctx["inv"].val("supplier_tax_id")},
                 "result": {"vendor_id": vm["vendor_id"], "method": vm["method"]}},
                {"tool": "search_po_candidates", "args": {"vendor_id": vm["vendor_id"], "printed_po": pm["printed_po"]},
                 "result": [c["po_number"] for c in pm["candidates"]] or ([pm["selected_po"]] if pm["selected_po"] else [])}]

    def build_input(self, ctx):
        vm, pm = ctx["vendor_match"], ctx["po_match"]
        vendor = dict(vm["vendor"] or {})
        if vendor.get("bank_account"):
            vendor["bank_account"] = "(masked)"
        payload = {"invoice": _summary(ctx["inv"]), "lookup_vendor": {"vendor": vendor, "match_method": vm["method"]},
                   "search_po_candidates": {"printed_po": pm["printed_po"], "deterministic_selected": pm["selected_po"],
                                            "deterministic_method": pm["method"], "candidates": pm["candidates"] or
                                            ([{"po_number": pm["selected_po"], "exact_reference": True}] if pm["selected_po"] else []),
                                            "line_mapping": pm.get("line_mapping")}}
        return "Assess vendor identity and PO match.\n" + json.dumps(payload, default=str, indent=1), []

    def deterministic(self, ctx):
        vm, pm = ctx["vendor_match"], ctx["po_match"]
        qs = []
        if pm["ambiguous"]:
            qs.append("Which PO does this invoice belong to: " + " or ".join(c["po_number"] for c in pm["candidates"] if c.get("plausible")) + "?")
        if not pm["selected_po"] and not pm["ambiguous"]:
            qs.append("Please provide the PO number for this invoice.")
        return {"vendor_consistent": vm["vendor_id"] is not None, "selected_po": pm["selected_po"], "ambiguous": pm["ambiguous"],
                "questions_for_human": qs, "findings": [],
                "summary": f"vendor {vm['vendor_id'] or 'not found'} ({vm['method']}); PO {pm['selected_po'] or '-'} via {pm['method'] or 'n/a'}"}

    def postprocess(self, parsed: VendorPOAssessment, ctx):
        pm, vm = ctx["po_match"], ctx["vendor_match"]
        findings = []
        cand_ids = {c["po_number"] for c in pm["candidates"]} | ({pm["selected_po"]} if pm["selected_po"] else set())
        if parsed.selected_po and parsed.selected_po not in cand_ids:
            findings.append(finding("AGENT_OUTPUT_INVALID", "ERROR", "FAIL",
                                    f"Agent selected {parsed.selected_po}, which is not among the tool results; ignored."))
        elif pm["selected_po"] and parsed.selected_po != pm["selected_po"]:
            findings.append(finding("AGENT_POLICY_CONFLICT", "ERROR", "FAIL",
                                    f"Deterministic match {pm['selected_po']} vs agent {parsed.selected_po or 'no selection'} - routed to review."))
        elif not pm["selected_po"] and parsed.selected_po:
            findings.append(finding("AGENT_SELECTION_IGNORED", "INFO", "INFO",
                                    f"Agent proposed {parsed.selected_po} but deterministic evidence was insufficient; agents cannot de-escalate."))
        if vm["vendor_id"] and not parsed.vendor_consistent:
            findings.append(finding("AGENT_RISK_ESCALATION", "ERROR", "FAIL", f"Agent doubts vendor identity: {parsed.vendor_rationale}"))
        return {"vendor_consistent": parsed.vendor_consistent, "selected_po": parsed.selected_po, "ambiguous": parsed.ambiguous,
                "questions_for_human": parsed.questions_for_human, "candidates": [c.model_dump() for c in parsed.candidates],
                "vendor_rationale": parsed.vendor_rationale, "findings": findings,
                "summary": f"agent: vendor {'consistent' if parsed.vendor_consistent else 'INCONSISTENT'}; PO {parsed.selected_po or '-'}"
                           f"{' (ambiguous)' if parsed.ambiguous else ''}"}
