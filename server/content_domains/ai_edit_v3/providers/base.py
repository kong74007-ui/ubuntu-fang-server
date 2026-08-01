"""Stable provider DTOs and submission-outcome taxonomy for AI Edit V3."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("provider_payload_invalid")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("provider_payload_invalid")


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"provider_{field_name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    capability: str
    request_id: str | None
    payload: Mapping[str, Any]
    usage: Mapping[str, int | float]
    elapsed_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _identifier(self.provider, "name"))
        object.__setattr__(
            self, "capability", _identifier(self.capability, "capability")
        )
        if self.request_id is not None:
            object.__setattr__(
                self, "request_id", _identifier(self.request_id, "request_id")
            )
        if not isinstance(self.payload, Mapping):
            raise ValueError("provider_payload_invalid")
        if not isinstance(self.usage, Mapping):
            raise ValueError("provider_usage_invalid")
        usage: dict[str, int | float] = {}
        for name, value in self.usage.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("provider_usage_invalid")
            usage[name] = value
        if (
            isinstance(self.elapsed_ms, bool)
            or not isinstance(self.elapsed_ms, int)
            or self.elapsed_ms < 0
        ):
            raise ValueError("provider_elapsed_invalid")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "usage", MappingProxyType(usage))


class _SubmissionOutcome(RuntimeError):
    def __init__(self, reason_code: str):
        if not isinstance(reason_code, str) or _REASON_CODE.fullmatch(reason_code) is None:
            reason_code = "provider_reason_invalid"
        self.reason_code = reason_code
        super().__init__(reason_code)


class DefinitiveNotAccepted(_SubmissionOutcome):
    """Authoritative evidence says the provider did not accept a submission."""


class SubmissionUnknown(_SubmissionOutcome):
    """Submission authority is unknown; absence and retry are not implied."""
