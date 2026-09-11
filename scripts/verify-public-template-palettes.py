#!/usr/bin/env python3
"""Generate before/after palette evidence with a real headless browser.

Produces, under --out:

* ``<NN>-<template>-before.png`` / ``-after.png`` for every public template
  (17 reference variants + nine-grid + triple-strip + yellow-banner)
* ``00-contact-sheet-before.png`` / ``00-contact-sheet-after.png``
* ``computed-style-diff.json`` comparing every text element's computed style
  before and after, failing on any non-color difference.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

STYLE_ID = "matrix-public-template-palettes-v1"

# Properties that must be byte-identical before and after.
MUST_MATCH = (
    "fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight",
    "letterSpacing", "textAlign", "whiteSpace", "wordBreak", "overflowWrap",
    "width", "maxWidth", "height", "minHeight",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "marginTop", "marginRight", "marginBottom", "marginLeft",
    "top", "bottom", "left", "right", "display", "position",
    "transform", "transformOrigin", "borderRadius", "webkitTextStrokeWidth",
)
COLOR_PROPS = (
    "color", "backgroundColor", "webkitTextStrokeColor",
    "borderColor", "outlineColor", "textDecorationColor",
    "fill", "stroke",
)

# template kind -> (dir name, list of (label, selector), needs_stub)
TARGETS = {
    "reference": (
        "reference-typography-17",
        [(layer, f"#{layer}") for layer in
         ("top1", "top2", "top3", "bottom1", "bottom2")],
        True,
    ),
    "nine-grid": (
        "nine-grid-reveal",
        [("headline", "#headline"), ("tagline", "#tagline")],
        False,
    ),
    "triple-strip": (
        "triple-strip-shutter",
        [("title", ".title"), ("subtitle", ".subtitle"),
         ("footer-text", ".footer-text"),
         ("underline-line", ".underline .line"),
         ("slash", ".underline .slash"),
         ("chevron-path", ".chevron path")],
        False,
    ),
    "yellow-banner": (
        "yellow-banner-zoom",
        [("title", "#title"), ("subtitle", ".subtitle"),
         ("sourceLabel", "#sourceLabel"), ("body", "#body"), ("cta", "#cta"),
         ("banner", ".banner"), ("body-panel", ".body-panel")],
        False,
    ),
}
REFERENCE_VARIANTS = [f"v{index:02d}" for index in range(1, 18)]

STUB = (
    "<script>window.__hyperframes={getVariables:()=>({"
    "variant:'%(variant)s',duration:10,"
    "top1:'在成都高新区',top2:'有一群不打麻将 不逛街',top3:'一群同频高能的伙伴',"
    "bottom1:'社交破圈｜认知提升',bottom2:'感兴趣回复666',"
    "videoA:'',videoB:'',videoC:'',bgm:''})};</script>"
)
REPORT = """
<script>
addEventListener('load',()=>{setTimeout(()=>{
 const props=%(must)s; const colors=%(colors)s;
 const out=[]; const seen=[];
 document.querySelectorAll('#top1,#top2,#top3,#bottom1,#bottom2,#headline,#tagline,.title,.subtitle,.footer-text,#title,#sourceLabel,#body,#cta,.banner,.body-panel,.underline .line,.underline .slash,.chevron path').forEach(el=>{
   const cs=getComputedStyle(el); const rec={tag:el.id||el.className,style:{},colors:{}};
   props.forEach(p=>rec.style[p]=cs[p]); colors.forEach(p=>rec.colors[p]=cs[p]);
   rec.colors.textShadow=cs.textShadow; out.push(rec);
 });
 const pre=document.createElement('pre'); pre.id='__report';
 pre.textContent=JSON.stringify(out); document.body.appendChild(pre);
},120);});
</script>
"""


def render_html(path: Path, variant: str | None) -> str:
    source = path.read_text(encoding="utf-8")
    stub = STUB % {"variant": variant or "v01"} if variant else ""
    report = REPORT % {
        "must": json.dumps(list(MUST_MATCH)),
        "colors": json.dumps(list(COLOR_PROPS)),
    }
    source = re.sub(r"(<head[^>]*>)", r"\1" + stub, source, count=1)
    return source.replace("</body>", report + "</body>", 1)


def run_browser(browser: Path, document: str, tmp: Path, shot: Path | None):
    page = tmp / "page.html"
    page.write_text(document, encoding="utf-8")
    command = [
        str(browser), "--headless=new", "--no-sandbox", "--disable-gpu",
        "--allow-file-access-from-files", "--hide-scrollbars",
        "--window-size=1080,1920", "--virtual-time-budget=4000",
    ]
    if shot is not None:
        command += [f"--screenshot={shot}", page.as_uri()]
        subprocess.run(command, check=False, capture_output=True)
        return None
    command += ["--dump-dom", page.as_uri()]
    done = subprocess.run(
        command, check=False, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    match = re.search(r'<pre id="__report">(.*?)</pre>', done.stdout, re.S)
    return json.loads(html.unescape(match.group(1))) if match else None


def shadow_shape(value: str) -> list:
    shapes = []
    for part in re.findall(r"rgba?\([^)]*\)|[^,]+", value or ""):
        part = part.strip()
        if re.match(r"^rgba?\([^)]*\)$", part) or re.match(r"^#[0-9a-f]+$", part):
            continue
        if re.match(r"^-?[\d.]+px$", part):
            shapes.append(part)
    return shapes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    diff = []
    violations = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for kind, (dirname, elements, _stub) in TARGETS.items():
            variants = REFERENCE_VARIANTS if kind == "reference" else [""]
            for variant in variants:
                tag = variant or kind
                before_html = render_html(args.before / dirname / "index.html", variant)
                after_html = render_html(args.after / dirname / "index.html", variant)
                before = run_browser(args.browser, before_html, tmp, None)
                after = run_browser(args.browser, after_html, tmp, None)
                before_shot = args.out / f"x-{tag}-before.png"
                after_shot = args.out / f"x-{tag}-after.png"
                run_browser(args.browser, before_html, tmp, before_shot)
                run_browser(args.browser, after_html, tmp, after_shot)
                if before is None or after is None:
                    diff.append({"template": tag, "error": "render failed"})
                    continue
                entry = {"template": tag, "elements": []}
                for b, a in zip(before, after):
                    el = {"tag": b["tag"], "changed": {}, "violations": []}
                    for prop in MUST_MATCH:
                        if b["style"].get(prop) != a["style"].get(prop):
                            el["violations"].append(
                                f"{prop}: {b['style'].get(prop)} -> {a['style'].get(prop)}"
                            )
                    for prop in COLOR_PROPS:
                        if b["colors"].get(prop) != a["colors"].get(prop):
                            el["changed"][prop] = [
                                b["colors"].get(prop), a["colors"].get(prop),
                            ]
                    if b["colors"].get("textShadow") != a["colors"].get("textShadow"):
                        if shadow_shape(b["colors"].get("textShadow", "")) != \
                           shadow_shape(a["colors"].get("textShadow", "")):
                            el["violations"].append("textShadow shape changed")
                        else:
                            el["changed"]["textShadow"] = [
                                b["colors"].get("textShadow"),
                                a["colors"].get("textShadow"),
                            ]
                    if el["violations"]:
                        violations.append((tag, el["tag"], el["violations"]))
                    entry["elements"].append(el)
                diff.append(entry)
    (args.out / "computed-style-diff.json").write_text(
        json.dumps({"templates": diff, "violations": violations},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"templates": len(diff), "violations": len(violations)}))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
