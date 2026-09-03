#!/usr/bin/env python3
"""Verify the v04 60px preview with the production font in real Chrome."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path


BOTTOM1 = "实体老板｜自媒体博主｜行业精英"
BOTTOM2_LINES = ["交友破圈｜信息差｜", "自媒体｜AI智能体"]


def validate_report(report: dict) -> None:
    if report.get("font_loaded") is not True:
        raise RuntimeError("v04 preview did not load NotoSC 900")
    if report.get("font_size") != "60px":
        raise RuntimeError("v04 preview Bottom2 is not 60px")
    if report.get("lines") != BOTTOM2_LINES:
        raise RuntimeError("v04 preview changed its authored phrase lines")
    if any(len(re.sub(r"[\s｜|]", "", line)) <= 1 for line in report["lines"]):
        raise RuntimeError("v04 preview contains an orphan character line")
    if report.get("clipped") is not False:
        raise RuntimeError("v04 preview is clipped outside its canvas")
    if report.get("overlap") is not False:
        raise RuntimeError("v04 preview Bottom1 and Bottom2 overlap")


def _document(font_uri: str) -> str:
    bottom2 = "\n".join(BOTTOM2_LINES)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@font-face{{font-family:"NotoSC";src:url({json.dumps(font_uri)});font-weight:100 900}}
*{{box-sizing:border-box}}html,body{{margin:0;width:1080px;height:1920px;overflow:hidden}}
#stage{{position:relative;width:1080px;height:1920px;background:#555}}
.bottom{{position:absolute;left:0;bottom:15%;width:100%;padding:0 42px;text-align:center}}
.bottom1,.bottom2{{max-width:996px;margin-left:auto;margin-right:auto;font-family:"NotoSC";font-weight:900;line-height:1.13;letter-spacing:.01em;white-space:pre-line}}
.bottom1{{font-size:58px;color:#ffd923;-webkit-text-stroke:8px #111}}
.bottom2{{margin-top:16px;font-size:60px;color:#fff;-webkit-text-stroke:8px #111}}
#report{{display:none}}
</style></head><body><div id="stage"><div class="bottom">
<div id="bottom1" class="bottom1">{BOTTOM1}</div>
<div id="bottom2" class="bottom2">{bottom2}</div>
</div></div><pre id="report"></pre><script>
function renderedLines(element){{
  const node=element.firstChild, groups=[];
  for(let index=0;index<node.data.length;index+=1){{
    const char=node.data[index];
    if(char==='\\n')continue;
    const range=document.createRange();
    range.setStart(node,index);range.setEnd(node,index+1);
    const rect=range.getBoundingClientRect();
    let group=groups.find(item=>Math.abs(item.top-rect.top)<0.75);
    if(!group){{group={{top:rect.top,text:''}};groups.push(group)}}
    group.text+=char;
  }}
  return groups.sort((a,b)=>a.top-b.top).map(item=>item.text);
}}
addEventListener('load',async()=>{{
  await document.fonts.ready;
  const stage=document.getElementById('stage').getBoundingClientRect();
  const bottom1=document.getElementById('bottom1').getBoundingClientRect();
  const element=document.getElementById('bottom2');
  const bottom2=element.getBoundingClientRect();
  const report={{
    font_loaded:document.fonts.check('900 60px "NotoSC"'),
    font_size:getComputedStyle(element).fontSize,
    lines:renderedLines(element),
    clipped:bottom2.left<stage.left-0.5||bottom2.right>stage.right+0.5||bottom2.top<stage.top-0.5||bottom2.bottom>stage.bottom+0.5||element.scrollWidth>element.clientWidth+1,
    overlap:bottom1.bottom>bottom2.top+0.5
  }};
  document.getElementById('report').textContent=JSON.stringify(report);
  document.documentElement.dataset.ready='1';
}});
</script></body></html>"""


def browser_report(browser: Path, font: Path) -> dict:
    if not browser.is_file():
        raise RuntimeError("HyperFrames browser is unavailable")
    if not font.is_file():
        raise RuntimeError("NotoSC font is unavailable")
    with tempfile.TemporaryDirectory(prefix="hq-v04-preview-") as temporary:
        document = Path(temporary) / "preview.html"
        document.write_text(_document(font.resolve().as_uri()), encoding="utf-8")
        completed = subprocess.run(
            [
                str(browser), "--headless=new", "--no-sandbox",
                "--disable-gpu", "--allow-file-access-from-files",
                "--virtual-time-budget=5000", "--dump-dom", document.as_uri(),
            ],
            check=False, capture_output=True, text=True, encoding="utf-8",
            timeout=20,
        )
        if completed.returncode:
            raise RuntimeError("v04 preview browser check failed")
        match = re.search(r'<pre id="report">(.*?)</pre>', completed.stdout, re.S)
        if not match:
            raise RuntimeError("v04 preview browser report is missing")
        report = json.loads(html.unescape(match.group(1)))
    validate_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-root", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    preview = (args.pack_root / "preview-data.js").read_text(encoding="utf-8")
    expected = '"bottom2": "交友破圈｜信息差｜\\n自媒体｜AI智能体"'
    if expected not in preview:
        raise RuntimeError("v04 preview phrase boundary is missing")
    report = browser_report(
        args.browser,
        args.pack_root.parents[1] / "fonts" / "NotoSansSC-Variable.ttf",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
