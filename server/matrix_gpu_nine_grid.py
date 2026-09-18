"""Experimental HLG GPU renderer. Not wired to production job admission.

Only the reviewed nine-grid HTML revision is accepted. This is intentionally
not a general HTML renderer and cannot be advertised as all-template support.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time


TEMPLATE_SHA256 = "817377c202f1bad4810285d40b3c77092bc9be8bf3c05f243b7d09431cb2ecdf"
REVEALS = (32, 42, 51, 23, 5, 60, 85, 78, 68)
SECTIONS = (("impact", 1, 1, 0), ("main1", 1, 92, 1 / 30),
            ("main2", 5, 81, 0), ("main3", 9, 90, 0))
SIGNAL = ("format=yuv420p10le:colorspace=bt2020nc:color_primaries=bt2020:"
          "color_trc=linear:range=tv:peak_detect=0:apply_dolbyvision=0:"
          "apply_filmgrain=0:inverse_tonemapping=0:tonemapping=clip:"
          "gamut_mode=clip:contrast_recovery=0")
TEXT_COLOR = SIGNAL.replace("color_trc=linear", "color_trc=arib-std-b67").replace(
    "format=yuv420p10le", "format=gbrap16le").replace(
    "colorspace=bt2020nc", "colorspace=gbr").replace("range=tv", "range=pc")
DOWNLOAD = "hwdownload,format=yuv420p10le,setparams=color_trc=arib-std-b67"
ENCODE = ("-c:v", "hevc_nvenc", "-preset", "p5", "-cq", "15", "-pix_fmt",
          "yuv420p10le", "-tag:v", "hvc1", "-color_primaries", "bt2020",
          "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", "-color_range",
          "tv", "-video_track_timescale", "90000")


def require_hlg(stream: dict) -> None:
    actual = tuple(stream.get(k) for k in
                   ("codec_name", "pix_fmt", "color_transfer", "color_primaries", "color_space"))
    if actual != ("hevc", "yuv420p10le", "arib-std-b67", "bt2020", "bt2020nc"):
        raise ValueError("GPU prototype requires verified HEVC ten-bit BT.2020 HLG inputs")


def local_asset(project: Path, relative: str) -> Path:
    path = (project / relative).resolve()
    if not path.is_relative_to(project.resolve()) or not path.is_file():
        raise ValueError("Missing or out-of-project asset")
    return path


def validate_project(project: Path) -> None:
    if hashlib.sha256(local_asset(project, "index.html").read_bytes()).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("Unreviewed template revision; timeline adapter must be revalidated")
    values = json.loads(local_asset(project, "variables.json").read_text(encoding="utf-8"))
    for slot in range(1, 10):
        expected = f"assets/input/video-{slot}.mp4"
        if values.get(f"grid{slot}") != expected:
            raise ValueError("Unvalidated grid source mapping")
        local_asset(project, expected)
    for name, slot in (("main1", 1), ("main2", 5), ("main3", 9)):
        if values.get(name) != values[f"grid{slot}"]:
            raise ValueError("Unvalidated full-screen source mapping")
    if any(not isinstance(values.get(k), str) or not values[k].strip()
           for k in ("top_text", "bottom_text")):
        raise ValueError("Missing text fields")
    local_asset(project, "assets/audio/reference-bgm.m4a")


def video_upload(index: int, label: str) -> str:
    # Pixels remain encoded HLG throughout spatial processing, as in the CPU
    # reference. Internal linear tags prevent an extra display/EOTF transform.
    return (f"[{index}:v]setpts=PTS-STARTPTS,sidedata=mode=delete,"
            f"setparams=color_trc=linear,hwupload[{label}]")


def text_upload(index: int) -> str:
    return (f"[{index}:v]format=rgba,setparams=range=pc:color_primaries=bt709:"
            "color_trc=iec61966-2-1:colorspace=gbr,hwupload,"
            f"libplacebo={TEXT_COLOR},setparams=color_trc=linear[text]")


def opening_graph() -> str:
    filters = ["[0:v]format=yuv420p10le,setparams=range=tv:color_primaries=bt2020:"
               "color_trc=linear:colorspace=bt2020nc,hwupload[black]"]
    filters.extend(video_upload(i, f"v{i}") for i in range(1, 10))
    filters.append(text_upload(10))
    reveal = "0"
    for idx in range(9, 0, -1):
        reveal = f"if(eq(idx,{idx}),{REVEALS[idx-1]},({reveal}))"
    x = f"if(between(idx,1,9),if(lt(ot*30+0.001,{reveal}),2000,mod(idx-1,3)*360),0)"
    y = "if(between(idx,1,9),floor((idx-1)/3)*640,0)"
    w = "if(between(idx,1,9),if(eq(mod(idx,3),0),360,357),1080)"
    h = "if(between(idx,1,9),if(gte(idx,7),640,637),1920)"
    inputs = "[black]" + "".join(f"[v{i}]" for i in range(1, 10)) + "[text]"
    filters.append(inputs + f"libplacebo=inputs=11:w=1080:h=1920:fps=30:{SIGNAL}:"
                   f"pos_x='{x}':pos_y='{y}':pos_w='{w}':pos_h='{h}':fit_mode=cover,"
                   + DOWNLOAD + "[out]")
    return ";\n".join(filters)


def main_graph(impact: bool = False) -> str:
    filters = [video_upload(0, "source"), text_upload(1)]
    source, crop = "[source]", ""
    if impact:
        rgb = SIGNAL.replace("format=yuv420p10le", "format=gbrap16le").replace(
            "colorspace=bt2020nc", "colorspace=gbr").replace("range=tv", "range=pc")
        # Blur the enlarged RGB layer before viewport clipping, avoiding a
        # colored/black border from blurring an already-cropped YUV image.
        filters.append("[source]libplacebo=w=1264:h=2246:" + rgb
                       + ",gblur_vulkan=sigma=10:size=61[scaled]")
        source = "[scaled]"
        crop = ":crop_w='if(eq(idx,0),1080,iw)':crop_h='if(eq(idx,0),1920,ih)'"
    filters.append(source + "[text]libplacebo=inputs=2:w=1080:h=1920:fps=30:"
                   + SIGNAL + crop + ":fit_mode=cover," + DOWNLOAD + "[out]")
    return ";\n".join(filters)


class Renderer:
    def __init__(self, project: Path, output: Path, *, browser: Path,
                 puppeteer_module: Path, ffmpeg="ffmpeg", ffprobe="ffprobe",
                 node="node", device="NVIDIA", timeout=300):
        self.project, self.output = project.resolve(), output.resolve()
        self.browser, self.puppeteer = browser.resolve(), puppeteer_module.resolve()
        self.ffmpeg, self.ffprobe, self.node, self.device = ffmpeg, ffprobe, node, device
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.deadline = time.monotonic() + timeout
        self.timings = {}

    def remaining(self):
        budget = self.deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("GPU render deadline exceeded")
        return budget

    @staticmethod
    def stop(process):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=15)

    def run(self, name, args):
        started = time.monotonic()
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                   else {"start_new_session": True})
        with (self.output / (name + ".log")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen(args, stdout=log, stderr=log, **options)
            try:
                code = process.wait(timeout=self.remaining())
            except BaseException:
                self.stop(process)
                raise
        if code:
            raise RuntimeError(f"GPU render failed at {name}; see local stage log")
        self.timings[name] = round(time.monotonic() - started, 3)

    def probe(self, path):
        return json.loads(subprocess.check_output([self.ffprobe, "-v", "error",
            "-show_streams", "-show_format", "-of", "json", str(path)],
            text=True, encoding="utf-8", timeout=min(15, self.remaining())))

    def base(self):
        return [self.ffmpeg, "-hide_banner", "-loglevel", "verbose", "-nostdin", "-y",
                "-init_hw_device", f"vulkan=gpu:{self.device}", "-filter_hw_device", "gpu"]

    def encode(self, name, inputs, graph, frames):
        graph_path = self.output / (name + ".ffgraph")
        graph_path.write_text(graph, encoding="utf-8")
        self.run(name, self.base() + inputs + ["-filter_complex_script", str(graph_path),
            "-map", "[out]", "-an", "-frames:v", str(frames), "-r", "30", *ENCODE,
            str(self.output / (name + ".mp4"))])

    def render(self):
        validate_project(self.project)
        for slot in range(1, 10):
            info = self.probe(self.project / f"assets/input/video-{slot}.mp4")
            require_hlg(next(s for s in info["streams"] if s["codec_type"] == "video"))
            if float(info["format"]["duration"]) < 3.2:
                raise ValueError("Prepared clips must cover the 3.2-second opening")
        # Never overwrite another run or publish a partial output as a result.
        self.output.mkdir(parents=True, exist_ok=False)
        capture = Path(__file__).resolve().parents[1] / "scripts/matrix_gpu_capture.cjs"
        self.run("text", [self.node, str(capture), str(self.project), str(self.output / "text.png"),
                          str(self.browser), str(self.puppeteer)])
        from PIL import Image
        with Image.open(self.output / "text.png") as im:
            if im.mode != "RGBA" or im.getchannel("A").getextrema() != (0, 255):
                raise ValueError("Expected transparent text overlay")
        inputs = ["-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=3.2"]
        for slot in range(1, 10):
            inputs += ["-i", str(self.project / f"assets/input/video-{slot}.mp4")]
        text = ["-loop", "1", "-framerate", "30", "-i", str(self.output / "text.png")]
        self.encode("opening", inputs + text, opening_graph(), 96)
        for name, slot, frames, start in SECTIONS:
            inputs = ["-ss", str(start), "-i", str(self.project / f"assets/input/video-{slot}.mp4")]
            self.encode(name, inputs + text, main_graph(name == "impact"), frames)
        concat = self.output / "concat.txt"
        concat.write_text("".join(f"file '{name}.mp4'\n" for name in
                                 ("opening", *(s[0] for s in SECTIONS))), encoding="utf-8")
        pending = self.output / "output.part.mp4"
        self.run("mux", [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat), "-i",
            str(self.project / "assets/audio/reference-bgm.m4a"), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-t", "12", "-movflags", "+faststart", str(pending)])
        info = self.probe(pending)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        require_hlg(video)
        if (video.get("width"), video.get("height"), int(video.get("nb_frames", 0))) != (1080, 1920, 360):
            raise ValueError("Invalid GPU output dimensions/frame count")
        if abs(float(info["format"]["duration"]) - 12) > .05:
            raise ValueError("Invalid GPU output duration")
        target = self.output / "output.mp4"
        pending.replace(target)
        report = {"template": "nine-grid-reveal", "scope": "experimental-hlg-only",
                  "compositor": "libplacebo-vulkan", "encoder": "hevc_nvenc",
                  "timings": self.timings, "probe": info}
        (self.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--puppeteer-module", type=Path, required=True)
    parser.add_argument("--device", default="NVIDIA")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    target = Renderer(args.project, args.output_dir, browser=args.browser,
        puppeteer_module=args.puppeteer_module, device=args.device, timeout=args.timeout).render()
    print(json.dumps({"output": str(target)}))


if __name__ == "__main__":
    main()
