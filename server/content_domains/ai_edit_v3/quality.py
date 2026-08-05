"""Blocking, fail-closed quality aggregation for AI Edit V3 final media."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, Sequence

from .contracts import ContractError, validate_quality_verdict
from .media import FinalMux


_BLOCKING = MappingProxyType({
    "media_decode_codec_dimensions": True, "av_duration_sync": True,
    "black_frames": True, "abnormal_freeze": True, "audio_integrity": True,
    "caption_fact_accuracy": True, "safe_area_and_text_visibility": True,
    "face_product_obstruction": True, "material_provenance": True,
    "material_semantic_identity": True, "generated_evidence_claim": True,
    "opening_hook_visual_consistency": False,
})
_REPAIRABLE = frozenset({
    "safe_area_and_text_visibility", "face_product_obstruction",
    "material_semantic_identity", "opening_hook_visual_consistency",
})


class VisualInspector(Protocol):
    def inspect(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("quality_evidence_invalid")


@dataclass(frozen=True)
class QualityFinding:
    check_id: str
    status: Literal["pass", "fail", "unknown"]
    blocking: bool
    repairable: bool
    measured: Mapping[str, int | float | str | bool]
    evidence: tuple[Mapping[str, Any], ...]
    executor: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.check_id not in _BLOCKING or self.status not in {"pass", "fail", "unknown"}:
            raise ValueError("quality_finding_invalid")
        if self.blocking is not _BLOCKING[self.check_id]:
            raise ValueError("quality_blocking_invalid")
        if self.repairable and self.check_id not in _REPAIRABLE:
            raise ValueError("quality_repairability_invalid")
        object.__setattr__(self, "measured", _freeze(self.measured))
        object.__setattr__(self, "evidence", tuple(_freeze(item) for item in self.evidence))
        object.__setattr__(self, "executor", _freeze(self.executor))


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    findings: tuple[QualityFinding, ...]
    repairable_ids: tuple[str, ...]
    can_repair: bool
    report_sha256: str


def _canonical_findings(findings: tuple[QualityFinding, ...]) -> bytes:
    payload = [{
        "check_id": item.check_id, "status": item.status,
        "blocking": item.blocking, "repairable": item.repairable,
        "measured": dict(item.measured), "evidence": [dict(value) for value in item.evidence],
        "executor": dict(item.executor),
    } for item in findings]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _technical_results(final_mux: FinalMux, manifest: Mapping[str, Any], render_report: Mapping[str, Any]) -> dict[str, tuple[bool, Mapping[str, Any]]]:
    output = manifest.get("output_spec", {})
    audit = final_mux.audit
    expected_duration = manifest.get("duration_ms")
    dimensions = (
        final_mux.video_codec == "h264" and final_mux.audio_codec == "aac"
        and final_mux.width == output.get("width") and final_mux.height == output.get("height")
        and final_mux.fps_num == output.get("fps_num") and final_mux.fps_den == output.get("fps_den")
        and final_mux.sample_rate == 48000 and final_mux.channels == 2 and audit.get("decode_ok") is True
    )
    frame_ms = 1000 * final_mux.fps_den / final_mux.fps_num
    duration_ok = (
        isinstance(expected_duration, int)
        and abs(final_mux.duration_ms - expected_duration) <= max(40, math.ceil(frame_ms))
        and abs(float(audit.get("video_start_ms", 9999))) <= frame_ms
        and abs(float(audit.get("audio_start_ms", 9999))) <= 40
    )
    render_silent = (
        render_report.get("status") == "done"
        and isinstance(render_report.get("output"), Mapping)
        and render_report["output"].get("silent") is True
        and isinstance(render_report.get("audit"), Mapping)
        and render_report["audit"].get("audio_elements") == 0
        and render_report["audit"].get("audible_video_elements") == 0
    )
    black_ms = audit.get("black_max_ms", 0)
    freeze_ms = audit.get("freeze_max_ms", 0)
    silence_ms = audit.get("speech_silence_max_ms", 0)
    peak = audit.get("true_peak_dbfs", -1.0)
    lufs = audit.get("integrated_lufs", -16.0)
    fingerprint = audit.get("audio_fingerprint_unique", True)
    return {
        "media_decode_codec_dimensions": (bool(dimensions and render_silent), {"width": final_mux.width, "height": final_mux.height}),
        "av_duration_sync": (bool(duration_ok), {"duration_ms": final_mux.duration_ms, "expected_duration_ms": expected_duration}),
        "black_frames": (isinstance(black_ms, (int, float)) and not isinstance(black_ms, bool) and black_ms <= 300, {"black_max_ms": black_ms}),
        "abnormal_freeze": (isinstance(freeze_ms, (int, float)) and not isinstance(freeze_ms, bool) and freeze_ms <= 2000, {"freeze_max_ms": freeze_ms}),
        "audio_integrity": (
            isinstance(silence_ms, (int, float)) and silence_ms <= 500
            and isinstance(peak, (int, float)) and -3 <= peak <= -0.1
            and isinstance(lufs, (int, float)) and -18 <= lufs <= -14
            and fingerprint is True,
            {"speech_silence_max_ms": silence_ms, "true_peak_dbfs": peak, "integrated_lufs": lufs},
        ),
    }


def _provenance_ok(manifest: Mapping[str, Any], owner_evidence: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
    hashes = owner_evidence.get("asset_hashes")
    assets = manifest.get("assets")
    if not isinstance(hashes, Mapping) or not isinstance(assets, list):
        return False, {"reason": "owner_evidence_invalid"}
    owner = owner_evidence.get("owner")
    job_id = owner_evidence.get("job_id")
    for asset in assets:
        if not isinstance(asset, Mapping) or hashes.get(asset.get("id")) != asset.get("sha256"):
            return False, {"reason": "asset_hash_mismatch"}
        provenance = asset.get("provenance")
        if isinstance(provenance, Mapping) and (provenance.get("owner") != owner or provenance.get("task_id") != job_id):
            return False, {"reason": "cross_owner_material"}
    return True, {"asset_count": len(assets), "owner_verified": True}


def _snapshot_contract(
    values: Sequence[Mapping[str, Any]],
    duration_ms: object,
) -> tuple[Mapping[str, Any], ...]:
    if (
        not isinstance(values, (list, tuple))
        or not 1 <= len(values) <= 6
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 1 <= duration_ms <= 600_000
    ):
        raise ValueError("quality_snapshot_evidence_invalid")
    frozen: list[Mapping[str, Any]] = []
    identities: set[tuple[str, int]] = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) != {
            "local_path", "frame_sha256", "timestamp_ms", "size_bytes"
        }:
            raise ValueError("quality_snapshot_evidence_invalid")
        path = item.get("local_path")
        digest = item.get("frame_sha256")
        timestamp_ms = item.get("timestamp_ms")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or not 0 <= timestamp_ms <= duration_ms
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 32 * 1024 * 1024
            or (digest, timestamp_ms) in identities
        ):
            raise ValueError("quality_snapshot_evidence_invalid")
        identities.add((digest, timestamp_ms))
        frozen.append(MappingProxyType({
            "local_path": path,
            "frame_sha256": digest,
            "timestamp_ms": timestamp_ms,
            "size_bytes": size_bytes,
        }))
    return tuple(frozen)


def _assert_snapshot_files_unchanged(values: Sequence[Mapping[str, Any]]) -> None:
    for item in values:
        path = item["local_path"]
        expected_size = item["size_bytes"]
        expected_sha = item["frame_sha256"]
        try:
            before = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != expected_size
            ):
                raise ValueError("quality_snapshot_evidence_changed")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            descriptor = os.open(os.fspath(path), flags)
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                opened = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not os.path.samestat(before, opened)
                    or opened.st_size != expected_size
                ):
                    raise ValueError("quality_snapshot_evidence_changed")
                hasher = hashlib.sha256()
                total = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size or total > 32 * 1024 * 1024:
                        raise ValueError("quality_snapshot_evidence_changed")
                    hasher.update(chunk)
        except OSError as exc:
            raise ValueError("quality_snapshot_evidence_changed") from exc
        if total != expected_size or hasher.hexdigest() != expected_sha:
            raise ValueError("quality_snapshot_evidence_changed")


def run_blocking_quality(
    final_mux: FinalMux,
    manifest: Mapping[str, Any],
    render_report: Mapping[str, Any],
    *,
    owner_evidence: Mapping[str, Any],
    visual_inspector: VisualInspector,
    snapshot_inputs: Sequence[Mapping[str, Any]],
    deadline_at: float,
) -> QualityReport:
    if not isinstance(final_mux, FinalMux) or not isinstance(manifest, Mapping) or not isinstance(render_report, Mapping):
        raise ValueError("quality_input_invalid")
    if time.time() >= deadline_at:
        raise TimeoutError("quality_deadline_exceeded")
    snapshots = _snapshot_contract(snapshot_inputs, manifest.get("duration_ms"))
    inspector_snapshots = tuple(MappingProxyType(dict(item)) for item in snapshots)
    allowed_evidence = {
        (item["frame_sha256"], item["timestamp_ms"]) for item in snapshots
    }
    verdict: Mapping[str, Any] | None = None
    try:
        _assert_snapshot_files_unchanged(snapshots)
        raw = visual_inspector.inspect(
            manifest=manifest, render_report=render_report,
            final_mux_sha256=final_mux.sha256, snapshots=inspector_snapshots,
            deadline_at=deadline_at,
        )
        _assert_snapshot_files_unchanged(snapshots)
        verdict = validate_quality_verdict(raw)
        if any(
            (evidence.get("frame_sha256"), evidence.get("timestamp_ms"))
            not in allowed_evidence
            for check in verdict.get("checks", ())
            for evidence in check.get("evidence", ())
        ):
            raise ValueError("quality_visual_evidence_unbound")
    except Exception:
        verdict = None
    visual_by_id = {
        item["check_id"]: item for item in verdict.get("checks", [])
    } if isinstance(verdict, Mapping) else {}
    deterministic = _technical_results(final_mux, manifest, render_report)
    deterministic["material_provenance"] = _provenance_ok(manifest, owner_evidence)
    findings = []
    for check_id, blocking in _BLOCKING.items():
        visual = visual_by_id.get(check_id)
        evidence = tuple(visual.get("evidence", ())) if isinstance(visual, Mapping) else ()
        if check_id in deterministic:
            passed, measured = deterministic[check_id]
            status = "pass" if passed else "fail"
            repairable = not passed and check_id in _REPAIRABLE
            executor = {"kind": "deterministic", "version": "ai-edit-v3-quality-1"}
        elif isinstance(visual, Mapping):
            status = visual.get("result") if visual.get("result") in {"pass", "fail", "unknown"} else "unknown"
            measured = {"confidence": visual.get("confidence", 0), "reason": visual.get("reason", "invalid")}
            repairable = status == "fail" and visual.get("repairable") is True and check_id in _REPAIRABLE
            executor = {"kind": "visual_model", "version": "quality-verdict-v1"}
        else:
            status, measured, repairable = "unknown", {"reason": "visual_inspector_unavailable"}, False
            executor = {"kind": "visual_model", "version": "unavailable"}
        findings.append(QualityFinding(check_id, status, blocking, repairable, measured, evidence, executor))
    frozen = tuple(findings)
    passed = all(item.status == "pass" for item in frozen if item.blocking)
    repairable_ids = tuple(item.check_id for item in frozen if item.status == "fail" and item.repairable)
    blocking_failures = tuple(
        item for item in frozen if item.blocking and item.status != "pass"
    )
    can_repair = bool(blocking_failures) and all(
        item.status == "fail" and item.repairable for item in blocking_failures
    )
    return QualityReport(
        passed,
        frozen,
        repairable_ids,
        can_repair,
        hashlib.sha256(_canonical_findings(frozen)).hexdigest(),
    )


__all__ = ("QualityFinding", "QualityReport", "VisualInspector", "run_blocking_quality")
