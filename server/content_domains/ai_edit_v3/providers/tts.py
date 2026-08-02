from __future__ import annotations

from typing import Any, Protocol


class TtsProvider(Protocol):
    def submit(
        self,
        *,
        owner: str,
        text: str,
        voice_id: str,
        idempotency_key: str,
        deadline_at: float,
    ) -> Any:
        raise NotImplementedError

    def query(self, request_id: str, *, deadline_at: float) -> Any:
        raise NotImplementedError
