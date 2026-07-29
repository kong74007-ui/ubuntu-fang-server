#!/usr/bin/env python3
"""Fail-closed AI Edit V2 provider smoke checks with redacted output."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_READY = 3
EXIT_TIMEOUT = 4
EXIT_PROVIDER_FAILURE = 5

PROVIDERS = (
    "dashscope-asr",
    "dashscope-qwen",
    "openai-image",
    "elevenlabs-music",
    "elevenlabs-sfx",
    "shotstack",
    "cos",
)

_COS_ENV = (
    "AI_EDIT_V2_COS_SECRET_ID",
    "AI_EDIT_V2_COS_SECRET_KEY",
    "AI_EDIT_V2_COS_REGION",
    "AI_EDIT_V2_COS_BUCKET",
)
_REQUIRED_ENV = {
    "dashscope-asr": ("DASHSCOPE_API_KEY", "AI_EDIT_V2_SMOKE_ASR_URL"),
    "dashscope-qwen": ("DASHSCOPE_API_KEY",),
    "openai-image": ("OPENAI_API_KEY",),
    "elevenlabs-music": ("ELEVENLABS_API_KEY",) + _COS_ENV,
    "elevenlabs-sfx": ("ELEVENLABS_API_KEY",) + _COS_ENV,
    "shotstack": (
        "SHOTSTACK_API_KEY",
        "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL",
        "AI_EDIT_V2_WEBHOOK_SECRET",
        "AI_EDIT_V2_SMOKE_SHOTSTACK_SOURCE_URL",
    ),
    "cos": _COS_ENV + ("AI_EDIT_V2_SMOKE_COS_KEY",),
}
_RESULT_PREFIX = "AI_EDIT_V2_SMOKE_RESULT="
SMOKE_JOB_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class SmokeResult:
    exit_code: int
    stage: str
    request_id: str | None = None


def _ready(provider: str, environ: Mapping[str, str]) -> bool:
    required = _REQUIRED_ENV.get(provider)
    return bool(required) and all(str(environ.get(name, "")).strip() for name in required)


def _request_id(value: Any) -> str | None:
    candidate = None
    if isinstance(value, dict):
        candidate = value.get("request_id") or value.get("id")
    else:
        payload = getattr(value, "payload", None)
        if isinstance(payload, dict):
            candidate = payload.get("provider_task_id")
        candidate = candidate or getattr(value, "request_id", None)
    if not isinstance(candidate, str):
        return None
    safe = "".join(re.findall(r"[A-Za-z0-9_-]", candidate))
    return safe or None


def _redacted_request_id(value: str | None) -> str:
    if not value:
        return "none"
    return "..." + value[-4:] if len(value) > 4 else "..."


def format_result(result: SmokeResult) -> str:
    return f"stage={result.stage} request_id={_redacted_request_id(result.request_id)}"


def _import_provider_module(module_name: str) -> Any:
    try:
        return importlib.import_module("server." + module_name)
    except ModuleNotFoundError as exc:
        if exc.name != "server":
            raise
        return importlib.import_module(module_name)


def run_smoke(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
    command: Sequence[str] | None = None,
    timeout_seconds: float = 30.0,
) -> SmokeResult:
    env = os.environ if environ is None else environ
    if provider not in PROVIDERS:
        return SmokeResult(EXIT_USAGE, "argument_validation")
    if not _ready(provider, env):
        return SmokeResult(EXIT_NOT_READY, "not_ready")
    child_command = list(command or (
        sys.executable, str(Path(__file__).resolve()), "--_provider-child",
        "--provider", provider, "--timeout", str(timeout_seconds),
    ))
    try:
        process = subprocess.Popen(
            child_command, cwd=ROOT, env=dict(env), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        stdout, _stderr = process.communicate(timeout=max(0.001, float(timeout_seconds)))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.communicate(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        return SmokeResult(EXIT_TIMEOUT, "timeout")
    except (OSError, ValueError):
        return SmokeResult(EXIT_PROVIDER_FAILURE, "failed")
    if process.returncode != EXIT_OK:
        return SmokeResult(EXIT_PROVIDER_FAILURE, "failed")
    for line in stdout.splitlines():
        if not line.startswith(_RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(_RESULT_PREFIX):])
        except (TypeError, ValueError):
            return SmokeResult(EXIT_PROVIDER_FAILURE, "failed")
        return SmokeResult(EXIT_OK, "completed", _request_id(value))
    return SmokeResult(EXIT_PROVIDER_FAILURE, "failed")


def _run_child(provider: str, timeout_seconds: float) -> int:
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with redirect_stdout(sink), redirect_stderr(sink):
                value = _run_provider(provider, os.environ, timeout_seconds)
    except BaseException:
        return EXIT_PROVIDER_FAILURE
    print(_RESULT_PREFIX + json.dumps({"request_id": _request_id(value)}), flush=True)
    return EXIT_OK


def _run_provider(provider: str, environ: Mapping[str, str], timeout: float) -> Any:
    if provider.startswith("dashscope-"):
        DashScopeClient = _import_provider_module(
            "content_domains.ai_edit_v2_providers.dashscope"
        ).DashScopeClient

        client = DashScopeClient(timeout_seconds=max(1, round(timeout)))
        if provider == "dashscope-asr":
            return client.submit_asr(environ["AI_EDIT_V2_SMOKE_ASR_URL"], "provider-smoke-asr")
        return client.generate_edit_plan(
            "You are a provider connectivity check.",
            "Reply with the single word OK.",
        )
    if provider == "openai-image":
        payload = json.dumps(
            {
                "model": environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2"),
                "prompt": "A neutral gray square used only for an API smoke check",
                "size": "1024x1024",
                "quality": "low",
                "output_format": "png",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/images/generations",
            data=payload,
            headers={
                "Authorization": "Bearer " + environ["OPENAI_API_KEY"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(1, timeout)) as response:
            value = json.loads(response.read().decode("utf-8"))
        return {"request_id": value.get("id") or response.headers.get("x-request-id")}
    if provider.startswith("elevenlabs-"):
        ElevenLabsProvider = _import_provider_module(
            "content_domains.ai_edit_v2_providers.elevenlabs"
        ).ElevenLabsProvider

        with tempfile.TemporaryDirectory(prefix="ai-edit-v2-smoke-") as directory:
            adapter = ElevenLabsProvider(
                owner="provider-smoke",
                job_id=SMOKE_JOB_ID,
                db_path=str(Path(directory) / "provider.db"),
                timeout_seconds=max(1, round(timeout)),
            )
            if provider == "elevenlabs-music":
                return adapter.generate_music("brief neutral instrumental sting", 3_000, "provider-smoke-music")
            return adapter.generate_sfx("soft click", 500, "provider-smoke-sfx")
    if provider == "shotstack":
        store = _import_provider_module("content_domains.ai_edit_v2_store")
        ShotstackClient = _import_provider_module(
            "content_domains.ai_edit_v2_shotstack"
        ).ShotstackClient

        with tempfile.TemporaryDirectory(prefix="ai-edit-v2-smoke-") as directory:
            db_path = str(Path(directory) / "provider.db")
            store.init_db(db_path)
            store.create_job(
                "provider-smoke", {}, "provider-smoke-quote", "provider-smoke-job", 1,
                uuid_factory=lambda: "provider-smoke-job", db_path=db_path,
            )
            attempt_id = store.record_stage_attempt(
                "provider-smoke-job", "rendering", 1, "running", 1, db_path=db_path
            )
            graph = {
                "version": "1.0",
                "aspect_ratio": "16:9",
                "duration_ms": 1_000,
                "components": [{
                    "type": "broll_video",
                    "src": environ["AI_EDIT_V2_SMOKE_SHOTSTACK_SOURCE_URL"],
                    "start": 0.0,
                    "length": 1.0,
                }],
                "output": {
                    "format": "mp4", "resolution": "1080p",
                    "video_codec": "h264", "audio_codec": "aac",
                },
            }
            return ShotstackClient(
                job_id="provider-smoke-job", attempt_id=attempt_id, db_path=db_path,
                timeout_seconds=max(1, round(timeout)),
            ).submit(graph, "provider-smoke-render")
    if provider == "cos":
        ai_edit_v2_cos = _import_provider_module("content_domains.ai_edit_v2_cos")

        metadata = ai_edit_v2_cos.head_object(environ["AI_EDIT_V2_SMOKE_COS_KEY"])
        return {"request_id": metadata.get("etag")}
    raise ValueError("unsupported provider")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one AI Edit V2 provider smoke check")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--_provider-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.provider is None:
        return EXIT_USAGE
    if args._provider_child:
        return _run_child(args.provider, args.timeout)
    result = run_smoke(args.provider, timeout_seconds=args.timeout)
    print(format_result(result), flush=True)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
