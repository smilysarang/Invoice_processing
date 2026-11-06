"""Stage 9 - deterministic AP policy engine. Single source of truth for the final decision.

Agents never decide. Agent findings enter only as additional (escalating) findings.
"""
from __future__ import annotations

from typing import Optional

from src.master_data import policy
from src.models import DecisionResult

NEXT_ACTIONS = {
    "BANK_MISMATCH_HIGH_RISK": "Do not pay. Verify the bank change by calling the vendor on the contact already on file (not the one on the invoice); a controller must approve any vendor-master change.",
    "DUP_EXACT_FILE": "No action - this exact file was already received. Link to the original.",
    "DUP_EXACT_BUSINESS_KEY": "No action - same invoice already recorded (re-scan/re-send). Link to the original.",
    "DUP_POSSIBLE_REISSUE": "Compare both versions with the vendor; void one before either is paid.",
    "DUP_NEAR": "Check with the vendor whether this is a separate charge or a duplicate.",
    "PO_AMBIGUOUS": "Ask the requester or vendor which PO this invoice is for; select it in review.",
    "PO_NO_CANDIDATE": "Obtain the PO number from the vendor or requester.",
    "PO_WEAK_MATCH": "Confirm the proposed PO with the requester before approval.",
    "PO_NOT_FOUND": "PO number not in the procurement system - ask the vendor to correct it.",
    "PO_VENDOR_MISMATCH": "PO belongs to another vendor - return the invoice for correction.",
    "AMBIGUOUS_DATE": "Confirm the invoice date (day/month order) with the vendor.",
    "AMBIGUOUS_NUMBER": "Confirm the amount format with the vendor.",
    "AMBIGUOUS_CURRENCY": "Confirm the invoice currency with the vendor.",
    "MISSING_REQUIRED_FIELD": "Request a corrected invoice with the missing field(s).",
    "VENDOR_NOT_APPROVED": "Vendor is not active/approved - route to procurement; do not pay.",
    "VENDOR_NOT_FOUND": "Vendor not in master data - route to vendor onboarding.",
    "PROMPT_INJECTION_SUSPECTED": "Invoice contains instructions aimed at automation - treat as suspicious; review manually.",
    "GROUP_DISCREPANCY": "Resolve the PO-group discrepancies with the vendor (credit note or corrected invoice); no invoice in the group is accepted until the full set reconciles.",
    "GROUP_BLOCKED_BY_MEMBER": "Another invoice in this PO group failed its own checks; resolve it first - the group is accepted all-or-none.",
    "GROUP_INCOMPLETE": "Waiting for the remaining split invoice(s) for this PO; the group is re-reconciled when they arrive.",
    "TAX_RATE_CHECK": "Tax does not match the PO tax treatment - request a corrected invoice.",
    "TOTAL_RECONCILIATION": "Invoice totals do not add up - request a corrected invoice.",
    "LINE_ARITHMETIC": "Line amounts do not add up - request a corrected invoice.",
    "LINES_SUM_RECONCILIATION": "The line items do not add up to the invoice subtotal/total - check the PDF and request a corrected invoice if confirmed.",
    "EXTRACTION_DISAGREEMENT": "The two extraction passes disagree - check the highlighted field against the PDF.",
    "AGENT_RISK_ESCALATION": "An AI agent raised a risk the rules did not - review its evidence.",
    "AGENT_POLICY_CONFLICT": "AI agent and deterministic matching disagree - review the match.",
    "DOC_OUT_OF_SCOPE": "Not an invoice - route to the right process.",
    "INTAKE_CORRUPT": "File is damaged - ask the vendor to resend.",
    "INTAKE_FILE_TYPE": "File is not a PDF - ask the vendor to resend as PDF.",
    "OCR_UNAVAILABLE": "Install the OCR engine or review the scanned invoice manually.",
}


def decide(findings: list[dict], group: Optional[dict] = None, document_id: Optional[str] = None,
           selected_vendor: Optional[str] = None, selected_po: Optional[str] = None) -> DecisionResult:
    pol = policy()
    findings = list(findings)
    if group:
        is_blocker = any(m["document_id"] == document_id and m["own_blocking"] for m in group["member_details"])
        st = group["status"]
        if st == "BLOCKED" and not is_blocker:
            findings.append({"rule_id": "GROUP_BLOCKED_BY_MEMBER", "severity": "ERROR", "status": "FAIL",
                             "message": f"PO group {group['group_id']} is blocked by: {', '.join(group['blocking_members'])}."})
        if group["discrepancies"]:   # always surface what does not reconcile, even if the group is also blocked
            findings.append({"rule_id": "GROUP_DISCREPANCY", "severity": "ERROR", "status": "FAIL",
                             "message": "; ".join(group["discrepancies"])})
        if st == "INCOMPLETE":
            findings.append({"rule_id": "GROUP_INCOMPLETE", "severity": "ERROR", "status": "FAIL",
                             "message": "; ".join(group["still_needed"])})
        elif st == "ACCEPTED" and len(group["members"]) > 1:
            findings.append({"rule_id": "SPLIT_GROUP_RECONCILED", "severity": "INFO", "status": "PASS", "notable": True,
                             "message": f"{len(group['members'])} invoices together reconcile exactly to {group['po_number']}."})
    if not pol["po_inference"]["allow_auto_approve_inferred_po"]:
        for f in findings:
            if f["rule_id"] == "PO_INFERRED":
                f.update(severity="ERROR", status="FAIL")
    fails = [f for f in findings if f["status"] == "FAIL" and f["severity"] == "ERROR"]
    warns = [f for f in findings if f["status"] == "WARN"]
    fail_ids = list(dict.fromkeys(f["rule_id"] for f in fails))
    warn_ids = list(dict.fromkeys([f["rule_id"] for f in warns] + [f["rule_id"] for f in findings if f.get("notable")]))
    inv_rules, dup_rules, pend_rules = set(pol["reject_invalid_rules"]), set(pol["reject_duplicate_rules"]), set(pol["pending_rules"])

    if any(r in inv_rules for r in fail_ids):
        decision, reasons = "REJECT_INVALID", [r for r in fail_ids if r in inv_rules]
    elif any(r in dup_rules for r in fail_ids):
        decision, reasons = "REJECT_DUPLICATE", [r for r in fail_ids if r in dup_rules]
    elif [r for r in fail_ids if r not in pend_rules]:
        decision, reasons = "HOLD_FOR_REVIEW", [r for r in fail_ids if r not in pend_rules] + [r for r in fail_ids if r in pend_rules]
    elif fail_ids:
        decision, reasons = "PENDING_SPLIT_RECONCILIATION", fail_ids
    else:
        decision, reasons = "AUTO_APPROVE", ["ALL_REQUIRED_CHECKS_PASSED"] + warn_ids
    if decision == "AUTO_APPROVE":
        nxt = "Ready for the company's next approval/payment workflow. No payment has been initiated."
    else:
        nxt = " ".join(dict.fromkeys(NEXT_ACTIONS.get(r, f"Review {r}.") for r in reasons[:3]))
    return DecisionResult(decision=decision, reason_codes=reasons, warnings=warn_ids if decision != "AUTO_APPROVE" else [],
                          next_action=nxt, policy_version=pol["policy_version"], selected_vendor=selected_vendor,
                          selected_po=selected_po, group_id=group["group_id"] if group else None,
                          agent_conflicts=[f["rule_id"] for f in fails if f["rule_id"].startswith("AGENT_")])
