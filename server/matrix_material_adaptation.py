"""Opt-in, deterministic owned-media adaptation for the unified jobs API."""
import hashlib
import math

VERSION = "auto-v1"


def plan(items, scenes, inspect, *, safety=0.15):
    """Probe once, skip unreadable inputs, then fill every slot in source order."""
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("materials must contain 1-20 items")
    valid, skipped = [], []
    for index, item in enumerate(items):
        try:
            duration = inspect(item)
        except (ValueError, RuntimeError, OSError):
            skipped.append(index)
            continue
        if item.get("media_type") == "video" and (
                not math.isfinite(duration) or duration <= 0):
            skipped.append(index)
            continue
        valid.append((item, duration, index))
    if not valid:
        raise ValueError("MATERIAL_UNAVAILABLE: no decodable owned material")
    output = []
    for index, scene in enumerate(scenes):
        item, duration, original = valid[index % len(valid)]
        window = float(scene["clip_duration_seconds"])
        image = item["media_type"] == "image"
        room = max(0., duration - window - safety)
        requested = item.get("clip_start_seconds")
        if type(requested) in (int, float) and math.isfinite(requested) and 0 <= requested <= room:
            start = float(requested)
        else:
            seed = hashlib.sha256(f'{item["sha256"]}:{index}:{window}'.encode()).digest()
            start = round(room * int.from_bytes(seed[:8], "big") / (2**64-1), 3)
        mode = "image" if image else "trim"
        if not image and duration < window + safety:
            start = 0.
            mode = "freeze" if duration < 1.5 else "loop"
        output.append({
            "sha256": item["sha256"], "media_type": item["media_type"],
            "provider": "user", "scene_id": scene["scene_id"],
            "slot_index": index + 1, "slot_count": len(scenes),
            "clip_start_seconds": start, "clip_duration_seconds": window,
            "adaptation": {"version": VERSION, "mode": mode,
                "source_index": original, "source_duration": duration,
                "source_start": start, "reused": index >= len(valid),
                "skipped_indices": skipped},
        })
    return output


def prepare(service, item, source, destination, deadline_at):
    """Preserve source primaries/transfer; template code performs final crop."""
    import time
    mode = item["adaptation"]["mode"]
    # Valid video windows need no extra transcode.
    if mode == "trim":
        return source
    duration = float(item["clip_duration_seconds"]) + 0.3
    pixel_format, encoder = service._clip_color_encoding(
        service._source_color(source), gpu=bool(getattr(service, "gpu_runtime", None)))
    inputs = ["-loop", "1", "-framerate", "30"] if mode == "image" else (
        ["-stream_loop", "-1"] if mode == "loop" else [])
    filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2", "setsar=1", "fps=30"]
    if mode == "freeze":
        filters.append(f"tpad=stop_mode=clone:stop_duration={duration:.6f}")
    filters.append("format=" + pixel_format)
    destination.parent.mkdir(parents=True, exist_ok=True)
    code, _, _ = service._run_tracked_process([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *inputs, "-i", str(source), "-map", "0:v:0", "-an",
        "-vf", ",".join(filters), "-t", f"{duration:.6f}",
        *encoder, "-pix_fmt", pixel_format, "-map_metadata", "-1",
        "-movflags", "+faststart", str(destination),
    ], timeout_seconds=max(0.1, min(120., deadline_at-time.time())),
        timeout_error="material adaptation timed out")
    if code or not destination.is_file() or service._reference_video_duration(destination) < duration-.04:
        raise RuntimeError("material adaptation failed")
    item["media_type"] = "video"
    item["clip_start_seconds"] = 0.
    return destination
