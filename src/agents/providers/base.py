"""Shared LLM provider interface. One provider serves all four agents (each with its own prompt/schema)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Type

from pydantic import BaseModel


class ProviderError(Exception):
    """Visible provider failure (auth, quota, network, schema). Never silently converted to demo mode."""

    def __init__(self, kind: str, message: str, retries: int = 0):
        super().__init__(f"{kind}: {message}")
        self.kind, self.message, self.retries = kind, message, retries


@dataclass
class ImagePart:
    data: bytes
    mime_type: str = "image/png"


@dataclass
class ProviderResult:
    parsed: BaseModel
    raw_text: str
    model: str
    provider: str
    latency_ms: int
    request_id: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    retries: int = 0
    meta: dict = field(default_factory=dict)


class LLMProvider:
    name = "base"
    model: Optional[str] = None
    live = True

    def generate(self, *, agent: str, system: str, user: str, schema: Type[BaseModel],
                 images: Optional[list[ImagePart]] = None) -> ProviderResult:
        raise NotImplementedError

    def describe(self) -> str:
        return f"LIVE LLM - {self.name} / {self.model}"
