"""Deterministic demo mode: NO LLM calls. Agents run their rule-based fallback and are labelled as such."""
from src.agents.providers.base import LLMProvider


class DeterministicProvider(LLMProvider):
    name = "deterministic"
    model = None
    live = False

    def generate(self, **kw):  # pragma: no cover - must never be called
        raise RuntimeError("Deterministic demo mode makes no LLM calls.")

    def describe(self) -> str:
        return "DETERMINISTIC DEMO - NO LLM CALLS"
