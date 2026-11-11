"""Run the sample scenarios through the real pipeline from the command line.

  python scripts/run_demo.py                 # run all scenarios (fresh demo state), print decisions
  python scripts/run_demo.py --check         # also compare with data/expected_scenarios.json (test-only)
  python scripts/run_demo.py --scenario ec3  # run one scenario
  python scripts/run_demo.py --live          # use the configured live LLM provider (needs GEMINI_API_KEY)
  python scripts/run_demo.py --later-session # demo: split group incomplete in batch 1, completed in batch 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src import storage  # noqa: E402
from src.orchestrator import process_batch  # noqa: E402

ORDER = ["happy_itemized", "happy_bundled", "happy_scanned", "ec1a", "ec1b", "ec2", "ec3", "ec4a", "ec4b", "split_ok", "split_bad",
         "bonus_injection", "missing", "mixed_gst", "tax_per_item", "tax_not_stated", "invalid"]
ICON = {"AUTO_APPROVE": "[APPROVE]", "HOLD_FOR_REVIEW": "[HOLD]   ", "PENDING_SPLIT_RECONCILIATION": "[PENDING]",
        "REJECT_DUPLICATE": "[DUP]    ", "REJECT_INVALID": "[INVALID]"}


def run_scenario(sc: dict, mode: str, verbose: bool) -> list[dict]:
    files = [(f, (ROOT / "data" / "invoices" / f).read_bytes()) for f in sc["files"]]

    def ev(e):
        if verbose and e["status"] != "running":
            print(f"      {e['status']:>4} {e['label']:<38} {e['message'][:150]}")
    res = process_batch(files, mode=mode, on_event=ev, label=sc["title"])
    out = []
    for d in res["documents"]:
        dec = d["decision"]
        print(f"  {ICON[dec['decision']]} {d['file_name']:<42} {dec['decision']:<29} {', '.join(dec['reason_codes'])}")
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--later-session", action="store_true")
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()
    mode = "live_llm" if a.live else "deterministic_demo"
    scen = {s["id"]: s for s in json.loads((ROOT / "data" / "sample_scenarios.json").read_text(encoding="utf-8"))}
    if not a.no_reset:
        storage.init_db(reset=True)
    if a.later_session:
        for sid in ("split_step1", "split_step2"):
            print(f"\n== {scen[sid]['title']}")
            run_scenario(scen[sid], mode, a.verbose)
        print("\nFinal decisions for the group:")
        for r in storage.dashboard_rows():
            print(f"  {r['file_name']:<30} {r['decision']:<29} v{r['version']}")
        return 0
    ids = [a.scenario] if a.scenario else ORDER
    results = {}
    for sid in ids:
        print(f"\n== {scen[sid]['title']}")
        for d in run_scenario(scen[sid], mode, a.verbose):
            results[d["file_name"]] = d["decision"]
    if a.check:
        exp = json.loads((ROOT / "data" / "expected_scenarios.json").read_text(encoding="utf-8"))
        bad = 0
        print("\n== Check against expected outcomes (test-only manifest)")
        for f, e in exp.items():
            if f not in results:
                continue
            got = results[f]
            ok = got["decision"] == e["decision"] and all(r in got["reason_codes"] for r in e.get("reasons_include", []))
            bad += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {f:<42} expected {e['decision']} {e.get('reasons_include', [])}  got {got['decision']} {got['reason_codes']}")
        print(f"\n{len([f for f in exp if f in results]) - bad}/{len([f for f in exp if f in results])} scenarios match.")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
