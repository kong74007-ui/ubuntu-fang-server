from __future__ import annotations

from pathlib import Path


SUPPORTED_VIDEO_SIZES = frozenset({"1080x1920", "1920x1080", "1080x1080"})


def resolve_video_overlay_template(
    selected_template_path: str | Path,
    pixelle_root: str | Path | None = None,
) -> Path:
    """Resolve a platform-owned transparent overlay matching the selected size."""
    selected = Path(selected_template_path)
    size = selected.parent.name
    if size not in SUPPORTED_VIDEO_SIZES:
        raise ValueError(f"Unsupported video template size: {size}")

    if pixelle_root is None:
        from pixelle_video.utils.os_util import get_root_path

        root = Path(get_root_path())
    else:
        root = Path(pixelle_root)

    overlay = root / "templates" / size / "video_default.html"
    if not overlay.is_file():
        raise FileNotFoundError(f"Platform video overlay template not found: {overlay}")
    return overlay
