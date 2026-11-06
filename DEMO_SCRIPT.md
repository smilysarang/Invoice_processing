# 5-minute demo script (video and live interview)

## Before recording (about 10 minutes)

1. `python scripts/check_llm.py`: should print `groq / openai/gpt-oss-20b` and a successful ping. If it reports the daily quota is used up, switch `GROQ_MODEL` to the other gpt-oss model (each has its own daily budget).
2. Stop and restart Streamlit: `streamlit run Home.py`. This loads the latest code.
3. Sidebar: click **Reset demo state** and set live-view pacing to about 0.25 s.
4. Do one throwaway live run (for example the itemised happy path), then **Reset demo state** again. This warms up OCR and confirms the key.
5. Close other tabs and hide `.env`. Never show the API key on screen.

**Which mode for which segment.** Groq's free tier allows 8,000 tokens per minute, and an invoice uses about 5,000:
- **Live LLM** for single invoices: happy path, bank change, scanned. About 10–20 s each if you leave about a minute between them.
- **Deterministic demo** for the multi-invoice sets: split PO and duplicates. The decisions are identical, and live they take about 2 minutes per set. Switch the sidebar mode before running them; the badge shows the mode, so say it out loud.
- If you'd rather show everything live, record it and cut the waiting in editing.

The **Reset & run** button on Home re-runs a scenario whose files are already in history. Otherwise it would correctly come back as a duplicate.

---

## Script

| Time | Mode | Do | Say |
|---|---|---|---|
| 0:00–0:30 | – | Home page | "AP teams match PDF invoices to POs by hand. This process reads each invoice, checks it against the vendor master, POs and history, and makes a decision it can explain. AI agents read and investigate; deterministic rules decide; nothing is ever paid." |
| 0:30–1:30 | **Live** | Run **Happy path - clean itemised invoice**, watch Live Run, then **Review** | Walk the stages: "file type checked from the bytes, native text, Indian lakh format normalised to Decimal, every field grounded in the page, vendor by tax ID, exact PO, no duplicate, bank matches, the one-invoice PO group reconciles → auto-approve." Show the PDF with the highlighted total, then the **Agents** tab: "four separate agent calls, here on Groq's free tier; model, latency, tokens and the rules-vs-LLM comparison for every field." |
| 1:30–2:10 | **Live** | Run **EC3 - Bank details changed, everything else perfect** | "Same perfect invoice, except a new bank account. That's the classic payment-fraud pattern, and a tired person approves it. The system holds it, masks the account, never updates the master from an invoice, and says to call the number already on file. The drafted vendor note deliberately doesn't ask them to confirm by reply." |
| 2:10–3:10 | Demo | Run **Split PO - valid 3-invoice set (all-or-none)**, then **Split PO - failing 3-invoice set**, then **PO Groups** | "One PO split across three invoices. Each is only a partial bill, so none can be approved alone. Together they match every line, quantity and price, so all three are accepted in one transaction." Failing set: "one monitor short and keyboards at ₹2,100 instead of ₹2,000, so *none* is accepted, and every invoice shows exactly why." |
| 3:10–3:40 | Demo | Run **EC1 - No PO number, ambiguous candidates** | "No PO, just 'as discussed with Maria'. Two open POs fit equally, so it doesn't guess: here's the candidate table and the question for the requester." |
| 3:40–4:15 | Demo | Run **EC2 - Duplicates & look-alikes (4 variants)** | "Exact resend and re-scan rejected and linked to the original; corrected reissue held; the credit-and-rebill and the monthly retainer are *not* false-flagged, even though they look alike." |
| 4:15–5:00 | Demo | Run **Bonus - prompt injection + inactive vendor**, then **Dashboard** | "History, status and value across runs. And this invoice told the 'AI system' to approve it: the agent flagged the text, the rules held it. Agents can make a decision stricter, never looser." |

**Backup scenarios:**
- German invoice EC4a (`4.203,48 €`, `31.10.2026`) → approved, live in about 15 s;
- ambiguous date EC4b `07/10/2026` → held;
- scanned invoice → OCR at 300 dpi with deskew, live;
- later-session split: step 1 → pending, step 2 → all three approved together.

---

## Likely questions

- *Why not let the LLM decide?* Money needs reproducible, auditable rules. LLMs add reading and judgement on messy inputs, and they can only make a decision more conservative.
- *Did the LLM ever get something wrong?* Yes, and it's the best example of the design. In a live run the model called net prices tax-inclusive, which shifted the unit prices. The split group failed to reconcile, so the invoices were **held, not paid**. The fix keeps the rule that agents don't change values: code now settles the tax treatment from the printed totals, and the agent's view stays in the trace.
- *What if the LLM provider fails or runs out of quota?* The error is visible, the invoice is held with `AGENT_UNAVAILABLE`, and there is never a silent fallback to demo mode or to another provider.
- *Why is live mode slower?* The free tier caps tokens per minute. Model time is about 3–4 s per invoice, and the rest is waiting for the rate-limit window, read from the provider's headers. A paid tier or a local model removes it.
- *Real invoices on a free API?* No. The demo uses synthetic data only. The provider is one config value, so production can point the same adapter at a private or local model (Ollama).
- *Tolerances?* They are versioned YAML on the Rules page, and every decision records the policy version it used.
