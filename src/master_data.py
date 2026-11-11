"""Read-only access to the synthetic procurement system (vendor master + POs) and config."""
from __future__ import annotations

import csv
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from src.models import POLine, PurchaseOrder, Vendor

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CONFIG = ROOT / "config"


@lru_cache
def policy() -> dict:
    return yaml.safe_load((CONFIG / "policy_rules.yaml").read_text(encoding="utf-8"))


@lru_cache
def tolerances() -> dict:
    return yaml.safe_load((CONFIG / "tolerance_rules.yaml").read_text(encoding="utf-8"))


def tol(name: str) -> Decimal:
    return Decimal(str(tolerances()[name]))


@lru_cache
def vendors() -> dict[str, Vendor]:
    out = {}
    with open(DATA / "vendors.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["vendor_id"]] = Vendor(
                vendor_id=r["vendor_id"], name=r["name"], aliases=[a for a in r["aliases"].split("|") if a],
                status=r["status"], approved=r["approved"].lower() == "true", tax_id=r["tax_id"] or None,
                country=r["country"], allowed_currencies=r["allowed_currencies"].split("|"),
                bank_account=r["bank_account"] or None, contact_ref=r["contact_ref"])
    return out


@lru_cache
def purchase_orders() -> dict[str, PurchaseOrder]:
    lines: dict[str, list[POLine]] = {}
    with open(DATA / "po_lines.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lines.setdefault(r["po_number"], []).append(POLine(
                po_number=r["po_number"], line_no=int(r["line_no"]), item_code=r["item_code"], description=r["description"],
                uom=r["uom"], quantity=Decimal(r["quantity"]), unit_price=Decimal(r["unit_price"]),
                tax_rate=Decimal(r["tax_rate"]) if (r.get("tax_rate") or "").strip() else None))
    out = {}
    with open(DATA / "purchase_orders.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["po_number"]] = PurchaseOrder(
                po_number=r["po_number"], vendor_id=r["vendor_id"], currency=r["currency"], status=r["status"],
                po_date=r["po_date"], tax_rate=Decimal(r["tax_rate"]), billing_type=r["billing_type"],
                description=r["description"], allow_bundled_invoice=r["allow_bundled_invoice"] == "true",
                lines=lines.get(r["po_number"], []))
    return out


def history_seed() -> list[dict]:
    with open(DATA / "invoice_history_seed.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def consumption_seed() -> list[dict]:
    with open(DATA / "po_consumption_seed.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))
