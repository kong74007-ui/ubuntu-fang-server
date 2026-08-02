"""Frozen renderer boundary reserved by AI Edit V3 Phase A."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol, runtime_checkable


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_EXACT_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}\Z")
_CHROMIUM_BUILD_ID = re.compile(
    r"chromium-(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}\Z"
)
_RENDERER_BUILD_ID = re.compile(
    r"(?:renderer-[0-9]{8}-[0-9a-f]{12}|sha256:[0-9a-f]{64})\Z"
)
_CODE_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RENDER_EVIDENCE_CONTRACTS: Mapping[
    str, frozenset[str] | re.Pattern[str]
] = MappingProxyType(
    {
        "architecture": frozenset({"aarch64", "amd64", "arm64", "x86_64"}),
        "chromium_build_id": _CHROMIUM_BUILD_ID,
        "chromium_version": _EXACT_VERSION,
        "code_commit_sha": _CODE_COMMIT_SHA,
        "component_registry_sha256": _SHA256,
        "ffmpeg_version": _EXACT_VERSION,
        "ffprobe_version": _EXACT_VERSION,
        "font_bundle_sha256": _SHA256,
        "gsap_version": _EXACT_VERSION,
        "hyperframes_version": _EXACT_VERSION,
        "locale": frozenset({"C", "C.UTF-8", "en_US.UTF-8", "zh_CN.UTF-8"}),
        "node_version": _EXACT_VERSION,
        "os_name": frozenset({"darwin", "linux", "windows"}),
        "os_version": _EXACT_VERSION,
        "package_lock_sha256": _SHA256,
        "render_bundle_sha256": _SHA256,
        "renderer": frozenset({"hyperframes"}),
        "renderer_build_id": _RENDERER_BUILD_ID,
        "timezone": frozenset({"UTC"}),
    }
)


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _valid_render_evidence(name: object, value: object) -> bool:
    if not isinstance(name, str) or not isinstance(value, str):
        return False
    contract = _RENDER_EVIDENCE_CONTRACTS.get(name)
    if contract is None:
        return False
    if isinstance(contract, frozenset):
        return value in contract
    return contract.fullmatch(value) is not None


def _identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or _has_control(value)
    ):
        raise ValueError(f"render_{field_name}_invalid")
    return value


def _absolute_local_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise ValueError(f"render_{field_name}_invalid")
    text = str(value).replace("\\", "/")
    if text.startswith("//") or not value.is_absolute():
        raise ValueError(f"render_{field_name}_invalid")
    try:
        return value.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"render_{field_name}_invalid") from exc


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"render_{field_name}_invalid")
    if "\\" in value or ":" in value or _WINDOWS_DRIVE.match(value) or _has_control(value):
        raise ValueError(f"render_{field_name}_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError(f"render_{field_name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class RenderRequest:
    instance_id: str
    job_id: str
    attempt: int
    manifest_path: Path
    input_root: Path
    output_root: Path
    manifest_sha256: str
    renderer_build_id: str
    deadline_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instance_id", _identifier(self.instance_id, "instance_id")
        )
        object.__setattr__(self, "job_id", _identifier(self.job_id, "job_id"))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("render_attempt_invalid")
        manifest = _absolute_local_path(self.manifest_path, "manifest_path")
        input_root = _absolute_local_path(self.input_root, "input_root")
        output_root = _absolute_local_path(self.output_root, "output_root")
        if not _contains(input_root, manifest) or manifest == input_root:
            raise ValueError("render_manifest_outside_input_root")
        if _contains(input_root, output_root) or _contains(output_root, input_root):
            raise ValueError("render_output_root_overlap")
        if not isinstance(self.manifest_sha256, str) or _SHA256.fullmatch(self.manifest_sha256) is None:
            raise ValueError("render_manifest_sha256_invalid")
        object.__setattr__(
            self,
            "renderer_build_id",
            _identifier(self.renderer_build_id, "renderer_build_id"),
        )
        if (
            isinstance(self.deadline_at, bool)
            or not isinstance(self.deadline_at, (int, float))
            or not math.isfinite(self.deadline_at)
            or self.deadline_at <= 0
        ):
            raise ValueError("render_deadline_invalid")
        object.__setattr__(self, "manifest_path", manifest)
        object.__setattr__(self, "input_root", input_root)
        object.__setattr__(self, "output_root", output_root)


@dataclass(frozen=True, slots=True)
class RenderResult:
    silent_video_relpath: str
    sha256: str
    report_relpath: str
    snapshots: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
    performance: Mapping[str, int | float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "silent_video_relpath",
            _relative_path(self.silent_video_relpath, "silent_video_relpath"),
        )
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("render_sha256_invalid")
        object.__setattr__(
            self,
            "report_relpath",
            _relative_path(self.report_relpath, "report_relpath"),
        )
        if not isinstance(self.snapshots, (tuple, list)) or not self.snapshots:
            raise ValueError("render_snapshots_invalid")
        snapshots = tuple(
            _relative_path(value, "snapshot_relpath") for value in self.snapshots
        )
        if not isinstance(self.environment, Mapping):
            raise ValueError("render_environment_invalid")
        environment: dict[str, str] = {}
        for name, value in self.environment.items():
            if not _valid_render_evidence(name, value):
                raise ValueError("render_environment_invalid")
            environment[name] = value
        if not isinstance(self.performance, Mapping):
            raise ValueError("render_performance_invalid")
        performance: dict[str, int | float] = {}
        for name, value in self.performance.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("render_performance_invalid")
            performance[name] = value
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "environment", MappingProxyType(environment))
        object.__setattr__(self, "performance", MappingProxyType(performance))


@runtime_checkable
class Renderer(Protocol):
    def render(self, request: RenderRequest) -> RenderResult: ...


__all__ = ("RenderRequest", "RenderResult", "Renderer")
