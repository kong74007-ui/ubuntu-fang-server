#!/usr/bin/env python3
"""Internal API for catalog-driven text-media-text matrix videos."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import math
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfileobj
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont


MAX_BODY_BYTES = 128 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_WAITING_JOBS = 20
MAX_BATCH_SIZE = 5
RENDER_TIMEOUT_SECONDS = 900
REFERENCE_BGM_PREPARE_TIMEOUT_SECONDS = 120
DEFAULT_HYPERFRAMES_CONCURRENCY = 2
DEFAULT_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS = 900
DEFAULT_HYPERFRAMES_SLOT_TIMEOUT_SECONDS = 600
DEFAULT_RETENTION_SECONDS = 72 * 60 * 60
DEFAULT_DELIVERY_GRACE_SECONDS = 60 * 60
DEFAULT_CLEANUP_INTERVAL_SECONDS = 15 * 60
DEFAULT_CLEANUP_BATCH_SIZE = 10
DEFAULT_DISK_HIGH_WATER_PERCENT = 95.0
STATUS_WRITE_ATTEMPTS = 3
STATUS_WRITE_RETRY_SECONDS = 0.1
JOB_REQUEUE_SECONDS = 0.25
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BATCH_RE = re.compile(r"^[0-9a-f]{32}$")
CONTENT_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
}
BASE_FONT_FAMILIES = {
    "Noto Sans SC", "ZCOOL XiaoWei", "Ma Shan Zheng", "ZCOOL KuaiLe",
}
PRIVATE_FONT_FAMILIES = {
    "zihunbiantaoti", "Smiley Sans Oblique", "DaigoMinteuA",
    "Gen Jyuu Gothic Heavy", "GenSenRounded TW H", "HouZunSongTi",
    "AaHouDiHei", "Pangmenzhengdaoqingsongti", "Kingnam Bobo",
    "YS HelloFont BangBangTi",
}
FONT_LABELS = {
    "Noto Sans SC": "思源黑体",
    "ZCOOL XiaoWei": "站酷小薇体",
    "Ma Shan Zheng": "马善政毛笔楷书",
    "ZCOOL KuaiLe": "站酷快乐体",
    "zihunbiantaoti": "字魂扁桃体",
    "Smiley Sans Oblique": "得意黑",
    "DaigoMinteuA": "醍醐书体",
    "Gen Jyuu Gothic Heavy": "源柔黑体 Heavy",
    "GenSenRounded TW H": "源泉圆体 Heavy",
    "HouZunSongTi": "猴尊宋体",
    "AaHouDiHei": "Aa厚底黑",
    "Pangmenzhengdaoqingsongti": "庞门正道轻松体",
    "Kingnam Bobo": "荆南波波黑",
    "YS HelloFont BangBangTi": "优设字由棒棒体",
}
FONT_VARIANTS = {
    "full-overlay-bold": (("clean", "Noto Sans SC", "Noto Sans SC"), ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"), ("friendly", "ZCOOL KuaiLe", "Noto Sans SC")),
    "poster-split": (("clean", "Noto Sans SC", "Noto Sans SC"), ("editorial", "ZCOOL XiaoWei", "Noto Sans SC"), ("friendly", "ZCOOL KuaiLe", "Noto Sans SC")),
}
PRIVATE_FONT_VARIANTS = {
    "full-overlay-bold": (("private-heavy", "AaHouDiHei", "Noto Sans SC"), ("private-poster", "Kingnam Bobo", "Noto Sans SC"), ("private-display", "zihunbiantaoti", "Noto Sans SC")),
    "poster-split": (("private-heavy", "AaHouDiHei", "Noto Sans SC"), ("private-poster", "Kingnam Bobo", "Noto Sans SC"), ("private-display", "zihunbiantaoti", "Noto Sans SC")),
}
REFERENCE_PACK_ID = "reference-typography-17"
REFERENCE_HYPERFRAMES_VERSION = "0.8.16"
REFERENCE_TEMPLATE_COUNT = 17
REFERENCE_FEATURED_VARIANT = "v05"
REFERENCE_V01_VARIANT = "v01"
REFERENCE_V01_STYLE_CONTRACT = {
    "top1": (
        'font:40070px/1.08"mashan"',
        "color:#f7f5ec",
        "-webkit-text-stroke:11px#789822",
    ),
    "top2": (
        'font:40064px/1.15"mashan"',
        "color:#f8f7ef",
        "-webkit-text-stroke:9px#789822",
    ),
    "top3": (
        "font-size:52px",
        "font-weight:900",
        "color:#fff",
        "-webkit-text-stroke:7px#111",
    ),
    "bottom1": (
        'font:40056px/1.05"mashan"',
        "color:#fff",
        "-webkit-text-stroke:7px#111",
    ),
    "bottom2": (
        'font:40074px/1.15"mashan"',
        "background:#f5f4ee",
        "color:#426d24",
        "border-radius:22px",
    ),
}
REFERENCE_FEATURED_STYLE_CONTRACT = {
    "top1": (
        'font:900102px/1.02"notosc"',
        "color:#f4f7f2",
        "-webkit-text-stroke:12px#203449",
        "text-shadow:8px10px0#07111e",
    ),
    "top2": (
        'font:900104px/1.01"notosc"',
        "color:#f4f7f2",
        "-webkit-text-stroke:13px#203449",
        "text-shadow:9px11px0#07111e",
    ),
    "top3": (
        'font:90068px/1.04"notosc"',
        "color:#fff8d9",
        "-webkit-text-stroke:9px#26394a",
        "text-shadow:7px8px0#07111e",
    ),
    "bottom1": (
        'font:90068px/1.05"notosc"',
        "color:#ffe000",
        "-webkit-text-stroke:9px#263e32",
    ),
    "bottom2": (
        'font:90070px/1.06"notosc"',
        "background:#f4c900",
        "color:#26362d",
        "border-radius:28px",
    ),
}
REFERENCE_FONT_FILES = (
    "NotoSansSC-Variable.ttf",
    "MaShanZheng-Regular.ttf",
    "ZCOOLKuaiLe-Regular.ttf",
    "ZCOOLXiaoWei-Regular.ttf",
)
REFERENCE_FONT_FAMILY_FILES = {
    "Noto Sans SC": "NotoSansSC-Variable.ttf",
    "Ma Shan Zheng": "MaShanZheng-Regular.ttf",
    "ZCOOL KuaiLe": "ZCOOLKuaiLe-Regular.ttf",
    "ZCOOL XiaoWei": "ZCOOLXiaoWei-Regular.ttf",
}
REFERENCE_GSAP_CDN = (
    '<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>'
)
REFERENCE_GSAP_LOCAL = '<script src="./gsap.min.js"></script>'
REFERENCE_EMPTY_LAYER_STYLE = (
    '<style id="matrix-template-empty-layers">'
    '[data-var-text]:empty{display:none!important}'
    '</style>'
)
REFERENCE_CTA_SAFE_AREA_PERCENT = 15
REFERENCE_CTA_SAFE_AREA_STYLE_ID = "matrix-reference-cta-safe-area"
REFERENCE_CTA_SAFE_AREA_STYLE = (
    f'<style id="{REFERENCE_CTA_SAFE_AREA_STYLE_ID}">'
    f'#root .bottom{{bottom:{REFERENCE_CTA_SAFE_AREA_PERCENT}%}}'
    '</style>'
)
REFERENCE_BGM_SOURCE_RE = re.compile(
    r"assets/(?:input/bgm|bgm/silence)\.m4a"
)
REFERENCE_MEDIA_SAFETY_SECONDS = 0.1
REFERENCE_MIN_SEGMENT_SECONDS = 0.5
REFERENCE_DYNAMIC_TIMING_JS = """      const segment = duration / 3;
      const segmentStarts = [0, segment, segment * 2];
      const segmentDurations = [segment, segment, duration - segment * 2];"""
REFERENCE_BLACK_SCREEN_SECONDS = 0.5
REFERENCE_BLACK_SCREEN_FILTER = (
    "crop=1080:700:0:700,"
    "blackdetect=d=0.5:pix_th=0.10:pic_th=0.98"
)
REFERENCE_FIXED_PRIVATE_FONTS = {
    "v02": {
        "top2": {
            "family": "Smiley Sans Oblique",
            "alias": "HQSmileySansOblique",
            "font_size_px": 62,
        },
    },
    "v03": {
        "top2": {
            "family": "Smiley Sans Oblique",
            "alias": "HQSmileySansOblique",
            "font_size_px": 62,
        },
    },
}
REFERENCE_TEXT_LAYER_IDS = frozenset({
    "top1", "top2", "top3", "bottom1", "bottom2",
})
REFERENCE_PRIVATE_FONT_STYLE_ID = "matrix-reference-private-fonts"
REFERENCE_SEMANTIC_LAYOUT_VERSION = 1
REFERENCE_CANVAS_WIDTH_PX = 1080.0
REFERENCE_TEXT_MAX_WIDTH_PX = 996.0
REFERENCE_LETTER_SPACING_EM = 0.01
REFERENCE_CSS_FONT_FAMILIES = {
    "NotoSC": "Noto Sans SC",
    "MaShan": "Ma Shan Zheng",
    "KuaiLe": "ZCOOL KuaiLe",
    "XiaoWei": "ZCOOL XiaoWei",
}
_NUMERIC_PHRASE_RE = re.compile(
    r"(?:(?<![0-9])(?:"
    r"[0-9]{1,3}(?:[,，][0-9]{3})+(?:[.．][0-9]+)?"
    r"|[0-9]+(?:[.．][0-9]+)?"
    r")(?![0-9])|[零〇一二三四五六七八九十百千万亿两几]+)"
    r"\s*[十百千万亿个家人位名条款套种项台年月日天次岁]{0,2}"
)


def _css_declarations(value: str) -> dict[str, str]:
    result = {}
    for declaration in str(value or "").split(";"):
        if ":" not in declaration:
            continue
        name, raw = declaration.split(":", 1)
        name, raw = name.strip().lower(), raw.strip()
        if name and raw:
            result[name] = raw
    return result


def _css_font_family(value: str) -> str:
    alias = str(value or "").split(",", 1)[0].strip().strip('"\'')
    return REFERENCE_CSS_FONT_FAMILIES.get(alias, alias)


def _css_pixel_length(value: str, *, property_name: str) -> float:
    raw = str(value or "").strip().lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)px", raw)
    if match:
        return float(match.group(1))
    if raw in {"0", "+0", "-0"}:
        return 0.0
    raise MatrixTemplateError(
        f"HyperFrames reference template {property_name} is unsupported"
    )


def _css_padding_horizontal(value: str) -> tuple[float, float]:
    raw_parts = str(value or "").split()
    if not raw_parts:
        return 0.0, 0.0
    if len(raw_parts) > 4:
        raise MatrixTemplateError(
            "HyperFrames reference template padding is unsupported"
        )
    parts = [
        _css_pixel_length(item, property_name="padding")
        for item in raw_parts
    ]
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) in {2, 3}:
        return parts[1], parts[1]
    return parts[3], parts[1]


def _css_horizontal_padding(declarations: dict[str, str]) -> tuple[float, float]:
    padding_left, padding_right = _css_padding_horizontal(
        declarations.get("padding", "")
    )
    if declarations.get("padding-left"):
        padding_left = _css_pixel_length(
            declarations["padding-left"], property_name="padding",
        )
    if declarations.get("padding-right"):
        padding_right = _css_pixel_length(
            declarations["padding-right"], property_name="padding",
        )
    return padding_left, padding_right


def _reference_css_layer_metrics(
    index_html: str, variant: str, layer: str, max_lines: int,
) -> dict:
    styles = "\n".join(re.findall(
        r"<style\b[^>]*>(.*?)</style>", index_html,
        flags=re.IGNORECASE | re.DOTALL,
    ))
    styles = re.sub(r"/\*.*?\*/", "", styles, flags=re.DOTALL)
    declarations = {}
    parent_declarations = {}
    universal_declarations = {}
    parent = "top" if layer.startswith("top") else "bottom"
    target_selectors = {f".{layer}", f".{variant} .{layer}"}
    parent_selectors = {f".{parent}", f".{variant} .{parent}"}
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", styles, re.DOTALL):
        selectors = {
            re.sub(r"\s+", " ", item.strip())
            for item in rule.group(1).split(",")
        }
        if "*" in selectors:
            universal_declarations.update(_css_declarations(rule.group(2)))
        if selectors & parent_selectors:
            parent_declarations.update(_css_declarations(rule.group(2)))
        if selectors & target_selectors:
            declarations.update(_css_declarations(rule.group(2)))

    if universal_declarations.get("box-sizing") != "border-box":
        raise MatrixTemplateError(
            "HyperFrames reference template box sizing is unsupported"
        )
    parent_width = parent_declarations.get("width")
    if parent_width == "100%":
        parent_width_px = REFERENCE_CANVAS_WIDTH_PX
    elif parent_width:
        parent_width_px = _css_pixel_length(
            parent_width, property_name="parent width",
        )
    else:
        raise MatrixTemplateError(
            "HyperFrames reference template parent width is missing"
        )
    parent_padding_left, parent_padding_right = _css_horizontal_padding(
        parent_declarations
    )
    parent_content_width = (
        parent_width_px - parent_padding_left - parent_padding_right
    )

    family = "Noto Sans SC"
    font_size = None
    font_weight = 400
    shorthand = declarations.get("font")
    if shorthand:
        weight_match = re.match(
            r"\s*(normal|bold|[1-9]00)\s+", shorthand,
            flags=re.IGNORECASE,
        )
        if weight_match:
            raw_weight = weight_match.group(1).lower()
            font_weight = (
                400 if raw_weight == "normal"
                else 700 if raw_weight == "bold"
                else int(raw_weight)
            )
        match = re.search(
            r"(?:^|\s)([0-9]+(?:\.[0-9]+)?)px"
            r"(?:/[^\s]+)?\s+(.+)$",
            shorthand,
        )
        if not match:
            raise MatrixTemplateError(
                "HyperFrames reference template font shorthand is unsupported"
            )
        font_size = float(match.group(1))
        family = _css_font_family(match.group(2))
    if declarations.get("font-size"):
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)px", declarations["font-size"]
        )
        if not match:
            raise MatrixTemplateError(
                "HyperFrames reference template font size is unsupported"
            )
        font_size = float(match.group(1))
    if declarations.get("font-family"):
        family = _css_font_family(declarations["font-family"])
    if declarations.get("font-weight"):
        raw_weight = declarations["font-weight"].strip().lower()
        if raw_weight in {"normal", "bold"}:
            font_weight = {"normal": 400, "bold": 700}[raw_weight]
        elif re.fullmatch(r"[1-9]00", raw_weight):
            font_weight = int(raw_weight)
        else:
            raise MatrixTemplateError(
                "HyperFrames reference template font weight is unsupported"
            )
    if font_size is None:
        raise MatrixTemplateError(
            "HyperFrames reference template font size is missing"
        )

    letter_spacing = REFERENCE_LETTER_SPACING_EM
    if declarations.get("letter-spacing"):
        match = re.fullmatch(
            r"(-?[0-9]*\.?[0-9]+)em", declarations["letter-spacing"]
        )
        if not match:
            raise MatrixTemplateError(
                "HyperFrames reference template letter spacing is unsupported"
            )
        letter_spacing = float(match.group(1))

    stroke = 0.0
    if declarations.get("-webkit-text-stroke"):
        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)px",
            declarations["-webkit-text-stroke"],
        )
        if not match:
            raise MatrixTemplateError(
                "HyperFrames reference template text stroke is unsupported"
            )
        stroke = float(match.group(1))

    layer_max_width = REFERENCE_TEXT_MAX_WIDTH_PX
    if declarations.get("max-width"):
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)px", declarations["max-width"]
        )
        if match:
            layer_max_width = float(match.group(1))
        elif declarations["max-width"] == "none":
            layer_max_width = parent_content_width
        else:
            raise MatrixTemplateError(
                "HyperFrames reference template max width is unsupported"
            )
    padding_left, padding_right = _css_horizontal_padding(declarations)
    max_width = (
        min(layer_max_width, parent_content_width)
        - padding_left - padding_right
    )
    if (
        not 8 <= font_size <= 240
        or not 100 <= font_weight <= 900
        or not 100 <= max_width <= 996
    ):
        raise MatrixTemplateError(
            "HyperFrames reference template text metrics are unsafe"
        )
    return {
        "family": family,
        "font_size_px": int(font_size),
        "font_weight": int(font_weight),
        "stroke_px": int(stroke),
        "letter_spacing_em": letter_spacing,
        "max_width_px": int(max_width),
        "max_lines": int(max_lines),
    }


def _font_selection(template_id: str, job_id: str,
                    private_families: set[str] | frozenset[str] = frozenset()) -> dict:
    options = list(FONT_VARIANTS.get(template_id) or FONT_VARIANTS["full-overlay-bold"])
    options.extend(
        item for item in PRIVATE_FONT_VARIANTS.get(template_id, ())
        if item[1] in private_families and item[2] in private_families | BASE_FONT_FAMILIES
    )
    digest = hashlib.sha256(f"{template_id}:{job_id}".encode("utf-8")).digest()
    variant, top_font, bottom_font = options[int.from_bytes(digest[:4], "big") % len(options)]
    return {
        "variant": variant,
        "top_font": top_font,
        "bottom_font": bottom_font,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_private_fonts(root: Path | None) -> dict[str, dict]:
    if root is None:
        return {}
    if root.is_symlink():
        raise MatrixTemplateError("private font directory must not be a symlink")
    root = root.resolve()
    manifest_path = root / "sources.json"
    if not manifest_path.exists():
        return {}
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MatrixTemplateError("private font manifest is unsafe")
    manifest = _read_json(manifest_path)
    records = manifest.get("fonts")
    if manifest.get("schema_version") != 1 or not isinstance(records, list):
        raise MatrixTemplateError("private font manifest must use schema version 1")
    result = {}
    filenames = set()
    for item in records:
        if not isinstance(item, dict):
            raise MatrixTemplateError("invalid private font record")
        family = str(item.get("family") or "")
        filename = str(item.get("file") or "")
        expected = str(item.get("sha256") or "").lower()
        if family not in PRIVATE_FONT_FAMILIES or family in result:
            raise MatrixTemplateError("private font family is unknown or duplicated")
        if (
            Path(filename).name != filename
            or Path(filename).suffix.lower() not in {".ttf", ".otf", ".ttc"}
            or filename in filenames
            or not SHA_RE.fullmatch(expected)
            or item.get("authorized") is not True
        ):
            raise MatrixTemplateError("private font record is incomplete or unsafe")
        path = root / filename
        if path.is_symlink() or not path.is_file() or _file_sha256(path) != expected:
            raise MatrixTemplateError("private font file is missing or has changed")
        result[family] = {"family": family, "file": filename, "sha256": expected, "path": path}
        filenames.add(filename)
    return result


def _font_bundle_fingerprint(fonts: dict[str, dict]) -> str:
    records = [{key: item[key] for key in ("family", "file", "sha256")}
               for _, item in sorted(fonts.items())]
    return hashlib.sha256(_json_bytes({"fonts": records})).hexdigest()


def _load_bundled_fonts(skill_root: Path) -> dict[str, dict]:
    root = skill_root / "assets/fonts"
    manifest = _read_json(root / "sources.json")
    records = manifest.get("fonts")
    if not isinstance(records, list):
        raise MatrixTemplateError("stable Skill font manifest is invalid")
    result = {}
    for item in records:
        family = str(item.get("family") or "") if isinstance(item, dict) else ""
        if family not in BASE_FONT_FAMILIES:
            continue
        filename = str(item.get("file") or "")
        expected = str(item.get("sha256") or "").lower()
        path = root / filename
        if (
            family in result or Path(filename).name != filename
            or path.is_symlink() or not path.is_file()
            or not SHA_RE.fullmatch(expected) or _file_sha256(path) != expected
        ):
            raise MatrixTemplateError("stable Skill font bundle failed verification")
        result[family] = {"family": family, "file": filename, "sha256": expected, "path": path}
    if set(result) != BASE_FONT_FAMILIES:
        raise MatrixTemplateError("stable Skill font bundle is incomplete")
    return result


class MatrixTemplateError(RuntimeError):
    pass


class QueueCapacityError(MatrixTemplateError):
    pass


class DiskCapacityError(MatrixTemplateError):
    pass


def runtime_build_id() -> str:
    path = Path(__file__).resolve().parent / "BUILD_ID"
    try:
        value = path.read_text(encoding="ascii").strip().lower()
    except OSError:
        return "development"
    return value if SHA_RE.fullmatch(value) else "invalid"


def _now() -> int:
    return int(time.time())


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MatrixTemplateError(f"JSON root must be an object: {path.name}")
    return value


def _duration(top: str, bottom: str, requested) -> float:
    visible = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", top + bottom))
    minimum = max(8.0, visible / 5.0 + 1.5)
    if requested not in (None, ""):
        try:
            minimum = max(minimum, float(requested))
        except (TypeError, ValueError) as exc:
            raise ValueError("duration must be numeric") from exc
    if minimum > 15:
        raise ValueError("文案过长，请缩短标题或行动文案")
    return round(minimum, 3)


def _required_visuals(duration: float) -> int:
    return 2 if duration <= 10 else 3


def _visual_width(value: str) -> float:
    return sum(
        0.35 if char.isspace() else 0.62 if char.isascii() else 1.0
        for char in value
    )


_PROTECTED_TERMS = {
    "团队", "组团队", "关键词", "创业者", "评论区", "店员", "小时", "接单",
    "老板", "开店", "凌晨", "个人", "资料", "活动", "人工智能",
    "智能体", "资源共享", "AI 当店员", "AI员工", "24 小时", "1个人",
}
_PROTECTED_BREAK_PAIRS = {
    term[index:index + 2]
    for term in _PROTECTED_TERMS
    for index in range(len(term) - 1)
}
_PROTECTED_RIGHT_SUFFIXES = frozenset("者们队词员家区店圈群会局型式端率量性化力感")


def _balanced_title(text: str, max_chars: int, max_lines: int) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    counters = "个家人位名款套种项台年月日天次岁"
    tokens: list[tuple[str, str]] = []
    cursor = 0
    pending_space = False
    while cursor < len(compact):
        if compact[cursor].isspace():
            pending_space = True
            cursor += 1
            continue
        match = re.match(r"[+&./_-]?[A-Za-z0-9]+(?:[+&./_-][A-Za-z0-9]+)*", compact[cursor:])
        if match:
            token = match.group(0)
            cursor += len(token)
            if cursor < len(compact) and compact[cursor] in counters:
                token += compact[cursor]
                cursor += 1
            separator = " " if pending_space and tokens else ""
            tokens.append((token, separator))
            pending_space = False
            continue
        char = compact[cursor]
        cursor += 1
        tokens.append((char, " " if pending_space and tokens else ""))
        pending_space = False

    def boundary_penalty(left: str, right: str, separator: str) -> float:
        left_char, right_char = left[-1], right[0]
        if right_char in "，。！？；：、,.!?;:)]}）】》」』+%％":
            return 1000.0
        if left_char in "([{（【《「『+":
            return 1000.0
        if (
            right_char in _PROTECTED_RIGHT_SUFFIXES
            or left_char + right_char in _PROTECTED_BREAK_PAIRS
        ):
            return 1000.0
        if separator:
            return -1.0
        if (
            left_char.isascii() and right_char.isascii()
            and (left_char.isalnum() or left_char in "+_&./-")
            and (right_char.isalnum() or right_char in "+_&./-")
        ):
            return 1000.0
        if (
            left_char in "0123456789一二三四五六七八九十几两" and right_char in counters
        ) or left_char + right_char in {
            "也能", "都能", "可以", "不会", "不能", "需要", "想要",
            "已经", "正在", "还是", "就是", "如果", "所以", "但是",
            "而且", "以及",
        }:
            return 1000.0
        if left_char in "。！？!?；;":
            return -20.0
        if left_char in "，,：:":
            return -3.0
        return 0.0

    total_width = sum(
        _visual_width(value) + (_visual_width(separator) if index else 0.0)
        for index, (value, separator) in enumerate(tokens)
    )
    comfortable_width = max(1.0, max_chars * 0.82)
    target_lines = min(
        max(1, max_lines), max(1, math.ceil(total_width / comfortable_width))
    )
    ideal = total_width / target_lines
    line_limit = max(
        float(max_chars), max(_visual_width(value) for value, _ in tokens),
        math.ceil(ideal) + 3,
    )
    for _ in range(max(1, len(compact))):
        states = {(0, 0): (0.0, [])}
        for line_index in range(target_lines):
            for start in range(len(tokens)):
                state = states.get((line_index, start))
                if state is None:
                    continue
                remaining = target_lines - line_index - 1
                width = 0.0
                for end in range(start + 1, len(tokens) + 1):
                    if len(tokens) - end < remaining:
                        break
                    value, separator = tokens[end - 1]
                    if end - 1 > start:
                        width += _visual_width(separator)
                    width += _visual_width(value)
                    if width > line_limit + 0.001:
                        break
                    penalty = boundary_penalty(
                        tokens[end - 1][0], tokens[end][0], tokens[end][1]
                    ) if end < len(tokens) else 0.0
                    if penalty >= 1000:
                        continue
                    score = state[0] + (width - ideal) ** 2 + penalty
                    if line_index == target_lines - 1 and width < ideal * 0.58:
                        score += (ideal - width) ** 2 * 4
                    key = (line_index + 1, end)
                    if key not in states or score < states[key][0]:
                        states[key] = (score, state[1] + [end])
        result = states.get((target_lines, len(tokens)))
        if result:
            lines, start = [], 0
            for end in result[1]:
                parts = [tokens[start][0]]
                for value, separator in tokens[start + 1:end]:
                    parts.extend((separator, value))
                line = "".join(parts).strip()
                if not line:
                    return compact
                lines.append(line)
                start = end
            return "\n".join(lines)
        line_limit += 1
    return compact


def _semantic_break_penalty(value: str, index: int) -> float | None:
    if index >= len(value):
        return 0.0
    left, right = value[index - 1], value[index]
    if any(
        match.start() < index < match.end()
        for match in _NUMERIC_PHRASE_RE.finditer(value)
    ):
        return None
    if right.isspace():
        return None
    if right in "，。！？；：、,.!?;:)]}）】》」』+%％":
        return None
    if left in "([{（【《「『+":
        return None
    if (
        right in _PROTECTED_RIGHT_SUFFIXES
        or left + right in _PROTECTED_BREAK_PAIRS
    ):
        return None
    if (
        left.isascii() and right.isascii()
        and (left.isalnum() or left in "+_&./-")
        and (right.isalnum() or right in "+_&./-")
    ):
        return None
    boundary = left
    if left.isspace():
        cursor = index - 1
        while cursor > 0 and value[cursor - 1].isspace():
            cursor -= 1
        boundary = value[cursor - 1] if cursor else ""
    if boundary in "。！？!?；;":
        return -30.0
    if boundary in "，,：:":
        return -12.0
    if boundary == "、":
        return -5.0
    if left.isspace():
        return -3.0
    return 0.0


def _semantic_layers(text: str, max_chars: int, max_layers: int) -> list[str]:
    compact = " ".join(str(text or "").split())
    if not compact:
        return []
    width_limit = max(1.0, float(max_chars))
    layer_limit = max(1, int(max_layers))
    total_width = _visual_width(compact)
    if total_width > width_limit * layer_limit + 0.001:
        raise ValueError("文案超过模板文字层宽度预算")

    preferred_layers = min(
        layer_limit,
        max(1, math.ceil(total_width / max(1.0, width_limit * 0.9))),
    )
    for target_layers in range(preferred_layers, layer_limit + 1):
        ideal = total_width / target_layers
        states: dict[tuple[int, int], tuple[float, list[int]]] = {
            (0, 0): (0.0, [])
        }
        for layer_index in range(target_layers):
            for start in range(len(compact)):
                state = states.get((layer_index, start))
                if state is None:
                    continue
                for end in range(start + 1, len(compact) + 1):
                    segment = compact[start:end]
                    width = _visual_width(segment)
                    if width > width_limit + 0.001:
                        break
                    if not segment.strip():
                        continue
                    penalty = _semantic_break_penalty(compact, end)
                    if penalty is None:
                        continue
                    remaining_layers = target_layers - layer_index - 1
                    remaining_width = _visual_width(compact[end:])
                    if remaining_width > remaining_layers * width_limit + 0.001:
                        continue
                    if remaining_layers and not compact[end:].strip():
                        continue
                    if not remaining_layers and end != len(compact):
                        continue
                    score = state[0] + (width - ideal) ** 2 + penalty
                    if layer_index == target_layers - 1 and width < ideal * 0.55:
                        score += (ideal - width) ** 2 * 1.5
                    key = (layer_index + 1, end)
                    if key not in states or score < states[key][0]:
                        states[key] = (score, state[1] + [end])
        selected = states.get((target_layers, len(compact)))
        if selected is None:
            continue
        result, start = [], 0
        for end in selected[1]:
            result.append(compact[start:end])
            start = end
        if (
            "".join(result) == compact
            and all(_visual_width(item) <= width_limit + 0.001 for item in result)
        ):
            return result
    raise ValueError("文案无法在模板文字层内安全断句")


_REFERENCE_TOP_GROUP_SIZES = {
    2: {
        1: (1, 0, 0),
        2: (1, 1, 0),
        3: (1, 2, 0),
        4: (2, 2, 0),
        5: (2, 3, 0),
        6: (2, 4, 0),
    },
    3: {
        1: (1, 0, 0),
        2: (1, 1, 0),
        3: (1, 1, 1),
        4: (1, 2, 1),
        5: (2, 2, 1),
        6: (2, 2, 2),
    },
}
_REFERENCE_BOTTOM_GROUP_SIZES = {
    1: (0, 1),
    2: (1, 1),
    3: (1, 2),
}


def _pack_reference_lines(
    lines: list[str], group_sizes: dict[int, tuple[int, ...]]
) -> list[list[str]]:
    sizes = group_sizes[len(lines)]
    groups, cursor = [], 0
    for size in sizes:
        groups.append(lines[cursor:cursor + size])
        cursor += size
    return groups


def _reference_text_layout(
    top: str, bottom: str, top_layer_count: int = 3
) -> tuple[dict[str, str], dict[str, str]]:
    if top_layer_count not in _REFERENCE_TOP_GROUP_SIZES:
        raise ValueError("HyperFrames 模板顶部文字层配置无效")
    try:
        top_lines = _semantic_layers(top, 12, 6)
    except ValueError as exc:
        raise ValueError("HyperFrames 模板顶部文案过长，请缩短后重试") from exc
    try:
        bottom_lines = _semantic_layers(bottom, 15, 3)
    except ValueError as exc:
        raise ValueError("HyperFrames 模板底部文案过长，请缩短后重试") from exc
    top_groups = _pack_reference_lines(
        top_lines, _REFERENCE_TOP_GROUP_SIZES[top_layer_count]
    )
    bottom_groups = _pack_reference_lines(
        bottom_lines, _REFERENCE_BOTTOM_GROUP_SIZES
    )
    keys = ("top1", "top2", "top3", "bottom1", "bottom2")
    groups = top_groups + bottom_groups
    source_text = {
        key: "".join(group) for key, group in zip(keys, groups)
    }
    display_text = {
        key: "\n".join(_hide_reference_edge_punctuation(line) for line in group)
        for key, group in zip(keys, groups)
    }
    return source_text, display_text


def _reference_text_layers(
    top: str, bottom: str, top_layer_count: int = 3
) -> dict[str, str]:
    return _reference_text_layout(top, bottom, top_layer_count)[0]


_REFERENCE_EDGE_PUNCTUATION = "，。！？；：、,.!?;:|｜"
_REFERENCE_EDGE_PATTERN = re.compile(
    rf"^[{re.escape(_REFERENCE_EDGE_PUNCTUATION)}]+"
    rf"|[{re.escape(_REFERENCE_EDGE_PUNCTUATION)}]+$"
)


def _hide_reference_edge_punctuation(value: str) -> str:
    return _REFERENCE_EDGE_PATTERN.sub("", str(value or "").strip()).strip()


def _reference_display_layers(layers: dict[str, str]) -> dict[str, str]:
    return {
        key: "\n".join(
            _hide_reference_edge_punctuation(line)
            for line in str(layers.get(key) or "").splitlines()
        )
        for key in ("top1", "top2", "top3", "bottom1", "bottom2")
    }


def _reference_semantic_source_sha256(top: str, bottom: str) -> str:
    return hashlib.sha256(
        (str(top) + "\0" + str(bottom)).encode("utf-8")
    ).hexdigest()


def _normalize_reference_breaks(value, text: str, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError(f"HyperFrames {label}语义断点无效")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"HyperFrames {label}语义断点无效")
    breaks = sorted(set(value))
    if breaks != value or any(item < 0 or item >= len(text) - 1 for item in breaks):
        raise ValueError(f"HyperFrames {label}语义断点无效")
    return [
        item for item in breaks
        if _semantic_break_penalty(text, item + 1) is not None
    ]


def _normalize_reference_semantic_layout(value, top: str, bottom: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "version", "model", "source_sha256", "top1_end",
        "top_break_after", "bottom_break_after",
    }:
        raise ValueError("HyperFrames 语义排版参数无效")
    if value.get("version") != REFERENCE_SEMANTIC_LAYOUT_VERSION:
        raise ValueError("HyperFrames 语义排版版本无效")
    model = str(value.get("model") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", model):
        raise ValueError("HyperFrames 语义排版模型无效")
    if not hmac.compare_digest(
        str(value.get("source_sha256") or ""),
        _reference_semantic_source_sha256(top, bottom),
    ):
        raise ValueError("HyperFrames 语义排版与原文不匹配")
    top_breaks = _normalize_reference_breaks(
        value.get("top_break_after"), top, "顶部",
    )
    bottom_breaks = _normalize_reference_breaks(
        value.get("bottom_break_after"), bottom, "底部",
    )
    top1_end = value.get("top1_end")
    if (
        isinstance(top1_end, bool) or not isinstance(top1_end, int)
        or not 0 <= top1_end < len(top)
        or (top1_end != len(top) - 1 and top1_end not in top_breaks)
    ):
        raise ValueError("HyperFrames top1 语义边界无效")
    return {
        "version": REFERENCE_SEMANTIC_LAYOUT_VERSION,
        "model": model,
        "source_sha256": _reference_semantic_source_sha256(top, bottom),
        "top1_end": top1_end,
        "top_break_after": top_breaks,
        "bottom_break_after": bottom_breaks,
    }


def _reference_duration(job_id: str, template_id: str) -> int:
    digest = hashlib.sha256(f"{job_id}:{template_id}".encode("utf-8")).digest()
    return 8 + int.from_bytes(digest[:8], "big") % 8


def _format_reference_seconds(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _reference_segment_timing(
    total_duration: float, media_durations: list[float]
) -> tuple[list[float], list[float]]:
    total = float(total_duration)
    if not 8 <= total <= 15 or len(media_durations) != 3:
        raise MatrixTemplateError("HyperFrames 模板素材时间轴参数无效")
    capacities = [
        max(0.0, float(value) - REFERENCE_MEDIA_SAFETY_SECONDS)
        for value in media_durations
    ]
    if (
        any(value < REFERENCE_MIN_SEGMENT_SECONDS for value in capacities)
        or sum(capacities) + 0.001 < total
    ):
        raise MatrixTemplateError("HyperFrames 模板素材总时长不足")

    durations = [0.0, 0.0, 0.0]
    remaining = total
    active = {0, 1, 2}
    while active:
        share = remaining / len(active)
        capped = [index for index in active if capacities[index] < share - 1e-9]
        if not capped:
            for index in active:
                durations[index] = share
            remaining = 0.0
            break
        for index in capped:
            durations[index] = capacities[index]
            remaining -= capacities[index]
            active.remove(index)

    durations[-1] += total - sum(durations)
    if any(
        value < REFERENCE_MIN_SEGMENT_SECONDS
        or value > capacities[index] + 0.001
        for index, value in enumerate(durations)
    ):
        raise MatrixTemplateError("HyperFrames 模板素材时长分配失败")
    starts = [0.0, durations[0], durations[0] + durations[1]]
    return starts, durations


def _rewrite_reference_timeline(
    html: str, total_duration: float,
    starts: list[float], durations: list[float],
) -> str:
    if len(starts) != 3 or len(durations) != 3:
        raise MatrixTemplateError("HyperFrames 模板素材时间轴参数无效")

    def rewrite_element(source: str, element_id: str,
                        start: float, duration: float) -> str:
        pattern = re.compile(
            rf'<(?:video|audio|section)\b[^>]*\bid="{re.escape(element_id)}"[^>]*>'
        )
        matches = list(pattern.finditer(source))
        if len(matches) != 1:
            raise MatrixTemplateError("HyperFrames 模板时间轴元素发生变化")
        tag = matches[0].group(0)
        for attribute, value in (
            ("data-start", start), ("data-duration", duration),
        ):
            replacement = rf'\g<1>{_format_reference_seconds(value)}\g<2>'
            tag, count = re.subn(
                rf'(\s{attribute}=")[^"]*(")', replacement, tag, count=1
            )
            if count != 1:
                raise MatrixTemplateError("HyperFrames 模板时间轴属性发生变化")
        return source[:matches[0].start()] + tag + source[matches[0].end():]

    result = html
    for index, element_id in enumerate(("videoA", "videoB", "videoC")):
        result = rewrite_element(
            result, element_id, starts[index], durations[index]
        )
    result = rewrite_element(result, "bgm", 0.0, total_duration)
    result = rewrite_element(result, "typography", 0.0, total_duration)

    timing_js = (
        "      const segmentStarts = ["
        + ", ".join(_format_reference_seconds(value) for value in starts)
        + "];\n      const segmentDurations = ["
        + ", ".join(_format_reference_seconds(value) for value in durations)
        + "];"
    )
    if result.count(REFERENCE_DYNAMIC_TIMING_JS) != 1:
        raise MatrixTemplateError("HyperFrames 模板动态时间轴声明发生变化")
    return result.replace(REFERENCE_DYNAMIC_TIMING_JS, timing_js)


def _rewrite_reference_bgm_source(html: str, source: str) -> str:
    if not REFERENCE_BGM_SOURCE_RE.fullmatch(str(source or "")):
        raise MatrixTemplateError("HyperFrames 模板背景音乐路径无效")
    pattern = re.compile(r'<audio\b[^>]*\bid="bgm"[^>]*>')
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise MatrixTemplateError("HyperFrames 模板背景音乐元素发生变化")
    tag = matches[0].group(0)
    if tag.count(' data-var-src="bgm"') != 1:
        raise MatrixTemplateError("HyperFrames 模板背景音乐变量声明发生变化")
    tag, count = re.subn(
        r'(\ssrc=")[^"]*(")',
        lambda match: match.group(1) + source + match.group(2),
        tag,
        count=1,
    )
    if count != 1:
        raise MatrixTemplateError("HyperFrames 模板背景音乐来源声明发生变化")
    return html[:matches[0].start()] + tag + html[matches[0].end():]


def _reference_private_font_style(
    variant: str, fixed_fonts: dict[str, dict]
) -> str:
    if not fixed_fonts:
        return ""
    if not re.fullmatch(r"v(?:0[1-9]|1[0-7])", str(variant or "")):
        raise MatrixTemplateError("HyperFrames 固定私有字体模板标识无效")
    declarations = []
    overrides = []
    seen_aliases = set()
    for layer, item in sorted(fixed_fonts.items()):
        if layer not in REFERENCE_TEXT_LAYER_IDS or not isinstance(item, dict):
            raise MatrixTemplateError("HyperFrames 固定私有字体配置无效")
        alias = str(item.get("alias") or "")
        filename = str(item.get("file") or "")
        font_size_px = item.get("font_size_px")
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", alias)
            or Path(filename).name != filename
            or Path(filename).suffix.lower() not in {".ttf", ".otf", ".ttc"}
            or (
                font_size_px is not None
                and (
                    isinstance(font_size_px, bool)
                    or not isinstance(font_size_px, int)
                    or not 8 <= font_size_px <= 240
                )
            )
        ):
            raise MatrixTemplateError("HyperFrames 固定私有字体元数据无效")
        if alias not in seen_aliases:
            font_format = {
                ".ttf": "truetype", ".otf": "opentype", ".ttc": "collection",
            }[Path(filename).suffix.lower()]
            declarations.append(
                f'@font-face{{font-family:"{alias}";'
                f'src:url("assets/fonts/{filename}") format("{font_format}");'
                'font-display:block}'
            )
            seen_aliases.add(alias)
        properties = [f'font-family:"{alias}"!important']
        if font_size_px is not None:
            properties.append(f"font-size:{font_size_px}px!important")
        overrides.append(f'.{variant} .{layer}{{{";".join(properties)}}}')
    return (
        f'<style id="{REFERENCE_PRIVATE_FONT_STYLE_ID}">'
        + "".join(declarations + overrides)
        + "</style>"
    )


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "delivered_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN delivered_at INTEGER")
            if "cleaned_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN cleaned_at INTEGER")
            db.execute("UPDATE jobs SET status='pending', error=NULL WHERE status='running'")
            db.execute("""CREATE TABLE IF NOT EXISTS batch_material_selections(
                job_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                materials TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS batch_material_reservations(
                batch_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                job_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(batch_id,sha256)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_batch_material_job ON batch_material_reservations(job_id)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, request_id: str, payload: dict, admission_guard=None,
               freeze_payload=None) -> tuple[dict, bool]:
        now = _now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                existing_payload = json.loads(existing["payload"])
                existing_request = {
                    key: value for key, value in existing_payload.items()
                    if not key.startswith("_")
                }
                if existing_request != payload:
                    raise ValueError("request_id already belongs to another payload")
                return self.public(existing), False
            waiting = int(db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0])
            if waiting >= MAX_WAITING_JOBS:
                raise QueueCapacityError("任务队列已满")
            if admission_guard is not None:
                admission_guard()
            job_id = uuid.uuid4().hex
            stored_payload = freeze_payload(job_id, dict(payload)) if freeze_payload else payload
            db.execute(
                """INSERT INTO jobs(
                    id,request_id,status,payload,result,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, request_id, "pending", json.dumps(stored_payload, ensure_ascii=False),
                 None, None, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self.public(row), True

    def get(self, job_id: str):
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def get_by_request_id(self, request_id: str):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()

    def pending_ids(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE status='pending' ORDER BY created_at,id"
            )]

    def batch_material_selection(self, job_id: str) -> list[dict] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT materials FROM batch_material_selections WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return json.loads(row["materials"]) if row else None

    def batch_used_visuals(self, batch_id: str) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT sha256 FROM batch_material_reservations WHERE batch_id=? ORDER BY sha256",
                (batch_id,),
            )]

    def reserve_batch_materials(self, batch_id: str, job_id: str,
                                materials: list[dict]) -> None:
        visual_shas = [
            str(item.get("sha256") or "").lower() for item in materials
            if item.get("media_type") in {"image", "video"}
        ]
        if not visual_shas or any(not SHA_RE.fullmatch(value) for value in visual_shas):
            raise MatrixTemplateError("batch visual material reservation is invalid")
        now = _now()
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT materials,batch_id FROM batch_material_selections WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if existing:
                    if existing["batch_id"] != batch_id or json.loads(existing["materials"]) != materials:
                        raise MatrixTemplateError("batch material selection conflict")
                    return
                for sha256 in visual_shas:
                    db.execute(
                        "INSERT INTO batch_material_reservations(batch_id,sha256,job_id,created_at) VALUES(?,?,?,?)",
                        (batch_id, sha256, job_id, now),
                    )
                db.execute(
                    "INSERT INTO batch_material_selections(job_id,batch_id,materials,created_at) VALUES(?,?,?,?)",
                    (job_id, batch_id, json.dumps(materials, ensure_ascii=False), now),
                )
        except sqlite3.IntegrityError as exc:
            raise MatrixTemplateError("同批次视觉素材重复，请重新生成") from exc

    def cleanup_candidates(self, *, now: int, retention_seconds: int,
                           delivery_grace_seconds: int, limit: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("""
                SELECT * FROM jobs
                WHERE cleaned_at IS NULL
                  AND status IN ('completed','failed')
                  AND (
                    updated_at <= ?
                    OR (delivered_at IS NOT NULL AND delivered_at <= ?)
                  )
                ORDER BY updated_at,id
                LIMIT ?
            """, (now - retention_seconds, now - delivery_grace_seconds, limit)))

    def mark_delivered(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET delivered_at=COALESCE(delivered_at,?) WHERE id=? AND status='completed'",
                (_now(), job_id),
            )

    def mark_cleaned(self, job_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET cleaned_at=? WHERE id=? AND status IN ('completed','failed')",
                (_now(), job_id),
            )

    def update(self, job_id: str, status: str, *, result=None, error=None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False) if result else None,
                 str(error or "")[:500] or None, _now(), job_id),
            )

    @staticmethod
    def public(row) -> dict:
        result = json.loads(row["result"]) if row["result"] else None
        value = {
            "job_id": row["id"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if result:
            value["result"] = result
        if row["error"]:
            value["error"] = row["error"]
        if "cleaned_at" in row.keys() and row["cleaned_at"]:
            value["cleaned_at"] = row["cleaned_at"]
        return value


class MatrixTemplateService:
    def __init__(self, *, data_root: Path, skill_root: Path, library_url: str,
                 library_token: str, python: str = sys.executable,
                 private_font_root: Path | None = None,
                 reference_skill_root: Path | None = None,
                 hyperframes_cli: Path | None = None,
                 hyperframes_gsap: Path | None = None,
                 hyperframes_browser: Path | None = None,
                 hyperframes_concurrency: int = DEFAULT_HYPERFRAMES_CONCURRENCY,
                 hyperframes_total_timeout_seconds: int = DEFAULT_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS,
                 hyperframes_slot_timeout_seconds: int = DEFAULT_HYPERFRAMES_SLOT_TIMEOUT_SECONDS,
                 concurrency: int = 1,
                 start_worker: bool = True,
                 retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                 delivery_grace_seconds: int = DEFAULT_DELIVERY_GRACE_SECONDS,
                 cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS,
                 cleanup_batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
                 disk_high_water_percent: float = DEFAULT_DISK_HIGH_WATER_PERCENT):
        self.data_root = data_root.resolve()
        self.skill_root = skill_root.resolve()
        self.library_url = library_url.rstrip("/")
        self.library_token = library_token
        parsed_library = urlsplit(self.library_url)
        if (
            parsed_library.scheme != "http"
            or parsed_library.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_library.path not in {"", "/"}
            or parsed_library.username or parsed_library.password
            or parsed_library.query or parsed_library.fragment
        ):
            raise MatrixTemplateError("material library URL must be loopback HTTP")
        if not self.library_token:
            raise MatrixTemplateError("material library token is missing")
        self.python = python
        self.private_font_root = private_font_root.resolve() if private_font_root else None
        self.bundled_fonts = _load_bundled_fonts(self.skill_root)
        self.private_fonts = _load_private_fonts(private_font_root)
        self.private_font_fingerprint = _font_bundle_fingerprint(self.private_fonts)
        self.reference_skill_root = (
            reference_skill_root.resolve() if reference_skill_root else None
        )
        self.reference_pack_root = None
        self.reference_templates: dict[str, dict] = {}
        self.reference_semantic_layouts: dict[str, dict] = {}
        self.reference_fonts: dict[str, dict] = {}
        self.reference_font_fingerprint = _font_bundle_fingerprint({})
        self.reference_measure_fonts: dict[
            tuple[str, int, int], ImageFont.FreeTypeFont
        ] = {}
        self.hyperframes_cli = hyperframes_cli.resolve() if hyperframes_cli else None
        self.hyperframes_gsap = hyperframes_gsap.resolve() if hyperframes_gsap else None
        self.hyperframes_browser = (
            hyperframes_browser.resolve() if hyperframes_browser else None
        )
        self.hyperframes_concurrency = int(hyperframes_concurrency)
        if not 1 <= self.hyperframes_concurrency <= 2:
            raise MatrixTemplateError("HyperFrames concurrency must be between 1 and 2")
        self.hyperframes_total_timeout_seconds = int(hyperframes_total_timeout_seconds)
        self.hyperframes_slot_timeout_seconds = int(hyperframes_slot_timeout_seconds)
        if not 120 <= self.hyperframes_total_timeout_seconds <= 1100:
            raise MatrixTemplateError("HyperFrames total timeout must be between 120 and 1100 seconds")
        if not 1 <= self.hyperframes_slot_timeout_seconds < self.hyperframes_total_timeout_seconds:
            raise MatrixTemplateError("HyperFrames slot timeout is invalid")
        self.hyperframes_slots = threading.BoundedSemaphore(self.hyperframes_concurrency)
        self.concurrency = int(concurrency)
        if not 1 <= self.concurrency <= 5:
            raise MatrixTemplateError("concurrency must be between 1 and 5")
        self.retention_seconds = max(60, int(retention_seconds))
        self.delivery_grace_seconds = max(60, int(delivery_grace_seconds))
        self.cleanup_interval_seconds = max(1, int(cleanup_interval_seconds))
        self.cleanup_batch_size = max(1, int(cleanup_batch_size))
        self.disk_high_water_percent = float(disk_high_water_percent)
        if not 1 <= self.disk_high_water_percent <= 100:
            raise MatrixTemplateError("disk high-water percent must be between 1 and 100")
        self.store = JobStore(self.data_root / "jobs.db")
        # Recovery may legitimately contain one formerly-running job plus the
        # full waiting allowance. Admission is bounded transactionally in DB;
        # the in-memory recovery queue must not impose a second, smaller cap.
        self.jobs: queue.Queue[str] = queue.Queue()
        self.queue_lock = threading.Lock()
        self.queued_jobs: set[str] = set()
        self.active_jobs: set[str] = set()
        self.stop_event = threading.Event()
        self.worker_degraded = threading.Event()
        self.degraded_lock = threading.Lock()
        self.degraded_jobs: set[str] = set()
        self.process_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.batch_material_lock = threading.Lock()
        self.active_downloads: set[str] = set()
        self.active_processes: set[subprocess.Popen] = set()
        self.active_process = None
        self.workers = []
        self.worker = None
        self.cleanup_worker = None
        self.workers_expected = start_worker
        self.catalog = self._load_catalog()
        if self.reference_skill_root is not None:
            self.catalog.extend(self._load_reference_catalog())
        self.templates = {item["id"]: item for item in self.catalog}
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._purge_trash()
        self.cleanup_once()
        for job_id in self.store.pending_ids():
            self._enqueue(job_id)
        if start_worker:
            self.workers = [
                threading.Thread(
                    target=self._worker, name=f"matrix-template-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.concurrency)
            ]
            self.worker = self.workers[0]
            for worker in self.workers:
                worker.start()
            self.cleanup_worker = threading.Thread(target=self._cleanup_worker, daemon=True)
            self.cleanup_worker.start()

    def _load_catalog(self) -> list[dict]:
        path = self.skill_root / "assets/templates/catalog.json"
        catalog = _read_json(path)
        if catalog.get("version") != 1 or not isinstance(catalog.get("templates"), list):
            raise MatrixTemplateError("invalid template catalog")
        result = []
        text_limits = {}
        for item in catalog["templates"]:
            if not isinstance(item, dict):
                raise MatrixTemplateError("invalid template record")
            template_id = str(item.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", template_id):
                raise MatrixTemplateError("invalid template id")
            layout = item.get("layout") or {}
            if not isinstance(layout, dict):
                raise MatrixTemplateError("invalid template layout")
            text_limits[template_id] = (
                max(6, int(layout.get("top_max_chars", 12))),
                min(4, max(1, int(layout.get("top_max_lines", 3)))),
            )
            result.append({
                "id": template_id,
                "name": str(item.get("name") or template_id)[:40],
                "description": str(item.get("description") or "")[:160],
                "tags": [str(tag)[:20] for tag in (item.get("tags") or [])[:8]],
                "engine": "ffmpeg",
                "font_mode": "selectable",
                "font_selectable": True,
            })
        if len(result) != 2 or len({item["id"] for item in result}) != 2:
            raise MatrixTemplateError("expected exactly 2 unique templates")
        required = {"full-overlay-bold", "poster-split"}
        if {item["id"] for item in result} != required:
            raise MatrixTemplateError("required private-domain templates are missing")
        self.template_text_limits = text_limits
        return result

    def _load_reference_catalog(self) -> list[dict]:
        pack_root = (
            self.reference_skill_root
            / "assets/templates"
            / REFERENCE_PACK_ID
        )
        manifest_path = pack_root / "manifest.json"
        manifest = _read_json(manifest_path)
        if (
            manifest.get("version") != 2
            or manifest.get("pack_id") != REFERENCE_PACK_ID
            or manifest.get("engine") != "hyperframes"
            or manifest.get("hyperframes_version") != REFERENCE_HYPERFRAMES_VERSION
            or manifest.get("resolution") != "1080x1920"
            or manifest.get("fps") != 30
        ):
            raise MatrixTemplateError("invalid HyperFrames reference template manifest")
        records = manifest.get("templates")
        if not isinstance(records, list) or len(records) != REFERENCE_TEMPLATE_COUNT:
            raise MatrixTemplateError("expected exactly 17 HyperFrames reference templates")
        for required in (
            "index.html", "hyperframes.json", "preview-data.js",
            "assets/bgm/silence.m4a",
        ):
            path = pack_root.joinpath(*required.split("/"))
            if path.is_symlink() or not path.is_file():
                raise MatrixTemplateError("HyperFrames reference template pack is incomplete")
        index_html = (pack_root / "index.html").read_text(encoding="utf-8")
        if not re.search(
            r'<section\b[^>]*\bid="typography"[^>]*\bdata-start="0"',
            index_html,
        ):
            raise MatrixTemplateError(
                "HyperFrames typography must be visible from the first frame"
            )

        def has_variant_layer(variant: str, layer: str) -> bool:
            return bool(re.search(
                rf"\.{re.escape(variant)}\s+\.{re.escape(layer)}\s*(?:,|\{{)",
                index_html,
            ))

        def variant_layer_matches_contract(
            variant: str, layer: str, required: tuple[str, ...]
        ) -> bool:
            style = re.search(
                rf"\.{re.escape(variant)}\s+\.{re.escape(layer)}\s*\{{(?P<body>[^}}]*)\}}",
                index_html,
                re.DOTALL,
            )
            if not style:
                return False
            normalized = re.sub(r"\s+", "", style.group("body")).lower()
            return all(token in normalized for token in required)

        result = []
        variants = set()
        expected_ids = set()
        for index, item in enumerate(records, 1):
            if not isinstance(item, dict):
                raise MatrixTemplateError("invalid HyperFrames reference template record")
            template_id = str(item.get("id") or "")
            variant = str(item.get("variant") or "")
            expected_variant = f"v{index:02d}"
            if (
                not re.fullmatch(r"ref-[0-9]{2}-[a-z0-9-]{1,48}", template_id)
                or variant != expected_variant
                or template_id in expected_ids
                or variant in variants
            ):
                raise MatrixTemplateError("invalid HyperFrames reference template identity")
            expected_ids.add(template_id)
            variants.add(variant)
            if not all(
                has_variant_layer(variant, layer)
                for layer in ("top1", "top2")
            ):
                raise MatrixTemplateError(
                    "HyperFrames reference template top layer styles are incomplete"
                )
            top_layer_count = 3 if has_variant_layer(variant, "top3") else 2
            fixed_private_fonts = REFERENCE_FIXED_PRIVATE_FONTS.get(variant, {})
            for layer, font in fixed_private_fonts.items():
                font_size_px = (
                    font.get("font_size_px") if isinstance(font, dict) else None
                )
                if (
                    layer not in REFERENCE_TEXT_LAYER_IDS
                    or not isinstance(font, dict)
                    or font.get("family") not in self.private_fonts
                    or (
                        font_size_px is not None
                        and (
                            isinstance(font_size_px, bool)
                            or not isinstance(font_size_px, int)
                            or not 8 <= font_size_px <= 240
                        )
                    )
                ):
                    raise MatrixTemplateError(
                        "HyperFrames fixed private font is unavailable"
                    )
            if variant == REFERENCE_FEATURED_VARIANT and not all(
                variant_layer_matches_contract(variant, layer, required)
                for layer, required in REFERENCE_FEATURED_STYLE_CONTRACT.items()
            ):
                raise MatrixTemplateError(
                    "featured HyperFrames template style contract changed"
                )
            if variant == REFERENCE_V01_VARIANT and not all(
                variant_layer_matches_contract(variant, layer, required)
                for layer, required in REFERENCE_V01_STYLE_CONTRACT.items()
            ):
                raise MatrixTemplateError(
                    "v01 HyperFrames template style contract changed"
                )
            record = {
                "id": template_id,
                "name": str(item.get("name") or template_id)[:40],
                "description": str(item.get("description") or "")[:160],
                "tags": ["HyperFrames", "固定排版", "内置字体"],
                "engine": "hyperframes",
                "font_mode": "template_locked",
                "font_selectable": False,
                "text_layers": {"top": top_layer_count, "bottom": 2},
                "duration_mode": "random_integer_8_15",
                "required_visuals": 3,
                "variant": variant,
                "fixed_fonts": {
                    layer: font["family"]
                    for layer, font in fixed_private_fonts.items()
                },
            }
            top_layers = ["top1", "top2"] + (
                ["top3"] if top_layer_count == 3 else []
            )
            semantic_contract = {}
            for layer in top_layers + ["bottom2"]:
                max_lines = (
                    2 if top_layer_count == 3 or layer != "top2" else 4
                )
                semantic_contract[layer] = _reference_css_layer_metrics(
                    index_html, variant, layer,
                    2 if layer == "bottom2" else max_lines,
                )
                fixed = fixed_private_fonts.get(layer)
                if fixed:
                    semantic_contract[layer]["family"] = str(fixed["family"])
                    if fixed.get("font_size_px") is not None:
                        semantic_contract[layer]["font_size_px"] = int(
                            fixed["font_size_px"]
                        )
            self.reference_semantic_layouts[variant] = semantic_contract
            record["semantic_layout"] = {
                "version": REFERENCE_SEMANTIC_LAYOUT_VERSION,
                "max_width_px": int(REFERENCE_TEXT_MAX_WIDTH_PX),
                "layers": {
                    layer: {
                        "font_size_px": int(metrics["font_size_px"]),
                        "font_weight": int(metrics["font_weight"]),
                        "max_width_px": int(metrics["max_width_px"]),
                        "max_lines": int(metrics["max_lines"]),
                    }
                    for layer, metrics in semantic_contract.items()
                },
            }
            result.append(record)
            self.reference_templates[template_id] = record

        if sum(
            item["variant"] == REFERENCE_FEATURED_VARIANT for item in result
        ) != 1:
            raise MatrixTemplateError("featured HyperFrames template is missing")
        result.sort(
            key=lambda item: item["variant"] != REFERENCE_FEATURED_VARIANT
        )

        reference_fonts = _load_bundled_fonts(self.reference_skill_root)
        missing_files = [
            filename for filename in REFERENCE_FONT_FILES
            if not (self.reference_skill_root / "assets/fonts" / filename).is_file()
        ]
        if missing_files:
            raise MatrixTemplateError("HyperFrames reference template fonts are incomplete")
        if {
            family: item["file"] for family, item in reference_fonts.items()
        } != REFERENCE_FONT_FAMILY_FILES:
            raise MatrixTemplateError("HyperFrames reference template font mapping changed")
        self.reference_fonts = reference_fonts
        for contract in self.reference_semantic_layouts.values():
            for metrics in contract.values():
                if (
                    metrics["family"] not in reference_fonts
                    and metrics["family"] not in self.private_fonts
                ):
                    raise MatrixTemplateError(
                        "HyperFrames semantic layout font mapping changed"
                    )
                if (
                    metrics["family"] != "Noto Sans SC"
                    and int(metrics["font_weight"]) != 400
                ):
                    raise MatrixTemplateError(
                        "HyperFrames static font requires synthetic weight"
                    )
        if self.hyperframes_cli is None or not self.hyperframes_cli.is_file():
            raise MatrixTemplateError("HyperFrames 0.8.16 CLI is unavailable")
        if self.hyperframes_gsap is None or not self.hyperframes_gsap.is_file():
            raise MatrixTemplateError("HyperFrames GSAP runtime is unavailable")
        if self.hyperframes_browser is None or not self.hyperframes_browser.is_file():
            raise MatrixTemplateError("HyperFrames browser is unavailable")
        version = subprocess.run(
            [str(self.hyperframes_cli), "--version"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if version.returncode or version.stdout.strip() != REFERENCE_HYPERFRAMES_VERSION:
            raise MatrixTemplateError("HyperFrames CLI version mismatch")
        self.reference_pack_root = pack_root
        self.reference_font_fingerprint = _font_bundle_fingerprint(reference_fonts)
        return result

    def _reference_measure_font(self, family: str, size: int, weight: int):
        key = (str(family), int(size), int(weight))
        cached = self.reference_measure_fonts.get(key)
        if cached is not None:
            return cached
        record = self.private_fonts.get(family) or self.reference_fonts.get(family)
        if record is None or not Path(record["path"]).is_file():
            raise MatrixTemplateError("HyperFrames 语义排版字体不可用")
        try:
            font = ImageFont.truetype(str(record["path"]), int(size))
        except Exception as exc:
            raise MatrixTemplateError("HyperFrames 语义排版字体无法测量") from exc
        try:
            axes = font.get_variation_axes()
        except OSError:
            axes = []
        weight_axis = None
        for index, axis in enumerate(axes):
            name = axis.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("ascii", "ignore")
            if str(name).strip().lower() == "weight":
                weight_axis = index
                break
        if weight_axis is None:
            if int(weight) != 400:
                raise MatrixTemplateError(
                    "HyperFrames static font requires synthetic weight"
                )
        else:
            axis = axes[weight_axis]
            if not int(axis["minimum"]) <= int(weight) <= int(axis["maximum"]):
                raise MatrixTemplateError(
                    "HyperFrames variable font weight is unavailable"
                )
            values = [int(item["default"]) for item in axes]
            values[weight_axis] = int(weight)
            try:
                font.set_variation_by_axes(values)
            except Exception as exc:
                raise MatrixTemplateError(
                    "HyperFrames variable font weight cannot be applied"
                ) from exc
        self.reference_measure_fonts[key] = font
        return font

    def _reference_text_width(self, value: str, metrics: dict) -> float:
        text = _hide_reference_edge_punctuation(value)
        if not text:
            return 0.0
        size = int(metrics["font_size_px"])
        font = self._reference_measure_font(
            str(metrics["family"]), size, int(metrics["font_weight"]),
        )
        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = draw.textbbox(
            (0, 0), text, font=font,
            stroke_width=int(metrics.get("stroke_px") or 0),
        )
        letter_spacing = (
            max(0, len(text) - 1) * size
            * float(metrics.get("letter_spacing_em", REFERENCE_LETTER_SPACING_EM))
        )
        return float(box[2] - box[0]) + letter_spacing

    def _pack_reference_semantic_span(
        self, text: str, start: int, end: int,
        break_after: list[int], metrics: dict,
    ) -> list[str]:
        if start >= end:
            return []
        internal = sorted({
            item + 1 for item in break_after if start <= item < end - 1
        })
        boundaries = [start] + internal + [end]
        max_lines = int(metrics["max_lines"])
        max_width = float(
            metrics.get("max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX)
        )
        width_cache = {}

        def measured(left_index: int, right_index: int):
            key = (left_index, right_index)
            if key not in width_cache:
                value = text[boundaries[left_index]:boundaries[right_index]]
                display = _hide_reference_edge_punctuation(value)
                width_cache[key] = (
                    value, display,
                    self._reference_text_width(value, metrics) if display else 0.0,
                )
            return width_cache[key]

        total_width = self._reference_text_width(text[start:end], metrics)
        for line_count in range(1, min(max_lines, len(boundaries) - 1) + 1):
            ideal = min(max_width, total_width / line_count)
            states = {0: (0.0, [])}
            for _line_index in range(line_count):
                next_states = {}
                for left_index, (score, path) in states.items():
                    for right_index in range(left_index + 1, len(boundaries)):
                        value, display, width = measured(left_index, right_index)
                        if not display:
                            continue
                        if width > max_width + 0.001:
                            break
                        remaining_lines = line_count - len(path) - 1
                        remaining_boundaries = len(boundaries) - right_index - 1
                        if remaining_boundaries < remaining_lines:
                            continue
                        candidate = score + (width - ideal) ** 2
                        current = next_states.get(right_index)
                        if current is None or candidate < current[0]:
                            next_states[right_index] = (
                                candidate, path + [value],
                            )
                states = next_states
            selected = states.get(len(boundaries) - 1)
            if selected is not None:
                return selected[1]
        raise ValueError(
            "HyperFrames 文案无法在完整语义边界内排入模板"
        )

    def _reference_semantic_text_layout(
        self, top: str, bottom: str, variant: str, semantic_layout: dict,
    ) -> tuple[dict[str, str], dict[str, str]]:
        contract = self.reference_semantic_layouts.get(variant)
        if contract is None:
            raise ValueError("HyperFrames 模板不支持语义排版")
        layout = _normalize_reference_semantic_layout(
            semantic_layout, top, bottom,
        )
        top1_end = int(layout["top1_end"]) + 1
        top1_lines = self._pack_reference_semantic_span(
            top, 0, top1_end, layout["top_break_after"], contract["top1"],
        )
        top3_metrics = contract.get("top3")
        top3_start = len(top)
        top3_lines = []
        if top3_metrics is None or top1_end >= len(top):
            top2_lines = self._pack_reference_semantic_span(
                top, top1_end, len(top),
                layout["top_break_after"], contract["top2"],
            )
        else:
            split_candidates = [
                item + 1 for item in layout["top_break_after"]
                if top1_end <= item < len(top) - 1
            ] + [len(top)]
            candidates = []
            for split in split_candidates:
                try:
                    top2_candidate = self._pack_reference_semantic_span(
                        top, top1_end, split,
                        layout["top_break_after"], contract["top2"],
                    )
                    top3_candidate = self._pack_reference_semantic_span(
                        top, split, len(top),
                        layout["top_break_after"], top3_metrics,
                    )
                except ValueError:
                    continue
                total_lines = len(top2_candidate) + len(top3_candidate)
                top3_empty = not top3_candidate and len(split_candidates) > 1
                widths = [
                    self._reference_text_width(line, contract["top2"])
                    / float(contract["top2"].get(
                        "max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX,
                    ))
                    for line in top2_candidate
                ] + [
                    self._reference_text_width(line, top3_metrics)
                    / float(top3_metrics.get(
                        "max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX,
                    ))
                    for line in top3_candidate
                ]
                ideal = sum(widths) / max(1, len(widths))
                raggedness = sum((width - ideal) ** 2 for width in widths)
                semantic_penalty = (
                    0 if top[split - 1] in "，。！？；,.!?;" else 1
                )
                candidates.append((
                    total_lines, top3_empty, semantic_penalty, raggedness,
                    split, top2_candidate, top3_candidate,
                ))
            if not candidates:
                raise ValueError(
                    "HyperFrames 文案无法在完整语义边界内排入模板"
                )
            (
                _total_lines, _top3_empty, _semantic_penalty, _raggedness,
                top3_start, top2_lines, top3_lines,
            ) = min(candidates, key=lambda item: item[:4])
        bottom2_lines = self._pack_reference_semantic_span(
            bottom, 0, len(bottom), layout["bottom_break_after"],
            contract["bottom2"],
        )
        source_text = {
            "top1": top[:top1_end],
            "top2": top[top1_end:top3_start],
            "top3": top[top3_start:],
            "bottom1": "", "bottom2": bottom,
        }
        display_text = {
            "top1": "\n".join(map(_hide_reference_edge_punctuation, top1_lines)),
            "top2": "\n".join(map(_hide_reference_edge_punctuation, top2_lines)),
            "top3": "\n".join(map(_hide_reference_edge_punctuation, top3_lines)),
            "bottom1": "",
            "bottom2": "\n".join(map(_hide_reference_edge_punctuation, bottom2_lines)),
        }
        return source_text, display_text

    def validate_payload(self, raw: dict, *, require_available_font: bool = True,
                         allowed_template_ids=None,
                         default_template_id: str = "full-overlay-bold",
                         enforce_reference_layout: bool = True) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("request body must be an object")
        top = " ".join(str(raw.get("top_text") or "").split())
        bottom = " ".join(str(raw.get("bottom_text") or "").split())
        if not 2 <= len(top) <= 60:
            raise ValueError("顶部标题需要 2-60 个字符")
        if not 2 <= len(bottom) <= 80:
            raise ValueError("底部行动文案需要 2-80 个字符")
        template_id = str(raw.get("template_id") or default_template_id)
        allowed_templates = (
            set(self.templates) if allowed_template_ids is None
            else set(allowed_template_ids)
        )
        if template_id not in allowed_templates:
            raise ValueError("请选择有效模板")
        reference_template = template_id in self.reference_templates
        semantic_layout = raw.get("semantic_layout")
        normalized_semantic_layout = None
        if reference_template:
            variant = self.reference_templates[template_id]["variant"]
            if semantic_layout is not None:
                if variant not in self.reference_semantic_layouts:
                    raise ValueError("HyperFrames 当前模板不支持语义排版")
                normalized_semantic_layout = _normalize_reference_semantic_layout(
                    semantic_layout, top, bottom,
                )
                if enforce_reference_layout:
                    self._reference_semantic_text_layout(
                        top, bottom, variant, normalized_semantic_layout,
                    )
            elif enforce_reference_layout:
                _reference_text_layout(
                    top,
                    bottom,
                    self.reference_templates[template_id]["text_layers"]["top"],
                )
        elif semantic_layout is not None:
            raise ValueError("semantic_layout 仅支持指定 HyperFrames 模板")
        font_family = str(raw.get("font_family") or "").strip()
        if (
            not reference_template and font_family
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}", font_family)
        ):
            raise ValueError("字体参数格式无效")
        if (
            not reference_template and require_available_font and font_family
            and font_family not in self.available_font_families()
        ):
            raise ValueError("请选择当前可用字体")
        duration = _duration(
            top, bottom, None if reference_template else raw.get("duration")
        )
        bgm = raw.get("bgm", True)
        if not isinstance(bgm, bool):
            raise ValueError("bgm must be boolean")
        result = {
            "top_text": top, "bottom_text": bottom,
            "template_id": template_id, "duration": duration,
            "bgm": bgm,
        }
        if font_family and not reference_template:
            result["font_family"] = font_family
        if normalized_semantic_layout is not None:
            result["semantic_layout"] = normalized_semantic_layout
        batch_id = str(raw.get("batch_id") or "").strip().lower()
        batch_index = raw.get("batch_index")
        batch_size = raw.get("batch_size")
        if batch_id or batch_index is not None or batch_size is not None:
            if (
                not BATCH_RE.fullmatch(batch_id)
                or isinstance(batch_index, bool) or not isinstance(batch_index, int)
                or isinstance(batch_size, bool) or not isinstance(batch_size, int)
                or not 1 <= batch_index <= batch_size <= MAX_BATCH_SIZE
            ):
                raise ValueError("批量任务参数无效")
            result.update({
                "batch_id": batch_id,
                "batch_index": batch_index,
                "batch_size": batch_size,
            })
        return result

    def available_font_families(self) -> set[str]:
        return set(self.bundled_fonts) | set(self.private_fonts)

    def public_fonts(self) -> list[dict]:
        values = [{"value": "", "label": "自动搭配", "source": "automatic"}]
        for family in sorted(self.available_font_families(), key=lambda item: (FONT_LABELS[item], item)):
            values.append({
                "value": family,
                "label": FONT_LABELS[family],
                "source": "private" if family in self.private_fonts else "bundled",
            })
        return values

    def required_visuals(self, payload: dict) -> int:
        if payload.get("template_id") in self.reference_templates:
            return 3
        return _required_visuals(payload["duration"])

    def submit(self, raw: dict, request_id: str) -> dict:
        if not REQUEST_RE.fullmatch(request_id):
            raise ValueError("invalid request id")
        existing = self.store.get_by_request_id(request_id)
        if existing is not None:
            stored_payload = json.loads(existing["payload"])
            stored_template_id = str(stored_payload.get("template_id") or "")
            payload = self.validate_payload(
                raw,
                require_available_font=False,
                allowed_template_ids={stored_template_id},
                default_template_id=stored_template_id,
                enforce_reference_layout=False,
            )
        else:
            payload = self.validate_payload(raw, require_available_font=False)
        job, created = self.store.create(
            request_id, payload, admission_guard=self._ensure_disk_capacity,
            freeze_payload=self._freeze_font_provenance,
        )
        if created:
            self._enqueue(job["job_id"])
        return job

    def _freeze_font_provenance(self, job_id: str, payload: dict) -> dict:
        template_id = payload["template_id"]
        if template_id in self.reference_templates:
            payload.pop("font_family", None)
            template = self.reference_templates[template_id]
            top_layer_count = int(template["text_layers"]["top"])
            fixed_fonts = {}
            private_font_records = {}
            for layer, font in REFERENCE_FIXED_PRIVATE_FONTS.get(
                template["variant"], {}
            ).items():
                family = str(font["family"])
                current = self.private_fonts.get(family)
                if current is None:
                    raise MatrixTemplateError(
                        "HyperFrames fixed private font is unavailable"
                    )
                frozen_font = {
                    "family": family,
                    "alias": str(font["alias"]),
                    "file": current["file"],
                    "sha256": current["sha256"],
                }
                if font.get("font_size_px") is not None:
                    frozen_font["font_size_px"] = int(font["font_size_px"])
                fixed_fonts[layer] = frozen_font
                private_font_records[family] = {
                    "family": family,
                    "file": current["file"],
                    "sha256": current["sha256"],
                    "source": "private",
                }
            if payload.get("semantic_layout") is not None:
                source_text, display_text = self._reference_semantic_text_layout(
                    payload["top_text"], payload["bottom_text"],
                    template["variant"], payload["semantic_layout"],
                )
            else:
                source_text, display_text = _reference_text_layout(
                    payload["top_text"], payload["bottom_text"], top_layer_count
                )
            payload["_reference_template"] = {
                "pack_id": REFERENCE_PACK_ID,
                "engine": "hyperframes",
                "hyperframes_version": REFERENCE_HYPERFRAMES_VERSION,
                "variant": template["variant"],
                "top_layer_count": top_layer_count,
                "duration": _reference_duration(job_id, template_id),
                "text": source_text,
                "display_text": display_text,
                "fixed_fonts": fixed_fonts,
            }
            reference_records = [
                {
                    "family": family,
                    "file": item["file"],
                    "sha256": item["sha256"],
                    "source": "reference-template",
                }
                for family, item in sorted(self.reference_fonts.items())
            ]
            combined_fonts = {
                **self.reference_fonts,
                **{
                    family: self.private_fonts[family]
                    for family in private_font_records
                },
            }
            payload["_font_provenance"] = {
                "selection": {
                    "variant": "template-locked",
                    "top_font": "template-defined",
                    "bottom_font": "template-defined",
                },
                "fonts": reference_records + [
                    private_font_records[family]
                    for family in sorted(private_font_records)
                ],
                "private_bundle_sha256": self.reference_font_fingerprint,
                "template_font_bundle_sha256": _font_bundle_fingerprint(
                    combined_fonts
                ),
            }
            payload["_display_top_text"] = "\n".join(
                value
                for key, value in payload["_reference_template"]["display_text"].items()
                if key.startswith("top") and value
            )
            return payload
        requested_font = str(payload.get("font_family") or "")
        if requested_font and requested_font not in self.available_font_families():
            raise ValueError("请选择当前可用字体")
        selection = (
            {"variant": "user-selected", "top_font": requested_font, "bottom_font": requested_font}
            if requested_font else
            _font_selection(payload["template_id"], job_id, set(self.private_fonts))
        )
        selected = []
        for family in dict.fromkeys((selection["top_font"], selection["bottom_font"])):
            source = self.private_fonts.get(family) or self.bundled_fonts.get(family)
            if source is None:
                raise MatrixTemplateError("selected font is unavailable")
            selected.append({
                "family": family, "file": source["file"], "sha256": source["sha256"],
                "source": "private" if family in self.private_fonts else "bundled",
            })
        payload["_font_provenance"] = {
            "selection": selection,
            "fonts": selected,
            "private_bundle_sha256": self.private_font_fingerprint,
        }
        max_chars, max_lines = self.template_text_limits[payload["template_id"]]
        payload["_display_top_text"] = _balanced_title(
            payload["top_text"], max_chars, max_lines
        )
        return payload

    def _enqueue(self, job_id: str) -> bool:
        with self.queue_lock:
            if job_id in self.queued_jobs or job_id in self.active_jobs:
                return False
            self.queued_jobs.add(job_id)
            self.jobs.put_nowait(job_id)
            return True

    def health(self) -> dict:
        worker_threads = self.workers or ([self.worker] if self.worker is not None else [])
        live_workers = sum(worker.is_alive() for worker in worker_threads)
        worker_alive = live_workers == self.concurrency
        cleanup_alive = self.cleanup_worker is not None and self.cleanup_worker.is_alive()
        with self.degraded_lock:
            degraded_job_count = len(self.degraded_jobs)
        worker_degraded = degraded_job_count > 0
        ready = not self.workers_expected or (
            worker_alive and cleanup_alive and not worker_degraded
        )
        return {
            "ok": ready,
            "worker_alive": worker_alive,
            "worker_count": live_workers,
            "cleanup_worker_alive": cleanup_alive,
            "worker_degraded": worker_degraded,
            "degraded_jobs": degraded_job_count,
            "concurrency": self.concurrency,
            "private_fonts": len(self.private_fonts),
            "private_font_bundle_sha256": self.private_font_fingerprint,
            "hyperframes_templates": len(self.reference_templates),
            "hyperframes_version": (
                REFERENCE_HYPERFRAMES_VERSION if self.reference_templates else ""
            ),
            "reference_top_layer_counts": {
                str(layer_count): sum(
                    1 for item in self.reference_templates.values()
                    if item["text_layers"]["top"] == layer_count
                )
                for layer_count in (2, 3)
            },
            "reference_fixed_private_fonts": sorted({
                family
                for item in self.reference_templates.values()
                for family in item.get("fixed_fonts", {}).values()
            }),
            "reference_semantic_layout_templates": sorted(
                item["variant"] for item in self.reference_templates.values()
                if item.get("semantic_layout")
            ),
            "hyperframes_concurrency": self.hyperframes_concurrency,
            "hyperframes_total_timeout_seconds": self.hyperframes_total_timeout_seconds,
            "hyperframes_slot_timeout_seconds": self.hyperframes_slot_timeout_seconds,
        }

    def _ensure_disk_capacity(self) -> None:
        usage = shutil.disk_usage(self.data_root)
        used_percent = 100.0 * usage.used / max(1, usage.total)
        if used_percent >= self.disk_high_water_percent:
            raise DiskCapacityError("生成服务器存储空间不足，请稍后再试")

    def _purge_trash(self) -> None:
        trash = self.data_root / ".trash"
        if not trash.is_dir():
            return
        for path in list(trash.iterdir())[:self.cleanup_batch_size]:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def cleanup_once(self, *, now: int | None = None) -> int:
        cleaned = 0
        current = _now() if now is None else int(now)
        candidates = self.store.cleanup_candidates(
            now=current,
            retention_seconds=self.retention_seconds,
            delivery_grace_seconds=self.delivery_grace_seconds,
            limit=self.cleanup_batch_size,
        )
        trash = self.data_root / ".trash"
        for row in candidates:
            job_id = row["id"]
            with self.file_lock:
                if job_id in self.active_downloads:
                    continue
                root = self.data_root / job_id
                moved = None
                if root.exists():
                    trash.mkdir(parents=True, exist_ok=True)
                    moved = trash / f"{job_id}-{uuid.uuid4().hex}"
                    os.replace(root, moved)
                self.store.mark_cleaned(job_id)
            if moved is not None:
                shutil.rmtree(moved, ignore_errors=True)
            cleaned += 1
        return cleaned

    def _cleanup_worker(self) -> None:
        while not self.stop_event.wait(self.cleanup_interval_seconds):
            try:
                self._purge_trash()
                self.cleanup_once()
            except Exception as exc:
                print(f"[matrix-template] cleanup failed: {exc}", flush=True)

    @contextlib.contextmanager
    def open_completed_file(self, job_id: str):
        with self.file_lock:
            row = self.store.get(job_id)
            expected_url = f"/v1/files/{job_id}.mp4"
            result = json.loads(row["result"]) if row and row["result"] else {}
            if (
                not row or row["status"] != "completed" or row["cleaned_at"]
                or result.get("file_url") != expected_url
            ):
                raise FileNotFoundError(job_id)
            output = self.data_root / job_id / "output/published.mp4"
            handle = output.open("rb")
            self.active_downloads.add(job_id)
        try:
            yield handle
            self.store.mark_delivered(job_id)
        finally:
            handle.close()
            with self.file_lock:
                self.active_downloads.discard(job_id)

    def _discard_output(self, job_id: str) -> None:
        output_dir = self.data_root / job_id / "output"
        for name in ("final.mp4", "published.mp4"):
            (output_dir / name).unlink(missing_ok=True)

    def _library_request(self, method: str, path: str, body=None):
        data = _json_bytes(body) if body is not None else None
        request = urllib.request.Request(
            self.library_url + path, data=data, method=method,
            headers={
                "Authorization": "Bearer " + self.library_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            raise MatrixTemplateError(str(detail or "平台素材库暂不可用")) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MatrixTemplateError("平台素材库暂不可用") from exc

    def _select_materials_once(self, payload: dict, job_id: str,
                               used_sha256=()) -> list[dict]:
        count = self.required_visuals(payload)
        reference_template = payload.get("template_id") in self.reference_templates
        query = payload["top_text"] + " " + payload["bottom_text"]
        scenes = [{
            "scene_id": "media_01", "query": query,
            "purpose": "模板成片主视频", "media_type": "video",
        }]
        scenes.extend({
            "scene_id": f"media_{index:02d}", "query": query,
            "purpose": "模板成片补充视频",
            "media_type": "video" if reference_template else "visual",
        } for index in range(2, count + 1))
        if payload["bgm"]:
            scenes.append({
                "scene_id": "bgm", "query": query,
                "purpose": "模板成片背景音乐", "media_type": "bgm",
            })
        result = self._library_request("POST", "/v1/select", {
            "scenes": scenes, "orientation": "portrait", "seed": job_id,
            "used_sha256": list(used_sha256),
            "selection_mode": "random",
        })
        values = result.get("materials") or []
        by_scene = {str(item.get("scene_id") or ""): item for item in values if isinstance(item, dict)}
        expected = [scene["scene_id"] for scene in scenes]
        if set(by_scene) != set(expected) or len(by_scene) != len(expected):
            raise MatrixTemplateError("素材库返回的分镜绑定不完整")
        ordered = [by_scene[scene_id] for scene_id in expected]
        shas = [str(item.get("sha256") or "").lower() for item in ordered]
        if any(not SHA_RE.fullmatch(value) for value in shas) or len(set(shas)) != len(shas):
            raise MatrixTemplateError("素材库返回了无效或重复素材")
        if ordered[0].get("media_type") != "video":
            raise MatrixTemplateError("模板成片至少需要一个视频素材")
        if reference_template:
            if any(item.get("media_type") != "video" for item in ordered[:count]):
                raise MatrixTemplateError("HyperFrames 模板需要三个不同的视频素材")
        else:
            for item in ordered[1:count]:
                if item.get("media_type") not in {"image", "video"}:
                    raise MatrixTemplateError("素材库返回了无效画面素材")
        if payload["bgm"] and ordered[-1].get("media_type") != "bgm":
            raise MatrixTemplateError("素材库返回了无效背景音乐")
        return ordered

    def _select_materials(self, payload: dict, job_id: str) -> list[dict]:
        batch_id = str(payload.get("batch_id") or "")
        if not batch_id:
            return self._select_materials_once(payload, job_id)
        with self.batch_material_lock:
            frozen = self.store.batch_material_selection(job_id)
            if frozen is not None:
                return frozen
            used = self.store.batch_used_visuals(batch_id)
            selected = self._select_materials_once(
                payload, job_id, used_sha256=used
            )
            self.store.reserve_batch_materials(batch_id, job_id, selected)
            return selected

    def _download(self, item: dict, target_dir: Path) -> Path:
        sha = str(item["sha256"]).lower()
        request = urllib.request.Request(
            self.library_url + "/v1/assets/" + sha,
            headers={"Authorization": "Bearer " + self.library_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content_type = response.headers.get_content_type()
                suffix = CONTENT_SUFFIXES.get(content_type)
                if not suffix:
                    raise MatrixTemplateError("素材库文件类型不受支持")
                target = target_dir / (sha + suffix)
                temporary = target.with_suffix(target.suffix + ".part")
                digest = hashlib.sha256()
                total = 0
                try:
                    with temporary.open("wb") as handle:
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_ASSET_BYTES:
                                raise MatrixTemplateError("素材库文件过大")
                            digest.update(chunk)
                            handle.write(chunk)
                    if not total or not hmac.compare_digest(digest.hexdigest(), sha):
                        raise MatrixTemplateError("素材库文件校验失败")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                return target
        except urllib.error.HTTPError as exc:
            raise MatrixTemplateError("素材库文件读取失败") from exc

    def _stage_project_fonts(self, root: Path, provenance: dict) -> str | None:
        frozen_fonts = provenance.get("fonts") if isinstance(provenance, dict) else None
        if not isinstance(frozen_fonts, list):
            raise MatrixTemplateError("frozen font provenance is missing")
        requested = [item for item in frozen_fonts
                     if isinstance(item, dict) and item.get("source") == "private"]
        if not requested:
            return None
        destination = root / "assets/fonts"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        staged = []
        filenames = set()
        for family, item in sorted(self.bundled_fonts.items()):
            filename, expected, source = item["file"], item["sha256"], item["path"]
            if filename in filenames or _file_sha256(source) != expected:
                raise MatrixTemplateError("stable Skill font bundle failed verification")
            shutil.copy2(source, destination / filename)
            staged.append({"family": family, "file": filename, "sha256": expected})
            filenames.add(filename)
        for frozen in sorted(requested, key=lambda item: str(item.get("family") or "")):
            family = str(frozen.get("family") or "")
            current = self.private_fonts.get(family)
            if (
                current is None
                or current["file"] != frozen.get("file")
                or current["sha256"] != frozen.get("sha256")
                or _file_sha256(current["path"]) != frozen.get("sha256")
            ):
                raise MatrixTemplateError("frozen private font is unavailable or has changed")
            filename = current["file"]
            if filename in filenames:
                raise MatrixTemplateError("private font filename conflicts with bundled font")
            shutil.copy2(current["path"], destination / filename)
            staged.append({key: current[key] for key in ("family", "file", "sha256")})
            filenames.add(filename)
        (destination / "sources.json").write_text(
            json.dumps({"fonts": staged}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return "assets/fonts"

    def _project(self, payload: dict, job_id: str, materials: list[dict], paths: list[Path]) -> dict:
        count = _required_visuals(payload["duration"])
        provenance = payload.get("_font_provenance")
        if not isinstance(provenance, dict) or not isinstance(provenance.get("selection"), dict):
            raise MatrixTemplateError("frozen font provenance is missing")
        font_selection = provenance["selection"]
        frozen_families = {
            str(item.get("family") or "") for item in provenance.get("fonts", [])
            if isinstance(item, dict)
        }
        if (
            font_selection.get("top_font") not in frozen_families
            or font_selection.get("bottom_font") not in frozen_families
            or not SHA_RE.fullmatch(str(provenance.get("private_bundle_sha256") or ""))
        ):
            raise MatrixTemplateError("frozen font provenance is invalid")
        media = []
        for item, path in zip(materials[:count], paths[:count]):
            media.append({
                "path": path.relative_to(self.data_root / job_id).as_posix(),
                "type": item["media_type"],
                "record_id": item.get("record_id"),
            })
        project = {
            "version": 1,
            "project_id": job_id,
            "source_text": payload["top_text"] + "\n" + payload["bottom_text"],
            "platforms": ["douyin", "xiaohongshu", "wechat_channels"],
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "material_library": {
                "enabled": True, "index_source": "huangque-internal-api",
                "required_status": "可使用", "selection_policy": "library-only",
            },
            "layout": {
                "template_id": payload["template_id"],
                "top_font": font_selection["top_font"],
                "bottom_font": font_selection["bottom_font"],
            },
            "font_selection": font_selection,
            "material_policy": {"allow_image_only": False, "image_only_reason": ""},
            "voice": {"enabled": False},
            "scenes": [{
                "id": "s01", "role": "hook", "text": "",
                "top_text": str(payload.get("_display_top_text") or payload["top_text"]),
                "bottom_text": payload["bottom_text"],
                "duration": payload["duration"], "media": media,
                "motion": "zoom-in", "transition": "cut",
                "caption_chunks": [], "sfx": [],
            }],
            "render": {
                "output": "output/final.mp4", "video_codec": "libx264",
                "audio_codec": "aac", "crf": 18, "preset": "medium",
            },
        }
        if payload["bgm"]:
            bgm_item, bgm_path = materials[-1], paths[-1]
            project["bgm"] = {
                "enabled": True,
                "path": bgm_path.relative_to(self.data_root / job_id).as_posix(),
                "record_id": bgm_item.get("record_id"),
                "loop_mode": "crossfade", "target_lufs": -18,
            }
        return project

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    def _run_tracked_process(
        self, command: list[str], *, timeout_seconds: float,
        timeout_error: str, env: dict | None = None,
    ) -> tuple[int, bytes, bytes]:
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if env is not None:
            options["env"] = env
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        with self.process_lock:
            if self.stop_event.is_set():
                raise MatrixTemplateError("模板成片服务正在停止")
            process = subprocess.Popen(command, **options)
            self.active_processes.add(process)
            self.active_process = process
        try:
            try:
                stdout, stderr = process.communicate(
                    timeout=max(0.001, float(timeout_seconds))
                )
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise MatrixTemplateError(timeout_error) from exc
            if self.stop_event.is_set():
                raise MatrixTemplateError("模板成片服务正在停止")
            return process.returncode, stdout or b"", stderr or b""
        finally:
            with self.process_lock:
                self.active_processes.discard(process)
                self.active_process = next(iter(self.active_processes), None)

    def _render(self, project_path: Path) -> None:
        output = project_path.parent / "output/final.mp4"
        command = [
            self.python, str(self.skill_root / "scripts/render_video.py"),
            str(project_path),
        ]
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(command, **options)
        with self.process_lock:
            self.active_processes.add(process)
            self.active_process = process
        try:
            try:
                _stdout, stderr = process.communicate(timeout=RENDER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                output.unlink(missing_ok=True)
                raise MatrixTemplateError("模板成片渲染超时") from exc
            if process.returncode:
                output.unlink(missing_ok=True)
                detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
                raise MatrixTemplateError("模板成片渲染失败" + (": " + detail if detail else ""))
        finally:
            with self.process_lock:
                self.active_processes.discard(process)
                self.active_process = next(iter(self.active_processes), None)

    @staticmethod
    def _copy_reference_asset(source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination.as_posix()

    def _prepare_reference_bgm(
        self, source: Path, destination: Path, duration: float,
        *, deadline_at: float,
    ) -> None:
        if (
            not source.is_file()
            or not math.isfinite(duration)
            or duration <= 0
            or not math.isfinite(deadline_at)
        ):
            raise MatrixTemplateError("HyperFrames 模板背景音乐参数无效")
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise MatrixTemplateError("HyperFrames 模板任务超过总时限")
        deadline_limited = remaining < REFERENCE_BGM_PREPARE_TIMEOUT_SECONDS
        timeout_seconds = min(
            float(REFERENCE_BGM_PREPARE_TIMEOUT_SECONDS), remaining
        )
        timeout_error = (
            "HyperFrames 模板任务超过总时限"
            if deadline_limited
            else "HyperFrames 模板背景音乐预处理超时"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".part.m4a")
        temporary.unlink(missing_ok=True)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-stream_loop", "-1", "-i", str(source),
            "-map", "0:a:0", "-t", _format_reference_seconds(duration),
            "-vn", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(temporary),
        ]
        try:
            returncode, _stdout, _stderr = self._run_tracked_process(
                command, timeout_seconds=timeout_seconds,
                timeout_error=timeout_error,
            )
            prepared = temporary.is_file() and temporary.stat().st_size > 0
            if returncode or not prepared:
                raise MatrixTemplateError("HyperFrames 模板背景音乐预处理失败")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _reference_video_duration(path: Path) -> float:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,duration:format=duration",
            "-of", "json", str(path),
        ], check=False, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise MatrixTemplateError("HyperFrames 模板素材时长探测失败")
        try:
            data = json.loads(result.stdout)
            video = next(
                item for item in (data.get("streams") or [])
                if item.get("codec_type") == "video"
            )
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise MatrixTemplateError("HyperFrames 模板素材时长无效") from exc
        values = []
        for raw_value in (
            video.get("duration"),
            (data.get("format") or {}).get("duration"),
        ):
            try:
                value = float(raw_value or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
        if not values:
            raise MatrixTemplateError("HyperFrames 模板素材时长无效")
        duration = min(values)
        if duration < REFERENCE_MIN_SEGMENT_SECONDS:
            raise MatrixTemplateError("HyperFrames 模板素材时长不足")
        return duration

    def _validate_reference_visual_coverage(
        self, output: Path, timeout_seconds: float = 120
    ) -> None:
        command = [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(output),
            "-vf", REFERENCE_BLACK_SCREEN_FILTER,
            "-an", "-f", "null", "-",
        ]
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(command, **options)
        with self.process_lock:
            self.active_processes.add(process)
            self.active_process = process
        try:
            try:
                _stdout, stderr = process.communicate(
                    timeout=max(1.0, float(timeout_seconds))
                )
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise MatrixTemplateError(
                    "HyperFrames 模板成片黑屏检测超时"
                ) from exc
            if process.returncode:
                raise MatrixTemplateError("HyperFrames 模板成片黑屏检测失败")
        finally:
            with self.process_lock:
                self.active_processes.discard(process)
                self.active_process = next(iter(self.active_processes), None)
        detail = (stderr or b"").decode("utf-8", "replace")
        black_durations = [
            float(value) for value in re.findall(
                r"black_duration:([0-9]+(?:\.[0-9]+)?)", detail
            )
        ]
        if any(
            value + 0.001 >= REFERENCE_BLACK_SCREEN_SECONDS
            for value in black_durations
        ):
            raise MatrixTemplateError("HyperFrames 模板成片存在持续黑屏")

    def _acquire_hyperframes_slot(self, deadline_at: float) -> None:
        slot_deadline = min(
            float(deadline_at), time.time() + self.hyperframes_slot_timeout_seconds
        )
        while True:
            if self.stop_event.is_set():
                raise MatrixTemplateError("模板成片服务正在停止")
            remaining = slot_deadline - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("HyperFrames 模板任务排队超时")
            if self.hyperframes_slots.acquire(timeout=min(1.0, remaining)):
                if time.time() >= deadline_at:
                    self.hyperframes_slots.release()
                    raise MatrixTemplateError("HyperFrames 模板任务超过总时限")
                return

    def _render_reference(self, payload: dict, job_id: str,
                          materials: list[dict], paths: list[Path],
                          *, deadline_at: float | None = None) -> dict:
        reference = payload.get("_reference_template")
        display_text = reference.get("display_text") if isinstance(reference, dict) else None
        fixed_fonts = reference.get("fixed_fonts") if isinstance(reference, dict) else None
        if (
            not isinstance(reference, dict)
            or reference.get("pack_id") != REFERENCE_PACK_ID
            or reference.get("hyperframes_version") != REFERENCE_HYPERFRAMES_VERSION
            or not isinstance(reference.get("text"), dict)
            or (display_text is not None and not isinstance(display_text, dict))
            or (fixed_fonts is not None and not isinstance(fixed_fonts, dict))
            or self.reference_pack_root is None
        ):
            raise MatrixTemplateError("frozen HyperFrames template metadata is invalid")
        if len(paths) < 3 or any(
            item.get("media_type") != "video" for item in materials[:3]
        ):
            raise MatrixTemplateError("HyperFrames 模板需要三个不同的视频素材")
        if deadline_at is None:
            deadline_at = time.time() + self.hyperframes_total_timeout_seconds
        if time.time() >= deadline_at:
            raise MatrixTemplateError("HyperFrames 模板任务超过总时限")

        root = self.data_root / job_id
        workdir = root / "hyperframes"
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(self.reference_pack_root, workdir)
        fonts_dir = workdir / "assets/fonts"
        fonts_dir.mkdir(parents=True, exist_ok=True)
        for filename in REFERENCE_FONT_FILES:
            source = self.reference_skill_root / "assets/fonts" / filename
            if source.is_symlink() or not source.is_file():
                raise MatrixTemplateError("HyperFrames reference template fonts changed")
            shutil.copy2(source, fonts_dir / filename)
        fixed_fonts = fixed_fonts or {}
        staged_filenames = set(REFERENCE_FONT_FILES)
        for layer, frozen in sorted(fixed_fonts.items()):
            if layer not in REFERENCE_TEXT_LAYER_IDS or not isinstance(frozen, dict):
                raise MatrixTemplateError("HyperFrames fixed private font metadata is invalid")
            family = str(frozen.get("family") or "")
            current = self.private_fonts.get(family)
            if (
                current is None
                or current["file"] != frozen.get("file")
                or current["sha256"] != frozen.get("sha256")
                or _file_sha256(current["path"]) != frozen.get("sha256")
                or current["file"] in staged_filenames
            ):
                raise MatrixTemplateError(
                    "HyperFrames frozen private font is unavailable or changed"
                )
            shutil.copy2(current["path"], fonts_dir / current["file"])
            staged_filenames.add(current["file"])

        index_path = workdir / "index.html"
        index = index_path.read_text(encoding="utf-8")
        if index.count(REFERENCE_GSAP_CDN) != 1:
            raise MatrixTemplateError("HyperFrames template GSAP declaration changed")
        index = index.replace(REFERENCE_GSAP_CDN, REFERENCE_GSAP_LOCAL)
        if REFERENCE_GSAP_CDN in index:
            raise MatrixTemplateError("HyperFrames template GSAP localization failed")
        if index.count("</head>") != 1:
            raise MatrixTemplateError("HyperFrames template head declaration changed")
        fixed_font_style = _reference_private_font_style(
            str(reference.get("variant") or ""), fixed_fonts
        )
        if (
            fixed_font_style
            and REFERENCE_PRIVATE_FONT_STYLE_ID in index
        ):
            raise MatrixTemplateError("HyperFrames fixed private font style conflicts")
        if REFERENCE_CTA_SAFE_AREA_STYLE_ID in index:
            raise MatrixTemplateError("HyperFrames CTA safe-area style conflicts")
        index = index.replace(
            "</head>",
            REFERENCE_EMPTY_LAYER_STYLE
            + "\n" + REFERENCE_CTA_SAFE_AREA_STYLE
            + ("\n" + fixed_font_style if fixed_font_style else "")
            + "\n</head>",
        )
        shutil.copy2(self.hyperframes_gsap, workdir / "gsap.min.js")

        input_dir = workdir / "assets/input"
        video_values = []
        for asset_index, source in enumerate(paths[:3], 1):
            target = input_dir / f"video-{asset_index}{source.suffix.lower()}"
            self._copy_reference_asset(source, target)
            video_values.append(target.relative_to(workdir).as_posix())
        bgm_source = None
        bgm_target = None
        if payload["bgm"]:
            if len(paths) < 4 or materials[3].get("media_type") != "bgm":
                raise MatrixTemplateError("HyperFrames template BGM binding is invalid")
            bgm_target = input_dir / "bgm.m4a"
            bgm_source = paths[3]
            bgm = bgm_target.relative_to(workdir).as_posix()
        else:
            bgm = "assets/bgm/silence.m4a"
        index = _rewrite_reference_bgm_source(index, bgm)

        variables = {
            "name": job_id,
            "variant": reference["variant"],
            **(display_text if isinstance(display_text, dict) else reference["text"]),
            "duration": reference["duration"],
            "videoA": video_values[0],
            "videoB": video_values[1],
            "videoC": video_values[2],
            "bgm": bgm,
        }
        media_durations = [
            self._reference_video_duration(path) for path in paths[:3]
        ]
        segment_starts, segment_durations = _reference_segment_timing(
            float(reference["duration"]), media_durations
        )
        index = _rewrite_reference_timeline(
            index, float(reference["duration"]),
            segment_starts, segment_durations,
        )
        index_path.write_text(index, encoding="utf-8")
        variables_path = workdir / "variables.json"
        variables_path.write_text(
            json.dumps(variables, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output = root / "output/final.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_home = self.data_root / ".hyperframes-runtime"
        cache_home = runtime_home / "cache"
        runtime_home.mkdir(parents=True, exist_ok=True)
        cache_home.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.hyperframes_cli), "render", str(workdir),
            "--output", str(output),
            "--quality", "high",
            "--workers", "1",
            "--fps", "30",
            "--sdr",
            "--no-browser-gpu",
            "--strict-variables",
            "--variables-file", str(variables_path),
        ]
        env = os.environ.copy()
        env.update({
            "HOME": str(runtime_home),
            "XDG_CACHE_HOME": str(cache_home),
            "HYPERFRAMES_BROWSER_PATH": str(self.hyperframes_browser),
            "ONNXRUNTIME_NODE_INSTALL_CUDA": "skip",
            "PRODUCER_LOW_MEMORY_MODE": "true",
        })
        options = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "env": env,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self._acquire_hyperframes_slot(deadline_at)
        try:
            if self.stop_event.is_set():
                raise MatrixTemplateError("模板成片服务正在停止")
            if bgm_source is not None and bgm_target is not None:
                self._prepare_reference_bgm(
                    bgm_source, bgm_target, float(reference["duration"]),
                    deadline_at=deadline_at,
                )
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("HyperFrames 模板任务超过总时限")
            process = subprocess.Popen(command, **options)
            with self.process_lock:
                self.active_processes.add(process)
                self.active_process = process
            try:
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(RENDER_TIMEOUT_SECONDS, remaining)
                    )
                except subprocess.TimeoutExpired as exc:
                    self._terminate(process)
                    output.unlink(missing_ok=True)
                    raise MatrixTemplateError("HyperFrames 模板任务超过总时限") from exc
                if process.returncode:
                    output.unlink(missing_ok=True)
                    detail = b"\n".join((stdout or b"", stderr or b"")).decode(
                        "utf-8", "replace"
                    ).strip()[-800:]
                    raise MatrixTemplateError(
                        "HyperFrames 模板成片渲染失败"
                        + (": " + detail if detail else "")
                    )
            finally:
                with self.process_lock:
                    self.active_processes.discard(process)
                    self.active_process = next(iter(self.active_processes), None)
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("HyperFrames 模板任务超过总时限")
            self._validate_reference_visual_coverage(
                output, timeout_seconds=min(120.0, remaining)
            )
        finally:
            self.hyperframes_slots.release()
        return variables

    def _probe(self, output: Path) -> dict:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        duration = float((data.get("format") or {}).get("duration") or 0)
        if not video or video.get("codec_name") != "h264" or (video.get("width"), video.get("height")) != (1080, 1920):
            raise MatrixTemplateError("模板成片画面规格校验失败")
        if not audio or audio.get("codec_name") != "aac" or duration <= 0:
            raise MatrixTemplateError("模板成片音频或时长校验失败")
        return {"duration": round(duration, 3), "width": 1080, "height": 1920}

    def _execute(self, job_id: str) -> dict:
        row = self.store.get(job_id)
        payload = json.loads(row["payload"])
        root = self.data_root / job_id
        self._discard_output(job_id)
        assets = root / "assets/library"
        assets.mkdir(parents=True, exist_ok=True)
        materials = self._select_materials(payload, job_id)
        paths = [self._download(item, assets) for item in materials]
        provenance = payload["_font_provenance"]
        reference_template = payload["template_id"] in self.reference_templates
        if reference_template:
            deadline_at = (
                float(row["created_at"]) + self.hyperframes_total_timeout_seconds
            )
            variables = self._render_reference(
                payload, job_id, materials, paths, deadline_at=deadline_at
            )
            font_selection = provenance["selection"]
            display_top_text = "\n".join(
                value for key, value in variables.items()
                if key.startswith("top") and value
            )
            engine = "hyperframes"
        else:
            project = self._project(payload, job_id, materials, paths)
            fonts_dir = self._stage_project_fonts(root, provenance)
            if fonts_dir:
                project["render"]["fonts_dir"] = fonts_dir
            project_path = root / "project.json"
            project_path.write_text(
                json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._render(project_path)
            font_selection = project["font_selection"]
            display_top_text = project["scenes"][0]["top_text"]
            engine = "ffmpeg"
        output = root / "output/final.mp4"
        try:
            probe = self._probe(output)
            os.replace(output, root / "output/published.mp4")
        except Exception:
            self._discard_output(job_id)
            raise
        return {
            **probe,
            "template_id": payload["template_id"],
            "batch_id": payload.get("batch_id") or "",
            "batch_index": payload.get("batch_index"),
            "batch_size": payload.get("batch_size"),
            "file_url": f"/v1/files/{job_id}.mp4",
            "engine": engine,
            "font_mode": "template_locked" if reference_template else "selectable",
            "font_selection": font_selection,
            "display_top_text": display_top_text,
            "font_files": provenance["fonts"],
            "private_font_bundle_sha256": provenance["private_bundle_sha256"],
            "material_manifest": [{
                "record_id": item.get("record_id"), "sha256": item.get("sha256"),
                "media_type": item.get("media_type"), "match_level": item.get("match_level"),
            } for item in materials],
        }

    def _update_with_retry(self, job_id: str, status: str, **kwargs) -> bool:
        for attempt in range(1, STATUS_WRITE_ATTEMPTS + 1):
            try:
                self.store.update(job_id, status, **kwargs)
                return True
            except Exception as exc:
                print(
                    f"[matrix-template] status write failed job={job_id} "
                    f"status={status} attempt={attempt}: {exc}",
                    flush=True,
                )
                if attempt < STATUS_WRITE_ATTEMPTS:
                    self.stop_event.wait(STATUS_WRITE_RETRY_SECONDS)
        return False

    def _run_job(self, job_id: str) -> bool:
        if not self._update_with_retry(job_id, "running"):
            return False
        try:
            result = self._execute(job_id)
            if self._update_with_retry(job_id, "completed", result=result):
                return True
            self._discard_output(job_id)
            if self._update_with_retry(
                job_id, "failed", error="模板成片完成状态保存失败"
            ):
                return True
            return False
        except Exception as exc:
            self._discard_output(job_id)
            return self._update_with_retry(job_id, "failed", error=exc)

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job_id = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.queue_lock:
                self.queued_jobs.discard(job_id)
                self.active_jobs.add(job_id)
            finished = False
            try:
                finished = self._run_job(job_id)
            except Exception as exc:
                print(
                    f"[matrix-template] unexpected worker error job={job_id}: {exc}",
                    flush=True,
                )
            finally:
                with self.queue_lock:
                    self.active_jobs.discard(job_id)
                self.jobs.task_done()
            if finished:
                self._clear_job_degraded(job_id)
            else:
                self._mark_job_degraded(job_id)
                if not self.stop_event.wait(JOB_REQUEUE_SECONDS):
                    self._enqueue(job_id)

    def _mark_job_degraded(self, job_id: str) -> None:
        with self.degraded_lock:
            self.degraded_jobs.add(job_id)
            self.worker_degraded.set()

    def _clear_job_degraded(self, job_id: str) -> None:
        with self.degraded_lock:
            self.degraded_jobs.discard(job_id)
            if not self.degraded_jobs:
                self.worker_degraded.clear()

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.process_lock:
            processes = list(self.active_processes)
        for process in processes:
            self._terminate(process)
        workers = self.workers or ([self.worker] if self.worker is not None else [])
        for worker in workers:
            worker.join(timeout=3)
        if self.cleanup_worker is not None:
            self.cleanup_worker.join(timeout=3)


class Handler(BaseHTTPRequestHandler):
    server_version = "HuangqueMatrixTemplate/1.0"

    @property
    def service(self) -> MatrixTemplateService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        print("[matrix-template] " + fmt % args, flush=True)

    def send_json(self, status: int, payload: dict):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        supplied = value[7:].strip() if value.lower().startswith("bearer ") else ""
        expected = self.server.api_token  # type: ignore[attr-defined]
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            health = self.service.health()
            self.send_json(200 if health["ok"] else 503, {
                **health, "build_id": runtime_build_id(),
                "templates": len(self.service.catalog),
                "concurrency": self.service.concurrency,
                "max_batch_size": MAX_BATCH_SIZE,
                "engine_concurrency": {
                    "ffmpeg": self.service.concurrency,
                    "hyperframes": self.service.hyperframes_concurrency,
                },
            })
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        if path == "/v1/templates":
            self.send_json(200, {
                "templates": self.service.catalog,
                "default_template": "full-overlay-bold",
                "fonts": self.service.public_fonts(),
                "default_font": "",
                "max_batch_size": MAX_BATCH_SIZE,
                "hyperframes_concurrency": self.service.hyperframes_concurrency,
                "engine_concurrency": {
                    "ffmpeg": self.service.concurrency,
                    "hyperframes": self.service.hyperframes_concurrency,
                },
            })
            return
        if path.startswith("/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            if not JOB_RE.fullmatch(job_id):
                self.send_json(404, {"error": "not_found"})
                return
            row = self.service.store.get(job_id)
            self.send_json(200, self.service.store.public(row)) if row else self.send_json(404, {"error": "not_found"})
            return
        match = re.fullmatch(r"/v1/files/([0-9a-f]{32})\.mp4", path)
        if match:
            file_context = self.service.open_completed_file(match.group(1))
            try:
                handle = file_context.__enter__()
            except (FileNotFoundError, OSError):
                self.send_json(404, {"error": "not_found"})
                return
            try:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                copyfileobj(handle, self.wfile, 1024 * 1024)
            except BaseException:
                file_context.__exit__(*sys.exc_info())
                raise
            else:
                file_context.__exit__(None, None, None)
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/v1/jobs", "/v1/preflight"}:
            self.send_json(404, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if path == "/v1/preflight":
                payload = self.service.validate_payload(body)
                self.send_json(200, {
                    "ok": True,
                    "payload": payload,
                    "duration": payload["duration"],
                    "required_visuals": self.service.required_visuals(payload),
                    "duration_mode": (
                        "random_integer_8_15"
                        if payload["template_id"] in self.service.reference_templates
                        else "copy_length"
                    ),
                })
                return
            request_id = str(self.headers.get("X-Request-Id") or "")
            job = self.service.submit(body, request_id)
            self.send_json(202, job)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": "invalid_request", "detail": str(exc)})
        except MatrixTemplateError as exc:
            self.send_json(409, {"error": "submission_failed", "detail": str(exc)})


def build_server(host: str, port: int, service: MatrixTemplateService, token: str):
    if not token:
        raise SystemExit("MATRIX_TEMPLATE_API_TOKEN is required")
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service  # type: ignore[attr-defined]
    server.api_token = token  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8112)
    args = parser.parse_args()
    reference_root_value = os.environ.get(
        "MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT", ""
    ).strip()
    service = MatrixTemplateService(
        data_root=Path(os.environ.get("MATRIX_TEMPLATE_DATA_ROOT", "/var/lib/huangque-matrix-template")),
        skill_root=Path(os.environ.get("MATRIX_TEMPLATE_SKILL_ROOT", "/opt/huangque/matrix-template-video/source/skill/script-to-matrix-video")),
        library_url=os.environ.get("PIXELLE_MATERIAL_LIBRARY_URL", "http://127.0.0.1:8111"),
        library_token=os.environ.get("PIXELLE_MATERIAL_LIBRARY_TOKEN", ""),
        python=os.environ.get("MATRIX_TEMPLATE_PYTHON", sys.executable),
        private_font_root=Path(os.environ.get(
            "MATRIX_TEMPLATE_PRIVATE_FONT_ROOT",
            "/var/lib/huangque-matrix-template/private-fonts",
        )),
        reference_skill_root=Path(reference_root_value) if reference_root_value else None,
        hyperframes_cli=Path(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_CLI", "/usr/local/bin/hyperframes"
        )),
        hyperframes_gsap=Path(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_GSAP",
            "/opt/huangque/matrix-template-video/source/reference-runtime/node_modules/gsap/dist/gsap.min.js",
        )),
        hyperframes_browser=Path(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_BROWSER", "/usr/bin/google-chrome-stable"
        )),
        hyperframes_concurrency=int(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY",
            str(DEFAULT_HYPERFRAMES_CONCURRENCY),
        )),
        hyperframes_total_timeout_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS",
            str(DEFAULT_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS),
        )),
        hyperframes_slot_timeout_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_SLOT_TIMEOUT_SECONDS",
            str(DEFAULT_HYPERFRAMES_SLOT_TIMEOUT_SECONDS),
        )),
        concurrency=int(os.environ.get("MATRIX_TEMPLATE_CONCURRENCY", "1")),
        retention_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS
        )),
        delivery_grace_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS", DEFAULT_DELIVERY_GRACE_SECONDS
        )),
        cleanup_interval_seconds=int(os.environ.get(
            "MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS", DEFAULT_CLEANUP_INTERVAL_SECONDS
        )),
        cleanup_batch_size=int(os.environ.get(
            "MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE", DEFAULT_CLEANUP_BATCH_SIZE
        )),
        disk_high_water_percent=float(os.environ.get(
            "MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT", DEFAULT_DISK_HIGH_WATER_PERCENT
        )),
    )
    server = build_server(
        args.host, args.port, service,
        os.environ.get("MATRIX_TEMPLATE_API_TOKEN", ""),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.shutdown()


if __name__ == "__main__":
    main()
