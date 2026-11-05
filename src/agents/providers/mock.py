"""Offline mock provider for tests: records every call and returns schema-valid canned output."""
from __future__ import annotations

from typing import Callable, Optional

from src.agents.providers.base import LLMProvider, ProviderError, ProviderResult


class MockProvider(LLMProvider):
    name = "mock"
    model = "mock-model"
    live = True

    def __init__(self, responder: Optional[Callable] = None, fail_with: Optional[str] = None):
        self.calls: list[dict] = []
        self.responder = responder
        self.fail_with = fail_with

    def generate(self, *, agent, system, user, schema, images=None):
        self.calls.append({"agent": agent, "system": system, "user": user, "schema": schema.__name__, "images": len(images or [])})
        if self.fail_with:
            raise ProviderError(self.fail_with, f"mock {self.fail_with}")
        data = self.responder(agent, user, schema) if self.responder else None
        parsed = schema.model_validate(data if data is not None else default_for(schema))
        return ProviderResult(parsed=parsed, raw_text=parsed.model_dump_json(), model=self.model, provider=self.name, latency_ms=1,
                              request_id=f"mock-{len(self.calls)}", tokens_in=10, tokens_out=10)


def default_for(schema) -> dict:
    name = schema.__name__
    if name == "LLMExtraction":
        return {"document_type": "invoice", "fields": [], "line_items": [], "tax_inclusive": False,
                "references_to_other_documents": [], "suspicious_instructions_found": [], "notes": None}
    if name == "VendorPOAssessment":
        return {"vendor_consistent": True, "vendor_rationale": "mock", "candidates": [], "selected_po": None, "ambiguous": False,
                "questions_for_human": []}
    if name == "RiskAssessment":
        return {"duplicate_classification": "none", "risk_flags": [], "recommended_action": "PROCEED", "rationale": "mock"}
    if name == "ExplanationOut":
        return {"decision_echo": "", "headline": "mock", "explanation": "mock", "reviewer_checklist": [], "vendor_message_draft": None}
    raise ValueError(name)
