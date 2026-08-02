from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BUILD_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RendererReleaseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RendererReleaseReport:
    renderer_build_id: str
    node_major: int
    hyperframes_version: str
    gsap_version: str
    font_count: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RendererReleaseError("renderer_release_file_missing") from exc
    return digest.hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RendererReleaseError("renderer_release_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise RendererReleaseError("renderer_release_json_invalid")
    return value


def _build_id(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("renderer_build_id", None)
    payload.pop("release_archive_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def verify_renderer_release(release_root: Path) -> RendererReleaseReport:
    root = release_root.resolve(strict=True)
    lock = _read_json(root / "renderer-release.lock.json")
    if lock.get("schema_version") != 1:
        raise RendererReleaseError("renderer_schema_version_invalid")
    build_id = lock.get("renderer_build_id")
    if not isinstance(build_id, str) or _BUILD_ID.fullmatch(build_id) is None:
        raise RendererReleaseError("renderer_build_id_invalid")
    if build_id != _build_id(lock):
        raise RendererReleaseError("renderer_build_id_mismatch")
    if _sha256(root / "package-lock.json") != lock.get("package_lock_sha256"):
        raise RendererReleaseError("renderer_package_lock_hash_mismatch")
    package = _read_json(root / "package.json")
    dependencies = package.get("dependencies")
    if dependencies != {"gsap": "3.15.0", "hyperframes": "0.7.84"}:
        raise RendererReleaseError("renderer_dependency_version_invalid")
    if package.get("engines") != {"node": ">=22 <23"}:
        raise RendererReleaseError("renderer_node_engine_invalid")
    if lock.get("hyperframes_version") != "0.7.84" or lock.get("gsap_version") != "3.15.0":
        raise RendererReleaseError("renderer_dependency_version_invalid")
    node_version = lock.get("node", {}).get("version") if isinstance(lock.get("node"), Mapping) else None
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+", node_version or "")
    if match is None or int(match.group(1)) != 22:
        raise RendererReleaseError("renderer_node_version_invalid")
    fonts = lock.get("fonts")
    if not isinstance(fonts, list) or not fonts:
        raise RendererReleaseError("renderer_fonts_invalid")
    relative_paths = [item.get("relative_path") for item in fonts if isinstance(item, Mapping)]
    if len(relative_paths) != len(fonts) or relative_paths != sorted(relative_paths):
        raise RendererReleaseError("renderer_fonts_invalid")
    for item in fonts:
        relative_path = item["relative_path"]
        expected = item.get("sha256")
        if (
            not isinstance(relative_path, str)
            or relative_path.startswith(("/", "\\"))
            or ".." in Path(relative_path).parts
            or not isinstance(expected, str)
            or _SHA256.fullmatch(expected) is None
        ):
            raise RendererReleaseError("renderer_fonts_invalid")
        if _sha256(root / relative_path) != expected:
            raise RendererReleaseError("renderer_font_hash_mismatch")
    return RendererReleaseReport(build_id, 22, "0.7.84", "3.15.0", len(fonts))
