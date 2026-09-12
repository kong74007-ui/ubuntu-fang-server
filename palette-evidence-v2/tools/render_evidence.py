#!/usr/bin/env python3
"""用同一份生产链路 manifest 变量，分别渲染 before(v1) / after(v2) 两套包。

用法（在 fang 上）:
  python3 render_evidence.py /tmp/pipeline_manifest.json /tmp/ev /tmp/ev/out
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CLI = "/usr/local/bin/hyperframes"
BROWSER = "/usr/bin/google-chrome-stable"
PACK_NAME = "reference-typography-17"
VIDEOS = {
    "videoA": "assets/library/default-a.mp4",
    "videoB": "assets/library/default-b.mp4",
    "videoC": "assets/library/default-c.mp4",
}
PHASES = {"before": "pack_before", "after": "pack_after"}
# 关键帧取点（总时长比例，before/after 完全一致）
KEYFRAME_RATIOS = (0.15, 0.5, 0.85)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def main() -> int:
    manifest_path, root, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    env = dict(os.environ)
    env["HYPERFRAMES_BROWSER_PATH"] = BROWSER
    rendered, failed = [], []

    for tid, entry in manifest["templates"].items():
        display = entry["display_text"]
        variables = {
            "variant": entry["variant"],
            "duration": float(entry["duration"]),
            **VIDEOS,
            "bgm": "",
            **{key: display.get(key, "") for key in
               ("top1", "top2", "top3", "bottom1", "bottom2")},
        }
        var_file = out_dir / f"{entry['variant']}.variables.json"
        var_file.write_text(json.dumps(variables, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        for phase, pack_dir in PHASES.items():
            target = out_dir / f"{entry['variant']}-{phase}.mp4"
            pack = root / pack_dir / PACK_NAME
            result = subprocess.run(
                [CLI, "render", str(pack), "--variables-file", str(var_file),
                 "--format", "mp4", "-q", "draft", "-w", "1", "-o", str(target)],
                capture_output=True, text=True, env=env, timeout=900,
            )
            if target.is_file() and target.stat().st_size > 0:
                duration = probe_duration(target)
                for index, ratio in enumerate(KEYFRAME_RATIOS):
                    frame = frames_dir / f"{entry['variant']}-{phase}-k{index}.png"
                    subprocess.run(
                        ["ffmpeg", "-v", "error", "-y", "-ss", f"{duration * ratio:.3f}",
                         "-i", str(target), "-frames:v", "1", str(frame)],
                        check=False,
                    )
                rendered.append({"template": tid, "phase": phase,
                                 "file": target.name, "duration": duration,
                                 "size": target.stat().st_size,
                                 "keyframes": [f.name for f in sorted(frames_dir.glob(f"{entry['variant']}-{phase}-k*.png"))]})
                print(f"[ok]   {tid:34s} {phase:6s} {duration:5.2f}s {target.stat().st_size/1024:7.1f} KB")
            else:
                failed.append({"template": tid, "phase": phase,
                               "stderr": (result.stderr or result.stdout or "")[-400:]})
                print(f"[fail] {tid:34s} {phase}")

    report = {"manifest": str(manifest_path), "rendered": rendered, "failed": failed}
    (out_dir / "render-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n渲染成功 {len(rendered)} / 失败 {len(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
