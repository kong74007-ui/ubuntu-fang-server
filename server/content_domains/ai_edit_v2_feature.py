"""Single fail-closed capability decision for AI Edit V2."""

from __future__ import annotations

import os
from typing import Any

from . import ai_edit_v2_runtime as runtime


def _enabled() -> bool:
    return str(os.environ.get("AI_EDIT_V2_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def runtime_ready() -> bool:
    return runtime.runtime_ready()


def capability() -> dict[str, Any]:
    enabled = _enabled()
    ready = runtime_ready()
    accepts = enabled and ready
    reason = None if accepts else ("disabled" if not enabled else "pipeline_not_ready")
    return {
        "feature": "ai_edit_v2",
        "enabled": enabled,
        "runtime_ready": ready,
        "accepts_submissions": accepts,
        "phase": "phase_a",
        "reason": reason,
    }


def rejection() -> tuple[int, dict[str, str]] | None:
    state = capability()
    if state["accepts_submissions"]:
        return None
    code = "ai_edit_v2_disabled" if not state["enabled"] else "ai_edit_v2_not_ready"
    return 503, {"code": code, "detail": code}
