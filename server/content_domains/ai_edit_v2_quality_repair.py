"""Concrete fail-closed quality analysis and targeted repair providers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import ai_edit_v2_store as store
from .ai_edit_v2_providers.base import ProviderError


_QWEN_PATH = "/services/aigc/multimodal-generation/generation"
_SHOTSTACK_REPAIR_CODES = frozenset({
    "caption_invalid", "caption_out_of_safe_area", "caption_tofu_detected",
    "caption_glyph_missing", "black_frames_detected", "blank_frames_detected",
})
_LOCAL_REPAIR_CODES = frozenset({
    "output_dimensions_invalid", "output_rotation_invalid",
    "output_duration_mismatch", "audio_clipping_detected",
})


def _configured(name: str) -> bool:
    value = str(os.environ.get(name) or "").strip()
    return bool(value and not value.lower().startswith("replace-with-"))


def _strict_object(raw: Any, code: str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "strict")
    value = json.loads(str(raw), parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(f"non-finite constant: {value}")
    ))
    if not isinstance(value, dict):
        raise ProviderError(code)
    return value


class DashScopeFinalMediaAnalyzer:
    """Analyze the actual final MP4 with Qwen-VL and FFmpeg evidence."""

    def __init__(
        self, *, cos_api: Any, video_url: Callable[[str], str],
        http_request: Callable[..., Any] | None = None,
        process_runner: Callable[..., Any] = subprocess.run,
        binary_finder: Callable[[str], str | None] | None = None,
    ) -> None:
        self.cos = cos_api
        self.video_url = video_url
        self.http_request = http_request or self._stdlib_request
        self.injected_http = http_request is not None
        self.process_runner = process_runner
        self.binary_finder = binary_finder or shutil.which

    def capabilities(self) -> dict[str, bool]:
        qwen = (
            (self.injected_http or _configured("DASHSCOPE_API_KEY"))
            and callable(self.video_url)
        )
        materials = qwen and callable(getattr(self.cos, "presign_get", None))
        ffmpeg = (
            self.binary_finder(self._binary("ffmpeg")) is not None
            and self.binary_finder(self._binary("ffprobe")) is not None
            and callable(getattr(self.cos, "download_file", None))
        )
        return {
            "captions_ocr": qwen,
            "glyphs": qwen,
            "materials": materials,
            "transcript_facts": qwen,
            "audio": ffmpeg,
        }

    @staticmethod
    def _binary(name: str) -> str:
        configured = str(os.environ.get(
            f"AI_EDIT_V2_QUALITY_{name.upper()}_BIN", ""
        ) or "").strip()
        return configured or name

    def __call__(self, check: str, *, path: str, expected: dict[str, Any]) -> dict[str, Any]:
        capability = {
            "captions": "captions_ocr", "materials": "materials",
            "transcript": "transcript_facts", "audio": "audio",
        }.get(check)
        if capability is None or self.capabilities().get(capability) is not True:
            raise ProviderError("final_media_analyzer_capability_unavailable")
        if check == "audio":
            return self._audio(path, expected)
        return self._qwen(check, path, expected)

    def _qwen(self, check: str, path: str, expected: dict[str, Any]) -> dict[str, Any]:
        video_url = self.video_url(path)
        if not isinstance(video_url, str) or not video_url.startswith("https://"):
            raise ProviderError("quality_video_url_invalid")
        content: list[dict[str, Any]] = [{"video": video_url}]
        if check == "materials":
            for item in self._material_references(expected):
                content.append({"text": f"Required asset reference {item['asset_id']}:"})
                content.append({item["kind"]: item["url"]})
        prompt = self._prompt(check, expected)
        content.append({"text": prompt})
        body = json.dumps({
            "model": os.environ.get("DASHSCOPE_QWEN_VL_MODEL", "qwen-vl-max-latest"),
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {"result_format": "message"},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        response = self.http_request(
            "POST", self._base_url() + _QWEN_PATH, self._headers(), body, 120,
        )
        return self._parse_qwen(check, response)

    def _material_references(self, expected: dict[str, Any]) -> list[dict[str, str]]:
        required = {str(value) for value in expected.get("required_asset_ids") or []}
        materials = expected.get("materials") or {}
        values = materials.values() if isinstance(materials, dict) else (
            materials if isinstance(materials, list) else []
        )
        result = []
        for item in values:
            if not isinstance(item, dict) or str(item.get("asset_id")) not in required:
                continue
            key = item.get("cos_key")
            if not isinstance(key, str) or not key:
                raise ProviderError("quality_material_reference_missing")
            kind = item.get("kind")
            if kind not in {"image", "video"}:
                raise ProviderError("quality_material_reference_unsupported")
            url = self.cos.presign_get(key)
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ProviderError("quality_material_reference_invalid")
            result.append({"asset_id": str(item["asset_id"]), "kind": kind, "url": url})
        if required != {item["asset_id"] for item in result}:
            raise ProviderError("quality_material_reference_missing")
        return result

    @staticmethod
    def _prompt(check: str, expected: dict[str, Any]) -> str:
        safe_expected = expected
        if check == "materials":
            safe_expected = {"required_asset_ids": expected.get("required_asset_ids") or []}
        frozen = json.dumps(safe_expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        instructions = {
            "captions": (
                "Inspect only burned-in captions in the supplied final video. Compare them "
                "with the expected caption/text timeline. Return strict JSON with exactly "
                "safe_area:boolean,tofu_count:nonnegative integer,missing_glyphs:string array."
            ),
            "materials": (
                "The video is followed by reference images for required asset IDs. Determine "
                "which required assets visibly appear without guessing. Return strict JSON with "
                "exactly covered_asset_ids:string array."
            ),
            "transcript": (
                "Compare visible captions and claims in the final video with the expected text "
                "timeline. Do not infer missing facts. Return strict JSON with exactly "
                "source_matches:boolean,facts_match:boolean."
            ),
        }
        return instructions[check] + " Expected evidence: " + frozen

    @staticmethod
    def _parse_qwen(check: str, response: Any) -> dict[str, Any]:
        try:
            output = response["output"]
            choices = output["choices"]
            message = choices[0]["message"]
            content = message["content"]
            if isinstance(content, list):
                texts = [item.get("text") for item in content if isinstance(item, dict)]
                if len(texts) != 1:
                    raise ValueError
                content = texts[0]
            value = _strict_object(content, "quality_qwen_response_invalid")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("quality_qwen_response_invalid") from exc
        valid = False
        if check == "captions":
            valid = (
                set(value) == {"safe_area", "tofu_count", "missing_glyphs"}
                and isinstance(value["safe_area"], bool)
                and isinstance(value["tofu_count"], int)
                and not isinstance(value["tofu_count"], bool)
                and value["tofu_count"] >= 0
                and isinstance(value["missing_glyphs"], list)
                and all(isinstance(item, str) for item in value["missing_glyphs"])
            )
        elif check == "materials":
            valid = set(value) == {"covered_asset_ids"} and isinstance(
                value["covered_asset_ids"], list
            ) and all(isinstance(item, str) for item in value["covered_asset_ids"])
        elif check == "transcript":
            valid = set(value) == {"source_matches", "facts_match"} and all(
                isinstance(value[name], bool) for name in value
            )
        if not valid:
            raise ProviderError("quality_qwen_evidence_invalid")
        return value

    def _audio(self, path: str, expected: dict[str, Any]) -> dict[str, Any]:
        duration_result = self._run([
            self._binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", os.fspath(path),
        ])
        try:
            duration = float(self._stdout(duration_result).strip())
        except ValueError as exc:
            raise ProviderError("quality_audio_duration_invalid") from exc
        if duration <= 0:
            raise ProviderError("quality_audio_duration_invalid")
        measured = self._run([
            self._binary("ffmpeg"), "-hide_banner", "-nostdin", "-v", "info",
            "-i", os.fspath(path), "-vn", "-af",
            "silencedetect=n=-50dB:d=0.2,ebur128=peak=true", "-f", "null", "-",
        ])
        text = self._stderr(measured)
        silence = sum(float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", text))
        peaks = re.findall(r"Peak:\s*(-?[0-9.]+)\s*dBFS", text)
        if not peaks:
            raise ProviderError("quality_audio_measurement_invalid")
        sources = expected.get("quality_sources") or {}
        generated = sources.get("generated_audio") or {}
        dialogue = sources.get("primary_media")
        dialogue_loudness = self._source_loudness(dialogue)
        bgm = generated.get("bgm")
        sfx = generated.get("sfx") or []
        bgm_ratio = 200.0 if not bgm else dialogue_loudness - self._source_loudness(
            bgm, sidechain=dialogue
        )
        sfx_ratio = 200.0 if not sfx else min(
            dialogue_loudness - self._source_loudness(item) for item in sfx
        )
        return {
            "silence_ratio": min(1.0, silence / duration),
            "true_peak_dbfs": float(peaks[-1]),
            "dialogue_to_bgm_db": bgm_ratio,
            "dialogue_to_sfx_db": sfx_ratio,
        }

    def _source_loudness(self, source: Any, *, sidechain: Any = None) -> float:
        if not isinstance(source, dict) or not isinstance(source.get("cos_key"), str):
            raise ProviderError("quality_audio_source_missing")
        if not callable(getattr(self.cos, "download_file", None)):
            raise ProviderError("quality_audio_source_unavailable")
        with tempfile.TemporaryDirectory(prefix="ai-edit-v2-quality-audio-") as directory:
            path = os.path.join(directory, "source")
            self.cos.download_file(source["cos_key"], path)
            command = [
                self._binary("ffmpeg"), "-hide_banner", "-nostdin", "-v", "info",
                "-i", path,
            ]
            if sidechain is None:
                command.extend(["-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
            else:
                if not isinstance(sidechain, dict) or not isinstance(sidechain.get("cos_key"), str):
                    raise ProviderError("quality_audio_source_missing")
                voice = os.path.join(directory, "dialogue")
                self.cos.download_file(sidechain["cos_key"], voice)
                command.extend([
                    "-i", voice, "-filter_complex",
                    "[0:a][1:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[ducked];"
                    "[ducked]ebur128=peak=true[out]",
                    "-map", "[out]", "-f", "null", "-",
                ])
            result = self._run(command)
        values = re.findall(r"I:\s*(-?[0-9.]+)\s*LUFS", self._stderr(result))
        if not values:
            raise ProviderError("quality_audio_source_measurement_invalid")
        return float(values[-1])

    def _run(self, command: list[str]) -> Any:
        result = self.process_runner(
            command, check=False, timeout=600,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise ProviderError("quality_audio_analysis_failed")
        return result

    @staticmethod
    def _stdout(result: Any) -> str:
        value = getattr(result, "stdout", b"") or b""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _stderr(result: Any) -> str:
        value = getattr(result, "stderr", b"") or b""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _base_url() -> str:
        return os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/")

    def _headers(self) -> dict[str, str]:
        key = str(os.environ.get("DASHSCOPE_API_KEY") or "")
        if not key and not self.injected_http:
            raise ProviderError("dashscope_not_configured")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _stdlib_request(method: str, url: str, headers: dict[str, str],
                        body: bytes | None, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ProviderError("quality_qwen_response_invalid")
        return value


class ProductionRepairProvider:
    """Repair only enumerated technical layers; async rerenders are durable."""

    def __init__(self, *, db_path: str, cos_api: Any,
                 shotstack_http: Callable[..., Any] | None = None,
                 process_runner: Callable[..., Any] = subprocess.run,
                 downloader: Callable[[str], bytes] | None = None,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.db_path, self.cos = db_path, cos_api
        self.shotstack_http, self.process_runner = shotstack_http, process_runner
        self.downloader = downloader or self._download
        self.now_fn, self.sleep_fn = now_fn, sleep_fn

    def submit(self, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        codes = self._codes(context)
        if not codes.issubset(_LOCAL_REPAIR_CODES | _SHOTSTACK_REPAIR_CODES):
            raise ProviderError("repair_layer_unsupported")
        if not codes.intersection(_SHOTSTACK_REPAIR_CODES):
            return self._local(job, context, codes)
        from .ai_edit_v2_schema import BUNDLED_NOTO_SANS_SC_URL
        from .ai_edit_v2_shotstack import ShotstackClient, build_render_graph
        plan = self._resolved_plan(str(job["id"]))
        keys = [value["cos_key"] for value in (plan.get("materials") or {}).values()]
        for name in ("primary_video", "mastered_audio"):
            value = plan.get(name)
            if isinstance(value, dict) and isinstance(value.get("cos_key"), str):
                keys.append(value["cos_key"])
        graph = build_render_graph(
            plan, {key: self.cos.presign_get(key) for key in keys},
            BUNDLED_NOTO_SANS_SC_URL,
        )
        client = self._client(job, context)
        result = client.submit(graph, context["idempotency_key"])
        task_id = result.payload["provider_task_id"]
        context["save_provider_task_id"](task_id)
        return self._wait(job, context, client, result)

    def reconcile(self, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        task_id = context.get("provider_task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("repair_provider_task_id_missing")
        client = self._client(job, context)
        return self._wait(job, context, client, client.reconcile(provider_task_id=task_id))

    def _client(self, job: dict[str, Any], context: dict[str, Any]) -> Any:
        from .ai_edit_v2_shotstack import ShotstackClient
        kwargs = {
            "job_id": str(job["id"]), "attempt_id": int(context["attempt_id"]),
            "db_path": self.db_path,
        }
        if self.shotstack_http is not None:
            kwargs["http_request"] = self.shotstack_http
        return ShotstackClient(**kwargs)

    def _wait(self, job: dict[str, Any], context: dict[str, Any], client: Any,
              result: Any) -> dict[str, Any]:
        task_id = result.payload["provider_task_id"]
        while result.payload["status"] == "pending":
            context["assert_active"]()
            if self.now_fn() >= int(context["deadline_at"]):
                raise ProviderError("repair_budget_exceeded")
            self.sleep_fn(min(1.0, max(0.0, int(context["deadline_at"]) - self.now_fn())))
            result = client.reconcile(provider_task_id=task_id)
        if result.payload["status"] != "succeeded":
            raise ProviderError("repair_provider_failed")
        return self._store_output(job, result.payload["output_url"], {
            "provider": "shotstack", "provider_task_id": task_id,
            "request_id": str(result.request_id), "cost_units": result.cost_units,
        })

    def _local(self, job: dict[str, Any], context: dict[str, Any],
               codes: frozenset[str]) -> dict[str, Any]:
        source_key = self._postprocessing_key(str(job["id"]))
        plan = self._resolved_plan(str(job["id"]))
        directory = self._directory(str(job["id"]))
        source, output = os.path.join(directory, "source.mp4"), os.path.join(directory, "final.mp4")
        self.cos.download_file(source_key, source)
        command = [DashScopeFinalMediaAnalyzer._binary("ffmpeg"),
                   "-hide_banner", "-nostdin", "-y", "-i", source]
        video_filter = codes.intersection({"output_dimensions_invalid", "output_rotation_invalid"})
        if video_filter:
            width, height = (1920, 1080) if plan.get("aspect_ratio") == "16:9" else (1080, 1920)
            command.extend(["-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1", "-metadata:s:v:0", "rotate=0"])
        if "output_duration_mismatch" in codes:
            command.extend(["-t", f"{int(plan['target_duration_ms']) / 1000:g}"])
        if "audio_clipping_detected" in codes:
            command.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
        command.extend(["-c:v", "libx264" if video_filter else "copy",
                        "-c:a", "aac", "-movflags", "+faststart", output])
        result = self.process_runner(command, check=False, timeout=600,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if int(getattr(result, "returncode", 1)) != 0 or not os.path.isfile(output) or os.path.getsize(output) <= 0:
            raise ProviderError("repair_ffmpeg_failed")
        key = self._repair_key(job)
        self.cos.put_file(output, key, "video/mp4", private=True)
        return {"provider": "ffmpeg", "request_id": context["idempotency_key"],
                "cost_units": 0, "cos_key": key, "output_path": output}

    def _store_output(self, job: dict[str, Any], url: str,
                      details: dict[str, Any]) -> dict[str, Any]:
        data = self.downloader(url)
        if not isinstance(data, bytes) or not data:
            raise ProviderError("repair_output_invalid")
        key = self._repair_key(job)
        self.cos.put_bytes(data, key, "video/mp4", private=True)
        path = os.path.join(self._directory(str(job["id"])), "final.mp4")
        Path(path).write_bytes(data)
        return {**details, "cos_key": key, "output_path": path}

    def _resolved_plan(self, job_id: str) -> dict[str, Any]:
        outputs = self._outputs(job_id)
        for stage in ("generating_media", "resolving_materials"):
            value = outputs.get(stage, {}).get("resolved_plan")
            if isinstance(value, dict):
                return value
        raise ProviderError("repair_plan_missing")

    def _postprocessing_key(self, job_id: str) -> str:
        artifact = self._outputs(job_id).get("postprocessing", {}).get("artifact")
        key = artifact.get("cos_key") if isinstance(artifact, dict) else None
        if not isinstance(key, str) or not key:
            raise ProviderError("repair_source_missing")
        return key

    def _outputs(self, job_id: str) -> dict[str, dict[str, Any]]:
        with closing(store.open_store(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT stage,output_json FROM edit_v2_pipeline_checkpoints WHERE job_id=? AND status='completed'",
                (job_id,),
            ).fetchall()
        result = {}
        for row in rows:
            try:
                value = json.loads(row["output_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                result[row["stage"]] = value
        return result

    @staticmethod
    def _codes(context: dict[str, Any]) -> frozenset[str]:
        values = context.get("error_codes")
        if not isinstance(values, (tuple, list)) or not values or not all(isinstance(v, str) for v in values):
            raise ProviderError("repair_error_codes_invalid")
        return frozenset(values)

    @staticmethod
    def _directory(job_id: str) -> str:
        path = os.path.join(tempfile.gettempdir(), "ai-edit-v2-repair", job_id)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _repair_key(job: dict[str, Any]) -> str:
        import hashlib
        owner = hashlib.sha256(str(job["owner"]).encode("utf-8")).hexdigest()[:16]
        return f"ai-edit-v2/{owner}/{job['id']}/repair/final.mp4"

    @staticmethod
    def _download(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()


def create_quality_analyzer(**kwargs: Any) -> DashScopeFinalMediaAnalyzer:
    return DashScopeFinalMediaAnalyzer(**kwargs)


def create_repair_provider(**kwargs: Any) -> ProductionRepairProvider:
    return ProductionRepairProvider(**kwargs)
