"""Google Gemini provider (default live provider; free tier -> synthetic data only)."""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

from src.agents.providers.base import ImagePart, LLMProvider, ProviderError, ProviderResult

_last_call = 0.0
_lock = threading.Lock()


class GeminiProvider(LLMProvider):
    name = "gemini"
    live = True

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
        self.timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
        self.max_retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
        self.min_interval = float(os.environ.get("LLM_MIN_INTERVAL_SECONDS", "8"))  # free-tier pacing (requests/minute limits)
        if not self.api_key:
            raise ProviderError("CONFIG", "GEMINI_API_KEY is not set. Add it to .env or switch to deterministic demo mode.")
        try:
            from google import genai
        except Exception as e:
            raise ProviderError("CONFIG", f"google-genai SDK not installed: {e}")
        self._client = genai.Client(api_key=self.api_key, http_options={"timeout": int(self.timeout * 1000)})

    def list_models(self) -> list[str]:
        return [m.name for m in self._client.models.list()]

    def generate(self, *, agent, system, user, schema, images: Optional[list[ImagePart]] = None) -> ProviderResult:
        from google.genai import errors, types
        global _last_call
        parts = [types.Part.from_text(text=user)] + [types.Part.from_bytes(data=i.data, mime_type=i.mime_type) for i in images or []]
        cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.0, response_mime_type="application/json",
                                          response_json_schema=schema.model_json_schema())
        retries = 0
        while True:
            with _lock:
                wait = self.min_interval - (time.time() - _last_call)
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.time()
            t0 = time.time()
            try:
                resp = self._client.models.generate_content(model=self.model, contents=[types.Content(role="user", parts=parts)], config=cfg)
                text = resp.text or ""
                parsed = schema.model_validate_json(text)
                um = resp.usage_metadata
                return ProviderResult(parsed=parsed, raw_text=text, model=getattr(resp, "model_version", None) or self.model,
                                      provider=self.name, latency_ms=int((time.time() - t0) * 1000),
                                      request_id=getattr(resp, "response_id", None),
                                      tokens_in=getattr(um, "prompt_token_count", None) if um else None,
                                      tokens_out=getattr(um, "candidates_token_count", None) if um else None, retries=retries)
            except errors.APIError as e:
                code = getattr(e, "code", None)
                kind = {400: "BAD_REQUEST", 401: "AUTH", 403: "AUTH", 404: "MODEL_NOT_FOUND", 429: "RATE_LIMIT_OR_QUOTA",
                        503: "MODEL_OVERLOADED", 504: "MODEL_OVERLOADED"}.get(code, "PROVIDER_ERROR")
                msg = str(e)
                m = re.search(r"retryDelay'?\"?:\s*'?\"?([\d.]+)s", msg) or re.search(r"retry in ([\d.]+)s", msg)
                delay = float(m.group(1)) if m else None
                if code == 429 and ("PerDay" in msg or (delay is not None and delay > 90)):
                    # daily free-tier quota: waiting cannot help -> fail fast, provider marked unusable for this batch
                    q = re.search(r"quotaValue'?\"?:\s*'?\"?(\d+)", msg)
                    raise ProviderError("DAILY_QUOTA_EXHAUSTED",
                                        f"Daily free-tier quota for '{self.model}' is used up ({q.group(1) if q else '?'} requests/day; "
                                        f"resets in ~{(delay or 0) / 3600:.1f} h). Switch GEMINI_MODEL to another model in .env "
                                        "(python scripts/check_gemini.py) or use deterministic demo mode.", retries)
                if code in (429, 500, 503, 504) and retries < self.max_retries:
                    retries += 1
                    time.sleep(min(60.0, delay + 1) if (code == 429 and delay is not None) else (20 if code == 429 else 4 * retries))
                    continue
                hint = (f" Model '{self.model}' is busy/timing out - retry later or set GEMINI_MODEL to another model "
                        "(python scripts/check_gemini.py lists them)." if kind == "MODEL_OVERLOADED" else "")
                raise ProviderError(kind, f"HTTP {code}: {msg[:200]}{hint}", retries)
            except Exception as e:
                is_schema = type(e).__name__ == "ValidationError"
                if is_schema and retries < self.max_retries:
                    retries += 1
                    continue
                raise ProviderError("SCHEMA_INVALID" if is_schema else "NETWORK_OR_TIMEOUT", f"{type(e).__name__}: {str(e)[:300]}", retries)
