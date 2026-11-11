"""Rules, master data, provider setup and assumptions."""
import pandas as pd
import streamlit as st

from src.master_data import CONFIG, ROOT, purchase_orders, vendors
from src.pipeline.normalization import mask_account
from src.pipeline.text_acquisition import ocr_available
from src.ui.common import current_mode, mode_badge, provider_status, setup

setup("Rules & data", "📚")
st.title("📚 Rules, data & setup")
mode_badge()

t1, t2, t3, t4 = st.tabs(["Policy & tolerances", "Vendor master & POs", "Setup check", "Assumptions"])
with t1:
    a, b = st.columns(2)
    a.markdown("**config/policy_rules.yaml**")
    a.code((CONFIG / "policy_rules.yaml").read_text(encoding="utf-8"), language="yaml")
    b.markdown("**config/tolerance_rules.yaml**")
    b.code((CONFIG / "tolerance_rules.yaml").read_text(encoding="utf-8"), language="yaml")
    st.caption("Every decision stores the policy version it was made under. Change a value and re-run a scenario to see the effect.")
with t2:
    st.markdown("**Vendor master** (read-only to agents; bank accounts masked)")
    st.dataframe(pd.DataFrame([{"id": v.vendor_id, "name": v.name, "status": v.status, "approved": v.approved, "tax id": v.tax_id,
                                "currencies": ", ".join(v.allowed_currencies), "bank (verified)": mask_account(v.bank_account),
                                "registered contact": v.contact_ref} for v in vendors().values()]), hide_index=True, width="stretch")
    st.markdown("**Purchase orders**")
    rows = []
    for p in purchase_orders().values():
        for l in p.lines:
            rows.append({"PO": p.po_number, "vendor": p.vendor_id, "type": p.billing_type, "date": p.po_date, "currency": p.currency,
                         "tax": f"{p.line_rate(l) * 100:.4g}%", "item": l.item_code, "description": l.description, "qty": str(l.quantity),
                         "uom": l.uom, "unit price": str(l.unit_price), "PO subtotal": str(p.subtotal)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
with t3:
    ok, err = ocr_available()
    st.markdown(f"**OCR engine (RapidOCR):** {'✅ available' if ok else '⛔ ' + str(err)}")
    p, perr = provider_status()
    st.markdown(f"**Selected mode:** `{current_mode()}`")
    if current_mode() == "live_llm":
        if perr:
            st.error(perr)
        else:
            st.success(f"Provider ready: {p.name} / {p.model}")
            if st.button("List available models (verifies key + model ID)"):
                try:
                    models = p.list_models()
                    st.write(f"{len(models)} models. Configured model present: "
                             f"{'✅' if any(m.endswith(p.model) for m in models) else '⛔ NOT FOUND - set GEMINI_MODEL'}")
                    st.code("\n".join(sorted(m for m in models if m)))
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")
    st.info("Free-tier cloud APIs may use submitted content to improve the provider's products: use synthetic invoices only.")
with t4:
    st.markdown((ROOT / "ASSUMPTIONS.md").read_text(encoding="utf-8") if (ROOT / "ASSUMPTIONS.md").exists() else "ASSUMPTIONS.md missing")
