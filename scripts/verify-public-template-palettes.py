#!/usr/bin/env python3
"""Independent headless-Chrome gate for the public-template palettes.

It renders the real template entry points twice (before / after the palette
overlay), extracts every target element's computed style, and proves that only
colour properties changed. It also writes stable, production-numbered
screenshots and two 5x4 contact sheets.

Outputs, under ``--out``:

* ``01-ref-01-chengdu-green-brush-before.png`` / ``-after.png`` (and 02..20)
* ``00-contact-sheet-before.png`` / ``00-contact-sheet-after.png``
* ``computed-style-diff.json``

Hard rules enforced here (not only in unit tests):

* the shadow parser keeps layer count, order, inset, x, y, blur, spread and
  only ignores the RGB part; alpha is compared explicitly by policy;
* before/after element lists must have identical length and identity;
* a missing report, a non-zero browser exit or invalid JSON exits non-zero;
* every screenshot must exist, be non-empty and be exactly 1080x1920.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- #
# Structured shadow parsing (unit-tested in tests/test_matrix_template_palettes)
# --------------------------------------------------------------------------- #
COLOR_TOKEN_RE = re.compile(r"rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}")
LENGTH_RE = re.compile(r"(-?[0-9]+(?:\.[0-9]+)?)px")
ALPHA_RE = re.compile(r"rgba\([^)]*,\s*([0-9]*\.?[0-9]+)\s*\)|hsla\([^)]*,\s*([0-9]*\.?[0-9]+)\s*\)")


def split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def parse_shadow_layer(raw: str) -> dict:
    text = raw.strip()
    colour = COLOR_TOKEN_RE.search(text)
    lengths = [float(match.group(1)) for match in LENGTH_RE.finditer(text)]
    return {
        "inset": bool(re.search(r"\binset\b", text)),
        "color": colour.group(0) if colour else "",
        "x": lengths[0] if len(lengths) > 0 else 0.0,
        "y": lengths[1] if len(lengths) > 1 else 0.0,
        "blur": lengths[2] if len(lengths) > 2 else 0.0,
        "spread": lengths[3] if len(lengths) > 3 else 0.0,
    }


def parse_shadow(value: str) -> list[dict]:
    """Parse a computed text-shadow / box-shadow into ordered layers."""
    if not value or value.strip().lower() in {"none", "initial", "inherit"}:
        return []
    return [parse_shadow_layer(part) for part in split_top_level(value)]


def shadow_geometry(layers: list[dict]) -> list[tuple]:
    return [
        (layer["inset"], layer["x"], layer["y"], layer["blur"], layer["spread"])
        for layer in layers
    ]


def shadow_alpha(value: str) -> list[float | None]:
    alphas: list[float | None] = []
    for part in split_top_level(value or ""):
        match = ALPHA_RE.search(part)
        if match:
            alphas.append(float(match.group(1) or match.group(2)))
        elif COLOR_TOKEN_RE.search(part):
            alphas.append(1.0)
        else:
            alphas.append(None)
    return alphas


def shadow_geometry_equal(before: str, after: str) -> tuple[bool, str]:
    left, right = parse_shadow(before), parse_shadow(after)
    if len(left) != len(right):
        return False, f"shadow layer count {len(left)} -> {len(right)}"
    if shadow_geometry(left) != shadow_geometry(right):
        return False, "shadow geometry changed"
    if shadow_alpha(before) != shadow_alpha(after):
        return False, "shadow alpha changed"
    return True, ""


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
MUST_MATCH = (
    "fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight",
    "letterSpacing", "textAlign", "whiteSpace", "wordBreak", "overflowWrap",
    "width", "maxWidth", "height", "minHeight",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "marginTop", "marginRight", "marginBottom", "marginLeft",
    "top", "bottom", "left", "right", "display", "position",
    "transform", "transformOrigin", "borderRadius", "webkitTextStrokeWidth",
    "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "borderTopStyle", "outlineWidth",
)
COLOR_PROPS = (
    "color", "backgroundColor", "webkitTextStrokeColor",
    "borderTopColor", "borderRightColor", "borderBottomColor", "borderLeftColor",
    "outlineColor", "textDecorationColor", "fill", "stroke",
)
SHADOW_PROPS = ("textShadow", "boxShadow")

REFERENCE_VARIANTS = [f"v{index:02d}" for index in range(1, 18)]
REFERENCE_IDS = [
    "ref-01-chengdu-green-brush", "ref-02-shenzhen-ai-orange",
    "ref-03-zhengzhou-blue-banner", "ref-04-foshan-yellow-strip",
    "ref-05-changsha-white-red", "ref-06-guangzhou-yellow-button",
    "ref-07-shenzhen-red-growth", "ref-08-puyang-yellow-white",
    "ref-09-urumqi-soft-brush", "ref-10-shenzhen-sisters",
    "ref-11-nansha-clean", "ref-12-guangzhou-brush",
    "ref-13-shenzhen-green-location", "ref-14-karamay-green",
    "ref-15-tianjin-monochrome", "ref-16-shenzhen-opc",
    "ref-17-shenzhen-yellow-red",
]
TARGETS: list[dict] = []
for index, (variant, template_id) in enumerate(
    zip(REFERENCE_VARIANTS, REFERENCE_IDS), start=1
):
    TARGETS.append({
        "index": index, "template_id": template_id, "kind": "reference",
        "variant": variant, "dir": "reference-typography-17", "stub": True,
        "selectors": [f"#{layer}" for layer in
                      ("top1", "top2", "top3", "bottom1", "bottom2")],
    })
TARGETS.append({
    "index": 18, "template_id": "nine-grid-reveal", "kind": "nine-grid",
    "variant": "", "dir": "nine-grid-reveal", "stub": False,
    "selectors": ["#headline", "#tagline"],
})
TARGETS.append({
    "index": 19, "template_id": "triple-strip-shutter", "kind": "triple-strip",
    "variant": "", "dir": "triple-strip-shutter", "stub": False,
    "selectors": [".title", ".subtitle", ".footer-text",
                  ".underline .line", ".underline .slash", ".chevron path"],
})
TARGETS.append({
    "index": 20, "template_id": "yellow-banner-zoom", "kind": "yellow-banner",
    "variant": "", "dir": "yellow-banner-zoom", "stub": False,
    "selectors": ["#title", ".subtitle", "#sourceLabel", "#body", "#cta",
                  ".banner", ".body-panel"],
})

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
 const must=%(must)s, colors=%(colors)s, shadows=%(shadows)s, selectors=%(selectors)s;
 const out=[];
 selectors.forEach(sel=>{
   document.querySelectorAll(sel).forEach((el,idx)=>{
     const cs=getComputedStyle(el);
     const rec={selector:sel, index:idx, style:{}, colors:{}, shadows:{}};
     must.forEach(p=>rec.style[p]=cs[p]);
     colors.forEach(p=>rec.colors[p]=cs[p]);
     shadows.forEach(p=>rec.shadows[p]=cs[p]);
     out.push(rec);
   });
 });
 const pre=document.createElement('pre'); pre.id='__report';
 pre.textContent=JSON.stringify(out); document.body.appendChild(pre);
},150);});
</script>
"""


def render_html(path: Path, target: dict) -> str:
    source = path.read_text(encoding="utf-8")
    stub = STUB % {"variant": target["variant"]} if target["stub"] else ""
    report = REPORT % {
        "must": json.dumps(list(MUST_MATCH)),
        "colors": json.dumps(list(COLOR_PROPS)),
        "shadows": json.dumps(list(SHADOW_PROPS)),
        "selectors": json.dumps(target["selectors"]),
    }
    source = re.sub(r"(<head[^>]*>)", r"\1" + stub, source, count=1)
    return source.replace("</body>", report + "</body>", 1)


def run_browser(browser: Path, document: str, tmp: Path):
    page = tmp / "page.html"
    page.write_text(document, encoding="utf-8")
    done = subprocess.run(
        [str(browser), "--headless=new", "--no-sandbox", "--disable-gpu",
         "--allow-file-access-from-files", "--hide-scrollbars",
         "--window-size=1080,1920", "--virtual-time-budget=4000",
         "--dump-dom", page.as_uri()],
        check=False, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(f"browser exited {done.returncode}")
    match = re.search(r'<pre id="__report">(.*?)</pre>', done.stdout, re.S)
    if not match:
        raise RuntimeError("browser report is missing")
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise RuntimeError("browser report is not JSON") from exc


def screenshot(browser: Path, document: str, tmp: Path, shot: Path) -> None:
    page = tmp / "page.html"
    page.write_text(document, encoding="utf-8")
    done = subprocess.run(
        [str(browser), "--headless=new", "--no-sandbox", "--disable-gpu",
         "--allow-file-access-from-files", "--hide-scrollbars",
         "--window-size=1080,1920", "--virtual-time-budget=4000",
         f"--screenshot={shot}", page.as_uri()],
        check=False, capture_output=True, timeout=60,
    )
    if done.returncode != 0:
        raise RuntimeError(f"screenshot browser exited {done.returncode}")
    if not shot.is_file() or shot.stat().st_size <= 0:
        raise RuntimeError(f"screenshot missing or empty: {shot.name}")
    from PIL import Image
    with Image.open(shot) as image:
        if image.size != (1080, 1920):
            raise RuntimeError(f"screenshot {shot.name} is {image.size}")


def compare(before: list[dict], after: list[dict], label: str) -> list[str]:
    problems: list[str] = []
    if len(before) != len(after):
        return [f"{label}: element count {len(before)} -> {len(after)}"]
    for left, right in zip(before, after):
        identity = f"{left['selector']}#{left['index']}"
        if (left["selector"], left["index"]) != (right["selector"], right["index"]):
            problems.append(
                f"{label}: element identity {identity} -> "
                f"{right['selector']}#{right['index']}"
            )
            continue
        for prop in MUST_MATCH:
            if left["style"].get(prop) != right["style"].get(prop):
                problems.append(
                    f"{label}:{identity}: {prop} "
                    f"{left['style'].get(prop)} -> {right['style'].get(prop)}"
                )
        for prop in SHADOW_PROPS:
            ok, why = shadow_geometry_equal(
                left["shadows"].get(prop, ""), right["shadows"].get(prop, "")
            )
            if not ok:
                problems.append(f"{label}:{identity}: {prop} {why}")
    return problems


def parse_only(value: str) -> list[int]:
    if not value.strip():
        return []
    indices: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            indices.extend(range(int(start), int(end) + 1))
        elif part:
            indices.append(int(part))
    return indices


def valid_shot(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    from PIL import Image
    try:
        with Image.open(path) as image:
            return image.size == (1080, 1920)
    except Exception:
        return False


def shot_paths(out: Path, target: dict) -> tuple[Path, Path]:
    stem = f"{target['index']:02d}-{target['template_id']}"
    return out / f"{stem}-before.png", out / f"{stem}-after.png"


def process_target(target, before_root, after_root, browser, out, tmp) -> dict:
    before_html = render_html(before_root / target["dir"] / "index.html", target)
    after_html = render_html(after_root / target["dir"] / "index.html", target)
    before = run_browser(browser, before_html, tmp)
    after = run_browser(browser, after_html, tmp)
    violations = compare(before, after, target["template_id"])
    before_shot, after_shot = shot_paths(out, target)
    if not valid_shot(before_shot):
        screenshot(browser, before_html, tmp, before_shot)
    if not valid_shot(after_shot):
        screenshot(browser, after_html, tmp, after_shot)
    return {
        "index": target["index"], "template": target["template_id"],
        "elements": len(before), "violations": violations,
        "changes": collect_changes(before, after, target["template_id"]),
    }


def collect_changes(before: list[dict], after: list[dict], label: str) -> list[dict]:
    changes: list[dict] = []
    if len(before) != len(after):
        return changes
    for left, right in zip(before, after):
        identity = f"{left['selector']}#{left['index']}"
        entry = {"element": f"{label}:{identity}", "colors": {}, "shadows": {}}
        for prop in COLOR_PROPS:
            if left["colors"].get(prop) != right["colors"].get(prop):
                entry["colors"][prop] = [
                    left["colors"].get(prop), right["colors"].get(prop),
                ]
        for prop in SHADOW_PROPS:
            if left["shadows"].get(prop) != right["shadows"].get(prop):
                entry["shadows"][prop] = [
                    left["shadows"].get(prop), right["shadows"].get(prop),
                ]
        if entry["colors"] or entry["shadows"]:
            changes.append(entry)
    return changes


def contact_sheets(out: Path) -> None:
    from PIL import Image, ImageDraw
    for phase in ("before", "after"):
        tiles = []
        for target in TARGETS:
            path = shot_paths(out, target)[0 if phase == "before" else 1]
            image = Image.open(path).convert("RGB").resize((216, 384))
            draw = ImageDraw.Draw(image)
            draw.rectangle([0, 0, 60, 30], fill=(0, 0, 0))
            draw.text((6, 8), f"{target['index']:02d}", fill=(255, 255, 255))
            tiles.append(image)
        sheet = Image.new("RGB", (216 * 5, 384 * 4), (24, 24, 24))
        for position, image in enumerate(tiles):
            sheet.paste(image, ((position % 5) * 216, (position // 5) * 384))
        sheet.save(out / f"00-contact-sheet-{phase}.png")


def merge_outputs(out: Path) -> int:
    targets = {target["index"]: target for target in TARGETS}
    results: dict[int, dict] = {}
    for index in sorted(targets):
        path = out / f"result-{index:02d}.json"
        if not path.is_file():
            print(f"缺少模板 {index:02d} 的批次结果", file=sys.stderr)
            return 1
        results[index] = json.loads(path.read_text(encoding="utf-8"))
    if sorted(results) != list(range(1, 21)):
        print("批次覆盖不是恰好 01..20", file=sys.stderr)
        return 1
    violations: list[str] = []
    elements = []
    changes: list[dict] = []
    for index in sorted(results):
        record = results[index]
        if record.get("template") != targets[index]["template_id"]:
            violations.append(f"{index:02d}：模板身份不一致")
        if not isinstance(record.get("elements"), int) or record["elements"] <= 0:
            violations.append(f"{index:02d}：未采集到元素")
        violations += record.get("violations") or []
        changes += record.get("changes") or []
        elements.append({
            "template": targets[index]["template_id"],
            "elements": record.get("elements"),
        })
    for target in TARGETS:
        for path in shot_paths(out, target):
            if not valid_shot(path):
                violations.append(f"截图无效：{path.name}")
    if not violations:
        contact_sheets(out)
    (out / "computed-style-diff.json").write_text(
        json.dumps({"templates": 20, "elements": elements,
                    "changes": changes, "violations": violations},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"templates": 20, "violations": len(violations)}))
    if violations:
        for item in violations[:20]:
            print(item, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="e.g. 1-5 or 1,3,7")
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.merge:
        return merge_outputs(args.out)
    if not (args.before and args.after and args.browser):
        parser.error("除非 --merge，否则必须提供 --before/--after/--browser")
    targets = {target["index"]: target for target in TARGETS}
    indices = parse_only(args.only) or sorted(targets)
    unknown = [index for index in indices if index not in targets]
    if unknown:
        parser.error(f"未知模板编号：{unknown}")
    failures = 0
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for index in indices:
            target = targets[index]
            result_path = args.out / f"result-{index:02d}.json"
            before_shot, after_shot = shot_paths(args.out, target)
            if (result_path.is_file() and valid_shot(before_shot)
                    and valid_shot(after_shot)):
                cached = json.loads(result_path.read_text(encoding="utf-8"))
                if not cached.get("violations") and "changes" in cached:
                    print(f"复用 {index:02d} {target['template_id']}")
                    continue
            try:
                result = process_target(
                    target, args.before, args.after, args.browser, args.out, tmp,
                )
            except Exception as exc:  # 任一渲染失败即视为该批失败
                print(f"{index:02d} {target['template_id']} 渲染失败：{exc}",
                      file=sys.stderr)
                failures += 1
                continue
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            status = "存在违规" if result["violations"] else "通过"
            print(f"{index:02d} {target['template_id']} {status}")
            failures += 1 if result["violations"] else 0
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
