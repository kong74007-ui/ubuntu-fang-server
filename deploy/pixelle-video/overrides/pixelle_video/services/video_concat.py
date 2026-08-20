from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


FINAL_CONCAT_TIMEOUT_SECONDS = 600
TARGET_FPS = 30


def build_concat_filter(input_count: int) -> str:
    if input_count < 2:
        raise ValueError("at least two inputs are required for filtered concatenation")

    filters: list[str] = []
    streams: list[str] = []
    for index in range(input_count):
        filters.append(
            f"[{index}:v]fps={TARGET_FPS},settb=AVTB,setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[{index}:a]aresample=44100:async=1:first_pts=0,"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        streams.extend((f"[v{index}]", f"[a{index}]"))
    filters.append(
        f"{''.join(streams)}concat=n={input_count}:v=1:a=1[v][a]"
    )
    return ";".join(filters)


def concat_with_normalized_streams(
    videos: Sequence[str],
    output: str,
    timeout_seconds: int = FINAL_CONCAT_TIMEOUT_SECONDS,
) -> str:
    if len(videos) < 2:
        raise ValueError("at least two videos are required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats"]
    for video in videos:
        command.extend(("-i", video))
    command.extend(
        (
            "-filter_complex",
            build_concat_filter(len(videos)),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(TARGET_FPS),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-y",
            output,
        )
    )

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        Path(output).unlink(missing_ok=True)
        raise RuntimeError(
            f"Final video concatenation timed out after {timeout_seconds} seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        Path(output).unlink(missing_ok=True)
        detail = (exc.stderr or str(exc)).strip()
        raise RuntimeError(f"Final video concatenation failed: {detail}") from exc

    return output
