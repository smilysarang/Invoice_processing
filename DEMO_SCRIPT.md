# 5-minute demo script (video and live interview)

**Before recording:** run `streamlit run Home.py`, then click **Reset demo state** in the sidebar. Set the live-view pacing to about 0.25 s. Choose the mode:
- **Deterministic demo:** reliable and needs no key.
- **Live LLM:** shows the real Gemini agents but is slower and needs quota. Option: run #1 live, the rest in demo mode.

| Time | Do | Say |
|---|---|---|
| 0:00–0:30 | Home page | "AP teams match hundreds of PDF invoices to POs by hand. This process reads each invoice, checks it against the vendor master and POs, and makes a decision it can explain. AI agents read and investigate; deterministic rules decide; no payment is ever made." |
| 0:30–1:30 | Run **Happy path - clean itemised invoice** → watch Live Run | Walk the stages: "file type checked from bytes, native text, Indian lakh format normalised to Decimal, every field grounded in the page, vendor by tax ID, exact PO, no duplicate, bank matches, one-invoice PO group reconciles exactly → auto-approve." Click **Review**: the PDF with the highlighted total and the evidence table. |
| 1:30–2:15 | Run **EC3 - Bank details changed** | "Same perfect invoice, except a new bank account. A tired person approves this - it's the classic payment-fraud pattern. The system holds it, masks the account, refuses to update the master and says to call the number already on file." |
| 2:15–3:15 | Run **Split PO - valid 3-invoice set**, then **failing set** → **PO Groups** | "A vendor split one PO across three invoices. Each is only a partial bill, so none can be approved alone. Together they match every line, quantity and price - so all three are accepted in one transaction." Then the failing set: "one monitor short and keyboards at ₹2,100 instead of ₹2,000 - so *none* is accepted, and the table shows exactly why." |
| 3:15–3:45 | Run **EC1 - No PO number, ambiguous** | "No PO, just 'as discussed with Maria'. Two open POs fit equally, so it doesn't guess: here's the candidate table and the question for the requester." |
| 3:45–4:20 | Run **EC2 - Duplicates & look-alikes** | "Exact resend and re-scan rejected and linked to the original; corrected reissue held; the credit-and-rebill and the monthly retainer are *not* false-flagged even though they look similar." |
| 4:20–5:00 | **Dashboard**, then a review **Agents** tab | "History, status and value across runs. Each agent call is traced with mode, model, latency and tool calls. Agents can escalate but never approve - this invoice even told the 'AI system' to approve it, and it was held." |

**Backup scenarios:** EC4 German invoice (`4.203,48`, `31.10.2026`) → approved; ambiguous date `07/10/2026` → held; scanned invoice → OCR; later-session split (step 1 → pending, step 2 → all approved).

**Likely questions:**
- *Why not let the LLM decide?* Money needs reproducible, auditable rules. LLMs add reading and judgement on messy inputs, and they can only make a decision more conservative.
- *What if Gemini fails?* The error is visible, the invoice is held, and there is never a silent fallback.
- *Tolerances?* They are versioned YAML on the Rules page, and every decision records the policy version it used.
