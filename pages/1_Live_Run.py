"""Live run view: every stage of every document as it executes."""
import time

import streamlit as st

from src.orchestrator import STAGES, process_batch
from src.ui.common import STAGE_ICON, badge, chips, current_mode, label, mode_badge, money, open_review, provider_status, setup

setup("Live run", "▶️")
st.title("▶️ Live run")
mode_badge()

STAGE_KEYS = [k for k, _ in STAGES]


class LiveView:
    """Renders one status box per document with one line per stage, updated as events arrive."""

    def __init__(self, area, pace: float):
        self.area, self.pace = area, pace
        self.docs: dict[str, dict] = {}

    def _doc(self, ev):
        key = ev["document_id"] or ev["file_name"]
        if key not in self.docs:
            box = self.area.status(f"📄 {ev['file_name']}", expanded=True, state="running")
            slots = {k: box.empty() for k in STAGE_KEYS + ["pipeline"]}
            self.docs[key] = {"box": box, "slots": slots, "worst": "pass", "name": ev["file_name"]}
        return self.docs[key]

    def __call__(self, ev):
        d = self._doc(ev)
        slot = d["slots"].get(ev["stage"]) or d["slots"]["pipeline"]
        icon = STAGE_ICON.get(ev["status"], "•")
        msg = ev["message"] if ev["status"] != "running" else "_running…_"
        slot.markdown(f'<div class="stage-row">{icon} <b>{ev["label"]}</b> - {msg}</div>', unsafe_allow_html=True)
        if ev["status"] in ("fail",):
            d["worst"] = "fail"
        if ev["stage"] == "decision":
            dec = ev["details"].get("decision")
            d["box"].update(label=f"📄 {d['name']} → {label(dec)}", state="complete" if dec == "AUTO_APPROVE" else "error"
                            if dec and dec.startswith("REJECT") else "complete")
        if ev["status"] != "running" and self.pace:
            time.sleep(self.pace)


pending = st.session_state.pop("pending_batch", None)
if pending:
    st.markdown(f"**Batch:** {pending['label']} · {len(pending['files'])} file(s)")
    provider, err = provider_status()
    if err:
        st.error(f"Live LLM mode is selected but the provider is not ready - nothing was processed. {err}. "
                 "Fix the configuration or switch to deterministic demo mode in the sidebar. (No silent fallback.)")
        st.stop()
    area = st.container()
    view = LiveView(area, st.session_state.get("pace", 0.15))
    with st.spinner("Processing…"):
        result = process_batch(pending["files"], mode=current_mode(), on_event=view, label=pending["label"], provider=provider)
    for key, d in view.docs.items():
        if d["box"] is not None:
            d["box"].update(expanded=False)
    st.session_state.last_batch = result
    st.success("Batch complete - results below. Expand a document above to replay its stages.")
    st.divider()

result = st.session_state.get("last_batch")
if not result:
    st.info("No batch yet. Go to **Home** to upload invoices or run a sample scenario.")
    st.page_link("Home.py", label="Go to Home", icon="🏠")
    st.stop()

st.markdown(f"**Batch `{result['batch_id']}`** · mode **{result['provider']}**")
summ = result["summary"]
cols = st.columns(max(1, len(summ)))
for c, (k, v) in zip(cols, sorted(summ.items())):
    c.markdown(f"{badge(k)}<br><span style='font-size:1.8rem;font-weight:700'>{v}</span>", unsafe_allow_html=True)

groups = {}
for d in result["documents"]:
    if d.get("group"):
        groups.setdefault(d["group"]["group_id"], d["group"]["status"])
if groups:
    st.info("PO groups in this batch: " + " · ".join(f"**{g}** → {s}" for g, s in groups.items())
            + " - see the **PO Groups** page for ordered vs invoiced lines.")

st.subheader("Results")
for d in result["documents"]:
    dec = d["decision"]
    f = (d.get("invoice") or {}).get("fields", {})
    cur = (f.get("currency") or {}).get("value")
    with st.container(border=True):
        a, b, c = st.columns([3, 5, 1.3])
        a.markdown(f"**{d['file_name']}**<br>{badge(dec['decision'])}", unsafe_allow_html=True)
        a.caption(f"{(d.get('vendor') or {}).get('name') or '-'} · {(f.get('invoice_number') or {}).get('value') or 'no invoice no.'} · "
                  f"{money((f.get('grand_total') or {}).get('value'), cur)}")
        ex = d.get("explanation") or {}
        b.markdown(f"**{ex.get('headline', '')}** - {dec['next_action']}")
        b.markdown(chips(dec["reason_codes"]), unsafe_allow_html=True)
        if c.button("Review →", key=f"rv_{d['document_id']}"):
            open_review(d["document_id"])
        with st.expander("Stage timeline"):
            for ev in d.get("stages", []):
                st.markdown(f'<div class="stage-row">{STAGE_ICON.get(ev["status"], "•")} <b>{ev["label"]}</b> - {ev["message"]}</div>',
                            unsafe_allow_html=True)
