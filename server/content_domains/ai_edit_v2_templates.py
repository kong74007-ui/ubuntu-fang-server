"""Audited, published visual-language templates for AI editing V2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final


class TemplateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_PUBLISHED_TEMPLATES: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "business_diagnostic",
        "version": "1.0",
        "status": "published",
        "component_family": "editorial_business",
        "typography": {
            "heading": "bold_sans",
            "body": "neutral_sans",
            "number": "tabular_sans",
        },
        "palette_relationships": {
            "background": "low_chroma_dark",
            "foreground": "high_contrast_light",
            "accent": "single_warm_signal",
        },
        "motion_intensity": "measured",
        "sound_policy": {
            "music_policy": "duck_under_speech",
            "sfx_policy": "semantic_only",
        },
    },
    {
        "id": "modern_documentary",
        "version": "1.0",
        "status": "published",
        "component_family": "documentary_modern",
        "typography": {
            "heading": "condensed_sans",
            "body": "neutral_sans",
            "number": "tabular_sans",
        },
        "palette_relationships": {
            "background": "source_led_neutral",
            "foreground": "high_contrast_light",
            "accent": "single_cool_signal",
        },
        "motion_intensity": "restrained",
        "sound_policy": {
            "music_policy": "duck_under_speech",
            "sfx_policy": "none",
        },
    },
)


def get_published_template(template_id: str, version: str | None = None) -> dict[str, Any]:
    """Return a defensive copy of one explicitly published template version."""

    if not isinstance(template_id, str) or not template_id.strip():
        raise TemplateError("template_not_found")
    candidates = [
        template
        for template in _PUBLISHED_TEMPLATES
        if template["id"] == template_id and template["status"] == "published"
    ]
    if version is not None:
        candidates = [template for template in candidates if template["version"] == version]
    if not candidates:
        raise TemplateError("template_not_found")
    candidates.sort(key=lambda template: template["version"])
    return deepcopy(candidates[-1])


def list_published_templates() -> list[dict[str, Any]]:
    """List published versions in deterministic catalog order."""

    return [
        deepcopy(template)
        for template in sorted(
            _PUBLISHED_TEMPLATES,
            key=lambda item: (item["id"], item["version"]),
        )
        if template["status"] == "published"
    ]
