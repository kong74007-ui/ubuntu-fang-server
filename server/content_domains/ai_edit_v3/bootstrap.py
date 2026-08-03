"""Lazy production wiring for the isolated AI Edit V3 API and worker."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Mapping

from server.content_domains import ai_edit_v2_platform_assets, points
from server.content_domains.video_asset_publish import build_sqlite_publisher

from .billing import LedgerResult, LedgerTransaction
from .catalog import load_template_catalog
from .contracts import schema_sha256
from .cos import V3Cos
from .feature import load_config
from .media import _probe_image, probe_media
from .production import (
    CapabilityPlaceholder,
    DashScopeAsr,
    ProductionStageCoordinator,
    QwenCompiledDirector,
)
from .providers.base import SecretValue
from .providers.elevenlabs import ElevenLabsAudioGenerator
from .providers.openai_image import OpenAIImageGenerator
from .renderers.hyperframes import HyperframesRenderer
from .renderers.release import verify_renderer_release
from .runtime import (
    RuntimeDependencies,
    build_runtime,
    build_stage_handlers,
    preflight,
)
from .service import CapacityDecision, EditV3Service, UploadObservation
from .store import V3Store


_LOCK = threading.Lock()
_RUNTIME = None
_SERVICE = None


class SystemClock:
    def now(self) -> float:
        return time.time()

    def probe_capability(self, capability: str, *, environment: str | None):
        return {"available": capability == "clock", "environment": environment}


class ProcessSupervisor:
    def terminate_job(self, job_id: str) -> None:
        # The isolated renderer has a separate bounded systemd lifetime.  A lost
        # lease cannot safely guess its instance id here; the render adapter owns
        # that cancellation boundary.
        return None

    def probe_capability(self, capability: str, *, environment: str | None):
        return {
            "available": capability == "process_supervisor",
            "environment": environment,
        }


class HttpPointsLedger:
    @staticmethod
    def _transaction(raw: Mapping[str, Any] | None) -> LedgerTransaction | None:
        if raw is None:
            return None
        operation = str(raw.get("operation") or "")
        if operation not in {"deduct", "refund"}:
            raise ValueError("points_transaction_invalid")
        return LedgerTransaction(
            transaction_key=str(raw["transaction_key"]),
            operation=operation,
            owner=str(raw.get("username", raw.get("owner", ""))),
            amount=int(raw["amount"]),
            points_after=int(raw["points_after"]),
            created_at=int(raw["created_at"]) * 1000 + 999,
        )

    def deduct(self, owner: str, amount: int, transaction_key: str, reason: str):
        try:
            points.deduct_points(owner, amount, reason, transaction_key)
            transaction = self.query_transaction(owner, transaction_key)
            return LedgerResult(transaction is not None, transaction, None)
        except Exception as exc:
            code = getattr(exc, "status", None)
            if code in {400, 402, 409, 422}:
                return LedgerResult(False, None, "points_rejected")
            raise

    def refund(self, owner: str, amount: int, transaction_key: str, reason: str):
        points.refund_points(owner, amount, reason, transaction_key)
        transaction = self.query_transaction(owner, transaction_key)
        return LedgerResult(transaction is not None, transaction, None)

    def query_transaction(self, owner: str, transaction_key: str):
        return self._transaction(points.get_points_transaction(owner, transaction_key))

    def probe_capability(self, capability: str, *, environment: str | None):
        return {
            "available": capability == "points_transaction_query"
            and bool(os.environ.get("HQ_INTERNAL_TOKEN")),
            "environment": environment,
        }


class SharedPublisher:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._delegate = build_sqlite_publisher(self.db_path)

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def probe_capability(self, capability: str, *, environment: str | None):
        return {"available": capability == "asset_publication", "environment": environment}


class UploadInspector:
    def __init__(self, cos: V3Cos) -> None:
        self.cos = cos

    def inspect(self, key: str, *, upload_type: str, head: Mapping[str, Any]):
        suffix = {
            "main_video": ".mp4",
            "main_audio": ".audio",
            "material_image": ".image",
        }[upload_type]
        with tempfile.TemporaryDirectory(prefix="ai-edit-v3-inspect-") as directory:
            path = Path(directory) / f"source{suffix}"
            self.cos.download_file(key, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            mime = str(head["content_type"]).split(";", 1)[0].lower()
            if upload_type == "material_image":
                image = _probe_image(path, timeout_seconds=10)
                return UploadObservation(
                    mime_type=mime,
                    media_kind="image",
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                    width=image.width,
                    height=image.height,
                    probe_evidence={"width": image.width, "height": image.height},
                )
            media = probe_media(path)
            stream = next(
                (
                    item
                    for item in media.streams
                    if item.get("codec_type") == ("video" if upload_type == "main_video" else "audio")
                ),
                {},
            )
            if upload_type == "main_video":
                rate = media.fps_num / media.fps_den if media.fps_num and media.fps_den else None
                evidence = {
                    "duration_ms": media.duration_ms,
                    "width": int(media.width or 0),
                    "height": int(media.height or 0),
                }
                if rate:
                    evidence["frame_rate"] = rate
                return UploadObservation(
                    mime_type=mime,
                    media_kind="video",
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                    duration_ms=media.duration_ms,
                    width=media.width,
                    height=media.height,
                    frame_rate=rate,
                    probe_evidence=evidence,
                )
            evidence = {"duration_ms": media.duration_ms}
            if stream.get("sample_rate"):
                evidence["sample_rate"] = int(stream["sample_rate"])
            if stream.get("channels"):
                evidence["channels"] = int(stream["channels"])
            return UploadObservation(
                mime_type=mime,
                media_kind="audio",
                size_bytes=path.stat().st_size,
                sha256=digest,
                duration_ms=media.duration_ms,
                probe_evidence=evidence,
            )


class ProductionCatalog:
    def __init__(self, templates) -> None:
        self.templates = tuple(templates)
        self._probe_cache: dict[tuple[str, int], tuple[int, str]] = {}

    def _platform(self, owner: str, asset_id: str) -> dict[str, Any] | None:
        try:
            row = ai_edit_v2_platform_assets._owned_row(owner, int(asset_id))
        except (TypeError, ValueError):
            return None
        if row is None or not ai_edit_v2_platform_assets._is_digital_ip_asset(row):
            return None
        path = ai_edit_v2_platform_assets._source_path(row["video_file"])
        stamp = int(path.stat().st_mtime_ns)
        cached = self._probe_cache.get((owner, int(asset_id)))
        if cached is None or cached[0] != stamp:
            media = probe_media(path)
            ratio = "9:16" if (media.height or 0) > (media.width or 0) else "16:9"
            cached = (stamp, f"{media.duration_ms}:{ratio}")
            self._probe_cache[(owner, int(asset_id))] = cached
        duration, ratio = cached[1].split(":", 1)
        title = " ".join(str(row["text"] or "").split())[:120] or path.stem[:120]
        return {
            "asset_id": str(asset_id),
            "title": title,
            "cover_asset_id": str(asset_id),
            "cover_reference": str(asset_id),
            "duration_ms": int(duration),
            "ratio": ratio,
            "status": "ready",
        }

    def list_platform_assets(self, owner: str):
        values = []
        for item in ai_edit_v2_platform_assets.list_assets(owner, limit=100):
            record = self._platform(owner, str(item["id"]))
            if record is not None:
                values.append(record)
        return values

    def resolve_platform_asset(self, owner: str, asset_id: str):
        return self._platform(owner, asset_id)

    def list_audio_assets(self, owner: str):
        return []

    def resolve_audio_asset(self, owner: str, asset_id: str):
        return None

    def list_voices(self, owner: str):
        return []

    def resolve_voice(self, owner: str, voice_id: str):
        return None

    def list_templates(self, owner: str):
        return [
            {
                "template_id": item.template_id,
                "version": item.version,
                "title": item.title,
                "description": item.creative_direction,
                "preview_asset_id": item.template_id,
                "preview_reference": item.template_id,
                "supported_ratios": list(item.supported_ratios),
            }
            for item in self.templates
            if item.status == "published"
        ]

    def resolve_template(self, template_id: str, ratio: str):
        for item in self.templates:
            if item.template_id == template_id and item.status == "published" and (
                ratio == "auto" or ratio in item.supported_ratios
            ):
                return {
                    "template_id": item.template_id,
                    "version": item.version,
                    "status": item.status,
                    "supported_ratios": list(item.supported_ratios),
                    "ratio": item.ratio,
                }
        return None

    @staticmethod
    def preview(owner: str, asset_id: str) -> dict[str, str] | None:
        for item in ai_edit_v2_platform_assets.list_assets(owner, limit=100):
            if str(item["id"]) == str(asset_id):
                return {
                    "video_url": item.get("preview_url"),
                    "cover_url": item.get("thumbnail_url"),
                }
        return None


class Capacity:
    def __init__(self, store: V3Store, queue_capacity: int, temp_limit: int, work_root: Path):
        self.store = store
        self.queue_capacity = queue_capacity
        self.temp_limit = temp_limit
        self.work_root = work_root

    def check(self, normalized_request: Mapping[str, Any]):
        active = self.store._read(
            lambda connection: int(
                connection.execute(
                    "SELECT COUNT(*) FROM edit_v3_jobs WHERE state NOT IN ('completed','refunded','prehold_absent')"
                ).fetchone()[0]
            )
        )
        free = shutil.disk_usage(self.work_root.parent).free
        required = min(self.temp_limit, 2 * 1024 * 1024 * 1024)
        slots = max(0, self.queue_capacity - active)
        accepted = slots > 0 and free >= required
        return CapacityDecision(accepted, slots, required, None if accepted else 15)


def _read_secret(path: Path) -> bytes:
    value = path.read_bytes().strip()
    if len(value) < 16 or len(set(value)) < 8:
        raise RuntimeError("owner_hmac_secret_invalid")
    return value


def _seed(store: V3Store, now_ms: int) -> tuple[Any, ...]:
    templates = load_template_catalog()
    store.seed_template_versions(templates, now_ms=now_ms)
    if not store.list_published_pricing_versions():
        parameters = {
            "parts": {
                "base_task": {"ceiling_quantity": 1, "min_rate": 20, "max_rate": 20},
                "duration_tier": {"ceiling_quantity": 10, "min_rate": 1, "max_rate": 2},
                "tts_ceiling": {"ceiling_quantity": 4, "unit_size": 1000, "min_rate": 0, "max_rate": 1},
                "qwen_ceiling": {"ceiling_quantity": 2, "min_rate": 2, "max_rate": 3},
                "image_ceiling": {"ceiling_quantity": 10, "min_rate": 0, "max_rate": 2},
                "bgm_sfx_ceiling": {"ceiling_quantity": 6, "min_rate": 1, "max_rate": 2},
                "render_complexity": {"ceiling_quantity": 3, "min_rate": 2, "max_rate": 4},
                "one_repair_reserve": {"ceiling_quantity": 1, "min_rate": 2, "max_rate": 5},
            }
        }
        store.insert_pricing_version(
            "v3-test-2026-08-03",
            parameters,
            status="published",
            created_at=now_ms,
            published_at=now_ms,
        )
    return templates


def _build():
    config = load_config()
    if not config.enabled:
        return build_runtime(), None
    assert config.db_path and config.v2_db_path and config.environment
    assert config.owner_hmac_secret_file and config.queue_capacity and config.temp_bytes_limit
    store = V3Store(config.db_path, v2_db_path=config.v2_db_path, environment=config.environment)
    templates = _seed(store, int(time.time() * 1000))
    secret = _read_secret(config.owner_hmac_secret_file)
    cos = V3Cos(environment=config.environment)
    renderer_root = Path(
        os.environ.get("AI_EDIT_V3_RENDERER_ROOT", "/opt/huangque/ai-edit-v3-renderer/current")
    ).resolve()
    release = verify_renderer_release(renderer_root)
    registry = (renderer_root / "registry-sha256.txt").read_text(encoding="ascii").strip()
    renderer = HyperframesRenderer(
        renderer_build_id=release.renderer_build_id,
        registry_sha256=registry,
        schema_sha256=schema_sha256("render-manifest-v1.schema.json"),
    )
    audio_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    audio = ElevenLabsAudioGenerator(api_key=SecretValue(audio_key) if audio_key else None)
    image_key = os.environ.get("OPENAI_API_KEY", "").strip()
    image_generator = OpenAIImageGenerator(
        api_key=SecretValue(image_key) if image_key else None
    )
    asr = DashScopeAsr()
    director = QwenCompiledDirector()
    work_root = Path(
        os.environ.get("AI_EDIT_V3_WORK_ROOT", "/var/lib/huangque-ai-edit-v3/work")
    ).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    coordinator = ProductionStageCoordinator(
        store=store,
        cos=cos,
        asr=asr,
        director=director,
        audio_generator=audio,
        image_generator=image_generator,
        renderer=renderer,
        work_root=work_root,
        owner_hmac_secret=secret,
        renderer_root=renderer_root,
    )
    ledger = HttpPointsLedger()
    asset_db = Path(
        os.environ.get("CONTENT_ASSET_DB", Path(__file__).resolve().parents[2] / "audio_assets.db")
    ).resolve()
    publisher = SharedPublisher(asset_db)
    dependencies = RuntimeDependencies(
        store=store,
        clock=SystemClock(),
        points=ledger,
        assets=publisher,
        cos=cos,
        tts=CapabilityPlaceholder("tts"),
        asr=asr,
        director=director,
        image_generator=image_generator,
        audio_generator=audio,
        renderer=renderer,
        process_supervisor=ProcessSupervisor(),
        stage_handlers=build_stage_handlers(coordinator),
    )
    runtime = build_runtime(dependencies)
    catalog = ProductionCatalog(templates)
    capacity = Capacity(store, config.queue_capacity, config.temp_bytes_limit, work_root)
    service = EditV3Service(
        store,
        object_store=cos,
        upload_inspector=UploadInspector(cos),
        owner_hmac_secret=secret,
        enabled=True,
        source_catalog=catalog,
        capacity_gate=capacity,
        capability_report=lambda: preflight(runtime),
        result_signer=lambda key, expires, download: cos.presign_get(key, expires=expires),
    )
    service.platform_catalog = catalog
    return runtime, service


def get_default_runtime():
    global _RUNTIME, _SERVICE
    if _RUNTIME is None:
        with _LOCK:
            if _RUNTIME is None:
                _RUNTIME, _SERVICE = _build()
    return _RUNTIME


def get_default_service():
    get_default_runtime()
    return _SERVICE


def reset_for_tests() -> None:
    global _RUNTIME, _SERVICE
    with _LOCK:
        _RUNTIME = None
        _SERVICE = None


__all__ = ("get_default_runtime", "get_default_service", "reset_for_tests")
