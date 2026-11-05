import os

from src.agents.providers.base import LLMProvider, ProviderError
from src.agents.providers.deterministic import DeterministicProvider


def get_provider(mode: str) -> LLMProvider:
    """mode: 'live_llm' | 'deterministic_demo'. Live mode never falls back silently."""
    if mode == "deterministic_demo":
        return DeterministicProvider()
    name = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if name == "gemini":
        from src.agents.providers.gemini import GeminiProvider
        return GeminiProvider()
    raise ProviderError("CONFIG", f"LLM_PROVIDER '{name}' is not implemented in this build (gemini only; see README next steps).")
