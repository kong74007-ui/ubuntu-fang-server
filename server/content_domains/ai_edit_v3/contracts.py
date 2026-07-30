from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator


TERMINAL_STATES = frozenset({"completed", "refunded", "prehold_absent"})
RECONCILIATION_STATES = frozenset(
    {
        "billing_reconciling",
        "failed_reconciliation_pending",
        "asset_decision_reconciling",
        "failed_asset_decision_pending",
    }
)
MEDIA_STATES = (
    "queued",
    "generating_voice",
    "normalizing",
    "transcribing",
    "aligning",
    "planning",
    "resolving_materials",
    "generating_images",
    "generating_audio",
    "mixing_audio",
    "compiling",
    "rendering",
    "quality_checking",
    "repair_planning",
    "staging_delivery",
)
ALLOWED_TRANSITIONS = {
    "created_draft": {"preholding"},
    "preholding": {"queued", "prehold_absent", "billing_reconciling"},
    "queued": {"generating_voice", "failed"},
    "generating_voice": {"normalizing", "failed"},
    "normalizing": {"transcribing", "failed"},
    "transcribing": {"aligning", "failed"},
    "aligning": {"planning", "failed"},
    "planning": {"resolving_materials", "failed"},
    "resolving_materials": {"generating_images", "failed"},
    "generating_images": {"generating_audio", "failed"},
    "generating_audio": {"mixing_audio", "failed"},
    "mixing_audio": {"compiling", "failed"},
    "compiling": {"rendering", "failed"},
    "rendering": {"quality_checking", "failed"},
    "quality_checking": {"repair_planning", "staging_delivery", "failed"},
    "repair_planning": {"compiling", "failed"},
    "staging_delivery": {"settling", "failed"},
    "settling": {"publishing", "billing_reconciling"},
    "publishing": {"completed", "failed", "asset_decision_reconciling"},
    "asset_decision_reconciling": {
        "completed",
        "failed",
        "publishing",
        "failed_asset_decision_pending",
    },
    "failed_asset_decision_pending": {"completed", "failed"},
    "failed": {"refund_pending"},
    "refund_pending": {"refunded", "billing_reconciling"},
    "billing_reconciling": {
        "queued",
        "prehold_absent",
        "publishing",
        "settling",
        "refunded",
        "refund_pending",
        "failed_reconciliation_pending",
    },
    "failed_reconciliation_pending": {
        "prehold_absent",
        "refund_pending",
        "refunded",
    },
    "completed": set(),
    "refunded": set(),
    "prehold_absent": set(),
}


class ContractError(ValueError):
    def __init__(self, error_code: str, field_path: str, message: str):
        self.error_code = error_code
        self.field_path = field_path
        self.message = message
        super().__init__(f"{error_code} at {field_path}: {message}")


_SOURCE_FIELD = {
    "platform_talking_head": "source_asset_id",
    "uploaded_video": "source_upload_id",
    "existing_audio": "source_asset_id",
    "uploaded_audio": "source_upload_id",
    "script_to_audio_video": "tts_input",
}
_SOURCE_FIELDS = frozenset(_SOURCE_FIELD.values())
_CREATION_FIELDS = frozenset({"style_prompt", "template_id"})
_ALLOWED_REQUEST_FIELDS = frozenset(
    {
        "input_type",
        "ratio",
        "creation_mode",
        "material_asset_ids",
        *_SOURCE_FIELDS,
        *_CREATION_FIELDS,
    }
)
_AUTHORITY_FIELDS = frozenset(
    {
        "authoritative_text",
        "cos_key",
        "model",
        "renderer",
        "render_component",
        "output_path",
        "template_version",
        "template_published",
        "template_ratios",
    }
)
_SCHEMA_NAMES = frozenset(
    {
        "edit-plan-2.0.schema.json",
        "render-manifest-v1.schema.json",
        "quality-verdict-v1.schema.json",
    }
)
_SCHEMA_ROOT = Path(__file__).with_name("schemas")
_LAYOUT_IDS = frozenset(
    {
        "speaker_fullscreen",
        "speaker_left_info_right",
        "speaker_right_evidence_left",
        "material_fullscreen_speaker_pip",
        "product_hero",
        "editorial_collage",
        "comparison_split",
        "steps_stack",
        "number_proof",
        "quote_reversal",
        "method_timeline",
        "cta_offer",
    }
)
_OVERLAY_IDS = frozenset(
    {
        "standard_caption",
        "emphasis_caption",
        "headline_block",
        "chapter_label",
        "lower_third",
        "number_proof",
        "bullet_list",
        "info_card",
        "quote_card",
        "product_label",
        "step_indicator",
        "cta_block",
        "evidence_label",
    }
)
_ANIMATION_IDS = frozenset(
    {
        "fade",
        "slide",
        "scale",
        "rotate",
        "wipe",
        "stagger",
        "count_up",
        "image_pan_zoom",
        "card_reveal",
        "stamp",
        "light_sweep",
        "highlight_draw",
        "split_screen",
        "subtitle_pop",
    }
)
_TRANSITION_IDS = frozenset(
    {
        "hard_cut",
        "soft_wipe",
        "directional_slide",
        "light_flash",
        "card_match_cut",
    }
)
_THEME_CAPABILITIES = {
    "palette_id": frozenset({"midnight_gold"}),
    "typography_id": frozenset({"editorial_sans"}),
    "density": frozenset({"airy", "balanced", "dense"}),
    "motion_energy": frozenset({"low", "medium", "high"}),
    "image_fit": frozenset({"contain", "cover", "smart_crop"}),
}
_QUALITY_BLOCKING = {
    "media_decode_codec_dimensions": True,
    "av_duration_sync": True,
    "black_frames": True,
    "abnormal_freeze": True,
    "audio_integrity": True,
    "caption_fact_accuracy": True,
    "safe_area_and_text_visibility": True,
    "face_product_obstruction": True,
    "material_provenance": True,
    "material_semantic_identity": True,
    "generated_evidence_claim": True,
    "opening_hook_visual_consistency": False,
}
_QUALITY_CHECK_IDS = frozenset(_QUALITY_BLOCKING)
_NEGATION_MARKERS = (
    "不会",
    "没有",
    "不得",
    "不能",
    "不再",
    "未曾",
    "无需",
    "并非",
    "不",
    "没",
    "无",
    "非",
    "未",
    "否",
)
_CAUSAL_MARKERS = (
    "之所以",
    "是因为",
    "由于",
    "因为",
    "所以",
    "因此",
    "导致",
    "从而",
    "使得",
    "于是",
)
_PROMISE_MARKERS = (
    "100%",
    "保证",
    "承诺",
    "一定",
    "必然",
    "永久",
    "绝对",
    "立刻",
    "马上",
)


def _raise(error_code: str, field_path: str, message: str) -> None:
    raise ContractError(error_code, field_path, message)


def _has_control_character(value: str) -> bool:
    return any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _reject_control_characters(value: Any, field_path: str = "$") -> None:
    if isinstance(value, str):
        if _has_surrogate(value):
            _raise(
                "unicode_scalar_invalid",
                field_path,
                "lone surrogate code points are forbidden",
            )
        if _has_control_character(value):
            _raise(
                "control_character_forbidden",
                field_path,
                "control characters are forbidden",
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                if _has_surrogate(key):
                    _raise(
                        "unicode_scalar_invalid",
                        field_path,
                        "lone surrogate code points are forbidden",
                    )
                if _has_control_character(key):
                    _raise(
                        "control_character_forbidden",
                        field_path,
                        "control characters are forbidden",
                    )
            _reject_control_characters(item, f"{field_path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_control_characters(item, f"{field_path}[{index}]")


def _require_identifier(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        _raise("identifier_invalid", field_path, "identifier is invalid")
    return value


def normalize_job_request(body: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        _raise("request_type_invalid", "$", "request must be an object")

    _reject_control_characters(body)
    if any(not isinstance(key, str) for key in body):
        _raise(
            "request_unknown_field",
            "$",
            "request field names must be strings",
        )
    unknown = sorted(set(body) - _ALLOWED_REQUEST_FIELDS)
    if unknown:
        if unknown[0] in _AUTHORITY_FIELDS:
            _raise(
                "request_authority_field_forbidden",
                unknown[0],
                "client authority fields are forbidden",
            )
        _raise("request_unknown_field", unknown[0], "unknown request field")

    input_type = body.get("input_type")
    if input_type not in _SOURCE_FIELD:
        _raise("input_type_invalid", "input_type", "unsupported input type")
    expected_source = _SOURCE_FIELD[input_type]
    present_sources = {name for name in _SOURCE_FIELDS if name in body}
    if present_sources != {expected_source}:
        _raise(
            "input_discriminator_conflict",
            expected_source,
            "exactly the discriminator source field must be present",
        )
    source = body[expected_source]
    if expected_source == "tts_input":
        if not isinstance(source, Mapping):
            _raise(
                "tts_input_invalid",
                "tts_input",
                "TTS input must be an object",
            )
        if any(not isinstance(key, str) for key in source):
            _raise(
                "request_unknown_field",
                "tts_input",
                "TTS field names must be strings",
            )
        unknown_tts = sorted(set(source) - {"text", "voice_id"})
        if unknown_tts:
            _raise(
                "request_unknown_field",
                f"tts_input.{unknown_tts[0]}",
                "unknown TTS input field",
            )
        if set(source) != {"text", "voice_id"}:
            _raise(
                "tts_input_invalid",
                "tts_input",
                "TTS text and voice are required",
            )
        text = source["text"]
        voice_id = source["voice_id"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 4000
            or not isinstance(voice_id, str)
            or not voice_id.strip()
            or len(voice_id) > 128
        ):
            _raise(
                "tts_input_invalid",
                "tts_input",
                "TTS text and voice are invalid",
            )
    else:
        _require_identifier(source, expected_source)

    ratio = body.get("ratio")
    if input_type in {"platform_talking_head", "uploaded_video"}:
        if ratio != "auto":
            _raise("ratio_invalid", "ratio", "video inputs require auto ratio")
    else:
        if ratio is None:
            ratio = "16:9"
        if ratio not in {"16:9", "9:16"}:
            _raise("ratio_invalid", "ratio", "audio inputs require 16:9 or 9:16")

    creation_mode = body.get("creation_mode")
    if creation_mode not in {"ai_auto", "style_prompt", "template_reference"}:
        _raise(
            "creation_mode_invalid",
            "creation_mode",
            "unsupported creation mode",
        )
    present_creation_fields = {
        name for name in _CREATION_FIELDS if name in body
    }
    expected_creation_fields = {
        "style_prompt": {"style_prompt"},
        "template_reference": {"template_id"},
        "ai_auto": set(),
    }[creation_mode]
    if present_creation_fields != expected_creation_fields:
        _raise(
            "creation_mode_conflict",
            "creation_mode",
            "creation discriminator fields conflict",
        )
    if creation_mode == "style_prompt":
        style_prompt = body["style_prompt"]
        if (
            not isinstance(style_prompt, str)
            or not style_prompt.strip()
            or len(style_prompt) > 1000
        ):
            _raise(
                "style_prompt_invalid",
                "style_prompt",
                "style prompt must contain 1 to 1000 characters",
            )
    elif creation_mode == "template_reference":
        template_id = body["template_id"]
        if (
            isinstance(template_id, str)
            and template_id.lower().startswith(("draft:", "unpublished:"))
        ):
            _raise(
                "template_reference_unpublished",
                "template_id",
                "template reference is not published",
            )
        _require_identifier(template_id, "template_id")

    material_asset_ids = body.get("material_asset_ids", [])
    if (
        not isinstance(material_asset_ids, list)
        or len(material_asset_ids) > 10
    ):
        _raise(
            "material_asset_ids_invalid",
            "material_asset_ids",
            "material IDs must be unique and contain at most ten items",
        )
    if any(
            not isinstance(material_id, str)
            or not material_id.strip()
            or len(material_id) > 128
            for material_id in material_asset_ids
    ):
        _raise(
            "material_asset_ids_invalid",
            "material_asset_ids",
            "material IDs must be unique and contain at most ten items",
        )
    if len(material_asset_ids) != len(set(material_asset_ids)):
        _raise(
            "material_asset_ids_invalid",
            "material_asset_ids",
            "material IDs must be unique and contain at most ten items",
        )

    normalized = copy.deepcopy(dict(body))
    normalized["ratio"] = ratio
    normalized.setdefault("material_asset_ids", [])
    return normalized


def canonical_json(value: Any) -> bytes:
    _reject_control_characters(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "canonical_json_invalid",
            "$",
            "value cannot be represented as canonical JSON",
        ) from exc
    return text.encode("utf-8")


def request_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_strict_json(
    raw: str | bytes,
    *,
    max_bytes: int,
    max_depth: int,
    max_items: int,
    max_string_chars: int,
) -> Any:
    limits = (max_bytes, max_depth, max_items, max_string_chars)
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        for limit in limits
    ):
        _raise("json_limit_invalid", "$", "JSON limits must be positive integers")
    if isinstance(raw, bytes):
        raw_bytes = raw
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ContractError(
                "json_utf8_invalid",
                "$",
                "JSON must be valid UTF-8",
            ) from exc
    elif isinstance(raw, str):
        text = raw
        raw_bytes = raw.encode("utf-8")
    else:
        _raise("json_input_invalid", "$", "JSON input must be text or bytes")
    if len(raw_bytes) > max_bytes:
        _raise("json_bytes_exceeded", "$", "JSON byte limit exceeded")

    json_whitespace = " \t\r\n"
    start = 0
    while start < len(text) and text[start] in json_whitespace:
        start += 1
    if start == len(text):
        _raise("json_invalid", "$", "JSON input is empty")

    structural_depth = 0
    in_string = False
    escaped = False
    for character in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            structural_depth += 1
            if structural_depth > max_depth:
                _raise(
                    "json_depth_exceeded",
                    "$",
                    "JSON depth limit exceeded",
                )
        elif character in "]}":
            structural_depth = max(0, structural_depth - 1)

    def reject_constant(token: str) -> None:
        _raise(
            "json_nonfinite_number",
            "$",
            f"non-finite number {token} is forbidden",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _raise(
                    "json_duplicate_key",
                    key,
                    "duplicate JSON object key",
                )
            result[key] = value
        return result

    decoder = json.JSONDecoder(
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    try:
        value, end = decoder.raw_decode(text, start)
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(
            "json_invalid",
            "$",
            "JSON syntax is invalid",
        ) from exc
    except RecursionError as exc:
        raise ContractError(
            "json_depth_exceeded",
            "$",
            "JSON depth limit exceeded",
        ) from exc
    if any(character not in json_whitespace for character in text[end:]):
        _raise(
            "json_trailing_content",
            "$",
            "multiple roots or trailing content are forbidden",
        )

    item_count = 0

    def inspect(item: Any, depth: int, path: str) -> None:
        nonlocal item_count
        if depth > max_depth:
            _raise("json_depth_exceeded", path, "JSON depth limit exceeded")
        if isinstance(item, str):
            if _has_surrogate(item):
                _raise(
                    "unicode_scalar_invalid",
                    path,
                    "lone surrogate code points are forbidden",
                )
            if len(item) > max_string_chars:
                _raise(
                    "json_string_exceeded",
                    path,
                    "JSON string limit exceeded",
                )
            if _has_control_character(item):
                _raise(
                    "control_character_forbidden",
                    path,
                    "control characters are forbidden",
                )
            return
        if isinstance(item, float) and not math.isfinite(item):
            _raise(
                "json_nonfinite_number",
                path,
                "non-finite numbers are forbidden",
            )
        if isinstance(item, Mapping):
            item_count += len(item)
            if item_count > max_items:
                _raise(
                    "json_items_exceeded",
                    path,
                    "JSON item limit exceeded",
                )
            for key, child in item.items():
                if len(key) > max_string_chars:
                    _raise(
                        "json_string_exceeded",
                        path,
                        "JSON key limit exceeded",
                    )
                if _has_surrogate(key):
                    _raise(
                        "unicode_scalar_invalid",
                        path,
                        "lone surrogate code points are forbidden",
                    )
                if _has_control_character(key):
                    _raise(
                        "control_character_forbidden",
                        path,
                        "control characters are forbidden",
                    )
                inspect(child, depth + 1, f"{path}.{key}")
            return
        if isinstance(item, list):
            item_count += len(item)
            if item_count > max_items:
                _raise(
                    "json_items_exceeded",
                    path,
                    "JSON item limit exceeded",
                )
            for index, child in enumerate(item):
                inspect(child, depth + 1, f"{path}[{index}]")

    try:
        inspect(value, 1, "$")
    except RecursionError as exc:
        raise ContractError(
            "json_depth_exceeded",
            "$",
            "JSON depth limit exceeded",
        ) from exc
    return value


def _schema_path(name: str) -> Path:
    if name not in _SCHEMA_NAMES:
        _raise("schema_name_unknown", "name", "unknown frozen Schema")
    return _SCHEMA_ROOT / name


def schema_sha256(name: str) -> str:
    return hashlib.sha256(_schema_path(name).read_bytes()).hexdigest()


def _load_schema(name: str) -> dict[str, Any]:
    try:
        return json.loads(_schema_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            "schema_unavailable",
            name,
            "frozen Schema is unavailable",
        ) from exc


def _json_path(parts: Any) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def _validate_schema(value: Any, name: str, error_code: str) -> None:
    errors = sorted(
        Draft202012Validator(_load_schema(name)).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        first = errors[0]
        raise ContractError(
            error_code,
            _json_path(first.absolute_path),
            first.message,
        )


def _ensure_unique_ids(
    values: list[Mapping[str, Any]],
    *,
    field: str,
    key: str = "id",
) -> None:
    seen: set[str] = set()
    for index, value in enumerate(values):
        identifier = value[key]
        if identifier in seen:
            _raise(
                "director_id_duplicate",
                f"{field}[{index}].{key}",
                "IDs must be unique",
            )
        seen.add(identifier)


def _timeline_capability(
    timeline: Mapping[str, Any],
    primary: str,
    alternate: str,
    default: frozenset[str],
) -> frozenset[str]:
    configured = timeline.get(primary, timeline.get(alternate))
    if configured is None:
        return default
    if not isinstance(configured, (list, tuple, set, frozenset)):
        _raise(
            "timeline_capability_invalid",
            primary,
            "capability list is invalid",
        )
    configured_set = frozenset(configured)
    if (
        any(not isinstance(item, str) for item in configured_set)
        or not configured_set.issubset(default)
    ):
        _raise(
            "timeline_capability_invalid",
            primary,
            "capability list may not enlarge the frozen registry",
        )
    return configured_set


def _marker_sequence(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    pattern = "|".join(
        re.escape(marker) for marker in sorted(markers, key=len, reverse=True)
    )
    return tuple(match.group(0) for match in re.finditer(pattern, text))


def _compressed_text_preserves_facts(
    source: str,
    output: str,
    protected_terms: list[str],
) -> bool:
    source_normalized = "".join(
        character
        for character in unicodedata.normalize("NFC", source)
        if not unicodedata.category(character).startswith(("P", "Z"))
    )
    output_normalized = "".join(
        character
        for character in unicodedata.normalize("NFC", output)
        if not unicodedata.category(character).startswith(("P", "Z"))
    )
    source_position = 0
    for character in output_normalized:
        source_position = source_normalized.find(character, source_position)
        if source_position < 0:
            return False
        source_position += 1
    for term in protected_terms:
        if (
            not isinstance(term, str)
            or not term
            or source.count(term) == 0
            or output.count(term) != source.count(term)
        ):
            return False
    if re.findall(r"\d+(?:\.\d+)?(?:%|元|万|亿)?", source) != re.findall(
        r"\d+(?:\.\d+)?(?:%|元|万|亿)?",
        output,
    ):
        return False
    return all(
        _marker_sequence(source, markers) == _marker_sequence(output, markers)
        for markers in (
            _NEGATION_MARKERS,
            _CAUSAL_MARKERS,
            _PROMISE_MARKERS,
        )
    )


def _validate_time_range(
    value: Mapping[str, Any],
    duration_ms: int,
    *,
    field_path: str,
    error_code: str,
) -> None:
    start = value["start_ms"]
    end = value["end_ms"]
    if start < 0 or end <= start or end > duration_ms:
        _raise(error_code, field_path, "time range is invalid")


def validate_edit_plan(
    plan: Any,
    *,
    timeline: Mapping[str, Any],
) -> dict[str, Any]:
    _reject_control_characters(plan)
    _validate_schema(plan, "edit-plan-2.0.schema.json", "director_schema_invalid")
    if not isinstance(timeline, Mapping):
        _raise("timeline_invalid", "timeline", "timeline must be an object")
    duration_ms = plan["duration_ms"]
    timeline_duration = timeline.get("duration_ms")
    if (
        isinstance(timeline_duration, bool)
        or not isinstance(timeline_duration, int)
        or timeline_duration != duration_ms
    ):
        _raise(
            "director_duration_mismatch",
            "duration_ms",
            "plan duration must equal the frozen timeline",
        )

    for field in (
        "narrative_arc",
        "captions",
        "source_segments",
        "scenes",
        "audio_cues",
    ):
        _ensure_unique_ids(plan[field], field=field)
    _ensure_unique_ids(plan["materials"], field="materials", key="request_id")

    accurate_values = timeline.get(
        "accurate_captions",
        timeline.get("captions"),
    )
    if not isinstance(accurate_values, list) or not accurate_values:
        _raise(
            "timeline_invalid",
            "accurate_captions",
            "authoritative captions are required",
        )
    accurate_by_id = {
        caption["id"]: caption
        for caption in accurate_values
        if isinstance(caption, Mapping) and isinstance(caption.get("id"), str)
    }
    plan_caption_ids = [caption["id"] for caption in plan["captions"]]
    if len(accurate_by_id) != len(accurate_values):
        _raise(
            "timeline_invalid",
            "accurate_captions",
            "authoritative caption IDs must be unique",
        )
    for index, caption in enumerate(plan["captions"]):
        authoritative = accurate_by_id.get(caption["id"])
        if authoritative is None:
            _raise(
                "director_reference_unknown",
                f"captions[{index}].id",
                "caption is absent from the authoritative timeline",
            )
        if any(
            caption[field] != authoritative.get(field)
            for field in ("start_ms", "end_ms", "text")
        ):
            _raise(
                "accurate_text_changed",
                f"captions[{index}]",
                "accurate caption text or timing changed",
            )

    scenes = plan["scenes"]
    next_start = 0
    for index, scene in enumerate(scenes):
        if (
            scene["start_ms"] != next_start
            or scene["end_ms"] <= scene["start_ms"]
            or scene["end_ms"] - scene["start_ms"] < 500
        ):
            _raise(
                "scene_timeline_invalid",
                f"scenes[{index}]",
                "scenes must be continuous from zero",
            )
        next_start = scene["end_ms"]
    if next_start != duration_ms:
        _raise(
            "scene_timeline_invalid",
            "scenes",
            "scenes must end at the plan duration",
        )

    previous_caption_end = 0
    for index, caption in enumerate(plan["captions"]):
        _validate_time_range(
            caption,
            duration_ms,
            field_path=f"captions[{index}]",
            error_code="caption_timeline_invalid",
        )
        if caption["start_ms"] < previous_caption_end:
            _raise(
                "caption_timeline_invalid",
                f"captions[{index}]",
                "captions must be monotonic",
            )
        if not any(
            scene["start_ms"] <= caption["start_ms"]
            and caption["end_ms"] <= scene["end_ms"]
            for scene in scenes
        ):
            _raise(
                "caption_timeline_invalid",
                f"captions[{index}]",
                "caption must be contained by one scene",
            )
        previous_caption_end = caption["end_ms"]

    layout_ids = _timeline_capability(
        timeline,
        "layout_capabilities",
        "layout_ids",
        _LAYOUT_IDS,
    )
    overlay_ids = _timeline_capability(
        timeline,
        "overlay_capabilities",
        "overlay_ids",
        _OVERLAY_IDS,
    )
    animation_ids = _timeline_capability(
        timeline,
        "animation_capabilities",
        "animation_ids",
        _ANIMATION_IDS,
    )
    transition_ids = _timeline_capability(
        timeline,
        "transition_capabilities",
        "transition_ids",
        _TRANSITION_IDS,
    )
    caption_index = {
        caption_id: index for index, caption_id in enumerate(plan_caption_ids)
    }
    material_requests = {
        material["request_id"]: material for material in plan["materials"]
    }
    for material_index, material in enumerate(plan["materials"]):
        _validate_time_range(
            material["time_range"],
            duration_ms,
            field_path=f"materials[{material_index}].time_range",
            error_code="material_request_timeline_invalid",
        )
    bound_material_ids: set[str] = set()
    for scene_index, scene in enumerate(scenes):
        if scene["layout_id"] not in layout_ids:
            _raise(
                "director_capability_unknown",
                f"scenes[{scene_index}].layout_id",
                "layout is not in the frozen capability registry",
            )
        if scene["transition"] not in transition_ids:
            _raise(
                "director_capability_unknown",
                f"scenes[{scene_index}].transition",
                "transition is not in the frozen capability registry",
            )
        if any(overlay not in overlay_ids for overlay in scene["overlay_ids"]):
            _raise(
                "director_capability_unknown",
                f"scenes[{scene_index}].overlay_ids",
                "overlay is not in the frozen capability registry",
            )
        valid_targets = set(scene["overlay_ids"]) | {
            slot["id"] for slot in scene["material_slots"]
        }
        for animation_index, animation in enumerate(scene["animations"]):
            if animation["preset"] not in animation_ids:
                _raise(
                    "director_capability_unknown",
                    f"scenes[{scene_index}].animations[{animation_index}].preset",
                    "animation is not in the frozen capability registry",
                )
            if animation["target"] not in valid_targets:
                _raise(
                    "director_reference_unknown",
                    f"scenes[{scene_index}].animations[{animation_index}].target",
                    "animation target does not exist in the scene",
                )

        for text_field in ("headline", "highlight"):
            visible = scene[text_field]
            if visible["text_kind"] == "ui_label":
                continue
            references = visible["source_caption_ids"]
            if any(reference not in caption_index for reference in references):
                _raise(
                    "director_reference_unknown",
                    f"scenes[{scene_index}].{text_field}.source_caption_ids",
                    "visible text references an unknown caption",
                )
            indices = [caption_index[reference] for reference in references]
            if indices != list(range(indices[0], indices[0] + len(indices))):
                _raise(
                    "director_reference_unknown",
                    f"scenes[{scene_index}].{text_field}.source_caption_ids",
                    "visible text caption references must be consecutive",
                )
            authoritative = "".join(
                plan["captions"][index]["text"] for index in indices
            )
            if visible["text_kind"] == "verbatim":
                if visible["text"] != authoritative:
                    _raise(
                        "visible_text_inaccurate",
                        f"scenes[{scene_index}].{text_field}.text",
                        "verbatim visible text changed accurate text",
                    )
            else:
                protected_terms: list[str] = []
                for reference in references:
                    protected_terms.extend(
                        accurate_by_id[reference].get("protected_terms", [])
                    )
                if not _compressed_text_preserves_facts(
                    authoritative,
                    visible["text"],
                    protected_terms,
                ):
                    _raise(
                        "visible_text_protected_fact_changed",
                        f"scenes[{scene_index}].{text_field}.text",
                        "compressed text changed a protected fact",
                    )

        for slot_index, slot in enumerate(scene["material_slots"]):
            if (
                slot["start_ms"] < scene["start_ms"]
                or slot["end_ms"] <= slot["start_ms"]
                or slot["end_ms"] > scene["end_ms"]
            ):
                _raise(
                    "material_slot_timeline_invalid",
                    f"scenes[{scene_index}].material_slots[{slot_index}]",
                    "material slot must be contained by its scene",
                )
            request = material_requests.get(slot["id"])
            if request is None:
                if slot["priority"] == "required":
                    _raise(
                        "required_material_unresolved",
                        f"scenes[{scene_index}].material_slots[{slot_index}]",
                        "required material slot has no request",
                    )
                _raise(
                    "material_request_unbound",
                    f"scenes[{scene_index}].material_slots[{slot_index}]",
                    "material slot has no matching request",
                )
            bound_material_ids.add(slot["id"])
            expected_range = {
                "start_ms": slot["start_ms"],
                "end_ms": slot["end_ms"],
            }
            if any(
                request[field] != slot[field]
                for field in ("semantic", "purpose", "priority", "ratio")
            ) or request["time_range"] != expected_range:
                _raise(
                    "material_request_mismatch",
                    f"materials[{slot['id']}]",
                    "material request does not exactly match its slot",
                )

    unbound_materials = set(material_requests) - bound_material_ids
    if unbound_materials:
        unbound = sorted(unbound_materials)[0]
        _raise(
            "material_request_unbound",
            "materials",
            f"material request {unbound} has no matching slot",
        )

    segment_output = 0
    previous_source_end = 0
    for index, segment in enumerate(plan["source_segments"]):
        if (
            segment["source_start_ms"] < previous_source_end
            or segment["source_end_ms"] <= segment["source_start_ms"]
            or segment["output_start_ms"] != segment_output
            or segment["output_end_ms"] <= segment["output_start_ms"]
        ):
            _raise(
                "source_segment_timeline_invalid",
                f"source_segments[{index}]",
                "source and output segments must be monotonic and continuous",
            )
        if any(
            caption_id not in caption_index
            for caption_id in segment["caption_ids"]
        ):
            _raise(
                "director_reference_unknown",
                f"source_segments[{index}].caption_ids",
                "source segment references an unknown caption",
            )
        previous_source_end = segment["source_end_ms"]
        segment_output = segment["output_end_ms"]
    if segment_output != duration_ms:
        _raise(
            "source_segment_timeline_invalid",
            "source_segments",
            "source segments must cover the full output",
        )

    for field in ("narrative_arc", "audio_cues"):
        for index, value in enumerate(plan[field]):
            _validate_time_range(
                value,
                duration_ms,
                field_path=f"{field}[{index}]",
                error_code=f"{field}_timeline_invalid",
            )

    theme_capabilities = timeline.get("theme_capabilities")
    if theme_capabilities is None:
        _raise(
            "timeline_capability_missing",
            "theme_capabilities",
            "theme capabilities are required for a themed plan",
        )
    if (
        not isinstance(theme_capabilities, Mapping)
        or set(theme_capabilities) != set(_THEME_CAPABILITIES)
    ):
        _raise(
            "timeline_capability_invalid",
            "theme_capabilities",
            "theme capability registry is incomplete",
        )
    for field, frozen in _THEME_CAPABILITIES.items():
        allowed = theme_capabilities[field]
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            _raise(
                "timeline_capability_invalid",
                f"theme_capabilities.{field}",
                "theme capability list is invalid",
            )
        allowed_set = frozenset(allowed)
        if not allowed_set or not allowed_set.issubset(frozen):
            _raise(
                "timeline_capability_invalid",
                f"theme_capabilities.{field}",
                "theme capabilities may not enlarge the frozen registry",
            )
        if plan["theme"][field] not in allowed_set:
            _raise(
                "director_capability_unknown",
                f"theme.{field}",
                "theme token is not in the supplied frozen capabilities",
            )
    return copy.deepcopy(plan)


def _validate_relative_media_path(value: Any, field_path: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or value.startswith("/")
    ):
        _raise("render_path_invalid", field_path, "media path must be relative POSIX")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        _raise(
            "render_path_invalid",
            field_path,
            "media path escapes or is not normalized",
        )
    return path


def _verify_declared_file(
    declaration: Mapping[str, Any],
    *,
    path_key: str,
    sandbox_root: Path,
    field_path: str,
) -> tuple[str, str, int, tuple[int, int]]:
    relative = _validate_relative_media_path(
        declaration.get(path_key),
        f"{field_path}.{path_key}",
    )
    try:
        root_input = sandbox_root.absolute()
        root_metadata = root_input.lstat()
        reparse_attribute = getattr(
            stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0,
        )
        if stat.S_ISLNK(root_metadata.st_mode) or (
            getattr(root_metadata, "st_file_attributes", 0)
            & reparse_attribute
        ):
            _raise(
                "render_file_not_regular",
                field_path,
                "sandbox root may not be a reparse point",
            )
        root = sandbox_root.resolve(strict=True)
        candidate = root
        for part in relative.parts:
            candidate = candidate / part
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0)
                & reparse_attribute
            ):
                _raise(
                    "render_file_not_regular",
                    field_path,
                    "symlinks are forbidden",
                )
        pre_open = candidate.lstat()
        if not stat.S_ISREG(pre_open.st_mode) or pre_open.st_nlink != 1:
            _raise(
                "render_file_not_regular",
                field_path,
                "media must be an ordinary single-link file",
            )
        resolved = candidate.resolve(strict=True)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            "render_file_not_regular",
            field_path,
            "declared media path cannot be inspected",
        ) from exc
    if not resolved.is_relative_to(root):
        _raise("render_path_invalid", field_path, "media path escapes sandbox")

    declared_size = declaration.get("size_bytes")
    if declared_size is not None and pre_open.st_size != declared_size:
        _raise(
            "render_size_mismatch",
            field_path,
            "declared media size does not match",
        )
    hard_size_limit = declared_size if declared_size is not None else 1073741824
    try:
        stream = candidate.open("rb")
    except OSError as exc:
        raise ContractError(
            "render_file_unreadable",
            field_path,
            "declared media file cannot be opened",
        ) from exc
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _raise(
                "render_file_not_regular",
                field_path,
                "media must be an ordinary unlinked file",
            )
        if not os.path.samestat(pre_open, opened):
            _raise(
                "render_file_identity_changed",
                field_path,
                "media identity changed before open",
            )
        if opened.st_size != pre_open.st_size or opened.st_size > hard_size_limit:
            _raise(
                "render_size_mismatch",
                field_path,
                "opened media size does not match bounded declaration",
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = stream.read(min(1024 * 1024, hard_size_limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > hard_size_limit:
                _raise(
                    "render_size_mismatch",
                    field_path,
                    "media exceeded its declared size while hashing",
                )
            digest.update(chunk)
        after_read = os.fstat(stream.fileno())
        if not os.path.samestat(opened, after_read) or after_read.st_size != total:
            _raise(
                "render_file_identity_changed",
                field_path,
                "opened media changed while hashing",
            )
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            "render_file_unreadable",
            field_path,
            "declared media file could not be read",
        ) from exc
    finally:
        stream.close()
    try:
        post_open = candidate.lstat()
        post_resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError(
            "render_file_identity_changed",
            field_path,
            "media path disappeared after hashing",
        ) from exc
    if (
        not os.path.samestat(opened, post_open)
        or post_resolved != resolved
        or not post_resolved.is_relative_to(root)
    ):
        _raise(
            "render_file_identity_changed",
            field_path,
            "media path identity changed while hashing",
        )
    if digest.hexdigest() != declaration.get("sha256"):
        _raise(
            "render_hash_mismatch",
            field_path,
            "declared media SHA-256 does not match",
        )
    return (
        relative.as_posix(),
        digest.hexdigest(),
        total,
        (opened.st_dev, opened.st_ino),
    )


def validate_render_manifest(
    manifest: Any,
    *,
    sandbox_root: Path,
) -> dict[str, Any]:
    try:
        _validate_schema(
            manifest,
            "render-manifest-v1.schema.json",
            "render_schema_invalid",
        )
    except ContractError as exc:
        if exc.field_path.endswith((".path", ".source_path")):
            raise ContractError(
                "render_path_invalid",
                exc.field_path,
                "media path must be normalized relative POSIX",
            ) from exc
        raise
    _reject_control_characters(manifest)
    if manifest["schema_sha256"] != schema_sha256(
        "render-manifest-v1.schema.json"
    ):
        _raise(
            "render_schema_hash_mismatch",
            "schema_sha256",
            "manifest Schema hash does not match",
        )
    output_spec = manifest["output_spec"]
    dimensions = {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
    }[output_spec["ratio"]]
    if (output_spec["width"], output_spec["height"]) != dimensions:
        _raise(
            "render_ratio_dimensions_mismatch",
            "output_spec",
            "output dimensions do not match ratio",
        )
    duration_ms = manifest["duration_ms"]
    if manifest["master_audio"]["duration_ms"] != duration_ms:
        _raise(
            "render_duration_mismatch",
            "master_audio.duration_ms",
            "master audio duration must match output",
        )

    asset_ids = [asset["id"] for asset in manifest["assets"]]
    if len(asset_ids) != len(set(asset_ids)):
        _raise("render_id_duplicate", "assets", "asset IDs must be unique")
    composition_ids = [
        composition["id"] for composition in manifest["compositions"]
    ]
    if len(composition_ids) != len(set(composition_ids)):
        _raise(
            "render_id_duplicate",
            "compositions",
            "composition IDs must be unique",
        )
    known_assets = set(asset_ids)
    composition_start = 0
    for index, composition in enumerate(manifest["compositions"]):
        if (
            composition["start_ms"] != composition_start
            or composition["end_ms"] <= composition["start_ms"]
            or composition["end_ms"] > duration_ms
        ):
            _raise(
                "render_timeline_invalid",
                f"compositions[{index}]",
                "compositions must be continuous",
            )
        composition_start = composition["end_ms"]
        if composition["layout_id"] not in _LAYOUT_IDS:
            _raise(
                "render_capability_unknown",
                f"compositions[{index}].layout_id",
                "layout is not registered",
            )
        if composition["transition"] not in _TRANSITION_IDS:
            _raise(
                "render_capability_unknown",
                f"compositions[{index}].transition",
                "transition is not registered",
            )
        if any(
            overlay not in _OVERLAY_IDS
            for overlay in composition["overlay_ids"]
        ) or any(
            animation["preset"] not in _ANIMATION_IDS
            for animation in composition["animations"]
        ):
            _raise(
                "render_capability_unknown",
                f"compositions[{index}]",
                "composition references an unknown capability",
            )
        if any(
            animation["target"] not in composition["overlay_ids"]
            for animation in composition["animations"]
        ):
            _raise(
                "render_reference_unknown",
                f"compositions[{index}].animations",
                "animation target is absent from the composition",
            )
        if any(
            asset_id not in known_assets
            for asset_id in composition["asset_ids"]
        ):
            _raise(
                "render_reference_unknown",
                f"compositions[{index}].asset_ids",
                "composition references an unknown asset",
            )
    if composition_start != duration_ms:
        _raise(
            "render_timeline_invalid",
            "compositions",
            "compositions must cover output duration",
        )

    segment_output = 0
    previous_source_end = 0
    source_video = manifest["source_video"]
    for index, segment in enumerate(manifest["source_segments"]):
        if (
            source_video is None
            or segment["source_path"] != source_video["path"]
            or segment["sha256"] != source_video["sha256"]
            or segment["source_end_ms"] > source_video["duration_ms"]
        ):
            _raise(
                "render_source_video_binding_invalid",
                f"source_segments[{index}]",
                "source segment must bind to the declared source video",
            )
        if (
            segment["source_start_ms"] < previous_source_end
            or segment["source_end_ms"] <= segment["source_start_ms"]
            or segment["output_start_ms"] != segment_output
            or segment["output_end_ms"] <= segment["output_start_ms"]
        ):
            _raise(
                "render_source_mapping_invalid",
                f"source_segments[{index}]",
                "source mapping must be monotonic and continuous",
            )
        previous_source_end = segment["source_end_ms"]
        segment_output = segment["output_end_ms"]
    if segment_output != duration_ms:
        _raise(
            "render_source_mapping_invalid",
            "source_segments",
            "source mapping must cover output duration",
        )

    previous_caption_end = 0
    for index, caption in enumerate(manifest["captions"]):
        if (
            caption["start_ms"] < previous_caption_end
            or caption["end_ms"] <= caption["start_ms"]
            or caption["end_ms"] > duration_ms
        ):
            _raise(
                "render_caption_timeline_invalid",
                f"captions[{index}]",
                "caption timeline is invalid",
            )
        previous_caption_end = caption["end_ms"]

    root = Path(sandbox_root)
    file_declarations: list[tuple[Mapping[str, Any], str, str]] = []
    if manifest["source_video"] is not None:
        file_declarations.append(
            (manifest["source_video"], "path", "source_video")
        )
    file_declarations.append(
        (manifest["master_audio"], "path", "master_audio")
    )
    file_declarations.extend(
        (asset, "path", f"assets[{index}]")
        for index, asset in enumerate(manifest["assets"])
    )
    file_declarations.extend(
        (segment, "source_path", f"source_segments[{index}]")
        for index, segment in enumerate(manifest["source_segments"])
    )
    verified_paths: dict[str, tuple[str, int, tuple[int, int]]] = {}
    verified_identities: dict[tuple[int, int], str] = {}
    for declaration, path_key, field_path in file_declarations:
        relative = _validate_relative_media_path(
            declaration[path_key],
            f"{field_path}.{path_key}",
        ).as_posix()
        cached = verified_paths.get(relative)
        if cached is not None:
            cached_sha, cached_size, _identity = cached
            if declaration["sha256"] != cached_sha:
                _raise(
                    "render_hash_mismatch",
                    field_path,
                    "same path has conflicting declared hashes",
                )
            if (
                "size_bytes" in declaration
                and declaration["size_bytes"] != cached_size
            ):
                _raise(
                    "render_size_mismatch",
                    field_path,
                    "same path has conflicting declared sizes",
                )
            continue
        verified_path, digest, size, identity = _verify_declared_file(
            declaration,
            path_key=path_key,
            sandbox_root=root,
            field_path=field_path,
        )
        other_path = verified_identities.get(identity)
        if other_path is not None and other_path != verified_path:
            _raise(
                "render_file_not_regular",
                field_path,
                "different media paths may not share one file identity",
            )
        verified_paths[verified_path] = (digest, size, identity)
        verified_identities[identity] = verified_path
    return copy.deepcopy(manifest)


def validate_quality_verdict(verdict: Any) -> dict[str, Any]:
    _reject_control_characters(verdict)
    if isinstance(verdict, Mapping) and isinstance(verdict.get("checks"), list):
        seen: set[str] = set()
        for index, check in enumerate(verdict["checks"]):
            if not isinstance(check, Mapping):
                continue
            check_id = check.get("check_id")
            if check_id not in _QUALITY_CHECK_IDS:
                _raise(
                    "quality_check_unknown",
                    f"checks[{index}].check_id",
                    "quality check ID is not frozen",
                )
            if check_id in seen:
                _raise(
                    "quality_check_duplicate",
                    f"checks[{index}].check_id",
                    "quality check IDs must be unique",
                )
            seen.add(check_id)
            if (
                "blocking" in check
                and check["blocking"] is not _QUALITY_BLOCKING[check_id]
            ):
                _raise(
                    "quality_blocking_mismatch",
                    f"checks[{index}].blocking",
                    "quality blocking classification is frozen",
                )
            confidence = check.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                _raise(
                    "quality_confidence_invalid",
                    f"checks[{index}].confidence",
                    "quality confidence must be finite from zero to one",
                )
        missing = _QUALITY_CHECK_IDS - seen
        if missing:
            _raise(
                "quality_check_missing",
                "checks",
                "quality verdict must contain the complete frozen check set",
            )
    _validate_schema(
        verdict,
        "quality-verdict-v1.schema.json",
        "quality_schema_invalid",
    )
    if verdict["schema_sha256"] != schema_sha256(
        "quality-verdict-v1.schema.json"
    ):
        _raise(
            "quality_schema_hash_mismatch",
            "schema_sha256",
            "quality Schema hash does not match",
        )
    return copy.deepcopy(verdict)
