"""Invoice review: PDF + evidence side by side, checks, matching, agents, audit, human review."""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from src import storage
from src.orchestrator import STAGE_LABEL
from src.pipeline.text_acquisition import render_page_png
from src.ui.common import CHECK_ICON, STAGE_ICON, badge, chips, label, mode_badge, money, setup

setup("Invoice review", "🔎")
st.title("🔎 Invoice review")
mode_badge()

docs = storage.dashboard_rows()
if not docs:
    st.info("Nothing processed yet.")
    st.stop()
ids = [d["document_id"] for d in docs]
names = {d["document_id"]: f"{d['file_name']} · {label(d['decision'])} · {d['document_id']}" for d in docs}
default = st.session_state.get("review_doc")
doc_id = st.selectbox("Invoice", ids, index=ids.index(default) if default in ids else 0, format_func=names.get)
st.session_state.review_doc = doc_id
rec = storage.document(doc_id)
cd = storage.current_decision(doc_id)
if not rec or not cd:
    st.warning("Record incomplete.")
    st.stop()
res, ex = cd["result"], cd["explanation"] or {}
inv = rec.get("invoice") or {}
F = inv.get("fields", {})
cur = (F.get("currency") or {}).get("value")
fv = lambda k: (F.get(k) or {}).get("value")

# ---------------- decision card
with st.container(border=True):
    a, b = st.columns([1.2, 3])
    a.markdown(badge(res["decision"]), unsafe_allow_html=True)
    a.markdown(f"<span style='font-size:1.6rem;font-weight:700'>{money(fv('grand_total'), cur)}</span>", unsafe_allow_html=True)
    a.caption(f"{(rec.get('vendor') or {}).get('name') or 'vendor unknown'} · {fv('invoice_number') or 'no invoice no.'} · "
              f"PO {res.get('selected_po') or '-'}")
    a.caption(f"decision v{cd['version']} · {cd['trigger']} · policy {res['policy_version']} · mode {rec['processing_mode']}")
    b.markdown(f"### {ex.get('headline', '')}")
    b.markdown(ex.get("explanation", ""))
    b.markdown("**Reason codes** " + chips(res["reason_codes"]), unsafe_allow_html=True)
    b.markdown(f"**Next action:** {res['next_action']}")
    if ex.get("reviewer_checklist"):
        b.markdown("**Reviewer checklist**\n" + "\n".join(f"- [ ] {x}" for x in ex["reviewer_checklist"]))
    if ex.get("vendor_message_draft"):
        with b.expander("✉️ Draft message to vendor (not sent)"):
            st.text(ex["vendor_message_draft"])
    b.caption(f"Explanation source: {ex.get('source', '-')}")

# ---------------- PDF + evidence
pdf = Path(rec["stored_path"]).read_bytes() if rec.get("stored_path") and Path(rec["stored_path"]).exists() else None
left, right = st.columns([1, 1.25], gap="large")
with left:
    st.markdown("#### 📄 Document")
    if pdf and rec.get("page_count"):
        fields_with_box = [k for k, x in F.items() if x.get("bbox")]
        pref = "supplier_bank_account" if "BANK_MISMATCH_HIGH_RISK" in res["reason_codes"] else "grand_total"
        opts = ["(none)"] + fields_with_box
        hl = st.selectbox("Highlight evidence for", opts, index=opts.index(pref) if pref in opts else 0)
        page = (F[hl]["source_page"] if hl != "(none)" else None) or 1
        page = st.number_input("Page", 1, rec["page_count"], int(page))
        boxes = [F[hl]["bbox"]] if hl != "(none)" and F[hl].get("source_page") == page else []
        st.image(render_page_png(pdf, int(page), dpi=110, highlights=boxes), width="stretch")
    else:
        st.warning("No renderable PDF (file was rejected at intake).")
with right:
    tabs = st.tabs(["Fields", "Checks", "Vendor & PO", "Duplicates", "Agents", "Audit", "Review"])
    with tabs[0]:
        if F:
            rows = [{"field": k, "value": x.get("value"), "raw text (as printed)": x.get("raw_text"), "page": x.get("source_page"),
                     "method": x.get("method"), "confidence": x.get("confidence"),
                     "grounded": "✅" if x.get("grounded") else ("⛔" if x.get("grounded") is False else "-"),
                     "missing / note": x.get("missing_reason") or "; ".join(x.get("notes") or [])} for k, x in F.items()]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if inv.get("lines"):
                st.markdown("**Line items**")
                st.dataframe(pd.DataFrame([{k: l.get(k) for k in ("line_no", "item_code", "description", "quantity", "uom", "unit_price",
                                                                    "line_total", "net_unit_price", "source_page", "method")}
                                           for l in inv["lines"]]), hide_index=True, width="stretch")
            calc = rec.get("calc") or {}
            st.caption(f"Locale: {inv.get('locale', {}).get('language')} · numbers {inv.get('locale', {}).get('number_style')} · "
                       f"dates {inv.get('locale', {}).get('date_order') or 'undetermined'} · tax-inclusive {calc.get('tax_inclusive')} · "
                       f"net subtotal {calc.get('net_subtotal')} · tax {calc.get('tax_total')}")
            if inv.get("service_period") or inv.get("order_reference_text") or inv.get("references"):
                st.caption(f"Service period: {inv.get('service_period') or '-'} · order reference: {inv.get('order_reference_text') or '-'} · "
                           f"referenced documents: {', '.join(inv.get('references') or []) or '-'}")
            if rec.get("extraction_comparison"):
                st.markdown("**Rule extractor vs LLM (Agent 1) cross-check**")
                st.dataframe(pd.DataFrame(rec["extraction_comparison"]), hide_index=True, width="stretch")
            if rec.get("normalization"):
                with st.expander("Normalisation log (raw → normalised)"):
                    st.dataframe(pd.DataFrame(rec["normalization"]), hide_index=True, width="stretch")
        else:
            st.info("No fields extracted - the document stopped before extraction.")
    with tabs[1]:
        for stg in ["intake", "text", "understanding", "agents", "verification", "vendor_po", "risk", "duplicates", "pipeline"]:
            fs = [f for f in rec["findings"] if f.get("stage") == stg]
            if not fs:
                continue
            st.markdown(f"**{stg.replace('_', ' ').title()}**")
            for f in fs:
                st.markdown(f"{CHECK_ICON.get(f['status'], '•')} `{f['rule_id']}` <span class='muted'>({f['severity']})</span> - {f['message']}",
                            unsafe_allow_html=True)
        st.caption("✅ pass · ⚠️ warning (non-blocking) · ⛔ blocking - any ⛔ prevents auto-approval")
    with tabs[2]:
        v = rec.get("vendor") or {}
        pm = rec.get("po_match") or {}
        st.markdown(f"**Vendor:** {v.get('vendor_id') or '-'} {v.get('name') or ''} · matched by {v.get('method') or '-'} · status {v.get('status') or '-'}")
        st.markdown(f"**Verified bank (master):** {v.get('bank_masked') or '-'} · **Registered contact:** {v.get('contact_ref') or '-'}")
        st.markdown(f"**PO printed:** {pm.get('printed_po') or '— none —'} · **selected:** {pm.get('selected_po') or '-'} · "
                    f"**method:** {pm.get('method') or ('ambiguous' if pm.get('ambiguous') else '-')}")
        if pm.get("candidates"):
            st.markdown("**Candidate POs (ranked; scores rank, they never approve)**")
            st.dataframe(pd.DataFrame([{"PO": c["po_number"], "score": c["score"], "plausible": c.get("plausible"), "PO date": c["po_date"],
                                        "description": c["description"], "remaining": c["remaining_value"], **c["features"],
                                        "bonus": c.get("bonus")} for c in pm["candidates"]]), hide_index=True, width="stretch")
        lm = pm.get("line_mapping")
        if lm and lm.get("mapped"):
            st.markdown("**Invoice lines → PO lines**")
            st.dataframe(pd.DataFrame(lm["mapped"]), hide_index=True, width="stretch")
        if lm and lm.get("mode") == "BUNDLED":
            st.info(f"Bundled invoice: one line covers PO lines {', '.join(lm['bundled']['po_lines'])} - compared on totals.")
        a2 = rec.get("agent2") or {}
        if a2.get("questions_for_human"):
            st.markdown("**Questions for a human (Agent 2):**\n" + "\n".join(f"- {q}" for q in a2["questions_for_human"]))
        if rec.get("group"):
            st.markdown(f"**PO group:** `{rec['group']['group_id']}` → {rec['group']['status']} (open the PO Groups page for line-level detail)")
    with tabs[3]:
        dup = rec.get("dup") or {}
        st.markdown(f"**Duplicate classification:** `{dup.get('classification', '-')}`")
        if dup.get("matches"):
            st.dataframe(pd.DataFrame([{**m, "signals": "; ".join(m["signals"])} for m in dup["matches"]]), hide_index=True, width="stretch")
        a3 = rec.get("agent3") or {}
        if a3.get("risk_flags"):
            st.markdown("**Risk flags (Agent 3)**")
            st.dataframe(pd.DataFrame(a3["risk_flags"]), hide_index=True, width="stretch")
        if a3.get("rationale"):
            st.caption(f"Agent 3 rationale: {a3['rationale']}")
        hits = (rec.get("understanding") or {}).get("injection_hits") or inv.get("injection_hits")
        if hits:
            st.error("Instruction-like text found in the document (treated as data, never followed):\n\n" + "\n".join(f"- {h}" for h in hits))
    with tabs[4]:
        traces = storage.traces(doc_id)
        if not traces:
            st.info("No agent ran (document stopped early).")
        for t in traces:
            icon = "✅" if t["status"] == "completed" else "⛔"
            with st.expander(f"{icon} {t['agent']} · {t['mode']} · {t.get('provider') or ''} {t.get('model') or ''} · "
                             f"{t.get('latency_ms') or 0} ms"):
                st.markdown(f"started {t['started_at']} · tokens in/out {t.get('tokens_in')}/{t.get('tokens_out')} · "
                            f"request id {t.get('request_id') or '-'} · retries {t.get('retries')}")
                if t.get("error"):
                    st.error(t["error"])
                if t.get("tool_calls"):
                    st.markdown("**Read-only tool calls**")
                    st.json(t["tool_calls"], expanded=False)
                st.markdown("**Output**")
                st.json(t.get("output") or {}, expanded=False)
    with tabs[5]:
        st.markdown("**Decision versions**")
        st.dataframe(pd.DataFrame(storage.decision_history(doc_id)), hide_index=True, width="stretch")
        ev = storage.audit_for(doc_id)
        st.markdown(f"**Timeline** · correlation `{rec.get('correlation_id')}` · SHA-256 `{rec.get('sha256', '')[:24]}…`")
        for e in ev:
            st.markdown(f'<div class="stage-row"><span class="muted">{e["ts"][11:23]}</span> {STAGE_ICON.get(e["status"], "•")} '
                        f'<b>{STAGE_LABEL.get(e["stage"], e["stage"])}</b> <span class="muted">[{e["actor"]}]</span> - {e["message"]}</div>',
                        unsafe_allow_html=True)
    with tabs[6]:
        st.caption("Human actions are recorded alongside - never over - the automated result. They change only this demo's state; "
                   "no payment or external system is touched.")
        with st.form("review_form", clear_on_submit=True):
            act = st.radio("Action", ["Approve for next workflow", "Request correction from vendor", "Reject / return", "Add note"], horizontal=True)
            who = st.text_input("Reviewer", "ap.reviewer@demo")
            note = st.text_area("Comment (required)")
            if st.form_submit_button("Record action") and note.strip():
                storage.reviewer_action(doc_id, act, who, note.strip())
                st.success("Recorded in the audit trail.")
        with st.form("fb_form", clear_on_submit=True):
            st.markdown("**Correct an extracted field** (stored as labelled feedback for evaluation; source evidence is not rewritten)")
            fld = st.selectbox("Field", list(F) or ["-"])
            new = st.text_input("Correct value")
            if st.form_submit_button("Save correction") and new.strip() and F:
                storage.add_feedback(doc_id, fld, str((F.get(fld) or {}).get("value")), new.strip(), "ap.reviewer@demo")
                st.success("Saved as feedback.")
        acts = storage.rows("SELECT ts, action, reviewer, comment FROM reviewer_actions WHERE document_id=? ORDER BY id", (doc_id,))
        fb = storage.rows("SELECT ts, field, original_value, corrected_value FROM feedback WHERE document_id=? ORDER BY id", (doc_id,))
        if acts:
            st.dataframe(pd.DataFrame(acts), hide_index=True, width="stretch")
        if fb:
            st.dataframe(pd.DataFrame(fb), hide_index=True, width="stretch")
