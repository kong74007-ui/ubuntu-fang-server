"""Fail-closed Python boundary for the root-owned HyperFrames sandbox."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
from typing import Any, Callable, Mapping

from . import RenderRequest, RenderResult


_BUILD_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INSTANCE_ID = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,63})\Z")
_MAX_CONTROL_OUTPUT = 64 * 1024
_CONTROL_ENVIRONMENT = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
_RENDERCTL = Path("/usr/local/libexec/huangque-ai-edit-v3-renderctl")


class HyperframesRendererError(RuntimeError):
    pass


class _SubprocessRunner:
    def run(self, argv, *, timeout_seconds, environment):
        started = time.monotonic()
        command = [str(value) for value in argv]
        if command and Path(command[0]) == _RENDERCTL:
            command = ["/usr/bin/sudo", "--non-interactive", *command]
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            env=dict(environment),
        )
        completed.elapsed_ms = int((time.monotonic() - started) * 1000)
        return completed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or ":" in value:
        raise HyperframesRendererError("render_manifest_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise HyperframesRendererError("render_manifest_path_invalid")
    return value


def _plain_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and not path.is_symlink()


def _declared_files(manifest: Mapping[str, Any]) -> dict[str, tuple[str, int | None]]:
    declarations: dict[str, tuple[str, int | None]] = {}
    candidates = []
    source = manifest.get("source_video")
    master = manifest.get("master_audio")
    if source is not None:
        candidates.append(source)
    if master is not None:
        candidates.append(master)
    assets = manifest.get("assets", [])
    if not isinstance(assets, list):
        raise HyperframesRendererError("render_manifest_assets_invalid")
    candidates.extend(assets)
    for declaration in candidates:
        if not isinstance(declaration, Mapping):
            raise HyperframesRendererError("render_manifest_file_invalid")
        relative = _relative(declaration.get("path"))
        digest = declaration.get("sha256")
        size = declaration.get("size_bytes")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise HyperframesRendererError("render_manifest_file_hash_invalid")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise HyperframesRendererError("render_manifest_file_size_invalid")
        existing = declarations.get(relative)
        if existing is not None and existing != (digest, size):
            raise HyperframesRendererError("render_manifest_file_conflict")
        declarations[relative] = (digest, size)
    if not declarations:
        raise HyperframesRendererError("render_manifest_files_missing")
    return declarations


class HyperframesRenderer:
    def __init__(
        self,
        *,
        renderctl_path: Path = _RENDERCTL,
        spool_root: Path = Path("/var/spool/huangque-ai-edit-v3"),
        renderer_build_id: str,
        registry_sha256: str,
        schema_sha256: str,
        command_runner=None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        helper = Path(renderctl_path)
        if helper != _RENDERCTL:
            raise ValueError("renderctl_path_invalid")
        if _BUILD_ID.fullmatch(renderer_build_id) is None or _BUILD_ID.fullmatch(registry_sha256) is None:
            raise ValueError("renderer_release_identity_invalid")
        if _SHA256.fullmatch(schema_sha256) is None:
            raise ValueError("renderer_schema_identity_invalid")
        self._renderctl_path = helper
        self._spool_root = Path(spool_root)
        self._renderer_build_id = renderer_build_id
        self._registry_sha256 = registry_sha256
        self._schema_sha256 = schema_sha256
        self._command_runner = command_runner or _SubprocessRunner()
        self._clock = clock
        self._sleeper = sleeper
        self._stopped_instances: set[str] = set()

    @property
    def renderer_build_id(self) -> str:
        return self._renderer_build_id

    @property
    def registry_sha256(self) -> str:
        return self._registry_sha256

    @property
    def schema_sha256(self) -> str:
        return self._schema_sha256

    def probe_capability(self, capability: str, *, environment: str | None):
        return {
            "available": capability == "renderer",
            "environment": environment,
            "renderer_build_id": self._renderer_build_id,
        }

    def _command(self, action: str, instance_id: str) -> dict[str, Any]:
        if action not in {"start", "query", "stop"} or _INSTANCE_ID.fullmatch(instance_id) is None:
            raise HyperframesRendererError("render_control_argument_invalid")
        try:
            result = self._command_runner.run(
                [self._renderctl_path, action, instance_id],
                timeout_seconds=30,
                environment=_CONTROL_ENVIRONMENT,
            )
        except Exception as exc:
            raise HyperframesRendererError("render_control_failed") from exc
        stdout = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else bytes(result.stdout)
        stderr = result.stderr.encode("utf-8") if isinstance(result.stderr, str) else bytes(result.stderr)
        if len(stdout) > _MAX_CONTROL_OUTPUT or len(stderr) > _MAX_CONTROL_OUTPUT or result.returncode != 0:
            raise HyperframesRendererError("render_control_failed")
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HyperframesRendererError("render_control_response_invalid") from exc
        if not isinstance(payload, dict) or payload.get("state") not in {"queued", "running", "succeeded", "failed", "stopped"}:
            raise HyperframesRendererError("render_control_response_invalid")
        return payload

    def _stop_quietly(self, instance_id: str) -> None:
        if instance_id in self._stopped_instances:
            return
        self._stopped_instances.add(instance_id)
        try:
            self._command("stop", instance_id)
        except HyperframesRendererError:
            pass

    def _poll(self, instance_id: str, deadline_at: float, assert_active: Callable[[], None]) -> dict[str, Any]:
        delay = 0.25
        while True:
            try:
                assert_active()
            except Exception as exc:
                self._stop_quietly(instance_id)
                raise HyperframesRendererError("render_lease_lost") from exc
            if self._clock() >= deadline_at:
                self._stop_quietly(instance_id)
                raise HyperframesRendererError("render_deadline_exceeded")
            payload = self._command("query", instance_id)
            if payload["state"] == "succeeded" and payload.get("result_ready") is True:
                return payload
            if payload["state"] in {"failed", "stopped"}:
                code = payload.get("error_code")
                raise HyperframesRendererError(code if isinstance(code, str) and code.startswith("render_") else "render_process_failed")
            self._sleeper(delay)
            delay = min(delay * 1.5, 2.0)

    def _stage(self, request: RenderRequest) -> Mapping[str, Any]:
        if request.renderer_build_id != self._renderer_build_id:
            raise HyperframesRendererError("renderer_build_id_mismatch")
        if not _plain_file(request.manifest_path) or _sha256(request.manifest_path) != request.manifest_sha256:
            raise HyperframesRendererError("render_manifest_hash_mismatch")
        try:
            manifest = json.loads(request.manifest_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HyperframesRendererError("render_manifest_json_invalid") from exc
        if not isinstance(manifest, Mapping):
            raise HyperframesRendererError("render_manifest_json_invalid")
        environment = manifest.get("renderer_environment")
        if not isinstance(environment, Mapping) or environment.get("renderer_build_id") != self._renderer_build_id:
            raise HyperframesRendererError("render_manifest_release_mismatch")
        declarations = _declared_files(manifest)
        incoming_root = self._spool_root / "incoming"
        incoming_root.mkdir(parents=True, exist_ok=True)
        final = incoming_root / request.instance_id
        staging = incoming_root / f".{request.instance_id}.{os.getpid()}.staging"
        if final.exists() or staging.exists():
            raise HyperframesRendererError("render_instance_exists")
        assets_root = staging / "assets"
        assets_root.mkdir(parents=True)
        try:
            for relative, (expected_hash, expected_size) in declarations.items():
                source = request.input_root.joinpath(*PurePosixPath(relative).parts)
                if not _plain_file(source):
                    raise HyperframesRendererError("render_input_file_invalid")
                if expected_size is not None and source.stat().st_size != expected_size:
                    raise HyperframesRendererError("render_input_size_mismatch")
                if _sha256(source) != expected_hash:
                    raise HyperframesRendererError("render_input_hash_mismatch")
                destination = assets_root.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination, follow_symlinks=False)
                if _sha256(destination) != expected_hash:
                    raise HyperframesRendererError("render_staged_hash_mismatch")
            shutil.copyfile(request.manifest_path, assets_root / "render-manifest.json", follow_symlinks=False)
            control = {
                "version": "1.0",
                "manifest_path": "render-manifest.json",
                "manifest_sha256": request.manifest_sha256,
                "renderer_build_id": self._renderer_build_id,
                "registry_sha256": self._registry_sha256,
                "schema_sha256": self._schema_sha256,
            }
            (staging / "request.json").write_text(
                json.dumps(control, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(staging, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    def _collect_result(self, request: RenderRequest, manifest: Mapping[str, Any]) -> RenderResult:
        source = self._spool_root / "results" / request.instance_id
        video = source / "silent.mp4"
        report_path = source / "silent.report.json"
        snapshots_root = source / "snapshots"
        if not (_plain_file(video) and _plain_file(report_path) and snapshots_root.is_dir()):
            raise HyperframesRendererError("render_result_missing")
        try:
            report_bytes = report_path.read_bytes()
            if len(report_bytes) > _MAX_CONTROL_OUTPUT:
                raise HyperframesRendererError("render_report_too_large")
            report = json.loads(report_bytes)
            output = report["output"]
            if report.get("status") != "done" or output.get("path") != "silent.mp4" or output.get("silent") is not True:
                raise HyperframesRendererError("render_report_invalid")
            digest = _sha256(video)
            if output.get("sha256") != digest or output.get("size_bytes") != video.stat().st_size:
                raise HyperframesRendererError("render_output_hash_mismatch")
            snapshot_names = []
            for item in report.get("snapshots", []):
                name = item.get("path")
                if not isinstance(name, str) or Path(name).name != name:
                    raise HyperframesRendererError("render_snapshot_invalid")
                path = snapshots_root / name
                if not _plain_file(path) or item.get("sha256") != _sha256(path) or item.get("size_bytes") != path.stat().st_size:
                    raise HyperframesRendererError("render_snapshot_hash_mismatch")
                snapshot_names.append(name)
            if not snapshot_names:
                raise HyperframesRendererError("render_snapshots_missing")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, HyperframesRendererError):
                raise
            raise HyperframesRendererError("render_report_invalid") from exc
        if any(request.output_root.iterdir()):
            raise HyperframesRendererError("render_output_root_not_empty")
        (request.output_root / "snapshots").mkdir(parents=True)
        shutil.copyfile(video, request.output_root / "silent.mp4")
        shutil.copyfile(report_path, request.output_root / "silent.report.json")
        for name in snapshot_names:
            shutil.copyfile(snapshots_root / name, request.output_root / "snapshots" / name)
        renderer_environment = manifest.get("renderer_environment", {})
        node_version = renderer_environment.get("node_version", "v22.0.0")
        if isinstance(node_version, str) and node_version.startswith("v"):
            node_version = node_version[1:]
        return RenderResult(
            silent_video_relpath="silent.mp4",
            sha256=digest,
            report_relpath="silent.report.json",
            snapshots=tuple(f"snapshots/{name}" for name in snapshot_names),
            environment={
                "renderer": "hyperframes",
                "renderer_build_id": self._renderer_build_id,
                "node_version": node_version,
            },
            performance=report.get("performance", {}),
        )

    def render(self, request: RenderRequest) -> RenderResult:
        manifest = self._stage(request)
        started = False
        try:
            self._command("start", request.instance_id)
            started = True
            self._poll(request.instance_id, request.deadline_at, lambda: None)
            return self._collect_result(request, manifest)
        except Exception:
            if started:
                self._stop_quietly(request.instance_id)
            raise


__all__ = ("HyperframesRenderer", "HyperframesRendererError")
