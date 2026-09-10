#!/usr/bin/env python3
"""Adapt the pinned nine-grid Skill template to the website copy contract."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path


TEMPLATE_ID = "nine-grid-reveal"
STYLE_ID = "matrix-nine-grid-copy-layout"
OVERRIDE_STYLE = f"""<style id="{STYLE_ID}">
#headline{{left:54px;right:54px;top:154px;min-height:116px;max-height:340px;gap:8px;text-align:center;font-size:var(--top-font-size,82px);line-height:1.08}}
#top-text{{display:block;max-width:800px;white-space:pre;overflow-wrap:normal;word-break:keep-all;letter-spacing:0}}
#tagline{{left:55px;right:55px;top:auto;bottom:220px;height:auto;max-height:250px;display:block;text-align:center;white-space:pre;overflow-wrap:normal;word-break:keep-all;font-size:var(--bottom-font-size,58px);line-height:1.12;letter-spacing:0}}
</style>"""


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise ValueError(f"nine-grid {label} source contract changed")
    return value.replace(old, new, 1)


def rewrite_media_start(source: str, element_id: str) -> str:
    pattern = re.compile(
        rf'<video\b(?=[^>]*\bid="{re.escape(element_id)}")[^>]*>'
    )
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError("nine-grid main video source contract changed")
    tag, count = re.subn(
        r'(\sdata-media-start=")[^"]*(")', r'\g<1>0\g<2>',
        matches[0].group(0), count=1,
    )
    if count != 1:
        raise ValueError("nine-grid main media start contract changed")
    return source[:matches[0].start()] + tag + source[matches[0].end():]


def adapt(root: Path) -> None:
    root = root.resolve(strict=True)
    index_path = root / "index.html"
    manifest_path = root / "template.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (index_path, manifest_path)
    ):
        raise ValueError("nine-grid template source is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("id") != TEMPLATE_ID
        or manifest.get("version") != 3
        or manifest.get("renderer") != "hyperframes@0.8.33"
        or manifest.get("canvas") != [1080, 1920]
        or manifest.get("fps") != 30
        or manifest.get("duration") != 12
        or manifest.get("text_fields") != ["title", "tagline"]
        or manifest.get("text_limits") != {"title": 9, "tagline": 16}
    ):
        raise ValueError("nine-grid template manifest source contract changed")
    manifest["version"] = 4
    manifest["text_fields"] = ["top_text", "bottom_text"]
    manifest["text_limits"] = {"top_text": 60, "bottom_text": 80}
    manifest["text_layout"] = {
        "mode": "semantic-then-width",
        "semantic_layout_required": True,
        "top_max_lines": 4,
        "bottom_max_lines": 4,
        "hide_edge_punctuation": True,
        "truncate": False,
    }

    source = index_path.read_text(encoding="utf-8-sig")
    schema_pattern = re.compile(
        r'data-composition-variables=(["\'])(.*?)\1', re.DOTALL,
    )
    match = schema_pattern.search(source)
    if match is None:
        raise ValueError("nine-grid variable schema is missing")
    schema = json.loads(html.unescape(match.group(2)))
    if not isinstance(schema, list):
        raise ValueError("nine-grid variable schema is invalid")
    records = {
        item.get("id"): item for item in schema if isinstance(item, dict)
    }
    if set(("title", "tagline")) - set(records):
        raise ValueError("nine-grid copy variables changed")
    title = records["title"]
    tagline = records["tagline"]
    if title.get("maxLength") != 9 or tagline.get("maxLength") != 16:
        raise ValueError("nine-grid copy limits changed")
    title.update({
        "id": "top_text", "label": "顶部标题", "default": "输入顶部标题",
    })
    tagline.update({
        "id": "bottom_text", "label": "底部行动文案",
        "default": "评论区留下关键词，领取完整方案",
    })
    title.pop("maxLength", None)
    tagline.pop("maxLength", None)
    encoded = html.escape(
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    source = source[:match.start(2)] + encoded + source[match.end(2):]
    source = replace_once(
        source,
        '<span id="title" data-var-text="title">输入公司名称</span>',
        '<span id="top-text" data-var-text="top_text">输入顶部标题</span>',
        "top copy binding",
    )
    source = replace_once(
        source,
        '<div id="tagline" data-var-text="tagline">品质｜细节｜诚信｜口碑</div>',
        '<div id="tagline" data-var-text="bottom_text">'
        '评论区留下关键词，领取完整方案</div>',
        "bottom copy binding",
    )
    if STYLE_ID in source or source.count("</head>") != 1:
        raise ValueError("nine-grid style insertion contract changed")
    source = source.replace("</head>", OVERRIDE_STYLE + "\n</head>", 1)
    for index in range(1, 4):
        source = rewrite_media_start(source, f"main-video{index}")
    index_path.write_text(source, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    adapt(args.root)
    print(f"adapted nine-grid template: {args.root}")


if __name__ == "__main__":
    main()
