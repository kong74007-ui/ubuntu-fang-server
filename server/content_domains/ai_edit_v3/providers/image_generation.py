from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .base import ProviderResult


@dataclass(frozen=True)
class ImageGenerationRequest:
    slot_id: str
    semantic: tuple[str, ...]
    purpose: str
    ratio: str
    theme: Mapping[str, str]
    fact_boundary: str


class ImageGenerationProvider(Protocol):
    def submit(
        self,
        request: Mapping[str, Any] | ImageGenerationRequest,
        *,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult: ...

    def query(self, request_id: str, *, deadline_at: float) -> ProviderResult: ...
