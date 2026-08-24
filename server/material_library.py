#!/usr/bin/env python3
"""Read-only selector for the Huangque approved material library."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ALLOWED_EXTENSIONS = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mp3": "bgm",
    ".wav": "bgm",
    ".m4a": "bgm",
}
SEARCH_FIELDS = {
    "标签": 6,
    "素材名称": 5,
    "一级场景": 4,
    "二级场景": 4,
    "使用环节": 3,
    "情绪氛围": 3,
    "主体": 2,
    "行业": 2,
    "画面方向": 2,
}
SEARCH_FIELD_ALIASES = {
    "主体": ("主体", "画面主体"),
}
SHA256_FIELDS = ("sha256", "SHA256")
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 20_000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


class MaterialLibraryError(RuntimeError):
    pass


class MaterialShortageError(MaterialLibraryError):
    pass


@dataclass(frozen=True)
class Material:
    record_id: str
    sha256: str
    name: str
    media_type: str
    relative_path: str
    orientation: str
    duration_seconds: float | None
    searchable: tuple[tuple[str, int], ...]

    def public_dict(self, match_level: str, scene_id: str) -> dict[str, Any]:
        return {
            "scene_id": scene_id,
            "record_id": self.record_id,
            "sha256": self.sha256,
            "name": self.name,
            "media_type": self.media_type,
            "orientation": self.orientation,
            "duration_seconds": self.duration_seconds,
            "match_level": match_level,
        }


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return " ".join(str(value or "").lower().split())


def _tokens(*values: Any) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for token in TOKEN_RE.findall(_text(value)):
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            result.append(token)
    return tuple(result)


def _search_values(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(text for item in values if (text := _text(item)))


def _search_values_for_row(row: dict[str, Any], field: str) -> tuple[str, ...]:
    aliases = SEARCH_FIELD_ALIASES.get(field, (field,))
    values: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        for text in _search_values(row.get(alias)):
            if text not in seen:
                seen.add(text)
                values.append(text)
    return tuple(values)


def _material_sha256(row: dict[str, Any]) -> str:
    values = {
        value
        for field in SHA256_FIELDS
        if (value := _text(row.get(field)))
    }
    if len(values) > 1:
        raise MaterialLibraryError("conflicting material sha256 aliases")
    return next(iter(values), "")


def _orientation(value: Any) -> str:
    text = _text(value)
    if any(word in text for word in ("竖", "portrait", "9:16")):
        return "portrait"
    if any(word in text for word in ("横", "landscape", "16:9")):
        return "landscape"
    if any(word in text for word in ("方", "square", "1:1")):
        return "square"
    return "unknown"


def _safe_relative_path(root: Path, value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        raise MaterialLibraryError("missing material path")
    candidate = (root / raw).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise MaterialLibraryError("material path escapes library root") from exc
    return candidate.relative_to(resolved_root).as_posix()


def _duration(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _record_to_material(root: Path, row: dict[str, Any]) -> Material | None:
    if _text(row.get("状态")) != "可使用":
        return None
    relative_path = _safe_relative_path(root, row.get("server_relative_path"))
    suffix = Path(relative_path).suffix.lower()
    media_type = ALLOWED_EXTENSIONS.get(suffix)
    sha256 = _material_sha256(row)
    if not media_type or not SHA256_RE.fullmatch(sha256):
        return None
    file_path = (root / relative_path).resolve()
    if not file_path.is_file():
        return None
    searchable = tuple(
        (text, weight)
        for field, weight in SEARCH_FIELDS.items()
        for text in _search_values_for_row(row, field)
    )
    return Material(
        record_id=str(row.get("record_id") or sha256[:16]),
        sha256=sha256,
        name=str(row.get("素材名称") or file_path.name),
        media_type=media_type,
        relative_path=relative_path,
        orientation=_orientation(row.get("画面方向")),
        duration_seconds=_duration(row.get("时长秒")),
        searchable=searchable,
    )


def _score(material: Material, tokens: Iterable[str], query_text: str) -> int:
    total = 0
    for token in tokens:
        total += sum(weight for text, weight in material.searchable if token in text)
    for text, weight in material.searchable:
        if len(text) >= 2 and text in query_text and not any(token == text for token in tokens):
            total += weight
    return total


def _stable_rank(seed: str, material: Material) -> str:
    return hashlib.sha256(f"{seed}:{material.sha256}".encode("utf-8")).hexdigest()


class MaterialLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.index_path = self.root / "index.jsonl"
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._materials: tuple[Material, ...] = ()
        self._by_sha: dict[str, Material] = {}

    def _reload_if_needed(self) -> None:
        stat = self.index_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        if stat.st_size > MAX_INDEX_BYTES:
            raise MaterialLibraryError("material index is too large")
        materials: list[Material] = []
        seen_sha: set[str] = set()
        with self.index_path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number > MAX_RECORDS:
                    raise MaterialLibraryError("material index has too many records")
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    material = _record_to_material(self.root, row)
                except (json.JSONDecodeError, MaterialLibraryError) as exc:
                    raise MaterialLibraryError(f"invalid material index row {line_number}: {exc}") from exc
                if material and material.sha256 not in seen_sha:
                    seen_sha.add(material.sha256)
                    materials.append(material)
        self._materials = tuple(materials)
        self._by_sha = {item.sha256: item for item in materials}
        self._stamp = stamp

    def refresh(self) -> None:
        with self._lock:
            self._reload_if_needed()

    def stats(self) -> dict[str, Any]:
        self.refresh()
        counts = {kind: 0 for kind in ("image", "video", "bgm")}
        for material in self._materials:
            counts[material.media_type] += 1
        return {"records": len(self._materials), "media_types": counts}

    def resolve(self, sha256: str) -> tuple[Material, Path]:
        self.refresh()
        material = self._by_sha.get(sha256.lower())
        if not material:
            raise KeyError(sha256)
        path = (self.root / material.relative_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise MaterialLibraryError("material path escapes library root") from exc
        if not path.is_file():
            raise MaterialLibraryError("material file is unavailable")
        return material, path

    def select(
        self,
        scenes: list[dict[str, Any]],
        *,
        orientation: str = "portrait",
        seed: str = "",
        used_sha256: Iterable[str] = (),
    ) -> dict[str, Any]:
        self.refresh()
        if not isinstance(scenes, list) or not scenes:
            raise ValueError("scenes must not be empty")
        if len(scenes) > 21:
            raise ValueError("scenes must not exceed 21 items")
        if any(not isinstance(scene, dict) for scene in scenes):
            raise ValueError("each scene must be an object")
        requested_orientation = _orientation(orientation)
        used = {str(value).lower() for value in used_sha256 if SHA256_RE.fullmatch(str(value).lower())}
        selected: list[dict[str, Any]] = []

        for position, scene in enumerate(scenes):
            scene_id = str(scene.get("scene_id") or f"scene_{position + 1:02d}")
            media_type = _text(scene.get("media_type") or "visual")
            allowed_types = {"image", "video"} if media_type == "visual" else {media_type}
            if not allowed_types <= {"image", "video", "bgm"}:
                raise ValueError(f"unsupported media_type for {scene_id}")
            candidates = [
                item
                for item in self._materials
                if item.sha256 not in used
                and item.media_type in allowed_types
                and (
                    requested_orientation == "unknown"
                    or item.orientation in {requested_orientation, "unknown"}
                    or item.media_type == "bgm"
                )
            ]
            if not candidates:
                raise MaterialShortageError(f"no unique approved material remains for {scene_id}")

            query_values = (
                scene.get("query"),
                scene.get("keywords"),
                scene.get("purpose"),
                scene.get("mood"),
            )
            tokens = _tokens(*query_values)
            query_text = " ".join(_text(value) for value in query_values)
            scored = [(item, _score(item, tokens, query_text)) for item in candidates]
            exact = [(item, score) for item, score in scored if score >= 8]
            loose = [(item, score) for item, score in scored if score > 0]
            if exact:
                pool, match_level = exact, "exact"
            elif loose:
                pool, match_level = loose, "loose"
            else:
                pool, match_level = [(item, 0) for item in candidates], "random"
            rank_seed = f"{seed}:{scene_id}:{position}"
            material, score = sorted(
                pool,
                key=lambda pair: (-pair[1], _stable_rank(rank_seed, pair[0])),
            )[0]
            used.add(material.sha256)
            item = material.public_dict(match_level, scene_id)
            item["match_score"] = score
            selected.append(item)

        return {
            "materials": selected,
            "used_sha256": sorted(used),
            "fallback_policy": ["exact", "loose", "random"],
            "ai_fallback": False,
        }


def content_type_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
