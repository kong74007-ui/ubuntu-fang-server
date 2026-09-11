#!/usr/bin/env python3
"""Internal API for catalog-driven text-media-text matrix videos."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import html
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
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageDraw, ImageFont


MAX_BODY_BYTES = 128 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_WAITING_JOBS = 20
MAX_BATCH_SIZE = 5
MATERIAL_SELECTION_CONTRACT_VERSION = 2
MATERIAL_CLIP_CONTRACT_VERSION = 3
PUBLIC_TEMPLATE_PALETTE_VERSION = "reference-palettes-v1"
PUBLIC_TEMPLATE_PALETTE_COUNT = 20
MAX_MATERIAL_CLIP_START_SECONDS = 30 * 60
MAX_MATERIAL_CLIP_SLOTS = 600
MAX_MATERIAL_CLIP_DURATION_SECONDS = 5.0
MATERIAL_LIBRARY_READINESS_TTL_SECONDS = 5.0
PEXELS_API_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_SEARCH_CACHE_SECONDS = 24 * 60 * 60
PEXELS_SEARCH_RESPONSE_BYTES = 4 * 1024 * 1024
PEXELS_SEARCH_PER_PAGE = 80
PEXELS_CHINA_QUERIES = (
    "中国城市生活", "中国商务团队", "中国女性聚会", "中国办公室",
    "中国创业者", "中国社交活动", "中国健康生活", "中国商业交流",
    "中国餐厅聚会", "中国都市女性",
)
RENDER_TIMEOUT_SECONDS = 900
REFERENCE_BGM_PREPARE_TIMEOUT_SECONDS = 120
NINE_GRID_PREPARE_CLIP_TIMEOUT_SECONDS = 120
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
# 用户自带素材（内测期新增）：走 provider="user"，文件在落盘时就按 sha256 存好。
MATERIAL_PROVIDER_USER = "user"
MATERIAL_POLICIES = frozenset({"shared", "owned_public"})
USER_ASSET_DIRNAME = "user-assets"
MAX_USER_ASSET_BYTES = 128 * 1024 * 1024
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
NINE_GRID_TEMPLATE_ID = "nine-grid-reveal"
NINE_GRID_TEMPLATE_VERSION = 4
NINE_GRID_HYPERFRAMES_VERSION = "0.8.33"
NINE_GRID_DURATION_SECONDS = 12.0
NINE_GRID_VISUAL_COUNT = 9
NINE_GRID_SELECTED_CLIP_SECONDS = 3.0
NINE_GRID_RENDER_CLIP_SECONDS = 3.2
NINE_GRID_OUTPUT_FPS = 30
NINE_GRID_HIDDEN_TAIL_SECONDS = 1 / NINE_GRID_OUTPUT_FPS
NINE_GRID_MAIN_SLOT_INDEXES = (0, 4, 8)
NINE_GRID_BOUND_BGM_SHA256 = (
    "d9b3d892623b9dfc9dee4f8642e2844e3700c13a9795e76b48e3a96b24ac9874"
)
NINE_GRID_TOP_FONT = {
    "file": "NotoSerifSC-Variable.ttf", "weight": 900,
    "maximum": 82, "minimum": 46, "width": 800,
    "height": 340, "line_height": 1.08, "max_lines": 4,
}
NINE_GRID_BOTTOM_FONT = {
    "file": "NotoSansSC-Variable.ttf", "weight": 900,
    "maximum": 58, "minimum": 40, "width": 930,
    "height": 250, "line_height": 1.12, "max_lines": 4,
}
FIXED_SKILL_HYPERFRAMES_VERSION = "0.8.33"
MOTION_V2_HYPERFRAMES_VERSION = "0.8.34"
TRIPLE_STRIP_TEMPLATE_ID = "triple-strip-shutter"
YELLOW_BANNER_TEMPLATE_ID = "yellow-banner-zoom"
FAN_WHIP_TEMPLATE_ID = "fan-whip-static"
BRUSH_PANEL_TEMPLATE_ID = "brush-panel-transitions"
MOTION_V2_TEMPLATE_IDS = (
    FAN_WHIP_TEMPLATE_ID, BRUSH_PANEL_TEMPLATE_ID,
)
FIXED_SKILL_TEMPLATE_IDS = (
    TRIPLE_STRIP_TEMPLATE_ID, YELLOW_BANNER_TEMPLATE_ID,
    *MOTION_V2_TEMPLATE_IDS,
)
FIXED_SKILL_TEMPLATE_CONFIGS = {
    TRIPLE_STRIP_TEMPLATE_ID: {
        "name": "三横屏开场·光栅快切",
        "description": "三条横屏同时开场，随后五段全屏光栅快切",
        "variant": "triple-strip",
        "version": 1,
        "duration": 17.6,
        "frames": 528,
        "required_visuals": 8,
        "slot_frames": (117, 117, 117, 82, 82, 82, 82, 83),
        "slot_heights": (640, 640, 640, 1920, 1920, 1920, 1920, 1920),
        "media_paths": (
            "assets/opening/01.mp4", "assets/opening/02.mp4",
            "assets/opening/03.mp4", "assets/main/01.mp4",
            "assets/main/02.mp4", "assets/main/03.mp4",
            "assets/main/04.mp4", "assets/main/05.mp4",
        ),
        "bgm_path": "assets/audio/bound-bgm.m4a",
        "bgm_sha256": (
            "96895f960060f986c034c13fcd5eb8ef1f467c7da250104f4d1976fd86b3c558"
        ),
        "bgm_duration": 17.577007,
        "font_files": {"Noto Sans SC": "NotoSansSC-Variable.ttf"},
        "semantic": {
            "top1": {
                "family": "Noto Sans SC", "font_size_px": 64,
                "font_weight": 900, "max_width_px": 732,
                "max_lines": 2, "stroke_px": 4,
                "letter_spacing_em": -0.065,
            },
            "top2": {
                "family": "Noto Sans SC", "font_size_px": 38,
                "font_weight": 900, "max_width_px": 738,
                "max_lines": 3, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
            "bottom2": {
                "family": "Noto Sans SC", "font_size_px": 26,
                "font_weight": 750, "max_width_px": 620,
                "max_lines": 4, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
        },
        "field_specs": {
            "title": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 186, "minimum": 64, "width": 732,
                "height": 228, "line_height": 1.0, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.065,
            },
            "subtitle": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 78, "minimum": 38, "width": 738,
                "height": 135, "line_height": 1.05, "max_lines": 3,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "ctaLine1": {
                "family": "Noto Sans SC", "weight": 750,
                "maximum": 69, "minimum": 26, "width": 620,
                "height": 94, "line_height": 1.1, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "ctaLine2": {
                "family": "Noto Sans SC", "weight": 750,
                "maximum": 69, "minimum": 26, "width": 620,
                "height": 94, "line_height": 1.1, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
        },
    },
    YELLOW_BANNER_TEMPLATE_ID: {
        "name": "黄条标题·变幅冲击",
        "description": "黄条信息标题与三段素材变幅冲击",
        "variant": "yellow-banner",
        "version": 1,
        "duration": 302 / 30,
        "frames": 302,
        "required_visuals": 3,
        "slot_frames": (86, 97, 119),
        "slot_heights": (1920, 1920, 1920),
        "media_paths": (
            "assets/media/01.mp4", "assets/media/02.mp4",
            "assets/media/03.mp4",
        ),
        "bgm_path": "assets/audio/bound-bgm.m4a",
        "bgm_sha256": (
            "7822689569adca0db3ca2113cb17d2a0ace947a2af8d220a6e96f2b9cfe8db8f"
        ),
        "bgm_duration": 10.053991,
        "font_files": {
            "Noto Sans SC": "NotoSansSC-Variable.ttf",
            "Noto Serif SC": "NotoSerifSC-Variable.ttf",
        },
        "semantic": {
            "top1": {
                "family": "Noto Sans SC", "font_size_px": 50,
                "font_weight": 900, "max_width_px": 804,
                "max_lines": 2,
            },
            "top2": {
                "family": "Noto Sans SC", "font_size_px": 38,
                "font_weight": 900, "max_width_px": 900,
                "max_lines": 2, "stroke_px": 9,
            },
            "top3": {
                "family": "Noto Sans SC", "font_size_px": 38,
                "font_weight": 900, "max_width_px": 900,
                "max_lines": 2, "stroke_px": 9,
            },
            "bottom2": {
                "family": "Noto Sans SC", "font_size_px": 32,
                "font_weight": 750, "max_width_px": 787,
                "max_lines": 4,
            },
        },
        "field_specs": {
            "title": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 81, "minimum": 50, "width": 804,
                "height": 138, "line_height": 1.05, "max_lines": 2,
            },
            "subtitle1": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 63, "minimum": 38, "width": 900,
                "height": 78, "line_height": 1.0, "max_lines": 2,
                "stroke_px": 9,
            },
            "subtitle2": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 63, "minimum": 38, "width": 900,
                "height": 78, "line_height": 1.0, "max_lines": 2,
                "stroke_px": 9,
            },
            "body": {
                "family": "Noto Sans SC", "weight": 750,
                "maximum": 48, "minimum": 32, "width": 787,
                "height": 173, "line_height": 1.15625, "max_lines": 3,
            },
            "cta": {
                "family": "Noto Serif SC", "weight": 700,
                "maximum": 45, "minimum": 30, "width": 882,
                "height": 90, "line_height": 1.1, "max_lines": 1,
                "stroke_px": 6,
            },
        },
    },
    FAN_WHIP_TEMPLATE_ID: {
        "name": "三屏旋展甩切·红黄粗体",
        "description": "三屏旋展、扇形卡片与甩切动效，红黄粗体常驻",
        "variant": "fan-whip",
        "version": 1,
        "manifest_version": None,
        "hyperframes_version": MOTION_V2_HYPERFRAMES_VERSION,
        "composition_id": "main",
        "duration": 377 / 30,
        "frames": 377,
        "required_visuals": 5,
        "slot_frames": (120, 120, 120, 120, 120),
        "slot_heights": (1920, 1920, 1920, 1920, 1920),
        "media_paths": (
            "assets/media/01-aspect-fixed.mp4",
            "assets/media/02-aspect-fixed.mp4",
            "assets/media/03-aspect-fixed.mp4",
            "assets/media/04-aspect-fixed.mp4",
            "assets/media/05.mp4",
        ),
        "bgm_path": "assets/media/reference-bgm.m4a",
        "bgm_sha256": (
            "95183944e0c5f63e583c52686bff3a57d94fa078c22eee10fd5749026c5fdae8"
        ),
        "bgm_duration": 12.538776,
        "bgm_manifest_key": "referenceAudio",
        "audio_id": "reference-bgm",
        "font_files": {"Noto Sans SC": "NotoSansSC-Variable.ttf"},
        "expected_fields": ("title", "subtitle", "body", "cta"),
        "extra_variable_ids": (),
        "required_files": (
            "hyperframes.json", "index.motion.json",
            "assets/vendor/gsap.min.js",
        ),
        "still_frames": (
            (
                "assets/media/01-aspect-fixed.mp4",
                "assets/media/01-aspect-fixed.jpg", 0.5,
            ),
            (
                "assets/media/02-aspect-fixed.mp4",
                "assets/media/02-aspect-fixed.jpg", 0.5,
            ),
            (
                "assets/media/03-aspect-fixed.mp4",
                "assets/media/03-aspect-fixed.jpg", 0.5,
            ),
        ),
        "semantic": {
            "top1": {
                "family": "Noto Sans SC", "font_size_px": 54,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 11,
                "letter_spacing_em": -0.025,
            },
            "top2": {
                "family": "Noto Sans SC", "font_size_px": 46,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
            "top3": {
                "family": "Noto Sans SC", "font_size_px": 38,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
            "bottom2": {
                "family": "Noto Sans SC", "font_size_px": 42,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 4, "stroke_px": 10,
                "letter_spacing_em": -0.025,
            },
        },
        "field_specs": {
            "title": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 96, "minimum": 54, "width": 996,
                "height": 216, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 11, "letter_spacing_em": -0.025,
            },
            "subtitle": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 82, "minimum": 46, "width": 996,
                "height": 184, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "body": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 62, "minimum": 38, "width": 996,
                "height": 140, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "cta": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 86, "minimum": 42, "width": 996,
                "height": 220, "line_height": 1.16, "max_lines": 4,
                "stroke_px": 10, "letter_spacing_em": -0.025,
            },
        },
    },
    BRUSH_PANEL_TEMPLATE_ID: {
        "name": "横屏笔刷分片·上下黑底",
        "description": "横屏素材居中，笔刷、分片与几何窗口连续切换",
        "variant": "brush-panel",
        "version": 1,
        "manifest_version": None,
        "hyperframes_version": MOTION_V2_HYPERFRAMES_VERSION,
        "composition_id": BRUSH_PANEL_TEMPLATE_ID,
        "duration": 15.133333,
        "frames": 454,
        "required_visuals": 7,
        "slot_frames": (150, 150, 150, 150, 150, 150, 150),
        "slot_heights": (1920, 1920, 1920, 1920, 1920, 1920, 1920),
        "media_paths": (
            "assets/media/00.mp4", "assets/media/01.mp4",
            "assets/media/02.mp4", "assets/media/03.mp4",
            "assets/media/04.mp4", "assets/media/05.mp4",
            "assets/media/06.mp4",
        ),
        "bgm_path": "assets/media/reference-bgm.m4a",
        "bgm_sha256": (
            "f3327c91050b6b77c95ffde4d5aeb3926ec447bb4de2d7a45a77acdeba74c4ea"
        ),
        "bgm_duration": 15.116009,
        "bgm_manifest_key": "referenceAudio",
        "audio_id": "bound-bgm",
        "font_files": {"Noto Sans SC": "NotoSansSC-Variable.ttf"},
        "expected_fields": ("title", "subtitle", "body", "cta"),
        "extra_variable_ids": tuple(f"media{index}" for index in range(8)),
        "required_files": (
            "index.motion.json", "assets/vendor/gsap.min.js",
        ),
        "semantic": {
            "top1": {
                "family": "Noto Sans SC", "font_size_px": 54,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 11,
                "letter_spacing_em": -0.025,
            },
            "top2": {
                "family": "Noto Sans SC", "font_size_px": 46,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
            "top3": {
                "family": "Noto Sans SC", "font_size_px": 38,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 2, "stroke_px": 4,
                "letter_spacing_em": -0.025,
            },
            "bottom2": {
                "family": "Noto Sans SC", "font_size_px": 42,
                "font_weight": 900, "max_width_px": 996,
                "max_lines": 4, "stroke_px": 10,
                "letter_spacing_em": -0.025,
            },
        },
        "field_specs": {
            "title": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 96, "minimum": 54, "width": 996,
                "height": 216, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 11, "letter_spacing_em": -0.025,
            },
            "subtitle": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 82, "minimum": 46, "width": 996,
                "height": 184, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "body": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 62, "minimum": 38, "width": 996,
                "height": 140, "line_height": 1.12, "max_lines": 2,
                "stroke_px": 4, "letter_spacing_em": -0.025,
            },
            "cta": {
                "family": "Noto Sans SC", "weight": 900,
                "maximum": 86, "minimum": 42, "width": 996,
                "height": 220, "line_height": 1.16, "max_lines": 4,
                "stroke_px": 10, "letter_spacing_em": -0.025,
            },
        },
    },
}
REFERENCE_FEATURED_VARIANT = "v05"
REFERENCE_V01_VARIANT = "v01"
REFERENCE_V07_VARIANT = "v07"
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
REFERENCE_V07_STYLE_CONTRACT = {
    "top1": (
        'font-family:"notosc"',
        "font-size:118px",
        "font-weight:900",
        "letter-spacing:-0.045em",
        "color:#d4140d",
        "-webkit-text-stroke:13px#ffe9be",
        "paint-order:strokefill",
    ),
    "top2": (
        'font-family:"notosc"',
        "font-size:82px",
        "font-weight:900",
        "letter-spacing:-0.045em",
        "color:#ffd51c",
        "-webkit-text-stroke:11px#101010",
        "paint-order:strokefill",
    ),
    "top3": (
        'font-family:"notosc"',
        "font-size:51px",
        "font-weight:900",
        "letter-spacing:-0.045em",
        "color:#ffd51c",
        "-webkit-text-stroke:8px#101010",
        "paint-order:strokefill",
    ),
    "bottom1": (
        'font-family:"notosc"',
        "font-size:57px",
        "font-weight:900",
        "letter-spacing:-0.045em",
        "color:#d4140d",
        "-webkit-text-stroke:9px#ffe9be",
        "paint-order:strokefill",
    ),
    "bottom2": (
        'font-family:"notosc"',
        "font-size:86px",
        "font-weight:900",
        "letter-spacing:-0.045em",
        "color:#d4140d",
        "-webkit-text-stroke:11px#ffe9be",
        "paint-order:strokefill",
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
REFERENCE_MIN_SEGMENT_SECONDS = 2.0
REFERENCE_MAX_SEGMENT_SECONDS = 3.0
REFERENCE_VIDEO_IDS = ("videoA", "videoB", "videoC", "videoD", "videoE")
REFERENCE_DYNAMIC_TIMING_JS = """      const segment = duration / 3;
      const segmentStarts = [0, segment, segment * 2];
      const segmentDurations = [segment, segment, duration - segment * 2];"""
REFERENCE_BASE_TIMELINE_JS = '''      const tl = gsap.timeline({ paused: true });
      videos.forEach((video, index) => {
        tl.fromTo(video, { scale: 1.015 }, { scale: 1.04, duration: segmentDurations[index], ease: "none" }, segmentStarts[index]);
      });
      window.__timelines["main"] = tl;'''
REFERENCE_EDITING_PLAN_VERSION = 2
REFERENCE_EDITING_SCRIPT_ID = "matrix-reference-editing-plan"
REFERENCE_EDITING_STYLE_ID = "matrix-reference-editing-layers"
REFERENCE_MOTIONS = (
    "slow_push", "pull_back", "pan_left", "pan_right",
    "pan_up", "tilt", "handheld", "breath_zoom",
)
REFERENCE_ENTRANCES = (
    "zoom_in", "slide_left", "slide_right", "slide_up",
    "circle_reveal", "diagonal_reveal",
)
REFERENCE_EXITS = (
    "zoom_out", "slide_left", "slide_right", "slide_down",
    "circle_close", "diagonal_close",
)
REFERENCE_TRANSITIONS = (
    "whip_left", "whip_right", "zoom_swap", "diagonal_wipe",
    "page_turn", "cube_flip",
)
REFERENCE_FORBIDDEN_COLOR_EFFECTS = (
    "filter", "mix-blend-mode", "mixblendmode", "hue-rotate",
    "saturate(", "contrast(", "grayscale(", "sepia(",
    "brightness(", "invert(", "opacity", "background:",
    "backgroundcolor", "color:", "box-shadow", "boxshadow",
    "rgb(", "rgba(", "hsl(", "hsla(", "linear-gradient(",
    "radial-gradient(", "fecolormatrix", "color-matrix",
    "<canvas", "webgl", "shader",
)
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
    "v16": {
        "top2": {
            "family": "Smiley Sans Oblique",
            "alias": "HQSmileySansOblique",
            "font_size_px": 68,
        },
        "bottom1": {
            "family": "Smiley Sans Oblique",
            "alias": "HQSmileySansOblique",
            "font_size_px": 70,
        },
        "bottom2": {
            "family": "Smiley Sans Oblique",
            "alias": "HQSmileySansOblique",
            "font_size_px": 70,
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


def _reference_variant_has_layer(
    index_html: str, variant: str, layer: str,
) -> bool:
    """True when the template's own CSS declares the layer for the variant.

    The service and the installer's post-palette compatibility gate both use
    this single predicate, so an injected overlay can never shadow detection.
    """
    return bool(re.search(
        rf"\.{re.escape(variant)}\s+\.{re.escape(layer)}\s*(?:,|\{{)",
        index_html,
    ))


def _reference_variant_layer_plan(
    index_html: str, variant: str,
) -> tuple[int, list[str]]:
    if variant == REFERENCE_V07_VARIANT:
        return 4, ["top1", "top2", "top3", "bottom1"]
    top_layer_count = 3 if _reference_variant_has_layer(
        index_html, variant, "top3",
    ) else 2
    return top_layer_count, ["top1", "top2"] + (
        ["top3"] if top_layer_count == 3 else []
    )


def reference_pack_layer_audit(index_html: str) -> dict:
    """Re-parse the reference pack exactly like the service does.

    Returns the template count, the top-layer-count histogram and every present
    layer's parsed ``font_size_px``. Raises if any layer is missing or its size
    cannot be parsed, so the installer can fail before switching the release.
    """
    histogram = {"2": 0, "3": 0, "4": 0}
    font_sizes: dict[str, int] = {}
    for index in range(1, REFERENCE_TEMPLATE_COUNT + 1):
        variant = f"v{index:02d}"
        if not all(
            _reference_variant_has_layer(index_html, variant, layer)
            for layer in ("top1", "top2")
        ):
            raise MatrixTemplateError(
                "HyperFrames reference template top layer styles are incomplete"
            )
        top_layer_count, top_layers = _reference_variant_layer_plan(
            index_html, variant,
        )
        histogram[str(top_layer_count)] += 1
        for layer in top_layers + ["bottom2"]:
            metrics = _reference_css_layer_metrics(
                index_html, variant, layer, 2,
            )
            font_sizes[f"{variant}.{layer}"] = int(metrics["font_size_px"])
    return {
        "templates": REFERENCE_TEMPLATE_COUNT,
        "top_layer_counts": histogram,
        "font_sizes": font_sizes,
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
    count = max(1, math.ceil(float(duration) / REFERENCE_MAX_SEGMENT_SECONDS))
    segment_duration = float(duration) / count
    if not REFERENCE_MIN_SEGMENT_SECONDS <= segment_duration <= REFERENCE_MAX_SEGMENT_SECONDS:
        raise MatrixTemplateError("模板单素材时长必须在 2 到 3 秒之间")
    return count


def _material_source_plan(
    count: int, *, include_middle_library: bool = False, seed: str = "",
) -> tuple[str, ...]:
    """Return the source assigned to each visible clip.

    Three-clip outputs keep the opening clip in the approved Huangque library.
    Four-, five-, eight- and nine-clip outputs keep both bookends there. All
    middle clips are supplied by the Pexels China-oriented search pool.
    """
    if include_middle_library:
        if count < 5:
            raise MatrixTemplateError(
                "三段黄雀素材规则至少需要 5 个画面位"
            )
        plan = ["pexels"] * count
        plan[0] = plan[-1] = "huangque"
        middle = 1 + (
            int.from_bytes(
                hashlib.sha256(
                    f"matrix-middle-library:{seed}:{count}".encode("utf-8")
                ).digest()[:4],
                "big",
            ) % (count - 2)
        )
        plan[middle] = "huangque"
        return tuple(plan)
    if count == 3:
        return ("huangque", "pexels", "pexels")
    if count in {4, 5, 8, 9}:
        return tuple(
            "huangque" if index in {0, count - 1} else "pexels"
            for index in range(count)
        )
    raise MatrixTemplateError("模板素材片段数量必须在 3 到 5（或九宫格 9 / 三横屏 8）之间")


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
    4: {
        1: (1, 0, 0, 0),
        2: (1, 1, 0, 0),
        3: (1, 1, 1, 0),
        4: (1, 1, 1, 1),
        5: (1, 2, 1, 1),
        6: (1, 2, 2, 1),
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
    if top_layer_count == 4:
        bottom_groups = [bottom_lines]
    else:
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
    # 动效模板时间轴按 8-15 秒设计（模板变量声明 duration min=8），
    # 随机出 7 秒会被引擎 strict-variables 拒绝（Variable validation failed）。
    return 8 + int.from_bytes(digest[:8], "big") % 8


def _reference_effect_order(seed: str, category: str,
                            values: tuple[str, ...]) -> list[str]:
    return sorted(
        values,
        key=lambda value: hashlib.sha256(
            f"{seed}:{category}:{value}".encode("utf-8")
        ).digest(),
    )


def _reference_editing_plan(job_id: str, template_id: str,
                            segment_count: int = 3) -> dict:
    if not 3 <= segment_count <= len(REFERENCE_VIDEO_IDS):
        raise MatrixTemplateError("HyperFrames 剪辑片段数量无效")
    seed = hashlib.sha256(
        f"{job_id}:{template_id}:reference-editing-v2".encode("utf-8")
    ).hexdigest()[:16]
    motions = _reference_effect_order(seed, "motion", REFERENCE_MOTIONS)
    entrances = _reference_effect_order(seed, "entrance", REFERENCE_ENTRANCES)
    exits = _reference_effect_order(seed, "exit", REFERENCE_EXITS)
    transitions = _reference_effect_order(
        seed, "transition", REFERENCE_TRANSITIONS
    )
    return {
        "version": REFERENCE_EDITING_PLAN_VERSION,
        "seed": seed,
        "segments": [
            {
                "index": index + 1,
                "motion": motions[index],
            }
            for index in range(segment_count)
        ],
        "bookends": {
            "entrance": entrances[0],
            "exit": exits[0],
        },
        "transitions": [
            {"boundary": index + 1, "name": transitions[index]}
            for index in range(segment_count - 1)
        ],
        "color_effects": [],
    }


def _validate_reference_editing_plan(value) -> dict:
    if not isinstance(value, dict):
        raise MatrixTemplateError("HyperFrames 剪辑方案无效")
    segments = value.get("segments")
    transitions = value.get("transitions")
    bookends = value.get("bookends")
    if (
        value.get("version") != REFERENCE_EDITING_PLAN_VERSION
        or not re.fullmatch(r"[0-9a-f]{16}", str(value.get("seed") or ""))
        or not isinstance(segments, list)
        or not 3 <= len(segments) <= len(REFERENCE_VIDEO_IDS)
        or not isinstance(transitions, list)
        or len(transitions) != len(segments) - 1
        or not isinstance(bookends, dict)
        or bookends.get("entrance") not in REFERENCE_ENTRANCES
        or bookends.get("exit") not in REFERENCE_EXITS
        or set(bookends) != {"entrance", "exit"}
        or value.get("color_effects") != []
    ):
        raise MatrixTemplateError("HyperFrames 剪辑方案无效")
    for index, segment in enumerate(segments, 1):
        if (
            not isinstance(segment, dict)
            or segment.get("index") != index
            or segment.get("motion") not in REFERENCE_MOTIONS
            or set(segment) != {"index", "motion"}
        ):
            raise MatrixTemplateError("HyperFrames 剪辑方案无效")
    for index, transition in enumerate(transitions, 1):
        transition_keys = set(transition) if isinstance(transition, dict) else set()
        if (
            not isinstance(transition, dict)
            or transition.get("boundary") != index
            or transition.get("name") not in REFERENCE_TRANSITIONS
            or transition_keys != {"boundary", "name"}
        ):
            raise MatrixTemplateError("HyperFrames 剪辑方案无效")
    return value


def _inject_reference_editing_plan(html: str, plan: dict) -> str:
    plan = _validate_reference_editing_plan(plan)
    if (
        REFERENCE_EDITING_SCRIPT_ID in html
        or REFERENCE_EDITING_STYLE_ID in html
        or html.count("</head>") != 1
        or html.count("</body>") != 1
        or html.count(REFERENCE_BASE_TIMELINE_JS) != 1
    ):
        raise MatrixTemplateError("HyperFrames 剪辑脚本声明发生变化")
    html = html.replace(REFERENCE_BASE_TIMELINE_JS, "")
    video_ids = REFERENCE_VIDEO_IDS[:len(plan["segments"])]
    for element_id in video_ids:
        pattern = re.compile(
            rf'(<video\b[^>]*\bid="{element_id}"[^>]*>.*?</video>)',
            re.DOTALL,
        )
        matches = list(pattern.finditer(html))
        if len(matches) != 1:
            raise MatrixTemplateError("HyperFrames 剪辑素材层发生变化")
        video = matches[0].group(1)
        layers = (
            f'<div id="{element_id}-transition" class="matrix-media-transition" '
            f'data-layout-allow-overflow="">'
            f'<div id="{element_id}-motion" class="matrix-media-motion">'
            f'{video}</div></div>'
        )
        html = html[:matches[0].start()] + layers + html[matches[0].end():]
    plan_json = json.dumps(plan, ensure_ascii=True, separators=(",", ":"))
    style = f'''<style id="{REFERENCE_EDITING_STYLE_ID}">
.matrix-media-transition,.matrix-media-motion{{position:absolute;inset:0;width:1080px;height:1920px;overflow:hidden;transform-origin:50% 50%;transform-style:preserve-3d;backface-visibility:hidden}}
</style>'''
    runtime = f'''<script id="{REFERENCE_EDITING_SCRIPT_ID}">
(() => {{
  const plan = {plan_json};
  const videos = {json.dumps(video_ids)}.map(id => document.getElementById(id));
  const transitionLayers = videos.map(video => document.getElementById(`${{video.id}}-transition`));
  const motionLayers = videos.map(video => document.getElementById(`${{video.id}}-motion`));
  const neutral = {{x: 0, y: 0, scale: 1, rotation: 0, rotationX: 0, rotationY: 0, clipPath: "inset(0% 0% 0% 0%)"}};
  const motions = {{
    slow_push: [{{scale: 1.03}}, {{scale: 1.14}}],
    pull_back: [{{scale: 1.15}}, {{scale: 1.03}}],
    pan_left: [{{x: 72, scale: 1.16}}, {{x: -72, scale: 1.16}}],
    pan_right: [{{x: -72, scale: 1.16}}, {{x: 72, scale: 1.16}}],
    pan_up: [{{y: 82, scale: 1.15}}, {{y: -82, scale: 1.15}}],
    tilt: [{{rotation: -2.4, scale: 1.12}}, {{rotation: 2.4, scale: 1.12}}],
    handheld: [{{x: -34, y: 22, rotation: -1.2, scale: 1.13}}, {{x: 38, y: -24, rotation: 1.2, scale: 1.13}}],
    breath_zoom: [{{scale: 1.04}}, {{scale: 1.13}}]
  }};
  const entrances = {{
    zoom_in: {{scale: 1.28}},
    slide_left: {{x: -118, scale: 1.24}},
    slide_right: {{x: 118, scale: 1.24}},
    slide_up: {{y: 150, scale: 1.22}},
    circle_reveal: {{clipPath: "circle(34% at 50% 50%)", scale: 1.12}},
    diagonal_reveal: {{clipPath: "polygon(0 0, 38% 0, 18% 100%, 0 100%)", scale: 1.12}}
  }};
  const exits = {{
    zoom_out: {{scale: 1.28}},
    slide_left: {{x: -118, scale: 1.24}},
    slide_right: {{x: 118, scale: 1.24}},
    slide_down: {{y: 150, scale: 1.22}},
    circle_close: {{clipPath: "circle(34% at 50% 50%)", scale: 1.12}},
    diagonal_close: {{clipPath: "polygon(62% 0, 100% 0, 100% 100%, 82% 100%)", scale: 1.12}}
  }};
  const transitions = {{
    whip_left: [{{x: -150, scale: 1.28, rotation: -1.5}}, {{x: 150, scale: 1.28, rotation: 1.5}}],
    whip_right: [{{x: 150, scale: 1.28, rotation: 1.5}}, {{x: -150, scale: 1.28, rotation: -1.5}}],
    zoom_swap: [{{scale: 1.34}}, {{scale: 1.34}}],
    diagonal_wipe: [{{x: -92, y: -116, scale: 1.24}}, {{x: 92, y: 116, scale: 1.24}}],
    page_turn: [{{rotationY: -42, scale: 1.13}}, {{rotationY: 42, scale: 1.13}}],
    cube_flip: [{{rotationX: 38, scale: 1.14}}, {{rotationX: -38, scale: 1.14}}]
  }};
  const timeline = gsap.timeline({{paused: true}});
  videos.forEach((video, index) => {{
    const start = Number(video.dataset.start);
    const duration = Number(video.dataset.duration);
    const segment = plan.segments[index];
    const motion = motions[segment.motion];
    timeline.fromTo(motionLayers[index], motion[0], {{...motion[1], duration, ease: "none", immediateRender: false}}, start);
  }});
  const firstStart = Number(videos[0].dataset.start);
  const firstEdge = Math.min(0.34, Number(videos[0].dataset.duration) * 0.16);
  timeline.fromTo(transitionLayers[0], entrances[plan.bookends.entrance], {{...neutral, duration: firstEdge, ease: "power3.out", immediateRender: false}}, firstStart);
  plan.transitions.forEach((item, index) => {{
    const outgoingEnd = Number(videos[index].dataset.start) + Number(videos[index].dataset.duration);
    const incomingStart = Number(videos[index + 1].dataset.start);
    const edge = Math.min(0.24, Number(videos[index].dataset.duration) * 0.1, Number(videos[index + 1].dataset.duration) * 0.1);
    const transition = transitions[item.name];
    timeline.to(transitionLayers[index], {{...transition[0], duration: edge, ease: "power4.in"}}, outgoingEnd - edge);
    timeline.fromTo(transitionLayers[index + 1], transition[1], {{...neutral, duration: edge, ease: "power4.out", immediateRender: false}}, incomingStart);
  }});
  const lastIndex = videos.length - 1;
  const lastStart = Number(videos[lastIndex].dataset.start);
  const lastDuration = Number(videos[lastIndex].dataset.duration);
  const lastEdge = Math.min(0.34, lastDuration * 0.16);
  timeline.to(transitionLayers[lastIndex], {{...exits[plan.bookends.exit], duration: lastEdge, ease: "power3.in"}}, lastStart + lastDuration - lastEdge);
  window.__matrixEditingPlan = plan;
  window.__timelines["main"] = timeline;
}})();
</script>'''
    injected = style + runtime
    normalized_injected = injected.lower()
    if any(
        token in normalized_injected
        for token in REFERENCE_FORBIDDEN_COLOR_EFFECTS
    ):
        raise MatrixTemplateError("HyperFrames 剪辑脚本包含禁用调色效果")
    html = html.replace("</head>", style + "\n</head>")
    return html.replace("</body>", runtime + "\n</body>")


def _format_reference_seconds(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _bounded_float(value, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        return None
    return parsed


def _reference_segment_timing(
    total_duration: float, media_durations: list[float], seed: str = ""
) -> tuple[list[float], list[float], list[float]]:
    total = float(total_duration)
    count = len(media_durations)
    if not 7 <= total <= 15 or count != _required_visuals(total):
        raise MatrixTemplateError("HyperFrames 模板素材时间轴参数无效")
    segment_duration = total / count
    capacities = [
        max(0.0, float(value) - REFERENCE_MEDIA_SAFETY_SECONDS)
        for value in media_durations
    ]
    if any(value + 0.001 < segment_duration for value in capacities):
        raise MatrixTemplateError("HyperFrames 模板单素材可用时长不足")
    durations = [segment_duration] * count
    durations[-1] += total - sum(durations)
    if any(
        value < REFERENCE_MIN_SEGMENT_SECONDS
        or value > REFERENCE_MAX_SEGMENT_SECONDS
        or value > capacities[index] + 0.001
        for index, value in enumerate(durations)
    ):
        raise MatrixTemplateError("HyperFrames 模板素材时长分配失败")
    starts = []
    cursor = 0.0
    for duration in durations:
        starts.append(cursor)
        cursor += duration
    media_offsets = []
    for index, duration in enumerate(durations):
        max_start = max(
            0.0, media_durations[index] - duration - REFERENCE_MEDIA_SAFETY_SECONDS
        )
        if max_start <= 0.001:
            media_offsets.append(0.0)
            continue
        digest = hashlib.sha256(
            f"{seed}:reference-media-offset:{index}".encode("utf-8")
        ).digest()
        fraction = int.from_bytes(digest[:4], "big") / float(0xFFFFFFFF)
        media_offsets.append(round(fraction * max_start, 3))
    return starts, durations, media_offsets


def _expand_reference_video_slots(html: str, video_sources: list[str]) -> str:
    count = len(video_sources)
    if not 3 <= count <= len(REFERENCE_VIDEO_IDS):
        raise MatrixTemplateError("HyperFrames 模板视频槽位数量无效")
    if count == 3:
        return html
    video_ids = REFERENCE_VIDEO_IDS[:count]
    pattern = re.compile(r'<video\b[^>]*\bid="videoC"[^>]*></video>')
    matches = list(pattern.finditer(html))
    if len(matches) != 1:
        raise MatrixTemplateError("HyperFrames 模板基础视频槽位发生变化")
    source_tag = matches[0].group(0)
    additions = []
    for index, element_id in enumerate(video_ids[3:], start=3):
        tag, hf_count = re.subn(
            r'data-hf-id="[^"]+"',
            f'data-hf-id="matrix-{element_id}"', source_tag, count=1,
        )
        tag, id_count = re.subn(
            r'id="videoC"', f'id="{element_id}"', tag, count=1,
        )
        tag, variable_count = re.subn(
            r'\sdata-var-src="videoC"', "", tag, count=1,
        )
        tag, source_count = re.subn(
            r'(\ssrc=")[^"]*(")',
            rf'\g<1>{video_sources[index]}\g<2>', tag, count=1,
        )
        if (hf_count, id_count, variable_count, source_count) != (1, 1, 1, 1):
            raise MatrixTemplateError("HyperFrames 模板基础视频槽位发生变化")
        additions.append(tag)
    html = (
        html[:matches[0].end()] + "\n      "
        + "\n      ".join(additions) + html[matches[0].end():]
    )
    variable_anchor = re.compile(
        r'\{&quot;id&quot;:&quot;videoC&quot;.*?&quot;\},'
    )
    anchors = list(variable_anchor.finditer(html))
    if len(anchors) != 1:
        raise MatrixTemplateError("HyperFrames 模板视频变量声明发生变化")
    extra_variables = "".join(
        '{&quot;id&quot;:&quot;%s&quot;,&quot;type&quot;:&quot;string&quot;,'
        '&quot;label&quot;:&quot;素材%s&quot;,'
        '&quot;default&quot;:&quot;assets/library/default-a.mp4&quot;},'
        % (element_id, element_id[-1].upper())
        for element_id in video_ids[3:]
    )
    html = (
        html[:anchors[0].end()] + extra_variables + html[anchors[0].end():]
    )
    array_pattern = re.compile(
        r'(?ms)^      const videos = \[\n.*?^      \];'
    )
    arrays = list(array_pattern.finditer(html))
    if len(arrays) != 1:
        raise MatrixTemplateError("HyperFrames 模板视频时间轴声明发生变化")
    declaration = (
        "      const videos = [\n"
        + ",\n".join(
            f'        document.getElementById("{element_id}")'
            for element_id in video_ids
        )
        + "\n      ];"
    )
    return html[:arrays[0].start()] + declaration + html[arrays[0].end():]


def _rewrite_reference_timeline(
    html: str, total_duration: float,
    starts: list[float], durations: list[float],
    media_offsets: list[float] | None = None,
) -> str:
    offsets = [0.0] * len(starts) if media_offsets is None else media_offsets
    if (
        not 3 <= len(starts) <= len(REFERENCE_VIDEO_IDS)
        or len(durations) != len(starts) or len(offsets) != len(starts)
        or any(
            not math.isfinite(float(value)) or float(value) < 0
            for value in offsets
        )
    ):
        raise MatrixTemplateError("HyperFrames 模板素材时间轴参数无效")

    def rewrite_element(source: str, element_id: str,
                        start: float, duration: float,
                        media_start: float | None = None) -> str:
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
        if media_start is not None:
            occurrences = len(re.findall(r'\sdata-media-start="[^"]*"', tag))
            if occurrences > 1:
                raise MatrixTemplateError("HyperFrames 模板媒体起点属性发生变化")
            value = _format_reference_seconds(media_start)
            if occurrences == 1:
                tag = re.sub(
                    r'(\sdata-media-start=")[^"]*(")',
                    rf'\g<1>{value}\g<2>', tag, count=1,
                )
            else:
                tag, count = re.subn(
                    r'(\sdata-duration="[^"]*")',
                    rf'\g<1> data-media-start="{value}"', tag, count=1,
                )
                if count != 1:
                    raise MatrixTemplateError(
                        "HyperFrames 模板媒体起点属性发生变化"
                    )
        return source[:matches[0].start()] + tag + source[matches[0].end():]

    result = html
    for index, element_id in enumerate(REFERENCE_VIDEO_IDS[:len(starts)]):
        result = rewrite_element(
            result, element_id, starts[index], durations[index], offsets[index]
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
            selection_columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(batch_material_selections)"
                )
            }
            if "selection_contract_version" not in selection_columns:
                db.execute(
                    "ALTER TABLE batch_material_selections ADD COLUMN "
                    "selection_contract_version INTEGER NOT NULL DEFAULT 1"
                )
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

    def material_selection(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT batch_id,materials,selection_contract_version "
                "FROM batch_material_selections WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "batch_id": row["batch_id"],
            "materials": json.loads(row["materials"]),
            "selection_contract_version": int(
                row["selection_contract_version"]
            ),
        }

    def batch_material_selection(self, job_id: str) -> list[dict] | None:
        selection = self.material_selection(job_id)
        return selection["materials"] if selection else None

    def batch_used_visuals(self, batch_id: str) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT sha256 FROM batch_material_reservations WHERE batch_id=? ORDER BY sha256",
                (batch_id,),
            )]

    def _reserve_materials(
        self, batch_id: str, job_id: str, materials: list[dict],
        selection_contract_version: int, *, reserve_batch: bool,
    ) -> None:
        if (
            isinstance(selection_contract_version, bool)
            or selection_contract_version not in {
                1, MATERIAL_SELECTION_CONTRACT_VERSION,
            }
        ):
            raise MatrixTemplateError("material selection contract is invalid")
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
                    "SELECT materials,batch_id,selection_contract_version "
                    "FROM batch_material_selections WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["batch_id"] != batch_id
                        or int(existing["selection_contract_version"])
                        != selection_contract_version
                        or json.loads(existing["materials"]) != materials
                    ):
                        raise MatrixTemplateError("material selection conflict")
                    return
                if reserve_batch:
                    for sha256 in visual_shas:
                        db.execute(
                            "INSERT INTO batch_material_reservations("
                            "batch_id,sha256,job_id,created_at) VALUES(?,?,?,?)",
                            (batch_id, sha256, job_id, now),
                        )
                db.execute(
                    "INSERT INTO batch_material_selections("
                    "job_id,batch_id,materials,created_at,"
                    "selection_contract_version) VALUES(?,?,?,?,?)",
                    (
                        job_id, batch_id,
                        json.dumps(materials, ensure_ascii=False), now,
                        selection_contract_version,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            message = (
                "同批次视觉素材重复，请重新生成"
                if reserve_batch else "素材选择冻结冲突"
            )
            raise MatrixTemplateError(message) from exc

    def reserve_job_materials(
        self, job_id: str, materials: list[dict],
        selection_contract_version: int,
    ) -> None:
        self._reserve_materials(
            "", job_id, materials, selection_contract_version,
            reserve_batch=False,
        )

    def reserve_batch_materials(
        self, batch_id: str, job_id: str, materials: list[dict],
        selection_contract_version: int = 1,
    ) -> None:
        self._reserve_materials(
            batch_id, job_id, materials, selection_contract_version,
            reserve_batch=True,
        )

    def update_material_content_sha256(
        self, job_id: str, scene_id: str, content_sha256: str,
    ) -> bool:
        """原子持久化冻结选择中某片段的内容哈希；已有不同哈希时 fail closed。"""
        digest = str(content_sha256 or "").lower()
        if not SHA_RE.fullmatch(digest):
            raise MatrixTemplateError("Pexels 内容哈希无效")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT materials FROM batch_material_selections WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise MatrixTemplateError("素材选择冻结缺失")
            materials = json.loads(row["materials"])
            for item in materials:
                if str(item.get("scene_id") or "") != str(scene_id):
                    continue
                existing = str(item.get("content_sha256") or "").lower()
                if existing:
                    if existing != digest:
                        raise MatrixTemplateError("Pexels 素材文件发生变化")
                    return False
                item["content_sha256"] = digest
                db.execute(
                    "UPDATE batch_material_selections SET materials=? WHERE job_id=?",
                    (json.dumps(materials, ensure_ascii=False), job_id),
                )
                return True
            raise MatrixTemplateError("素材选择冻结分镜缺失")

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
                 pexels_api_key: str = "",
                 private_font_root: Path | None = None,
                 reference_skill_root: Path | None = None,
                 nine_grid_root: Path | None = None,
                 triple_strip_root: Path | None = None,
                 yellow_banner_root: Path | None = None,
                 fan_whip_root: Path | None = None,
                 brush_panel_root: Path | None = None,
                 hyperframes_cli: Path | None = None,
                 nine_grid_hyperframes_cli: Path | None = None,
                 motion_v2_hyperframes_cli: Path | None = None,
                 hyperframes_gsap: Path | None = None,
                 hyperframes_browser: Path | None = None,
                 hyperframes_concurrency: int = DEFAULT_HYPERFRAMES_CONCURRENCY,
                 hyperframes_total_timeout_seconds: int = DEFAULT_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS,
                 hyperframes_slot_timeout_seconds: int = DEFAULT_HYPERFRAMES_SLOT_TIMEOUT_SECONDS,
                 concurrency: int = 1,
                 legacy_templates_enabled: bool = True,
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
        self.pexels_api_key = str(pexels_api_key or "").strip()
        self.legacy_templates_enabled = bool(legacy_templates_enabled)
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
        self.nine_grid_root = (
            nine_grid_root.resolve() if nine_grid_root else None
        )
        self.nine_grid_template: dict | None = None
        self.nine_grid_fonts: dict[str, dict] = {}
        self.nine_grid_measure_fonts: dict[
            tuple[str, int, int], ImageFont.FreeTypeFont
        ] = {}
        self.fixed_skill_roots = {
            template_id: root.resolve()
            for template_id, root in (
                (TRIPLE_STRIP_TEMPLATE_ID, triple_strip_root),
                (YELLOW_BANNER_TEMPLATE_ID, yellow_banner_root),
                (FAN_WHIP_TEMPLATE_ID, fan_whip_root),
                (BRUSH_PANEL_TEMPLATE_ID, brush_panel_root),
            )
            if root is not None
        }
        self.fixed_skill_templates: dict[str, dict] = {}
        self.fixed_skill_fonts: dict[str, dict[str, dict]] = {}
        self.fixed_skill_source_sha256: dict[str, str] = {}
        self.fixed_skill_measure_fonts: dict[
            tuple[str, str, int, int], ImageFont.FreeTypeFont
        ] = {}
        self.hyperframes_cli = hyperframes_cli.resolve() if hyperframes_cli else None
        self.nine_grid_hyperframes_cli = (
            nine_grid_hyperframes_cli.resolve()
            if nine_grid_hyperframes_cli else None
        )
        self.motion_v2_hyperframes_cli = (
            motion_v2_hyperframes_cli.resolve()
            if motion_v2_hyperframes_cli else None
        )
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
        self.pexels_cache_root = self.data_root / ".pexels-search-cache"
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
        self.pexels_cache_lock = threading.Lock()
        self.library_readiness_lock = threading.Lock()
        self._library_readiness_cache: tuple[float, dict] | None = None
        self.active_downloads: set[str] = set()
        self.active_processes: set[subprocess.Popen] = set()
        self.active_process = None
        self.workers = []
        self.worker = None
        self.cleanup_worker = None
        self.workers_expected = start_worker
        self.enforce_library_readiness = bool(start_worker)
        self.catalog = (
            self._load_catalog() if self.legacy_templates_enabled else []
        )
        if self.reference_skill_root is not None:
            self.catalog.extend(self._load_reference_catalog())
        if self.nine_grid_root is not None:
            self.catalog.append(self._load_nine_grid_catalog())
        for template_id in FIXED_SKILL_TEMPLATE_IDS:
            if template_id in self.fixed_skill_roots:
                self.catalog.append(self._load_fixed_skill_template(template_id))
        if not self.catalog and start_worker:
            raise MatrixTemplateError("no public matrix templates are available")
        self.default_template_id = (
            self.catalog[0]["id"] if self.catalog else ""
        )
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
            return _reference_variant_has_layer(index_html, variant, layer)

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
            top_layer_count, top_layers = _reference_variant_layer_plan(
                index_html, variant,
            )
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
            if variant == REFERENCE_V07_VARIANT and not all(
                variant_layer_matches_contract(variant, layer, required)
                for layer, required in REFERENCE_V07_STYLE_CONTRACT.items()
            ):
                raise MatrixTemplateError(
                    "v07 HyperFrames template style contract changed"
                )
            record = {
                "id": template_id,
                "name": str(item.get("name") or template_id)[:40],
                "description": str(item.get("description") or "")[:160],
                "tags": ["HyperFrames", "固定排版", "内置字体"],
                "engine": "hyperframes",
                "font_mode": "template_locked",
                "font_selectable": False,
                "text_layers": {
                    "top": top_layer_count,
                    "bottom": 1 if variant == REFERENCE_V07_VARIANT else 2,
                },
                "duration_mode": "random_integer_8_15",
                "required_visuals": 3,
                "required_visuals_max": 5,
                "clip_duration_range_seconds": [
                    REFERENCE_MIN_SEGMENT_SECONDS,
                    REFERENCE_MAX_SEGMENT_SECONDS,
                ],
                "variant": variant,
                "fixed_fonts": {
                    layer: font["family"]
                    for layer, font in fixed_private_fonts.items()
                },
            }
            semantic_contract = {}
            for layer in top_layers + ["bottom2"]:
                if variant == REFERENCE_V07_VARIANT:
                    max_lines = 2
                elif layer == "bottom2":
                    max_lines = 2
                else:
                    max_lines = (
                        2 if top_layer_count == 3 or layer != "top2" else 4
                    )
                semantic_contract[layer] = _reference_css_layer_metrics(
                    index_html, variant, layer, max_lines,
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

    def _load_nine_grid_catalog(self) -> dict:
        root = self.nine_grid_root
        if root is None or root.is_symlink() or not root.is_dir():
            raise MatrixTemplateError("nine-grid template root is unavailable")
        required = (
            "index.html", "template.json", "hyperframes.json",
            "index.motion.json", "assets/audio/reference-bgm.m4a",
            "assets/fonts/NotoSerifSC-Variable.ttf",
            "assets/fonts/NotoSansSC-Variable.ttf",
            "assets/vendor/gsap.min.js",
        )
        for relative in required:
            path = root.joinpath(*relative.split("/"))
            if path.is_symlink() or not path.is_file():
                raise MatrixTemplateError("nine-grid template is incomplete")
        manifest = _read_json(root / "template.json")
        layout = manifest.get("text_layout")
        if (
            manifest.get("id") != NINE_GRID_TEMPLATE_ID
            or manifest.get("version") != NINE_GRID_TEMPLATE_VERSION
            or manifest.get("renderer")
                != f"hyperframes@{NINE_GRID_HYPERFRAMES_VERSION}"
            or manifest.get("canvas") != [1080, 1920]
            or manifest.get("fps") != NINE_GRID_OUTPUT_FPS
            or float(manifest.get("duration") or 0)
                != NINE_GRID_DURATION_SECONDS
            or manifest.get("text_fields") != ["top_text", "bottom_text"]
            or manifest.get("text_limits")
                != {"top_text": 60, "bottom_text": 80}
            or not isinstance(layout, dict)
            or layout.get("mode") != "semantic-then-width"
            or layout.get("semantic_layout_required") is not True
            or layout.get("top_max_lines") != 4
            or layout.get("bottom_max_lines") != 4
            or layout.get("hide_edge_punctuation") is not True
            or layout.get("truncate") is not False
        ):
            raise MatrixTemplateError("nine-grid template contract is invalid")
        binding = manifest.get("bgm")
        bgm_path = root / "assets/audio/reference-bgm.m4a"
        if (
            not isinstance(binding, dict)
            or binding.get("mode") != "bound"
            or binding.get("path") != "assets/audio/reference-bgm.m4a"
            or binding.get("sha256") != NINE_GRID_BOUND_BGM_SHA256
            or binding.get("duration") != 12
            or binding.get("start") != 0
            or binding.get("volume") != 1
            or _file_sha256(bgm_path) != NINE_GRID_BOUND_BGM_SHA256
        ):
            raise MatrixTemplateError("nine-grid bound BGM changed")
        index_html = (root / "index.html").read_text(encoding="utf-8")
        if (
            index_html.count('data-composition-id="nine-grid-reveal"') != 1
            or index_html.count('data-var-text="top_text"') != 1
            or index_html.count('data-var-text="bottom_text"') != 1
            or 'data-var-text="title"' in index_html
            or 'data-var-text="tagline"' in index_html
            or index_html.count('src="assets/audio/reference-bgm.m4a"') != 1
            or not re.search(
                r'<audio\b[^>]*\bid="bgm"[^>]*\bdata-volume="1"',
                index_html,
            )
            or any(
                not re.search(
                    rf'<video\b[^>]*\bid="main-video{index}"[^>]*'
                    r'\bdata-media-start="0"',
                    index_html,
                )
                for index in range(1, 4)
            )
        ):
            raise MatrixTemplateError("nine-grid template HTML contract changed")
        font_specs = {
            "top_text": NINE_GRID_TOP_FONT,
            "bottom_text": NINE_GRID_BOTTOM_FONT,
        }
        fonts = {}
        for role, spec in font_specs.items():
            path = root / "assets/fonts" / spec["file"]
            fonts[role] = {
                "family": (
                    "Noto Serif SC" if role == "top_text"
                    else "Noto Sans SC"
                ),
                "file": spec["file"],
                "path": path,
                "sha256": _file_sha256(path),
            }
        if (
            self.nine_grid_hyperframes_cli is None
            or self.nine_grid_hyperframes_cli.is_symlink()
            or not self.nine_grid_hyperframes_cli.is_file()
        ):
            raise MatrixTemplateError("HyperFrames 0.8.33 CLI is unavailable")
        version = subprocess.run(
            [str(self.nine_grid_hyperframes_cli), "--version"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if (
            version.returncode
            or version.stdout.strip() != NINE_GRID_HYPERFRAMES_VERSION
        ):
            raise MatrixTemplateError("nine-grid HyperFrames CLI version mismatch")
        semantic = {
            "top1": {
                "family": "Noto Serif SC",
                "font_size_px": NINE_GRID_TOP_FONT["maximum"],
                "font_weight": NINE_GRID_TOP_FONT["weight"],
                "max_width_px": NINE_GRID_TOP_FONT["width"],
                "max_lines": NINE_GRID_TOP_FONT["max_lines"],
            },
            "top2": {
                "family": "Noto Serif SC",
                "font_size_px": NINE_GRID_TOP_FONT["maximum"],
                "font_weight": NINE_GRID_TOP_FONT["weight"],
                "max_width_px": NINE_GRID_TOP_FONT["width"],
                "max_lines": NINE_GRID_TOP_FONT["max_lines"],
            },
            "bottom2": {
                "family": "Noto Sans SC",
                "font_size_px": NINE_GRID_BOTTOM_FONT["maximum"],
                "font_weight": NINE_GRID_BOTTOM_FONT["weight"],
                "max_width_px": NINE_GRID_BOTTOM_FONT["width"],
                "max_lines": NINE_GRID_BOTTOM_FONT["max_lines"],
            },
        }
        self.reference_semantic_layouts["nine-grid"] = semantic
        public_semantic = {
            "version": REFERENCE_SEMANTIC_LAYOUT_VERSION,
            "max_width_px": max(
                NINE_GRID_TOP_FONT["width"],
                NINE_GRID_BOTTOM_FONT["width"],
            ),
            "layers": {
                layer: {
                    key: int(value)
                    for key, value in metrics.items()
                    if key in {
                        "font_size_px", "font_weight",
                        "max_width_px", "max_lines",
                    }
                }
                for layer, metrics in semantic.items()
            },
        }
        record = {
            "id": NINE_GRID_TEMPLATE_ID,
            "name": "九宫格开场·全屏展示",
            "description": "九格依次显现，随后切换三段全屏素材",
            "tags": ["HyperFrames", "九宫格", "固定12秒", "绑定音乐"],
            "engine": "hyperframes",
            "font_mode": "template_locked",
            "font_selectable": False,
            "variant": "nine-grid",
            "duration_mode": "fixed_12",
            "required_visuals": NINE_GRID_VISUAL_COUNT,
            "required_visuals_max": NINE_GRID_VISUAL_COUNT,
            "clip_duration_range_seconds": [
                NINE_GRID_SELECTED_CLIP_SECONDS,
                NINE_GRID_SELECTED_CLIP_SECONDS,
            ],
            "bgm_mode": "bound",
            "bgm_optional": True,
            "semantic_layout": public_semantic,
        }
        self.nine_grid_fonts = fonts
        self.nine_grid_template = record
        return record

    def _load_fixed_skill_template(self, template_id: str) -> dict:
        config = FIXED_SKILL_TEMPLATE_CONFIGS[template_id]
        hyperframes_version = str(
            config.get(
                "hyperframes_version", FIXED_SKILL_HYPERFRAMES_VERSION,
            )
        )
        root = self.fixed_skill_roots[template_id]
        if root.is_symlink() or not root.is_dir():
            raise MatrixTemplateError("fixed Skill template root is unavailable")
        required = {
            "index.html", "template.json", "package.json",
            str(config["bgm_path"]),
            *(f"assets/fonts/{filename}" for filename in config["font_files"].values()),
            *config.get("required_files", ()),
        }
        if template_id == TRIPLE_STRIP_TEMPLATE_ID:
            required.update({
                "hyperframes.json", "index.motion.json",
                "assets/vendor/gsap.min.js",
                "compositions/opening.html",
                *(f"compositions/main-{index:02d}.html" for index in range(1, 6)),
            })
        elif template_id == YELLOW_BANNER_TEMPLATE_ID:
            required.update({
                "hyperframes.json",
                "assets/vendor/gsap.min.js",
                "assets/vendor/yellow-banner-motion.js",
            })
        for relative in required:
            path = root.joinpath(*relative.split("/"))
            if path.is_symlink() or not path.is_file():
                raise MatrixTemplateError("fixed Skill template is incomplete")

        manifest = _read_json(root / "template.json")
        manifest_version = config.get("manifest_version", config["version"])
        binding = manifest.get(
            str(config.get("bgm_manifest_key", "boundBgm"))
        )
        if (
            manifest.get("id") != template_id
            or (
                manifest_version is not None
                and manifest.get("version") != manifest_version
            )
            or manifest.get("renderer") != "hyperframes"
            or manifest.get("width") != 1080
            or manifest.get("height") != 1920
            or manifest.get("fps") != 30
            or abs(float(manifest.get("duration") or 0) - config["duration"]) > 1e-9
            or not isinstance(binding, dict)
            or binding.get("path") != config["bgm_path"]
            or binding.get("sha256") != config["bgm_sha256"]
            or abs(float(binding.get("duration") or 0) - config["bgm_duration"]) > 1e-6
            or _file_sha256(root / str(config["bgm_path"]))
                != config["bgm_sha256"]
        ):
            raise MatrixTemplateError("fixed Skill template contract is invalid")
        if template_id == TRIPLE_STRIP_TEMPLATE_ID:
            if (
                manifest.get("openingSlots") != 3
                or manifest.get("mainSlots") != 5
                or manifest.get("cutFrames") != [0, 117, 199, 281, 363, 445, 528]
            ):
                raise MatrixTemplateError("triple-strip timing contract changed")
            expected_fields = ("title", "subtitle", "ctaLine1", "ctaLine2")
        elif template_id == YELLOW_BANNER_TEMPLATE_ID:
            if (
                manifest.get("hyperframesVersion")
                    != FIXED_SKILL_HYPERFRAMES_VERSION
                or manifest.get("frames") != 302
                or manifest.get("cutFrames") != [0, 86, 183, 302]
                or manifest.get("mediaSlots") != 3
            ):
                raise MatrixTemplateError("yellow-banner timing contract changed")
            expected_fields = (
                "title", "subtitle1", "subtitle2", "sourceLabel", "body", "cta",
            )
        else:
            media = manifest.get("media")
            if (
                manifest.get("frames") != config["frames"]
                or not isinstance(media, dict)
                or media.get("requiredDistinctVideos")
                    != config["required_visuals"]
                or media.get("minimumPreparedDuration")
                    != config["slot_frames"][0] / 30
                or media.get("paths") != list(config["media_paths"])
            ):
                raise MatrixTemplateError(
                    "motion v2 template timing contract changed"
                )
            expected_fields = tuple(config["expected_fields"])
        package = _read_json(root / "package.json")
        scripts = package.get("scripts")
        if (
            not isinstance(scripts, dict)
            or any(
                f"hyperframes@{hyperframes_version}"
                    not in str(scripts.get(name) or "")
                for name in ("dev", "check", "render", "publish")
            )
        ):
            raise MatrixTemplateError("fixed Skill HyperFrames version changed")
        index_html = (root / "index.html").read_text(encoding="utf-8")
        composition_id = str(config.get("composition_id", template_id))
        audio_id = str(config.get("audio_id", "bound-bgm"))
        if (
            index_html.count(f'data-composition-id="{composition_id}"') != 1
            or any(
                index_html.count(f'data-var-text="{field}"') != 1
                for field in expected_fields
            )
            or not re.search(
                rf'<audio\b[^>]*\bid="{re.escape(audio_id)}"'
                r'[^>]*\bdata-volume="1"',
                index_html,
            )
        ):
            raise MatrixTemplateError("fixed Skill template HTML contract changed")
        if (
            template_id == YELLOW_BANNER_TEMPLATE_ID
            and index_html.count("data-color-grading=") != 2
        ):
            raise MatrixTemplateError("yellow-banner blur contract changed")
        fonts = {}
        for family, filename in config["font_files"].items():
            path = root / "assets/fonts" / filename
            fonts[family] = {
                "family": family, "file": filename, "path": path,
                "sha256": _file_sha256(path),
            }
        cli = (
            self.motion_v2_hyperframes_cli
            if hyperframes_version == MOTION_V2_HYPERFRAMES_VERSION
            else self.nine_grid_hyperframes_cli
        )
        if cli is None or cli.is_symlink() or not cli.is_file():
            raise MatrixTemplateError(
                f"HyperFrames {hyperframes_version} CLI is unavailable"
            )
        version = subprocess.run(
            [str(cli), "--version"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if (
            version.returncode
            or version.stdout.strip() != hyperframes_version
        ):
            raise MatrixTemplateError("fixed Skill HyperFrames CLI version mismatch")
        semantic = config["semantic"]
        self.reference_semantic_layouts[str(config["variant"])] = semantic
        public_semantic = {
            "version": REFERENCE_SEMANTIC_LAYOUT_VERSION,
            "max_width_px": max(
                int(item["max_width_px"]) for item in semantic.values()
            ),
            "layers": {
                layer: {
                    key: int(value)
                    for key, value in metrics.items()
                    if key in {
                        "font_size_px", "font_weight",
                        "max_width_px", "max_lines",
                    }
                }
                for layer, metrics in semantic.items()
            },
        }
        record = {
            "id": template_id,
            "name": config["name"],
            "description": config["description"],
            "tags": ["HyperFrames", "固定节奏", "绑定音乐"],
            "engine": "hyperframes",
            "font_mode": "template_locked",
            "font_selectable": False,
            "variant": config["variant"],
            "duration_mode": "fixed",
            "fixed_duration_seconds": config["duration"],
            "required_visuals": config["required_visuals"],
            "required_visuals_max": config["required_visuals"],
            "clip_duration_range_seconds": [
                min(config["slot_frames"]) / 30,
                max(config["slot_frames"]) / 30,
            ],
            "bgm_mode": "bound",
            "bgm_optional": True,
            "semantic_layout": public_semantic,
        }
        source_hashes = {
            relative: _file_sha256(root.joinpath(*relative.split("/")))
            for relative in sorted(required)
            if relative != config["bgm_path"]
            and not relative.startswith("assets/fonts/")
        }
        self.fixed_skill_source_sha256[template_id] = hashlib.sha256(
            json.dumps(
                source_hashes, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.fixed_skill_fonts[template_id] = fonts
        self.fixed_skill_templates[template_id] = record
        return record

    def _reference_measure_font(self, family: str, size: int, weight: int):
        key = (str(family), int(size), int(weight))
        cached = self.reference_measure_fonts.get(key)
        if cached is not None:
            return cached
        record = self.private_fonts.get(family) or self.reference_fonts.get(family)
        if record is None:
            record = next((
                values[family]
                for values in self.fixed_skill_fonts.values()
                if family in values
            ), None)
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

    def _nine_grid_measure_font(self, role: str, size: int):
        spec = (
            NINE_GRID_TOP_FONT if role == "top_text"
            else NINE_GRID_BOTTOM_FONT
        )
        key = (role, int(size), int(spec["weight"]))
        cached = self.nine_grid_measure_fonts.get(key)
        if cached is not None:
            return cached
        record = self.nine_grid_fonts.get(role)
        if record is None or not Path(record["path"]).is_file():
            raise MatrixTemplateError("nine-grid semantic font is unavailable")
        try:
            font = ImageFont.truetype(str(record["path"]), int(size))
            axes = font.get_variation_axes()
            weight_axis = next(
                index for index, axis in enumerate(axes)
                if str(
                    axis.get("name", b"").decode("ascii", "ignore")
                    if isinstance(axis.get("name", b""), bytes)
                    else axis.get("name", "")
                ).strip().lower() == "weight"
            )
            values = [int(axis["default"]) for axis in axes]
            values[weight_axis] = int(spec["weight"])
            font.set_variation_by_axes(values)
        except Exception as exc:
            raise MatrixTemplateError(
                "nine-grid semantic font cannot be measured"
            ) from exc
        self.nine_grid_measure_fonts[key] = font
        return font

    def _nine_grid_text_width(
        self, value: str, role: str, size: int,
    ) -> float:
        display = _hide_reference_edge_punctuation(value)
        if not display:
            return 0.0
        font = self._nine_grid_measure_font(role, size)
        box = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox(
            (0, 0), display, font=font, stroke_width=2,
        )
        return float(box[2] - box[0])

    def _nine_grid_pack_lines(
        self, text: str, break_after: list[int], role: str,
    ) -> tuple[list[str], list[str], int]:
        spec = (
            NINE_GRID_TOP_FONT if role == "top_text"
            else NINE_GRID_BOTTOM_FONT
        )
        boundaries = [0] + sorted({
            item + 1 for item in break_after if item < len(text) - 1
        }) + [len(text)]
        for size in range(int(spec["maximum"]), int(spec["minimum"]) - 1, -1):
            candidates = []
            maximum_lines = min(
                int(spec["max_lines"]), len(boundaries) - 1,
                math.floor(float(spec["height"]) / (size * float(spec["line_height"]))),
            )
            for line_count in range(1, maximum_lines + 1):
                widths = {}

                def measured(left_index: int, right_index: int):
                    key = (left_index, right_index)
                    if key not in widths:
                        source = text[
                            boundaries[left_index]:boundaries[right_index]
                        ]
                        display = _hide_reference_edge_punctuation(source)
                        widths[key] = (
                            source,
                            display,
                            self._nine_grid_text_width(
                                source, role, size,
                            ) if display else 0.0,
                        )
                    return widths[key]

                total_width = self._nine_grid_text_width(text, role, size)
                ideal = min(float(spec["width"]), total_width / line_count)
                states = {0: (0.0, [])}
                for _line_index in range(line_count):
                    next_states = {}
                    for left_index, (score, path) in states.items():
                        for right_index in range(
                            left_index + 1, len(boundaries)
                        ):
                            source, display, width = measured(
                                left_index, right_index,
                            )
                            if not display:
                                continue
                            if width > float(spec["width"]) + 0.001:
                                break
                            remaining_lines = line_count - len(path) - 1
                            remaining_boundaries = (
                                len(boundaries) - right_index - 1
                            )
                            if remaining_boundaries < remaining_lines:
                                continue
                            candidate = score + (width - ideal) ** 2
                            current = next_states.get(right_index)
                            if current is None or candidate < current[0]:
                                next_states[right_index] = (
                                    candidate, path + [(source, display)],
                                )
                    states = next_states
                selected = states.get(len(boundaries) - 1)
                if selected is not None:
                    candidates.append((line_count, selected[0], selected[1]))
            if candidates:
                _line_count, _score, lines = min(
                    candidates, key=lambda item: item[:2],
                )
                return (
                    [item[0] for item in lines],
                    [item[1] for item in lines],
                    size,
                )
        raise ValueError("九宫格文案无法在完整语义边界内排入模板")

    def _nine_grid_text_layout(
        self, top: str, bottom: str, semantic_layout: dict,
    ) -> dict:
        layout = _normalize_reference_semantic_layout(
            semantic_layout, top, bottom,
        )
        top_source, top_display, top_size = self._nine_grid_pack_lines(
            top, layout["top_break_after"], "top_text",
        )
        bottom_source, bottom_display, bottom_size = self._nine_grid_pack_lines(
            bottom, layout["bottom_break_after"], "bottom_text",
        )
        return {
            "source": {"top_text": top, "bottom_text": bottom},
            "source_lines": {
                "top_text": top_source, "bottom_text": bottom_source,
            },
            "display": {
                "top_text": "\n".join(top_display),
                "bottom_text": "\n".join(bottom_display),
            },
            "font_size_px": {
                "top_text": top_size, "bottom_text": bottom_size,
            },
            "semantic_layout": layout,
        }

    def _fixed_skill_field_font_size(
        self, template_id: str, field: str, value: str,
    ) -> int:
        spec = FIXED_SKILL_TEMPLATE_CONFIGS[template_id]["field_specs"][field]
        lines = [line for line in str(value or "").splitlines() if line]
        if not lines:
            return int(spec["maximum"])
        if len(lines) > int(spec["max_lines"]):
            raise ValueError("新模板文案行数超过文字区域")
        for size in range(int(spec["maximum"]), int(spec["minimum"]) - 1, -1):
            if (
                len(lines) * size * float(spec["line_height"])
                > float(spec["height"]) + 0.001
            ):
                continue
            metrics = {
                "family": spec["family"],
                "font_size_px": size,
                "font_weight": spec["weight"],
                "max_width_px": spec["width"],
                "stroke_px": spec.get("stroke_px", 0),
                "letter_spacing_em": spec.get("letter_spacing_em", 0),
            }
            if all(
                self._reference_text_width(line, metrics)
                <= float(spec["width"]) + 0.001
                for line in lines
            ):
                return size
        raise ValueError("新模板文案无法在完整语义边界内排入模板")

    def _fixed_skill_text_layout(
        self, template_id: str, top: str, bottom: str,
        semantic_layout: dict,
    ) -> dict:
        config = FIXED_SKILL_TEMPLATE_CONFIGS[template_id]
        source_text, display_text = self._reference_semantic_text_layout(
            top, bottom, str(config["variant"]), semantic_layout,
        )
        bottom_lines = [
            line for line in display_text["bottom2"].splitlines() if line
        ]
        if not bottom_lines:
            raise ValueError("底部行动文案无法在完整语义边界内排入模板")
        if template_id == TRIPLE_STRIP_TEMPLATE_ID:
            split = min(2, max(1, math.ceil(len(bottom_lines) / 2)))
            fields = {
                "title": display_text["top1"],
                "subtitle": display_text["top2"],
                "ctaLine1": "\n".join(bottom_lines[:split]),
                "ctaLine2": "\n".join(bottom_lines[split:]),
            }
        elif template_id == YELLOW_BANNER_TEMPLATE_ID:
            fields = {
                "title": display_text["top1"],
                "subtitle1": display_text["top2"],
                "subtitle2": display_text["top3"],
                "sourceLabel": "",
                "body": "\n".join(bottom_lines[:-1]),
                "cta": bottom_lines[-1],
            }
        else:
            fields = {
                "title": display_text["top1"],
                "subtitle": display_text["top2"],
                "body": display_text["top3"],
                "cta": "\n".join(bottom_lines),
            }
        if not fields["title"]:
            raise ValueError("顶部标题无法在完整语义边界内排入模板")
        sizes = {
            field: self._fixed_skill_field_font_size(
                template_id, field, value,
            )
            for field, value in fields.items()
            if field in config["field_specs"]
        }
        return {
            "source": {"top_text": top, "bottom_text": bottom},
            "source_layers": source_text,
            "display": fields,
            "font_size_px": sizes,
            "semantic_layout": _normalize_reference_semantic_layout(
                semantic_layout, top, bottom,
            ),
        }

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
        bottom1_start = len(top)
        bottom1_lines = []
        if variant == REFERENCE_V07_VARIANT:
            bottom1_metrics = contract["bottom1"]
            break_points = sorted(set(
                [
                    item + 1 for item in layout["top_break_after"]
                    if top1_end <= item < len(top)
                ] + [len(top)]
            ))
            candidates = []
            for split2 in break_points:
                for split3 in [
                    item for item in break_points if split2 < item
                ] + [len(top)]:
                    try:
                        top2_candidate = self._pack_reference_semantic_span(
                            top, top1_end, split2,
                            layout["top_break_after"], contract["top2"],
                        )
                        top3_candidate = self._pack_reference_semantic_span(
                            top, split2, split3,
                            layout["top_break_after"], contract["top3"],
                        )
                        bottom1_candidate = self._pack_reference_semantic_span(
                            top, split3, len(top),
                            layout["top_break_after"], bottom1_metrics,
                        )
                    except ValueError:
                        continue
                    total_lines = (
                        len(top2_candidate) + len(top3_candidate)
                        + len(bottom1_candidate)
                    )
                    top3_empty = not top3_candidate
                    bottom1_empty = not bottom1_candidate
                    widths = [
                        self._reference_text_width(line, contract["top2"])
                        / float(contract["top2"].get(
                            "max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX,
                        ))
                        for line in top2_candidate
                    ] + [
                        self._reference_text_width(line, contract["top3"])
                        / float(contract["top3"].get(
                            "max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX,
                        ))
                        for line in top3_candidate
                    ] + [
                        self._reference_text_width(line, bottom1_metrics)
                        / float(bottom1_metrics.get(
                            "max_width_px", REFERENCE_TEXT_MAX_WIDTH_PX,
                        ))
                        for line in bottom1_candidate
                    ]
                    ideal = sum(widths) / max(1, len(widths))
                    raggedness = sum((width - ideal) ** 2 for width in widths)
                    semantic_penalty = 0
                    if split2 < len(top) and top[split2 - 1] not in "，。！？；,.!?;":
                        semantic_penalty += 1
                    if split3 < len(top) and top[split3 - 1] not in "，。！？；,.!?;":
                        semantic_penalty += 1
                    candidates.append((
                        total_lines, top3_empty, bottom1_empty,
                        semantic_penalty, raggedness,
                        split2, split3, top2_candidate, top3_candidate,
                        bottom1_candidate,
                    ))
            if not candidates:
                raise ValueError(
                    "HyperFrames 文案无法在完整语义边界内排入模板"
                )
            (
                _total_lines, _top3_empty, _bottom1_empty,
                _semantic_penalty, _raggedness,
                top3_start, bottom1_start, top2_lines, top3_lines,
                bottom1_lines,
            ) = min(candidates, key=lambda item: item[:5])
        elif top3_metrics is None or top1_end >= len(top):
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
            "top3": top[top3_start:bottom1_start],
            "bottom1": top[bottom1_start:],
            "bottom2": bottom,
        }
        display_text = {
            "top1": "\n".join(map(_hide_reference_edge_punctuation, top1_lines)),
            "top2": "\n".join(map(_hide_reference_edge_punctuation, top2_lines)),
            "top3": "\n".join(map(_hide_reference_edge_punctuation, top3_lines)),
            "bottom1": "\n".join(map(_hide_reference_edge_punctuation, bottom1_lines)),
            "bottom2": "\n".join(map(_hide_reference_edge_punctuation, bottom2_lines)),
        }
        return source_text, display_text

    def validate_payload(self, raw: dict, *, require_available_font: bool = True,
                         allowed_template_ids=None,
                         default_template_id: str | None = None,
                         enforce_reference_layout: bool = True,
                         require_reference_semantic_layout: bool = False) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("request body must be an object")
        top = " ".join(str(raw.get("top_text") or "").split())
        bottom = " ".join(str(raw.get("bottom_text") or "").split())
        if not 2 <= len(top) <= 60:
            raise ValueError("顶部标题需要 2-60 个字符")
        if not 2 <= len(bottom) <= 80:
            raise ValueError("底部行动文案需要 2-80 个字符")
        template_id = str(
            raw.get("template_id")
            or default_template_id
            or self.default_template_id
        )
        allowed_templates = (
            set(self.templates) if allowed_template_ids is None
            else set(allowed_template_ids)
        )
        if template_id not in allowed_templates:
            raise ValueError("请选择有效模板")
        reference_template = template_id in self.reference_templates
        nine_grid_template = (
            template_id == NINE_GRID_TEMPLATE_ID
            and self.nine_grid_template is not None
        )
        fixed_skill_template = template_id in self.fixed_skill_templates
        hyperframes_template = (
            reference_template or nine_grid_template or fixed_skill_template
        )
        semantic_layout = raw.get("semantic_layout")
        normalized_semantic_layout = None
        if hyperframes_template:
            variant = self.templates[template_id]["variant"]
            if semantic_layout is not None:
                if variant not in self.reference_semantic_layouts:
                    raise ValueError("HyperFrames 当前模板不支持语义排版")
                normalized_semantic_layout = _normalize_reference_semantic_layout(
                    semantic_layout, top, bottom,
                )
                if enforce_reference_layout:
                    if nine_grid_template:
                        self._nine_grid_text_layout(
                            top, bottom, normalized_semantic_layout,
                        )
                    elif fixed_skill_template:
                        self._fixed_skill_text_layout(
                            template_id, top, bottom,
                            normalized_semantic_layout,
                        )
                    else:
                        self._reference_semantic_text_layout(
                            top, bottom, variant, normalized_semantic_layout,
                        )
            elif enforce_reference_layout and require_reference_semantic_layout:
                raise ValueError("HyperFrames 模板必须提供 AI 语义排版")
            elif enforce_reference_layout and reference_template:
                _reference_text_layout(
                    top,
                    bottom,
                    self.reference_templates[template_id]["text_layers"]["top"],
                )
            elif enforce_reference_layout:
                raise ValueError("HyperFrames 模板必须提供 AI 语义排版")
        elif semantic_layout is not None:
            raise ValueError("semantic_layout 仅支持指定 HyperFrames 模板")
        font_family = str(raw.get("font_family") or "").strip()
        if (
            not hyperframes_template and font_family
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}", font_family)
        ):
            raise ValueError("字体参数格式无效")
        if (
            not hyperframes_template and require_available_font and font_family
            and font_family not in self.available_font_families()
        ):
            raise ValueError("请选择当前可用字体")
        if nine_grid_template:
            duration = NINE_GRID_DURATION_SECONDS
        elif fixed_skill_template:
            duration = float(
                FIXED_SKILL_TEMPLATE_CONFIGS[template_id]["duration"]
            )
        else:
            duration = _duration(
                top, bottom,
                None if reference_template else raw.get("duration"),
            )
        bgm = raw.get("bgm", True)
        if not isinstance(bgm, bool):
            raise ValueError("bgm must be boolean")
        material_policy = raw.get("material_policy", "shared")
        if (
            not isinstance(material_policy, str)
            or material_policy not in MATERIAL_POLICIES
        ):
            raise ValueError("material_policy must be shared or owned_public")
        result = {
            "top_text": top, "bottom_text": bottom,
            "template_id": template_id, "duration": duration,
            "bgm": bgm, "material_policy": material_policy,
        }
        if font_family and not hyperframes_template:
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
        # 用户自带素材：透传给后续挑选/渲染环节（数量与格式在 _user_materials 里终审）
        user_materials = raw.get("user_materials")
        if user_materials:
            result["user_materials"] = user_materials
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
        if payload.get("template_id") == NINE_GRID_TEMPLATE_ID:
            return NINE_GRID_VISUAL_COUNT
        if payload.get("template_id") in FIXED_SKILL_TEMPLATE_CONFIGS:
            return int(FIXED_SKILL_TEMPLATE_CONFIGS[
                payload["template_id"]
            ]["required_visuals"])
        duration = payload["duration"]
        reference = payload.get("_reference_template")
        if isinstance(reference, dict):
            duration = reference.get("duration", duration)
        calculated = int(math.ceil(float(duration) / 3.0))
        template = self.reference_templates.get(str(payload.get("template_id") or ""))
        minimum = int((template or {}).get("required_visuals") or 3)
        maximum = int((template or {}).get("required_visuals_max") or 5)
        return max(minimum, min(maximum, calculated))

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
            if (
                "material_policy" not in stored_payload
                and payload["material_policy"] == "shared"
            ):
                payload.pop("material_policy")
        else:
            payload = self.validate_payload(
                raw, require_available_font=False,
                require_reference_semantic_layout=True,
            )
            self._validate_material_policy(payload)
            if (
                self.enforce_library_readiness
                and payload["material_policy"] == "shared"
                and not payload.get("user_materials")
            ):
                self.require_library_ready()
        job, created = self.store.create(
            request_id, payload, admission_guard=self._ensure_disk_capacity,
            freeze_payload=self._freeze_font_provenance,
        )
        if created:
            self._enqueue(job["job_id"])
        return job

    def _validate_material_policy(self, payload: dict) -> None:
        materials = (
            self._user_materials(payload)
            if payload.get("user_materials") is not None else None
        )
        if payload.get("material_policy", "shared") != "owned_public":
            return
        if (
            payload["bgm"]
            and payload.get("template_id") != NINE_GRID_TEMPLATE_ID
            and payload.get("template_id") not in FIXED_SKILL_TEMPLATE_CONFIGS
        ):
            raise MatrixTemplateError(
                "owned_public 不允许使用共享背景音乐，请关闭 bgm"
            )
        if (
            len(materials or []) < self.required_visuals(payload)
            and not self.pexels_api_key
        ):
            raise MatrixTemplateError("owned_public 补充画面需要可用的 Pexels 素材库")

    def _freeze_font_provenance(self, job_id: str, payload: dict) -> dict:
        payload["_material_selection_contract_version"] = (
            MATERIAL_SELECTION_CONTRACT_VERSION
        )
        template_id = payload["template_id"]
        if template_id in self.fixed_skill_templates:
            payload.pop("font_family", None)
            semantic_layout = payload.get("semantic_layout")
            if not isinstance(semantic_layout, dict):
                raise MatrixTemplateError(
                    "固定 Skill 模板必须提供 AI 语义排版"
                )
            config = FIXED_SKILL_TEMPLATE_CONFIGS[template_id]
            text = self._fixed_skill_text_layout(
                template_id, payload["top_text"], payload["bottom_text"],
                semantic_layout,
            )
            fonts = [
                {
                    "family": family,
                    "file": item["file"],
                    "sha256": item["sha256"],
                    "source": "fixed-skill-template",
                }
                for family, item in sorted(
                    self.fixed_skill_fonts[template_id].items()
                )
            ]
            font_map = {
                item["family"]: item
                for item in self.fixed_skill_fonts[template_id].values()
            }
            payload["_fixed_skill_template"] = {
                "template_id": template_id,
                "version": config["version"],
                "engine": "hyperframes",
                "hyperframes_version": str(config.get(
                    "hyperframes_version", FIXED_SKILL_HYPERFRAMES_VERSION,
                )),
                "duration": config["duration"],
                "frames": config["frames"],
                "required_visuals": config["required_visuals"],
                "slot_frames": list(config["slot_frames"]),
                "text": text,
                "font_sha256": {
                    family: item["sha256"]
                    for family, item in self.fixed_skill_fonts[
                        template_id
                    ].items()
                },
                "source_sha256": self.fixed_skill_source_sha256[template_id],
                "bgm_sha256": config["bgm_sha256"],
                "bgm_enabled": bool(payload["bgm"]),
            }
            payload["_font_provenance"] = {
                "selection": {
                    "variant": "template-locked",
                    "top_font": "template-defined",
                    "bottom_font": "template-defined",
                },
                "fonts": fonts,
                "private_bundle_sha256": _font_bundle_fingerprint(font_map),
                "template_font_bundle_sha256": _font_bundle_fingerprint(
                    font_map
                ),
            }
            payload["_display_top_text"] = "\n".join(
                value for key, value in text["display"].items()
                if key in {"title", "subtitle", "subtitle1", "subtitle2"}
                and value
            )
            return payload
        if (
            template_id == NINE_GRID_TEMPLATE_ID
            and self.nine_grid_template is not None
        ):
            payload.pop("font_family", None)
            semantic_layout = payload.get("semantic_layout")
            if not isinstance(semantic_layout, dict):
                raise MatrixTemplateError("九宫格模板必须提供 AI 语义排版")
            text = self._nine_grid_text_layout(
                payload["top_text"], payload["bottom_text"], semantic_layout,
            )
            fonts = [
                {
                    "family": item["family"],
                    "file": item["file"],
                    "sha256": item["sha256"],
                    "source": "nine-grid-template",
                }
                for item in self.nine_grid_fonts.values()
            ]
            font_map = {item["family"]: item for item in fonts}
            payload["_nine_grid_template"] = {
                "template_id": NINE_GRID_TEMPLATE_ID,
                "version": NINE_GRID_TEMPLATE_VERSION,
                "engine": "hyperframes",
                "hyperframes_version": NINE_GRID_HYPERFRAMES_VERSION,
                "duration": NINE_GRID_DURATION_SECONDS,
                "required_visuals": NINE_GRID_VISUAL_COUNT,
                "text": text,
                "font_sha256": {
                    role: item["sha256"]
                    for role, item in self.nine_grid_fonts.items()
                },
                "bgm_sha256": NINE_GRID_BOUND_BGM_SHA256,
                "bgm_enabled": bool(payload["bgm"]),
                "main_slot_indexes": list(NINE_GRID_MAIN_SLOT_INDEXES),
            }
            payload["_font_provenance"] = {
                "selection": {
                    "variant": "template-locked",
                    "top_font": "Noto Serif SC",
                    "bottom_font": "Noto Sans SC",
                },
                "fonts": fonts,
                "private_bundle_sha256": _font_bundle_fingerprint(font_map),
                "template_font_bundle_sha256": _font_bundle_fingerprint(
                    font_map
                ),
            }
            payload["_display_top_text"] = text["display"]["top_text"]
            return payload
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
                # Persisted jobs accepted before semantic layout became mandatory
                # remain renderable during rollout and crash recovery.
                source_text, display_text = _reference_text_layout(
                    payload["top_text"], payload["bottom_text"], top_layer_count
                )
            reference_duration = _reference_duration(job_id, template_id)
            payload["_reference_template"] = {
                "pack_id": REFERENCE_PACK_ID,
                "engine": "hyperframes",
                "hyperframes_version": REFERENCE_HYPERFRAMES_VERSION,
                "variant": template["variant"],
                "top_layer_count": top_layer_count,
                "duration": reference_duration,
                "text": source_text,
                "display_text": display_text,
                "fixed_fonts": fixed_fonts,
                "editing_plan": _reference_editing_plan(
                    job_id, template_id, _required_visuals(reference_duration)
                ),
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
        library = (
            self.library_readiness()
            if self.enforce_library_readiness else {
                "ready": True,
                "selection_contract_version": MATERIAL_SELECTION_CONTRACT_VERSION,
                "clip_contract_version": MATERIAL_CLIP_CONTRACT_VERSION,
            }
        )
        workers_ready = not self.workers_expected or (
            worker_alive and cleanup_alive and not worker_degraded
        )
        pexels_ready = bool(self.pexels_api_key)
        ready = workers_ready and library["ready"]
        return {
            "ok": ready,
            "worker_alive": worker_alive,
            "worker_count": live_workers,
            "cleanup_worker_alive": cleanup_alive,
            "worker_degraded": worker_degraded,
            "degraded_jobs": degraded_job_count,
            "material_library_ready": library["ready"],
            "pexels_material_ready": pexels_ready,
            "pexels_material_optional": True,
            "material_source_policy": "huangque-bookends-extra-middle-pexels-v2",
            "material_selection_contract_version": library[
                "selection_contract_version"
            ],
            "material_clip_contract_version": library[
                "clip_contract_version"
            ],
            "concurrency": self.concurrency,
            "private_fonts": len(self.private_fonts),
            "private_font_bundle_sha256": self.private_font_fingerprint,
            "hyperframes_templates": len(self.reference_templates),
            "hyperframes_version": (
                REFERENCE_HYPERFRAMES_VERSION if self.reference_templates else ""
            ),
            "nine_grid_templates": 1 if self.nine_grid_template else 0,
            "nine_grid_hyperframes_version": (
                NINE_GRID_HYPERFRAMES_VERSION
                if self.nine_grid_template else ""
            ),
            "fixed_skill_templates": sorted(self.fixed_skill_templates),
            "fixed_skill_template_count": len(self.fixed_skill_templates),
            "fixed_skill_hyperframes_version": (
                (
                    "mixed"
                    if any(
                        template_id in MOTION_V2_TEMPLATE_IDS
                        for template_id in self.fixed_skill_templates
                    ) and any(
                        template_id not in MOTION_V2_TEMPLATE_IDS
                        for template_id in self.fixed_skill_templates
                    )
                    else (
                        MOTION_V2_HYPERFRAMES_VERSION
                        if any(
                            template_id in MOTION_V2_TEMPLATE_IDS
                            for template_id in self.fixed_skill_templates
                        )
                        else FIXED_SKILL_HYPERFRAMES_VERSION
                    )
                )
                if self.fixed_skill_templates else ""
            ),
            "fixed_skill_hyperframes_versions": {
                version: sum(
                    1 for template_id in self.fixed_skill_templates
                    if str(FIXED_SKILL_TEMPLATE_CONFIGS[template_id].get(
                        "hyperframes_version",
                        FIXED_SKILL_HYPERFRAMES_VERSION,
                    )) == version
                )
                for version in (
                    FIXED_SKILL_HYPERFRAMES_VERSION,
                    MOTION_V2_HYPERFRAMES_VERSION,
                )
            },
            "reference_top_layer_counts": {
                str(layer_count): sum(
                    1 for item in self.reference_templates.values()
                    if item["text_layers"]["top"] == layer_count
                )
                for layer_count in (2, 3, 4)
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
            "public_template_palette_version": PUBLIC_TEMPLATE_PALETTE_VERSION,
            "public_template_palette_count": PUBLIC_TEMPLATE_PALETTE_COUNT,
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

    def cleanup_user_assets(self, *, now: int | None = None) -> int:
        """清理过期的用户上传素材，保留期与任务目录一致（默认 3 天）。

        先把最近保留期内被任务引用过的素材续期，再删真正过期的。
        """
        root = self.data_root / USER_ASSET_DIRNAME
        if not root.is_dir():
            return 0
        current = _now() if now is None else int(now)
        cutoff = current - self.retention_seconds
        referenced: set[str] = set()
        try:
            with self.store.connect() as db:
                for row in db.execute(
                    "SELECT payload FROM jobs WHERE updated_at > ? LIMIT 2000",
                    (cutoff,),
                ):
                    try:
                        data = json.loads(row["payload"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    for item in data.get("user_materials") or []:
                        if isinstance(item, dict):
                            sha = str(item.get("sha256") or "").lower()
                            if SHA_RE.fullmatch(sha):
                                referenced.add(sha)
        except sqlite3.Error:
            referenced = set()
        for sha in referenced:
            for path in root.glob(sha + ".*"):
                try:
                    os.utime(path, (current, current))
                except OSError:
                    pass
        removed = 0
        for path in root.iterdir():
            if not path.is_file() or path.name.endswith(".part"):
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def cleanup_once(self, *, now: int | None = None) -> int:
        cleaned = 0
        current = _now() if now is None else int(now)
        try:
            self.cleanup_user_assets(now=current)
        except Exception as exc:  # 清理用户素材失败不应影响任务目录清理
            print("[matrix-template] user-asset cleanup failed: %s" % exc, flush=True)
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

    def _library_request(
        self, method: str, path: str, body=None, *, timeout: float = 30,
    ):
        data = _json_bytes(body) if body is not None else None
        request = urllib.request.Request(
            self.library_url + path, data=data, method=method,
            headers={
                "Authorization": "Bearer " + self.library_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            raise MatrixTemplateError(str(detail or "平台素材库暂不可用")) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MatrixTemplateError("平台素材库暂不可用") from exc

    def library_readiness(self, *, force: bool = False) -> dict:
        with self.library_readiness_lock:
            now = time.monotonic()
            if (
                not force
                and self._library_readiness_cache is not None
                and self._library_readiness_cache[0] > now
            ):
                return dict(self._library_readiness_cache[1])
            try:
                payload = self._library_request(
                    "GET", "/v1/ping", timeout=3,
                )
                selection_version = payload.get("selection_contract_version")
                clip_version = payload.get("clip_contract_version")
                ready = (
                    payload.get("ok") is True
                    and isinstance(payload.get("records"), int)
                    and not isinstance(payload.get("records"), bool)
                    and payload["records"] > 0
                    and type(selection_version) is int
                    and selection_version == MATERIAL_SELECTION_CONTRACT_VERSION
                    and type(clip_version) is int
                    and clip_version == MATERIAL_CLIP_CONTRACT_VERSION
                )
                result = {
                    "ready": ready,
                    "selection_contract_version": (
                        selection_version if type(selection_version) is int else 0
                    ),
                    "clip_contract_version": (
                        clip_version if type(clip_version) is int else 0
                    ),
                }
            except (MatrixTemplateError, AttributeError, TypeError, ValueError):
                result = {
                    "ready": False,
                    "selection_contract_version": 0,
                    "clip_contract_version": 0,
                }
            self._library_readiness_cache = (
                now + MATERIAL_LIBRARY_READINESS_TTL_SECONDS, result,
            )
            return dict(result)

    def require_library_ready(self, *, force: bool = False) -> dict:
        result = self.library_readiness(force=force)
        if not result["ready"]:
            raise MatrixTemplateError("素材库切片能力暂不可用")
        return result

    def _material_contract_version(self, payload: dict) -> int:
        value = payload.get("_material_selection_contract_version", 1)
        if isinstance(value, bool) or value not in {
            1, MATERIAL_SELECTION_CONTRACT_VERSION,
        }:
            raise MatrixTemplateError("素材选择契约版本无效")
        return int(value)

    @staticmethod
    def _pexels_search_query(job_id: str) -> str:
        digest = hashlib.sha256(("pexels-china-query:" + job_id).encode("utf-8")).digest()
        return PEXELS_CHINA_QUERIES[int.from_bytes(digest[:2], "big") % len(PEXELS_CHINA_QUERIES)]

    def _pexels_search(self, query: str) -> dict:
        if not self.pexels_api_key:
            raise MatrixTemplateError("Pexels 素材库密钥未配置")
        parameters = {
            "query": query,
            "orientation": "portrait",
            "size": "medium",
            "locale": "zh-CN",
            "page": 1,
            "per_page": PEXELS_SEARCH_PER_PAGE,
        }
        canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
        cache_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cache_path = self.pexels_cache_root / (cache_key + ".json")
        with self.pexels_cache_lock:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    isinstance(cached, dict)
                    and time.time() - float(cached.get("fetched_at", 0))
                    < PEXELS_SEARCH_CACHE_SECONDS
                    and isinstance(cached.get("response"), dict)
                ):
                    return cached["response"]
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass

            request = urllib.request.Request(
                PEXELS_API_URL + "?" + urlencode(parameters),
                headers={
                    "Authorization": self.pexels_api_key,
                    "Accept": "application/json",
                    "User-Agent": "HuangqueMatrixTemplate/1.0",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read(PEXELS_SEARCH_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    detail = "Pexels 素材库密钥无效"
                elif exc.code == 429:
                    detail = "Pexels 素材库调用额度已用完"
                else:
                    detail = "Pexels 素材库暂不可用"
                raise MatrixTemplateError(detail) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise MatrixTemplateError("Pexels 素材库暂不可用") from exc
            if len(raw) > PEXELS_SEARCH_RESPONSE_BYTES:
                raise MatrixTemplateError("Pexels 素材库响应过大")
            try:
                result = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MatrixTemplateError("Pexels 素材库响应无效") from exc
            if not isinstance(result, dict) or not isinstance(result.get("videos"), list):
                raise MatrixTemplateError("Pexels 素材库响应无效")
            self.pexels_cache_root.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(cache_path.name + "." + uuid.uuid4().hex + ".part")
            try:
                temporary.write_text(json.dumps({
                    "fetched_at": time.time(), "response": result,
                }, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
            return result

    @staticmethod
    def _pexels_file(video: dict) -> dict | None:
        candidates = []
        for item in video.get("video_files") or []:
            if not isinstance(item, dict) or item.get("file_type") != "video/mp4":
                continue
            width, height = item.get("width"), item.get("height")
            parsed = urlsplit(str(item.get("link") or ""))
            if (
                isinstance(width, bool) or not isinstance(width, int)
                or isinstance(height, bool) or not isinstance(height, int)
                or width < 360 or height <= width
                or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
            ):
                continue
            candidates.append(item)
        return min(candidates, key=lambda item: (
            0 if item.get("quality") == "hd" else 1,
            abs(int(item["width"]) - 1080),
            -int(item["width"]) * int(item["height"]),
            int(item.get("id") or 0),
        ), default=None)

    def _select_pexels_materials(
        self, scenes: list[dict], job_id: str, used_sha256=(),
    ) -> list[dict]:
        if not scenes:
            return []
        primary = self._pexels_search_query(job_id)
        videos = []
        seen_video_ids = set()
        for video in self._pexels_search(primary).get("videos") or []:
            if not isinstance(video, dict):
                continue
            video_id = video.get("id")
            if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            file = self._pexels_file(video)
            duration = video.get("duration")
            if file is not None and isinstance(duration, (int, float)) and not isinstance(duration, bool):
                videos.append((video, file, float(duration), primary))

        used = {str(value).lower() for value in used_sha256 if SHA_RE.fullmatch(str(value).lower())}
        selected = []
        for position, scene in enumerate(scenes):
            required_duration = float(scene["clip_duration_seconds"])
            eligible = []
            for video, file, source_duration, query in videos:
                source_id = hashlib.sha256(
                    f"pexels:video:{video['id']}:file:{file.get('id')}".encode("utf-8")
                ).hexdigest()
                if source_id in used or source_duration + 0.001 < required_duration + 0.1:
                    continue
                eligible.append((video, file, source_duration, query, source_id))
            if not eligible:
                raise MatrixTemplateError("Pexels 中国场景素材不足")
            ranked = sorted(eligible, key=lambda item: hashlib.sha256(
                f"{job_id}:{scene['scene_id']}:{position}:{item[4]}".encode("utf-8")
            ).hexdigest())
            video, file, source_duration, query, source_id = ranked[0]
            max_start = max(0.0, source_duration - required_duration - 0.1)
            start_digest = hashlib.sha256(
                f"{job_id}:{scene['scene_id']}:{source_id}:start".encode("utf-8")
            ).digest()
            fraction = int.from_bytes(start_digest[:4], "big") / float(0xFFFFFFFF)
            start = round(fraction * max_start, 3)
            clip_id = hashlib.sha256(
                f"{source_id}:clip:{start:.3f}:{required_duration:.3f}".encode("utf-8")
            ).hexdigest()
            contributor = video.get("user") if isinstance(video.get("user"), dict) else {}
            selected.append({
                "scene_id": scene["scene_id"],
                "record_id": f"pexels-video-{video['id']}",
                "sha256": source_id,
                "source_identity": source_id,
                "media_type": "video",
                "match_level": "pexels_china_query",
                "orientation": "portrait",
                "orientation_match": "same",
                "clip_id": clip_id,
                "clip_start_seconds": start,
                "clip_duration_seconds": required_duration,
                "clip_slot_index": 1,
                "clip_slot_count": 1,
                "provider": "pexels",
                "provider_video_id": video["id"],
                "provider_file_id": file.get("id"),
                "provider_url": str(video.get("url") or ""),
                "contributor_name": str(contributor.get("name") or ""),
                "contributor_url": str(contributor.get("url") or ""),
                "source_url": str(file["link"]),
                "search_query": query,
            })
            used.add(source_id)
        return selected

    def _material_scenes(self, payload: dict) -> tuple[list[dict], int, bool]:
        nine_grid_template = payload.get("template_id") == NINE_GRID_TEMPLATE_ID
        fixed_skill_template = (
            payload.get("template_id") in FIXED_SKILL_TEMPLATE_CONFIGS
        )
        count = self.required_visuals(payload)
        reference = payload.get("_reference_template")
        duration = (
            reference.get("duration", payload["duration"])
            if isinstance(reference, dict) else payload["duration"]
        )
        if fixed_skill_template:
            clip_durations = [
                round(frames / 30.0, 6)
                for frames in FIXED_SKILL_TEMPLATE_CONFIGS[
                    payload["template_id"]
                ]["slot_frames"]
            ]
        else:
            segment_duration = (
                NINE_GRID_SELECTED_CLIP_SECONDS
                if nine_grid_template else float(duration) / count
            )
            clip_durations = [segment_duration] * count
        reference_template = (
            payload.get("template_id") in self.reference_templates
            or nine_grid_template or fixed_skill_template
        )
        query = payload["top_text"] + " " + payload["bottom_text"]
        scenes = [{
            "scene_id": f"media_{index:02d}", "query": query,
            "purpose": (
                "模板成片主视频" if index == 1 else "模板成片补充视频"
            ),
            "media_type": (
                "video"
                if index == 1 or reference_template or self.pexels_api_key
                else "visual"
            ),
            "clip_duration_seconds": clip_durations[index - 1],
        } for index in range(1, count + 1)]
        if payload["bgm"] and not (
            nine_grid_template or fixed_skill_template
        ):
            scenes.append({
                "scene_id": "bgm", "query": query,
                "purpose": "模板成片背景音乐", "media_type": "bgm",
            })
        return scenes, count, reference_template

    def _validate_material_selection(
        self, payload: dict, values: list[dict], contract_version: int,
    ) -> list[dict]:
        scenes, count, reference_template = self._material_scenes(payload)
        by_scene = {
            str(item.get("scene_id") or ""): item
            for item in values if isinstance(item, dict)
        }
        expected = [scene["scene_id"] for scene in scenes]
        if set(by_scene) != set(expected) or len(by_scene) != len(expected):
            raise MatrixTemplateError("素材库返回的分镜绑定不完整")
        ordered = [by_scene[scene_id] for scene_id in expected]
        shas = [str(item.get("sha256") or "").lower() for item in ordered]
        if (
            any(not SHA_RE.fullmatch(value) for value in shas)
            or len(set(shas)) != len(shas)
        ):
            raise MatrixTemplateError("素材库返回了无效或重复素材")
        user_supplied = any(
            item.get("provider") == MATERIAL_PROVIDER_USER for item in ordered
        )
        if ordered[0].get("media_type") != "video" and not user_supplied:
            raise MatrixTemplateError("模板成片至少需要一个视频素材")
        if reference_template:
            if any(
                item.get("media_type") != "video"
                and item.get("provider") != MATERIAL_PROVIDER_USER
                for item in ordered[:count]
            ):
                raise MatrixTemplateError("HyperFrames 模板需要不同的视频素材")
        else:
            for item in ordered[1:count]:
                if item.get("media_type") not in {"image", "video"}:
                    raise MatrixTemplateError("素材库返回了无效画面素材")
        if (
            payload["bgm"]
            and payload.get("template_id") != NINE_GRID_TEMPLATE_ID
            and payload.get("template_id") not in FIXED_SKILL_TEMPLATE_CONFIGS
            and ordered[-1].get("media_type") != "bgm"
        ):
            raise MatrixTemplateError("素材库返回了无效背景音乐")
        if contract_version >= MATERIAL_SELECTION_CONTRACT_VERSION:
            clip_ids = []
            for scene, item in zip(scenes[:count], ordered[:count]):
                if (
                    item.get("media_type") != "video"
                    or item.get("provider") == MATERIAL_PROVIDER_USER
                ):
                    continue
                clip_id = str(item.get("clip_id") or "").lower()
                start = _bounded_float(
                    item.get("clip_start_seconds"),
                    0, MAX_MATERIAL_CLIP_START_SECONDS,
                )
                expected_duration = float(scene["clip_duration_seconds"])
                duration = _bounded_float(
                    item.get("clip_duration_seconds"),
                    REFERENCE_MIN_SEGMENT_SECONDS,
                    MAX_MATERIAL_CLIP_DURATION_SECONDS,
                )
                slot_index = item.get("clip_slot_index")
                slot_count = item.get("clip_slot_count")
                if (
                    not SHA_RE.fullmatch(clip_id)
                    or start is None
                    or duration is None
                    or abs(duration - expected_duration) > 0.001
                    or isinstance(slot_index, bool)
                    or not isinstance(slot_index, int)
                    or isinstance(slot_count, bool)
                    or not isinstance(slot_count, int)
                    or not 1 <= slot_index <= slot_count <= MAX_MATERIAL_CLIP_SLOTS
                ):
                    raise MatrixTemplateError("素材库返回的切片契约不完整")
                clip_ids.append(clip_id)
            if len(clip_ids) != len(set(clip_ids)):
                raise MatrixTemplateError("素材库返回了重复切片")
        return ordered

    def _select_materials_once(self, payload: dict, job_id: str,
                               used_sha256=()) -> list[dict]:
        scenes, count, _reference_template = self._material_scenes(payload)
        contract_version = self._material_contract_version(payload)
        if not self.pexels_api_key:
            result = self._library_request("POST", "/v1/select", {
                "scenes": scenes, "orientation": "portrait", "seed": job_id,
                "used_sha256": list(used_sha256),
                "selection_mode": "round_robin",
                "selection_id": "matrix-template:" + job_id,
            })
            if contract_version >= MATERIAL_SELECTION_CONTRACT_VERSION and (
                result.get("selection_contract_version") != MATERIAL_SELECTION_CONTRACT_VERSION
                or result.get("clip_contract_version") != MATERIAL_CLIP_CONTRACT_VERSION
            ):
                raise MatrixTemplateError("素材库切片能力版本不兼容")
            return self._validate_material_selection(
                payload, result.get("materials") or [], contract_version,
            )
        visual_scenes = scenes[:count]
        bgm_scenes = scenes[count:]
        source_plan = _material_source_plan(
            count,
            include_middle_library=(
                payload.get("template_id") in MOTION_V2_TEMPLATE_IDS
            ),
            seed=job_id,
        )
        own_scenes = [
            scene for scene, source in zip(visual_scenes, source_plan)
            if source == "huangque"
        ] + bgm_scenes
        pexels_scenes = [
            scene for scene, source in zip(visual_scenes, source_plan)
            if source == "pexels"
        ]
        pexels = self._select_pexels_materials(
            pexels_scenes, job_id, used_sha256=used_sha256,
        )
        result = self._library_request("POST", "/v1/select", {
            "scenes": own_scenes, "orientation": "portrait", "seed": job_id,
            "used_sha256": list(used_sha256),
            "selection_mode": "round_robin",
            "selection_id": "matrix-template:" + job_id,
        })
        if contract_version >= MATERIAL_SELECTION_CONTRACT_VERSION:
            selection_version = result.get("selection_contract_version")
            clip_version = result.get("clip_contract_version")
            if (
                isinstance(selection_version, bool)
                or not isinstance(selection_version, int)
                or selection_version != MATERIAL_SELECTION_CONTRACT_VERSION
                or isinstance(clip_version, bool)
                or not isinstance(clip_version, int)
                or clip_version != MATERIAL_CLIP_CONTRACT_VERSION
            ):
                raise MatrixTemplateError("素材库切片能力版本不兼容")
        own_values = result.get("materials") or []
        by_scene = {
            str(item.get("scene_id") or ""): item
            for item in own_values + pexels if isinstance(item, dict)
        }
        values = [by_scene.get(scene["scene_id"]) for scene in scenes]
        return self._validate_material_selection(
            payload, values, contract_version,
        )

    def _select_materials(self, payload: dict, job_id: str) -> list[dict]:
        batch_id = str(payload.get("batch_id") or "")
        contract_version = self._material_contract_version(payload)
        with self.batch_material_lock:
            frozen = self.store.material_selection(job_id)
            if frozen is not None:
                if (
                    frozen["selection_contract_version"]
                    != contract_version
                    or frozen["batch_id"] != batch_id
                ):
                    raise MatrixTemplateError("素材选择冻结版本冲突")
                return self._validate_material_selection(
                    payload, frozen["materials"], contract_version,
                )
            user_selected = self._user_materials(payload)
            if user_selected is not None:
                selected = user_selected
                if (
                    payload.get("material_policy", "shared") == "owned_public"
                    and len(selected) < self.required_visuals(payload)
                ):
                    scenes, count, _reference = self._material_scenes(payload)
                    used = (
                        self.store.batch_used_visuals(batch_id)
                        if batch_id else []
                    )
                    selected = selected + self._select_pexels_materials(
                        scenes[len(selected):count], job_id,
                        used_sha256=used + [
                            item["sha256"] for item in selected
                        ],
                    )
                    selected = self._validate_material_selection(
                        payload, selected, contract_version,
                    )
                if batch_id:
                    self.store.reserve_batch_materials(
                        batch_id, job_id, selected, contract_version,
                    )
                else:
                    self.store.reserve_job_materials(
                        job_id, selected, contract_version,
                    )
                return selected
            if payload.get("material_policy", "shared") == "owned_public":
                scenes, count, _reference = self._material_scenes(payload)
                selected = self._select_pexels_materials(
                    scenes[:count], job_id,
                    used_sha256=(
                        self.store.batch_used_visuals(batch_id)
                        if batch_id else []
                    ),
                )
                selected = self._validate_material_selection(
                    payload, selected, contract_version,
                )
                if batch_id:
                    self.store.reserve_batch_materials(
                        batch_id, job_id, selected, contract_version,
                    )
                else:
                    self.store.reserve_job_materials(
                        job_id, selected, contract_version,
                    )
                return selected
            used = (
                self.store.batch_used_visuals(batch_id) if batch_id else []
            )
            selected = self._select_materials_once(
                payload, job_id, used_sha256=used
            )
            if batch_id:
                self.store.reserve_batch_materials(
                    batch_id, job_id, selected, contract_version,
                )
            else:
                self.store.reserve_job_materials(
                    job_id, selected, contract_version,
                )
            return selected

    def _download(self, item: dict, target_dir: Path, job_id: str = "") -> Path:
        if item.get("provider") == "pexels":
            return self._download_pexels(item, target_dir, job_id)
        if item.get("provider") == MATERIAL_PROVIDER_USER:
            return self._download_user_asset(item, target_dir)
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
                item["content_sha256"] = sha
                return target
        except urllib.error.HTTPError as exc:
            raise MatrixTemplateError("素材库文件读取失败") from exc

    def user_asset_path(self, sha: str) -> Path | None:
        """按 sha256 找用户上传素材的落地文件。"""
        value = str(sha or "").lower()
        if not SHA_RE.fullmatch(value):
            return None
        root = self.data_root / USER_ASSET_DIRNAME
        if not root.is_dir():
            return None
        for candidate in sorted(root.glob(value + ".*")):
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        return None

    def _download_user_asset(self, item: dict, target_dir: Path) -> Path:
        """用户素材在接收时已按 sha256 落盘，这里直接复用本地文件并校验。"""
        sha = str(item.get("sha256") or "").lower()
        source = self.user_asset_path(sha)
        if source is None:
            raise MatrixTemplateError("用户素材不存在或已过期，请重新上传")
        target = target_dir / (sha + source.suffix)
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            digest = hashlib.sha256()
            total = 0
            with source.open("rb") as reader, temporary.open("wb") as handle:
                while chunk := reader.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_USER_ASSET_BYTES:
                        raise MatrixTemplateError("用户素材文件过大")
                    digest.update(chunk)
                    handle.write(chunk)
            if not total or not hmac.compare_digest(digest.hexdigest(), sha):
                raise MatrixTemplateError("用户素材文件校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def _inspect_user_asset(source: Path, media_type: str) -> float:
        if media_type == "image":
            if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise MatrixTemplateError("用户图片素材 MIME 不匹配")
            try:
                with Image.open(source) as image:
                    image.verify()
            except Exception as exc:
                raise MatrixTemplateError("用户图片素材无法读取") from exc
            return 0.0
        if source.suffix.lower() not in {".mp4", ".mov"}:
            raise MatrixTemplateError("用户视频素材 MIME 不匹配")
        try:
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json",
                str(source),
            ], check=True, capture_output=True, text=True, timeout=30)
            media = json.loads(probe.stdout or "{}")
            duration = float((media.get("format") or {}).get("duration") or 0)
            if not any(
                stream.get("codec_type") == "video"
                for stream in media.get("streams") or []
            ):
                raise ValueError("missing video stream")
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            raise MatrixTemplateError("用户视频素材无法读取") from exc
        return duration

    def _user_materials(self, payload: dict) -> list[dict] | None:
        """校验 payload 自带用户素材并绑定到最前面的画面位。

        payload["user_materials"] 形如 [{"sha256": "<64 hex>", "media_type": "image"|"video"}, ...]
        shared 数量必须填满画面位；owned_public 允许剩余画面由 Pexels 补齐。
        """
        raw = payload.get("user_materials")
        if not raw:
            return None
        if not isinstance(raw, list):
            raise MatrixTemplateError("用户素材清单格式无效")
        scenes, count, _reference = self._material_scenes(payload)
        owned_public = payload.get("material_policy", "shared") == "owned_public"
        if len(raw) > count or (not owned_public and len(raw) != count):
            raise MatrixTemplateError(
                "用户素材数量与模板画面位不符（需要 %d 个）" % count
            )
        records = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise MatrixTemplateError("用户素材条目格式无效")
            sha = str(item.get("sha256") or "").lower()
            if not SHA_RE.fullmatch(sha):
                raise MatrixTemplateError("用户素材缺少有效校验值")
            source = self.user_asset_path(sha)
            if source is None:
                raise MatrixTemplateError("用户素材不存在或已过期，请重新上传")
            media_type = str(item.get("media_type") or "").strip().lower()
            if media_type not in {"image", "video"}:
                raise MatrixTemplateError("用户素材只支持图片或视频")
            clip_duration = float(scenes[index]["clip_duration_seconds"])
            start = item.get("clip_start_seconds")
            if media_type == "image":
                if start is not None:
                    raise MatrixTemplateError("图片素材不能设置 clip_start_seconds")
                self._inspect_user_asset(source, media_type)
                clip_start = 0.0
            else:
                source_duration = self._inspect_user_asset(source, media_type)
                clip_start = _bounded_float(start if start is not None else 0, 0, source_duration)
                if clip_start is None or clip_start + clip_duration > source_duration + 0.001:
                    raise MatrixTemplateError(
                        "视频素材入点超过可用时长，请调整 clip_start_seconds"
                    )
            record = {
                "sha256": sha,
                "media_type": media_type,
                "provider": MATERIAL_PROVIDER_USER,
                "scene_id": scenes[index]["scene_id"],
                "slot_index": index + 1,
                "slot_count": count,
                "clip_start_seconds": round(float(clip_start), 3),
                "clip_duration_seconds": round(clip_duration, 3),
            }
            records.append(record)
        return records

    def _download_pexels(self, item: dict, target_dir: Path, job_id: str = "") -> Path:
        identity = str(item.get("sha256") or "").lower()
        parsed = urlsplit(str(item.get("source_url") or ""))
        if (
            not SHA_RE.fullmatch(identity)
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
        ):
            raise MatrixTemplateError("Pexels 素材下载地址无效")
        request = urllib.request.Request(
            parsed.geturl(), headers={"User-Agent": "HuangqueMatrixTemplate/1.0"},
        )
        target = target_dir / (identity + ".mp4")
        temporary = target.with_suffix(".mp4.part")
        digest = hashlib.sha256()
        total = 0
        try:
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    if response.headers.get_content_type() != "video/mp4":
                        raise MatrixTemplateError("Pexels 素材文件类型不受支持")
                    with temporary.open("wb") as handle:
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_ASSET_BYTES:
                                raise MatrixTemplateError("Pexels 素材文件过大")
                            digest.update(chunk)
                            handle.write(chunk)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                raise MatrixTemplateError("Pexels 素材文件读取失败") from exc
            if not total:
                raise MatrixTemplateError("Pexels 素材文件为空")
            content_sha256 = digest.hexdigest()
            frozen = str(item.get("content_sha256") or "").lower()
            if frozen and (
                not SHA_RE.fullmatch(frozen)
                or not hmac.compare_digest(frozen, content_sha256)
            ):
                raise MatrixTemplateError("Pexels 素材文件发生变化")
            # 先以 CAS 持久化内容哈希，成功后再原子替换最终文件，
            # 避免「下载完成但数据库未写入」的崩溃窗口。
            if job_id and item.get("scene_id"):
                self.store.update_material_content_sha256(
                    job_id, str(item["scene_id"]), content_sha256,
                )
            item["content_sha256"] = content_sha256
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

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
        segment_hint = max(1.0, float(payload["duration"]) / max(1, count))
        for item, path in zip(materials[:count], paths[:count]):
            entry = {
                "path": path.relative_to(self.data_root / job_id).as_posix(),
                "type": item["media_type"],
                "record_id": item.get("record_id"),
            }
            if item["media_type"] == "video":
                selected_start = item.get("clip_start_seconds")
                try:
                    source_duration = self._reference_video_duration(path)
                except MatrixTemplateError:
                    source_duration = 0.0
                if (
                    not isinstance(selected_start, bool)
                    and isinstance(selected_start, (int, float))
                    and math.isfinite(float(selected_start))
                    and float(selected_start) >= 0
                ):
                    if (
                        float(selected_start) + segment_hint
                        > source_duration - REFERENCE_MEDIA_SAFETY_SECONDS + 0.001
                    ):
                        raise MatrixTemplateError("模板素材切片超出原视频时长")
                    entry["start"] = round(float(selected_start), 3)
                else:
                    max_start = max(
                        0.0,
                        source_duration - segment_hint
                        - REFERENCE_MEDIA_SAFETY_SECONDS,
                    )
                    if max_start > 0.001:
                        digest = hashlib.sha256(
                            f"{job_id}:ffmpeg-media-start:"
                            f"{item.get('sha256') or path.name}".encode("utf-8")
                        ).digest()
                        fraction = (
                            int.from_bytes(digest[:4], "big")
                            / float(0xFFFFFFFF)
                        )
                        entry["start"] = round(fraction * max_start, 3)
            media.append(entry)
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
            # 画面位全是图片时必须显式放行，否则渲染器直接拒单
            # （render_video.py: text-media-text image-only output is disabled by default）。
            "material_policy": (
                {
                    "allow_image_only": True,
                    "image_only_reason": "用户自带图片素材（模板成片·内测免费）",
                }
                if media and all(entry["type"] == "image" for entry in media)
                else {"allow_image_only": False, "image_only_reason": ""}
            ),
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

    def _prepare_nine_grid_clip(
        self, source: Path, destination: Path, start: float,
        *, deadline_at: float,
    ) -> None:
        if (
            not source.is_file()
            or not math.isfinite(float(start))
            or float(start) < 0
        ):
            raise MatrixTemplateError("九宫格素材切片参数无效")
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise MatrixTemplateError("九宫格模板任务超过总时限")
        timeout = min(
            float(NINE_GRID_PREPARE_CLIP_TIMEOUT_SECONDS), remaining,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".part.mp4")
        temporary.unlink(missing_ok=True)
        visible = NINE_GRID_SELECTED_CLIP_SECONDS
        encoded = NINE_GRID_RENDER_CLIP_SECONDS + NINE_GRID_HIDDEN_TAIL_SECONDS
        tail = encoded - visible
        video_filter = (
            f"trim=duration={visible:.6f},setpts=PTS-STARTPTS,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1,fps=30,"
            f"tpad=stop_mode=clone:stop_duration={tail:.6f},"
            "format=yuv420p"
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", _format_reference_seconds(float(start)), "-i", str(source),
            "-map", "0:v:0", "-an", "-vf", video_filter,
            "-t", f"{encoded:.6f}", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-color_range", "tv",
            "-map_metadata", "-1", "-movflags", "+faststart", str(temporary),
        ]
        try:
            returncode, _stdout, _stderr = self._run_tracked_process(
                command,
                timeout_seconds=max(1.0, timeout),
                timeout_error="九宫格素材预处理超时",
            )
            if (
                returncode
                or not temporary.is_file()
                or temporary.stat().st_size < 1024
                or self._reference_video_duration(temporary)
                    + 0.001 < NINE_GRID_RENDER_CLIP_SECONDS
            ):
                raise MatrixTemplateError("九宫格素材预处理失败")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _rewrite_nine_grid_root_style(index_html: str, text: dict) -> str:
        sizes = text.get("font_size_px") if isinstance(text, dict) else None
        if (
            not isinstance(sizes, dict)
            or isinstance(sizes.get("top_text"), bool)
            or not isinstance(sizes.get("top_text"), int)
            or isinstance(sizes.get("bottom_text"), bool)
            or not isinstance(sizes.get("bottom_text"), int)
        ):
            raise MatrixTemplateError("九宫格冻结字号无效")
        pattern = re.compile(r'<div\b(?=[^>]*\bid="root")[^>]*>')
        matches = list(pattern.finditer(index_html))
        if len(matches) != 1 or re.search(r'\sstyle="', matches[0].group(0)):
            raise MatrixTemplateError("九宫格模板根元素发生变化")
        tag = matches[0].group(0)[:-1] + (
            f' style="--top-font-size:{sizes["top_text"]}px;'
            f'--bottom-font-size:{sizes["bottom_text"]}px">'
        )
        return (
            index_html[:matches[0].start()] + tag
            + index_html[matches[0].end():]
        )

    @staticmethod
    def _rewrite_nine_grid_bgm(index_html: str, enabled: bool) -> str:
        if not isinstance(enabled, bool):
            raise MatrixTemplateError("九宫格背景音乐参数无效")
        pattern = re.compile(r'<audio\b(?=[^>]*\bid="bgm")[^>]*>')
        matches = list(pattern.finditer(index_html))
        if len(matches) != 1:
            raise MatrixTemplateError("九宫格背景音乐元素发生变化")
        tag, count = re.subn(
            r'(\sdata-volume=")[^"]*(")',
            rf'\g<1>{1 if enabled else 0}\g<2>',
            matches[0].group(0), count=1,
        )
        if count != 1:
            raise MatrixTemplateError("九宫格背景音乐音量属性发生变化")
        return (
            index_html[:matches[0].start()] + tag
            + index_html[matches[0].end():]
        )

    @staticmethod
    def _rewrite_nine_grid_media_sources(
        index_html: str, variables: dict[str, str],
    ) -> str:
        result = index_html
        for key in (
            *(f"grid{index}" for index in range(1, 10)),
            *(f"main{index}" for index in range(1, 4)),
        ):
            value = str(variables.get(key) or "")
            if not re.fullmatch(r"assets/input/video-[1-9]\.mp4", value):
                raise MatrixTemplateError("九宫格素材变量无效")
            pattern = re.compile(
                rf'<video\b(?=[^>]*\bdata-var-src="{key}")[^>]*>'
            )
            matches = list(pattern.finditer(result))
            if len(matches) != 1:
                raise MatrixTemplateError("九宫格素材变量绑定发生变化")
            tag, count = re.subn(
                r'(\ssrc=")[^"]*(")',
                lambda match: match.group(1) + value + match.group(2),
                matches[0].group(0), count=1,
            )
            if count != 1:
                raise MatrixTemplateError("九宫格素材备用路径发生变化")
            result = (
                result[:matches[0].start()] + tag
                + result[matches[0].end():]
            )
        return result

    @staticmethod
    def _rewrite_fixed_skill_variables(
        index_html: str, fields: dict[str, str], sizes: dict[str, int],
        template_id: str,
    ) -> str:
        pattern = re.compile(
            r'data-composition-variables=(["\'])(.*?)\1', re.S,
        )
        matches = list(pattern.finditer(index_html))
        if len(matches) != 1:
            raise MatrixTemplateError("固定 Skill 模板变量声明发生变化")
        try:
            schema = json.loads(html.unescape(matches[0].group(2)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MatrixTemplateError("固定 Skill 模板变量声明无效") from exc
        expected_variable_ids = set(fields) | set(
            FIXED_SKILL_TEMPLATE_CONFIGS[template_id].get(
                "extra_variable_ids", (),
            )
        )
        if (
            not isinstance(schema, list)
            or {str(item.get("id") or "") for item in schema}
                != expected_variable_ids
        ):
            raise MatrixTemplateError("固定 Skill 模板文字字段发生变化")
        for item in schema:
            field = str(item["id"])
            if field in fields:
                item["default"] = fields[field]
                item["maxLength"] = 200
        replacement = 'data-composition-variables="' + html.escape(
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            quote=True,
        ) + '"'
        result = (
            index_html[:matches[0].start()] + replacement
            + index_html[matches[0].end():]
        )
        if template_id == TRIPLE_STRIP_TEMPLATE_ID:
            style = f'''<style id="matrix-fixed-skill-copy">
[data-var-text]:empty{{display:none!important}}
#title,#subtitle,#ctaLine1,#ctaLine2{{white-space:pre-line!important;overflow-wrap:normal!important;text-align:center}}
#title{{font-size:{sizes["title"]}px!important;line-height:1!important}}
#subtitle{{font-size:{sizes["subtitle"]}px!important;line-height:1.05!important}}
#ctaLine1{{font-size:{sizes["ctaLine1"]}px!important;line-height:1.1!important}}
#ctaLine2{{font-size:{sizes["ctaLine2"]}px!important;line-height:1.1!important}}
</style>'''
        elif template_id == YELLOW_BANNER_TEMPLATE_ID:
            result, grading_count = re.subn(
                r'\sdata-color-grading="[^"]*"', "", result,
            )
            if grading_count != 2:
                raise MatrixTemplateError(
                    "黄条模板背景模糊绑定发生变化"
                )
            style = f'''<style id="matrix-fixed-skill-copy">
[data-var-text]:empty,#sourceLabel:empty,.body-panel:has(#body:empty),.footer:has(#cta:empty){{display:none!important}}
.background{{filter:blur(14px)!important;transform:scale(1.08)!important}}
#title,#subtitle1,#subtitle2,#body,#cta{{white-space:pre-line!important;overflow-wrap:normal!important;text-align:center}}
#title{{font-size:{sizes["title"]}px!important;line-height:1.05!important}}
#subtitle1{{font-size:{sizes["subtitle1"]}px!important;line-height:1!important}}
#subtitle2{{font-size:{sizes["subtitle2"]}px!important;line-height:1!important}}
#body{{font-size:{sizes["body"]}px!important;line-height:1.15625!important}}
#cta{{font-size:{sizes["cta"]}px!important;line-height:1.1!important}}
</style>'''
        elif template_id in MOTION_V2_TEMPLATE_IDS:
            style = f'''<style id="matrix-fixed-skill-copy">
[data-var-text]:empty{{display:none!important}}
#title,#subtitle,#body,#cta{{white-space:pre-line!important;overflow-wrap:normal!important;text-align:center}}
#title{{font-size:{sizes["title"]}px!important;line-height:1.12!important}}
#subtitle{{font-size:{sizes["subtitle"]}px!important;line-height:1.12!important}}
#body{{font-size:{sizes["body"]}px!important;line-height:1.12!important}}
#cta{{font-size:{sizes["cta"]}px!important;line-height:1.16!important}}
</style>'''
        else:
            raise MatrixTemplateError("固定 Skill 模板 ID 无效")
        if result.count("</head>") != 1:
            raise MatrixTemplateError("固定 Skill 模板 head 发生变化")
        return result.replace("</head>", style + "\n</head>", 1)

    @staticmethod
    def _rewrite_fixed_skill_bgm(
        index_html: str, enabled: bool, audio_id: str = "bound-bgm",
    ) -> str:
        if not isinstance(enabled, bool):
            raise MatrixTemplateError("固定 Skill 模板背景音乐参数无效")
        pattern = re.compile(
            rf'<audio\b(?=[^>]*\bid="{re.escape(audio_id)}")[^>]*>'
        )
        matches = list(pattern.finditer(index_html))
        if len(matches) != 1:
            raise MatrixTemplateError("固定 Skill 模板背景音乐元素发生变化")
        tag, count = re.subn(
            r'(\sdata-volume=")[^"]*(")',
            rf'\g<1>{1 if enabled else 0}\g<2>',
            matches[0].group(0), count=1,
        )
        if count != 1:
            raise MatrixTemplateError("固定 Skill 模板背景音乐音量发生变化")
        return (
            index_html[:matches[0].start()] + tag
            + index_html[matches[0].end():]
        )

    def _prepare_fixed_skill_clip(
        self, source: Path, destination: Path, start: float,
        frames: int, height: int, *, deadline_at: float,
    ) -> float:
        if (
            not source.is_file()
            or not math.isfinite(float(start))
            or float(start) < 0
            or not isinstance(frames, int) or frames <= 0
            or height not in {640, 1920}
        ):
            raise MatrixTemplateError("固定 Skill 模板素材切片参数无效")
        visible = frames / 30.0
        source_duration = self._reference_video_duration(source)
        if (
            source_duration + 0.001
            < float(start) + visible + REFERENCE_MEDIA_SAFETY_SECONDS
        ):
            raise MatrixTemplateError("固定 Skill 模板素材时长不足")
        actual_start = float(start)
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise MatrixTemplateError("固定 Skill 模板任务超过总时限")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + ".part.mp4")
        temporary.unlink(missing_ok=True)
        video_filter = (
            f"scale=1080:{height}:force_original_aspect_ratio=increase,"
            f"crop=1080:{height},setsar=1,fps=30,format=yuv420p"
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", _format_reference_seconds(actual_start), "-i", str(source),
            "-map", "0:v:0", "-an", "-vf", video_filter,
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-threads", "2",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-color_range", "tv",
            "-map_metadata", "-1", "-movflags", "+faststart", str(temporary),
        ]
        try:
            returncode, _stdout, _stderr = self._run_tracked_process(
                command,
                timeout_seconds=max(1.0, min(120.0, remaining)),
                timeout_error="固定 Skill 模板素材预处理超时",
            )
            if (
                returncode
                or not temporary.is_file()
                or temporary.stat().st_size < 1024
                or abs(self._reference_video_duration(temporary) - visible)
                    > 0.04
            ):
                raise MatrixTemplateError("固定 Skill 模板素材预处理失败")
            os.replace(temporary, destination)
            return round(actual_start, 3)
        finally:
            temporary.unlink(missing_ok=True)

    def _prepare_fixed_skill_stills(
        self, workdir: Path, config: dict, *, deadline_at: float,
    ) -> None:
        for source_relative, target_relative, at_seconds in config.get(
            "still_frames", ()
        ):
            source = workdir.joinpath(*str(source_relative).split("/"))
            target = workdir.joinpath(*str(target_relative).split("/"))
            remaining = deadline_at - time.time()
            if (
                not source.is_file()
                or not math.isfinite(float(at_seconds))
                or float(at_seconds) < 0
                or remaining <= 0
            ):
                raise MatrixTemplateError(
                    "固定 Skill 模板抽帧参数无效"
                )
            temporary = target.with_name("." + target.name + ".part.jpg")
            temporary.unlink(missing_ok=True)
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-nostdin", "-y", "-ss",
                _format_reference_seconds(float(at_seconds)),
                "-i", str(source), "-frames:v", "1", "-q:v", "2",
                str(temporary),
            ]
            try:
                returncode, _stdout, _stderr = self._run_tracked_process(
                    command,
                    timeout_seconds=max(1.0, min(30.0, remaining)),
                    timeout_error="固定 Skill 模板抽帧超时",
                )
                if (
                    returncode
                    or not temporary.is_file()
                    or temporary.stat().st_size < 1024
                ):
                    raise MatrixTemplateError(
                        "固定 Skill 模板抽帧失败"
                    )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    def _render_fixed_skill_template(
        self, payload: dict, job_id: str,
        materials: list[dict], paths: list[Path],
        *, deadline_at: float,
    ) -> dict:
        template_id = payload["template_id"]
        config = FIXED_SKILL_TEMPLATE_CONFIGS.get(template_id)
        hyperframes_version = str((config or {}).get(
            "hyperframes_version", FIXED_SKILL_HYPERFRAMES_VERSION,
        ))
        cli = (
            self.motion_v2_hyperframes_cli
            if hyperframes_version == MOTION_V2_HYPERFRAMES_VERSION
            else self.nine_grid_hyperframes_cli
        )
        frozen = payload.get("_fixed_skill_template")
        text = frozen.get("text") if isinstance(frozen, dict) else None
        root = self.fixed_skill_roots.get(template_id)
        if (
            config is None
            or not isinstance(frozen, dict)
            or frozen.get("template_id") != template_id
            or frozen.get("version") != config["version"]
            or frozen.get("hyperframes_version")
                != hyperframes_version
            or abs(float(frozen.get("duration") or 0) - config["duration"]) > 1e-9
            or frozen.get("frames") != config["frames"]
            or frozen.get("required_visuals") != config["required_visuals"]
            or frozen.get("slot_frames") != list(config["slot_frames"])
            or frozen.get("bgm_enabled") is not payload.get("bgm")
            or not isinstance(text, dict)
            or root is None
            or template_id not in self.fixed_skill_templates
            or cli is None
            or len(paths) != config["required_visuals"]
            or len(materials) != config["required_visuals"]
            or any(item.get("media_type") != "video" for item in materials)
        ):
            raise MatrixTemplateError("固定 Skill 模板冻结数据无效")
        expected_fonts = frozen.get("font_sha256")
        fonts = self.fixed_skill_fonts[template_id]
        if (
            expected_fonts != {
                family: item["sha256"] for family, item in fonts.items()
            }
            or any(
                _file_sha256(Path(item["path"])) != item["sha256"]
                for item in fonts.values()
            )
            or frozen.get("source_sha256")
                != self.fixed_skill_source_sha256[template_id]
            or frozen.get("bgm_sha256") != config["bgm_sha256"]
            or _file_sha256(root / str(config["bgm_path"]))
                != config["bgm_sha256"]
        ):
            raise MatrixTemplateError("固定 Skill 模板资源发生变化")
        selected_starts = [item.get("clip_start_seconds") for item in materials]
        selected_durations = [
            item.get("clip_duration_seconds") for item in materials
        ]
        expected_durations = [
            round(frames / 30.0, 6) for frames in config["slot_frames"]
        ]
        if not all(
            not isinstance(start, bool)
            and isinstance(start, (int, float))
            and math.isfinite(float(start))
            and float(start) >= 0
            and not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and abs(float(duration) - expected_duration) <= 0.000001
            for start, duration, expected_duration in zip(
                selected_starts, selected_durations, expected_durations,
            )
        ):
            raise MatrixTemplateError("固定 Skill 模板素材切片契约无效")
        work_root = self.data_root / job_id
        workdir = work_root / ("hyperframes-" + template_id)
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(root, workdir)
        index_path = workdir / "index.html"
        index_html = self._rewrite_fixed_skill_variables(
            index_path.read_text(encoding="utf-8"),
            text["display"], text["font_size_px"], template_id,
        )
        index_html = self._rewrite_fixed_skill_bgm(
            index_html, bool(payload["bgm"]),
            str(config.get("audio_id", "bound-bgm")),
        )
        index_path.write_text(index_html, encoding="utf-8")
        variables_path = workdir / "variables.json"
        variables_path.write_text(
            json.dumps(text["display"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        output = work_root / "output/final.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_home = self.data_root / ".hyperframes-runtime"
        cache_home = runtime_home / "cache"
        runtime_home.mkdir(parents=True, exist_ok=True)
        cache_home.mkdir(parents=True, exist_ok=True)
        command = [
            str(cli), "render", str(workdir),
            "--output", str(output), "--quality", "high", "--workers", "1",
            "--fps", "30", "--sdr", "--no-browser-gpu",
            "--strict-variables", "--variables-file", str(variables_path),
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
        actual_starts = []
        try:
            for source, start, frames, height, relative in zip(
                paths, selected_starts, config["slot_frames"],
                config["slot_heights"], config["media_paths"],
            ):
                if self.stop_event.is_set():
                    raise MatrixTemplateError("模板成片服务正在停止")
                actual_starts.append(self._prepare_fixed_skill_clip(
                    source, workdir.joinpath(*str(relative).split("/")),
                    float(start), int(frames), int(height),
                    deadline_at=deadline_at,
                ))
            for item, actual_start, actual_duration in zip(
                materials, actual_starts, expected_durations,
            ):
                item["clip_start_seconds"] = actual_start
                item["clip_duration_seconds"] = actual_duration
            self._prepare_fixed_skill_stills(
                workdir, config, deadline_at=deadline_at,
            )
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("固定 Skill 模板任务超过总时限")
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
                    raise MatrixTemplateError(
                        "固定 Skill 模板任务超过总时限"
                    ) from exc
                if process.returncode:
                    output.unlink(missing_ok=True)
                    detail = b"\n".join((stdout or b"", stderr or b"")).decode(
                        "utf-8", "replace",
                    ).strip()[-800:]
                    raise MatrixTemplateError(
                        "固定 Skill 模板成片渲染失败"
                        + (": " + detail if detail else "")
                    )
            finally:
                with self.process_lock:
                    self.active_processes.discard(process)
                    self.active_process = next(
                        iter(self.active_processes), None,
                    )
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("固定 Skill 模板任务超过总时限")
            if template_id not in MOTION_V2_TEMPLATE_IDS:
                self._validate_reference_visual_coverage(
                    output, timeout_seconds=min(120.0, remaining),
                )
        finally:
            self.hyperframes_slots.release()
        return {
            **text["display"],
            "_material_render_starts": actual_starts,
            "_bound_bgm": {
                "sha256": config["bgm_sha256"],
                "duration": config["bgm_duration"],
                "enabled": bool(payload["bgm"]),
            },
        }

    def _render_nine_grid(
        self, payload: dict, job_id: str,
        materials: list[dict], paths: list[Path],
        *, deadline_at: float,
    ) -> dict:
        frozen = payload.get("_nine_grid_template")
        text = frozen.get("text") if isinstance(frozen, dict) else None
        if (
            not isinstance(frozen, dict)
            or frozen.get("template_id") != NINE_GRID_TEMPLATE_ID
            or frozen.get("version") != NINE_GRID_TEMPLATE_VERSION
            or frozen.get("hyperframes_version")
                != NINE_GRID_HYPERFRAMES_VERSION
            or frozen.get("duration") != NINE_GRID_DURATION_SECONDS
            or frozen.get("required_visuals") != NINE_GRID_VISUAL_COUNT
            or frozen.get("main_slot_indexes")
                != list(NINE_GRID_MAIN_SLOT_INDEXES)
            or frozen.get("bgm_enabled") is not payload.get("bgm")
            or not isinstance(text, dict)
            or self.nine_grid_root is None
            or self.nine_grid_template is None
            or self.nine_grid_hyperframes_cli is None
            or len(paths) != NINE_GRID_VISUAL_COUNT
            or len(materials) != NINE_GRID_VISUAL_COUNT
            or any(item.get("media_type") != "video" for item in materials)
        ):
            raise MatrixTemplateError("frozen nine-grid template metadata is invalid")
        expected_fonts = frozen.get("font_sha256")
        if (
            not isinstance(expected_fonts, dict)
            or expected_fonts != {
                role: item["sha256"]
                for role, item in self.nine_grid_fonts.items()
            }
            or any(
                _file_sha256(Path(item["path"])) != item["sha256"]
                for item in self.nine_grid_fonts.values()
            )
            or frozen.get("bgm_sha256") != NINE_GRID_BOUND_BGM_SHA256
            or _file_sha256(
                self.nine_grid_root / "assets/audio/reference-bgm.m4a"
            ) != NINE_GRID_BOUND_BGM_SHA256
        ):
            raise MatrixTemplateError("frozen nine-grid template assets changed")
        selected_starts = [item.get("clip_start_seconds") for item in materials]
        selected_durations = [
            item.get("clip_duration_seconds") for item in materials
        ]
        if not all(
            not isinstance(start, bool)
            and isinstance(start, (int, float))
            and math.isfinite(float(start))
            and float(start) >= 0
            and not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and abs(float(duration) - NINE_GRID_SELECTED_CLIP_SECONDS) <= 0.001
            for start, duration in zip(selected_starts, selected_durations)
        ):
            raise MatrixTemplateError("九宫格素材切片契约无效")
        if time.time() >= deadline_at:
            raise MatrixTemplateError("九宫格模板任务超过总时限")
        root = self.data_root / job_id
        workdir = root / "hyperframes-nine-grid"
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(self.nine_grid_root, workdir)
        index_path = workdir / "index.html"
        variables = {
            **text["display"],
            **{
                f"grid{index}": f"assets/input/video-{index}.mp4"
                for index in range(1, NINE_GRID_VISUAL_COUNT + 1)
            },
        }
        for index, source_index in enumerate(NINE_GRID_MAIN_SLOT_INDEXES, 1):
            variables[f"main{index}"] = (
                f"assets/input/video-{source_index + 1}.mp4"
            )
        index_html = self._rewrite_nine_grid_root_style(
            index_path.read_text(encoding="utf-8"), text,
        )
        index_html = self._rewrite_nine_grid_bgm(
            index_html, bool(payload["bgm"]),
        )
        index_html = self._rewrite_nine_grid_media_sources(
            index_html, variables,
        )
        index_path.write_text(index_html, encoding="utf-8")
        variables_path = workdir / "variables.json"
        variables_path.write_text(
            json.dumps(variables, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        output = root / "output/final.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime_home = self.data_root / ".hyperframes-runtime"
        cache_home = runtime_home / "cache"
        runtime_home.mkdir(parents=True, exist_ok=True)
        cache_home.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.nine_grid_hyperframes_cli), "render", str(workdir),
            "--output", str(output), "--quality", "high", "--workers", "1",
            "--fps", str(NINE_GRID_OUTPUT_FPS), "--sdr", "--no-browser-gpu",
            "--strict-variables", "--variables-file", str(variables_path),
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
            for index, (source, start) in enumerate(
                zip(paths, selected_starts), 1,
            ):
                if self.stop_event.is_set():
                    raise MatrixTemplateError("模板成片服务正在停止")
                self._prepare_nine_grid_clip(
                    source, workdir / f"assets/input/video-{index}.mp4",
                    float(start), deadline_at=deadline_at,
                )
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("九宫格模板任务超过总时限")
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
                    raise MatrixTemplateError(
                        "九宫格模板任务超过总时限"
                    ) from exc
                if process.returncode:
                    output.unlink(missing_ok=True)
                    detail = b"\n".join((stdout or b"", stderr or b"")).decode(
                        "utf-8", "replace",
                    ).strip()[-800:]
                    raise MatrixTemplateError(
                        "九宫格模板成片渲染失败"
                        + (": " + detail if detail else "")
                    )
            finally:
                with self.process_lock:
                    self.active_processes.discard(process)
                    self.active_process = next(
                        iter(self.active_processes), None,
                    )
            remaining = deadline_at - time.time()
            if remaining <= 0:
                raise MatrixTemplateError("九宫格模板任务超过总时限")
            self._validate_reference_visual_coverage(
                output, timeout_seconds=min(120.0, remaining),
            )
        finally:
            self.hyperframes_slots.release()
        variables["_bound_bgm"] = {
            "sha256": NINE_GRID_BOUND_BGM_SHA256,
            "duration": NINE_GRID_DURATION_SECONDS,
            "enabled": bool(payload["bgm"]),
        }
        return variables

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
        visual_count = _required_visuals(float(reference["duration"]))
        if len(paths) < visual_count or any(
            item.get("media_type") != "video"
            for item in materials[:visual_count]
        ):
            raise MatrixTemplateError("HyperFrames 模板视频素材数量不足")
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
        staged_private_fonts = {}
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
            ):
                raise MatrixTemplateError(
                    "HyperFrames frozen private font is unavailable or changed"
                )
            filename = current["file"]
            fingerprint = (family, current["sha256"])
            if filename in staged_filenames:
                if staged_private_fonts.get(filename) != fingerprint:
                    raise MatrixTemplateError(
                        "HyperFrames frozen private font is unavailable or changed"
                    )
                continue
            shutil.copy2(current["path"], fonts_dir / current["file"])
            staged_filenames.add(filename)
            staged_private_fonts[filename] = fingerprint

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
        media_durations = [
            self._reference_video_duration(path)
            for path in paths[:visual_count]
        ]
        segment_starts, segment_durations, media_offsets = _reference_segment_timing(
            float(reference["duration"]), media_durations, seed=job_id
        )
        selected_clip_starts = [
            item.get("clip_start_seconds")
            for item in materials[:visual_count]
        ]
        selected_clip_durations = [
            item.get("clip_duration_seconds")
            for item in materials[:visual_count]
        ]
        if any(
            value is not None
            for value in selected_clip_starts + selected_clip_durations
        ):
            if not all(
                not isinstance(start, bool)
                and isinstance(start, (int, float))
                and math.isfinite(float(start))
                and float(start) >= 0
                and not isinstance(duration, bool)
                and isinstance(duration, (int, float))
                and abs(float(duration) - segment_durations[index]) <= 0.001
                and float(start) + segment_durations[index]
                <= media_durations[index] - REFERENCE_MEDIA_SAFETY_SECONDS + 0.001
                for index, (start, duration) in enumerate(zip(
                    selected_clip_starts, selected_clip_durations,
                ))
            ):
                raise MatrixTemplateError(
                    "HyperFrames 模板素材切片参数无效"
                )
            media_offsets = [float(value) for value in selected_clip_starts]
        video_values = []
        for asset_index, source in enumerate(paths[:visual_count], 1):
            target = input_dir / f"video-{asset_index}{source.suffix.lower()}"
            self._copy_reference_asset(source, target)
            video_values.append(target.relative_to(workdir).as_posix())
        bgm_source = None
        bgm_target = None
        if payload["bgm"]:
            if (
                len(paths) <= visual_count
                or materials[visual_count].get("media_type") != "bgm"
            ):
                raise MatrixTemplateError("HyperFrames template BGM binding is invalid")
            bgm_target = input_dir / "bgm.m4a"
            bgm_source = paths[visual_count]
            bgm = bgm_target.relative_to(workdir).as_posix()
        else:
            bgm = "assets/bgm/silence.m4a"
        index = _rewrite_reference_bgm_source(index, bgm)

        variables = {
            "name": job_id,
            "variant": reference["variant"],
            **(display_text if isinstance(display_text, dict) else reference["text"]),
            "duration": reference["duration"],
            "bgm": bgm,
        }
        for asset_index, value in enumerate(video_values):
            variables[REFERENCE_VIDEO_IDS[asset_index]] = value
        index = _expand_reference_video_slots(index, video_values)
        index = _rewrite_reference_timeline(
            index, float(reference["duration"]),
            segment_starts, segment_durations, media_offsets,
        )
        editing_plan = reference.get("editing_plan")
        if editing_plan is not None:
            editing_plan = _validate_reference_editing_plan(editing_plan)
            if len(editing_plan["segments"]) != visual_count:
                raise MatrixTemplateError("HyperFrames 剪辑方案片段数量不匹配")
            index = _inject_reference_editing_plan(index, editing_plan)
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
        if editing_plan is not None:
            variables["_editing_plan"] = editing_plan
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

    def _material_manifest(self, payload: dict, materials: list[dict]) -> list[dict]:
        scenes, _count, _reference = self._material_scenes(payload)
        scene_map = {scene["scene_id"]: scene for scene in scenes}
        manifest = []
        for index, item in enumerate(materials, 1):
            provider = str(item.get("provider") or "")
            source = (
                "user" if provider == MATERIAL_PROVIDER_USER
                else "pexels" if provider == "pexels"
                else "shared"
            )
            scene = scene_map.get(str(item.get("scene_id") or ""), {})
            record = {
                "slot": index,
                "scene_id": item.get("scene_id"),
                "source": source,
                "record_id": item.get("record_id"),
                "media_type": item.get("media_type"),
                "clip_start_seconds": float(item.get("clip_start_seconds") or 0),
                "clip_duration_seconds": float(
                    item.get("clip_duration_seconds")
                    or scene.get("clip_duration_seconds") or 0
                ),
                "match_level": item.get("match_level"),
            }
            if source == "pexels":
                record.update({
                    "pexels_id": item.get("provider_video_id"),
                    "content_sha256": item.get("content_sha256"),
                    "source_identity": item.get("source_identity"),
                    "provider_file_id": item.get("provider_file_id"),
                    "provider_url": item.get("provider_url"),
                    "contributor_name": item.get("contributor_name"),
                    "contributor_url": item.get("contributor_url"),
                    "search_query": item.get("search_query"),
                })
            else:
                record["sha256"] = item.get("sha256")
            if item.get("clip_id"):
                record.update({
                    "clip_id": item.get("clip_id"),
                    "clip_slot_index": item.get("clip_slot_index"),
                    "clip_slot_count": item.get("clip_slot_count"),
                })
            manifest.append(record)
        return manifest

    def _execute(self, job_id: str) -> dict:
        row = self.store.get(job_id)
        payload = json.loads(row["payload"])
        root = self.data_root / job_id
        self._discard_output(job_id)
        assets = root / "assets/library"
        assets.mkdir(parents=True, exist_ok=True)
        materials = self._select_materials(payload, job_id)
        paths = [self._download(item, assets, job_id) for item in materials]
        provenance = payload["_font_provenance"]
        reference_template = payload["template_id"] in self.reference_templates
        nine_grid_template = payload["template_id"] == NINE_GRID_TEMPLATE_ID
        fixed_skill_template = payload["template_id"] in self.fixed_skill_templates
        if fixed_skill_template:
            deadline_at = (
                float(row["created_at"]) + self.hyperframes_total_timeout_seconds
            )
            variables = self._render_fixed_skill_template(
                payload, job_id, materials, paths, deadline_at=deadline_at,
            )
            font_selection = provenance["selection"]
            display_top_text = "\n".join(
                variables[key]
                for key in ("title", "subtitle", "subtitle1", "subtitle2")
                if variables.get(key)
            )
            editing_plan = None
            engine = "hyperframes"
        elif nine_grid_template:
            deadline_at = (
                float(row["created_at"]) + self.hyperframes_total_timeout_seconds
            )
            variables = self._render_nine_grid(
                payload, job_id, materials, paths, deadline_at=deadline_at,
            )
            font_selection = provenance["selection"]
            display_top_text = variables["top_text"]
            editing_plan = None
            engine = "hyperframes"
        elif reference_template:
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
            editing_plan = variables.get("_editing_plan")
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
            editing_plan = None
            engine = "ffmpeg"
        output = root / "output/final.mp4"
        try:
            probe = self._probe(output)
            os.replace(output, root / "output/published.mp4")
        except Exception:
            self._discard_output(job_id)
            raise
        material_contract_version = self._material_contract_version(payload)
        return {
            **probe,
            "template_id": payload["template_id"],
            "batch_id": payload.get("batch_id") or "",
            "batch_index": payload.get("batch_index"),
            "batch_size": payload.get("batch_size"),
            "file_url": f"/v1/files/{job_id}.mp4",
            "engine": engine,
            "font_mode": (
                "template_locked"
                if reference_template or nine_grid_template
                or fixed_skill_template else "selectable"
            ),
            "font_selection": font_selection,
            "display_top_text": display_top_text,
            "font_files": provenance["fonts"],
            "private_font_bundle_sha256": provenance["private_bundle_sha256"],
            "material_selection_contract_version": material_contract_version,
            **({
                "material_clip_contract_version": MATERIAL_CLIP_CONTRACT_VERSION,
            } if material_contract_version >= MATERIAL_SELECTION_CONTRACT_VERSION else {}),
            "material_manifest": self._material_manifest(payload, materials),
            "editing_plan": editing_plan,
            **({
                "bgm_mode": "bound",
                "nine_grid_visuals": NINE_GRID_VISUAL_COUNT,
            } if nine_grid_template else {}),
            **({
                "bgm_mode": "bound",
                "fixed_duration_seconds": FIXED_SKILL_TEMPLATE_CONFIGS[
                    payload["template_id"]
                ]["duration"],
                "fixed_skill_template": True,
            } if fixed_skill_template else {}),
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
                "templates": [
                    {**item, "palette_version": PUBLIC_TEMPLATE_PALETTE_VERSION}
                    for item in self.service.catalog
                ],
                "default_template": self.service.default_template_id,
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

    def _receive_user_asset(self) -> None:
        """接收一个用户素材文件，按 sha256 落盘。请求体是原始二进制。"""
        expected = str(self.headers.get("X-HQ-Asset-Sha256") or "").strip().lower()
        if not SHA_RE.fullmatch(expected):
            self.send_json(400, {"error": "invalid_request", "detail": "缺少有效的素材校验值"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        suffix = CONTENT_SUFFIXES.get(content_type)
        if suffix not in {".jpg", ".png", ".webp", ".mp4", ".mov"}:
            self.send_json(400, {"error": "invalid_request", "detail": "素材类型不受支持"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            self.send_json(400, {"error": "invalid_request", "detail": "请求长度无效"})
            return
        if length <= 0 or length > MAX_USER_ASSET_BYTES:
            self.send_json(400, {"error": "invalid_request", "detail": "素材大小超出限制"})
            return
        target_dir = self.service.data_root / USER_ASSET_DIRNAME
        target = target_dir / (expected + suffix)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            total = 0
            temporary = target.with_suffix(target.suffix + ".part")
            try:
                with temporary.open("wb") as handle:
                    while True:
                        chunk = self.rfile.read(min(1024 * 1024, length - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_USER_ASSET_BYTES:
                            raise ValueError("素材大小超出限制")
                        digest.update(chunk)
                        handle.write(chunk)
                if total != length:
                    raise ValueError("素材传输不完整")
                if not hmac.compare_digest(digest.hexdigest(), expected):
                    raise ValueError("素材校验失败")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except ValueError as exc:
            self.send_json(400, {"error": "invalid_request", "detail": str(exc)})
            return
        except OSError:
            self.send_json(500, {"error": "storage_failed"})
            return
        self.send_json(200, {"ok": True, "sha256": expected, "bytes": total})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/v1/jobs", "/v1/preflight", "/v1/user-assets"}:
            self.send_json(404, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        if path == "/v1/user-assets":
            self._receive_user_asset()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if path == "/v1/preflight":
                payload = self.service.validate_payload(
                    body, require_reference_semantic_layout=True,
                )
                self.service._validate_material_policy(payload)
                if payload["material_policy"] == "owned_public":
                    library = {
                        "selection_contract_version": MATERIAL_SELECTION_CONTRACT_VERSION,
                        "clip_contract_version": MATERIAL_CLIP_CONTRACT_VERSION,
                    }
                else:
                    library = self.service.require_library_ready(force=True)
                self.send_json(200, {
                    "ok": True,
                    "payload": payload,
                    "duration": payload["duration"],
                    "required_visuals": self.service.required_visuals(payload),
                    "material_selection_contract_version": library[
                        "selection_contract_version"
                    ],
                    "material_clip_contract_version": library[
                        "clip_contract_version"
                    ],
                    "duration_mode": (
                        "fixed_12"
                        if payload["template_id"] == NINE_GRID_TEMPLATE_ID
                        else (
                            "fixed"
                            if payload["template_id"]
                                in self.service.fixed_skill_templates
                            else (
                                "random_integer_8_15"
                                if payload["template_id"]
                                    in self.service.reference_templates
                                else "copy_length"
                            )
                        )
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
    nine_grid_root_value = os.environ.get(
        "MATRIX_TEMPLATE_NINE_GRID_ROOT", ""
    ).strip()
    triple_strip_root_value = os.environ.get(
        "MATRIX_TEMPLATE_TRIPLE_STRIP_ROOT", ""
    ).strip()
    yellow_banner_root_value = os.environ.get(
        "MATRIX_TEMPLATE_YELLOW_BANNER_ROOT", ""
    ).strip()
    fan_whip_root_value = os.environ.get(
        "MATRIX_TEMPLATE_FAN_WHIP_ROOT", ""
    ).strip()
    brush_panel_root_value = os.environ.get(
        "MATRIX_TEMPLATE_BRUSH_PANEL_ROOT", ""
    ).strip()
    service = MatrixTemplateService(
        data_root=Path(os.environ.get("MATRIX_TEMPLATE_DATA_ROOT", "/var/lib/huangque-matrix-template")),
        skill_root=Path(os.environ.get("MATRIX_TEMPLATE_SKILL_ROOT", "/opt/huangque/matrix-template-video/source/skill/script-to-matrix-video")),
        library_url=os.environ.get("PIXELLE_MATERIAL_LIBRARY_URL", "http://127.0.0.1:8111"),
        library_token=os.environ.get("PIXELLE_MATERIAL_LIBRARY_TOKEN", ""),
        pexels_api_key=os.environ.get("PEXELS_API_KEY", ""),
        legacy_templates_enabled=False,
        python=os.environ.get("MATRIX_TEMPLATE_PYTHON", sys.executable),
        private_font_root=Path(os.environ.get(
            "MATRIX_TEMPLATE_PRIVATE_FONT_ROOT",
            "/var/lib/huangque-matrix-template/private-fonts",
        )),
        reference_skill_root=Path(reference_root_value) if reference_root_value else None,
        nine_grid_root=Path(nine_grid_root_value) if nine_grid_root_value else None,
        triple_strip_root=(
            Path(triple_strip_root_value) if triple_strip_root_value else None
        ),
        yellow_banner_root=(
            Path(yellow_banner_root_value) if yellow_banner_root_value else None
        ),
        fan_whip_root=(
            Path(fan_whip_root_value) if fan_whip_root_value else None
        ),
        brush_panel_root=(
            Path(brush_panel_root_value) if brush_panel_root_value else None
        ),
        hyperframes_cli=Path(os.environ.get(
            "MATRIX_TEMPLATE_HYPERFRAMES_CLI", "/usr/local/bin/hyperframes"
        )),
        nine_grid_hyperframes_cli=Path(os.environ.get(
            "MATRIX_TEMPLATE_NINE_GRID_HYPERFRAMES_CLI",
            "/opt/huangque/matrix-template-video/source/"
            "nine-grid-runtime/node_modules/.bin/hyperframes",
        )),
        motion_v2_hyperframes_cli=Path(os.environ.get(
            "MATRIX_TEMPLATE_MOTION_V2_HYPERFRAMES_CLI",
            "/opt/huangque/matrix-template-video/source/"
            "motion-v2-runtime/hyperframes",
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
