"""PO split-billing view: ordered lines vs aggregated invoice lines, all-or-none group status."""
from decimal import Decimal

import pandas as pd
import streamlit as st

from src import storage
from src.master_data import purchase_orders, vendors
from src.pipeline.matching import remaining_value
from src.ui.common import badge, label, mode_badge, money, open_review, setup

setup("PO groups", "🧩")
st.title("🧩 PO groups - all-or-none reconciliation")
mode_badge()
st.caption("Every invoice that references the same one-time PO is reconciled together. The group is accepted only when ALL invoices "
           "together match every PO line, quantity, unit price, subtotal, tax and total. Until then, none of them is accepted and "
           "nothing is consumed from the PO.")

groups = storage.all_groups()
if not groups:
    st.info("No PO groups yet. Run **Split PO - valid 3-invoice set** or **failing set** from Home.")
    st.stop()
if st.toggle("Split groups only (2+ invoices)", value=any(len(g["members"]) > 1 for g in groups)):
    groups = [g for g in groups if len(g["members"]) > 1] or groups
STATUS = {"ACCEPTED": ("✅", "#0ca30c"), "INCOMPLETE": ("⏳", "#fab219"), "DISCREPANCY": ("⛔", "#d03b3b"), "BLOCKED": ("✋", "#ec835a")}
gid = st.selectbox("Group", [g["group_id"] for g in groups],
                   format_func=lambda i: next(f"{STATUS[g['status']][0]} {g['group_id']} · {g['status']} · {len(g['members'])} invoice(s)"
                                              for g in groups if g["group_id"] == i))
g = next(x for x in groups if x["group_id"] == gid)
po = purchase_orders()[g["po_number"]]
cur = po.currency

with st.container(border=True):
    a, b, c, d = st.columns(4)
    icon, col = STATUS[g["status"]]
    a.markdown(f"<span class='badge' style='background:{col};{'color:#0b0b0b' if g['status'] == 'INCOMPLETE' else ''}'>{icon} {g['status']}</span>",
               unsafe_allow_html=True)
    a.caption(f"{po.po_number} · {vendors()[po.vendor_id].name}")
    b.metric("Expected (PO, incl. tax)", money(g["totals"]["expected_grand_total"], cur))
    diff = Decimal(g["totals"]["invoiced_grand_total"]) - Decimal(g["totals"]["expected_grand_total"])
    c.metric("Invoiced (all members)", money(g["totals"]["invoiced_grand_total"], cur))
    c.caption("✅ exact match" if not diff else f"⛔ difference {diff:+,.2f}")
    d.metric("PO remaining, net of tax (accepted groups only)", money(remaining_value(po), cur))
    st.caption(f"{po.description} · billing {po.billing_type} · tax {po.tax_rate * 100:.0f}% · tolerance {cur} {g['tolerance']} · "
               f"policy {g['policy_version']} · reconciled {g['reconciled_at']}")

if g["discrepancies"]:
    st.error("**Discrepancies - no invoice in this group can be accepted:**\n" + "\n".join(f"- {x}" for x in g["discrepancies"]))
if g["still_needed"]:
    st.warning("**Still needed before the group can be accepted:**\n" + "\n".join(f"- {x}" for x in g["still_needed"]))
if g["status"] == "BLOCKED":
    st.warning("Blocked by a member that failed its own checks: " + ", ".join(g["blocking_members"]))
if g["status"] == "ACCEPTED":
    st.success("All members reconcile exactly - accepted together in one transaction; PO consumption recorded per invoice.")

st.markdown("#### PO ordered lines vs aggregated invoice lines")
SYM = {"MATCH": "✅ match", "UNDER": "⏳ under-billed", "OVER": "⛔ over-billed", "PRICE_MISMATCH": "⛔ price mismatch", "BUNDLED": "ℹ️ bundled"}
st.dataframe(pd.DataFrame([{
    "item": l["item_code"], "description": l["description"], "ordered": l["ordered_qty"], "already accepted": l["previously_accepted_qty"],
    "expected now": l["expected_qty"], "invoiced": l["invoiced_qty"], "by invoice": ", ".join(f"{k}: {v}" for k, v in l["by_invoice"].items()),
    "PO unit price": l["po_unit_price"], "invoice unit price(s)": ", ".join(l["invoice_unit_prices"]) or "-",
    "expected value": l["expected_value"], "invoiced value": l["invoiced_value"], "result": SYM.get(l["status"], l["status"])}
    for l in g["lines"]]), hide_index=True, width="stretch")

t = g["totals"]
st.dataframe(pd.DataFrame([
    {"": "Subtotal (net)", "expected": t["expected_subtotal"], "invoiced": t["invoiced_subtotal"]},
    {"": "Tax", "expected": t["expected_tax"], "invoiced": t["invoiced_tax"]},
    {"": "Grand total", "expected": t["expected_grand_total"], "invoiced": t["invoiced_grand_total"]}]), hide_index=True)

st.markdown("#### Member invoices")
for m in g["member_details"]:
    cd = storage.current_decision(m["document_id"])
    dec = cd["result"]["decision"] if cd else None
    with st.container(border=True):
        x, y, z = st.columns([3, 3, 1])
        x.markdown(f"**{m['file_name']}** · {m['invoice_number']}<br>{badge(dec)}", unsafe_allow_html=True)
        y.caption(f"total {money(m['grand_total'], cur)} · net {money(m['net_subtotal'], cur)} · PO via {m['po_method']}"
                  + (f" · own blocking issues: {', '.join(m['own_reasons'])}" if m["own_blocking"] else "")
                  + (f" · decision v{cd['version']} ({cd['trigger']})" if cd else ""))
        if z.button("Review →", key=f"g_{m['document_id']}"):
            open_review(m["document_id"])

st.markdown("#### PO consumption ledger")
led = storage.consumption(po.po_number)
if led:
    st.dataframe(pd.DataFrame(led)[["accepted_at", "item_code", "quantity", "amount", "group_id", "document_id", "source", "idempotency_key"]],
                 hide_index=True, width="stretch")
else:
    st.caption("No consumption recorded - only fully accepted groups consume the PO.")
