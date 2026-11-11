"""Shared Streamlit UI helpers: page setup, sidebar (mode, reset), badges, formatting."""
from __future__ import annotations

import hmac
import html
import os
from decimal import Decimal
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

from src import storage  # noqa: E402
from src.agents.providers import ProviderError, get_provider  # noqa: E402

# status palette (reference dataviz instance) - always paired with an icon + text label
DECISION_STYLE = {
    "AUTO_APPROVE": ("#0ca30c", "✅", "Auto-approve", "Ready for next workflow"),
    "PENDING_SPLIT_RECONCILIATION": ("#fab219", "⏳", "Pending split", "Waiting for remaining invoices"),
    "HOLD_FOR_REVIEW": ("#ec835a", "✋", "Hold for review", "A person must decide"),
    "REJECT_DUPLICATE": ("#d03b3b", "⛔", "Reject - duplicate", "Already received"),
    "REJECT_INVALID": ("#d03b3b", "🚫", "Reject - invalid", "Not a processable invoice"),
}
STAGE_ICON = {"running": "⏳", "pass": "✅", "warn": "⚠️", "fail": "⛔", "info": "ℹ️"}
CHECK_ICON = {"PASS": "✅", "WARN": "⚠️", "FAIL": "⛔", "INFO": "ℹ️"}

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1350px;}
.badge {display:inline-block; padding:2px 10px; border-radius:999px; font-weight:600; font-size:0.85rem; color:#fff;}
.badge.warn {color:#0b0b0b;}
.chip {display:inline-block; padding:1px 8px; margin:2px 4px 2px 0; border-radius:6px; font-size:0.78rem;
       background:rgba(120,120,120,0.12); border:1px solid rgba(120,120,120,0.25); font-family:ui-monospace,monospace;}
.mode-live {background:#2a78d6; color:#fff; padding:6px 10px; border-radius:8px; font-weight:700; font-size:0.85rem;}
.mode-demo {background:#52514e; color:#fff; padding:6px 10px; border-radius:8px; font-weight:700; font-size:0.85rem;}
.card {border:1px solid rgba(120,120,120,0.25); border-radius:12px; padding:14px 18px; margin-bottom:10px;}
.card h3 {margin:0 0 4px 0;}
.muted {color: rgba(120,120,120,1); font-size:0.85rem;}
.stage-row {font-size:0.9rem; padding:2px 0;}
</style>
"""


def setup(title: str, icon: str = "🧾"):
    st.set_page_config(page_title=f"{title} · Invoice→Decision", page_icon=icon, layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    storage.init_db()
    sidebar()


def current_mode() -> str:
    if "mode" not in st.session_state:
        st.session_state.mode = os.environ.get("PROCESSING_MODE", "deterministic_demo")
    return st.session_state.mode


def live_locked() -> bool:
    """On a public deployment, LIVE_ACCESS_CODE keeps visitors from spending the LLM quota; demo mode stays open to all."""
    return bool(os.environ.get("LIVE_ACCESS_CODE", "").strip()) and not st.session_state.get("live_unlocked")


def provider_status():
    """Returns (provider or None, error str or None) for the selected mode - never silently falls back."""
    mode = current_mode()
    if mode == "live_llm" and live_locked():
        return None, "LOCKED: live LLM mode needs the access code (sidebar). Deterministic demo mode is open to everyone."
    try:
        return get_provider(mode), None
    except ProviderError as e:
        return None, f"{e.kind}: {e.message}"


def mode_badge(compact: bool = False):
    mode = current_mode()
    if mode == "live_llm":
        p, err = provider_status()
        if err:
            st.markdown('<span class="mode-live">LIVE LLM - NOT READY</span>', unsafe_allow_html=True)
            st.error(err)
        else:
            st.markdown(f'<span class="mode-live">LIVE LLM · {p.name} · {html.escape(p.model or "")}</span>', unsafe_allow_html=True)
    else:
        st.markdown(f'<span class="mode-demo">{"DEMO · NO LLM CALLS" if compact else "DETERMINISTIC DEMO - NO LLM CALLS"}</span>',
                    unsafe_allow_html=True)


def sidebar():
    with st.sidebar:
        st.markdown("### ⚙️ Processing mode")
        cur = current_mode()
        choice = st.radio("Agents run as", ["deterministic_demo", "live_llm"], index=0 if cur == "deterministic_demo" else 1,
                          format_func=lambda m: "Deterministic demo (no LLM)" if m == "deterministic_demo" else f"Live LLM agents ({os.environ.get('LLM_PROVIDER', 'gemini')})",
                          key="mode_radio")
        st.session_state.mode = choice
        if choice == "live_llm" and live_locked():
            code = st.text_input("Access code for live LLM mode", type="password",
                                 help="Live mode calls a rate-limited LLM API, so it is protected on the public demo.")
            if code:
                if hmac.compare_digest(code.strip(), os.environ.get("LIVE_ACCESS_CODE", "").strip()):
                    st.session_state.live_unlocked = True
                    st.rerun()
                st.error("Wrong access code.")
        mode_badge(compact=True)
        st.caption("Deterministic controls (arithmetic, tolerances, duplicates, bank check, policy) run in both modes. "
                   "Only the four agents change.")
        if "pace" not in st.session_state:
            st.session_state.pace = 0.15
        st.slider("Live-view pacing (s per stage)", 0.0, 1.0, key="pace", step=0.05,
                  help="UI pacing only so stages are visible; does not change processing.")
        st.divider()
        st.markdown("### 🧹 Demo state")
        st.caption("Duplicate detection remembers everything processed. Reset before re-running scenarios.")
        if st.button("Reset demo state", width="stretch"):
            storage.init_db(reset=True)
            for k in ("last_batch", "pending_batch", "review_doc"):
                st.session_state.pop(k, None)
            st.success("Database re-seeded (vendor master, POs, history, ledger).")
        st.divider()
        st.caption("Synthetic demo data only. No payment is ever initiated; AUTO_APPROVE = ready for the next workflow step.")


def badge(decision: str | None) -> str:
    if not decision:
        return '<span class="badge" style="background:#888">-</span>'
    color, icon, label, _ = DECISION_STYLE[decision]
    cls = "badge warn" if decision == "PENDING_SPLIT_RECONCILIATION" else "badge"
    return f'<span class="{cls}" style="background:{color}">{icon} {label}</span>'


def chips(codes) -> str:
    return "".join(f'<span class="chip">{html.escape(c)}</span>' for c in codes or [])


def label(decision: str | None) -> str:
    if not decision:
        return "-"
    _, icon, lab, _ = DECISION_STYLE[decision]
    return f"{icon} {lab}"


def money(v, cur: str | None = None) -> str:
    if v in (None, ""):
        return "-"
    d = Decimal(str(v))
    if cur == "INR":
        s = f"{abs(d):.2f}"
        i, f = s.split(".")
        if len(i) > 3:
            head, tail = i[:-3], i[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            i = ",".join(parts) + "," + tail
        return f"{'-' if d < 0 else ''}₹{i}.{f}"
    sym = {"EUR": "€", "USD": "$"}.get(cur or "", (cur + " ") if cur else "")
    return f"{sym}{d:,.2f}"


def money_short(v, cur: str = "INR") -> str:
    d = Decimal(str(v or 0))
    if cur == "INR":
        if abs(d) >= 10_000_000:
            return f"₹{d / 10_000_000:.2f} Cr"
        if abs(d) >= 100_000:
            return f"₹{d / 100_000:.2f} L"
    return money(d, cur)


def open_review(doc_id: str):
    st.session_state.review_doc = doc_id
    st.switch_page("pages/3_Invoice_Review.py")
