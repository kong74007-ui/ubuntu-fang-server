"""Frozen renderer boundary reserved by AI Edit V3 Phase A."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol, runtime_checkable


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
_RENDER_EVIDENCE_KEYS = frozenset(
    {
        "architecture",
        "chromium_build_id",
        "ffmpeg_version",
        "ffprobe_version",
        "font_bundle_sha256",
        "gsap_version",
        "hyperframes_version",
        "node",
        "node_version",
        "os_name",
        "os_version",
        "renderer",
        "renderer_build_id",
    }
)
_EVIDENCE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+()\-]{0,127}\Z")
_SECRET_VALUE_PREFIXES = (
    "AKID",
    "ASIA",
    "AUTH",
    "BASIC",
    "BEARER",
    "COOKIE",
    "HMAC",
    "PASSWORD",
    "SECRET",
    "SK-",
    "TOKEN",
)


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


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
    environment: Mapping[str, str]
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
            if (
                name not in _RENDER_EVIDENCE_KEYS
                or not isinstance(value, str)
                or not value
                or value != value.strip()
                or _has_control(value)
                or _EVIDENCE_VALUE.fullmatch(value) is None
                or value.upper().startswith(_SECRET_VALUE_PREFIXES)
            ):
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
