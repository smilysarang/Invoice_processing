"""Pipeline orchestrator: runs every stage for each document, emits live stage events, reconciles PO groups
across the batch (and earlier batches), lets the policy engine decide, and commits atomically."""
from __future__ import annotations

import time
import traceback
import uuid
from datetime import datetime
from typing import Callable, Optional

from src import storage
from src.agents.base import mask_sensitive
from src.agents.duplicate_risk_agent import DuplicateRiskAgent
from src.agents.explanation_agent import ExplanationAgent
from src.agents.extraction_agent import ExtractionAgent
from src.agents.providers import get_provider
from src.agents.providers.base import LLMProvider
from src.agents.vendor_po_agent import VendorPOAgent
from src.master_data import policy, purchase_orders
from src.models import ExtractedInvoice
from src.pipeline import duplicates, matching, policy_engine, split_groups, verification
from src.pipeline.extraction_rules import extract
from src.pipeline.intake import intake
from src.pipeline.normalization import mask_account
from src.pipeline.text_acquisition import acquire, ocr_available
from src.pipeline.understanding import understand

STAGES = [
    ("intake", "1 · Intake & security"),
    ("text", "2 · Text acquisition (native / OCR)"),
    ("understanding", "3 · Document understanding"),
    ("extraction", "4 · Extraction - Agent 1"),
    ("normalization", "5 · Normalisation"),
    ("verification", "6 · Verification & grounding"),
    ("vendor_po", "7 · Vendor & PO matching - Agent 2"),
    ("duplicates_risk", "8 · Duplicates & risk - Agent 3"),
    ("po_group", "9 · PO group reconciliation"),
    ("decision", "10 · Policy decision"),
    ("explanation", "11 · Explanation - Agent 4"),
]
STAGE_LABEL = dict(STAGES)

EventCb = Optional[Callable[[dict], None]]


def _fd(f) -> dict:
    return f if isinstance(f, dict) else f.model_dump()


def _status(findings: list[dict]) -> str:
    if any(f["status"] == "FAIL" and f["severity"] == "ERROR" for f in findings):
        return "fail"
    if any(f["status"] in ("WARN", "FAIL") for f in findings):
        return "warn"
    return "pass"


class Run:
    def __init__(self, batch_id: str, provider: LLMProvider, on_event: EventCb):
        self.batch_id, self.provider, self.on_event = batch_id, provider, on_event
        self.mode = "live_llm" if provider.live else "deterministic_demo"

    def emit(self, rec: dict, stage: str, status: str, message: str, details: Optional[dict] = None):
        ev = {"document_id": rec.get("document_id"), "file_name": rec.get("file_name"), "stage": stage, "label": STAGE_LABEL.get(stage, stage),
              "status": status, "message": message, "ts": storage.now(), "details": details or {}}
        rec.setdefault("stages", []).append(ev)
        storage.audit(self.batch_id, rec.get("correlation_id"), rec.get("document_id"), "system", stage, "STAGE", status, message, details)
        if self.on_event:
            self.on_event(ev)

    def running(self, rec, stage):
        if self.on_event:
            self.on_event({"document_id": rec.get("document_id"), "file_name": rec.get("file_name"), "stage": stage,
                           "label": STAGE_LABEL.get(stage, stage), "status": "running", "message": "running...", "ts": storage.now(), "details": {}})

    def agent(self, rec, agent, ctx) -> dict:
        out, trace = agent.run(self.provider, ctx)
        t = trace.model_dump()
        storage.save_trace(rec["document_id"], t)
        rec.setdefault("agents", {})[agent.name] = {"mode": t["mode"], "status": t["status"], "provider": t["provider"], "model": t["model"],
                                                    "latency_ms": t["latency_ms"], "summary": out.get("summary"), "error": t["error"]}
        return out


# ---------------------------------------------------------------------- per document
def process_document(run: Run, file_name: str, data: bytes) -> dict:
    t0 = time.time()
    rec: dict = {"batch_id": run.batch_id, "file_name": file_name, "received_at": storage.now(), "processing_mode": run.mode,
                 "provider": run.provider.name if run.provider.live else None, "model": run.provider.model if run.provider.live else None,
                 "findings": [], "terminal": False}
    try:
        _process(run, rec, file_name, data)
    except Exception as e:  # visible failure, never a silent pass
        rec["findings"].append({"rule_id": "PIPELINE_ERROR", "stage": "pipeline", "severity": "ERROR", "status": "FAIL",
                                "message": f"Unexpected processing error: {type(e).__name__}: {e}"})
        rec["error_trace"] = traceback.format_exc()[-2000:]
        rec["terminal"] = True
        rec.setdefault("document_id", "DOC-ERR-" + uuid.uuid4().hex[:6])
        rec.setdefault("correlation_id", "RUN-ERR")
        rec.setdefault("sha256", "")
        rec.setdefault("stored_path", None)
        run.emit(rec, "pipeline", "fail", f"Processing error: {type(e).__name__}: {e}")
    fails = [f for f in rec["findings"] if f["status"] == "FAIL" and f["severity"] == "ERROR"]
    rec["own_blocking"] = bool([f for f in fails if f["rule_id"] not in policy()["pending_rules"]])
    rec["own_reasons"] = list(dict.fromkeys(f["rule_id"] for f in fails))
    rec["duration_ms"] = int((time.time() - t0) * 1000)
    storage.save_document(rec)
    return rec


def _process(run: Run, rec: dict, file_name: str, data: bytes):
    # 1 intake
    ir = intake(file_name, data)
    rec.update(document_id=ir.document_id, correlation_id=ir.correlation_id, sha256=ir.sha256, stored_path=ir.stored_path,
               page_count=ir.page_count, size=ir.size)
    run.running(rec, "intake")
    f = [_fd(x) for x in ir.findings]
    rec["findings"] += f
    run.emit(rec, "intake", _status(f), f"{ir.size:,} bytes · SHA-256 {ir.sha256[:12]}… · {ir.page_count} page(s)" if ir.ok
             else next(x["message"] for x in f if x["status"] == "FAIL"), {"sha256": ir.sha256})
    if not ir.ok:
        rec["terminal"] = True
        return

    # 2 text acquisition
    run.running(rec, "text")
    pages, notes = acquire(data)
    rec["pages_meta"] = [{"page": p.page, "method": p.method, "quality": round(p.quality, 2), "ocr_confidence": p.ocr_confidence,
                          "note": p.note, "rows": len(p.rows)} for p in pages]
    if not any(p.rows for p in pages):
        ok, err = ocr_available()
        rule = "OCR_UNAVAILABLE" if not ok else "TEXT_UNREADABLE"
        x = {"rule_id": rule, "stage": "text", "severity": "ERROR", "status": "FAIL",
             "message": err if not ok else "No readable text on any page, even after OCR."}
        rec["findings"].append(x)
        run.emit(rec, "text", "fail", x["message"], {"pages": rec["pages_meta"]})
        rec["terminal"] = True
        return
    run.emit(rec, "text", "warn" if any(p.method == "ocr" for p in pages) else "pass", "; ".join(notes), {"pages": rec["pages_meta"]})

    # 3 understanding
    run.running(rec, "understanding")
    und = understand(pages)
    rec["understanding"] = {"doc_type": und.doc_type, "in_scope": und.in_scope, "title": und.title, "locale": und.locale,
                            "page_roles": und.page_roles, "injection_hits": und.injection_hits, "reason": und.reason}
    if not und.in_scope:
        x = {"rule_id": "DOC_OUT_OF_SCOPE", "stage": "understanding", "severity": "ERROR", "status": "FAIL", "message": und.reason}
        rec["findings"].append(x)
        run.emit(rec, "understanding", "fail", und.reason, rec["understanding"])
        rec["terminal"] = True
        return
    loc = und.locale
    run.emit(rec, "understanding", "pass",
             f"{und.doc_type} · language {loc.get('language')} · numbers {loc.get('number_style') or 'n/a'} · dates {loc.get('date_order') or 'undetermined'}"
             f" · scripts {', '.join(loc.get('scripts') or [])}", rec["understanding"])

    # 4 extraction (rules + Agent 1)
    run.running(rec, "extraction")
    rules_inv, ex_notes = extract(pages, und)
    a1 = run.agent(rec, ExtractionAgent(), {"pages": pages, "und": und, "rules_inv": rules_inv, "data": data})
    inv: ExtractedInvoice = a1["invoice"]
    if "error" in a1:
        rec["findings"] += a1["findings"]
    else:
        rec["findings"] += a1.get("findings", [])
    rec["extraction_comparison"] = a1.get("comparison")
    found = sum(1 for x in inv.fields.values() if x.value)
    missing = [k for k, x in inv.fields.items() if not x.value and x.missing_reason]
    run.emit(rec, "extraction", "fail" if "error" in a1 else _status(a1.get("findings", [])),
             f"{found} fields, {len(inv.lines)} line item(s) via {inv.extractor}" + (f" · not found: {', '.join(missing)}" if missing else "")
             + (f" · AGENT ERROR: {a1['error']}" if "error" in a1 else ""),
             {"notes": ex_notes})
    if inv.injection_hits:
        x = {"rule_id": "PROMPT_INJECTION_SUSPECTED", "stage": "understanding", "severity": "ERROR", "status": "FAIL",
             "message": "Document contains instructions aimed at an AI/automation (ignored as data): " + " | ".join(inv.injection_hits[:3])}
        rec["findings"].append(x)

    # 5 normalisation
    run.running(rec, "normalization")
    trans = [{"field": k, "raw": x.raw_text if k != "supplier_bank_account" else mask_sensitive(x.raw_text),
              "normalized": x.value if k != "supplier_bank_account" else mask_account(x.value),
              "method": x.method, "page": x.source_page, "note": "; ".join(x.notes)} for k, x in inv.fields.items() if x.raw_text]
    rec["normalization"] = trans
    amb = [k for k, x in inv.fields.items() if (x.missing_reason or "").startswith("AMBIGUOUS")]
    run.emit(rec, "normalization", "warn" if amb else "pass",
             f"{len(trans)} values normalised (Decimal money, ISO dates, ISO currency)" + (f" · AMBIGUOUS: {', '.join(amb)}" if amb else ""),
             {"transformations": trans})

    # 6 verification
    run.running(rec, "verification")
    vf, calc = verification.verify(inv, pages)
    vf = [_fd(x) for x in vf]
    rec["findings"] += vf
    rec["calc"] = calc
    fails = [x for x in vf if x["status"] == "FAIL"]
    run.emit(rec, "verification", _status(vf), f"{len([x for x in vf if x['status'] == 'PASS'])} passed, {len(fails)} failed"
             + (": " + "; ".join(x["rule_id"] for x in fails) if fails else ""), {"calc": calc})

    # 7 vendor + PO (Agent 2)
    run.running(rec, "vendor_po")
    vm = matching.resolve_vendor(inv)
    vfind = [_fd(x) for x in vm.pop("findings")]
    pm = matching.match_po(inv, vm["vendor_id"], calc)
    pfind = [_fd(x) for x in pm.pop("findings")]
    a2 = run.agent(rec, VendorPOAgent(), {"inv": inv, "vendor_match": vm, "po_match": pm})
    vendor_stage = [x for x in vfind if x["stage"] != "risk"]
    risk_stage = [x for x in vfind if x["stage"] == "risk"]
    rec["findings"] += vendor_stage + pfind + a2.get("findings", [])
    rec["vendor"] = {"vendor_id": vm["vendor_id"], "name": (vm["vendor"] or {}).get("name"), "method": vm["method"],
                     "status": (vm["vendor"] or {}).get("status"), "contact_ref": (vm["vendor"] or {}).get("contact_ref"),
                     "bank_masked": mask_account((vm["vendor"] or {}).get("bank_account"))}
    rec["po_match"] = pm
    rec["agent2"] = {k: a2.get(k) for k in ("questions_for_human", "vendor_rationale", "candidates", "selected_po", "ambiguous")}
    allf = vendor_stage + pfind + a2.get("findings", [])
    run.emit(rec, "vendor_po", _status(allf),
             f"vendor {vm['vendor_id'] or 'NOT FOUND'} ({vm['method'] or '-'}) · PO {pm['selected_po'] or '-'} "
             f"({pm['method'] or ('ambiguous' if pm['ambiguous'] else 'none')})"
             + (f" · {len(pm['candidates'])} candidate(s) scored" if pm['candidates'] else "")
             + (f" · AGENT ERROR: {a2['error']}" if "error" in a2 else ""))

    # 8 duplicates + risk (Agent 3)
    run.running(rec, "duplicates_risk")
    hist = storage.history_rows(exclude_document=rec["document_id"])
    po_type = purchase_orders()[pm["selected_po"]].billing_type if pm["selected_po"] else None
    dup = duplicates.check(rec["sha256"], inv, vm["vendor_id"], pm["selected_po"], hist)
    dfind = [_fd(x) for x in dup.pop("findings")]
    risk_all = risk_stage + [x for x in vendor_stage if x["rule_id"] == "VENDOR_NOT_APPROVED"] + \
        [x for x in rec["findings"] if x["rule_id"] == "PROMPT_INJECTION_SUSPECTED"]
    a3 = run.agent(rec, DuplicateRiskAgent(), {"inv": inv, "dup": dup, "history": hist, "vendor_id": vm["vendor_id"],
                                               "po_number": pm["selected_po"], "po_type": po_type, "risk_findings": risk_all})
    rec["findings"] += dfind + risk_stage + a3.get("findings", [])
    rec["dup"] = dup
    rec["agent3"] = {k: a3.get(k) for k in ("duplicate_classification", "risk_flags", "recommended_action", "rationale")}
    allf = dfind + risk_stage + a3.get("findings", [])
    run.emit(rec, "duplicates_risk", _status(allf),
             f"duplicate: {dup['classification']}" + (f" ({len(dup['matches'])} linked record(s))" if dup["matches"] else "")
             + " · " + "; ".join(f"{x['rule_id']}" for x in risk_stage if x["status"] == "FAIL")
             + (f" · AGENT ERROR: {a3['error']}" if "error" in a3 else ""))

    rec["invoice"] = inv.model_dump()
    ba = rec["invoice"]["fields"].get("supplier_bank_account")
    if ba:  # sensitive: persist masked only (comparison already done against the vendor master)
        ba["raw_text"], ba["value"] = mask_sensitive(ba["raw_text"] or ""), mask_account(ba["value"])
    for row in rec.get("extraction_comparison") or []:
        if row["field"] == "supplier_bank_account":
            row.update(rules=mask_account(row["rules"]), llm=mask_account(row["llm"]), llm_raw=None)
    storage.save_invoice_and_history(rec)


# ---------------------------------------------------------------------- batch
def _member(rec: dict) -> dict:
    return {"document_id": rec["document_id"], "file_name": rec["file_name"],
            "invoice_number": ((rec.get("invoice") or {}).get("fields", {}).get("invoice_number") or {}).get("value"),
            "own_blocking": rec["own_blocking"], "own_reasons": rec["own_reasons"], "po_method": rec["po_match"]["method"],
            "line_mapping": rec["po_match"].get("line_mapping"), "calc": rec["calc"]}


def process_batch(files: list[tuple[str, bytes]], mode: str = "deterministic_demo", on_event: EventCb = None,
                  label: Optional[str] = None, provider: Optional[LLMProvider] = None) -> dict:
    storage.init_db()
    provider = provider or get_provider(mode)   # live misconfiguration raises ProviderError -> shown to the user
    batch_id = "BATCH-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper()
    run = Run(batch_id, provider, on_event)
    recs = [process_document(run, n, d) for n, d in files]
    finalize(run, recs)
    summary = {}
    for r in recs:
        summary[r["decision"]["decision"]] = summary.get(r["decision"]["decision"], 0) + 1
    storage.save_batch(batch_id, run.mode, provider.name if provider.live else None, provider.model if provider.live else None,
                       [r["document_id"] for r in recs], summary, label)
    return {"batch_id": batch_id, "mode": run.mode, "provider": provider.describe(), "documents": recs, "summary": summary}


def finalize(run: Run, recs: list[dict]):
    pos = purchase_orders()
    # pre-decision on own findings (to exclude rejected documents from PO groups)
    for r in recs:
        r["pre"] = policy_engine.decide(r["findings"]).decision
    groups: dict[str, list[dict]] = {}
    standalone = []
    for r in recs:
        po = (r.get("po_match") or {}).get("selected_po")
        if not r["terminal"] and po and pos[po].billing_type == "ONE_TIME" and r["pre"] not in ("REJECT_DUPLICATE", "REJECT_INVALID"):
            groups.setdefault(po, []).append(r)
        else:
            standalone.append(r)

    redecided_prior: list[dict] = []
    for po_no, new_members in groups.items():
        po = pos[po_no]
        existing = storage.group_for_po(po_no, open_only=True)
        prior = []
        if existing:
            for did in existing["members"]:
                if did in {m["document_id"] for m in new_members}:
                    continue
                d = storage.document(did)
                cd = storage.current_decision(did)
                if d and cd and cd["result"]["decision"] not in ("REJECT_DUPLICATE", "REJECT_INVALID"):
                    prior.append(d)
            gid = existing["group_id"]
        else:
            n = len([g for g in storage.all_groups() if g["po_number"] == po_no])
            gid = f"GRP-{po_no}-{n + 1}"
        members = prior + new_members
        g = split_groups.reconcile(po, [_member(m) for m in members], gid)
        for m in new_members:
            run.running(m, "po_group")
            st = {"ACCEPTED": "pass", "INCOMPLETE": "warn", "DISCREPANCY": "fail", "BLOCKED": "fail"}[g["status"]]
            msg = (f"{gid}: {g['status']} · {len(members)} invoice(s) vs {po_no} · invoiced {g['totals']['invoiced_grand_total']} / "
                   f"expected {g['totals']['expected_grand_total']} {po.currency}")
            if g["discrepancies"]:
                msg += " · " + "; ".join(g["discrepancies"][:3])
            elif g["still_needed"]:
                msg += " · still needed: " + "; ".join(g["still_needed"][:2])
            if prior:
                msg += f" · re-reconciled with {len(prior)} invoice(s) from earlier upload(s)"
            run.emit(m, "po_group", st, msg, {"group_id": gid})
            m["group_id"] = gid
        with storage.tx() as c:   # atomic: all member decisions + group + ledger, or nothing
            for m in members:
                res = policy_engine.decide(m["findings"], g, m["document_id"], m["vendor"]["vendor_id"], po_no).model_dump()
                m["decision"] = res
                trig = f"batch {run.batch_id}" if m in new_members else f"group {gid} re-reconciled in batch {run.batch_id}"
                storage.save_decision(c, m["document_id"], res, None, trig)
            if g["status"] == "ACCEPTED":
                storage.add_consumption(c, split_groups.consumption_rows(g, [_member(m) for m in members]))
            storage.upsert_group(c, g)
        for m in prior:
            storage.audit(run.batch_id, m.get("correlation_id"), m["document_id"], "system", "po_group", "GROUP_RECONCILED",
                          "info", f"{gid} re-reconciled with new invoice(s): {g['status']} -> {m['decision']['decision']}")
            redecided_prior.append(m)
        for m in members:
            m["group"] = {"group_id": gid, "status": g["status"]}

    for r in standalone:
        po = (r.get("po_match") or {}).get("selected_po")
        res = policy_engine.decide(r["findings"], None, r["document_id"], (r.get("vendor") or {}).get("vendor_id"), po).model_dump()
        r["decision"] = res
        with storage.tx() as c:
            storage.save_decision(c, r["document_id"], res, None, f"batch {run.batch_id}")
            if res["decision"] == "AUTO_APPROVE" and po and pos[po].billing_type == "BLANKET":
                rows = [{"po_number": po, "item_code": x["item_code"], "quantity": x["quantity"], "amount": x["net_line_total"],
                         "group_id": None, "document_id": r["document_id"], "policy_version": res["policy_version"],
                         "idempotency_key": f"BLANKET:{po}:{r['document_id']}:{x['item_code']}"}
                        for x in (r["po_match"].get("line_mapping") or {}).get("mapped", [])]
                storage.add_consumption(c, rows)

    for r in recs + redecided_prior:
        d = r["decision"]
        if r in recs:
            run.running(r, "decision")
            run.emit(r, "decision", {"AUTO_APPROVE": "pass", "PENDING_SPLIT_RECONCILIATION": "warn"}.get(d["decision"], "fail"),
                     f"{d['decision']} · {', '.join(d['reason_codes'])}", d)
            run.running(r, "explanation")
        f = r.get("invoice", {}).get("fields", {}) if r.get("invoice") else {}
        ctx = {"decision": d, "findings": r["findings"], "invoice_number": (f.get("invoice_number") or {}).get("value"),
               "vendor_name": (r.get("vendor") or {}).get("name"),
               "amounts": {"grand_total": (f.get("grand_total") or {}).get("value"), "currency": (f.get("currency") or {}).get("value")},
               "group_summary": r.get("group")}
        ex = run.agent(r, ExplanationAgent(), ctx)
        ex.pop("findings", None)
        r["explanation"] = ex
        storage.update_explanation(r["document_id"], ex)
        if r in recs:
            run.emit(r, "explanation", "pass" if "error" not in ex else "warn", f"{ex.get('headline')} ({ex.get('source')})")
        storage.save_document(r)
