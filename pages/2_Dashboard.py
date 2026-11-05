"""Dashboard: history, status and outputs across all runs."""
import json
from decimal import Decimal

import altair as alt
import pandas as pd
import streamlit as st

from src import storage
from src.ui.common import DECISION_STYLE, label, mode_badge, money_short, open_review, setup

setup("Dashboard", "📊")
st.title("📊 Dashboard")
mode_badge()

rows = storage.dashboard_rows()
if not rows:
    st.info("Nothing processed yet. Run a sample scenario from **Home**.")
    st.stop()
df = pd.DataFrame(rows)
df["decision"] = df["decision"].fillna("HOLD_FOR_REVIEW")
df["reasons"] = df["reason_codes"].apply(lambda x: ", ".join(json.loads(x)) if x else "")
vendors = {v.vendor_id: v.name for v in __import__("src.master_data", fromlist=["vendors"]).vendors().values()}
df["vendor"] = df["vendor_id"].map(vendors).fillna("-")

# ---------------- filters (one row)
f1, f2, f3 = st.columns([2, 2, 1])
dec_f = f1.multiselect("Decision", list(DECISION_STYLE), format_func=label)
ven_f = f2.multiselect("Vendor", sorted(df["vendor"].unique()))
mode_f = f3.selectbox("Mode", ["all", "deterministic_demo", "live_llm"])
v = df.copy()
if dec_f:
    v = v[v["decision"].isin(dec_f)]
if ven_f:
    v = v[v["vendor"].isin(ven_f)]
if mode_f != "all":
    v = v[v["processing_mode"] == mode_f]

# ---------------- KPI tiles
n = len(v)
appr = (v["decision"] == "AUTO_APPROVE").sum()
hold = v["decision"].isin(["HOLD_FOR_REVIEW", "PENDING_SPLIT_RECONCILIATION"]).sum()
rej = v["decision"].str.startswith("REJECT").sum()


def total(mask, cur="INR"):
    s = v[mask & (v["currency"] == cur)]["grand_total"].dropna()
    return sum((Decimal(x) for x in s), Decimal("0"))


k = st.columns(6)
k[0].metric("Invoices processed", n)
k[1].metric(f"✅ Auto-approved ({appr / n:.0%})" if n else "✅ Auto-approved", appr)
k[2].metric("✋ Held / ⏳ pending", hold)
k[3].metric("⛔ Rejected", rej)
k[4].metric("Value ready (INR)", money_short(total(v["decision"] == "AUTO_APPROVE")))
k[5].metric("Value on hold (INR)", money_short(total(v["decision"].isin(["HOLD_FOR_REVIEW", "PENDING_SPLIT_RECONCILIATION"]))))
avg = v["duration_ms"].dropna()
st.caption(f"Average processing time {avg.mean() / 1000:.1f}s per invoice · non-INR invoices are counted but not summed across currencies."
           if len(avg) else "")

# ---------------- charts
c1, c2 = st.columns(2)
order = list(DECISION_STYLE)
counts = v.groupby("decision").size().reindex(order).fillna(0).reset_index(name="count")
counts["label"] = counts["decision"].map(label)
counts["color"] = counts["decision"].map(lambda d: DECISION_STYLE[d][0])
bar = alt.Chart(counts).mark_bar(cornerRadiusEnd=4, height=22).encode(
    x=alt.X("count:Q", title="Invoices", axis=alt.Axis(tickMinStep=1, grid=True, gridColor="#e1e0d9")),
    y=alt.Y("label:N", sort=[label(d) for d in order], title=None, axis=alt.Axis(labelLimit=220)),
    color=alt.Color("color:N", scale=None, legend=None),
    tooltip=[alt.Tooltip("label:N", title="Decision"), alt.Tooltip("count:Q", title="Invoices")])
txt = bar.mark_text(align="left", dx=4, color="#52514e").encode(text="count:Q", color=alt.value("#52514e"))
c1.markdown("**Decisions**")
c1.altair_chart((bar + txt).properties(height=200), width="stretch")

reasons = [r for x in v["reason_codes"].dropna() for r in json.loads(x) if r != "ALL_REQUIRED_CHECKS_PASSED"]
if reasons:
    rc = pd.Series(reasons).value_counts().head(8).reset_index()
    rc.columns = ["reason", "count"]
    rbar = alt.Chart(rc).mark_bar(cornerRadiusEnd=4, height=16, color="#2a78d6").encode(
        x=alt.X("count:Q", title="Invoices", axis=alt.Axis(tickMinStep=1, gridColor="#e1e0d9")),
        y=alt.Y("reason:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)), tooltip=["reason", "count"])
    c2.markdown("**Top reason codes**")
    c2.altair_chart(rbar.properties(height=200), width="stretch")

# ---------------- table
st.subheader("All invoices")
st.caption("Select a row to open the full review.")
show = v[["file_name", "vendor", "invoice_number", "invoice_date", "currency", "grand_total", "po_number", "decision", "reasons",
          "processing_mode", "received_at", "document_id"]].copy()
show["decision"] = show["decision"].map(label)
show = show.fillna("-")
sel = st.dataframe(show, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                   column_config={"document_id": st.column_config.TextColumn("doc id", width="small"),
                                  "grand_total": st.column_config.TextColumn("total"),
                                  "received_at": st.column_config.TextColumn("received (UTC)")})
if sel and sel.selection.rows:
    open_review(show.iloc[sel.selection.rows[0]]["document_id"])

st.subheader("Batches")
b = pd.DataFrame(storage.batches())
if len(b):
    b["summary"] = b["summary"].apply(lambda s: " · ".join(f"{label(k)}: {n}" for k, n in json.loads(s).items()))
    b["documents"] = b["doc_ids"].apply(lambda s: len(json.loads(s)))
    st.dataframe(b[["created_at", "label", "mode", "model", "documents", "summary", "batch_id"]], hide_index=True, width="stretch")
