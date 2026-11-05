"""SQLite persistence for the demo (documents, decisions, PO groups, ledger, traces, audit)."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from src.master_data import consumption_seed, history_seed

DB_PATH = Path(os.environ.get("AP_DB_PATH", Path(__file__).resolve().parents[1] / "storage" / "ap_demo.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, created_at TEXT, mode TEXT, provider TEXT, model TEXT,
    doc_ids TEXT, summary TEXT, label TEXT);
CREATE TABLE IF NOT EXISTS documents (document_id TEXT PRIMARY KEY, batch_id TEXT, correlation_id TEXT, file_name TEXT,
    sha256 TEXT, stored_path TEXT, page_count INTEGER, received_at TEXT, processing_mode TEXT, duration_ms INTEGER,
    record TEXT);
CREATE TABLE IF NOT EXISTS invoices (document_id TEXT PRIMARY KEY, vendor_id TEXT, invoice_number TEXT, invoice_number_norm TEXT,
    invoice_date TEXT, currency TEXT, grand_total TEXT, subtotal TEXT, po_number TEXT, po_match_method TEXT,
    own_blocking INTEGER, service_period TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT, document_id TEXT, source TEXT, vendor_id TEXT,
    doc_type TEXT, invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT, currency TEXT, grand_total TEXT,
    po_number TEXT, file_sha256 TEXT, status TEXT, related_invoice TEXT, service_period TEXT, file_name TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT, version INTEGER, decision TEXT,
    reason_codes TEXT, result TEXT, explanation TEXT, policy_version TEXT, decided_at TEXT, is_current INTEGER, trigger TEXT);
CREATE TABLE IF NOT EXISTS po_groups (group_id TEXT PRIMARY KEY, po_number TEXT, status TEXT, members TEXT, reconciliation TEXT,
    policy_version TEXT, updated_at TEXT, reviewer_action TEXT);
CREATE TABLE IF NOT EXISTS po_consumption (id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, item_code TEXT, quantity TEXT,
    amount TEXT, group_id TEXT, document_id TEXT, source TEXT, accepted_at TEXT, policy_version TEXT, idempotency_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS agent_traces (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT, agent TEXT, mode TEXT, status TEXT,
    provider TEXT, model TEXT, started_at TEXT, latency_ms INTEGER, trace TEXT);
CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, batch_id TEXT, correlation_id TEXT,
    document_id TEXT, actor TEXT, stage TEXT, event_type TEXT, status TEXT, message TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS reviewer_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT, action TEXT, reviewer TEXT,
    comment TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT, field TEXT, original_value TEXT,
    corrected_value TEXT, reviewer TEXT, ts TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx():
    """Atomic transaction (BEGIN IMMEDIATE ... COMMIT / ROLLBACK)."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def init_db(reset: bool = False) -> None:
    with conn() as c:
        if reset:
            for t in ("batches", "documents", "invoices", "history", "decisions", "po_groups", "po_consumption", "agent_traces",
                      "audit_events", "reviewer_actions", "feedback"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript(SCHEMA)
        n = c.execute("SELECT COUNT(*) FROM history WHERE source='seed'").fetchone()[0]
    if n == 0:
        seed()


def seed() -> None:
    from src.pipeline.normalization import norm_invoice_number
    with tx() as c:
        for h in history_seed():
            c.execute("""INSERT INTO history (ref, document_id, source, vendor_id, doc_type, invoice_number, invoice_number_norm,
                invoice_date, currency, grand_total, po_number, file_sha256, status, related_invoice, service_period, file_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (h["invoice_id"], None, "seed", h["vendor_id"], h["doc_type"], h["invoice_number"],
                       norm_invoice_number(h["invoice_number"]), h["invoice_date"], h["currency"], h["grand_total"], h["po_number"],
                       h["file_sha256"] or None, h["status"], h["related_invoice"] or None, h["service_period"] or None, None))
        for r in consumption_seed():
            c.execute("""INSERT OR IGNORE INTO po_consumption (po_number, item_code, quantity, amount, group_id, document_id, source,
                accepted_at, policy_version, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (r["po_number"], r["item_code"], r["quantity"], r["amount"], None, None, r["source"], now(), "seed",
                       r["idempotency_key"]))


def _j(x: Any) -> str:
    return json.dumps(x, default=str)


# ------------------------------------------------------------------ writes
def save_batch(batch_id, mode, provider, model, doc_ids, summary, label=None):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?,?,?,?)",
                  (batch_id, now(), mode, provider, model, _j(doc_ids), _j(summary), label))


def save_document(rec: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (rec["document_id"], rec["batch_id"], rec["correlation_id"], rec["file_name"], rec["sha256"], rec["stored_path"],
                   rec.get("page_count", 0), rec["received_at"], rec["processing_mode"], rec.get("duration_ms"), _j(rec)))


def save_invoice_and_history(rec: dict) -> None:
    """Register an extracted invoice so later documents (even in the same batch) can detect it as a duplicate."""
    inv = rec.get("invoice") or {}
    f = inv.get("fields", {})
    v = lambda k: (f.get(k) or {}).get("value")
    from src.pipeline.normalization import norm_invoice_number
    po = (rec.get("po_match") or {}).get("selected_po")
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rec["document_id"], (rec.get("vendor") or {}).get("vendor_id"), v("invoice_number"),
                   norm_invoice_number(v("invoice_number")), v("invoice_date"), v("currency"), v("grand_total"), v("subtotal"),
                   po, (rec.get("po_match") or {}).get("method"), int(bool(rec.get("own_blocking"))),
                   inv.get("service_period"), now()))
        c.execute("""INSERT INTO history (ref, document_id, source, vendor_id, doc_type, invoice_number, invoice_number_norm,
            invoice_date, currency, grand_total, po_number, file_sha256, status, related_invoice, service_period, file_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rec["document_id"], rec["document_id"], "processed", (rec.get("vendor") or {}).get("vendor_id"), "INVOICE",
                   v("invoice_number"), norm_invoice_number(v("invoice_number")), v("invoice_date"), v("currency"), v("grand_total"),
                   po, rec["sha256"], "PROCESSING", None, inv.get("service_period"), rec["file_name"]))


def register_file_hash_only(rec: dict) -> None:
    with conn() as c:
        c.execute("""INSERT INTO history (ref, document_id, source, doc_type, file_sha256, status, file_name)
            VALUES (?,?,?,?,?,?,?)""", (rec["document_id"], rec["document_id"], "processed", "UNREADABLE", rec["sha256"], "REJECTED",
                                         rec["file_name"]))


def save_decision(c: sqlite3.Connection, document_id: str, result: dict, explanation: Optional[dict], trigger: str) -> int:
    row = c.execute("SELECT COALESCE(MAX(version),0) FROM decisions WHERE document_id=?", (document_id,)).fetchone()
    ver = row[0] + 1
    c.execute("UPDATE decisions SET is_current=0 WHERE document_id=?", (document_id,))
    c.execute("INSERT INTO decisions (document_id, version, decision, reason_codes, result, explanation, policy_version, decided_at, is_current, trigger) VALUES (?,?,?,?,?,?,?,?,1,?)",
              (document_id, ver, result["decision"], _j(result["reason_codes"]), _j(result), _j(explanation), result["policy_version"],
               now(), trigger))
    c.execute("UPDATE history SET status=? WHERE document_id=?", (result["decision"], document_id))
    return ver


def update_explanation(document_id: str, explanation: dict) -> None:
    with conn() as c:
        c.execute("UPDATE decisions SET explanation=? WHERE document_id=? AND is_current=1", (_j(explanation), document_id))


def save_trace(document_id: str, t: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO agent_traces (document_id, agent, mode, status, provider, model, started_at, latency_ms, trace) VALUES (?,?,?,?,?,?,?,?,?)",
                  (document_id, t["agent"], t["mode"], t["status"], t.get("provider"), t.get("model"), t["started_at"],
                   t.get("latency_ms"), _j(t)))


def audit(batch_id, correlation_id, document_id, actor, stage, event_type, status, message, details=None) -> None:
    with conn() as c:
        c.execute("INSERT INTO audit_events (ts, batch_id, correlation_id, document_id, actor, stage, event_type, status, message, details) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (now(), batch_id, correlation_id, document_id, actor, stage, event_type, status, message, _j(details or {})))


def upsert_group(c: sqlite3.Connection, group: dict) -> None:
    c.execute("INSERT OR REPLACE INTO po_groups VALUES (?,?,?,?,?,?,?,?)",
              (group["group_id"], group["po_number"], group["status"], _j(group["members"]), _j(group), group["policy_version"],
               now(), _j(group.get("reviewer_action"))))


def add_consumption(c: sqlite3.Connection, rows: Iterable[dict]) -> None:
    for r in rows:
        c.execute("""INSERT INTO po_consumption (po_number, item_code, quantity, amount, group_id, document_id, source, accepted_at,
            policy_version, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (r["po_number"], r["item_code"], r["quantity"], r["amount"], r.get("group_id"), r.get("document_id"),
                   r.get("source", "accepted group"), now(), r["policy_version"], r["idempotency_key"]))


def reviewer_action(document_id, action, reviewer, comment):
    with conn() as c:
        c.execute("INSERT INTO reviewer_actions (document_id, action, reviewer, comment, ts) VALUES (?,?,?,?,?)",
                  (document_id, action, reviewer, comment, now()))
    audit(None, None, document_id, f"human:{reviewer}", "human_review", "REVIEWER_ACTION", "info", f"{action}: {comment}")


def add_feedback(document_id, field, original, corrected, reviewer):
    with conn() as c:
        c.execute("INSERT INTO feedback (document_id, field, original_value, corrected_value, reviewer, ts) VALUES (?,?,?,?,?,?)",
                  (document_id, field, original, corrected, reviewer, now()))
    audit(None, None, document_id, f"human:{reviewer}", "human_review", "FIELD_CORRECTION", "info",
          f"{field}: '{original}' -> '{corrected}' (stored as labelled feedback; source evidence unchanged)")


# ------------------------------------------------------------------ reads
def rows(sql: str, args: tuple = ()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def history_rows(exclude_document: Optional[str] = None) -> list[dict]:
    return rows("SELECT * FROM history WHERE document_id IS NULL OR document_id != ? ORDER BY id", (exclude_document or "",))


def consumption(po_number: str) -> list[dict]:
    return rows("SELECT * FROM po_consumption WHERE po_number=? ORDER BY id", (po_number,))


def document(document_id: str) -> Optional[dict]:
    r = rows("SELECT * FROM documents WHERE document_id=?", (document_id,))
    if not r:
        return None
    d = json.loads(r[0]["record"])
    d["duration_ms"] = r[0]["duration_ms"]
    return d


def current_decision(document_id: str) -> Optional[dict]:
    r = rows("SELECT * FROM decisions WHERE document_id=? AND is_current=1", (document_id,))
    if not r:
        return None
    x = r[0]
    return {"version": x["version"], "decided_at": x["decided_at"], "trigger": x["trigger"], "result": json.loads(x["result"]),
            "explanation": json.loads(x["explanation"]) if x["explanation"] else None}


def decision_history(document_id: str) -> list[dict]:
    return rows("SELECT version, decision, reason_codes, decided_at, trigger FROM decisions WHERE document_id=? ORDER BY version", (document_id,))


def group_for_po(po_number: str, open_only: bool = True) -> Optional[dict]:
    q = "SELECT * FROM po_groups WHERE po_number=?" + (" AND status != 'ACCEPTED'" if open_only else "") + " ORDER BY updated_at DESC LIMIT 1"
    r = rows(q, (po_number,))
    return json.loads(r[0]["reconciliation"]) if r else None


def all_groups() -> list[dict]:
    return [json.loads(r["reconciliation"]) | {"updated_at": r["updated_at"]} for r in rows("SELECT * FROM po_groups ORDER BY updated_at DESC")]


def traces(document_id: str) -> list[dict]:
    return [json.loads(r["trace"]) for r in rows("SELECT trace FROM agent_traces WHERE document_id=? ORDER BY id", (document_id,))]


def audit_for(document_id: str) -> list[dict]:
    return rows("SELECT * FROM audit_events WHERE document_id=? ORDER BY id", (document_id,))


def dashboard_rows() -> list[dict]:
    return rows("""SELECT d.document_id, d.batch_id, d.file_name, d.received_at, d.processing_mode, d.duration_ms,
        i.vendor_id, i.invoice_number, i.invoice_date, i.currency, i.grand_total, i.po_number,
        x.decision, x.reason_codes, x.version
        FROM documents d LEFT JOIN invoices i ON i.document_id=d.document_id
        LEFT JOIN decisions x ON x.document_id=d.document_id AND x.is_current=1
        ORDER BY d.received_at DESC""")


def batches() -> list[dict]:
    return rows("SELECT * FROM batches ORDER BY created_at DESC")
