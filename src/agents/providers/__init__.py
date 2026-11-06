import os

from src.agents.providers.base import LLMProvider, ProviderError
from src.agents.providers.deterministic import DeterministicProvider

SUPPORTED = ("gemini", "groq", "openrouter", "ollama", "openai_compat")


def get_provider(mode: str) -> LLMProvider:
    """mode: 'live_llm' | 'deterministic_demo'. Live mode never falls back silently; the provider is chosen
    explicitly with LLM_PROVIDER and is never switched automatically."""
    if mode == "deterministic_demo":
        return DeterministicProvider()
    name = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if name == "gemini":
        from src.agents.providers.gemini import GeminiProvider
        return GeminiProvider()
    if name in SUPPORTED:
        from src.agents.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(name)
    raise ProviderError("CONFIG", f"LLM_PROVIDER '{name}' is not supported. Use one of: {', '.join(SUPPORTED)}.")
