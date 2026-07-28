"""Stable runtime stage names and dependency lookup for AI Edit V2."""

from __future__ import annotations

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


def runtime_ready() -> bool:
    return REQUIRED_STAGES.issubset(STAGE_HANDLERS)
