"""Verify the configured live LLM provider (LLM_PROVIDER in .env): key, model ID, one structured call.

  python scripts/check_llm.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pydantic import BaseModel  # noqa: E402

from src.agents.providers import get_provider  # noqa: E402
from src.agents.providers.base import ProviderError  # noqa: E402


class Ping(BaseModel):
    ok: bool
    echo: str


try:
    p = get_provider("live_llm")
    print(f"Provider: {p.name} · model: {p.model}")
    try:
        names = p.list_models()
        print(f"{len(names)} models visible. Configured model available: {any(n and n.endswith(p.model) for n in names)}")
    except ProviderError as e:
        print("Could not list models:", e.message)
    r = p.generate(agent="ping", system="You return JSON.", user='Return JSON with ok=true and echo="pong".', schema=Ping)
    print(f"Structured call OK: {r.parsed} · {r.latency_ms} ms · tokens {r.tokens_in}/{r.tokens_out} · model {r.model}")
except ProviderError as e:
    print(f"PROVIDER ERROR ({os.environ.get('LLM_PROVIDER', 'gemini')}):", e.kind, e.message)
    sys.exit(1)
