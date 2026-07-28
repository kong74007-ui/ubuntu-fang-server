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
ARTIFACT_REQUIRED_STAGES = frozenset(
    {"normalizing", "resolving_materials", "generating_media", "rendering", "postprocessing"}
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


def extract_artifacts(value: Any) -> list[dict[str, Any]]:
    """Extract artifact records at any nested ``artifact(s)`` boundary."""

    found: list[dict[str, Any]] = []

    def visit(item: Any, artifact_context: bool = False) -> None:
        if isinstance(item, dict):
            if artifact_context and "cos_key" in item:
                found.append(item)
            for key, child in item.items():
                visit(child, artifact_context or key in {"artifact", "artifacts"})
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, artifact_context)

    visit(value)
    return found


def validate_stage_output(
    stage: str, output: Any, verifier: Callable[[str, dict[str, Any]], bool] | None
) -> tuple[bool, str | None]:
    if not isinstance(output, dict):
        return False, "stage_output_invalid"
    artifacts = extract_artifacts(output)
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


def production_dependencies(db_path: str, *, services: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create the production Task 2-6 adapter bundle consumed by ``run_job``.

    Service hooks carry environment-specific file/COS repository operations, while
    provider defaults are the real stable adapter classes.
    """

    from . import ai_edit_v2_cos as cos_api
    from .ai_edit_v2_providers.dashscope import DashScopeClient
    from .ai_edit_v2_providers.elevenlabs import ElevenLabsProvider
    from .ai_edit_v2_providers.openai_image import OpenAIImageProvider
    from .ai_edit_v2_shotstack import ShotstackClient

    services = dict(services or {})
    adapter_types = {
        "dashscope": DashScopeClient,
        "openai_image": OpenAIImageProvider,
        "elevenlabs": ElevenLabsProvider,
        "shotstack": ShotstackClient,
    }

    def handler(stage: str) -> Callable[..., Any]:
        def invoke(job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> Any:
            context["assert_active"]()
            supplied = services.get(stage)
            if not callable(supplied):
                raise RuntimeError(f"production_{stage}_not_configured")
            return supplied(
                job,
                context,
                stage_input,
                adapter_types=adapter_types,
                cos_api=services.get("cos_api", cos_api),
                db_path=db_path,
            )

        return invoke

    def reconciler(stage: str) -> Callable[..., Any]:
        def invoke(job: dict[str, Any], context: dict[str, Any], stage_input: dict[str, Any]) -> Any:
            context["assert_active"]()
            supplied = services.get(f"reconcile_{stage}")
            if not callable(supplied):
                raise RuntimeError(f"production_{stage}_reconciler_not_configured")
            return supplied(
                job,
                context,
                stage_input,
                adapter_types=adapter_types,
                cos_api=services.get("cos_api", cos_api),
                db_path=db_path,
            )

        return invoke

    def verify(_stage: str, artifact: dict[str, Any]) -> bool:
        try:
            head = services.get("cos_api", cos_api).head_object(artifact["cos_key"])
            return (
                int(head.get("content_length", -1)) == artifact["size_bytes"]
                and str(head.get("etag", "")).strip('"') == artifact["etag"].strip('"')
            )
        except Exception:
            return False

    return {
        "production": True,
        "handlers": {stage: handler(stage) for stage in STABLE_STAGE_SEQUENCE},
        "reconcilers": {stage: reconciler(stage) for stage in PROVIDER_STAGES},
        "verify_artifact": verify,
        "db_path": db_path,
        "adapter_types": adapter_types,
    }


def runtime_ready() -> bool:
    return REQUIRED_STAGES.issubset(STAGE_HANDLERS)
