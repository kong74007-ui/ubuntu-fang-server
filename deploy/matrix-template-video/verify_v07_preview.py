#!/usr/bin/env python3
"""Verify the patched v07 red-yellow five-layer layout at frame zero.

This loads the real patched ``reference-typography-17/index.html`` CSS and the
v07 ``preview-data.js`` phrase, then renders a 1080x1920 frame in headless
Chrome and checks fonts, styles, first-frame visibility, safe areas and
layer overlap. It never mutates the template pack.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path


CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
SIDE_SAFE_PX = 42
CTA_SAFE_BOTTOM_PERCENT = 15

EXPECTED_LAYERS = {
    "top1": {"font-size": "118px", "color": "rgb(212, 20, 13)"},
    "top2": {"font-size": "82px", "color": "rgb(255, 213, 28)"},
    "top3": {"font-size": "51px", "color": "rgb(255, 213, 28)"},
    "bottom1": {"font-size": "57px", "color": "rgb(212, 20, 13)"},
    "bottom2": {"font-size": "86px", "color": "rgb(212, 20, 13)"},
}
EXPECTED_STROKE_COLORS = {
    "top1": "rgb(255, 233, 190)",
    "top2": "rgb(16, 16, 16)",
    "top3": "rgb(16, 16, 16)",
    "bottom1": "rgb(255, 233, 190)",
    "bottom2": "rgb(255, 233, 190)",
}

CSS_RULE_RE = re.compile(
    r"\.v07\s+\.(?P<layer>top1|top2|top3|bottom1|bottom2)"
    r"\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)


def _extract_v07_css(index_html: str) -> dict[str, str]:
    rules = {}
    for match in CSS_RULE_RE.finditer(index_html):
        rules[match.group("layer")] = match.group("body").strip()
    if set(rules) != {"top1", "top2", "top3", "bottom1", "bottom2"}:
        raise RuntimeError(
            "v07 template is missing one of the five layer styles"
        )
    return rules


def _extract_v07_row(preview_js: str) -> dict:
    match = re.search(
        r"window\.__previewRows\s*=\s*(\[.*?\])\s*;", preview_js, re.DOTALL
    )
    if not match:
        raise RuntimeError("v07 preview-data.js is not a previewRows array")
    rows = json.loads(match.group(1))
    for row in rows:
        if row.get("variant") == "v07":
            return row
    raise RuntimeError("v07 preview row is missing")


def _document(rules: dict[str, str], row: dict, font_uri: str) -> str:
    phrases = {
        layer: str(row.get(layer) or "").replace("\n", "\\n")
        for layer in ("top1", "top2", "top3", "bottom1", "bottom2")
    }
    top_layers = "".join(
        f'<div id="{layer}" class="{layer}">{phrases[layer]}</div>'
        for layer in ("top1", "top2", "top3", "bottom1")
    )
    bottom_layer = f'<div id="bottom2" class="bottom2">{phrases["bottom2"]}</div>'
    layer_css = "\n".join(
        f"#{layer}{{{body}}}" for layer, body in rules.items()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@font-face{{font-family:"NotoSC";src:url({json.dumps(font_uri)});font-weight:100 900}}
*{{box-sizing:border-box}}
html,body{{margin:0;width:1080px;height:1920px;overflow:hidden;background:#000}}
#stage{{position:relative;width:1080px;height:1920px;overflow:hidden}}
.top,.bottom{{position:absolute;left:0;width:100%;padding-left:42px;padding-right:42px;text-align:center}}
.top{{top:8%}}
.bottom{{bottom:15%}}
.top1,.top2,.top3,.bottom1,.bottom2{{margin:0 auto;max-width:996px;white-space:pre-line;overflow-wrap:anywhere;font-family:"NotoSC";font-weight:900;paint-order:stroke fill}}
.top2,.top3,.bottom1{{margin-top:16px}}
{layer_css}
#report{{display:none}}
</style></head><body><div id="stage">
<div class="top">{top_layers}</div>
<div class="bottom">{bottom_layer}</div>
</div><pre id="report"></pre><script>
function styleOf(layer){{
  const element=document.getElementById(layer);
  const computed=getComputedStyle(element);
  return {{
    display:computed.display,
    visibility:computed.visibility,
    opacity:computed.opacity,
    color:computed.color,
    fontSize:computed.fontSize,
    stroke:computed.webkitTextStroke,
    strokeColor:computed.webkitTextStrokeColor,
    rect:element.getBoundingClientRect(),
    width:element.getBoundingClientRect().width,
    height:element.getBoundingClientRect().height
  }};
}}
addEventListener('load',async()=>{{
  await document.fonts.ready;
  const stage=document.getElementById('stage').getBoundingClientRect();
  const layers={{}};
  for(const name of ['top1','top2','top3','bottom1','bottom2']){{
    layers[name]=styleOf(name);
  }}
  const report={{
    font_loaded:document.fonts.check('900 118px "NotoSC"'),
    layers:layers,
    stage:{{left:stage.left,right:stage.right,top:stage.top,bottom:stage.bottom}}
  }};
  document.getElementById('report').textContent=JSON.stringify(report);
  document.documentElement.dataset.ready='1';
}});
</script></body></html>"""


def validate_report(report: dict) -> None:
    if report.get("font_loaded") is not True:
        raise RuntimeError("v07 preview did not load NotoSC 900")
    layers = report["layers"]
    stage = report["stage"]
    for name, expected in EXPECTED_LAYERS.items():
        item = layers[name]
        if item["display"] == "none" or item["visibility"] == "hidden":
            raise RuntimeError(f"v07 {name} is hidden at frame zero")
        if item["opacity"] != "1":
            raise RuntimeError(f"v07 {name} is not fully opaque at frame zero")
        if item["fontSize"] != expected["font-size"]:
            raise RuntimeError(
                f"v07 {name} font size is {item['fontSize']}, "
                f"expected {expected['font-size']}"
            )
        if item["color"] != expected["color"]:
            raise RuntimeError(
                f"v07 {name} color is {item['color']}, "
                f"expected {expected['color']}"
            )
        if item["strokeColor"] != EXPECTED_STROKE_COLORS[name]:
            raise RuntimeError(
                f"v07 {name} stroke color is {item['strokeColor']}, "
                f"expected {EXPECTED_STROKE_COLORS[name]}"
            )
        rect = item["rect"]
        if (
            rect["left"] < stage["left"] + SIDE_SAFE_PX - 0.5
            or rect["right"] > stage["right"] - SIDE_SAFE_PX + 0.5
        ):
            raise RuntimeError(f"v07 {name} crosses the side safe area")
        if (
            rect["top"] < stage["top"] - 0.5
            or rect["bottom"] > stage["bottom"] + 0.5
        ):
            raise RuntimeError(f"v07 {name} is clipped outside the canvas")
        if item["width"] <= 0 or item["height"] <= 0:
            raise RuntimeError(f"v07 {name} has no visible box")
    top_order = ["top1", "top2", "top3", "bottom1"]
    for previous, current in zip(top_order, top_order[1:]):
        if layers[previous]["rect"]["bottom"] > layers[current]["rect"]["top"] + 0.5:
            raise RuntimeError(f"v07 {previous} overlaps {current}")
    if layers["bottom1"]["rect"]["bottom"] > layers["bottom2"]["rect"]["top"] + 0.5:
        raise RuntimeError("v07 bottom1 overlaps the bottom CTA")
    cta_limit = (
        CANVAS_HEIGHT * (100 - CTA_SAFE_BOTTOM_PERCENT) / 100
    )
    if layers["bottom2"]["rect"]["bottom"] > cta_limit + 0.5:
        raise RuntimeError("v07 bottom CTA crosses the bottom safe area")


def browser_report(browser: Path, font: Path, document: str) -> dict:
    if not browser.is_file():
        raise RuntimeError("HyperFrames browser is unavailable")
    if not font.is_file():
        raise RuntimeError("NotoSC font is unavailable")
    with tempfile.TemporaryDirectory(prefix="hq-v07-preview-") as temporary:
        path = Path(temporary) / "preview.html"
        path.write_text(document, encoding="utf-8")
        completed = subprocess.run(
            [
                str(browser), "--headless=new", "--no-sandbox",
                "--disable-gpu", "--allow-file-access-from-files",
                "--virtual-time-budget=5000", "--dump-dom", path.as_uri(),
            ],
            check=False, capture_output=True, text=True, encoding="utf-8",
            timeout=25,
        )
        if completed.returncode:
            raise RuntimeError("v07 preview browser check failed")
        match = re.search(r'<pre id="report">(.*?)</pre>', completed.stdout, re.S)
        if not match:
            raise RuntimeError("v07 preview browser report is missing")
        report = json.loads(html.unescape(match.group(1)))
    validate_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-root", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    index_html = (args.pack_root / "index.html").read_text(encoding="utf-8")
    preview_js = (args.pack_root / "preview-data.js").read_text(encoding="utf-8")
    rules = _extract_v07_css(index_html)
    row = _extract_v07_row(preview_js)
    font = args.pack_root.parents[1] / "fonts" / "NotoSansSC-Variable.ttf"
    report = browser_report(
        args.browser, font,
        _document(rules, row, font.resolve().as_uri()),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
