"""OpenAI-compatible chat-completions provider.

One adapter covers several free / local options that expose the same API:
  groq        https://api.groq.com/openai/v1      (GROQ_API_KEY)
  openrouter  https://openrouter.ai/api/v1        (OPENROUTER_API_KEY, use a ':free' model)
  ollama      http://localhost:11434/v1           (local, no key, no quota)
  openai_compat  any other endpoint via LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

Structured output: JSON mode + the agent's JSON schema in the system prompt, validated with
Pydantic in code (schema-invalid output is retried once, then surfaced as an error).
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Optional

import httpx

from src.agents.providers.base import ImagePart, LLMProvider, ProviderError, ProviderResult

PRESETS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", None),
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/v1", None, "OLLAMA_MODEL", "qwen2.5:7b"),
    "openai_compat": (os.environ.get("LLM_BASE_URL", ""), "LLM_API_KEY", "LLM_MODEL", None),
}

_last_call = 0.0
_lock = threading.Lock()
_tok = {"remaining": None, "reset_at": 0.0}


def _duration(s: str) -> float:
    """'30.772s' / '1m26.4s' / '585ms' -> seconds."""
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s or ""):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _record_limits(headers) -> None:
    rem, reset = headers.get("x-ratelimit-remaining-tokens"), headers.get("x-ratelimit-reset-tokens")
    if rem is not None and rem.isdigit():
        _tok["remaining"] = int(rem)
        _tok["reset_at"] = time.time() + _duration(reset)


class OpenAICompatProvider(LLMProvider):
    live = True

    def __init__(self, name: str):
        if name not in PRESETS:
            raise ProviderError("CONFIG", f"Unknown provider '{name}'.")
        base, key_env, model_env, default_model = PRESETS[name]
        self.name = name
        self.base_url = base.rstrip("/")
        self.api_key = os.environ.get(key_env) if key_env else None
        self.model = os.environ.get(model_env) or default_model
        self.timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "45"))
        self.max_retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
        self.min_interval = float(os.environ.get("LLM_MIN_INTERVAL_SECONDS", "2"))
        self.vision = os.environ.get("LLM_VISION", "false").lower() == "true"
        self.response_mode = os.environ.get("LLM_RESPONSE_FORMAT", "json_schema")   # json_schema | json_object
        # reasoning models (gpt-oss, qwen3): low effort = far fewer hidden tokens -> faster and gentler on tokens/min
        self.reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "low" if name == "groq" else "").strip() or None
        if not self.base_url:
            raise ProviderError("CONFIG", "LLM_BASE_URL is not set.")
        if key_env and not self.api_key:
            raise ProviderError("CONFIG", f"{key_env} is not set. Add it to .env or switch to deterministic demo mode.")
        if not self.model:
            raise ProviderError("CONFIG", f"{model_env} is not set (choose a model; for OpenRouter use one ending in ':free').")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if name == "openrouter":
            headers["X-Title"] = "Invoice-to-Decision demo"
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=self.timeout)

    def list_models(self) -> list[str]:
        try:
            r = self._client.get("/models")
            r.raise_for_status()
            return [m.get("id") for m in r.json().get("data", [])]
        except httpx.HTTPError as e:
            raise ProviderError("NETWORK_OR_TIMEOUT", f"{type(e).__name__}: {e}")

    def generate(self, *, agent, system, user, schema, images: Optional[list[ImagePart]] = None) -> ProviderResult:
        global _last_call
        schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        content: object = user
        if images and self.vision:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:{i.mime_type};base64,{base64.b64encode(i.data).decode()}"}}
                for i in images]
        body = {"model": self.model, "temperature": 0}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        top_keys = ", ".join(schema.model_fields)
        repair: list[dict] = []          # feedback turns added after a schema-invalid answer
        mode = self.response_mode        # per-call; a provider-side JSON failure only downgrades THIS call
        retries = 0
        while True:
            # structured outputs (JSON schema, non-strict) where supported; plain JSON mode otherwise.
            # The schema travels once: in response_format (json_schema mode) or in the prompt (json_object mode).
            sys_text = (f"{system}\n\nRespond ONLY with one JSON object (no prose, no markdown) whose top-level keys are: {top_keys}."
                        + ("" if mode == "json_schema" else f" It must conform to this JSON Schema:\n{schema_text}"))
            body["messages"] = [{"role": "system", "content": sys_text}, {"role": "user", "content": content}] + repair
            body["response_format"] = ({"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                                                                              "strict": False}}
                                       if mode == "json_schema" else {"type": "json_object"})
            with _lock:
                wait = self.min_interval - (time.time() - _last_call)
                # token-per-minute budget (from the provider's rate-limit headers): wait for the window to reset
                # instead of sending a call that would be rejected
                need = len(json.dumps(body)) / 3.5 + 1500
                if _tok["remaining"] is not None and _tok["remaining"] < need and _tok["reset_at"] > time.time():
                    wait = max(wait, min(60.0, _tok["reset_at"] - time.time() + 0.5))
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.time()
            t0 = time.time()
            try:
                r = self._client.post("/chat/completions", json=body)
                _record_limits(r.headers)
            except httpx.HTTPError as e:
                if retries < self.max_retries:
                    retries += 1
                    time.sleep(3 * retries)
                    continue
                hint = " Is Ollama running (ollama serve)?" if self.name == "ollama" else ""
                raise ProviderError("NETWORK_OR_TIMEOUT", f"{type(e).__name__}: {str(e)[:200]}{hint}", retries)
            if r.status_code >= 400:
                text = r.text[:400]
                code = r.status_code
                if code == 400 and "reasoning" in text.lower() and body.pop("reasoning_effort", None):
                    self.reasoning_effort = None      # this model has no reasoning-effort control; drop it for good
                    continue
                if code == 400 and ("json_validate_failed" in text or "Failed to validate JSON" in text or "Failed to generate JSON" in text) \
                        and retries < self.max_retries:
                    retries += 1                      # provider-side schema check failed once: retry THIS call in plain JSON
                    mode = "json_object"              # mode, where our own Pydantic validation applies
                    continue
                if code == 400 and mode == "json_schema" and ("response_format" in text or "json_schema" in text):
                    self.response_mode = mode = "json_object"   # model does not support JSON-schema output at all
                    continue
                ra = r.headers.get("retry-after")
                delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                if code == 429 and ("per day" in text.lower() or "daily" in text.lower() or "rpd" in text.lower() or (delay or 0) > 90):
                    raise ProviderError("DAILY_QUOTA_EXHAUSTED", f"{self.name}/{self.model}: daily free quota used up. {text[:200]}", retries)
                kind = {400: "BAD_REQUEST", 401: "AUTH", 403: "AUTH", 404: "MODEL_NOT_FOUND", 413: "REQUEST_TOO_LARGE",
                        429: "RATE_LIMIT_OR_QUOTA"}.get(code, "MODEL_OVERLOADED" if code >= 500 else "PROVIDER_ERROR")
                if code in (429, 500, 502, 503, 504) and retries < self.max_retries + (2 if code == 429 else 0):
                    retries += 1
                    time.sleep(min(60.0, (delay or 0) + 1) if delay else (10 if code == 429 else 3 * retries))
                    continue
                raise ProviderError(kind, f"HTTP {code}: {text}", retries)
            data = r.json()
            raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            try:
                parsed = schema.model_validate_json(raw)
            except Exception as e:
                if retries < self.max_retries:
                    retries += 1     # tell the model what was wrong instead of repeating the identical request
                    repair = [{"role": "assistant", "content": raw[:1500]},
                              {"role": "user", "content": f"That JSON does not match the required schema ({str(e)[:400]}). "
                                                          f"Return ONE JSON object with exactly these top-level keys: {top_keys}. "
                                                          f"Schema: {schema_text}"}]
                    continue
                raise ProviderError("SCHEMA_INVALID", f"{type(e).__name__}: {str(e)[:300]}", retries)
            usage = data.get("usage") or {}
            return ProviderResult(parsed=parsed, raw_text=raw, model=data.get("model") or self.model, provider=self.name,
                                  latency_ms=int((time.time() - t0) * 1000), request_id=data.get("id"),
                                  tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"), retries=retries,
                                  meta={"response_format": mode, "reasoning_effort": self.reasoning_effort})
