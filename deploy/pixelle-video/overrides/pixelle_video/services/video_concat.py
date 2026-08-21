from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence


FINAL_CONCAT_TIMEOUT_SECONDS = 600
TARGET_FPS = 30


class ConcatCancelled(RuntimeError):
    pass


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
    cancel_event: threading.Event | None = None,
) -> str:
    if len(videos) < 2:
        raise ValueError("at least two videos are required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    if streams_are_copy_compatible(videos, cancel_event):
        return concat_copy_compatible_streams(
            videos,
            output,
            timeout_seconds,
            cancel_event,
        )

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

    run_cancellable_process(command, output, timeout_seconds, cancel_event)

    return output


def stream_signature(
    video: str,
    cancel_event: threading.Event | None = None,
) -> tuple[tuple[tuple[str, Any], ...], ...]:
    if cancel_event is not None and cancel_event.is_set():
        raise ConcatCancelled("Final video concatenation cancelled")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_data",
            "-of",
            "json",
            video,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    streams = json.loads(result.stdout).get("streams", [])
    keys_by_type = {
        "video": (
            "codec_name",
            "profile",
            "level",
            "width",
            "height",
            "pix_fmt",
            "r_frame_rate",
            "avg_frame_rate",
            "time_base",
            "extradata",
        ),
        "audio": (
            "codec_name",
            "profile",
            "sample_rate",
            "channels",
            "channel_layout",
            "time_base",
            "extradata",
        ),
    }
    signature = []
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type not in keys_by_type:
            continue
        values = [("codec_type", codec_type)]
        values.extend((key, stream.get(key)) for key in keys_by_type[codec_type])
        signature.append(tuple(values))
    if [dict(item)["codec_type"] for item in signature] != ["video", "audio"]:
        raise ValueError(f"Expected one video and one audio stream: {video}")
    return tuple(signature)


def streams_are_copy_compatible(
    videos: Sequence[str],
    cancel_event: threading.Event | None = None,
) -> bool:
    try:
        signatures = [stream_signature(video, cancel_event) for video in videos]
    except ConcatCancelled:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return bool(signatures) and all(signature == signatures[0] for signature in signatures[1:])


def concat_copy_compatible_streams(
    videos: Sequence[str],
    output: str,
    timeout_seconds: int,
    cancel_event: threading.Event | None = None,
) -> str:
    filelist_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as filelist:
            filelist_path = filelist.name
            for video in videos:
                path = str(Path(video).resolve()).replace("\\", "/")
                escaped = path.replace("'", "'\\''")
                filelist.write(f"file '{escaped}'\n")

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            filelist_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ]
        run_cancellable_process(command, output, timeout_seconds, cancel_event)
        return output
    finally:
        if filelist_path:
            Path(filelist_path).unlink(missing_ok=True)


def build_bgm_command(
    video: str,
    bgm: str,
    output: str,
    volume: float,
    loop: bool,
) -> list[str]:
    if volume < 0:
        raise ValueError("BGM volume cannot be negative")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats"]
    command.extend(("-i", video))
    if loop:
        command.extend(("-stream_loop", "-1"))
    command.extend(("-i", bgm))
    command.extend(
        (
            "-filter_complex",
            f"[1:a]volume={volume:.6g}[bgm];"
            "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            output,
        )
    )
    return command


def add_bgm_with_controlled_process(
    video: str,
    bgm: str,
    output: str,
    volume: float,
    loop: bool,
    timeout_seconds: int = FINAL_CONCAT_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> str:
    run_cancellable_process(
        build_bgm_command(video, bgm, output, volume, loop),
        output,
        timeout_seconds,
        cancel_event,
    )
    return output


def terminate_process_group(process: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace_seconds)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass

    if process.poll() is None:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=grace_seconds)


def run_cancellable_process(
    command: Sequence[str],
    output: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> None:
    output_path = Path(output)
    popen_options: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "text": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, stderr=stderr, **popen_options)
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.1):
                    terminate_process_group(process)
                    output_path.unlink(missing_ok=True)
                    raise ConcatCancelled("Final video concatenation cancelled")
                if time.monotonic() >= deadline:
                    terminate_process_group(process)
                    output_path.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Final video concatenation timed out after {timeout_seconds:g} seconds"
                    )
                if cancel_event is None:
                    time.sleep(0.1)

            if process.returncode:
                stderr.seek(0)
                detail = stderr.read().strip() or f"exit code {process.returncode}"
                output_path.unlink(missing_ok=True)
                raise RuntimeError(f"Final video concatenation failed: {detail}")
        except BaseException:
            if process.poll() is None:
                terminate_process_group(process)
            raise


async def concat_videos_cancellable(
    concat: Callable[..., str],
    *args,
    **kwargs,
) -> str:
    cancel_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(concat, *args, cancel_event=cancel_event, **kwargs)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await asyncio.shield(worker)
        except (ConcatCancelled, RuntimeError):
            pass
        output = kwargs.get("output")
        if output is None and len(args) >= 2:
            output = args[1]
        if output:
            Path(output).unlink(missing_ok=True)
        raise
