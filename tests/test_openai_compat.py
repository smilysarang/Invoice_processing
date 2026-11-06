"""OpenAI-compatible adapter (Groq / OpenRouter / Ollama) with a mocked HTTP transport - no network."""
import json

import httpx
import pytest
from pydantic import BaseModel

from src.agents.providers.base import ProviderError
from src.agents.providers.openai_compat import OpenAICompatProvider


class Ping(BaseModel):
    ok: bool
    echo: str


def provider(monkeypatch, handler, name="groq"):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = OpenAICompatProvider(name)
    p._client = httpx.Client(base_url=p.base_url, transport=httpx.MockTransport(handler))
    return p


def ok(content):
    return httpx.Response(200, json={"id": "req-1", "model": "llama-test", "choices": [{"message": {"content": content}}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 3}})


def test_structured_call_and_schema_in_prompt(monkeypatch):
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        return ok('{"ok": true, "echo": "pong"}')
    r = provider(monkeypatch, h).generate(agent="a", system="sys", user="u", schema=Ping)
    assert r.parsed.ok and r.parsed.echo == "pong" and r.provider == "groq" and r.tokens_in == 5 and r.request_id == "req-1"
    assert seen["response_format"]["type"] == "json_schema" and seen["response_format"]["json_schema"]["name"] == "Ping"
    assert '"echo"' in json.dumps(seen["response_format"])          # schema sent once, in response_format
    assert '"echo"' not in seen["messages"][0]["content"]


def test_falls_back_to_json_mode_when_schema_output_unsupported(monkeypatch):
    formats = []

    def h(req):
        body = json.loads(req.content)
        formats.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, text='{"error": {"message": "response_format json_schema is not supported"}}')
        return ok('{"ok": true, "echo": "pong"}')
    p = provider(monkeypatch, h)
    r = p.generate(agent="a", system="s", user="u", schema=Ping)
    assert formats == ["json_schema", "json_object"] and r.parsed.ok and p.response_mode == "json_object"


def test_schema_invalid_is_retried_then_succeeds(monkeypatch):
    calls = iter(['{"ok": "maybe"}', '```json\n{"ok": true, "echo": "x"}\n```'])
    r = provider(monkeypatch, lambda req: ok(next(calls))).generate(agent="a", system="s", user="u", schema=Ping)
    assert r.parsed.echo == "x" and r.retries == 1


def test_schema_invalid_retry_sends_feedback_and_json_failure_is_per_call(monkeypatch):
    bodies = []
    replies = iter([httpx.Response(400, text='{"error": {"code": "json_validate_failed"}}'),
                    ok('{"supplier": "flat"}'), ok('{"ok": true, "echo": "fixed"}'), ok('{"ok": true, "echo": "next"}')])

    def h(req):
        bodies.append(json.loads(req.content))
        return next(replies)
    p = provider(monkeypatch, h)
    r = p.generate(agent="a", system="s", user="u", schema=Ping)
    assert r.parsed.echo == "fixed"
    assert bodies[0]["reasoning_effort"] == "low"
    assert "top-level keys: ok, echo" in bodies[2]["messages"][-1]["content"]      # model told what was wrong
    assert bodies[2]["messages"][-2] == {"role": "assistant", "content": '{"supplier": "flat"}'}
    p.generate(agent="a", system="s", user="u", schema=Ping)
    assert bodies[3]["response_format"]["type"] == "json_schema"                   # downgrade did not stick


def test_reasoning_effort_dropped_if_unsupported(monkeypatch):
    bodies = []

    def h(req):
        bodies.append(json.loads(req.content))
        if "reasoning_effort" in bodies[-1]:
            return httpx.Response(400, text='{"error": {"message": "reasoning_effort is not supported with this model"}}')
        return ok('{"ok": true, "echo": "pong"}')
    p = provider(monkeypatch, h)
    assert p.generate(agent="a", system="s", user="u", schema=Ping).parsed.ok and p.reasoning_effort is None


def test_daily_quota_fails_fast(monkeypatch):
    h = lambda req: httpx.Response(429, json={"error": {"message": "Rate limit reached: requests per day (RPD)"}})
    with pytest.raises(ProviderError) as e:
        provider(monkeypatch, h).generate(agent="a", system="s", user="u", schema=Ping)
    assert e.value.kind == "DAILY_QUOTA_EXHAUSTED"


def test_auth_error_visible(monkeypatch):
    with pytest.raises(ProviderError) as e:
        provider(monkeypatch, lambda req: httpx.Response(401, text="invalid api key")).generate(agent="a", system="s", user="u", schema=Ping)
    assert e.value.kind == "AUTH"


def test_missing_key_is_config_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError) as e:
        OpenAICompatProvider("groq")
    assert e.value.kind == "CONFIG"
