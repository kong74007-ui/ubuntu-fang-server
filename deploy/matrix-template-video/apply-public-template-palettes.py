#!/usr/bin/env python3
"""Inject the fixed public-template color palette overlay.

This runs inside ``install.sh`` after the upstream reference patch and after
the nine-grid adapter have produced the four real template entry points:

* ``reference-typography-17/index.html`` (variants ``v01``..``v17``)
* ``nine-grid-reveal/index.html``
* ``triple-strip-shutter/index.html``
* ``yellow-banner-zoom/index.html``

It only ever injects a single ``<style id="matrix-public-template-palettes-v1">``
block before ``</head>``. It never rewrites the upstream CSS, the DOM copy, or
any layout property. Every target selector is verified to exist first, and the
written file is re-read to prove the palette colors landed and that no banned
property (filter / blend modes / grading) leaked in.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PALETTE_VERSION = "matrix-public-template-palettes-v1"
STYLE_ID = "matrix-public-template-palettes-v1"
OUTLINE = "#080808"

# role -> which layers use it, for the 17 reference variants.
REFERENCE_LAYER_ROLES = {
    "top1": "c1",
    "top2": "c3",
    "top3": "c1",
    "bottom1": "c3",
    "bottom2": "c1",
}
REFERENCE_LAYERS = ("top1", "top2", "top3", "bottom1", "bottom2")
REFERENCE_BACKGROUND_LAYERS = {
    "v01": ("bottom2",),
    "v03": ("bottom1",),
    "v04": ("top3",),
    "v05": ("bottom2",),
    "v06": ("bottom2",),
    "v08": ("bottom2",),
}
# Existing box-shadows whose colour only is neutralised. Offsets, blur, spread,
# layer count and alpha are copied verbatim from the upstream template.
REFERENCE_BOX_SHADOW_OVERRIDES = {
    "v05": {
        "bottom2": (
            "0 10px 0 rgba(8, 8, 8, 0.85), 0 15px 24px rgba(0, 0, 0, 0.35)"
        ),
    },
}

# The 20 public templates in production order. Index 1..17 are the reference
# variants, 18 is nine-grid, 19 is triple-strip, 20 is yellow-banner.
PALETTES = (
    ("ref-01-chengdu-green-brush", "成都绿描边手写", "reference", "v01", "#fffbea", "#470125", "#fffbea"),
    ("ref-02-shenzhen-ai-orange", "深圳AI橙色主标题", "reference", "v02", "#ff043a", "#002169", "#fffbea"),
    ("ref-03-zhengzhou-blue-banner", "郑州蓝色标题红横条", "reference", "v03", "#12f2fd", "#690dad", "#fffbea"),
    ("ref-04-foshan-yellow-strip", "佛山黄色信息条", "reference", "v04", "#fffbea", "#47176d", "#fffbea"),
    ("ref-05-changsha-white-red", "长沙白字红强调", "reference", "v05", "#f0ff0c", "#ff0086", "#fffbea"),
    ("ref-06-guangzhou-yellow-button", "广州黄色按钮CTA", "reference", "v06", "#fffeee", "#4e8966", "#fffeee"),
    ("ref-07-shenzhen-red-growth", "深圳红色成长强调", "reference", "v07", "#fe019a", "#101820", "#fffbea"),
    ("ref-08-puyang-yellow-white", "濮阳黄白层级", "reference", "v08", "#fdeb55", "#185a56", "#fffbea"),
    ("ref-09-urumqi-soft-brush", "乌鲁木齐柔和手写", "reference", "v09", "#fade1f", "#690dad", "#fffbea"),
    ("ref-10-shenzhen-sisters", "深圳姐妹自我提升", "reference", "v10", "#ff6047", "#000035", "#fffbea"),
    ("ref-11-nansha-clean", "南沙清爽三层标题", "reference", "v11", "#12f2fd", "#e4058e", "#fffbea"),
    ("ref-12-guangzhou-brush", "广州手写聚会", "reference", "v12", "#ffff00", "#002fa7", "#fffbea"),
    ("ref-13-shenzhen-green-location", "深圳绿色坐标CTA", "reference", "v13", "#00e592", "#da2357", "#fffbea"),
    ("ref-14-karamay-green", "克拉玛依绿系手写", "reference", "v14", "#fffbea", "#0f64b5", "#fffbea"),
    ("ref-15-tianjin-monochrome", "天津黑白极简", "reference", "v15", "#08fc2e", "#101820", "#fffbea"),
    ("ref-16-shenzhen-opc", "深圳OPC多层信息", "reference", "v16", "#fd742d", "#01008a", "#fffbea"),
    ("ref-17-shenzhen-yellow-red", "深圳黄红爆款层级", "reference", "v17", "#fbfff2", "#008e6b", "#fbfff2"),
    ("nine-grid-reveal", "九宫格开场·全屏展示", "nine-grid", "", "#cca4e3", "#6583e0", "#fffbea"),
    ("triple-strip-shutter", "三横屏开场·光栅快切", "triple-strip", "", "#f2e1ff", "#61ac4c", "#fffbea"),
    ("yellow-banner-zoom", "黄条标题·变幅冲击", "yellow-banner", "", "#90e0d6", "#e97a46", "#fffbea"),
)

HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
BANNED_RE = re.compile(
    r"filter\s*:|hue-rotate|saturate\(|contrast\(|brightness\(|"
    r"mix-blend-mode|backdrop-filter",
    re.IGNORECASE,
)


def palette_by_kind(kind: str) -> list[tuple]:
    return [item for item in PALETTES if item[2] == kind]


def _hex(value: str) -> str:
    value = value.lower()
    if not HEX_RE.fullmatch(value):
        raise ValueError(f"invalid palette color: {value!r}")
    return value


def build_reference_css() -> str:
    blocks = []
    for template_id, name, _kind, variant, c1, c2, c3 in palette_by_kind("reference"):
        colors = {"c1": _hex(c1), "c2": _hex(c2), "c3": _hex(c3)}
        groups: dict[str, list[str]] = {"c1": [], "c3": []}
        for layer in REFERENCE_LAYERS:
            groups[REFERENCE_LAYER_ROLES[layer]].append(layer)
        lines = [f"/* {variant} {template_id} · {name} */"]
        for role in ("c1", "c3"):
            selectors = ",\n".join(
                f"#root.{variant} .{layer}" for layer in groups[role]
            )
            lines.append(
                f"{selectors} {{\n"
                f"  color: {colors[role]};\n"
                f"  -webkit-text-stroke-color: {OUTLINE};\n"
                f"}}"
            )
        for layer in REFERENCE_BACKGROUND_LAYERS.get(variant, ()):
            lines.append(
                f"#root.{variant} .{layer} {{ background-color: {colors['c2']}; }}"
            )
        for layer, shadow in REFERENCE_BOX_SHADOW_OVERRIDES.get(variant, {}).items():
            lines.append(
                f"#root.{variant} .{layer} {{ box-shadow: {shadow}; }}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


NINE_GRID_CSS = """/* 18 nine-grid-reveal · 九宫格开场·全屏展示 */
#headline {
  color: #cca4e3;
  -webkit-text-stroke-color: #080808;
  text-shadow: 5px 6px 3px #6583e0;
}
#tagline {
  color: #fffbea;
  -webkit-text-stroke-color: #080808;
  text-shadow: 3px 4px 2px #6583e0;
}"""

TRIPLE_STRIP_CSS = """/* 19 triple-strip-shutter · 三横屏开场·光栅快切 */
.title {
  color: #f2e1ff;
  -webkit-text-stroke-color: #080808;
  text-shadow: -4px -3px 0 #61ac4c, 5px 5px 0 #f2e1ff, 11px 9px 0 #101820;
}
.subtitle {
  color: #fffbea;
  -webkit-text-stroke-color: #080808;
  text-shadow: -3px -3px 0 #61ac4c, 4px 4px 0 #f2e1ff, 7px 6px 0 #101820;
}
.footer-text {
  color: #fffbea;
  -webkit-text-stroke-color: #080808;
  text-shadow: 3px 4px 0 #61ac4c, 7px 7px 1px #101820;
}
.underline .line {
  background-color: #61ac4c;
}
.underline .slash {
  background-color: #f2e1ff;
}
.underline .slash:last-child {
  background-color: #61ac4c;
}
.chevron path {
  stroke: #61ac4c;
}
.chevron path:not([fill="none"]) {
  fill: #61ac4c;
}"""

YELLOW_BANNER_CSS = """/* 20 yellow-banner-zoom · 黄条标题·变幅冲击 */
.banner {
  background-color: #e97a46;
}
#title {
  color: #101820;
}
.subtitle,
#cta {
  color: #90e0d6;
  -webkit-text-stroke-color: #080808;
}
#sourceLabel,
#body {
  color: #fffbea;
}
.body-panel {
  background-color: rgba(16, 24, 32, 0.42);
}"""

TEMPLATE_CSS = {
    "reference": build_reference_css,
    "nine-grid": lambda: NINE_GRID_CSS,
    "triple-strip": lambda: TRIPLE_STRIP_CSS,
    "yellow-banner": lambda: YELLOW_BANNER_CSS,
}

# Every selector that must already exist in the target HTML.
REQUIRED_SELECTORS = {
    "reference": [f'id="{layer}"' for layer in REFERENCE_LAYERS],
    "nine-grid": ['id="headline"', 'id="tagline"'],
    "triple-strip": ['class="title"', 'class="subtitle"', 'class="footer-text"', 'class="underline"', 'class="chevron"'],
    "yellow-banner": ['class="banner"', 'id="title"', 'class="subtitle"', 'id="body"'],
}

SOURCE_FILES = {
    "reference": "index.html",
    "nine-grid": "index.html",
    "triple-strip": "index.html",
    "yellow-banner": "index.html",
}


def style_block(kind: str) -> str:
    css = TEMPLATE_CSS[kind]()
    return f'<style id="{STYLE_ID}">\n{css}\n</style>\n'


def inject(index_html: str, kind: str) -> str:
    if f'id="{STYLE_ID}"' in index_html:
        raise ValueError(f"{STYLE_ID} already present; refusing double injection")
    if index_html.count("</head>") != 1:
        raise ValueError("template must contain exactly one </head>")
    for marker in REQUIRED_SELECTORS[kind]:
        if marker not in index_html:
            raise ValueError(f"missing required selector {marker!r}")
    block = style_block(kind)
    return index_html.replace("</head>", block + "</head>", 1)


def expected_colors(kind: str) -> set[str]:
    colors = {OUTLINE}
    for _tid, _name, _kind, variant, c1, c2, c3 in palette_by_kind(kind):
        colors.add(_hex(c1))
        colors.add(_hex(c3))
        if kind != "reference" or variant in REFERENCE_BACKGROUND_LAYERS:
            colors.add(_hex(c2))
    return colors


def assert_written(index_html: str, kind: str) -> None:
    block_match = re.search(
        rf'<style id="{STYLE_ID}">(.*?)</style>', index_html, re.DOTALL
    )
    if not block_match:
        raise ValueError("palette style block is missing after write")
    block = block_match.group(1)
    if BANNED_RE.search(block):
        raise ValueError("palette block introduced a banned property")
    for color in expected_colors(kind):
        if color not in block.lower():
            raise ValueError(f"palette color {color} missing from block")


def apply_root(kind: str, root: Path) -> Path:
    path = root / SOURCE_FILES[kind]
    if not path.is_file():
        raise ValueError(f"template entry point not found: {path}")
    index_html = path.read_text(encoding="utf-8")
    updated = inject(index_html, kind)
    assert_written(updated, kind)
    path.write_text(updated, encoding="utf-8")
    assert_written(path.read_text(encoding="utf-8"), kind)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--nine-grid-root", type=Path)
    parser.add_argument("--triple-strip-root", type=Path)
    parser.add_argument("--yellow-banner-root", type=Path)
    args = parser.parse_args()
    targets = [
        ("reference", args.reference_root),
        ("nine-grid", args.nine_grid_root),
        ("triple-strip", args.triple_strip_root),
        ("yellow-banner", args.yellow_banner_root),
    ]
    targets = [(kind, root) for kind, root in targets if root is not None]
    if not targets:
        parser.error("at least one template root is required")
    for kind, root in targets:
        path = apply_root(kind, root)
        print(f"[palette] {PALETTE_VERSION} applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
