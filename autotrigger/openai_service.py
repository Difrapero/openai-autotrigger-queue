"""Small adapter around the official OpenAI Python SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI


@dataclass(frozen=True, slots=True)
class OpenAIResult:
    text: str
    response_id: str | None
    request_id: str | None


class OpenAIService:
    def __init__(self, client: OpenAI | None = None) -> None:
        # The queue owns retry timing. Disabling SDK retries prevents hidden
        # exponential waits from being added before we inspect the 429 headers.
        self.client = client or OpenAI(max_retries=0)

    def execute(self, prompt: str, model: str) -> OpenAIResult:
        response: Any = self.client.responses.create(model=model, input=prompt)
        return OpenAIResult(
            text=response.output_text,
            response_id=getattr(response, "id", None),
            request_id=getattr(response, "_request_id", None),
        )
