"""Shared runtime registry used by the V2 API and worker readiness gate."""

from __future__ import annotations

from . import ai_edit_v2_pipeline as pipeline


STAGE_HANDLERS: dict[str, pipeline.Handler] = {}
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


def runtime_ready() -> bool:
    return REQUIRED_STAGES.issubset(STAGE_HANDLERS)
