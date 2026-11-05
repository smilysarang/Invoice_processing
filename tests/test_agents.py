"""Agent layer tests with an offline mock provider (no network)."""
import os

import pytest

from src.agents.providers import get_provider
from src.agents.providers.base import ProviderError
from src.agents.providers.mock import MockProvider
from src.orchestrator import process_batch
from src.pipeline.policy_engine import decide
from tests.conftest import load

AGENTS = {"DocumentExtractionAgent", "VendorPOMatchingAgent", "DuplicateRiskAgent", "DecisionExplanationAgent"}


def one(name, provider):
    return process_batch(load(name), mode="live_llm", provider=provider)["documents"][0]


def test_live_mode_calls_every_agent(fresh_db):
    p = MockProvider(responder=lambda agent, user, schema: {"decision_echo": "AUTO_APPROVE", "headline": "h", "explanation": "e",
                                                            "reviewer_checklist": []} if schema.__name__ == "ExplanationOut" else None)
    d = one("01_happy_apex_itemized.pdf", p)
    assert {c["agent"] for c in p.calls} == AGENTS
    traces = fresh_db.traces(d["document_id"])
    assert {t["agent"] for t in traces} == AGENTS and {t["mode"] for t in traces} == {"live_llm"}
    assert all(t["provider"] == "mock" and t["model"] == "mock-model" for t in traces)
    assert "untrusted" in next(c for c in p.calls if c["agent"] == "DocumentExtractionAgent")["system"].lower()


def test_deterministic_mode_makes_no_llm_calls(fresh_db):
    d = process_batch(load("01_happy_apex_itemized.pdf"), mode="deterministic_demo")["documents"][0]
    assert all(t["mode"] == "deterministic_demo" and t["provider"].startswith("none") for t in fresh_db.traces(d["document_id"]))


def test_provider_error_is_visible_and_never_falls_back(fresh_db):
    p = MockProvider(fail_with="RATE_LIMIT_OR_QUOTA")
    d = one("01_happy_apex_itemized.pdf", p)
    assert d["decision"]["decision"] == "HOLD_FOR_REVIEW" and "AGENT_UNAVAILABLE" in d["decision"]["reason_codes"]
    traces = fresh_db.traces(d["document_id"])
    assert all(t["status"] == "error" and t["mode"] == "live_llm" for t in traces)
    assert "RATE_LIMIT_OR_QUOTA" in traces[0]["error"]


def test_live_mode_without_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ProviderError) as e:
        get_provider("live_llm")
    assert e.value.kind == "CONFIG"


def test_extraction_disagreement_routes_to_review(fresh_db):
    def resp(agent, user, schema):
        if schema.__name__ == "LLMExtraction":
            return {"document_type": "invoice", "fields": [{"name": "grand_total", "raw_text": "1,81,000.00", "page": 1}],
                    "line_items": [], "tax_inclusive": False, "references_to_other_documents": [], "suspicious_instructions_found": []}
        if schema.__name__ == "ExplanationOut":
            return {"decision_echo": "HOLD_FOR_REVIEW", "headline": "h", "explanation": "e", "reviewer_checklist": []}
    d = one("01_happy_apex_itemized.pdf", MockProvider(responder=resp))
    assert "EXTRACTION_DISAGREEMENT" in d["decision"]["reason_codes"]


def test_ungrounded_llm_value_is_discarded(fresh_db):
    def resp(agent, user, schema):
        if schema.__name__ == "LLMExtraction":   # hallucinated invoice number for an invoice that has none
            return {"document_type": "invoice", "fields": [{"name": "invoice_number", "raw_text": "SPK-FAKE-9999", "page": 1}],
                    "line_items": [], "tax_inclusive": False, "references_to_other_documents": [], "suspicious_instructions_found": []}
    d = one("24_missing_invoice_number.pdf", MockProvider(responder=resp))
    assert d["invoice"]["fields"]["invoice_number"]["value"] is None
    assert "MISSING_REQUIRED_FIELD" in d["decision"]["reason_codes"]


def test_agent_cannot_override_blocking_rule(fresh_db):
    """Risk agent says PROCEED and explanation agent claims approval - bank mismatch still holds."""
    def resp(agent, user, schema):
        if schema.__name__ == "RiskAssessment":
            return {"duplicate_classification": "none", "risk_flags": [], "recommended_action": "PROCEED", "rationale": "looks fine"}
        if schema.__name__ == "ExplanationOut":
            return {"decision_echo": "AUTO_APPROVE", "headline": "Approved!", "explanation": "all good", "reviewer_checklist": []}
    d = one("11_ec3_bank_change.pdf", MockProvider(responder=resp))
    assert d["decision"]["decision"] == "HOLD_FOR_REVIEW" and "BANK_MISMATCH_HIGH_RISK" in d["decision"]["reason_codes"]
    assert d["explanation"]["source"].startswith("template - LLM explanation rejected")


def test_risk_agent_can_escalate(fresh_db):
    def resp(agent, user, schema):
        if schema.__name__ == "RiskAssessment":
            return {"duplicate_classification": "near", "risk_flags": [{"code": "X", "severity": "HIGH", "evidence": "e"}],
                    "recommended_action": "REVIEW", "rationale": "suspicious"}
        if schema.__name__ == "ExplanationOut":
            return {"decision_echo": "HOLD_FOR_REVIEW", "headline": "h", "explanation": "e", "reviewer_checklist": []}
    d = one("01_happy_apex_itemized.pdf", MockProvider(responder=resp))
    assert "AGENT_RISK_ESCALATION" in d["decision"]["reason_codes"]


def test_policy_engine_precedence():
    f = lambda r, s="ERROR", st="FAIL": {"rule_id": r, "severity": s, "status": st, "message": r}
    assert decide([f("BANK_MISMATCH_HIGH_RISK")]).decision == "HOLD_FOR_REVIEW"
    assert decide([f("DUP_EXACT_FILE"), f("BANK_MISMATCH_HIGH_RISK")]).decision == "REJECT_DUPLICATE"
    assert decide([f("INTAKE_CORRUPT"), f("DUP_EXACT_FILE")]).decision == "REJECT_INVALID"
    assert decide([f("PO_INFERRED", "WARN", "WARN")]).decision == "AUTO_APPROVE"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY_LIVE_TEST"), reason="opt-in: set GEMINI_API_KEY_LIVE_TEST to run")
def test_live_gemini_smoke(fresh_db):
    os.environ["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY_LIVE_TEST"]
    d = process_batch(load("01_happy_apex_itemized.pdf"), mode="live_llm")["documents"][0]
    assert {t["mode"] for t in fresh_db.traces(d["document_id"])} == {"live_llm"}
