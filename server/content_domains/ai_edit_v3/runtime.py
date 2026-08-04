"""Dependency injection and fail-closed capability preflight for AI Edit V3."""

from __future__ import annotations

import importlib.metadata
import hashlib
import json
import math
import platform
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from server.content_domains.video_asset_publish import AssetPublisher

from .billing import PointsLedger
from .contracts import LeaseClaim, schema_sha256
from .feature import (
    CapabilityItem,
    CapabilityReport,
    CapabilityUnavailable,
    FeatureConfig,
    load_config,
)
from .providers.base import ProviderResult
from .providers.elevenlabs import ElevenLabsAudioGenerator
from .renderers import Renderer
from .store import LeaseLost, V3Store, assert_isolated_db


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_SCHEMA_HASHES = MappingProxyType({
    name: schema_sha256(name)
    for name in (
        "director-decision-v1.schema.json", "edit-plan-2.0.schema.json",
        "render-manifest-v1.schema.json", "render-manifest-v2.schema.json",
        "quality-verdict-v1.schema.json",
    )
})
_HISTORICAL_SCHEMA_HASHES = MappingProxyType({
    "edit-plan-2.0.schema.json": frozenset({
        "b96c059fa2e4ef7d91cd48278b474d61a34606f1cbce6963c3b65fa66f7d046c",
        "1dfc64bdfe8bee1a37d2ceb8eb7d6f52f2c2e3df1f80be9919d42a788ec6627c",
    }),
    "render-manifest-v1.schema.json": frozenset({
        "eb1f656712ff94bbac31e9d8824d878795110597bca0141814839020f9e2cbc0",
    }),
})


def schema_hash_is_accepted(name: str, digest: str) -> bool:
    """Read historical frozen evidence without permitting it for a new version."""
    if not isinstance(name, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return False
    return digest == _SCHEMA_HASHES.get(name) or digest in _HISTORICAL_SCHEMA_HASHES.get(name, frozenset())
_DEPENDENCY_FIELDS = (
    "cos",
    "tts",
    "asr",
    "director",
    "image_generator",
    "audio_generator",
    "renderer",
)
_DEFAULT_REQUEST_CAPABILITIES = (*_DEPENDENCY_FIELDS, "stage_handlers")


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("runtime_checkpoint_invalid")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("runtime_checkpoint_invalid")


def _identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or _has_control(value)
    ):
        raise ValueError(f"runtime_{field_name}_invalid")
    return value


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...


@runtime_checkable
class ProcessSupervisor(Protocol):
    def terminate_job(self, job_id: str) -> None: ...


class LeaseHeartbeat:
    """Renew one claim until stopped; any failed renewal is permanently fatal."""

    def __init__(self, claim, lease_seconds, clock, renew):
        if not isinstance(claim, LeaseClaim):
            raise ValueError("runtime_claim_invalid")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("runtime_lease_seconds_invalid")
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._renew = renew
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ai-edit-v3-lease-{claim.job_id}",
            daemon=True,
        )
        self._started = False

    def _run(self):
        if self._stop.wait(max(0.01, self._lease_seconds / 3)):
            return
        while not self._stop.is_set():
            try:
                now_ms = int(self._clock.now() * 1000)
                renewed = self._renew(
                    self._claim, self._lease_seconds, now_ms
                )
            except Exception:
                renewed = False
            if not renewed:
                self._lost.set()
                return
            if self._stop.wait(max(0.01, self._lease_seconds / 3)):
                return

    def start(self):
        if not self._started:
            self._started = True
            self._thread.start()

    def assert_active(self):
        if self._lost.is_set():
            raise LeaseLost("lease_lost", "lease renewal failed")

    def close(self):
        self._stop.set()
        if self._started:
            self._thread.join()


@runtime_checkable
class StageHandler(Protocol):
    def __call__(
        self,
        job: Mapping[str, Any],
        context: StageContext,
    ) -> StageOutcome: ...


@dataclass(frozen=True, slots=True)
class StageContext:
    claim: LeaseClaim
    attempt_id: str
    stage_attempt_id: str
    deadline_at: float
    assert_active: Callable[[], None]

    def __post_init__(self) -> None:
        if not isinstance(self.claim, LeaseClaim):
            raise ValueError("runtime_claim_invalid")
        object.__setattr__(
            self, "attempt_id", _identifier(self.attempt_id, "attempt_id")
        )
        object.__setattr__(
            self,
            "stage_attempt_id",
            _identifier(self.stage_attempt_id, "stage_attempt_id"),
        )
        if (
            isinstance(self.deadline_at, bool)
            or not isinstance(self.deadline_at, (int, float))
            or not math.isfinite(self.deadline_at)
        ):
            raise ValueError("runtime_deadline_invalid")
        if not callable(self.assert_active):
            raise ValueError("runtime_assert_active_invalid")


@dataclass(frozen=True, slots=True)
class StageOutcome:
    next_state: str
    checkpoint: Mapping[str, Any]
    checkpoint_input_sha256: str
    provider_result: ProviderResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "next_state", _identifier(self.next_state, "next_state")
        )
        if not isinstance(self.checkpoint, Mapping):
            raise ValueError("runtime_checkpoint_invalid")
        if (
            not isinstance(self.checkpoint_input_sha256, str)
            or _SHA256.fullmatch(self.checkpoint_input_sha256) is None
        ):
            raise ValueError("runtime_checkpoint_sha256_invalid")
        if self.provider_result is not None and not isinstance(
            self.provider_result, ProviderResult
        ):
            raise ValueError("runtime_provider_result_invalid")
        object.__setattr__(self, "checkpoint", _freeze(self.checkpoint))


_PHASE_B_STAGE_NAMES = (
    "generating_voice",
    "normalizing",
    "transcribing",
    "aligning",
    "planning",
    "resolving_materials",
    "generating_images",
)

_ENTRY_STAGE_NAMES = ("queued",)

_PHASE_C_STAGE_NAMES = (
    "generating_audio",
    "mixing_audio",
    "compiling",
    "rendering",
    "quality_checking",
    "repair_planning",
    "staging_delivery",
)


def build_phase_b_stage_handlers(coordinator: Any) -> Mapping[str, StageHandler]:
    """Bind the seven Phase B stages without granting transition authority."""

    if coordinator is None or not callable(getattr(coordinator, "run_stage", None)):
        raise ValueError("phase_b_coordinator_invalid")

    def handler(name: str) -> StageHandler:
        def run(job: Mapping[str, Any], context: StageContext) -> StageOutcome:
            context.assert_active()
            outcome = coordinator.run_stage(name, job, context)
            if not isinstance(outcome, StageOutcome):
                raise ValueError("phase_b_stage_outcome_invalid")
            context.assert_active()
            return outcome

        return run

    return MappingProxyType({name: handler(name) for name in _PHASE_B_STAGE_NAMES})


def build_stage_handlers(coordinator: Any) -> Mapping[str, StageHandler]:
    """Bind the complete media state graph to one transition-free coordinator."""

    if coordinator is None or not callable(getattr(coordinator, "run_stage", None)):
        raise ValueError("stage_coordinator_invalid")

    def handler(name: str) -> StageHandler:
        def run(job: Mapping[str, Any], context: StageContext) -> StageOutcome:
            context.assert_active()
            outcome = coordinator.run_stage(name, job, context)
            if not isinstance(outcome, StageOutcome):
                raise ValueError("stage_outcome_invalid")
            context.assert_active()
            return outcome

        def probe_capability(capability: str, *, environment: str | None):
            expected = f"stage_handler:{name}"
            return {
                "available": capability == expected,
                "environment": environment,
                "reason_code": "capability_ready" if capability == expected else "capability_unknown",
            }

        run.probe_capability = probe_capability  # type: ignore[attr-defined]
        return run

    names = (*_ENTRY_STAGE_NAMES, *_PHASE_B_STAGE_NAMES, *_PHASE_C_STAGE_NAMES)
    return MappingProxyType({name: handler(name) for name in names})


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    store: V3Store
    clock: Clock
    points: PointsLedger
    assets: AssetPublisher
    cos: object | None
    tts: object | None
    asr: object | None
    director: object | None
    image_generator: object | None
    audio_generator: object | None
    renderer: Renderer | None
    process_supervisor: ProcessSupervisor
    stage_handlers: Mapping[str, StageHandler]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_handlers, Mapping) or any(
            not isinstance(name, str)
            or _CAPABILITY_NAME.fullmatch(name) is None
            for name in self.stage_handlers
        ):
            raise ValueError("runtime_stage_handlers_invalid")
        object.__setattr__(
            self,
            "stage_handlers",
            MappingProxyType(dict(self.stage_handlers)),
        )


@dataclass(frozen=True, slots=True)
class Runtime:
    config: FeatureConfig
    dependencies: RuntimeDependencies | None


def build_runtime(
    dependencies: RuntimeDependencies | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Runtime:
    """Build only local immutable state; real clients are never imported or opened."""

    if dependencies is not None and not isinstance(dependencies, RuntimeDependencies):
        raise TypeError("runtime_dependencies_invalid")
    return Runtime(config=load_config(env), dependencies=dependencies)


def _ready(detail: str = "side-effect-free capability probe passed") -> CapabilityItem:
    return CapabilityItem("configured_and_wired", "capability_ready", detail)


def _implemented(detail: str) -> CapabilityItem:
    return CapabilityItem("implemented", "capability_ready", detail)


def _missing(reason_code: str, detail: str) -> CapabilityItem:
    if _REASON_CODE.fullmatch(reason_code) is None:
        reason_code = "capability_unavailable"
    return CapabilityItem("missing_or_unavailable", reason_code, detail)


def _has_methods(dependency: object, names: tuple[str, ...]) -> bool:
    try:
        return all(callable(getattr(dependency, name, None)) for name in names)
    except Exception:
        return False


def _explicit_probe_result(
    dependency: object,
    capability: str,
    environment: str | None,
) -> Mapping[str, object] | bool | CapabilityItem | None:
    probe = getattr(dependency, "probe_capability", None)
    if callable(probe):
        return probe(capability, environment=environment)
    contract = getattr(dependency, "capabilities", None)
    if isinstance(contract, Mapping):
        if capability in contract:
            return contract[capability]
        if "*" in contract:
            return contract["*"]
    return None


def _probe_dependency(
    dependency: object | None,
    capability: str,
    environment: str | None,
    *,
    required_methods: tuple[str, ...] = (),
) -> CapabilityItem:
    if dependency is None:
        return _missing("capability_not_injected", "capability is not injected")
    if required_methods and not _has_methods(dependency, required_methods):
        return _missing(
            "capability_interface_invalid",
            "injected dependency does not satisfy its frozen interface",
        )
    try:
        result = _explicit_probe_result(dependency, capability, environment)
    except Exception:
        return _missing(
            "capability_probe_failed",
            "side-effect-free capability probe failed",
        )
    if result is None:
        return _missing(
            "capability_contract_missing",
            "injected dependency has no explicit capability contract",
        )
    if isinstance(result, CapabilityItem):
        if result.status == "missing_or_unavailable":
            return _missing(result.reason_code, "capability contract reported unavailable")
        return _ready()
    if isinstance(result, bool):
        return _ready() if result else _missing(
            "capability_unavailable", "capability contract reported unavailable"
        )
    if not isinstance(result, Mapping) or not isinstance(
        result.get("available"), bool
    ):
        return _missing(
            "capability_probe_invalid",
            "capability probe returned an invalid contract",
        )
    if not result["available"]:
        reason = result.get("reason_code", "capability_unavailable")
        return _missing(
            reason if isinstance(reason, str) else "capability_unavailable",
            "capability probe reported unavailable",
        )
    claimed_environment = result.get("environment")
    cos_claims = (
        "production_prefix_writable",
        "test_only",
        "test_prefix_only",
    )
    if capability == "cos" and any(
        name in result and not isinstance(result[name], bool)
        for name in cos_claims
    ):
        return _missing(
            "capability_probe_invalid",
            "COS capability probe returned an invalid authority claim",
        )
    if claimed_environment is not None and claimed_environment != environment:
        return _missing(
            "capability_environment_mismatch",
            "capability is wired for a different environment",
        )
    if capability == "cos":
        if environment == "test" and result.get("production_prefix_writable") is True:
            return _missing(
                "cos_environment_scope_invalid",
                "test capability claims production-prefix write access",
            )
        if environment == "production" and (
            result.get("test_only") is True
            or result.get("test_prefix_only") is True
        ):
            return _missing(
                "cos_environment_scope_invalid",
                "production capability claims test-only authority",
            )
    return _ready()


def _schema_items(
    items: dict[str, CapabilityItem],
    versions: dict[str, str],
) -> None:
    for name, expected in _SCHEMA_HASHES.items():
        item_name = f"schema:{name}"
        try:
            actual = schema_sha256(name)
        except Exception:
            versions[name] = "unavailable"
            items[item_name] = _missing(
                "schema_unavailable", "frozen schema is unavailable"
            )
            continue
        versions[name] = actual
        if actual != expected:
            items[item_name] = _missing(
                "schema_hash_mismatch", "frozen schema hash does not match"
            )
        else:
            items[item_name] = _implemented("frozen schema hash matches")


def _runtime_version_items(
    items: dict[str, CapabilityItem],
    versions: dict[str, str],
) -> None:
    versions["python"] = platform.python_version()
    versions["sqlite"] = sqlite3.sqlite_version
    try:
        jsonschema_version = importlib.metadata.version("jsonschema")
    except importlib.metadata.PackageNotFoundError:
        jsonschema_version = "unavailable"
    versions["jsonschema"] = jsonschema_version
    if jsonschema_version == "4.26.0":
        items["json_schema_runtime"] = _implemented(
            "exact JSON Schema runtime is installed"
        )
    else:
        items["json_schema_runtime"] = _missing(
            "jsonschema_version_mismatch",
            "exact JSON Schema runtime is unavailable",
        )


def _probe_store(
    store: object | None,
    environment: str | None,
) -> CapabilityItem:
    if isinstance(store, V3Store):
        try:
            assert_isolated_db(store.db_path, store.v2_db_path)
        except Exception:
            return _missing(
                "v3_store_isolation_unavailable",
                "V3 store isolation evidence is unavailable",
            )
        return _ready("native V3 store isolation validation passed")
    return _probe_dependency(store, "isolated_v3_store", environment)


def _probe_stage_handlers(
    handlers: Mapping[str, StageHandler] | None,
    environment: str | None,
    items: dict[str, CapabilityItem],
) -> CapabilityItem:
    if not handlers:
        return _missing("capability_not_injected", "stage handlers are not injected")
    all_ready = True
    for name, handler in handlers.items():
        item = _probe_dependency(
            handler,
            f"stage_handler:{name}",
            environment,
            required_methods=("__call__",),
        )
        items[f"stage_handler:{name}"] = item
        all_ready = all_ready and item.status != "missing_or_unavailable"
    if not all_ready:
        return _missing(
            "stage_handler_unavailable", "one or more stage handlers are unavailable"
        )
    return _ready("all injected stage handlers passed explicit probes")


def _is_ready(items: Mapping[str, CapabilityItem], names: tuple[str, ...]) -> bool:
    return all(
        name in items and items[name].status != "missing_or_unavailable"
        for name in names
    )


def _common_gate_names() -> tuple[str, ...]:
    return (
        "feature_enabled",
        "isolated_v3_store",
        *(f"schema:{name}" for name in _SCHEMA_HASHES),
        "json_schema_runtime",
        "points_transaction_query",
        "asset_publication",
        "owner_hmac_reference",
        "capacity_limits",
        "clock",
        "process_supervisor",
    )


def _request_gate_names(
    required_capabilities: tuple[str, ...],
) -> tuple[str, ...]:
    requested = (
        _DEFAULT_REQUEST_CAPABILITIES
        if not required_capabilities
        else required_capabilities
    )
    return tuple(dict.fromkeys(("cos", *requested)))


def preflight(
    runtime: Runtime,
    *,
    required_capabilities: tuple[str, ...] = (),
) -> CapabilityReport:
    """Return capability evidence using probes only; never submit or mutate."""

    if not isinstance(runtime, Runtime):
        raise TypeError("runtime_invalid")
    if not isinstance(required_capabilities, tuple) or any(
        not isinstance(name, str) or _CAPABILITY_NAME.fullmatch(name) is None
        for name in required_capabilities
    ):
        raise ValueError("required_capabilities_invalid")

    config = runtime.config
    dependencies = runtime.dependencies
    items: dict[str, CapabilityItem] = {}
    versions: dict[str, str] = {}

    items["feature_enabled"] = (
        _implemented("V3 feature is enabled")
        if config.enabled
        else _missing("feature_disabled", "V3 feature is disabled")
    )
    items["visual_program_v1"] = _implemented(
        "V3.1 visual program is enabled"
        if config.visual_program_enabled
        else "V3.1 visual program is disabled"
    )
    items["visual_program_v1"] = CapabilityItem(
        status=items["visual_program_v1"].status,
        reason_code=(
            "visual_program_enabled"
            if config.visual_program_enabled
            else "visual_program_disabled"
        ),
        detail=items["visual_program_v1"].detail,
    )
    items["owner_hmac_reference"] = (
        _ready("owner-HMAC secret file reference is configured")
        if config.owner_hmac_secret_file is not None
        else _missing(
            "owner_hmac_reference_missing",
            "owner-HMAC secret file reference is unavailable",
        )
    )
    items["capacity_limits"] = (
        _ready("worker capacity limits are configured")
        if all(
            value is not None
            for value in (
                config.worker_concurrency,
                config.queue_capacity,
                config.temp_bytes_limit,
            )
        )
        else _missing("capacity_limits_missing", "worker capacity limits are unavailable")
    )
    _schema_items(items, versions)
    _runtime_version_items(items, versions)

    if dependencies is None:
        items["isolated_v3_store"] = _missing(
            "capability_not_injected", "V3 store is not injected"
        )
        items["clock"] = _missing("capability_not_injected", "clock is not injected")
        items["points_transaction_query"] = _missing(
            "capability_not_injected", "points query is not injected"
        )
        items["asset_publication"] = _missing(
            "capability_not_injected", "asset publication is not injected"
        )
        items["process_supervisor"] = _missing(
            "capability_not_injected", "process supervisor is not injected"
        )
        for name in _DEPENDENCY_FIELDS:
            items[name] = _missing(
                "capability_not_injected", f"{name} capability is not injected"
            )
        items["stage_handlers"] = _missing(
            "capability_not_injected", "stage handlers are not injected"
        )
    else:
        environment = config.environment
        items["isolated_v3_store"] = _probe_store(dependencies.store, environment)
        items["clock"] = _probe_dependency(
            dependencies.clock, "clock", environment, required_methods=("now",)
        )
        items["points_transaction_query"] = _probe_dependency(
            dependencies.points,
            "points_transaction_query",
            environment,
            required_methods=("deduct", "refund", "query_transaction"),
        )
        items["asset_publication"] = _probe_dependency(
            dependencies.assets,
            "asset_publication",
            environment,
            required_methods=(
                "register_generation",
                "prepare_hidden",
                "commit_publish",
                "cancel_publish",
                "query_decision",
            ),
        )
        for name in _DEPENDENCY_FIELDS:
            required_methods = ("render",) if name == "renderer" else ()
            items[name] = _probe_dependency(
                getattr(dependencies, name),
                name,
                environment,
                required_methods=required_methods,
            )
        items["process_supervisor"] = _probe_dependency(
            dependencies.process_supervisor,
            "process_supervisor",
            environment,
            required_methods=("terminate_job",),
        )
        items["stage_handlers"] = _probe_stage_handlers(
            dependencies.stage_handlers, environment, items
        )

    if dependencies is None:
        items["elevenlabs_audio"] = _implemented(
            "ElevenLabs music and sound-effect adapter is installed"
        )
    elif isinstance(dependencies.audio_generator, ElevenLabsAudioGenerator):
        items["elevenlabs_audio"] = items["audio_generator"]
    else:
        items["elevenlabs_audio"] = _missing(
            "elevenlabs_not_wired",
            "ElevenLabs adapter is not the injected audio generator",
        )

    items["content_safety"] = _missing(
        "content_safety_not_implemented",
        "content safety is not implemented in Phase A",
    )

    for name in required_capabilities:
        if name.startswith("stage_handler:") and name not in items:
            items[name] = _missing(
                "capability_not_injected", "required stage handler is not injected"
            )
        elif name not in items:
            items[name] = _missing(
                "capability_unknown", "required capability is unknown"
            )

    common = _common_gate_names()
    request = _request_gate_names(required_capabilities)
    enabled_and_common = config.enabled and _is_ready(items, common)
    return CapabilityReport(
        items=items,
        runtime_versions=versions,
        current_schema_hashes=_SCHEMA_HASHES,
        historical_schema_hashes={name: tuple(sorted(values)) for name, values in _HISTORICAL_SCHEMA_HASHES.items()},
        allows_existing_reads=True,
        accepts_uploads=enabled_and_common and _is_ready(items, ("cos",)),
        accepts_new_jobs=enabled_and_common and _is_ready(items, request),
    )


def assert_ready_for_request(
    runtime: Runtime,
    *,
    required_capabilities: tuple[str, ...] = (),
) -> CapabilityReport:
    report = preflight(
        runtime,
        required_capabilities=required_capabilities,
    )
    if report.accepts_new_jobs:
        return report
    gate_names = (*_common_gate_names(), *_request_gate_names(required_capabilities))
    reasons = [
        report.items[name].reason_code
        for name in gate_names
        if name in report.items
        and report.items[name].status == "missing_or_unavailable"
    ]
    raise CapabilityUnavailable(reasons)


def get_or_generate_director_decision(
    store: V3Store,
    claim: LeaseClaim,
    stage_attempt_id: str,
    context: Any,
    provider: Any,
    *,
    now_ms: int,
):
    """Reuse immutable decision evidence on replay before calling Qwen again."""

    from .director_decision import ValidatedDecision, generate_director_decision

    if not store.lease_owned(claim, now_ms):
        raise LeaseLost("lease_lost", "director decision lease is no longer owned")
    existing = store.get_director_decision(claim.job_id)
    if existing is not None:
        raw_output = existing["raw_output_json"]
        return ValidatedDecision(
            value=json.loads(existing["normalized_decision_json"]),
            provider_request_id=None,
            raw_output_json=raw_output,
            raw_output_sha256=hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
            decision_sha256=existing["decision_sha256"],
            schema_sha256=existing["schema_sha256"],
            candidates_sha256=existing["candidates_sha256"],
            prompt_version=existing["prompt_version"],
        )
    generated = generate_director_decision(context, provider)
    store.save_director_decision(
        claim,
        stage_attempt_id,
        generated,
        now_ms=now_ms,
    )
    return generated


__all__ = (
    "Clock",
    "LeaseHeartbeat",
    "ProcessSupervisor",
    "Runtime",
    "RuntimeDependencies",
    "StageContext",
    "StageHandler",
    "StageOutcome",
    "assert_ready_for_request",
    "build_runtime",
    "build_phase_b_stage_handlers",
    "build_stage_handlers",
    "get_or_generate_director_decision",
    "preflight",
    "schema_hash_is_accepted",
)
