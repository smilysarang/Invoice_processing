# Assumptions & design decisions

Where the problem statement was ambiguous I chose an option and recorded it here.

1. **"Approve" never means "pay".** `AUTO_APPROVE` = the automated AP checks passed and the invoice can move to the company's next approval/payment step. Nothing here moves money, sends email or edits master data.
2. **PO types.**
   - One-time POs (goods/projects) must be invoiced **completely and exactly**, by one invoice or a split set. Acceptance is all-or-none at group level.
   - Blanket POs (retainers/subscriptions) are billed per period against the remaining balance.
   - The PO record carries `billing_type`.
3. **Split sets.**
   - An under-billed group is treated as *incomplete*: it waits as `PENDING_SPLIT_RECONCILIATION` and consumes nothing.
   - A price mismatch, over-billing or an unexpected item is a *discrepancy*: `HOLD_FOR_REVIEW` for every member.
   - Closing an incomplete group short (e.g. a short shipment) is a human decision.
4. **Tolerances (demo values, in YAML).**
   - Invoice-internal rounding: 1.00.
   - Tax vs PO rate: 1.00.
   - Unit prices must equal the PO.
   - Split-group aggregate: 0.01 (currency rounding only).
   - Real tolerances are a company policy choice.
5. **Inferred POs.** With no PO number, a single strong candidate (score ≥ 0.85, the next one ≥ 0.10 behind) may auto-approve when the invoice alone fully reconciles to that PO. This is a config switch (`allow_auto_approve_inferred_po`); a conservative company would set it to `false`. An inferred PO can never join a multi-invoice split group.
6. **Duplicates.**
   - Same vendor + invoice number + amount + date (or identical bytes) = confirmed duplicate → reject.
   - Same number with different content = possible corrected reissue → hold and link; a human decides which version is valid.
   - Same amount alone is never enough.
7. **Bank details.** Any difference from the verified vendor master holds the invoice, however clean everything else is. Verification must use the contact already on file, never the one on the invoice.
8. **Dates.**
   - Slash dates where both parts are ≤ 12 are ambiguous unless the *same document* proves the order (e.g. another date with day > 12).
   - Dot-separated dates in a German / comma-decimal document are day-first.
   - Vendor country is deliberately not used, because vendors' invoicing software varies.
9. **Currency.** It comes from a printed ISO code or an unambiguous symbol (€, ₹/Rs). A bare `$` is ambiguous. No FX conversion is inferred; an invoice whose currency differs from the PO's is held.
10. **Tax-inclusive invoices.** Net values are derived as gross ÷ (1 + rate), using the rate implied by the printed included-tax amount, before comparing with tax-exclusive PO prices.
11. **Bundled invoices.** A single bundled line may be matched to a multi-line PO only if the PO allows bundled billing; it is then compared on totals and flagged `BUNDLED_TOTAL_MATCH`.
12. **Invoice text is untrusted data.** Instruction-like text aimed at automation is flagged (`PROMPT_INJECTION_SUSPECTED`) and the invoice is held. It is never followed.
13. **Live-mode failures.** If a live agent fails (quota, auth, network), the invoice is held (`AGENT_UNAVAILABLE`). The deterministic reading is still shown to the reviewer, but the run is not silently treated as a demo-mode result.
14. **Data.** All vendors, tax IDs, bank accounts, contacts and invoices are fictitious. Free-tier LLM APIs are used with synthetic data only.
