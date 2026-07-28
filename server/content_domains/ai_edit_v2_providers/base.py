"""Frozen result and error contracts shared by provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    capability: str
    request_id: str
    payload: dict[str, Any]
    cost_units: int
    elapsed_ms: int


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    pass


class UnknownSubmissionError(ProviderError):
    pass
