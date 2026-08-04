"""Default-off configuration boundary for AI Edit V3."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .store import classify_filesystem


_CONFIG_NAMES = frozenset(
    {
        "AI_EDIT_V3_ENABLED",
        "AI_EDIT_V3_DB_PATH",
        "AI_EDIT_V2_DB",
        "AI_EDIT_V3_ENVIRONMENT",
        "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE",
        "AI_EDIT_V3_WORKER_CONCURRENCY",
        "AI_EDIT_V3_QUEUE_CAPACITY",
        "AI_EDIT_V3_TEMP_BYTES_LIMIT",
        "AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS",
        "AI_EDIT_V3_VISUAL_PROGRAM_ENABLED",
    }
)
_SECURITY_COMPOUND_MARKERS = frozenset(
    {
        "ACCESSKEY",
        "APIKEY",
        "AUTHHEADER",
        "AUTHTOKEN",
        "BEARERTOKEN",
        "HMACKEY",
        "PRIVATEKEY",
        "SECRETKEY",
        "SESSIONCOOKIE",
        "SIGNINGKEY",
    }
)
_SECURITY_NAME_TOKENS = frozenset(
    {
        *_SECURITY_COMPOUND_MARKERS,
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "HMAC",
        "KEY",
        "PASSWORD",
        "SECRET",
        "TOKEN",
    }
)
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_INT64_MAX = (1 << 63) - 1
_CAPABILITY_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_CAPABILITY_STATUSES = frozenset(
    {"implemented", "configured_and_wired", "missing_or_unavailable"}
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "AUX",
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class FeatureConfigurationError(ValueError):
    """Stable, non-secret configuration failure."""

    def __init__(self, reason_code: str, field_name: str):
        self.reason_code = reason_code
        self.field_name = field_name
        super().__init__(f"{reason_code}: {field_name}")


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    enabled: bool
    db_path: Path | None
    v2_db_path: Path | None
    environment: str | None
    owner_hmac_secret_file: Path | None
    worker_concurrency: int | None
    queue_capacity: int | None
    temp_bytes_limit: int | None
    director_timeout_seconds: int = 120
    visual_program_enabled: bool = False


CapabilityStatus = Literal[
    "implemented", "configured_and_wired", "missing_or_unavailable"
]


@dataclass(frozen=True, slots=True)
class CapabilityItem:
    status: CapabilityStatus
    reason_code: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in _CAPABILITY_STATUSES:
            raise ValueError("capability_status_invalid")
        if (
            not isinstance(self.reason_code, str)
            or _CAPABILITY_REASON.fullmatch(self.reason_code) is None
        ):
            raise ValueError("capability_reason_invalid")
        if not isinstance(self.detail, str) or any(
            ord(character) < 0x20 for character in self.detail
        ):
            raise ValueError("capability_detail_invalid")


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    items: Mapping[str, CapabilityItem]
    runtime_versions: Mapping[str, str]
    allows_existing_reads: bool
    accepts_uploads: bool
    accepts_new_jobs: bool
    current_schema_hashes: Mapping[str, str] = field(default_factory=dict)
    historical_schema_hashes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.items, Mapping) or any(
            not isinstance(name, str) or not isinstance(item, CapabilityItem)
            for name, item in self.items.items()
        ):
            raise ValueError("capability_items_invalid")
        if not isinstance(self.runtime_versions, Mapping) or any(
            not isinstance(name, str)
            or not isinstance(value, str)
            or not name
            or not value
            for name, value in self.runtime_versions.items()
        ):
            raise ValueError("runtime_versions_invalid")
        if not isinstance(self.current_schema_hashes, Mapping) or any(
            not isinstance(name, str) or not name or not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for name, value in self.current_schema_hashes.items()
        ):
            raise ValueError("current_schema_hashes_invalid")
        if not isinstance(self.historical_schema_hashes, Mapping) or any(
            not isinstance(name, str) or not name or not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values)
            for name, values in self.historical_schema_hashes.items()
        ):
            raise ValueError("historical_schema_hashes_invalid")
        booleans = (
            self.allows_existing_reads,
            self.accepts_uploads,
            self.accepts_new_jobs,
        )
        if any(not isinstance(value, bool) for value in booleans):
            raise ValueError("capability_report_boolean_invalid")
        object.__setattr__(self, "items", MappingProxyType(dict(self.items)))
        object.__setattr__(
            self,
            "runtime_versions",
            MappingProxyType(dict(self.runtime_versions)),
        )
        object.__setattr__(self, "current_schema_hashes", MappingProxyType(dict(self.current_schema_hashes)))
        object.__setattr__(self, "historical_schema_hashes", MappingProxyType({name: tuple(values) for name, values in self.historical_schema_hashes.items()}))


class CapabilityUnavailable(RuntimeError):
    """Fail-closed request gate exposing stable reason codes only."""

    error_code = "capability_unavailable"

    def __init__(self, reason_codes: tuple[str, ...] | list[str]):
        sanitized = sorted(
            {
                reason
                for reason in reason_codes
                if isinstance(reason, str)
                and _CAPABILITY_REASON.fullmatch(reason) is not None
            }
        )
        self.reason_codes = tuple(sanitized or ("capability_unavailable",))
        super().__init__(f"{self.error_code}:{','.join(self.reason_codes)}")


def _error(reason_code: str, field_name: str) -> FeatureConfigurationError:
    return FeatureConfigurationError(reason_code, field_name)


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _has_windows_reserved_component(value: str) -> bool:
    normalized = value.replace("\\", "/")
    for index, raw_component in enumerate(normalized.split("/")):
        if not raw_component:
            continue
        if index == 0 and re.fullmatch(r"[A-Za-z]:", raw_component):
            continue
        if ":" in raw_component or _has_control(raw_component):
            return True
        component = raw_component.rstrip(" .")
        stem = component.split(".", 1)[0].rstrip(" .").upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            return True
    return False


def _path(value: str | None, field_name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error("config_path_not_absolute", field_name)
    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        raise _error("config_path_network", field_name)
    if _has_windows_reserved_component(value):
        raise _error("config_path_reserved", field_name)
    path = Path(value)
    if not path.is_absolute():
        raise _error("config_path_not_absolute", field_name)
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _error("config_path_unresolvable", field_name) from exc


def _validate_db_filesystem(path: Path, field_name: str) -> None:
    candidates = [path.parent]
    if os.path.lexists(path):
        candidates.append(path)
    for candidate in candidates:
        try:
            classification = classify_filesystem(candidate).policy
        except Exception as exc:
            raise _error("config_db_filesystem_unknown", field_name) from exc
        if classification == "remote":
            raise _error("config_db_filesystem_remote", field_name)
        if classification != "local":
            raise _error("config_db_filesystem_unknown", field_name)


def _integer(
    value: str | None,
    field_name: str,
    *,
    minimum: int = 1,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or _POSITIVE_DECIMAL.fullmatch(value) is None:
        raise _error("config_integer_invalid", field_name)
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise _error("config_integer_invalid", field_name)
    return parsed


def _required(value: object | None, field_name: str, enabled: bool) -> None:
    if enabled and value is None:
        raise _error("config_required", field_name)


def _is_security_sensitive_config_name(name: str) -> bool:
    normalized = name.upper()
    prefix = "AI_EDIT_V3_"
    if not normalized.startswith(prefix):
        return False
    tokens = {token for token in normalized[len(prefix):].split("_") if token}
    return bool(tokens & _SECURITY_NAME_TOKENS) or any(
        marker in token
        for token in tokens
        for marker in _SECURITY_COMPOUND_MARKERS
    )


def load_config(env: Mapping[str, str] | None = None) -> FeatureConfig:
    """Validate configuration without mutating or retaining ``os.environ``."""

    source = dict(os.environ if env is None else env)
    for name in source:
        if not isinstance(name, str):
            continue
        if name in _CONFIG_NAMES:
            continue
        if _is_security_sensitive_config_name(name):
            raise _error("config_secret_forbidden", name)

    enabled_text = source.get("AI_EDIT_V3_ENABLED", "0")
    if enabled_text not in {"0", "1"}:
        raise _error("config_enabled_invalid", "AI_EDIT_V3_ENABLED")
    enabled = enabled_text == "1"

    visual_program_text = source.get("AI_EDIT_V3_VISUAL_PROGRAM_ENABLED", "0")
    if visual_program_text not in {"0", "1"}:
        raise _error(
            "config_visual_program_invalid", "AI_EDIT_V3_VISUAL_PROGRAM_ENABLED"
        )
    visual_program_enabled = visual_program_text == "1"

    db_path = _path(source.get("AI_EDIT_V3_DB_PATH"), "AI_EDIT_V3_DB_PATH")
    v2_db_path = _path(source.get("AI_EDIT_V2_DB"), "AI_EDIT_V2_DB")
    secret_file = _path(
        source.get("AI_EDIT_V3_OWNER_HMAC_SECRET_FILE"),
        "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE",
    )
    if db_path is not None:
        _validate_db_filesystem(db_path, "AI_EDIT_V3_DB_PATH")
    if v2_db_path is not None:
        _validate_db_filesystem(v2_db_path, "AI_EDIT_V2_DB")

    environment = source.get("AI_EDIT_V3_ENVIRONMENT")
    if environment is not None and environment not in {"test", "production"}:
        raise _error("config_environment_invalid", "AI_EDIT_V3_ENVIRONMENT")

    worker_concurrency = _integer(
        source.get("AI_EDIT_V3_WORKER_CONCURRENCY"),
        "AI_EDIT_V3_WORKER_CONCURRENCY",
        maximum=10,
    )
    queue_capacity = _integer(
        source.get("AI_EDIT_V3_QUEUE_CAPACITY"),
        "AI_EDIT_V3_QUEUE_CAPACITY",
        maximum=50,
    )
    temp_bytes_limit = _integer(
        source.get("AI_EDIT_V3_TEMP_BYTES_LIMIT"),
        "AI_EDIT_V3_TEMP_BYTES_LIMIT",
        maximum=_INT64_MAX,
    )
    director_timeout_seconds = _integer(
        source.get("AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS", "120"),
        "AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS",
        minimum=30,
        maximum=600,
    )
    assert director_timeout_seconds is not None

    for value, name in (
        (db_path, "AI_EDIT_V3_DB_PATH"),
        (v2_db_path, "AI_EDIT_V2_DB"),
        (environment, "AI_EDIT_V3_ENVIRONMENT"),
        (secret_file, "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE"),
        (worker_concurrency, "AI_EDIT_V3_WORKER_CONCURRENCY"),
        (queue_capacity, "AI_EDIT_V3_QUEUE_CAPACITY"),
        (temp_bytes_limit, "AI_EDIT_V3_TEMP_BYTES_LIMIT"),
    ):
        _required(value, name, enabled)

    if db_path is not None and v2_db_path is not None:
        if os.path.normcase(os.fspath(db_path)) == os.path.normcase(
            os.fspath(v2_db_path)
        ):
            raise _error("config_db_paths_same", "AI_EDIT_V3_DB_PATH")
        if db_path.exists() and v2_db_path.exists():
            try:
                if os.path.samefile(db_path, v2_db_path):
                    raise _error("config_db_paths_same", "AI_EDIT_V3_DB_PATH")
            except FeatureConfigurationError:
                raise
            except OSError as exc:
                raise _error(
                    "config_path_unresolvable", "AI_EDIT_V3_DB_PATH"
                ) from exc

    return FeatureConfig(
        enabled=enabled,
        db_path=db_path,
        v2_db_path=v2_db_path,
        environment=environment,
        owner_hmac_secret_file=secret_file,
        worker_concurrency=worker_concurrency,
        queue_capacity=queue_capacity,
        temp_bytes_limit=temp_bytes_limit,
        director_timeout_seconds=director_timeout_seconds,
        visual_program_enabled=visual_program_enabled,
    )
