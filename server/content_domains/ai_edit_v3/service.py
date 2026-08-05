"""Owner-bound application service for the AI Edit V3 HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .billing import (
    BillingError,
    create_job_with_predebit as billing_create_job_with_predebit,
    create_quote as billing_create_quote,
    validate_published_pricing_readiness,
)
from .contracts import (
    ContractError,
    canonical_json,
    normalize_job_request,
    parse_strict_json,
    request_fingerprint,
)
from .feature import CapabilityReport
from .delivery import ObjectKeyError, build_object_key as _delivery_build_object_key
from .runtime import schema_hash_is_accepted
from .store import StoreConflictError, StoreError, V3Store


_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UPLOAD_TYPES = frozenset({"main_video", "main_audio", "material_image"})
_DECLARED_MIMES = {
    "main_video": frozenset({"video/mp4", "video/quicktime", "video/webm"}),
    "main_audio": frozenset(
        {
            "audio/aac",
            "audio/flac",
            "audio/m4a",
            "audio/mp4",
            "audio/mpeg",
            "audio/ogg",
            "audio/wav",
            "audio/x-wav",
        }
    ),
    "material_image": frozenset({"image/jpeg", "image/png", "image/webp"}),
}
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
_MAX_MATERIAL_IMAGES = 10
_MIN_MEDIA_DURATION_MS = 3_000
_MAX_MEDIA_DURATION_MS = 600_000
_QUOTE_TTL_MS = 900_000
_ACCEPTANCE_PROVIDER_ITEMS = (
    "tts",
    "asr",
    "director",
    "image_generator",
    "audio_generator",
    "renderer",
)
_DEPLOYED_SHA = re.compile(r"[0-9a-f]{40}\Z")
_AUTHORITY_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)")
_PROBE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+()-]{0,127}\Z")
_PROBE_FORMAT_LIST = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]*(?:,[A-Za-z0-9][A-Za-z0-9._+-]*)*\Z"
)
_PROBE_RATIONAL = re.compile(r"(?:[1-9][0-9]*/[1-9][0-9]*|[0-9]+(?:\.[0-9]+)?)\Z")
_PROBE_LONG_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,_+():-]{0,255}\Z")
_MIME_VALUE = re.compile(
    r"(?:application|audio|font|image|message|model|multipart|text|video)/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_V3_COS_VALUE = re.compile(r"(?:^|/)(?:test|production)/ai-edit-v3(?:/|$)", re.I)
_ENCODED_PATH_MARKER = re.compile(r"%(?:2f|5c)", re.I)
_QUERY_SECRET_VALUE = re.compile(
    r"(?:^|[?&;\s])(?:authorization|credential|password|secret|signature|token)=",
    re.I,
)
_PROBE_FIELDS = frozenset(
    {
        "bit_rate",
        "channel_layout",
        "channels",
        "codec",
        "codec_long_name",
        "codec_name",
        "codec_tag_string",
        "color_primaries",
        "color_space",
        "color_transfer",
        "container",
        "duration_ms",
        "field_order",
        "format_long_name",
        "format_name",
        "frame_rate",
        "height",
        "level",
        "pixel_format",
        "profile",
        "rotation",
        "sample_rate",
        "stream_count",
        "width",
    }
)
_CATALOG_FIELDS = {
    "platform_assets": frozenset(
        {
            "asset_id",
            "title",
            "cover_asset_id",
            "cover_reference",
            "duration_ms",
            "ratio",
        }
    ),
    "audio_assets": frozenset(
        {
            "asset_id",
            "title",
            "duration_ms",
            "mime_type",
            "cover_asset_id",
            "cover_reference",
        }
    ),
    "voices": frozenset(
        {
            "voice_id",
            "name",
            "title",
            "language",
            "gender",
            "description",
            "preview_asset_id",
        }
    ),
    "templates": frozenset(
        {
            "template_id",
            "version",
            "title",
            "description",
            "preview_asset_id",
            "preview_reference",
            "supported_ratios",
        }
    ),
}
_SENSITIVE_PROBE_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "local_path",
    "object_key",
    "password",
    "secret",
    "signed",
    "token",
    "url",
)
_PROBE_NONNEGATIVE_INTEGER_FIELDS = frozenset(
    {
        "bit_rate",
        "channels",
        "duration_ms",
        "height",
        "level",
        "sample_rate",
        "stream_count",
        "width",
    }
)
_PROBE_TOKEN_FIELDS = frozenset(
    {
        "codec",
        "codec_name",
        "codec_tag_string",
        "color_primaries",
        "color_space",
        "color_transfer",
        "field_order",
        "pixel_format",
        "profile",
    }
)
_PROBE_FORMAT_FIELDS = frozenset({"container", "format_name"})
_PROBE_LONG_TEXT_FIELDS = frozenset({"codec_long_name", "format_long_name"})


class ServiceError(RuntimeError):
    """Stable, sanitized failure raised at the application boundary."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status: int = 400,
        retry_after: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"{error_code}: {message}")


@dataclass(frozen=True, slots=True)
class UploadObservation:
    """Authoritative, immutable upload inspection result."""

    mime_type: str
    media_kind: Literal["video", "audio", "image"]
    size_bytes: int
    sha256: str
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: int | float | None = None
    probe_evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        evidence = {} if self.probe_evidence is None else dict(self.probe_evidence)
        object.__setattr__(self, "probe_evidence", MappingProxyType(evidence))


class UploadObjectStore(Protocol):
    def presign_put(
        self, key: str, content_type: str, expires: int = 900
    ) -> str: ...

    def head_object(self, key: str) -> Mapping[str, Any]: ...

    def delete_object(self, key: str) -> None: ...


class UploadInspector(Protocol):
    def inspect(
        self,
        key: str,
        *,
        upload_type: str,
        head: Mapping[str, Any],
    ) -> UploadObservation: ...


class SourceCatalog(Protocol):
    def resolve_platform_asset(
        self, owner: str, asset_id: str
    ) -> Mapping[str, Any] | None: ...

    def resolve_audio_asset(
        self, owner: str, asset_id: str
    ) -> Mapping[str, Any] | None: ...

    def resolve_voice(
        self, owner: str, voice_id: str
    ) -> Mapping[str, Any] | None: ...

    def resolve_template(
        self, template_id: str, ratio: str
    ) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class CapacityDecision:
    accepted: bool
    queue_slots: int
    required_temp_bytes: int
    retry_after: int | None


class CapacityGate(Protocol):
    def check(self, normalized_request: Mapping[str, Any]) -> CapacityDecision: ...


class _AtomicCreateStoreView:
    """Narrow adapter that adds Task-9 bindings to billing's Store call."""

    def __init__(
        self,
        store: V3Store,
        *,
        predecessor_job_id: str | None,
        material_bindings: list[dict[str, Any]],
    ) -> None:
        self._store = store
        self.environment = store.environment
        self._predecessor_job_id = predecessor_job_id
        self._material_bindings = material_bindings

    def create_job_with_predebit(self, *args: Any, **kwargs: Any) -> Any:
        return self._store.create_job_with_predebit(
            *args,
            predecessor_job_id=self._predecessor_job_id,
            material_bindings=self._material_bindings,
            **kwargs,
        )


def _has_control_or_surrogate(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    )


def build_object_key(
    environment: str,
    owner: str,
    job_id: str,
    scope: str,
    filename: str,
    owner_hmac_secret: bytes,
) -> str:
    """Map the pure delivery key validator to the service error boundary."""

    try:
        return _delivery_build_object_key(
            environment,
            owner,
            job_id,
            scope,
            filename,
            owner_hmac_secret,
        )
    except ObjectKeyError as exc:
        raise ServiceError("object_key_invalid", "object key input is invalid") from exc


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _require_owner(owner: Any) -> str:
    if (
        not isinstance(owner, str)
        or not owner
        or owner != owner.strip()
        or len(owner) > 256
        or _has_control_or_surrogate(owner)
    ):
        raise ServiceError("authentication_required", "authentication is required", status=401)
    return owner


def _require_now(now: Any) -> int:
    if (
        isinstance(now, bool)
        or not isinstance(now, int)
        or now < 0
        or now > (1 << 63) - 1
    ):
        raise ServiceError("request_invalid", "request time is invalid")
    return now


def _require_identifier(name: str, value: Any, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _has_control_or_surrogate(value)
    ):
        raise ServiceError("request_invalid", f"{name} is invalid")
    return value


def _safe_authority_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ServiceError("input_probe_invalid", "probe evidence is too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < -(1 << 63) or value > (1 << 63) - 1:
            raise ServiceError("input_probe_invalid", "probe integer is invalid")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ServiceError("input_probe_invalid", "probe number is invalid")
        return value
    if isinstance(value, str):
        if (
            len(value) > 512
            or _has_control_or_surrogate(value)
            or "://" in value
            or "?" in value
            or "\\" in value
        ):
            raise ServiceError("input_probe_invalid", "probe string is unsafe")
        return value
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise ServiceError("input_probe_invalid", "probe evidence is too large")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or _AUTHORITY_KEY.fullmatch(key) is None
                or any(marker in key for marker in _SENSITIVE_PROBE_MARKERS)
            ):
                raise ServiceError("input_probe_invalid", "probe field is unsafe")
            result[key] = _safe_authority_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise ServiceError("input_probe_invalid", "probe evidence is too large")
        return [_safe_authority_value(item, depth=depth + 1) for item in value]
    raise ServiceError("input_probe_invalid", "probe evidence type is invalid")


def _safe_probe_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > len(_PROBE_FIELDS):
        raise ServiceError("input_probe_invalid", "probe evidence is invalid")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _PROBE_FIELDS:
            raise ServiceError("input_probe_invalid", "probe field is unsafe")
        if key in _PROBE_NONNEGATIVE_INTEGER_FIELDS:
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or not 0 <= item <= (1 << 63) - 1
            ):
                raise ServiceError("input_probe_invalid", "probe integer is invalid")
        elif key == "rotation":
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(item)
                or not -360 <= item <= 360
            ):
                raise ServiceError("input_probe_invalid", "probe rotation is invalid")
        elif key == "frame_rate":
            if isinstance(item, bool) or not (
                isinstance(item, (int, float))
                and math.isfinite(item)
                and item > 0
                or isinstance(item, str)
                and _PROBE_RATIONAL.fullmatch(item) is not None
            ):
                raise ServiceError("input_probe_invalid", "probe frame rate is invalid")
        elif key in _PROBE_TOKEN_FIELDS:
            if not isinstance(item, str) or _PROBE_TOKEN.fullmatch(item) is None:
                raise ServiceError("input_probe_invalid", "probe token is invalid")
        elif key in _PROBE_FORMAT_FIELDS:
            if not isinstance(item, str) or _PROBE_FORMAT_LIST.fullmatch(item) is None:
                raise ServiceError("input_probe_invalid", "probe format is invalid")
        elif key == "channel_layout":
            if not isinstance(item, str) or _PROBE_TOKEN.fullmatch(item) is None:
                raise ServiceError("input_probe_invalid", "probe channel layout is invalid")
        elif key in _PROBE_LONG_TEXT_FIELDS:
            if not isinstance(item, str) or _PROBE_LONG_TEXT.fullmatch(item) is None:
                raise ServiceError("input_probe_invalid", "probe description is invalid")
        else:  # pragma: no cover - every allowlisted field has one frozen domain
            raise ServiceError("input_probe_invalid", "probe value is invalid")
        if isinstance(item, str) and (
            _has_control_or_surrogate(item)
            or _V3_COS_VALUE.search(item) is not None
            or _QUERY_SECRET_VALUE.search(item) is not None
            or "://" in item
            or "?" in item
            or "#" in item
            or "\\" in item
            or any(segment in {".", ".."} for segment in item.split("/"))
        ):
            raise ServiceError("input_probe_invalid", "probe string is unsafe")
        result[key] = item
    return result


def _probe_frame_rate_number(value: int | float | str) -> float:
    if isinstance(value, str):
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return int(numerator) / int(denominator)
        return float(value)
    return float(value)


def _is_absolute_local_path(value: str) -> bool:
    return value.startswith("/") or _ABSOLUTE_WINDOWS_PATH.match(value) is not None


def _looks_like_private_reference(value: str, *, allow_mime: bool = False) -> bool:
    normalized = value.replace("\\", "/")
    return bool(
        _has_control_or_surrogate(value)
        or "://" in value
        or _V3_COS_VALUE.search(normalized) is not None
        or _ENCODED_PATH_MARKER.search(value) is not None
        or _QUERY_SECRET_VALUE.search(value) is not None
        or "\\" in value
        or _is_absolute_local_path(value)
        or any(segment in {".", ".."} for segment in normalized.split("/"))
        or (
            "/" in value
            and not (allow_mime and _MIME_VALUE.fullmatch(value) is not None)
        )
    )


def _safe_opaque_catalog_value(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _OPAQUE_ID.fullmatch(value) is None
        or ".." in value
        or _looks_like_private_reference(value)
    ):
        raise ValueError("catalog_reference_invalid")
    return value


def _safe_summary_text(
    value: Any,
    *,
    maximum: int = 512,
    allow_mime: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _has_control_or_surrogate(value)
        or _looks_like_private_reference(value, allow_mime=allow_mime)
    ):
        raise ValueError("catalog_text_invalid")
    return value


def _public_catalog_record(capability: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("catalog_record_invalid")
    fields = _CATALOG_FIELDS[capability]
    result: dict[str, Any] = {}
    for key in fields:
        if key not in value:
            continue
        item = value[key]
        if key in {"duration_ms"}:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError("catalog_duration_invalid")
            result[key] = item
        elif key == "ratio":
            if item not in {"16:9", "9:16"}:
                raise ValueError("catalog_ratio_invalid")
            result[key] = item
        elif key == "supported_ratios":
            if (
                not isinstance(item, (list, tuple))
                or not item
                or len(item) > 2
                or any(ratio not in {"16:9", "9:16"} for ratio in item)
                or len(set(item)) != len(item)
            ):
                raise ValueError("catalog_ratios_invalid")
            result[key] = list(item)
        elif key == "mime_type":
            text = _safe_summary_text(item, maximum=128, allow_mime=True)
            if text != text.lower() or not text.startswith("audio/"):
                raise ValueError("catalog_mime_invalid")
            result[key] = text
        elif key.endswith("_id") or key.endswith("_reference") or key == "version":
            result[key] = _safe_opaque_catalog_value(item)
        else:
            result[key] = _safe_summary_text(item)

    identity = {
        "platform_assets": "asset_id",
        "audio_assets": "asset_id",
        "voices": "voice_id",
        "templates": "template_id",
    }[capability]
    if identity not in result:
        raise ValueError("catalog_identity_invalid")
    if capability in {"platform_assets", "audio_assets"} and "duration_ms" not in result:
        raise ValueError("catalog_duration_invalid")
    return result


def _public_upload(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "upload_id": row["upload_id"],
        "upload_type": row["upload_type"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "mime_type": row["observed_mime"],
        "size_bytes": row["observed_size"],
        "duration_ms": row["duration_ms"],
        "width": row["width"],
        "height": row["height"],
        "sha256": row["sha256"],
    }
    return result


def _public_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "material_id": row["material_id"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
    }


class EditV3Service:
    """Owner-scoped use cases; providers, renderers and ledgers stay outside."""

    def __init__(
        self,
        store: V3Store,
        *,
        object_store: UploadObjectStore | None = None,
        upload_inspector: UploadInspector | None = None,
        owner_hmac_secret: bytes | None = None,
        enabled: bool = False,
        id_factory: Callable[[str], str] | None = None,
        source_catalog: SourceCatalog | None = None,
        capacity_gate: CapacityGate | None = None,
        clock: Callable[[], int] | None = None,
        capability_report: CapabilityReport
        | Callable[[], CapabilityReport]
        | None = None,
        result_signer: Callable[[str, int, str | None], str] | None = None,
        deployed_sha: str | None = None,
        acceptance_provider_identities: Mapping[str, str] | None = None,
        acceptance_evidence_reader: Any | None = None,
    ) -> None:
        if not isinstance(store, V3Store):
            raise TypeError("store_invalid")
        self.store = store
        self.environment = store.environment
        self.object_store = object_store
        self.upload_inspector = upload_inspector
        self.owner_hmac_secret = owner_hmac_secret
        self.enabled = enabled
        self._id_factory = _default_id if id_factory is None else id_factory
        self.source_catalog = source_catalog
        self.capacity_gate = capacity_gate
        self._clock = (
            (lambda: int(time.time() * 1000)) if clock is None else clock
        )
        self._capability_report_source = capability_report
        self._result_signer = result_signer
        if deployed_sha is not None and _DEPLOYED_SHA.fullmatch(deployed_sha) is None:
            raise ValueError("deployed_sha_invalid")
        self.deployed_sha = deployed_sha
        identities = dict(acceptance_provider_identities or {})
        if any(
            not isinstance(name, str)
            or not isinstance(identity, str)
            or not identity
            for name, identity in identities.items()
        ):
            raise ValueError("acceptance_provider_identities_invalid")
        self._acceptance_provider_identities = MappingProxyType(identities)
        self._acceptance_evidence_reader = acceptance_evidence_reader

    def now(self) -> int:
        return _require_now(self._clock())

    def _capability_report(self) -> CapabilityReport | None:
        source = self._capability_report_source
        if source is None:
            return None
        try:
            report = source() if callable(source) else source
        except Exception:
            return None
        return report if isinstance(report, CapabilityReport) else None

    def _acceptance_providers_ready(
        self, report: CapabilityReport | None
    ) -> bool:
        if report is None:
            return False
        identities = self._acceptance_provider_identities
        return all(
            report.items.get(name) is not None
            and report.items[name].status == "configured_and_wired"
            and isinstance(identities.get(name), str)
            and identities[name].casefold() != "placeholder"
            for name in _ACCEPTANCE_PROVIDER_ITEMS
        )

    def _owner_secret_ready(self) -> bool:
        secret = self.owner_hmac_secret
        return (
            isinstance(secret, bytes)
            and len(secret) >= 16
            and len(set(secret)) >= 8
        )

    def _pricing_ready(self) -> bool:
        try:
            validate_published_pricing_readiness(self.store)
        except Exception:
            return False
        return True

    def _accepts_uploads(self, report: CapabilityReport | None) -> bool:
        object_store_ready = self.object_store is not None and all(
            callable(getattr(self.object_store, method, None))
            for method in ("presign_put", "head_object", "delete_object")
        )
        inspector_ready = self.upload_inspector is not None and callable(
            getattr(self.upload_inspector, "inspect", None)
        )
        return bool(
            self.enabled
            and report is not None
            and report.accepts_uploads
            and object_store_ready
            and inspector_ready
            and self._owner_secret_ready()
        )

    def _accepts_new_jobs(self, report: CapabilityReport | None) -> bool:
        capacity_ready = self.capacity_gate is not None and callable(
            getattr(self.capacity_gate, "check", None)
        )
        return bool(
            self.enabled
            and report is not None
            and report.accepts_new_jobs
            and capacity_ready
            and self._owner_secret_ready()
            and self._pricing_ready()
        )

    def _require_write(self, *, upload: bool = False) -> None:
        self._require_feature_enabled()
        report = self._capability_report()
        if report is None:
            raise ServiceError(
                "capability_unavailable",
                "runtime readiness is unavailable",
                status=503,
            )
        if upload and not self._accepts_uploads(report):
            raise ServiceError(
                "upload_capability_unavailable",
                "upload capability is unavailable",
                status=503,
            )
        if not upload and not self._accepts_new_jobs(report):
            raise ServiceError(
                "capability_unavailable",
                "new job capability is unavailable",
                status=503,
            )

    def _require_feature_enabled(self) -> None:
        if not self.enabled:
            raise ServiceError(
                "feature_disabled",
                "AI Edit V3 write operations are disabled",
                status=503,
            )

    def _new_id(self, prefix: str) -> str:
        try:
            value = self._id_factory(prefix)
        except Exception as exc:
            raise ServiceError("identity_unavailable", "identity generation failed", status=503) from exc
        if (
            not isinstance(value, str)
            or _OPAQUE_ID.fullmatch(value) is None
            or ".." in value
        ):
            raise ServiceError("identity_unavailable", "identity generation failed", status=503)
        return value

    def create_upload(
        self,
        owner: str,
        request: Mapping[str, Any],
        *,
        now: int,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        now = _require_now(now)
        self._require_write(upload=True)
        if not isinstance(request, Mapping) or set(request) != {
            "upload_type",
            "filename",
            "content_type",
            "size_bytes",
        }:
            raise ServiceError("request_invalid", "upload request fields are invalid")
        upload_type = request["upload_type"]
        content_type = request["content_type"]
        size_bytes = request["size_bytes"]
        if upload_type not in _UPLOAD_TYPES:
            raise ServiceError("input_upload_type_invalid", "upload type is unsupported")
        if (
            not isinstance(content_type, str)
            or content_type not in _DECLARED_MIMES[upload_type]
        ):
            raise ServiceError("input_declared_mime_invalid", "declared media type is unsupported")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or size_bytes > _MAX_UPLOAD_BYTES
        ):
            raise ServiceError("input_declared_size_invalid", "declared size is invalid")
        if upload_type == "material_image" and size_bytes > _MAX_IMAGE_BYTES:
            raise ServiceError("input_image_size_exceeded", "image exceeds 25 MiB")

        upload_id = self._new_id("upload")
        scope = "materials/uploaded" if upload_type == "material_image" else "source"
        key = build_object_key(
            self.environment,
            owner,
            upload_id,
            scope,
            request["filename"],
            self.owner_hmac_secret,
        )
        if now > (1 << 63) - 1 - 900_000:
            raise ServiceError("request_invalid", "upload expiry is invalid")
        expires_at = now + 900_000
        try:
            row = self.store.insert_upload(
                owner,
                upload_id,
                upload_type=upload_type,
                object_key=key,
                declared_mime=content_type,
                declared_size=size_bytes,
                expires_at=expires_at,
                created_at=now,
                environment=self.environment,
            )
        except StoreError as exc:
            raise ServiceError(exc.error_code, exc.message, status=409) from exc
        if row is None:
            raise ServiceError("upload_identity_conflict", "upload identity conflict", status=409)
        try:
            put_url = self.object_store.presign_put(key, content_type, expires=900)
        except Exception as exc:
            raise ServiceError(
                "upload_presign_unavailable",
                "upload authorization is unavailable",
                status=503,
            ) from exc
        if (
            not isinstance(put_url, str)
            or not put_url
            or _has_control_or_surrogate(put_url)
        ):
            raise ServiceError(
                "upload_presign_unavailable",
                "upload authorization is unavailable",
                status=503,
            )
        result = _public_upload(row)
        result["put_url"] = put_url
        result["put_expires_in"] = 900
        return result

    @staticmethod
    def _validate_head(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ServiceError("input_object_metadata_invalid", "object metadata is invalid")
        size = value.get("size_bytes")
        content_type = value.get("content_type")
        etag = value.get("etag")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > (1 << 63) - 1
            or not isinstance(content_type, str)
            or not content_type
            or len(content_type) > 128
            or _has_control_or_surrogate(content_type)
            or not isinstance(etag, str)
            or not etag
            or len(etag) > 256
            or _has_control_or_surrogate(etag)
            or "://" in etag
            or "?" in etag
        ):
            raise ServiceError("input_object_metadata_invalid", "object metadata is invalid")
        return {"size_bytes": size, "content_type": content_type, "etag": etag}

    @staticmethod
    def _validate_observation(
        upload_type: str,
        head: Mapping[str, Any],
        observation: Any,
    ) -> tuple[UploadObservation, dict[str, Any]]:
        if not isinstance(observation, UploadObservation):
            raise ServiceError("input_probe_invalid", "upload inspection result is invalid")
        if (
            not isinstance(observation.mime_type, str)
            or observation.mime_type != observation.mime_type.lower()
            or len(observation.mime_type) > 128
            or _has_control_or_surrogate(observation.mime_type)
            or observation.media_kind not in {"video", "audio", "image"}
            or isinstance(observation.size_bytes, bool)
            or not isinstance(observation.size_bytes, int)
            or observation.size_bytes < 0
            or observation.size_bytes != head["size_bytes"]
            or re.fullmatch(r"[0-9a-f]{64}", observation.sha256 or "") is None
        ):
            raise ServiceError("input_probe_metadata_mismatch", "upload inspection result mismatches object metadata")

        expected_kind = {
            "main_video": "video",
            "main_audio": "audio",
            "material_image": "image",
        }[upload_type]
        if observation.media_kind != expected_kind:
            raise ServiceError("input_media_kind_mismatch", "uploaded media kind is invalid")
        evidence = _safe_probe_evidence(observation.probe_evidence)
        if not isinstance(evidence, dict):
            raise ServiceError("input_probe_invalid", "probe evidence is invalid")
        if (
            upload_type == "main_video"
            and "frame_rate" in evidence
            and _probe_frame_rate_number(evidence["frame_rate"]) > 60
        ):
            raise ServiceError("input_video_invalid", "video observation is invalid")

        duration = observation.duration_ms
        width = observation.width
        height = observation.height
        frame_rate = observation.frame_rate
        for name, value in (("duration", duration), ("width", width), ("height", height)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ServiceError("input_probe_invalid", f"{name} observation is invalid")
        if frame_rate is not None and (
            isinstance(frame_rate, bool)
            or not isinstance(frame_rate, (int, float))
            or not math.isfinite(frame_rate)
            or frame_rate <= 0
        ):
            raise ServiceError("input_probe_invalid", "frame rate observation is invalid")

        if upload_type == "material_image":
            if observation.mime_type not in _IMAGE_MIMES:
                raise ServiceError("input_image_type_unsupported", "image type is unsupported")
            if observation.size_bytes > _MAX_IMAGE_BYTES:
                raise ServiceError("input_image_size_exceeded", "image exceeds 25 MiB")
            if (
                duration is not None
                or width is None
                or height is None
                or width <= 0
                or height <= 0
                or max(width, height) > 12_000
                or width * height > 80_000_000
            ):
                raise ServiceError("input_image_dimensions_invalid", "image dimensions are invalid")
        else:
            if duration is None or not _MIN_MEDIA_DURATION_MS <= duration <= _MAX_MEDIA_DURATION_MS:
                raise ServiceError("input_duration_invalid", "media duration must be 3 to 600 seconds")
            if upload_type == "main_video":
                if (
                    not observation.mime_type.startswith("video/")
                    or width is None
                    or height is None
                    or width <= 0
                    or height <= 0
                    or max(width, height) > 4_096
                    or frame_rate is not None
                    and frame_rate > 60
                ):
                    raise ServiceError("input_video_invalid", "video observation is invalid")
            elif (
                not observation.mime_type.startswith("audio/")
                or width is not None
                or height is not None
                or frame_rate is not None
            ):
                raise ServiceError("input_audio_invalid", "audio observation is invalid")
        if frame_rate is not None:
            evidence["frame_rate"] = frame_rate
        return observation, evidence

    @staticmethod
    def _completed_upload_response(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            upload_type = row["upload_type"]
            declared_mime = row["declared_mime"]
            declared_size = row["declared_size"]
            observed_mime = row["observed_mime"]
            observed_size = row["observed_size"]
            duration = row["duration_ms"]
            width = row["width"]
            height = row["height"]
            sha256 = row["sha256"]
            if (
                row["status"] != "completed"
                or upload_type not in _UPLOAD_TYPES
                or declared_mime not in _DECLARED_MIMES[upload_type]
                or isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or not 0 <= declared_size <= _MAX_UPLOAD_BYTES
                or upload_type == "material_image"
                and declared_size > _MAX_IMAGE_BYTES
                or not isinstance(observed_mime, str)
                or observed_mime != observed_mime.lower()
                or len(observed_mime) > 128
                or _has_control_or_surrogate(observed_mime)
                or isinstance(observed_size, bool)
                or not isinstance(observed_size, int)
                or not 0 <= observed_size <= (1 << 63) - 1
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                or any(
                    isinstance(value, bool)
                    or value is not None
                    and (not isinstance(value, int) or value < 0)
                    for value in (duration, width, height)
                )
            ):
                raise ValueError("completed_upload_invalid")
            EditV3Service._validate_head(
                {
                    "size_bytes": observed_size,
                    "content_type": observed_mime,
                    "etag": row["observed_etag"],
                }
            )
            if upload_type == "material_image":
                if (
                    observed_mime not in _IMAGE_MIMES
                    or observed_size > _MAX_IMAGE_BYTES
                    or duration is not None
                    or width is None
                    or height is None
                    or width <= 0
                    or height <= 0
                    or max(width, height) > 12_000
                    or width * height > 80_000_000
                ):
                    raise ValueError("completed_image_invalid")
            elif upload_type == "main_video":
                if (
                    not observed_mime.startswith("video/")
                    or duration is None
                    or not _MIN_MEDIA_DURATION_MS <= duration <= _MAX_MEDIA_DURATION_MS
                    or width is None
                    or height is None
                    or width <= 0
                    or height <= 0
                    or max(width, height) > 4_096
                ):
                    raise ValueError("completed_video_invalid")
            elif (
                not observed_mime.startswith("audio/")
                or duration is None
                or not _MIN_MEDIA_DURATION_MS <= duration <= _MAX_MEDIA_DURATION_MS
                or width is not None
                or height is not None
            ):
                raise ValueError("completed_audio_invalid")
            raw_probe = row["probe_json"]
            evidence = parse_strict_json(
                raw_probe,
                max_bytes=16 * 1024,
                max_depth=2,
                max_items=len(_PROBE_FIELDS),
                max_string_chars=256,
            )
            frozen_probe = _safe_probe_evidence(evidence)
            if (
                canonical_json(frozen_probe).decode("utf-8") != raw_probe
                or upload_type == "main_video"
                and "frame_rate" in frozen_probe
                and _probe_frame_rate_number(frozen_probe["frame_rate"]) > 60
            ):
                raise ValueError("completed_probe_invalid")
        except (ContractError, KeyError, ServiceError, TypeError, ValueError) as exc:
            raise ServiceError(
                "upload_storage_failed",
                "stored completed upload is invalid",
                status=503,
            ) from exc
        return _public_upload(row)

    def complete_upload(
        self,
        owner: str,
        upload_id: str,
        *,
        now: int,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        upload_id = _require_identifier("upload_id", upload_id)
        now = _require_now(now)
        self._require_feature_enabled()
        row = self.store.get_upload_for_owner(
            owner, upload_id, environment=self.environment
        )
        if row is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        if row["status"] == "completed":
            return self._completed_upload_response(row)
        self._require_write(upload=True)
        if row["status"] != "pending":
            raise ServiceError("upload_not_completable", "upload cannot be completed", status=409)
        if now >= row["expires_at"]:
            raise ServiceError("upload_expired", "upload intent has expired", status=409)
        try:
            head = self._validate_head(self.object_store.head_object(row["object_key"]))
            observation = self.upload_inspector.inspect(
                row["object_key"], upload_type=row["upload_type"], head=head
            )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "input_inspection_unavailable",
                "uploaded object could not be inspected",
                status=503,
            ) from exc
        observation, evidence = self._validate_observation(
            row["upload_type"], head, observation
        )
        try:
            completed = self.store.complete_upload(
                owner,
                upload_id,
                observed_mime=observation.mime_type,
                observed_size=observation.size_bytes,
                observed_etag=head["etag"],
                sha256=observation.sha256,
                duration_ms=observation.duration_ms,
                width=observation.width,
                height=observation.height,
                probe=evidence,
                completed_at=now,
                environment=self.environment,
            )
        except StoreConflictError as exc:
            raise ServiceError(exc.error_code, exc.message, status=409) from exc
        except StoreError as exc:
            raise ServiceError("upload_storage_failed", "upload could not be completed", status=503) from exc
        if completed is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        return self._completed_upload_response(completed)

    def create_material(
        self,
        owner: str,
        upload_id: str,
        *,
        now: int,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        upload_id = _require_identifier("upload_id", upload_id)
        now = _require_now(now)
        self._require_write(upload=True)
        existing = self.store.get_material_for_upload(
            owner, upload_id, environment=self.environment
        )
        if existing is not None:
            return _public_material(existing)
        upload = self.store.get_upload_for_owner(
            owner, upload_id, environment=self.environment
        )
        if upload is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        if (
            upload["status"] != "completed"
            or upload["upload_type"] != "material_image"
            or upload["observed_mime"] not in _IMAGE_MIMES
            or upload["observed_size"] is None
            or upload["sha256"] is None
        ):
            raise ServiceError(
                "material_upload_invalid",
                "only a completed material image can be promoted",
                status=409,
            )
        try:
            metadata = json.loads(upload["probe_json"])
            material = self.store.insert_material(
                owner,
                self._new_id("material"),
                source_kind="uploaded",
                upload_id=upload_id,
                cos_key=upload["object_key"],
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata=metadata,
                created_at=now,
                environment=self.environment,
            )
        except StoreConflictError as exc:
            replay = self.store.get_material_for_upload(
                owner, upload_id, environment=self.environment
            )
            if replay is not None:
                return _public_material(replay)
            raise ServiceError(exc.error_code, exc.message, status=409) from exc
        except (StoreError, ValueError, TypeError) as exc:
            raise ServiceError(
                "material_storage_failed",
                "material could not be created",
                status=503,
            ) from exc
        if material is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        return _public_material(material)

    @staticmethod
    def _normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return normalize_job_request(request)
        except ContractError as exc:
            raise ServiceError(exc.error_code, exc.message) from exc

    def _catalog_record(
        self,
        capability: str,
        method_name: str,
        *arguments: Any,
    ) -> Mapping[str, Any]:
        if self.source_catalog is None:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            )
        method = getattr(self.source_catalog, method_name, None)
        if method is None:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            )
        try:
            record = method(*arguments)
        except Exception as exc:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            ) from exc
        if record is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        if not isinstance(record, Mapping):
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability returned invalid data",
                status=503,
            )
        safe_record = _safe_authority_value(record)
        if not isinstance(safe_record, dict):
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability returned invalid data",
                status=503,
            )
        return MappingProxyType(safe_record)

    @staticmethod
    def _require_catalog_duration(record: Mapping[str, Any]) -> None:
        duration = record.get("duration_ms")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not _MIN_MEDIA_DURATION_MS <= duration <= _MAX_MEDIA_DURATION_MS
        ):
            raise ServiceError("input_duration_invalid", "source duration is invalid")

    def _resolve_authorities(
        self,
        owner: str,
        normalized: Mapping[str, Any],
    ) -> dict[str, Any]:
        input_type = normalized["input_type"]
        source_upload_id = (
            normalized["source_upload_id"]
            if input_type in {"uploaded_video", "uploaded_audio"}
            else None
        )
        resolved_uploads = self.store.resolve_request_uploads_for_owner(
            owner,
            source_upload_id=source_upload_id,
            material_ids=normalized["material_asset_ids"],
            environment=self.environment,
        )
        if resolved_uploads is None:
            raise ServiceError("not_found", "resource was not found", status=404)

        total_bytes = 0
        source_upload = resolved_uploads["source_upload"]
        source_authority: Mapping[str, Any] | None = None
        if source_upload is not None:
            expected_type = (
                "main_video" if input_type == "uploaded_video" else "main_audio"
            )
            expected_mime_prefix = "video/" if input_type == "uploaded_video" else "audio/"
            duration = source_upload["duration_ms"]
            size = source_upload["observed_size"]
            if (
                source_upload["status"] != "completed"
                or source_upload["upload_type"] != expected_type
                or not isinstance(source_upload["observed_mime"], str)
                or not source_upload["observed_mime"].startswith(expected_mime_prefix)
                or isinstance(duration, bool)
                or not isinstance(duration, int)
                or not _MIN_MEDIA_DURATION_MS <= duration <= _MAX_MEDIA_DURATION_MS
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ServiceError("input_source_invalid", "uploaded source is invalid")
            if size > _MAX_UPLOAD_BYTES:
                raise ServiceError(
                    "input_upload_total_exceeded",
                    "selected uploads exceed 1 GiB",
                )
            total_bytes = size
            source_authority = {
                "upload_id": source_upload["upload_id"],
                "upload_type": source_upload["upload_type"],
                "mime_type": source_upload["observed_mime"],
                "size_bytes": size,
                "sha256": source_upload["sha256"],
                "duration_ms": duration,
                "width": source_upload["width"],
                "height": source_upload["height"],
                "probe_json": source_upload["probe_json"],
            }

        materials = resolved_uploads["materials"]
        material_authorities: list[dict[str, Any]] = []
        for material in materials:
            size = material["size_bytes"]
            if (
                material["source_kind"] != "uploaded"
                or material["mime_type"] not in _IMAGE_MIMES
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= _MAX_IMAGE_BYTES
            ):
                raise ServiceError("material_source_invalid", "material source is invalid")
            if total_bytes > _MAX_UPLOAD_BYTES - size:
                raise ServiceError(
                    "input_upload_total_exceeded",
                    "selected uploads exceed 1 GiB",
                )
            total_bytes += size
            material_authorities.append(
                {
                    "material_id": material["material_id"],
                    "upload_id": material["upload_id"],
                    "mime_type": material["mime_type"],
                    "size_bytes": size,
                    "sha256": material["sha256"],
                    "metadata_json": material["metadata_json"],
                }
            )

        if input_type == "platform_talking_head":
            record = self._catalog_record(
                "platform_assets",
                "resolve_platform_asset",
                owner,
                normalized["source_asset_id"],
            )
            if record.get("asset_id") != normalized["source_asset_id"]:
                raise ServiceError("not_found", "resource was not found", status=404)
            self._require_catalog_duration(record)
            source_authority = dict(record)
        elif input_type == "existing_audio":
            record = self._catalog_record(
                "audio_assets",
                "resolve_audio_asset",
                owner,
                normalized["source_asset_id"],
            )
            if record.get("asset_id") != normalized["source_asset_id"]:
                raise ServiceError("not_found", "resource was not found", status=404)
            self._require_catalog_duration(record)
            source_authority = dict(record)
        elif input_type == "script_to_audio_video":
            voice_id = normalized["tts_input"]["voice_id"]
            record = self._catalog_record(
                "voices", "resolve_voice", owner, voice_id
            )
            if record.get("voice_id") != voice_id or record.get("status") not in {
                "active",
                "available",
                "ready",
            }:
                raise ServiceError("not_found", "resource was not found", status=404)
            source_authority = dict(record)

        template_version = None
        template_authority: Mapping[str, Any] | None = None
        if normalized["creation_mode"] == "template_reference":
            template_id = normalized["template_id"]
            template = self._catalog_record(
                "templates",
                "resolve_template",
                template_id,
                normalized["ratio"],
            )
            template_version = template.get("version")
            if (
                template.get("template_id") != template_id
                or not isinstance(template_version, str)
                or not template_version
                or template.get("status", "published") != "published"
            ):
                raise ServiceError("not_found", "resource was not found", status=404)
            template_authority = dict(template)

        return {
            "materials": materials,
            "source_upload": source_upload,
            "template_version": template_version,
            "total_upload_bytes": total_bytes,
            "authority_snapshot": {
                "input_type": input_type,
                "source": source_authority,
                "materials": material_authorities,
                "template": template_authority,
            },
        }

    def _authority_token(self, owner: str, resolved: Mapping[str, Any]) -> str:
        if (
            not isinstance(self.owner_hmac_secret, bytes)
            or len(self.owner_hmac_secret) < 16
            or len(set(self.owner_hmac_secret)) < 8
        ):
            raise ServiceError(
                "quote_capability_unavailable",
                "quote authority signing is unavailable",
                status=503,
            )
        payload = canonical_json(
            {
                "environment": self.environment,
                "owner": owner,
                "snapshot": resolved["authority_snapshot"],
            }
        )
        return hmac.new(
            self.owner_hmac_secret, payload, hashlib.sha256
        ).hexdigest()[:32]

    def _check_capacity(self, normalized: Mapping[str, Any]) -> CapacityDecision:
        if self.capacity_gate is None:
            raise ServiceError(
                "capacity_unavailable",
                "capacity could not be confirmed",
                status=503,
                retry_after=1,
            )
        try:
            decision = self.capacity_gate.check(normalized)
        except Exception as exc:
            raise ServiceError(
                "capacity_unavailable",
                "capacity could not be confirmed",
                status=503,
                retry_after=1,
            ) from exc
        if (
            not isinstance(decision, CapacityDecision)
            or not isinstance(decision.accepted, bool)
            or isinstance(decision.queue_slots, bool)
            or not isinstance(decision.queue_slots, int)
            or decision.queue_slots < 0
            or isinstance(decision.required_temp_bytes, bool)
            or not isinstance(decision.required_temp_bytes, int)
            or decision.required_temp_bytes < 0
            or decision.required_temp_bytes > (1 << 63) - 1
            or decision.retry_after is not None
            and (
                isinstance(decision.retry_after, bool)
                or not isinstance(decision.retry_after, int)
                or not 1 <= decision.retry_after <= 3_600
            )
        ):
            raise ServiceError(
                "capacity_unavailable",
                "capacity could not be confirmed",
                status=503,
                retry_after=1,
            )
        if not decision.accepted:
            raise ServiceError(
                "capacity_unavailable",
                "capacity is temporarily unavailable",
                status=503,
                retry_after=decision.retry_after or 1,
            )
        return decision

    @staticmethod
    def _quote_response(quote: Any) -> dict[str, Any]:
        parts = {name: dict(value) for name, value in quote.parts.items()}
        return {
            "quote_id": quote.quote_id,
            "request_sha256": quote.request_sha256,
            "request_fingerprint": quote.request_sha256,
            "pricing_version": quote.pricing_version,
            "template_id": quote.template_id,
            "template_version": quote.template_version,
            "min_points": quote.min_points,
            "max_points": quote.max_points,
            "breakdown": {
                "parts": parts,
                "min_points": quote.min_points,
                "max_points": quote.max_points,
            },
            "expires_at": quote.expires_at,
            "created_at": quote.created_at,
        }

    @staticmethod
    def _quote_row_response(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            breakdown = parse_strict_json(
                row["breakdown_json"],
                max_bytes=64 * 1024,
                max_depth=8,
                max_items=128,
                max_string_chars=256,
            )
        except ContractError as exc:
            raise ServiceError(
                "quote_storage_invalid", "stored quote is invalid", status=503
            ) from exc
        if not isinstance(breakdown, dict):
            raise ServiceError(
                "quote_storage_invalid", "stored quote is invalid", status=503
            )
        return {
            "quote_id": row["quote_id"],
            "request_sha256": row["request_sha256"],
            "request_fingerprint": row["request_sha256"],
            "pricing_version": row["pricing_version"],
            "template_id": row["template_id"],
            "template_version": row["template_version"],
            "min_points": row["min_points"],
            "max_points": row["max_points"],
            "breakdown": breakdown,
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def _quote(
        self,
        owner: str,
        request: Mapping[str, Any],
        *,
        now: int,
        quote_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        now = _require_now(now)
        self._require_write()
        normalized = self._normalize_request(request)
        resolved = self._resolve_authorities(owner, normalized)
        self._check_capacity(normalized)
        authority_token = self._authority_token(owner, resolved)
        quote_base = self._new_id("quote") if quote_id is None else quote_id
        final_quote_id = f"{quote_base}-{authority_token}"
        if len(final_quote_id) > 128:
            raise ServiceError(
                "quote_identity_invalid", "quote identity is invalid", status=503
            )
        existing = self.store.get_quote(
            owner, final_quote_id, environment=self.environment
        )
        if existing is not None:
            if (
                existing["request_sha256"] != request_fingerprint(normalized)
                or existing["normalized_request_json"]
                != canonical_json(normalized).decode("utf-8")
                or existing["template_version"] != resolved["template_version"]
            ):
                raise ServiceError(
                    "quote_conflict",
                    "quote identity conflicts with different frozen input",
                    status=409,
                )
            if now >= existing["expires_at"]:
                raise ServiceError("quote_expired", "quote has expired", status=409)
            return self._quote_row_response(existing)
        try:
            quote = billing_create_quote(
                owner,
                normalized,
                now=now,
                store=self.store,
                quote_id=final_quote_id,
            )
        except BillingError as exc:
            raise ServiceError(exc.error_code, exc.message, status=409) from exc
        if (
            quote.template_id is not None
            and quote.template_version != resolved["template_version"]
        ):
            raise ServiceError(
                "quote_template_mismatch",
                "resolved template does not match the frozen quote",
                status=409,
            )
        return self._quote_response(quote)

    def quote(
        self,
        owner: str,
        request: Mapping[str, Any],
        *,
        now: int,
    ) -> dict[str, Any]:
        return self._quote(owner, request, now=now)

    @staticmethod
    def _validate_idempotency_key(
        value: Any,
        *,
        allow_retry_namespace: bool = False,
    ) -> str:
        valid = False
        if isinstance(value, str):
            if allow_retry_namespace:
                valid = (
                    len(value) <= 320
                    and re.fullmatch(r"retry:[A-Za-z0-9._:-]+", value) is not None
                )
            else:
                valid = (
                    not value.startswith("retry:")
                    and _IDEMPOTENCY_KEY.fullmatch(value) is not None
                )
        if not valid:
            raise ServiceError(
                "idempotency_key_invalid",
                "Idempotency-Key is invalid",
            )
        return value

    @staticmethod
    def _public_job(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "state": row["state"],
            "stage": row["state"],
            "predecessor_job_id": row["predecessor_job_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error_code": row["error_code"],
        }

    def _create_job(
        self,
        owner: str,
        request: Mapping[str, Any],
        quote_id: str,
        idempotency_key: str,
        *,
        now: int,
        allow_retry_namespace: bool = False,
        predecessor_job_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        quote_id = _require_identifier("quote_id", quote_id)
        idempotency_key = self._validate_idempotency_key(
            idempotency_key, allow_retry_namespace=allow_retry_namespace
        )
        now = _require_now(now)
        normalized = self._normalize_request(request)
        self._require_feature_enabled()
        fingerprint = request_fingerprint(normalized)
        normalized_json = canonical_json(normalized).decode("utf-8")
        material_bindings = [
            {"material_id": material_id, "purpose": "supplemental", "ordinal": index}
            for index, material_id in enumerate(normalized["material_asset_ids"])
        ]
        atomic_store = _AtomicCreateStoreView(
            self.store,
            predecessor_job_id=predecessor_job_id,
            material_bindings=material_bindings,
        )

        existing = self.store.get_job_by_idempotency_for_owner(
            owner, idempotency_key, environment=self.environment
        )
        if existing is not None:
            if (
                existing["quote_id"] != quote_id
                or existing["request_sha256"] != fingerprint
                or existing["normalized_request_json"] != normalized_json
            ):
                raise ServiceError(
                    "idempotency_conflict",
                    "Idempotency-Key was reused with different input",
                    status=409,
                )
            try:
                outcome = billing_create_job_with_predebit(
                    owner,
                    normalized,
                    quote_id,
                    idempotency_key,
                    now=now,
                    store=atomic_store,
                    environment=self.environment,
                )
            except BillingError as exc:
                raise ServiceError(exc.error_code, exc.message, status=409) from exc
            replay = self.store.get_job_for_owner(
                owner, outcome.job_id, environment=self.environment
            )
            if replay is None:
                raise ServiceError(
                    "job_storage_failed", "created job is unavailable", status=503
                )
            return self._public_job(replay)

        self._require_write()
        quote = self.store.get_quote(owner, quote_id, environment=self.environment)
        if quote is None:
            raise ServiceError("quote_not_found", "quote was not found", status=404)
        if now >= quote["expires_at"]:
            raise ServiceError("quote_expired", "quote has expired", status=409)
        if (
            quote["request_sha256"] != fingerprint
            or quote["normalized_request_json"] != normalized_json
        ):
            raise ServiceError(
                "quote_request_mismatch",
                "request does not match the frozen quote",
                status=409,
            )
        resolved = self._resolve_authorities(owner, normalized)
        authority_token = self._authority_token(owner, resolved)
        if not quote_id.endswith(f"-{authority_token}"):
            raise ServiceError(
                "quote_authority_mismatch",
                "resolved authorities changed after quote",
                status=409,
            )
        if (
            quote["template_version"] is not None
            and quote["template_version"] != resolved["template_version"]
        ):
            raise ServiceError(
                "quote_template_mismatch",
                "template version does not match the frozen quote",
                status=409,
            )
        self._check_capacity(normalized)
        try:
            outcome = billing_create_job_with_predebit(
                owner,
                normalized,
                quote_id,
                idempotency_key,
                now=now,
                store=atomic_store,
                environment=self.environment,
            )
        except BillingError as exc:
            status = 409 if exc.error_code != "quote_not_found" else 404
            raise ServiceError(exc.error_code, exc.message, status=status) from exc
        job = self.store.get_job_for_owner(
            owner, outcome.job_id, environment=self.environment
        )
        if job is None:
            raise ServiceError("job_storage_failed", "created job is unavailable", status=503)
        return self._public_job(job)

    def create_job(
        self,
        owner: str,
        request: Mapping[str, Any],
        quote_id: str,
        idempotency_key: str,
        *,
        now: int,
    ) -> dict[str, Any]:
        return self._create_job(
            owner,
            request,
            quote_id,
            idempotency_key,
            now=now,
        )

    def retry_job(
        self,
        owner: str,
        predecessor_job_id: str,
        idempotency_key: str,
        *,
        now: int,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        predecessor_job_id = _require_identifier(
            "predecessor_job_id", predecessor_job_id
        )
        client_key = self._validate_idempotency_key(idempotency_key)
        now = _require_now(now)
        self._require_feature_enabled()
        predecessor = self.store.get_job_for_owner(
            owner, predecessor_job_id, environment=self.environment
        )
        if predecessor is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        if predecessor["state"] not in {"refunded", "prehold_absent"}:
            raise ServiceError(
                "retry_not_allowed",
                "only a terminal failed job can be retried",
                status=409,
            )
        try:
            request = parse_strict_json(
                predecessor["normalized_request_json"],
                max_bytes=64 * 1024,
                max_depth=12,
                max_items=128,
                max_string_chars=4_000,
            )
        except ContractError as exc:
            raise ServiceError("retry_source_invalid", "predecessor request is invalid", status=409) from exc

        internal_key = f"retry:{predecessor_job_id}:{client_key}"
        existing = self.store.get_job_by_idempotency_for_owner(
            owner, internal_key, environment=self.environment
        )
        if existing is not None:
            if existing["predecessor_job_id"] != predecessor_job_id:
                raise ServiceError(
                    "idempotency_conflict",
                    "retry identity conflicts with another predecessor",
                    status=409,
                )
            return self._create_job(
                owner,
                request,
                existing["quote_id"],
                internal_key,
                now=now,
                allow_retry_namespace=True,
                predecessor_job_id=predecessor_job_id,
            )
        self._require_write()
        if (
            not isinstance(self.owner_hmac_secret, bytes)
            or len(self.owner_hmac_secret) < 16
        ):
            raise ServiceError(
                "retry_capability_unavailable",
                "retry capability is unavailable",
                status=503,
            )
        digest = hmac.new(
            self.owner_hmac_secret,
            canonical_json([owner, predecessor_job_id, client_key]),
            hashlib.sha256,
        ).hexdigest()[:32]
        quote_generation = now // _QUOTE_TTL_MS
        quote_id = f"retryquote-{digest}-{quote_generation}"
        quote = self._quote(owner, request, now=now, quote_id=quote_id)
        successor = self._create_job(
            owner,
            request,
            quote["quote_id"],
            internal_key,
            now=now,
            allow_retry_namespace=True,
            predecessor_job_id=predecessor_job_id,
        )
        return successor

    def get_job(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = _require_owner(owner)
        job_id = _require_identifier("job_id", job_id)
        row = self.store.get_job_for_owner(owner, job_id, environment=self.environment)
        if row is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        return self._public_job(row)

    def list_jobs(
        self,
        owner: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        try:
            page = self.store.list_jobs_for_owner(
                owner,
                cursor=cursor,
                limit=limit,
                environment=self.environment,
            )
        except StoreError as exc:
            raise ServiceError(exc.error_code, exc.message) from exc
        return {
            "items": [self._public_job(row) for row in page["items"]],
            "next_cursor": page["next_cursor"],
        }

    def get_capabilities(self, owner: str) -> dict[str, Any]:
        _require_owner(owner)
        report = self._capability_report()
        if report is None:
            response = {
                "items": {},
                "runtime_versions": {},
                "current_schema_hashes": {},
                "historical_schema_hashes": {},
                "allows_existing_reads": True,
                "accepts_uploads": False,
                "accepts_new_jobs": False,
                "feature_enabled": self.enabled,
            }
        else:
            response = {
                "items": {
                    name: {
                        "status": item.status,
                        "reason_code": item.reason_code,
                        "detail": item.detail,
                    }
                    for name, item in report.items.items()
                },
                "runtime_versions": dict(report.runtime_versions),
                "current_schema_hashes": dict(report.current_schema_hashes),
                "historical_schema_hashes": {
                    name: list(values)
                    for name, values in report.historical_schema_hashes.items()
                },
                "allows_existing_reads": report.allows_existing_reads,
                "accepts_uploads": self._accepts_uploads(report),
                "accepts_new_jobs": self._accepts_new_jobs(report),
                "feature_enabled": self.enabled,
            }
        if self.environment == "test":
            response["acceptance"] = {
                "environment": "test",
                "deployed_sha": self.deployed_sha,
                "active_v3_jobs": self.store.count_active_jobs(),
                "v3_enabled": self.enabled,
                "providers_ready": self._acceptance_providers_ready(report),
                "accepts_uploads": self._accepts_uploads(report),
                "accepts_new_jobs": self._accepts_new_jobs(report),
            }
        return response

    def _catalog_list(
        self,
        owner: str,
        capability: str,
        method_name: str,
    ) -> dict[str, Any]:
        owner = _require_owner(owner)
        if self.source_catalog is None:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            )
        method = getattr(self.source_catalog, method_name, None)
        if method is None:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            )
        try:
            rows = method(owner)
        except Exception as exc:
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability is unavailable",
                status=503,
            ) from exc
        if not isinstance(rows, (list, tuple)):
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability returned invalid data",
                status=503,
            )
        try:
            safe_rows = [_public_catalog_record(capability, row) for row in rows]
        except (KeyError, TypeError, ValueError):
            raise ServiceError(
                f"{capability}_unavailable",
                f"{capability.replace('_', ' ')} capability returned invalid data",
                status=503,
            )
        return {"items": safe_rows}

    def list_platform_assets(self, owner: str) -> dict[str, Any]:
        return self._catalog_list(owner, "platform_assets", "list_platform_assets")

    def authorize_platform_preview(self, owner: str, asset_id: str) -> dict[str, Any]:
        owner = _require_owner(owner)
        asset_id = _require_identifier("asset_id", asset_id)
        catalog = getattr(self, "platform_catalog", self.source_catalog)
        method = getattr(catalog, "preview", None)
        if not callable(method):
            raise ServiceError("platform_assets_unavailable", "preview unavailable", status=503)
        try:
            result = method(owner, asset_id)
        except Exception as exc:
            raise ServiceError("platform_assets_unavailable", "preview unavailable", status=503) from exc
        if not isinstance(result, Mapping):
            raise ServiceError("not_found", "resource was not found", status=404)
        safe: dict[str, Any] = {"asset_id": asset_id, "expires_in": 300}
        for source, target in (("video_url", "preview_url"), ("cover_url", "cover_url")):
            value = result.get(source)
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or not value.startswith("/api/gen/file/")
                or "?" in value
                or "\\" in value
                or ".." in value.split("/")
            ):
                raise ServiceError("platform_assets_unavailable", "preview unavailable", status=503)
            safe[target] = value
        if "preview_url" not in safe:
            raise ServiceError("not_found", "resource was not found", status=404)
        return safe

    def list_audio_assets(self, owner: str) -> dict[str, Any]:
        return self._catalog_list(owner, "audio_assets", "list_audio_assets")

    def list_voices(self, owner: str) -> dict[str, Any]:
        return self._catalog_list(owner, "voices", "list_voices")

    def list_templates(self, owner: str) -> dict[str, Any]:
        return self._catalog_list(owner, "templates", "list_templates")

    @staticmethod
    def _redact_public_value(value: Any, *, field_name: str | None = None) -> Any:
        blocked = (
            "authorization",
            "cos_key",
            "cost",
            "credential",
            "local_path",
            "manifest",
            "object_key",
            "path",
            "provider",
            "raw_",
            "secret",
            "signed",
            "token",
            "transcript",
            "url",
        )
        if isinstance(value, Mapping):
            return {
                key: EditV3Service._redact_public_value(item, field_name=key)
                for key, item in value.items()
                if isinstance(key, str)
                and not any(marker in key.lower() for marker in blocked)
            }
        if isinstance(value, list):
            return [
                EditV3Service._redact_public_value(item, field_name=field_name)
                for item in value
            ]
        if isinstance(value, str) and (
            _looks_like_private_reference(
                value,
                allow_mime=isinstance(field_name, str)
                and field_name in {"mime_type", "content_type"},
            )
        ):
            return "[redacted]"
        return value

    def get_plan(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = _require_owner(owner)
        job_id = _require_identifier("job_id", job_id)
        job = self.store.get_job_for_owner(owner, job_id, environment=self.environment)
        if job is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        row = self.store.get_latest_plan_for_owner(
            owner, job_id, environment=self.environment
        )
        if row is None:
            raise ServiceError(
                "plan_not_ready", "director plan is not ready", status=409
            )
        if not schema_hash_is_accepted(
            "edit-plan-2.0.schema.json", str(row["schema_sha256"])
        ):
            raise ServiceError(
                "plan_schema_unsupported", "stored plan schema is unsupported", status=503
            )
        try:
            plan = parse_strict_json(
                row["normalized_plan_json"],
                max_bytes=512 * 1024,
                max_depth=24,
                max_items=5_000,
                max_string_chars=4_000,
            )
        except ContractError as exc:
            raise ServiceError(
                "plan_storage_invalid", "stored plan is invalid", status=503
            ) from exc
        return {
            "job_id": job_id,
            "plan": self._redact_public_value(plan),
            "plan_sha256": row["plan_sha256"],
        }

    def get_result(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = _require_owner(owner)
        job_id = _require_identifier("job_id", job_id)
        job = self.store.get_job_for_owner(owner, job_id, environment=self.environment)
        if job is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        if job["result_json"] is None:
            raise ServiceError("result_not_ready", "job result is not ready", status=409)
        try:
            result = parse_strict_json(
                job["result_json"],
                max_bytes=256 * 1024,
                max_depth=16,
                max_items=1_000,
                max_string_chars=4_000,
            )
        except ContractError as exc:
            raise ServiceError(
                "result_storage_invalid", "stored result is invalid", status=503
            ) from exc
        public_result = self._redact_public_value(result)
        object_key = result.get("delivery_object_key") if isinstance(result, Mapping) else None
        if isinstance(object_key, str) and callable(self._result_signer):
            try:
                public_result["play_url"] = self._result_signer(object_key, 300, None)
                public_result["download_url"] = self._result_signer(
                    object_key, 300, f"ai-edit-v3-{job_id}.mp4"
                )
                public_result["expires_in"] = 300
            except Exception as exc:
                raise ServiceError(
                    "result_storage_invalid", "result authorization failed", status=503
                ) from exc
        return {
            "job_id": job_id,
            "state": job["state"],
            "result": public_result,
        }

    def get_acceptance_evidence(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = _require_owner(owner)
        job_id = _require_identifier("job_id", job_id)
        if self.environment != "test":
            raise ServiceError("not_found", "resource was not found", status=404)
        job = self.store.get_job_for_owner(
            owner, job_id, environment=self.environment
        )
        if job is None:
            raise ServiceError("not_found", "resource was not found", status=404)
        reader = getattr(self._acceptance_evidence_reader, "read", None)
        if not callable(reader):
            raise ServiceError(
                "acceptance_evidence_unavailable",
                "acceptance evidence is unavailable",
                status=503,
            )
        try:
            evidence = reader(owner, dict(job))
        except FileNotFoundError as exc:
            raise ServiceError(
                "acceptance_evidence_not_ready",
                "acceptance evidence is not ready",
                status=409,
            ) from exc
        except Exception as exc:
            raise ServiceError(
                "acceptance_evidence_unavailable",
                "acceptance evidence is unavailable",
                status=503,
            ) from exc
        required = {
            "normalized_request_sha256", "attempt_id", "stage_timings_ms",
            "plan_schema_sha256", "material_decisions", "provider_usage",
            "audio_evidence", "renderer_build_id", "render_manifest_sha256",
            "qc", "settlement", "publication_generation", "asset_id",
            "stable_cos_key", "output_sha256",
        }
        if not isinstance(evidence, Mapping) or set(evidence) != required:
            raise ServiceError(
                "acceptance_evidence_invalid",
                "acceptance evidence is invalid",
                status=503,
            )
        def unsafe_acceptance_value(value: Any, field_name: str | None = None) -> bool:
            if isinstance(value, Mapping):
                return any(
                    not isinstance(key, str)
                    or any(marker in key.lower() for marker in (
                        "access_key", "api_key", "authorization", "cookie",
                        "credential", "local_path", "password", "private_key",
                        "secret", "session", "signed", "token", "url",
                    ))
                    or unsafe_acceptance_value(item, key)
                    for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(unsafe_acceptance_value(item, field_name) for item in value)
            if not isinstance(value, str):
                return False
            if field_name == "stable_cos_key":
                return not (
                    value.startswith(f"{self.environment}/ai-edit-v3/")
                    and not _is_absolute_local_path(value)
                    and "\\" not in value
                    and "?" not in value
                    and "#" not in value
                    and all(part not in {"", ".", ".."} for part in value.split("/"))
                )
            return bool(
                _has_control_or_surrogate(value)
                or _is_absolute_local_path(value)
                or "\\" in value
                or "://" in value
                or _QUERY_SECRET_VALUE.search(value) is not None
            )
        if unsafe_acceptance_value(evidence):
            raise ServiceError(
                "acceptance_evidence_invalid",
                "acceptance evidence is invalid",
                status=503,
            )
        try:
            serialized = json.dumps(
                dict(evidence), ensure_ascii=False, allow_nan=False,
                separators=(",", ":"), sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError(
                "acceptance_evidence_invalid",
                "acceptance evidence is invalid",
                status=503,
            ) from exc
        if (
            evidence.get("normalized_request_sha256") != job["request_sha256"]
            or re.search(
                r"https?://|bearer\s|(?:token|secret|session|cookie|credential|authorization)[=:]",
                serialized,
                re.I,
            )
        ):
            raise ServiceError(
                "acceptance_evidence_invalid",
                "acceptance evidence is invalid",
                status=503,
            )
        return {
            "job_id": job_id,
            "state": job["state"],
            "evidence": json.loads(serialized),
        }
