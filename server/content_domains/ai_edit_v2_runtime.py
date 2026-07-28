"""Stable runtime stage names and dependency lookup for AI Edit V2."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
from contextlib import closing
from typing import Any, Callable


STABLE_STAGE_SEQUENCE = (
    "normalizing",
    "transcribing",
    "aligning",
    "directing",
    "resolving_materials",
    "generating_media",
    "rendering",
    "postprocessing",
)

STATE_TO_STAGE = {
    "normalizing": "normalizing",
    "transcribing": "transcribing",
    "aligning_transcript": "aligning",
    "directing": "directing",
    "resolving_assets": "resolving_materials",
    "generating_assets": "generating_media",
    "rendering": "rendering",
    "assembling": "postprocessing",
}

STAGE_TO_NEXT_STATE = {
    "normalizing": "transcribing",
    "transcribing": "aligning_transcript",
    "aligning": "directing",
    "directing": "resolving_assets",
    "resolving_materials": "generating_assets",
    "generating_media": "designing_audio",
    "rendering": "assembling",
    "postprocessing": "quality_check",
}

PROVIDER_STAGES = frozenset(
    {"transcribing", "directing", "resolving_materials", "generating_media", "rendering"}
)
ARTIFACT_REQUIRED_STAGES = frozenset({"normalizing", "postprocessing"})

# Phase A workers may still inject state-specific handlers.  Task 7's run_job consumes
# the explicit dependency bundle and leaves this backwards-compatible registry intact.
STAGE_HANDLERS: dict[str, Callable[..., Any]] = {}
REQUIRED_STAGES = frozenset(
    {
        "normalizing",
        "transcribing",
        "aligning_transcript",
        "directing",
        "resolving_assets",
        "routing_render",
        "rendering",
        "quality_check",
        "settling",
        "storing",
    }
)


def public_state(state: str) -> str:
    aliases = {
        "aligning_transcript": "aligning",
        "resolving_assets": "resolving_materials",
        "generating_assets": "generating_media",
        "designing_audio": "generating_media",
        "routing_render": "generating_media",
        "assembling": "postprocessing",
        "quality_check": "quality_checking",
    }
    return aliases.get(state, state)


def dependency_callable(dependencies: Any, group: str, stage: str) -> Callable[..., Any] | None:
    if isinstance(dependencies, dict):
        values = dependencies.get(group) or {}
        if isinstance(values, dict) and callable(values.get(stage)):
            return values[stage]
        direct = dependencies.get(stage if group == "handlers" else f"reconcile_{stage}")
        return direct if callable(direct) else None
    values = getattr(dependencies, group, None)
    if isinstance(values, dict) and callable(values.get(stage)):
        return values[stage]
    direct = getattr(
        dependencies, stage if group == "handlers" else f"reconcile_{stage}", None
    )
    return direct if callable(direct) else None


def option(dependencies: Any, name: str, default: Any = None) -> Any:
    if isinstance(dependencies, dict):
        return dependencies.get(name, default)
    return getattr(dependencies, name, default)


def extract_artifacts(value: Any) -> list[dict[str, Any]]:
    """Extract artifact records at any nested ``artifact(s)`` boundary."""

    _valid, found = _scan_artifact_boundaries(value)
    return found


def _scan_artifact_boundaries(value: Any) -> tuple[bool, list[dict[str, Any]]]:
    found: list[dict[str, Any]] = []
    if type(value) is dict:
        for key, child in value.items():
            if key == "artifact":
                if type(child) is not dict:
                    return False, found
                found.append(child)
            elif key == "artifacts":
                if type(child) is not list or not all(type(item) is dict for item in child):
                    return False, found
                found.extend(child)
            valid, nested = _scan_artifact_boundaries(child)
            if not valid:
                return False, found
            found.extend(nested)
    elif type(value) is list:
        for child in value:
            valid, nested = _scan_artifact_boundaries(child)
            if not valid:
                return False, found
            found.extend(nested)
    return True, found


def _json_value_valid(value: Any) -> bool:
    """Accept only values whose type and value survive checkpoint JSON unchanged."""
    if type(value) is dict:
        return all(type(key) is str and _json_value_valid(child) for key, child in value.items())
    if type(value) is list:
        return all(_json_value_valid(child) for child in value)
    if value is None or type(value) in {str, int, bool}:
        return True
    return type(value) is float and math.isfinite(value)


def validate_stage_output(
    stage: str, output: Any, verifier: Callable[[str, dict[str, Any]], bool] | None
) -> tuple[bool, str | None]:
    if type(output) is not dict or not _json_value_valid(output):
        return False, "stage_output_invalid"
    if not _semantic_output_valid(stage, output):
        return False, "stage_output_schema_invalid"
    boundaries_valid, artifacts = _scan_artifact_boundaries(output)
    if not boundaries_valid:
        return False, "stage_artifact_metadata_invalid"
    if stage in ARTIFACT_REQUIRED_STAGES and not artifacts:
        return False, "stage_artifact_missing"
    for artifact in artifacts:
        if (
            not isinstance(artifact.get("cos_key"), str)
            or not artifact["cos_key"]
            or not isinstance(artifact.get("etag"), str)
            or not artifact["etag"]
            or isinstance(artifact.get("size_bytes"), bool)
            or not isinstance(artifact.get("size_bytes"), int)
            or artifact["size_bytes"] <= 0
        ):
            return False, "stage_artifact_metadata_invalid"
        if verifier is None or verifier(stage, artifact) is not True:
            return False, "stage_artifact_invalid"
    return True, None


def _int(value: Any, *, positive: bool = False) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (value > 0 if positive else value >= 0)


def _timeline(value: Any) -> bool:
    if not isinstance(value, dict) or not _int(value.get("duration_ms"), positive=True):
        return False
    duration_ms = value["duration_ms"]
    return (
        isinstance(value.get("text"), str) and bool(value["text"])
        and _timed_items(value.get("words"), duration_ms)
        and _timed_items(value.get("sentences"), duration_ms)
        and value.get("alignment_status") == "aligned"
    )


def _timed_items(value: Any, duration_ms: int) -> bool:
    if not isinstance(value, list) or not value: return False
    previous_start = previous_end = -1
    for item in value:
        if (not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"]
                or not _int(item.get("start_ms")) or not _int(item.get("end_ms"), positive=True)
                or item["start_ms"] >= item["end_ms"] or item["end_ms"] > duration_ms
                or item["start_ms"] < previous_start or item["end_ms"] < previous_end):
            return False
        previous_start, previous_end = item["start_ms"], item["end_ms"]
    return True


def _normalized_media(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("cos_key"), str)
            and bool(value["cos_key"]) and value.get("media_type") in {"video", "audio"}
            and isinstance(value.get("metadata"), dict)
            and _int(value["metadata"].get("duration_ms"), positive=True))


def _material(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    allowed = {
        "asset_id", "cos_key", "kind", "width", "height", "content_type",
        "mime_type", "size_bytes", "etag", "source", "required",
    }
    asset_id = value.get("asset_id")
    if (set(value) - allowed
            or not ((isinstance(asset_id, str) and bool(asset_id)) or _int(asset_id, positive=True))
            or not isinstance(value.get("cos_key"), str) or not value["cos_key"]
            or value.get("kind") not in {"video", "image", "audio"}):
        return False
    if value.get("source") not in {
        "current_upload", "user_history", "platform_public", "gpt_image"
    } or not isinstance(value.get("required"), bool):
        return False
    for field in ("width", "height", "size_bytes"):
        if field in value and not _int(value[field], positive=True):
            return False
    for field in ("content_type", "mime_type", "etag"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            return False
    return True


def _resolved_plan(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        from .ai_edit_v2_schema import EDIT_PLAN_FIELDS, validate_edit_plan
        plan = {field: value[field] for field in EDIT_PLAN_FIELDS}
        validate_edit_plan(plan)
    except (KeyError, TypeError, ValueError):
        return False
    required = set(EDIT_PLAN_FIELDS) | {
        "materials", "material_resolution_status", "text_timeline"
    }
    allowed = required | {"primary_media", "primary_video", "mastered_audio"}
    if set(value) - allowed or not required.issubset(value):
        return False
    materials = value.get("materials")
    if not isinstance(materials, dict) or not all(
        isinstance(slot_id, str) and bool(slot_id) and _material(material)
        for slot_id, material in materials.items()
    ):
        return False
    slots = {
        slot_id
        for scene in value["scenes"]
        for slot_id in scene["material_slots"]
    }
    if set(materials) != slots:
        return False
    if value.get("material_resolution_status") not in {
        "resolved", "image_generation_degraded"
    }:
        return False
    primary = value.get("primary_media") or value.get("primary_video")
    if not _timeline(value.get("text_timeline")) or not _normalized_media(primary):
        return False
    if "mastered_audio" in value:
        mastered = value["mastered_audio"]
        if (not isinstance(mastered, dict) or mastered.get("source") != "mix_audio"
                or not isinstance(mastered.get("cos_key"), str) or not mastered["cos_key"]
                or not isinstance(mastered.get("etag"), str) or not mastered["etag"]
                or not _int(mastered.get("size_bytes"), positive=True)):
            return False
    return True


def _degradations(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _audio_request(value: Any, kind: str) -> bool:
    if not isinstance(value, dict):
        return False
    if kind == "bgm":
        return (set(value) == {"prompt", "duration_ms", "force_instrumental", "duck_under_speech"}
                and isinstance(value.get("prompt"), str) and bool(value["prompt"])
                and _int(value.get("duration_ms"), positive=True)
                and value.get("force_instrumental") is True
                and value.get("duck_under_speech") is True)
    return (set(value) == {"kind", "prompt", "at_ms", "duration_ms", "required"}
            and value.get("kind") in {"camera_cut", "semantic_turn", "emphasis"}
            and isinstance(value.get("prompt"), str) and bool(value["prompt"])
            and _int(value.get("at_ms")) and _int(value.get("duration_ms"), positive=True)
            and isinstance(value.get("required"), bool))


def _audio_plan(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"bgm", "sfx", "degradations"}
            and (value["bgm"] is None or _audio_request(value["bgm"], "bgm"))
            and isinstance(value["sfx"], list)
            and all(_audio_request(item, "sfx") for item in value["sfx"])
            and _degradations(value["degradations"]))


def _generated_audio_item(value: Any, kind: str) -> bool:
    if not isinstance(value, dict):
        return False
    request_fields = (
        {"prompt", "duration_ms", "force_instrumental", "duck_under_speech"}
        if kind == "bgm" else {"kind", "prompt", "at_ms", "duration_ms", "required"}
    )
    asset_fields = {"cos_key", "content_type", "size_bytes", "etag"}
    optional_fields = {"song_id", "cost"}
    if not request_fields | asset_fields <= set(value) or set(value) - request_fields - asset_fields - optional_fields:
        return False
    request = {field: value[field] for field in request_fields}
    if not _audio_request(request, kind):
        return False
    if (not isinstance(value.get("cos_key"), str) or not value["cos_key"]
            or not isinstance(value.get("content_type"), str)
            or not value["content_type"].startswith("audio/")
            or not _int(value.get("size_bytes"), positive=True)
            or not isinstance(value.get("etag"), str) or not value["etag"]):
        return False
    if "song_id" in value and not isinstance(value["song_id"], str):
        return False
    if "cost" in value:
        cost = value["cost"]
        if (not isinstance(cost, dict) or set(cost) != {"status", "unit", "value", "source"}
                or cost.get("status") not in {"reported", "unknown"}
                or not isinstance(cost.get("unit"), str) or not cost["unit"]
                or not (cost.get("value") is None or _int(cost["value"]))
                or not isinstance(cost.get("source"), str) or not cost["source"]):
            return False
    return True


def _generated_audio(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"bgm", "sfx", "degradations"}
            and (value["bgm"] is None or _generated_audio_item(value["bgm"], "bgm"))
            and isinstance(value["sfx"], list)
            and all(_generated_audio_item(item, "sfx") for item in value["sfx"])
            and _degradations(value["degradations"]))


def _semantic_output_valid(stage: str, output: dict[str, Any]) -> bool:
    """Fail closed at checkpoint boundaries; artifacts alone are never stage output."""
    if stage == "normalizing":
        return _normalized_media(output.get("normalized_media"))
    if stage == "transcribing":
        value = output.get("asr_result")
        return (isinstance(value, dict) and isinstance(value.get("provider_task_id"), str)
                and bool(value["provider_task_id"]) and _int(value.get("duration_ms"), positive=True)
                and _timed_items(value.get("words"), value["duration_ms"])
                and _timed_items(value.get("sentences"), value["duration_ms"]))
    if stage == "aligning":
        return _timeline(output.get("text_timeline"))
    if stage == "directing":
        value = output.get("edit_plan")
        if not isinstance(value, dict): return False
        try:
            from .ai_edit_v2_schema import validate_edit_plan
            validate_edit_plan(value)
            return True
        except (TypeError, ValueError): return False
    if stage == "resolving_materials":
        return _resolved_plan(output.get("resolved_plan"))
    if stage == "generating_media":
        return (_resolved_plan(output.get("resolved_plan"))
                and _audio_plan(output.get("audio_plan"))
                and _generated_audio(output.get("generated_audio")))
    if stage == "rendering":
        return (isinstance(output.get("provider_task_id"), str) and bool(output["provider_task_id"])
                and output.get("provider_status") == "succeeded"
                and isinstance(output.get("render_url"), str)
                and output["render_url"].startswith("https://"))
    if stage == "postprocessing":
        return isinstance(output.get("artifact"), dict) and output.get("output_available") is True
    return False


class _MaterialRepositories:
    """Concrete repository boundary used by Task 4's resolver."""
    def __init__(self, db_path: str, owner: str) -> None:
        from . import ai_edit_v2_store as store
        self.db_path = db_path
        self.store = store
        self.owner = owner
        self.records: list[dict[str, Any]] = []

    def owner_for_job(self, _job_id: str) -> str:
        return self.owner

    def _rows(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with closing(self.store.open_store(self.db_path)) as conn:
            if job_id:
                rows = conn.execute("""SELECT m.*,jm.job_id,jm.purpose AS bound_purpose
                    FROM edit_v2_materials m JOIN edit_v2_job_materials jm ON jm.material_id=m.id
                    WHERE jm.job_id=? AND m.owner=? AND m.status='ready'""", (job_id, self.owner)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM edit_v2_materials WHERE owner=? AND status='ready'", (self.owner,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _asset(row: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
        return {"asset_id": str(row["id"]), "cos_key": row["cos_key"], "kind": row["kind"],
                "width": row.get("width"), "height": row.get("height"), "size_bytes": row.get("size_bytes"),
                "etag": row.get("etag"), "owner": row["owner"], "job_id": row.get("job_id"),
                "required": required, "relevant": True, "score": 1.0}

    def required_materials(self, job_id: str) -> list[dict[str, Any]]:
        return [self._asset(row, required=True) for row in self._rows(job_id)
                if row.get("bound_purpose") == "required_material"]

    def search(self, source: str, job_id: str, _slot: dict[str, Any]) -> list[dict[str, Any]]:
        if source == "current_upload":
            return [self._asset(row) for row in self._rows(job_id) if row.get("bound_purpose") != "main_input"]
        if source == "user_history":
            return [self._asset(row) for row in self._rows() if row.get("source") != "platform_public"]
        if source == "platform_public":
            return []
        return []

    def save_resolution_records(self, _job_id: str, records: list[dict[str, Any]], **_kw: Any) -> None:
        self.records = records


class ProductionServices:
    """Eight concrete production stages composed from the audited Task 2-6 APIs."""
    def __init__(self, db_path: str, *, cos_api: Any = None, runner: Callable[..., Any] = subprocess.run,
                 dashscope_http: Callable[..., Any] | None = None,
                 shotstack_http: Callable[..., Any] | None = None,
                 openai_http: Callable[..., Any] | None = None, openai_downloader: Callable[..., Any] | None = None,
                 elevenlabs_http: Callable[..., Any] | None = None,
                 downloader: Callable[[str], bytes] | None = None,
                 repair_handler: Callable[..., Any] | None = None,
                 repair_reconciler: Callable[..., Any] | None = None,
                 quality_analyzer: Callable[..., Any] | None = None,
                 quality_binary_finder: Callable[[str], str | None] | None = None) -> None:
        from . import ai_edit_v2_cos
        self.db_path = db_path
        self.cos = cos_api or ai_edit_v2_cos
        self.runner = runner
        self.dashscope_http = dashscope_http
        self.shotstack_http = shotstack_http
        self.openai_http, self.openai_downloader = openai_http, openai_downloader
        self.elevenlabs_http = elevenlabs_http
        self.downloader = downloader or self._download
        self.repair_handler = repair_handler or _load_production_injection(
            "AI_EDIT_V2_REPAIR_HANDLER_FACTORY", db_path
        )
        self.repair_reconciler_handler = repair_reconciler or _load_production_injection(
            "AI_EDIT_V2_REPAIR_RECONCILER_FACTORY", db_path
        )
        from .ai_edit_v2_quality import LocalQualityRunner
        quality_analyzer = quality_analyzer or _load_production_injection(
            "AI_EDIT_V2_QUALITY_ANALYZER_FACTORY", db_path
        )
        self.quality_runner = LocalQualityRunner(
            runner,
            analyzer=quality_analyzer,
            binary_finder=quality_binary_finder,
        )

    def readiness_errors(self) -> list[str]:
        errors = []
        configured = lambda name: bool(
            (value := str(os.environ.get(name) or "").strip())
            and not value.lower().startswith("replace-with-")
        )
        if self.dashscope_http is None and not configured("DASHSCOPE_API_KEY"):
            errors.append("DASHSCOPE_API_KEY")
        if self.shotstack_http is None and not configured("SHOTSTACK_API_KEY"):
            errors.append("SHOTSTACK_API_KEY")
        if not configured("OPENAI_API_KEY"):
            errors.append("OPENAI_API_KEY")
        if os.environ.get("AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED") != "1":
            errors.append("AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED")
        if not configured("ELEVENLABS_API_KEY"):
            errors.append("ELEVENLABS_API_KEY")
        callback = str(os.environ.get("AI_EDIT_V2_SHOTSTACK_CALLBACK_URL") or "").strip()
        parsed = urllib.parse.urlsplit(callback)
        if (not callback or "replace-with-" in callback.lower() or parsed.scheme != "https"
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            errors.append("AI_EDIT_V2_SHOTSTACK_CALLBACK_URL")
        if not configured("AI_EDIT_V2_WEBHOOK_SECRET"):
            errors.append("AI_EDIT_V2_WEBHOOK_SECRET")
        enabled = getattr(self.cos, "enabled", None)
        if callable(enabled) and not enabled():
            errors.append("AI_EDIT_V2_COS")
        errors.extend(f"AI_EDIT_V2_QUALITY_{name.upper()}" for name in self.quality_runner.readiness_errors())
        if self.repair_handler is None or self.repair_reconciler_handler is None:
            errors.append("AI_EDIT_V2_REPAIR_PROVIDER")
        return errors

    def resolve_quality_output(self, job: dict[str, Any], outputs: dict[str, Any]) -> str:
        post = outputs.get("postprocessing") or {}
        artifact = post.get("artifact") if isinstance(post, dict) else None
        key = artifact.get("cos_key") if isinstance(artifact, dict) else None
        if not isinstance(key, str) or not key:
            raise RuntimeError("quality_output_missing")
        directory = os.path.join(tempfile.gettempdir(), "ai-edit-v2-quality", str(job["id"]))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "final.mp4")
        self.cos.download_file(key, path)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError("quality_output_missing")
        return path

    def actual_cost(self, job: dict[str, Any], _outputs: dict[str, Any]) -> int:
        from . import ai_edit_v2_billing as billing
        from . import ai_edit_v2_store as store
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT amount FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
                (job["id"],),
            ).fetchone()
        if row is None:
            raise RuntimeError("billing_not_found")
        return billing.aggregate_provider_cost(
            job["id"], held_points=int(row["amount"]), db_path=self.db_path
        )

    def _record_usage(self, job_id: str, operation_key: str, provider: str,
                      capability: str, request_id: str, cost_units: int | None) -> None:
        from . import ai_edit_v2_billing as billing
        from . import ai_edit_v2_store as store
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                """SELECT q.price_version FROM edit_v2_jobs j
                   JOIN edit_v2_quotes q ON q.id=j.quote_id WHERE j.id=?""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("quote_not_found")
        billing.record_provider_usage(
            job_id, operation_key, provider, capability, request_id,
            cost_units=cost_units, price_version=row["price_version"], db_path=self.db_path,
        )

    def repair_layer(self, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if self.repair_handler is None:
            raise RuntimeError("repair_provider_not_configured")
        result = self.repair_handler(job, context)
        self._record_repair_usage(job, context, result)
        return result

    def repair_reconciler(self, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if self.repair_reconciler_handler is None:
            raise RuntimeError("repair_provider_not_configured")
        result = self.repair_reconciler_handler(job, context)
        self._record_repair_usage(job, context, result)
        return result

    def _record_repair_usage(
        self, job: dict[str, Any], context: dict[str, Any], result: Any
    ) -> None:
        """Freeze one repair charge across submit, restart, and reconcile replay."""

        idempotency_key = str(context.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise RuntimeError("repair_idempotency_key_missing")
        if isinstance(result, dict):
            provider = str(result.get("provider") or "repair").strip() or "repair"
            request_id = str(
                result.get("request_id") or result.get("provider_task_id")
                or context.get("provider_task_id") or idempotency_key
            ).strip()
            cost_units = result.get("cost_units")
        else:
            provider = str(getattr(result, "provider", None) or "repair").strip()
            request_id = str(
                getattr(result, "request_id", None)
                or context.get("provider_task_id") or idempotency_key
            ).strip()
            cost_units = getattr(result, "cost_units", None)
        operation_key = f"repair:{idempotency_key}"
        self._record_usage(
            str(job["id"]), operation_key, provider, "repair", request_id,
            cost_units if isinstance(cost_units, int) and not isinstance(cost_units, bool) else None,
        )

    def _draft(self, stage_input: dict[str, Any]) -> dict[str, Any]:
        payload = stage_input.get("payload") or {}
        return payload.get("draft") or payload

    def _artifact(self, cos_key: str) -> dict[str, Any]:
        head = self.cos.head_object(cos_key)
        return {"cos_key": cos_key, "etag": str(head["etag"]).strip('"'),
                "size_bytes": int(head["content_length"])}

    def normalizing(self, job: dict[str, Any], _context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_media import prepare_cos_media
        draft = self._draft(stage_input)
        source = draft.get("main_input") or {}
        cos_key = source.get("cos_key")
        if not cos_key:
            from . import ai_edit_v2_store as store
            with closing(store.open_store(self.db_path)) as conn:
                row = conn.execute("""SELECT m.cos_key,m.kind FROM edit_v2_materials m
                    JOIN edit_v2_job_materials jm ON jm.material_id=m.id
                    WHERE jm.job_id=? AND jm.purpose='main_input' AND m.status='ready'""", (job["id"],)).fetchone()
            if row is not None:
                cos_key, source = row["cos_key"], {**source, "kind": row["kind"]}
        if not isinstance(cos_key, str) or not cos_key:
            raise RuntimeError("main_input_cos_key_missing")
        media_type = "audio" if source.get("kind") == "audio" else "video"
        owner_hash = hashlib.sha256(str(job["owner"]).encode()).hexdigest()[:16]
        suffix = "m4a" if media_type == "audio" else "mp4"
        target = f"ai-edit-v2/{owner_hash}/{job['id']}/normalized/main.{suffix}"
        value = prepare_cos_media(cos_key, target, media_type, cos_api=self.cos, runner=self.runner)
        value["media_type"] = media_type
        return {"normalized_media": value, "artifact": self._artifact(value["cos_key"])}

    def transcribing(self, _job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_asr import transcribe
        from .ai_edit_v2_providers.dashscope import DashScopeClient
        media = stage_input["previous"]["normalized_media"]
        client = DashScopeClient(http_request=self.dashscope_http) if self.dashscope_http else DashScopeClient()
        signed = self.cos.presign_get(media["cos_key"])
        result = transcribe(signed, client, context["deadline_at"], provider_task_id=context.get("provider_task_id"),
                            reference=media["cos_key"], save_provider_task_id=context["save_provider_task_id"],
                            now_fn=time.time, sleep_fn=time.sleep, poll_guard=context["assert_active"])
        self._record_usage(_job["id"], f"asr:{result['provider_task_id']}", "dashscope", "asr",
                           result["provider_task_id"], None)
        return {"normalized_media": media, "asr_result": result}

    def aligning(self, _job: dict[str, Any], _context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_alignment import build_text_timeline
        previous, draft = stage_input["previous"], self._draft(stage_input)
        if previous["normalized_media"]["media_type"] == "audio":
            source_type = "audio_only"
        elif draft.get("input_mode") == "platform_video" or (
            draft.get("input_mode") is None
            and draft.get("creation_mode") == "platform_template"
            and isinstance(draft.get("original_text"), str)
        ):
            source_type = "platform_video"
        else:
            source_type = "external_video"
        original_text = draft.get("original_text") if source_type == "platform_video" else None
        timeline = build_text_timeline(source_type, original_text, previous["asr_result"])
        timeline.update({"alignment_status": "aligned", "duration_ms": previous["asr_result"]["duration_ms"]})
        return {"normalized_media": previous["normalized_media"], "text_timeline": timeline}

    def directing(self, _job: dict[str, Any], _context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_director import generate_edit_plan
        from .ai_edit_v2_providers.dashscope import DashScopeClient
        previous, draft = stage_input["previous"], self._draft(stage_input)
        client = DashScopeClient(http_request=self.dashscope_http) if self.dashscope_http else DashScopeClient()
        director_context = {"creation_mode": draft["creation_mode"], "text_timeline": previous["text_timeline"],
                            "aspect_ratio": draft["aspect_ratio"], "target_duration_ms": (draft.get("target_duration_ms") or previous["text_timeline"]["duration_ms"])}
        if draft["creation_mode"] == "platform_template":
            director_context.update({"template_id": draft["template_id"], "template_version": draft["template_version"]})
        else:
            director_context["style_text"] = draft.get("style_text") or draft.get("brief") or "clean editorial"
        plan = generate_edit_plan(director_context, client)
        self._record_usage(_job["id"], f"director:{_context['attempt_id']}", "dashscope",
                           "director", str(_context["attempt_id"]), None)
        return {"normalized_media": previous["normalized_media"], "text_timeline": previous["text_timeline"], "edit_plan": plan}

    def resolving_materials(self, job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_materials import resolve_materials
        from .ai_edit_v2_providers.openai_image import OpenAIImageProvider
        previous = stage_input["previous"]
        provider = OpenAIImageProvider(owner=job["owner"], job_id=job["id"], cos_api=self.cos,
                                       db_path=self.db_path, worker_id=str(context["attempt_id"]),
                                       http_request=self.openai_http, downloader=self.openai_downloader)
        edit_plan = previous["edit_plan"]
        primary_media = previous["normalized_media"]
        if primary_media["media_type"] == "audio" and not any(
            scene.get("material_slots") for scene in edit_plan.get("scenes") or []
        ):
            edit_plan = json.loads(json.dumps(edit_plan))
            edit_plan["scenes"][0]["material_slots"] = ["slot_audio_visual"]
        plan = resolve_materials(job["id"], edit_plan, _MaterialRepositories(self.db_path, job["owner"]), provider)
        plan.update({"text_timeline": previous["text_timeline"], "primary_media": primary_media})
        if primary_media["media_type"] == "video":
            plan["primary_video"] = primary_media
        for material in plan["materials"].values():
            if material.get("source") == "gpt_image":
                request_id = str(material.get("asset_id") or material["cos_key"])
                self._record_usage(job["id"], f"image:{request_id}", "openai",
                                   "image_generation", request_id, None)
        return {"resolved_plan": plan}

    def generating_media(self, job: dict[str, Any], _context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_audio import build_audio_plan, generate_audio_assets, mix_audio
        from .ai_edit_v2_providers.elevenlabs import ElevenLabsProvider
        plan = stage_input["previous"]["resolved_plan"]
        audio_plan = build_audio_plan(plan, plan["text_timeline"])
        provider = ElevenLabsProvider(owner=job["owner"], job_id=job["id"], cos_api=self.cos, db_path=self.db_path,
                                      http_request=self.elevenlabs_http)
        generated = generate_audio_assets(job["id"], audio_plan, provider)
        for item in ([generated.get("bgm")] if generated.get("bgm") else []) + list(generated.get("sfx") or []):
            provider_result = item.get("provider_result")
            if provider_result is not None:
                capability = str(provider_result.capability)
                self._record_usage(
                    job["id"], f"{capability}:{provider_result.request_id}",
                    str(provider_result.provider), capability, str(provider_result.request_id),
                    provider_result.cost_units if provider_result.cost_units > 0 else None,
                )
            item.pop("provider_result", None)
        artifacts: list[dict[str, Any]] = []
        owner_hash = hashlib.sha256(str(job["owner"]).encode()).hexdigest()[:16]
        master_key = f"ai-edit-v2/{owner_hash}/{job['id']}/audio/master.m4a"
        with tempfile.TemporaryDirectory(prefix="ai-edit-v2-audio-") as directory:
            voice = os.path.join(directory, "voice-input")
            primary_media = plan.get("primary_media") or plan.get("primary_video")
            self.cos.download_file(primary_media["cos_key"], voice)
            bgm_path = None
            if generated.get("bgm"):
                bgm_path = os.path.join(directory, "bgm.mp3")
                self.cos.download_file(generated["bgm"]["cos_key"], bgm_path)
            sfx = []
            for index, cue in enumerate(generated.get("sfx") or []):
                path = os.path.join(directory, f"sfx-{index}.mp3")
                self.cos.download_file(cue["cos_key"], path)
                sfx.append({**cue, "path": path})
            output = os.path.join(directory, "master.m4a")
            mix_audio(voice, voice, bgm_path, sfx, output, self.runner)
            self.cos.put_file(output, master_key, "audio/mp4", private=True)
        master = {"source": "mix_audio", "cos_key": master_key, **self._artifact(master_key)}
        plan = dict(plan); plan["mastered_audio"] = master
        artifacts.append(self._artifact(master_key))
        return {"resolved_plan": plan, "audio_plan": audio_plan, "generated_audio": generated, "artifacts": artifacts}

    def rendering(self, job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        from .ai_edit_v2_schema import BUNDLED_NOTO_SANS_SC_URL
        from .ai_edit_v2_shotstack import ShotstackClient, build_render_graph
        plan = stage_input["previous"]["resolved_plan"]
        keys = [v["cos_key"] for v in plan["materials"].values()]
        if plan.get("primary_video"):
            keys.append(plan["primary_video"]["cos_key"])
        if plan.get("mastered_audio"):
            keys.append(plan["mastered_audio"]["cos_key"])
        graph = build_render_graph(plan, {key: self.cos.presign_get(key) for key in keys}, BUNDLED_NOTO_SANS_SC_URL)
        client = ShotstackClient(job_id=job["id"], attempt_id=context["attempt_id"], db_path=self.db_path,
                                 http_request=self.shotstack_http) if self.shotstack_http else ShotstackClient(job_id=job["id"], attempt_id=context["attempt_id"], db_path=self.db_path)
        saved = context.get("provider_task_id")
        result = client.reconcile(provider_task_id=saved) if saved else client.submit(graph, f"{job['id']}:render")
        task_id = result.payload["provider_task_id"]
        if not saved:
            context["save_provider_task_id"](task_id)
        while result.payload["status"] == "pending":
            context["assert_active"]()
            if time.time() >= context["deadline_at"]:
                raise RuntimeError("render_timeout")
            time.sleep(min(1.0, max(0.0, context["deadline_at"] - time.time())))
            result = client.reconcile(provider_task_id=task_id)
        self._record_usage(job["id"], f"render:{task_id}", "shotstack", "render",
                           str(result.request_id), result.cost_units if result.cost_units > 0 else None)
        return {"provider_task_id": task_id, "provider_status": result.payload["status"], "render_url": result.payload["output_url"]}

    def postprocessing(self, job: dict[str, Any], _context: dict[str, Any], stage_input: dict[str, Any]) -> dict[str, Any]:
        owner_hash = hashlib.sha256(str(job["owner"]).encode()).hexdigest()[:16]
        key = f"ai-edit-v2/{owner_hash}/{job['id']}/output/final.mp4"
        self.cos.put_bytes(self.downloader(stage_input["previous"]["render_url"]), key, "video/mp4", private=True)
        return {"artifact": self._artifact(key), "output_available": True}

    @staticmethod
    def _download(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()


def production_dependencies(db_path: str, *, services: Any = None) -> dict[str, Any]:
    """Build concrete stages; configuration is checked before the worker claims work."""
    service = services or ProductionServices(db_path)

    def handler(stage: str) -> Callable[..., Any]:
        def invoke(job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> Any:
            context["assert_active"]()
            return getattr(service, stage)(job, context, stage_input)
        return invoke

    def verify(_stage: str, artifact: dict[str, Any]) -> bool:
        try:
            head = service.cos.head_object(artifact["cos_key"])
            return (
                int(head.get("content_length", -1)) == artifact["size_bytes"]
                and str(head.get("etag", "")).strip('"') == artifact["etag"].strip('"')
            )
        except Exception:
            return False

    return {
        "production": True,
        "handlers": {stage: handler(stage) for stage in STABLE_STAGE_SEQUENCE},
        "reconcilers": {stage: handler(stage) for stage in PROVIDER_STAGES},
        "verify_artifact": verify,
        "quality_runner": service.quality_runner,
        "quality_output_path": service.resolve_quality_output,
        "actual_cost": service.actual_cost,
        "repair_layer": service.repair_layer,
        "repair_reconciler": service.repair_reconciler,
        "db_path": db_path,
        "services": service,
        "readiness_errors": service.readiness_errors,
    }


def _load_production_injection(name: str, db_path: str) -> Any:
    target = str(os.environ.get(name) or "").strip()
    if not target:
        return None
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError(f"{name}_invalid")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise RuntimeError(f"{name}_invalid")
    value = factory(db_path=db_path)
    if not callable(value):
        raise RuntimeError(f"{name}_invalid")
    return value


def production_readiness(dependencies: Any) -> list[str]:
    checker = option(dependencies, "readiness_errors")
    try:
        errors = checker() if callable(checker) else ["production_services"]
    except Exception:
        errors = ["production_services"]
    return sorted({str(error) for error in errors if error})


def assert_production_ready(dependencies: Any) -> None:
    errors = production_readiness(dependencies)
    if errors:
        raise RuntimeError("ai_edit_v2_not_ready:" + ",".join(sorted(errors)))


def runtime_ready() -> bool:
    return REQUIRED_STAGES.issubset(STAGE_HANDLERS)
