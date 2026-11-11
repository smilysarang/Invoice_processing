"""Invoice -> Decision · AP copilot (home). Run:  streamlit run Home.py"""
import hashlib
import json

import streamlit as st

from src import storage
from src.ui.common import ROOT, setup

setup("Home")

st.title("🧾 Invoice → Decision")
st.markdown("Upload vendor invoices (PDF). Each one is read, matched to the vendor and purchase order, checked for duplicates "
            "and payment risk, and gets a **clear, reasoned decision** with every step visible. "
            "**No payment is ever made** - *Auto-approve* means *ready for the next approval step*.")

c1, c2, c3, c4 = st.columns(4)
c1.markdown("**✅ Auto-approve**  \nall controls passed")
c2.markdown("**✋ Hold for review**  \na person must decide - reasons shown")
c3.markdown("**⏳ Pending split**  \nPO billed in parts; waits for the rest")
c4.markdown("**⛔ Reject**  \nconfirmed duplicate or not an invoice")
st.divider()

scen = json.loads((ROOT / "data" / "sample_scenarios.json").read_text(encoding="utf-8"))
inv_dir = ROOT / "data" / "invoices"

left, right = st.columns(2, gap="large")
with left:
    st.subheader("📤 Upload my invoices")
    ups = st.file_uploader("Drop one or more invoice PDFs (several invoices for the same PO are reconciled together)",
                           type=None, accept_multiple_files=True, help="File type is verified from the bytes, not the extension.")
    if st.button("Process uploaded invoices", type="primary", disabled=not ups, width="stretch"):
        st.session_state.pending_batch = {"files": [(u.name, u.getvalue()) for u in ups], "label": f"Upload ({len(ups)} file(s))"}
        st.switch_page("pages/1_Live_Run.py")

with right:
    st.subheader("🎬 Run a sample scenario")
    ids = [s["id"] for s in scen]
    sid = st.selectbox("Scenario", ids, format_func=lambda i: next(s["title"] for s in scen if s["id"] == i))
    sc = next(s for s in scen if s["id"] == sid)
    st.caption(sc["about"])
    st.markdown("Files: " + " ".join(f"`{f}`" for f in sc["files"]))
    hist = storage.history_rows()
    retry_ids = storage.technical_hold_ids()
    seen = {h["file_sha256"] for h in hist if h.get("file_sha256") and h.get("document_id") not in retry_ids}
    retry = {h["file_sha256"] for h in hist if h.get("file_sha256") and h.get("document_id") in retry_ids}
    hashes = {f: hashlib.sha256((inv_dir / f).read_bytes()).hexdigest() for f in sc["files"]}
    already = [f for f in sc["files"] if hashes[f] in seen]
    retrying = [f for f in sc["files"] if hashes[f] in retry and f not in already]
    if already:
        st.warning(f"{len(already)} of these files were already processed, so they will (correctly) be rejected as duplicates. "
                   "Use **Reset & run** for a clean demo.")
    if retrying:
        st.info(f"{len(retrying)} of these files were held earlier only for a technical reason (e.g. the LLM was unavailable). "
                "Running again re-processes them; they are not treated as duplicates.")
    b1, b2 = st.columns(2)
    run_it = b1.button("Run this scenario", type="primary", width="stretch")
    if b2.button("Reset & run", width="stretch", help="Re-seed the demo database, then run"):
        storage.init_db(reset=True)
        run_it = True
    if run_it:
        st.session_state.pending_batch = {"files": [(f, (inv_dir / f).read_bytes()) for f in sc["files"]], "label": sc["title"]}
        st.switch_page("pages/1_Live_Run.py")
    with st.expander("…or pick individual sample PDFs"):
        all_files = sorted(p.name for p in inv_dir.glob("*.pdf"))
        picked = st.multiselect("Sample invoices", all_files)
        if st.button("Run selected samples", disabled=not picked):
            st.session_state.pending_batch = {"files": [(f, (inv_dir / f).read_bytes()) for f in picked], "label": f"Samples ({len(picked)})"}
            st.switch_page("pages/1_Live_Run.py")

st.divider()
with st.expander("How it works (11 stages)", expanded=False):
    st.markdown("""
1. **Intake & security** - real file type from magic bytes, corrupt/encrypted detection, SHA-256.
2. **Text acquisition** - native PDF text when usable, OCR (RapidOCR, 300 dpi, deskew) for scanned pages.
3. **Understanding** - document type, language, number & date conventions, instruction-like text.
4. **Extraction - Agent 1** - every field with raw text, page, box and confidence; in live mode an LLM reads it too and the two readings are cross-checked.
5. **Normalisation** - Decimal money (lakh / German formats), ISO dates (ambiguous ones are held, not guessed), ISO currency.
6. **Verification** - grounding, required fields, line maths, totals, tax.
7. **Vendor & PO - Agent 2** - vendor master, bank check, PO hierarchy (exact → inferred → ambiguous), line mapping.
8. **Duplicates & risk - Agent 3** - file hash, business key, reissue, credit-rebill, recurring vs near-duplicate.
9. **PO group** - all invoices for one PO are reconciled together, **all-or-none**.
10. **Policy decision** - versioned deterministic rules. Agents may escalate, never approve.
11. **Explanation - Agent 4** - plain-language reason, checklist, vendor note draft.
""")
