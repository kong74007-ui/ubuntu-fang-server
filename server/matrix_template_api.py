#!/usr/bin/env python3
"""Internal API for catalog-driven text-media-text matrix videos."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfileobj
from urllib.parse import urlsplit


MAX_BODY_BYTES = 128 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_WAITING_JOBS = 20
RENDER_TIMEOUT_SECONDS = 900
DEFAULT_RETENTION_SECONDS = 72 * 60 * 60
DEFAULT_DELIVERY_GRACE_SECONDS = 60 * 60
DEFAULT_CLEANUP_INTERVAL_SECONDS = 15 * 60
DEFAULT_CLEANUP_BATCH_SIZE = 10
DEFAULT_DISK_HIGH_WATER_PERCENT = 95.0
STATUS_WRITE_ATTEMPTS = 3
STATUS_WRITE_RETRY_SECONDS = 0.1
JOB_REQUEUE_SECONDS = 0.25
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CONTENT_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
}
FONT_VARIANTS = {
    "native-bold": (
        ("clean", "Noto Sans SC", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
        ("rounded", "GenSenRounded TW H", "Noto Sans SC"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
    ),
    "video-diary": (
        ("handwritten", "Ma Shan Zheng", "ZCOOL XiaoWei"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("friendly", "ZCOOL KuaiLe", "Noto Sans SC"),
    ),
    "minimal-headline": (
        ("geometric", "Gen Jyuu Gothic Heavy", "GenSenRounded TW H"),
        ("serif", "HouZunSongTi", "Noto Sans SC"),
        ("clean", "Noto Sans SC", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
    ),
    "airy-blush": (
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
        ("friendly", "ZCOOL KuaiLe", "Noto Sans SC"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("handwritten", "Ma Shan Zheng", "ZCOOL XiaoWei"),
    ),
    "yellow-blue-pop": (
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("friendly", "ZCOOL KuaiLe", "Noto Sans SC"),
        ("rounded", "GenSenRounded TW H", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
    ),
    "business-black": (
        ("geometric", "Gen Jyuu Gothic Heavy", "GenSenRounded TW H"),
        ("rounded", "GenSenRounded TW H", "GenSenRounded TW H"),
        ("serif", "HouZunSongTi", "Noto Sans SC"),
        ("clean", "Noto Sans SC", "Noto Sans SC"),
    ),
    "black-gold-premium": (
        ("serif", "HouZunSongTi", "GenSenRounded TW H"),
        ("heritage", "Ma Shan Zheng", "ZCOOL XiaoWei"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("clean", "Noto Sans SC", "GenSenRounded TW H"),
    ),
    "data-compare": (
        ("geometric", "Gen Jyuu Gothic Heavy", "GenSenRounded TW H"),
        ("rounded", "GenSenRounded TW H", "GenSenRounded TW H"),
        ("clean", "Noto Sans SC", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
    ),
    "chinese-title": (
        ("serif", "HouZunSongTi", "ZCOOL XiaoWei"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("brush", "Ma Shan Zheng", "ZCOOL XiaoWei"),
        ("friendly", "ZCOOL KuaiLe", "GenSenRounded TW H"),
    ),
    "torn-magazine": (
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
        ("friendly", "ZCOOL KuaiLe", "Noto Sans SC"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("clean", "Noto Sans SC", "Noto Sans SC"),
    ),
    "vlog-journal": (
        ("friendly", "ZCOOL KuaiLe", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
        ("handwritten", "Ma Shan Zheng", "ZCOOL XiaoWei"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
    ),
    "bilingual-split": (
        ("geometric", "Gen Jyuu Gothic Heavy", "GenSenRounded TW H"),
        ("rounded", "GenSenRounded TW H", "GenSenRounded TW H"),
        ("clean", "Noto Sans SC", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
    ),
    "portrait-quote": (
        ("serif", "HouZunSongTi", "GenSenRounded TW H"),
        ("handwritten", "Ma Shan Zheng", "ZCOOL XiaoWei"),
        ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"),
        ("modern", "Smiley Sans Oblique", "GenSenRounded TW H"),
    ),
}


def _font_selection(template_id: str, job_id: str) -> dict:
    options = FONT_VARIANTS.get(template_id) or FONT_VARIANTS["native-bold"]
    digest = hashlib.sha256(f"{template_id}:{job_id}".encode("utf-8")).digest()
    variant, top_font, bottom_font = options[int.from_bytes(digest[:4], "big") % len(options)]
    return {
        "variant": variant,
        "top_font": top_font,
        "bottom_font": bottom_font,
    }


class MatrixTemplateError(RuntimeError):
    pass


class QueueCapacityError(MatrixTemplateError):
    pass


class DiskCapacityError(MatrixTemplateError):
    pass


def runtime_build_id() -> str:
    path = Path(__file__).resolve().parent / "BUILD_ID"
    try:
        value = path.read_text(encoding="ascii").strip().lower()
    except OSError:
        return "development"
    return value if SHA_RE.fullmatch(value) else "invalid"


def _now() -> int:
    return int(time.time())


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MatrixTemplateError(f"JSON root must be an object: {path.name}")
    return value


def _duration(top: str, bottom: str, requested) -> float:
    visible = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", top + bottom))
    minimum = max(8.0, visible / 5.0 + 1.5)
    if requested not in (None, ""):
        try:
            minimum = max(minimum, float(requested))
        except (TypeError, ValueError) as exc:
            raise ValueError("duration must be numeric") from exc
    if minimum > 15:
        raise ValueError("文案过长，请缩短标题或行动文案")
    return round(minimum, 3)


def _required_visuals(duration: float) -> int:
    return 2 if duration <= 10 else 3


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "delivered_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN delivered_at INTEGER")
            if "cleaned_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN cleaned_at INTEGER")
            db.execute("UPDATE jobs SET status='pending', error=NULL WHERE status='running'")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, request_id: str, payload: dict, admission_guard=None) -> tuple[dict, bool]:
        now = _now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                if json.loads(existing["payload"]) != payload:
                    raise ValueError("request_id already belongs to another payload")
                return self.public(existing), False
            waiting = int(db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0])
            if waiting >= MAX_WAITING_JOBS:
                raise QueueCapacityError("任务队列已满")
            if admission_guard is not None:
                admission_guard()
            job_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO jobs(
                    id,request_id,status,payload,result,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, request_id, "pending", json.dumps(payload, ensure_ascii=False),
                 None, None, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self.public(row), True

    def get(self, job_id: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def pending_ids(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE status='pending' ORDER BY created_at,id"
            )]

    def cleanup_candidates(self, *, now: int, retention_seconds: int,
                           delivery_grace_seconds: int, limit: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("""
                SELECT * FROM jobs
                WHERE cleaned_at IS NULL
                  AND status IN ('completed','failed')
                  AND (
                    updated_at <= ?
                    OR (delivered_at IS NOT NULL AND delivered_at <= ?)
                  )
                ORDER BY updated_at,id
                LIMIT ?
            """, (now - retention_seconds, now - delivery_grace_seconds, limit)))

    def mark_delivered(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET delivered_at=COALESCE(delivered_at,?) WHERE id=? AND status='completed'",
                (_now(), job_id),
            )

    def mark_cleaned(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET cleaned_at=? WHERE id=? AND status IN ('completed','failed')",
                (_now(), job_id),
            )

    def update(self, job_id: str, status: str, *, result=None, error=None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result else None,
                 str(error or "")[:500] or None, _now(), job_id),
            )

    @staticmethod
    def public(row) -> dict:
        result = json.loads(row["result"]) if row["result"] else None
        value = {
            "job_id": row["id"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if result:
            value["result"] = result
        if row["error"]:
            value["error"] = row["error"]
        if "cleaned_at" in row.keys() and row["cleaned_at"]:
            value["cleaned_at"] = row["cleaned_at"]
        return value


class MatrixTemplateService:
    def __init__(self, *, data_root: Path, skill_root: Path, library_url: str,
                 library_token: str, python: str = sys.executable,
                 start_worker: bool = True,
                 retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                 delivery_grace_seconds: int = DEFAULT_DELIVERY_GRACE_SECONDS,
                 cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
                 cleanup_batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
                 disk_high_water_percent: float = DEFAULT_DISK_HIGH_WATER_PERCENT):
        self.data_root = data_root.resolve()
        self.skill_root = skill_root.resolve()
        self.library_url = library_url.rstrip("/")
        self.library_token = library_token
        parsed_library = urlsplit(self.library_url)
        if (
            parsed_library.scheme != "http"
            or parsed_library.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_library.path not in {"", "/"}
            or parsed_library.username or parsed_library.password
            or parsed_library.query or parsed_library.fragment
        ):
            raise MatrixTemplateError("material library URL must be loopback HTTP")
        if not self.library_token:
            raise MatrixTemplateError("material library token is missing")
        self.python = python
        self.retention_seconds = max(60, int(retention_seconds))
        self.delivery_grace_seconds = max(60, int(delivery_grace_seconds))
        self.cleanup_interval_seconds = max(1, int(cleanup_interval_seconds))
        self.cleanup_batch_size = max(1, int(cleanup_batch_size))
        self.disk_high_water_percent = float(disk_high_water_percent)
        if not 1 <= self.disk_high_water_percent <= 100:
            raise MatrixTemplateError("disk high-water percent must be between 1 and 100")
        self.store = JobStore(self.data_root / "jobs.db")
        # Recovery may legitimately contain one formerly-running job plus the
        # full waiting allowance. Admission is bounded transactionally in DB;
        # the in-memory recovery queue must not impose a second, smaller cap.
        self.jobs: queue.Queue[str] = queue.Queue()
        self.queue_lock = threading.Lock()
        self.queued_jobs: set[str] = set()
        self.active_jobs: set[str] = set()
        self.stop_event = threading.Event()
        self.worker_degraded = threading.Event()
        self.process_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.active_downloads: set[str] = set()
        self.active_process = None
        self.worker = None
        self.cleanup_worker = None
        self.workers_expected = start_worker
        self.catalog = self._load_catalog()
        self.templates = {item["id"]: item for item in self.catalog}
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._purge_trash()
        self.cleanup_once()
        for job_id in self.store.pending_ids():
            self._enqueue(job_id)
        if start_worker:
            self.worker = threading.Thread(target=self._worker, daemon=True)
            self.worker.start()
            self.cleanup_worker = threading.Thread(target=self._cleanup_worker, daemon=True)
            self.cleanup_worker.start()

    def _load_catalog(self) -> list[dict]:
        path = self.skill_root / "assets/templates/catalog.json"
        catalog = _read_json(path)
        if catalog.get("version") != 1 or not isinstance(catalog.get("templates"), list):
            raise MatrixTemplateError("invalid template catalog")
        result = []
        for item in catalog["templates"]:
            if not isinstance(item, dict):
                raise MatrixTemplateError("invalid template record")
            template_id = str(item.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
                raise MatrixTemplateError("invalid template id")
            result.append({
                "id": template_id,
                "name": str(item.get("name") or template_id)[:40],
                "description": str(item.get("description") or "")[:160],
                "tags": [str(tag)[:20] for tag in (item.get("tags") or [])[:8]],
            })
        if len(result) != 13 or len({item["id"] for item in result}) != 13:
            raise MatrixTemplateError("expected exactly 13 unique templates")
        return result

    def validate_payload(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("request body must be an object")
        top = " ".join(str(raw.get("top_text") or "").split())
        bottom = " ".join(str(raw.get("bottom_text") or "").split())
        if not 2 <= len(top) <= 60:
            raise ValueError("顶部标题需要 2-60 个字符")
        if not 2 <= len(bottom) <= 80:
            raise ValueError("底部行动文案需要 2-80 个字符")
        template_id = str(raw.get("template_id") or "native-bold")
        if template_id not in self.templates:
            raise ValueError("请选择有效模板")
        duration = _duration(top, bottom, raw.get("duration"))
        bgm = raw.get("bgm", True)
        if not isinstance(bgm, bool):
            raise ValueError("bgm must be boolean")
        return {
            "top_text": top, "bottom_text": bottom,
            "template_id": template_id, "duration": duration,
            "bgm": bgm,
        }

    def submit(self, raw: dict, request_id: str) -> dict:
        if not REQUEST_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        payload = self.validate_payload(raw)
        job, created = self.store.create(
            request_id, payload, admission_guard=self._ensure_disk_capacity
        )
        if created:
            self._enqueue(job["job_id"])
        return job

    def _enqueue(self, job_id: str) -> bool:
        with self.queue_lock:
            if job_id in self.queued_jobs or job_id in self.active_jobs:
                return False
            self.queued_jobs.add(job_id)
            self.jobs.put_nowait(job_id)
            return True

    def health(self) -> dict:
        worker_alive = self.worker is not None and self.worker.is_alive()
        cleanup_alive = self.cleanup_worker is not None and self.cleanup_worker.is_alive()
        worker_degraded = self.worker_degraded.is_set()
        ready = not self.workers_expected or (
            worker_alive and cleanup_alive and not worker_degraded
        )
        return {
            "ok": ready,
            "worker_alive": worker_alive,
            "cleanup_worker_alive": cleanup_alive,
            "worker_degraded": worker_degraded,
        }

    def _ensure_disk_capacity(self) -> None:
        usage = shutil.disk_usage(self.data_root)
        used_percent = 100.0 * usage.used / max(1, usage.total)
        if used_percent >= self.disk_high_water_percent:
            raise DiskCapacityError("生成服务器存储空间不足，请稍后再试")

    def _purge_trash(self) -> None:
        trash = self.data_root / ".trash"
        if not trash.is_dir():
            return
        for path in list(trash.iterdir())[:self.cleanup_batch_size]:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def cleanup_once(self, *, now: int | None = None) -> int:
        cleaned = 0
        current = _now() if now is None else int(now)
        candidates = self.store.cleanup_candidates(
            now=current,
            retention_seconds=self.retention_seconds,
            delivery_grace_seconds=self.delivery_grace_seconds,
            limit=self.cleanup_batch_size,
        )
        trash = self.data_root / ".trash"
        for row in candidates:
            job_id = row["id"]
            with self.file_lock:
                if job_id in self.active_downloads:
                    continue
                root = self.data_root / job_id
                moved = None
                if root.exists():
                    trash.mkdir(parents=True, exist_ok=True)
                    moved = trash / f"{job_id}-{uuid.uuid4().hex}"
                    os.replace(root, moved)
                self.store.mark_cleaned(job_id)
            if moved is not None:
                shutil.rmtree(moved, ignore_errors=True)
            cleaned += 1
        return cleaned

    def _cleanup_worker(self) -> None:
        while not self.stop_event.wait(self.cleanup_interval_seconds):
            try:
                self._purge_trash()
                self.cleanup_once()
            except Exception as exc:
                print(f"[matrix-template] cleanup failed: {exc}", flush=True)

    @contextlib.contextmanager
    def open_completed_file(self, job_id: str):
        with self.file_lock:
            row = self.store.get(job_id)
            expected_url = f"/v1/files/{job_id}.mp4"
            result = json.loads(row["result"]) if row and row["result"] else {}
            if (
                not row or row["status"] != "completed" or row["cleaned_at"]
                or result.get("file_url") != expected_url
            ):
                raise FileNotFoundError(job_id)
            output = self.data_root / job_id / "output/published.mp4"
            handle = output.open("rb")
            self.active_downloads.add(job_id)
        try:
            yield handle
            self.store.mark_delivered(job_id)
        finally:
            handle.close()
            with self.file_lock:
                self.active_downloads.discard(job_id)

    def _discard_output(self, job_id: str) -> None:
        output_dir = self.data_root / job_id / "output"
        for name in ("final.mp4", "published.mp4"):
            (output_dir / name).unlink(missing_ok=True)

    def _library_request(self, method: str, path: str, body=None):
        data = _json_bytes(body) if body is not None else None
        request = urllib.request.Request(
            self.library_url + path, data=data, method=method,
            headers={
                "Authorization": "Bearer " + self.library_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            raise MatrixTemplateError(str(detail or "平台素材库暂不可用")) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MatrixTemplateError("平台素材库暂不可用") from exc

    def _select_materials(self, payload: dict, job_id: str) -> list[dict]:
        count = _required_visuals(payload["duration"])
        query = payload["top_text"] + " " + payload["bottom_text"]
        scenes = [{
            "scene_id": "media_01", "query": query,
            "purpose": "模板成片主视频", "media_type": "video",
        }]
        scenes.extend({
            "scene_id": f"media_{index:02d}", "query": query,
            "purpose": "模板成片补充素材", "media_type": "visual",
        } for index in range(2, count + 1))
        if payload["bgm"]:
            scenes.append({
                "scene_id": "bgm", "query": query,
                "purpose": "模板成片背景音乐", "media_type": "bgm",
            })
        result = self._library_request("POST", "/v1/select", {
            "scenes": scenes, "orientation": "portrait", "seed": job_id,
        })
        values = result.get("materials") or []
        by_scene = {str(item.get("scene_id") or ""): item for item in values if isinstance(item, dict)}
        expected = [scene["scene_id"] for scene in scenes]
        if set(by_scene) != set(expected) or len(by_scene) != len(expected):
            raise MatrixTemplateError("素材库返回的分镜绑定不完整")
        ordered = [by_scene[scene_id] for scene_id in expected]
        shas = [str(item.get("sha256") or "").lower() for item in ordered]
        if any(not SHA_RE.fullmatch(value) for value in shas) or len(set(shas)) != len(shas):
            raise MatrixTemplateError("素材库返回了无效或重复素材")
        if ordered[0].get("media_type") != "video":
            raise MatrixTemplateError("模板成片至少需要一个视频素材")
        for item in ordered[1:count]:
            if item.get("media_type") not in {"image", "video"}:
                raise MatrixTemplateError("素材库返回了无效画面素材")
        if payload["bgm"] and ordered[-1].get("media_type") != "bgm":
            raise MatrixTemplateError("素材库返回了无效背景音乐")
        return ordered

    def _download(self, item: dict, target_dir: Path) -> Path:
        sha = str(item["sha256"]).lower()
        request = urllib.request.Request(
            self.library_url + "/v1/assets/" + sha,
            headers={"Authorization": "Bearer " + self.library_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_type = response.headers.get_content_type()
                suffix = CONTENT_SUFFIXES.get(content_type)
                if not suffix:
                    raise MatrixTemplateError("素材库文件类型不受支持")
                target = target_dir / (sha + suffix)
                temporary = target.with_suffix(target.suffix + ".part")
                digest = hashlib.sha256()
                total = 0
                try:
                    with temporary.open("wb") as handle:
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_ASSET_BYTES:
                                raise MatrixTemplateError("素材库文件过大")
                            digest.update(chunk)
                            handle.write(chunk)
                    if not total or not hmac.compare_digest(digest.hexdigest(), sha):
                        raise MatrixTemplateError("素材库文件校验失败")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                return target
        except urllib.error.HTTPError as exc:
            raise MatrixTemplateError("素材库文件读取失败") from exc

    def _project(self, payload: dict, job_id: str, materials: list[dict], paths: list[Path]) -> dict:
        count = _required_visuals(payload["duration"])
        font_selection = _font_selection(payload["template_id"], job_id)
        media = []
        for item, path in zip(materials[:count], paths[:count]):
            media.append({
                "path": path.relative_to(self.data_root / job_id).as_posix(),
                "type": item["media_type"],
                "record_id": item.get("record_id"),
            })
        project = {
            "version": 1,
            "project_id": job_id,
            "source_text": payload["top_text"] + "\n" + payload["bottom_text"],
            "platforms": ["douyin", "xiaohongshu", "wechat_channels"],
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "material_library": {
                "enabled": True, "index_source": "huangque-internal-api",
                "required_status": "可使用", "selection_policy": "library-only",
            },
            "layout": {
                "template_id": payload["template_id"],
                "top_font": font_selection["top_font"],
                "bottom_font": font_selection["bottom_font"],
            },
            "font_selection": font_selection,
            "material_policy": {"allow_image_only": False, "image_only_reason": ""},
            "voice": {"enabled": False},
            "scenes": [{
                "id": "s01", "role": "hook", "text": "",
                "top_text": payload["top_text"],
                "bottom_text": payload["bottom_text"],
                "duration": payload["duration"], "media": media,
                "motion": "zoom-in", "transition": "cut",
                "caption_chunks": [], "sfx": [],
            }],
            "render": {
                "output": "output/final.mp4", "video_codec": "libx264",
                "audio_codec": "aac", "crf": 18, "preset": "medium",
            },
        }
        if payload["bgm"]:
            bgm_item, bgm_path = materials[-1], paths[-1]
            project["bgm"] = {
                "enabled": True,
                "path": bgm_path.relative_to(self.data_root / job_id).as_posix(),
                "record_id": bgm_item.get("record_id"),
                "loop_mode": "crossfade", "target_lufs": -18,
            }
        return project

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    def _render(self, project_path: Path) -> None:
        output = project_path.parent / "output/final.mp4"
        command = [
            self.python, str(self.skill_root / "scripts/render_video.py"),
            str(project_path),
        ]
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(command, **options)
        with self.process_lock:
            self.active_process = process
        try:
            try:
                _stdout, stderr = process.communicate(timeout=RENDER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                output.unlink(missing_ok=True)
                raise MatrixTemplateError("模板成片渲染超时") from exc
            if process.returncode:
                output.unlink(missing_ok=True)
                detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
                raise MatrixTemplateError("模板成片渲染失败" + (": " + detail if detail else ""))
        finally:
            with self.process_lock:
                if self.active_process is process:
                    self.active_process = None

    def _probe(self, output: Path) -> dict:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        duration = float((data.get("format") or {}).get("duration") or 0)
        if not video or video.get("codec_name") != "h264" or (video.get("width"), video.get("height")) != (1080, 1920):
            raise MatrixTemplateError("模板成片画面规格校验失败")
        if not audio or audio.get("codec_name") != "aac" or duration <= 0:
            raise MatrixTemplateError("模板成片音频或时长校验失败")
        return {"duration": round(duration, 3), "width": 1080, "height": 1920}

    def _execute(self, job_id: str) -> dict:
        row = self.store.get(job_id)
        payload = json.loads(row["payload"])
        root = self.data_root / job_id
        self._discard_output(job_id)
        assets = root / "assets/library"
        assets.mkdir(parents=True, exist_ok=True)
        materials = self._select_materials(payload, job_id)
        paths = [self._download(item, assets) for item in materials]
        project = self._project(payload, job_id, materials, paths)
        project_path = root / "project.json"
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        self._render(project_path)
        output = root / "output/final.mp4"
        try:
            probe = self._probe(output)
            os.replace(output, root / "output/published.mp4")
        except Exception:
            self._discard_output(job_id)
            raise
        return {
            **probe,
            "template_id": payload["template_id"],
            "file_url": f"/v1/files/{job_id}.mp4",
            "font_selection": project["font_selection"],
            "material_manifest": [{
                "record_id": item.get("record_id"), "sha256": item.get("sha256"),
                "media_type": item.get("media_type"), "match_level": item.get("match_level"),
            } for item in materials],
        }

    def _update_with_retry(self, job_id: str, status: str, **kwargs) -> bool:
        for attempt in range(1, STATUS_WRITE_ATTEMPTS + 1):
            try:
                self.store.update(job_id, status, **kwargs)
                return True
            except Exception as exc:
                print(
                    f"[matrix-template] status write failed job={job_id} "
                    f"status={status} attempt={attempt}: {exc}",
                    flush=True,
                )
                if attempt < STATUS_WRITE_ATTEMPTS:
                    self.stop_event.wait(STATUS_WRITE_RETRY_SECONDS)
        return False

    def _run_job(self, job_id: str) -> bool:
        if not self._update_with_retry(job_id, "running"):
            return False
        try:
            result = self._execute(job_id)
            if self._update_with_retry(job_id, "completed", result=result):
                return True
            self._discard_output(job_id)
            if self._update_with_retry(
                job_id, "failed", error="模板成片完成状态保存失败"
            ):
                return True
            return False
        except Exception as exc:
            self._discard_output(job_id)
            return self._update_with_retry(job_id, "failed", error=exc)

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job_id = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.queue_lock:
                self.queued_jobs.discard(job_id)
                self.active_jobs.add(job_id)
            finished = False
            try:
                finished = self._run_job(job_id)
            except Exception as exc:
                print(
                    f"[matrix-template] unexpected worker error job={job_id}: {exc}",
                    flush=True,
                )
            finally:
                with self.queue_lock:
                    self.active_jobs.discard(job_id)
                self.jobs.task_done()
            if finished:
                self.worker_degraded.clear()
            else:
                self.worker_degraded.set()
                if not self.stop_event.wait(JOB_REQUEUE_SECONDS):
                    self._enqueue(job_id)

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.process_lock:
            process = self.active_process
        if process is not None:
            self._terminate(process)
        if self.worker is not None:
            self.worker.join(timeout=3)
        if self.cleanup_worker is not None:
            self.cleanup_worker.join(timeout=3)


class Handler(BaseHTTPRequestHandler):
    server_version = "HuangqueMatrixTemplate/1.0"

    @property
    def service(self) -> MatrixTemplateService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        print("[matrix-template] " + fmt % args, flush=True)

    def send_json(self, status: int, payload: dict):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        supplied = value[7:].strip() if value.lower().startswith("bearer ") else ""
        expected = self.server.api_token  # type: ignore[attr-defined]
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            health = self.service.health()
            self.send_json(200 if health["ok"] else 503, {
                **health, "build_id": runtime_build_id(),
                "templates": len(self.service.catalog), "concurrency": 1,
            })
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        if path == "/v1/templates":
            self.send_json(200, {"templates": self.service.catalog, "default_template": "native-bold"})
            return
        if path.startswith("/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            if not JOB_RE.fullmatch(job_id):
                self.send_json(404, {"error": "not_found"})
                return
            row = self.service.store.get(job_id)
            self.send_json(200, self.service.store.public(row)) if row else self.send_json(404, {"error": "not_found"})
            return
        match = re.fullmatch(r"/v1/files/([0-9a-f]{32})\.mp4", path)
        if match:
            file_context = self.service.open_completed_file(match.group(1))
            try:
                handle = file_context.__enter__()
            except (FileNotFoundError, OSError):
                self.send_json(404, {"error": "not_found"})
                return
            try:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                copyfileobj(handle, self.wfile, 1024 * 1024)
            except BaseException:
                file_context.__exit__(*sys.exc_info())
                raise
            else:
                file_context.__exit__(None, None, None)
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/v1/jobs", "/v1/preflight"}:
            self.send_json(404, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if path == "/v1/preflight":
                payload = self.service.validate_payload(body)
                self.send_json(200, {
                    "ok": True,
                    "payload": payload,
                    "duration": payload["duration"],
                    "required_visuals": _required_visuals(payload["duration"]),
                })
                return
            request_id = str(self.headers.get("X-Request-Id") or "")
            job = self.service.submit(body, request_id)
            self.send_json(202, job)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": "invalid_request", "detail": str(exc)})
        except MatrixTemplateError as exc:
            self.send_json(409, {"error": "submission_failed", "detail": str(exc)})


def build_server(host: str, port: int, service: MatrixTemplateService, token: str):
    if not token:
        raise SystemExit("MATRIX_TEMPLATE_API_TOKEN is required")
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service  # type: ignore[attr-defined]
    server.api_token = token  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8112)
    args = parser.parse_args()
    service = MatrixTemplateService(
        data_root=Path(os.environ.get("MATRIX_TEMPLATE_DATA_ROOT", "/var/lib/huangque-matrix-template")),
        skill_root=Path(os.environ.get("MATRIX_TEMPLATE_SKILL_ROOT", "/opt/huangque/matrix-template-video/source/skill/script-to-matrix-video")),
        library_url=os.environ.get("PIXELLE_MATERIAL_LIBRARY_URL", "http://127.0.0.1:8111"),
        library_token=os.environ.get("PIXELLE_MATERIAL_LIBRARY_TOKEN", ""),
        python=os.environ.get("MATRIX_TEMPLATE_PYTHON", sys.executable),
        retention_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS
        )),
        delivery_grace_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS", DEFAULT_DELIVERY_GRACE_SECONDS
        )),
        cleanup_interval_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS", DEFAULT_CLEANUP_INTERVAL_SECONDS
        )),
        cleanup_batch_size=int(os.environ.get(
            "MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE", DEFAULT_CLEANUP_BATCH_SIZE
        )),
        disk_high_water_percent=float(os.environ.get(
            "MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT", DEFAULT_DISK_HIGH_WATER_PERCENT
        )),
    )
    server = build_server(
        args.host, args.port, service,
        os.environ.get("MATRIX_TEMPLATE_API_TOKEN", ""),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.shutdown()


if __name__ == "__main__":
    main()
