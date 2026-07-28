"""Single fail-closed capability decision for AI Edit V2."""

from __future__ import annotations

import os
import shutil
from typing import Any
from urllib.parse import urlsplit

from . import ai_edit_v2_runtime as runtime


def _enabled() -> bool:
    return str(os.environ.get("AI_EDIT_V2_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def runtime_ready() -> bool:
    return runtime.runtime_ready()


def _configured(*names: str) -> bool:
    return all(
        (value := os.environ.get(name, "").strip())
        and not value.lower().startswith("replace-with-")
        for name in names
    )


def _configured_https_url(name: str) -> bool:
    value = os.environ.get(name, "").strip()
    if not value or "replace-with-" in value.lower():
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _stable_components() -> dict[str, bool]:
    return {
        "dashscope": _configured("DASHSCOPE_API_KEY"),
        "openai_image": _configured("OPENAI_API_KEY"),
        "elevenlabs": _configured("ELEVENLABS_API_KEY"),
        "shotstack": _configured(
            "SHOTSTACK_API_KEY", "AI_EDIT_V2_WEBHOOK_SECRET"
        ) and _configured_https_url("AI_EDIT_V2_SHOTSTACK_CALLBACK_URL"),
        "cos": _configured(
            "AI_EDIT_V2_COS_SECRET_ID",
            "AI_EDIT_V2_COS_SECRET_KEY",
            "AI_EDIT_V2_COS_REGION",
            "AI_EDIT_V2_COS_BUCKET",
        ),
        "ffmpeg": shutil.which(os.environ.get("AI_EDIT_V2_FFMPEG_BIN", "ffmpeg"))
        is not None,
        "ffprobe": shutil.which(os.environ.get("AI_EDIT_V2_FFPROBE_BIN", "ffprobe"))
        is not None,
    }


def capability() -> dict[str, Any]:
    enabled = _enabled()
    ready = runtime_ready()
    stable_components = _stable_components()
    stable_runtime_ready = ready and all(stable_components.values())
    accepts = enabled and stable_runtime_ready
    reason = None if accepts else ("disabled" if not enabled else "pipeline_not_ready")
    return {
        "feature": "ai_edit_v2",
        "enabled": enabled,
        "runtime_ready": ready,
        "accepts_submissions": accepts,
        "phase": "phase_a",
        "reason": reason,
        "stable_components": stable_components,
        "stable_runtime_ready": stable_runtime_ready,
        "renderers": {
            "shotstack": accepts and stable_runtime_ready,
            "remotion": False,
            "hyperframes": False,
        },
        "generation": {"ai_video": False},
    }


def rejection() -> tuple[int, dict[str, str]] | None:
    state = capability()
    if state["accepts_submissions"]:
        return None
    code = "ai_edit_v2_disabled" if not state["enabled"] else "ai_edit_v2_not_ready"
    return 503, {"code": code, "detail": code}
