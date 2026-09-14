"""Structured results returned by the ModelRouter.

These are the only shapes the rest of the system sees from the LLM layer —
agents and the engine never touch raw gateway responses.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelResult(BaseModel):
    """Result of a chat completion."""

    ok: bool
    task: str
    model: str
    content: str | None = None
    raw: Any | None = None
    parsed_json: Any | None = Field(
        default=None,
        description="If output_schema was requested and extraction succeeded, "
        "the parsed JSON object; else None.",
    )
    prompt_version: str = ""
    latency: float = 0.0
    tokens: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    fallback: bool = False  # True iff a fallback path produced this result

    def require_json(self) -> dict[str, Any]:
        """Return parsed_json or raise. Use only where a structured result is
        mandatory — callers that can degrade should check ``parsed_json``."""
        if not self.ok or self.parsed_json is None:
            raise ValueError(f"model result not usable: {self.error or 'no json'}")
        return self.parsed_json  # type: ignore[return-value]


class EmbeddingResult(BaseModel):
    ok: bool
    model: str
    vectors: list[list[float]] = Field(default_factory=list)
    dim: int = 0
    fallback: bool = False


class RerankResult(BaseModel):
    ok: bool
    model: str
    scores: list[float] = Field(default_factory=list)
    fallback: bool = False
