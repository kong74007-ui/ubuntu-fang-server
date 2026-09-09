#!/usr/bin/env python3
"""Read-only selector for the Huangque approved material library."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
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
SELECTION_CONTRACT_VERSION = 2
CLIP_CONTRACT_VERSION = 1
MAX_MATERIAL_DURATION_SECONDS = 30 * 60
MAX_CLIP_SLOTS_PER_SOURCE = 600
MAX_VIRTUAL_CANDIDATES_PER_REQUEST = 20_000
MAX_SELECTION_SCENES = 21
# Keep the flat usage file inside the exact limits accepted by the previous
# production reader so a source rollback can reuse it without conversion.
MAX_USAGE_RECORDS = MAX_RECORDS
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_SELECTION_RECEIPTS = 2_000
SELECTION_RECEIPT_TTL_SECONDS = 4 * 24 * 60 * 60
SELECTION_RECEIPT_VERSION = 1
MAX_SELECTION_RECEIPT_BYTES = 256 * 1024
MAX_SELECTION_RECEIPT_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_SELECTION_RECEIPT_FILES = MAX_SELECTION_RECEIPTS * 2
MAX_SELECTION_RECEIPT_USAGE_RECORDS = MAX_SELECTION_SCENES * 2
# Recency penalties outrank the clamped count gap, so rotation wins without
# abandoning long-term per-file fairness.
ROUND_ROBIN_COUNT_SLACK = 2
ROUND_ROBIN_RECENT_GROUP_WINDOW = 9
ROUND_ROBIN_RECENT_SOURCE_WINDOW = 50
ROUND_ROBIN_RECENT_CLIP_WINDOW = 200
ROUND_ROBIN_RECENT_CLIP_PENALTY = ROUND_ROBIN_COUNT_SLACK + 1
ROUND_ROBIN_RECENT_SOURCE_PENALTY = (
    ROUND_ROBIN_COUNT_SLACK + ROUND_ROBIN_RECENT_CLIP_PENALTY + 1
)
ROUND_ROBIN_RECENT_GROUP_PENALTY = (
    ROUND_ROBIN_COUNT_SLACK + ROUND_ROBIN_RECENT_SOURCE_PENALTY
    + ROUND_ROBIN_RECENT_CLIP_PENALTY + 1
)
MIN_CLIP_SECONDS = 2.0
MAX_CLIP_SECONDS = 3.0
CLIP_SLOT_SECONDS = 3.0
CLIP_SAFETY_SECONDS = 0.1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SELECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


class MaterialLibraryError(RuntimeError):
    pass


class MaterialShortageError(MaterialLibraryError):
    pass


class MaterialSelectionConflictError(MaterialLibraryError):
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
    diversity_group: str
    searchable: tuple[tuple[str, int], ...]

    def public_dict(
        self, match_level: str, scene_id: str, orientation_match: str
    ) -> dict[str, Any]:
        return {
            "scene_id": scene_id,
            "record_id": self.record_id,
            "sha256": self.sha256,
            "name": self.name,
            "media_type": self.media_type,
            "orientation": self.orientation,
            "duration_seconds": self.duration_seconds,
            "match_level": match_level,
            "orientation_match": orientation_match,
        }


@dataclass(frozen=True)
class MaterialCandidate:
    material: Material
    usage_key: str
    clip_start_seconds: float | None = None
    clip_duration_seconds: float | None = None
    clip_slot_index: int | None = None
    clip_slot_count: int | None = None

    def public_dict(
        self, match_level: str, scene_id: str, orientation_match: str,
    ) -> dict[str, Any]:
        result = self.material.public_dict(
            match_level, scene_id, orientation_match,
        )
        if self.clip_duration_seconds is not None:
            result.update({
                "clip_id": self.usage_key,
                "clip_start_seconds": self.clip_start_seconds,
                "clip_duration_seconds": self.clip_duration_seconds,
                "clip_slot_index": self.clip_slot_index,
                "clip_slot_count": self.clip_slot_count,
            })
        return result


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
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MaterialLibraryError("invalid material duration")
    try:
        parsed = float(value)
        if (
            not math.isfinite(parsed)
            or parsed < 0
            or parsed > MAX_MATERIAL_DURATION_SECONDS
        ):
            raise MaterialLibraryError("invalid material duration")
        return parsed
    except (TypeError, ValueError, OverflowError):
        raise MaterialLibraryError("invalid material duration")


def _clip_duration(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("clip_duration_seconds must be between 2 and 3")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "clip_duration_seconds must be between 2 and 3"
        ) from exc
    if (
        not math.isfinite(parsed)
        or not MIN_CLIP_SECONDS <= parsed <= MAX_CLIP_SECONDS
    ):
        raise ValueError("clip_duration_seconds must be between 2 and 3")
    return round(parsed, 6)


def _material_candidates(
    material: Material, clip_duration: float | None,
) -> tuple[MaterialCandidate, ...]:
    if material.media_type != "video" or clip_duration is None:
        return (MaterialCandidate(material, material.sha256),)
    available = float(material.duration_seconds or 0) - CLIP_SAFETY_SECONDS
    if available + 0.001 < clip_duration:
        return ()
    slot_count = max(1, int(math.floor(
        available / CLIP_SLOT_SECONDS + 1e-9
    )))
    if slot_count > MAX_CLIP_SLOTS_PER_SOURCE:
        raise MaterialLibraryError("material clip slot limit exceeded")
    occupied = CLIP_SLOT_SECONDS * slot_count if slot_count > 1 else clip_duration
    leading = max(0.0, (available - occupied) / 2)
    inset = (
        (CLIP_SLOT_SECONDS - clip_duration) / 2
        if slot_count > 1 else 0.0
    )
    result = []
    for slot_index in range(slot_count):
        clip_id = hashlib.sha256(
            f"{material.sha256}:clip-slot:{slot_index}".encode("ascii")
        ).hexdigest()
        result.append(MaterialCandidate(
            material=material,
            usage_key=clip_id,
            clip_start_seconds=round(
                leading + inset + slot_index * CLIP_SLOT_SECONDS, 3,
            ),
            clip_duration_seconds=round(clip_duration, 3),
            clip_slot_index=slot_index + 1,
            clip_slot_count=slot_count,
        ))
    return tuple(result)


def _diversity_group(row: dict[str, Any], media_type: str, sha256: str) -> str:
    if media_type not in {"image", "video"}:
        return f"{media_type}:asset:{sha256}"
    collection = _text(row.get("导入批次")) or _text(row.get("来源平台"))
    scene = _text(row.get("二级场景")) or _text(row.get("一级场景"))
    if not collection and not scene:
        return f"{media_type}:asset:{sha256}"
    return f"{media_type}:{collection or 'unknown'}:{scene or 'unknown'}"


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
        diversity_group=_diversity_group(row, media_type, sha256),
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


def _stable_rank(seed: str, candidate: MaterialCandidate) -> str:
    return hashlib.sha256(
        f"{seed}:{candidate.usage_key}".encode("utf-8")
    ).hexdigest()


def _selection_request_sha256(
    scenes: list[dict[str, Any]], orientation: str, seed: str,
    selection_mode: str,
) -> str:
    try:
        canonical = json.dumps({
            "scenes": scenes,
            "orientation": orientation,
            "seed": seed,
            "selection_mode": selection_mode,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("selection request is not JSON serializable") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MaterialLibrary:
    def __init__(self, root: str | Path, *, usage_path: str | Path | None = None):
        self.root = Path(root).resolve()
        self.index_path = self.root / "index.jsonl"
        self._lock = threading.RLock()
        self._usage_lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._materials: tuple[Material, ...] = ()
        self._by_sha: dict[str, Material] = {}
        self._verification_cache: dict[
            str, tuple[tuple[int, int, int, int], bool]
        ] = {}
        self._verification_locks: dict[str, threading.Lock] = {}
        self._usage_path = (
            Path(usage_path).resolve() if usage_path is not None else None
        )
        self._receipt_dir = (
            self._usage_path.with_name(
                f"{self._usage_path.name}.receipts-v1"
            )
            if self._usage_path is not None else None
        )
        self._usage: dict[str, dict[str, int | float]] = {}
        self._selection_receipts: dict[str, dict[str, Any]] = {}
        self._selection_receipt_sizes: dict[str, int] = {}
        self._selection_receipt_journal_bytes = 0
        self._usage_state_ready = False
        self._load_usage()
        self._load_receipts()

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
        self._verification_cache.clear()
        self._stamp = stamp

    def _load_usage(self) -> None:
        if self._usage_path is None:
            return
        try:
            if not self._usage_path.exists():
                return
            if self._usage_path.is_symlink() or not self._usage_path.is_file():
                raise MaterialLibraryError("material usage state is unsafe")
            if self._usage_path.stat().st_size > MAX_STATE_BYTES:
                raise MaterialLibraryError("material usage state is too large")
            data = json.loads(self._usage_path.read_text(encoding="utf-8"))
            self._usage = self._validated_usage_records(
                data, MAX_USAGE_RECORDS, "material usage state is invalid",
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MaterialLibraryError("material usage state is unavailable") from exc

    @staticmethod
    def _validated_usage_records(
        data: Any, maximum: int, error_message: str,
    ) -> dict[str, dict[str, int | float]]:
        if not isinstance(data, dict) or len(data) > maximum:
            raise MaterialLibraryError(error_message)
        loaded: dict[str, dict[str, int | float]] = {}
        for key, value in data.items():
            if (
                not SHA256_RE.fullmatch(str(key))
                or not isinstance(value, dict)
                or set(value) != {"count", "last_used"}
                or isinstance(value.get("count"), bool)
                or not isinstance(value.get("count"), int)
                or value["count"] < 0
                or isinstance(value.get("last_used"), bool)
                or not isinstance(value.get("last_used"), (int, float))
                or not math.isfinite(float(value["last_used"]))
                or float(value["last_used"]) < 0
            ):
                raise MaterialLibraryError(error_message)
            loaded[str(key)] = {
                "count": int(value["count"]),
                "last_used": float(value["last_used"]),
            }
        return loaded

    def _receipt_path(self, selection_id: str) -> Path:
        if self._receipt_dir is None:
            raise MaterialLibraryError(
                "selection receipt state is not configured"
            )
        digest = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
        return self._receipt_dir / f"{digest}.json"

    def _validated_receipt(
        self, data: Any, *, expected_digest: str | None = None,
    ) -> dict[str, Any]:
        error = "selection receipt state is invalid"
        if (
            not isinstance(data, dict)
            or set(data) != {
                "version", "selection_id", "request_sha256", "result",
                "created_at", "usage_after",
            }
            or isinstance(data.get("version"), bool)
            or data.get("version") != SELECTION_RECEIPT_VERSION
        ):
            raise MaterialLibraryError(error)
        selection_id = str(data.get("selection_id") or "")
        if (
            not SELECTION_ID_RE.fullmatch(selection_id)
            or not SHA256_RE.fullmatch(
                str(data.get("request_sha256") or "")
            )
            or not isinstance(data.get("result"), dict)
            or isinstance(data.get("created_at"), bool)
            or not isinstance(data.get("created_at"), (int, float))
            or not math.isfinite(float(data["created_at"]))
            or float(data["created_at"]) < 0
        ):
            raise MaterialLibraryError(error)
        digest = hashlib.sha256(selection_id.encode("utf-8")).hexdigest()
        if expected_digest is not None and not hmac.compare_digest(
            digest, expected_digest,
        ):
            raise MaterialLibraryError(error)
        usage_after = self._validated_usage_records(
            data.get("usage_after"),
            MAX_SELECTION_RECEIPT_USAGE_RECORDS,
            error,
        )
        if not usage_after:
            raise MaterialLibraryError(error)
        return {
            "version": SELECTION_RECEIPT_VERSION,
            "selection_id": selection_id,
            "request_sha256": str(data["request_sha256"]),
            "result": json.loads(json.dumps(data["result"])),
            "created_at": float(data["created_at"]),
            "usage_after": usage_after,
        }

    def _load_receipts(self) -> None:
        if self._receipt_dir is None or not self._receipt_dir.exists():
            return
        try:
            if self._receipt_dir.is_symlink() or not self._receipt_dir.is_dir():
                raise MaterialLibraryError("selection receipt state is unsafe")
            paths = []
            for path in self._receipt_dir.iterdir():
                if path.is_symlink():
                    raise MaterialLibraryError(
                        "selection receipt state is unsafe"
                    )
                if path.name.startswith(".receipt-") and path.suffix == ".tmp":
                    path.unlink(missing_ok=True)
                    continue
                if path.suffix != ".json":
                    continue
                if not path.is_file() or not SHA256_RE.fullmatch(path.stem):
                    raise MaterialLibraryError(
                        "selection receipt state is invalid"
                    )
                paths.append(path)
            if len(paths) > MAX_SELECTION_RECEIPT_FILES:
                raise MaterialLibraryError(
                    "selection receipt state is too large"
                )
            receipts: dict[str, dict[str, Any]] = {}
            sizes: dict[str, int] = {}
            total_bytes = 0
            for path in sorted(paths):
                size = path.stat().st_size
                total_bytes += size
                if (
                    size > MAX_SELECTION_RECEIPT_BYTES
                    or total_bytes > MAX_SELECTION_RECEIPT_JOURNAL_BYTES
                ):
                    raise MaterialLibraryError(
                        "selection receipt state is too large"
                    )
                receipt = self._validated_receipt(
                    json.loads(path.read_text(encoding="utf-8")),
                    expected_digest=path.stem,
                )
                selection_id = receipt["selection_id"]
                if selection_id in receipts:
                    raise MaterialLibraryError(
                        "selection receipt state is invalid"
                    )
                receipts[selection_id] = receipt
                sizes[selection_id] = size
            self._selection_receipts = receipts
            self._selection_receipt_sizes = sizes
            self._selection_receipt_journal_bytes = total_bytes
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MaterialLibraryError(
                "selection receipt state is unavailable"
            ) from exc

    def _valid_usage_keys(self) -> set[str]:
        valid = {material.sha256 for material in self._materials}
        virtual_count = 0
        for material in self._materials:
            if material.media_type != "video":
                continue
            for candidate in _material_candidates(
                material, MIN_CLIP_SECONDS,
            ):
                virtual_count += 1
                if virtual_count > MAX_VIRTUAL_CANDIDATES_PER_REQUEST:
                    raise MaterialLibraryError(
                        "virtual candidate limit exceeded"
                    )
                valid.add(candidate.usage_key)
        return valid

    def _prepared_usage_state(
        self,
    ) -> tuple[dict[str, dict[str, int | float]], str]:
        valid_usage = self._valid_usage_keys()
        retained = {
            key: dict(value) for key, value in self._usage.items()
            if key in valid_usage
        }
        if len(retained) > MAX_USAGE_RECORDS:
            raise MaterialLibraryError("material usage state is too large")
        try:
            serialized = json.dumps(
                retained, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise MaterialLibraryError(
                "material usage state is unavailable"
            ) from exc
        if len(serialized.encode("utf-8")) > MAX_STATE_BYTES:
            raise MaterialLibraryError("material usage state is too large")
        return retained, serialized

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = None
        try:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(descriptor)
        except OSError:
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _ensure_receipt_directory(self) -> None:
        if self._receipt_dir is None or self._usage_path is None:
            raise MaterialLibraryError(
                "selection receipt state is not configured"
            )
        parent = self._usage_path.parent
        try:
            if parent.is_symlink() or not parent.is_dir():
                raise OSError("usage state directory is unavailable")
            self._receipt_dir.mkdir(mode=0o700, exist_ok=True)
            if self._receipt_dir.is_symlink() or not self._receipt_dir.is_dir():
                raise OSError("selection receipt directory is unavailable")
            os.chmod(self._receipt_dir, 0o700)
        except OSError as exc:
            raise MaterialLibraryError(
                "selection receipt state is unavailable"
            ) from exc

    def _serialized_receipt(self, receipt: dict[str, Any]) -> str:
        validated = self._validated_receipt(receipt)
        try:
            serialized = json.dumps(
                validated, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise MaterialLibraryError(
                "selection receipt state is unavailable"
            ) from exc
        if len(serialized.encode("utf-8")) > MAX_SELECTION_RECEIPT_BYTES:
            raise MaterialLibraryError("selection receipt state is too large")
        return serialized

    def _write_receipt(self, receipt: dict[str, Any]) -> None:
        validated = self._validated_receipt(receipt)
        selection_id = validated["selection_id"]
        serialized = self._serialized_receipt(validated)
        size = len(serialized.encode("utf-8"))
        previous_size = self._selection_receipt_sizes.get(selection_id, 0)
        if (
            self._selection_receipt_journal_bytes - previous_size + size
            > MAX_SELECTION_RECEIPT_JOURNAL_BYTES
        ):
            raise MaterialLibraryError("selection receipt state is too large")
        self._ensure_receipt_directory()
        target = self._receipt_path(selection_id)
        temporary = None
        try:
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise OSError("selection receipt path is unsafe")
                existing = self._validated_receipt(
                    json.loads(target.read_text(encoding="utf-8")),
                    expected_digest=target.stem,
                )
                if existing != validated:
                    raise MaterialSelectionConflictError(
                        "selection receipt state conflict"
                    )
                return
            descriptor, name = tempfile.mkstemp(
                prefix=".receipt-", suffix=".tmp", dir=self._receipt_dir,
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            temporary = None
            self._fsync_directory(self._receipt_dir)
        except MaterialSelectionConflictError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MaterialLibraryError(
                "selection receipt state is unavailable"
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._selection_receipt_journal_bytes += size - previous_size
        self._selection_receipt_sizes[selection_id] = size

    def _prune_receipts(self, *, reserve_slots: int = 0) -> None:
        cutoff = time.time() - SELECTION_RECEIPT_TTL_SECONDS
        retained = [
            (key, value)
            for key, value in self._selection_receipts.items()
            if float(value["created_at"]) >= cutoff
        ]
        retained.sort(
            key=lambda item: (-float(item[1]["created_at"]), item[0])
        )
        maximum = max(0, MAX_SELECTION_RECEIPTS - reserve_slots)
        next_receipts = dict(retained[:maximum])
        removed = set(self._selection_receipts) - set(next_receipts)
        if removed and self._receipt_dir is not None:
            try:
                for selection_id in removed:
                    path = self._receipt_path(selection_id)
                    if path.is_symlink():
                        raise OSError("selection receipt path is unsafe")
                    path.unlink(missing_ok=True)
                if self._receipt_dir.exists():
                    self._fsync_directory(self._receipt_dir)
            except OSError as exc:
                raise MaterialLibraryError(
                    "selection receipt state is unavailable"
                ) from exc
        for selection_id in removed:
            self._selection_receipt_journal_bytes -= (
                self._selection_receipt_sizes.pop(selection_id, 0)
            )
        self._selection_receipt_journal_bytes = max(
            0, self._selection_receipt_journal_bytes,
        )
        self._selection_receipts = next_receipts

    def _apply_receipt_usage(self) -> bool:
        changed = False
        # Absolute post-selection values make reconciliation idempotent while
        # preserving any larger counters written by a rolled-back old service.
        for receipt in sorted(
            self._selection_receipts.values(),
            key=lambda value: (
                float(value["created_at"]), value["selection_id"],
            ),
        ):
            for key, expected in receipt["usage_after"].items():
                current = self._usage.get(
                    key, {"count": 0, "last_used": 0.0},
                )
                merged = {
                    "count": max(
                        int(current.get("count", 0) or 0),
                        int(expected["count"]),
                    ),
                    "last_used": max(
                        float(current.get("last_used", 0.0) or 0.0),
                        float(expected["last_used"]),
                    ),
                }
                if self._usage.get(key) != merged:
                    self._usage[key] = merged
                    changed = True
        return changed

    def _save_usage(
        self,
        prepared: tuple[dict[str, dict[str, int | float]], str] | None = None,
    ) -> None:
        if self._usage_path is None:
            raise MaterialLibraryError("material usage state is not configured")
        retained, serialized = prepared or self._prepared_usage_state()
        parent = self._usage_path.parent
        temporary = None
        try:
            if parent.is_symlink() or not parent.is_dir():
                raise OSError("usage state directory is unavailable")
            descriptor, name = tempfile.mkstemp(
                prefix=".usage-", suffix=".tmp", dir=parent,
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._usage_path)
            temporary = None
            self._fsync_directory(parent)
        except OSError as exc:
            raise MaterialLibraryError("material usage state is unavailable") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._usage = retained

    def verify_usage_state(self) -> None:
        self.refresh()
        with self._usage_lock:
            self._apply_receipt_usage()
            self._save_usage()
            self._prune_receipts()
            self._usage_state_ready = True

    def _record_usage(self, sha256: str) -> None:
        entry = self._usage.setdefault(sha256, {"count": 0, "last_used": 0.0})
        entry["count"] = int(entry.get("count", 0) or 0) + 1
        entry["last_used"] = time.time()

    def _usage_values(
        self, value: str | Material | MaterialCandidate,
    ) -> tuple[int, float]:
        key = (
            value.usage_key if isinstance(value, MaterialCandidate)
            else value.sha256 if isinstance(value, Material)
            else value
        )
        entry = self._usage.get(key, {})
        return (
            int(entry.get("count", 0) or 0),
            float(entry.get("last_used", 0.0) or 0.0),
        )

    def _round_robin_recency(
        self, allowed_types: set[str], candidates: list[MaterialCandidate],
    ) -> tuple[dict[str, float], set[str], set[str], set[str]]:
        group_last_used: dict[str, float] = {}
        source_last_used: list[tuple[float, str]] = []
        for material in self._materials:
            if material.media_type not in allowed_types:
                continue
            _count, last_used = self._usage_values(material)
            group_last_used[material.diversity_group] = max(
                group_last_used.get(material.diversity_group, 0.0), last_used,
            )
            source_last_used.append((last_used, material.sha256))
        group_limit = min(
            ROUND_ROBIN_RECENT_GROUP_WINDOW,
            max(0, len(group_last_used) - 1),
        )
        recent_groups = {
            group
            for group, last_used in sorted(
                group_last_used.items(), key=lambda item: (-item[1], item[0]),
            )[:group_limit]
            if last_used > 0
        }
        source_limit = min(
            ROUND_ROBIN_RECENT_SOURCE_WINDOW,
            max(0, len(source_last_used) - 1),
        )
        recent_sources = {
            sha256
            for last_used, sha256 in sorted(
                source_last_used, key=lambda item: (-item[0], item[1]),
            )[:source_limit]
            if last_used > 0
        }
        clip_limit = min(
            ROUND_ROBIN_RECENT_CLIP_WINDOW,
            max(0, len(candidates) - 1),
        )
        recent_clips = {
            candidate.usage_key
            for candidate in sorted(
                candidates,
                key=lambda item: (
                    -self._usage_values(item)[1], item.usage_key,
                ),
            )[:clip_limit]
            if self._usage_values(candidate)[1] > 0
        }
        return group_last_used, recent_groups, recent_sources, recent_clips

    def refresh(self) -> None:
        with self._lock:
            self._reload_if_needed()

    def stats(self) -> dict[str, Any]:
        self.refresh()
        counts = {kind: 0 for kind in ("image", "video", "bgm")}
        for material in self._materials:
            counts[material.media_type] += 1
        return {
            "records": len(self._materials), "media_types": counts,
            "usage_state_ready": self._usage_state_ready,
            "selection_contract_version": SELECTION_CONTRACT_VERSION,
            "clip_contract_version": CLIP_CONTRACT_VERSION,
        }

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

    def _is_available(self, material: Material) -> bool:
        with self._lock:
            verification_lock = self._verification_locks.setdefault(
                material.sha256, threading.Lock(),
            )
        with verification_lock:
            for _attempt in range(2):
                try:
                    _record, path = self.resolve(material.sha256)
                    before = path.stat()
                except (KeyError, OSError, MaterialLibraryError):
                    return False
                identity = (
                    before.st_dev, before.st_ino,
                    before.st_mtime_ns, before.st_size,
                )
                cached = self._verification_cache.get(material.sha256)
                if cached and cached[0] == identity:
                    return cached[1]
                try:
                    actual_sha256 = sha256_file(path)
                    after = path.stat()
                except OSError:
                    continue
                current_identity = (
                    after.st_dev, after.st_ino,
                    after.st_mtime_ns, after.st_size,
                )
                if current_identity != identity:
                    continue
                available = bool(after.st_size) and hmac.compare_digest(
                    actual_sha256, material.sha256,
                )
                self._verification_cache[material.sha256] = (
                    current_identity, available,
                )
                return available
            return False

    def select(
        self,
        scenes: list[dict[str, Any]],
        *,
        orientation: str = "portrait",
        seed: str = "",
        used_sha256: Iterable[str] = (),
        selection_mode: str = "semantic",
        selection_id: str = "",
    ) -> dict[str, Any]:
        mode = _text(selection_mode or "semantic")
        used_values = tuple(str(value).lower() for value in used_sha256)
        receipt_id = str(selection_id or "").strip()
        if receipt_id and (
            mode != "round_robin"
            or not SELECTION_ID_RE.fullmatch(receipt_id)
        ):
            raise ValueError(
                "selection_id requires round_robin and must be a stable key"
            )
        if mode != "round_robin":
            return self._select_impl(
                scenes, orientation=orientation, seed=seed,
                used_sha256=used_values, selection_mode=mode,
            )
        request_sha256 = _selection_request_sha256(
            scenes, str(orientation), str(seed), mode,
        )
        self.refresh()
        with self._usage_lock:
            recovery_usage = {
                key: dict(value) for key, value in self._usage.items()
            }
            try:
                if self._apply_receipt_usage():
                    self._save_usage()
            except BaseException:
                self._usage = recovery_usage
                raise
            if receipt_id:
                receipt = self._selection_receipts.get(receipt_id)
                if receipt is not None:
                    if not hmac.compare_digest(
                        receipt["request_sha256"], request_sha256,
                    ):
                        raise MaterialSelectionConflictError(
                            "selection_id request conflict"
                        )
                    return json.loads(json.dumps(receipt["result"]))
                self._prune_receipts(reserve_slots=1)
            previous_usage = {
                key: dict(value) for key, value in self._usage.items()
            }
            receipt_committed = False
            try:
                result = self._select_impl(
                    scenes, orientation=orientation, seed=seed,
                    used_sha256=used_values, selection_mode=mode,
                )
                prepared = self._prepared_usage_state()
                if receipt_id:
                    usage_after = {
                        key: dict(value)
                        for key, value in prepared[0].items()
                        if previous_usage.get(key) != value
                    }
                    receipt = {
                        "version": SELECTION_RECEIPT_VERSION,
                        "selection_id": receipt_id,
                        "request_sha256": request_sha256,
                        "result": json.loads(json.dumps(result)),
                        "created_at": time.time(),
                        "usage_after": usage_after,
                    }
                    # The receipt must reach disk first so this selection can
                    # repair a crash before the flat usage replacement.
                    self._write_receipt(receipt)
                    self._selection_receipts[receipt_id] = receipt
                    receipt_committed = True
                self._save_usage(prepared)
                self._prune_receipts()
                return result
            except BaseException:
                self._usage = previous_usage
                if not receipt_committed and receipt_id:
                    self._selection_receipts.pop(receipt_id, None)
                raise

    def _select_impl(
        self,
        scenes: list[dict[str, Any]],
        *,
        orientation: str = "portrait",
        seed: str = "",
        used_sha256: Iterable[str] = (),
        selection_mode: str = "semantic",
    ) -> dict[str, Any]:
        self.refresh()
        if not isinstance(scenes, list) or not scenes:
            raise ValueError("scenes must not be empty")
        if len(scenes) > MAX_SELECTION_SCENES:
            raise ValueError(
                f"scenes must not exceed {MAX_SELECTION_SCENES} items"
            )
        if any(not isinstance(scene, dict) for scene in scenes):
            raise ValueError("each scene must be an object")
        requested_orientation = _orientation(orientation)
        mode = _text(selection_mode or "semantic")
        if mode not in {"semantic", "random", "round_robin"}:
            raise ValueError("selection_mode must be semantic, random or round_robin")
        used = {str(value).lower() for value in used_sha256 if SHA256_RE.fullmatch(str(value).lower())}
        used_groups = {
            material.diversity_group
            for sha256 in used
            if (material := self._by_sha.get(sha256)) is not None
        }
        selected: list[dict[str, Any]] = []
        virtual_candidate_ids: set[str] = set()

        for position, scene in enumerate(scenes):
            scene_id = str(scene.get("scene_id") or f"scene_{position + 1:02d}")
            media_type = _text(scene.get("media_type") or "visual")
            clip_duration = _clip_duration(scene.get("clip_duration_seconds"))
            allowed_types = {"image", "video"} if media_type == "visual" else {media_type}
            if not allowed_types <= {"image", "video", "bgm"}:
                raise ValueError(f"unsupported media_type for {scene_id}")
            candidates = []
            for material in self._materials:
                if (
                    material.sha256 in used
                    or material.media_type not in allowed_types
                ):
                    continue
                for candidate in _material_candidates(
                    material, clip_duration,
                ):
                    virtual_candidate_ids.add(candidate.usage_key)
                    if (
                        len(virtual_candidate_ids)
                        > MAX_VIRTUAL_CANDIDATES_PER_REQUEST
                    ):
                        raise MaterialLibraryError(
                            "virtual candidate limit exceeded"
                        )
                    candidates.append(candidate)
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
            scored = [
                (candidate, _score(candidate.material, tokens, query_text))
                for candidate in candidates
            ]
            same_orientation = lambda candidate: (
                candidate.material.media_type == "bgm"
                or requested_orientation == "unknown"
                or candidate.material.orientation
                in {requested_orientation, "unknown"}
            )
            exact_same = [
                (item, score) for item, score in scored
                if score >= 8 and same_orientation(item)
            ]
            loose_same = [
                (item, score) for item, score in scored
                if score > 0 and same_orientation(item)
            ]
            exact_any = [(item, score) for item, score in scored if score >= 8]
            loose_any = [(item, score) for item, score in scored if score > 0]
            random_same = [
                (item, 0) for item, _score_value in scored
                if same_orientation(item)
            ]
            random_all = [(item, 0) for item, _score_value in scored]
            tiers = (
                ((random_all, "random"),) if mode in {"random", "round_robin"} else (
                    (exact_same, "exact"),
                    (loose_same, "loose"),
                    (exact_any, "exact"),
                    (loose_any, "loose"),
                    (random_same, "random"),
                    (random_all, "random"),
                )
            )
            rank_seed = f"{seed}:{scene_id}:{position}"
            selected_pair = None
            match_level = ""
            for pool, level in tiers:
                if mode == "round_robin":
                    minimum_count = min(
                        (self._usage_values(pair[0])[0] for pair in pool),
                        default=0,
                    )
                    (
                        group_last_used, recent_groups,
                        recent_sources, recent_clips,
                    ) = self._round_robin_recency(allowed_types, candidates)

                    def round_robin_key(pair):
                        candidate = pair[0]
                        material = candidate.material
                        count, last_used = self._usage_values(candidate)
                        fairness_distance = min(
                            max(0, count - minimum_count),
                            ROUND_ROBIN_COUNT_SLACK,
                        )
                        rotation_score = fairness_distance
                        if material.diversity_group in recent_groups:
                            rotation_score += ROUND_ROBIN_RECENT_GROUP_PENALTY
                        if material.sha256 in recent_sources:
                            rotation_score += ROUND_ROBIN_RECENT_SOURCE_PENALTY
                        if candidate.usage_key in recent_clips:
                            rotation_score += ROUND_ROBIN_RECENT_CLIP_PENALTY
                        return (
                            material.diversity_group in used_groups,
                            rotation_score,
                            count,
                            group_last_used.get(material.diversity_group, 0.0),
                            self._usage_values(material)[1],
                            last_used,
                            _stable_rank(rank_seed, candidate),
                        )

                    ranked = sorted(
                        pool, key=round_robin_key,
                    )
                else:
                    ranked = sorted(
                        pool,
                        key=lambda pair: (
                            -pair[1], _stable_rank(rank_seed, pair[0])
                        ),
                    )
                selected_pair = next(
                    (
                        pair for pair in ranked
                        if self._is_available(pair[0].material)
                    ),
                    None,
                )
                if selected_pair is not None:
                    match_level = level
                    break
            if selected_pair is None:
                raise MaterialShortageError(
                    f"no healthy approved material remains for {scene_id}"
                )
            candidate, score = selected_pair
            material = candidate.material
            used.add(material.sha256)
            used_groups.add(material.diversity_group)
            if mode == "round_robin":
                self._record_usage(material.sha256)
                if candidate.usage_key != material.sha256:
                    self._record_usage(candidate.usage_key)
            orientation_match = (
                "not_applicable" if material.media_type == "bgm"
                else "same" if same_orientation(candidate)
                else "fallback"
            )
            item = candidate.public_dict(
                match_level, scene_id, orientation_match
            )
            item["match_score"] = score
            selected.append(item)

        return {
            "materials": selected,
            "used_sha256": sorted(used),
            "selection_mode": mode,
            "selection_contract_version": SELECTION_CONTRACT_VERSION,
            "clip_contract_version": CLIP_CONTRACT_VERSION,
            "fallback_policy": (
                [
                    "round_robin_all_orientations_unique"
                    if mode == "round_robin"
                    else "random_all_orientations_unique"
                ]
                if mode in {"random", "round_robin"} else [
                    "exact_same_orientation",
                    "loose_same_orientation",
                    "exact_any_orientation",
                    "loose_any_orientation",
                    "random_unique",
                ]
            ),
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
