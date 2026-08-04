from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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


@dataclass(frozen=True)
class RendererRelease:
    root: Path
    report: RendererReleaseReport


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


def _release_tree_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for directory, names, files in os.walk(root / "src", followlinks=False):
        directory_path = Path(directory)
        for name in names:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise RendererReleaseError("renderer_release_symlink_forbidden")
        for name in files:
            if name.endswith(".mjs"):
                paths.append(directory_path / name)
    fonts_root = root / "assets" / "fonts"
    try:
        paths.extend(path for path in fonts_root.iterdir())
    except OSError as exc:
        raise RendererReleaseError("renderer_release_file_missing") from exc
    paths.extend(
        root / name
        for name in (
            "package.json",
            "package-lock.json",
            "hyperframes.json",
            "registry-sha256.txt",
        )
    )
    normalized: list[Path] = []
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RendererReleaseError("renderer_release_file_missing") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RendererReleaseError("renderer_release_file_invalid")
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise RendererReleaseError("renderer_release_path_invalid") from exc
        normalized.append(path)
    return tuple(sorted(normalized, key=lambda item: item.relative_to(root).as_posix()))


def _verify_release_tree(root: Path, lock: Mapping[str, Any]) -> None:
    declarations = lock.get("release_tree_files")
    expected_tree = lock.get("release_tree_sha256")
    if not isinstance(declarations, list) or not isinstance(expected_tree, str) or _SHA256.fullmatch(expected_tree) is None:
        raise RendererReleaseError("renderer_release_tree_invalid")
    actual_paths = _release_tree_paths(root)
    actual_names = tuple(path.relative_to(root).as_posix() for path in actual_paths)
    declared_names: list[str] = []
    digest = hashlib.sha256()
    for item in declarations:
        if not isinstance(item, Mapping) or set(item) != {"relative_path", "sha256", "size_bytes"}:
            raise RendererReleaseError("renderer_release_tree_invalid")
        relative_path = item.get("relative_path")
        expected_hash = item.get("sha256")
        expected_size = item.get("size_bytes")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
            or not isinstance(expected_hash, str)
            or _SHA256.fullmatch(expected_hash) is None
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise RendererReleaseError("renderer_release_tree_invalid")
        declared_names.append(relative_path)
        path = root.joinpath(*relative_path.split("/"))
        try:
            size = path.lstat().st_size
        except OSError as exc:
            raise RendererReleaseError("renderer_release_file_missing") from exc
        if size != expected_size or _sha256(path) != expected_hash:
            raise RendererReleaseError("renderer_release_tree_hash_mismatch")
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(expected_hash.encode("ascii"))
    if tuple(declared_names) != actual_names or len(set(declared_names)) != len(declared_names):
        raise RendererReleaseError("renderer_release_tree_incomplete")
    if digest.hexdigest() != expected_tree:
        raise RendererReleaseError("renderer_release_tree_hash_mismatch")


def verify_renderer_release(release_root: Path) -> RendererReleaseReport:
    root = release_root.resolve(strict=True)
    lock = _read_json(root / "renderer-release.lock.json")
    schema_version = lock.get("schema_version")
    if schema_version not in {1, 2}:
        raise RendererReleaseError("renderer_schema_version_invalid")
    build_id = lock.get("renderer_build_id")
    if not isinstance(build_id, str) or _BUILD_ID.fullmatch(build_id) is None:
        raise RendererReleaseError("renderer_build_id_invalid")
    if build_id != _build_id(lock):
        raise RendererReleaseError("renderer_build_id_mismatch")
    if schema_version == 2:
        _verify_release_tree(root, lock)
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


def resolve_renderer_release(build_id: str, releases_root: Path) -> RendererRelease:
    if not isinstance(build_id, str) or _BUILD_ID.fullmatch(build_id) is None:
        raise RendererReleaseError("renderer_build_id_invalid")
    try:
        root_parent = Path(releases_root).resolve(strict=True)
    except OSError as exc:
        raise RendererReleaseError("renderer_releases_root_invalid") from exc
    candidate = root_parent / build_id.removeprefix("sha256:")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise RendererReleaseError("renderer_release_unknown") from exc
    if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RendererReleaseError("renderer_release_path_invalid")
    try:
        root = candidate.resolve(strict=True)
        root.relative_to(root_parent)
    except (OSError, ValueError) as exc:
        raise RendererReleaseError("renderer_release_path_invalid") from exc
    report = verify_renderer_release(root)
    if report.renderer_build_id != build_id:
        raise RendererReleaseError("renderer_build_id_mismatch")
    return RendererRelease(root, report)


__all__ = (
    "RendererRelease",
    "RendererReleaseError",
    "RendererReleaseReport",
    "resolve_renderer_release",
    "verify_renderer_release",
)
