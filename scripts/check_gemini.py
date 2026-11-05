"""Verify the Gemini key + model ID and make one tiny structured call.  python scripts/check_gemini.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pydantic import BaseModel  # noqa: E402

from src.agents.providers.base import ProviderError  # noqa: E402
from src.agents.providers.gemini import GeminiProvider  # noqa: E402


class Ping(BaseModel):
    ok: bool
    echo: str


try:
    p = GeminiProvider()
    names = p.list_models()
    print(f"Key OK. {len(names)} models visible. Configured model: {p.model}")
    print("Configured model available:", any(n.endswith(p.model) for n in names))
    print("Gemini models:", ", ".join(sorted(n.split("/")[-1] for n in names if "gemini" in n and "flash" in n)))
    r = p.generate(agent="ping", system="Reply with JSON only.", user='Return ok=true and echo="pong".', schema=Ping)
    print(f"Structured call OK: {r.parsed} · {r.latency_ms} ms · tokens {r.tokens_in}/{r.tokens_out} · model {r.model}")
except ProviderError as e:
    print("PROVIDER ERROR:", e.kind, e.message)
    sys.exit(1)
