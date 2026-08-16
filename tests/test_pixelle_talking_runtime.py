from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "patches"
    / "0011-render-talking-material-scenes.patch"
)
FIXTURE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "pixelle_task4_runtime"
    / "frame_processor_post_0010.zip"
)


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _install_import_stubs(monkeypatch):
    class ProgressEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class TalkingClipError(RuntimeError):
        def __init__(self, code="talking_failed", attempts=1):
            super().__init__(code)
            self.code = code
            self.attempts = attempts

    modules = {
        "httpx": _module("httpx", HTTPError=RuntimeError),
        "loguru": _module(
            "loguru",
            logger=SimpleNamespace(
                debug=lambda *_args, **_kwargs: None,
                info=lambda *_args, **_kwargs: None,
                warning=lambda *_args, **_kwargs: None,
                error=lambda *_args, **_kwargs: None,
            ),
        ),
        "pixelle_video": _module("pixelle_video"),
        "pixelle_video.models": _module("pixelle_video.models"),
        "pixelle_video.services": _module("pixelle_video.services"),
        "pixelle_video.utils": _module("pixelle_video.utils"),
        "pixelle_video.models.progress": _module(
            "pixelle_video.models.progress", ProgressEvent=ProgressEvent
        ),
        "pixelle_video.models.storyboard": _module(
            "pixelle_video.models.storyboard",
            CaptionCue=SimpleNamespace,
            Storyboard=SimpleNamespace,
            StoryboardFrame=SimpleNamespace,
            StoryboardConfig=SimpleNamespace,
        ),
        "pixelle_video.services.caption_cues": _module(
            "pixelle_video.services.caption_cues",
            build_caption_timeline=lambda *_args, **_kwargs: [],
            caption_timeline_duration=lambda *_args, **_kwargs: 0.0,
            split_caption_text=lambda text: [text],
        ),
        "pixelle_video.services.media_retry": _module(
            "pixelle_video.services.media_retry",
            RetryBudget=object,
            retry_async=lambda function, *_args, **_kwargs: function(),
        ),
        "pixelle_video.services.talking_client": _module(
            "pixelle_video.services.talking_client",
            TalkingClient=object,
            TalkingClipError=TalkingClipError,
        ),
        "pixelle_video.services.talking_material": _module(
            "pixelle_video.services.talking_material",
            build_talking_windows=lambda *_args, **_kwargs: [],
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _frame_patch_text() -> str:
    patch = PATCH_PATH.read_text(encoding="utf-8")
    start = patch.index("diff --git a/pixelle_video/services/frame_processor.py")
    try:
        end = patch.index("\ndiff --git ", start + 1)
    except ValueError:
        end = len(patch)
    return patch[start:end] + "\n"


def _extract_frame_processor_fixture(source_root: Path) -> None:
    member = "pixelle_video/services/frame_processor.py"
    module_path = source_root / member
    module_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(FIXTURE_PATH) as archive:
        matches = [
            info
            for info in archive.infolist()
            if info.filename.replace("\\", "/") == member
        ]
        assert len(matches) == 1, archive.namelist()
        source = archive.read(matches[0]).decode("utf-8").replace("\r\n", "\n")
        module_path.write_text(source, encoding="utf-8", newline="\n")


def load_patched_frame_processor(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "patched"
    _extract_frame_processor_fixture(source_root)
    patch_path = tmp_path / "frame_processor.patch"
    patch_path.write_text(_frame_patch_text(), encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "apply",
            "--unidiff-zero",
            str(patch_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    _install_import_stubs(monkeypatch)
    module_path = source_root / "pixelle_video" / "services" / "frame_processor.py"
    spec = importlib.util.spec_from_file_location(
        f"pixelle_frame_processor_runtime_{tmp_path.name}", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(tmp_path: Path, cue_count: int):
    ordinary = tmp_path / "ordinary.mp4"
    ordinary.write_bytes(b"ordinary")
    cues = [
        SimpleNamespace(
            text=f"cue-{index}",
            audio_path=str(tmp_path / f"cue-{index}.mp3"),
            duration=1.0,
            start_time=float(index),
            end_time=float(index + 1),
        )
        for index in range(cue_count)
    ]
    for cue in cues:
        Path(cue.audio_path).write_bytes(b"audio")
    return SimpleNamespace(
        index=0,
        scene_id="scene_01",
        image_path=None,
        image_prompt=None,
        video_path=str(ordinary),
        media_type="video",
        duration=float(cue_count),
        caption_cues=cues,
        talking_cue_video_paths={},
        talking_original_video_path=None,
        talking_original_media_type=None,
        talking_single_override=False,
        talking_attempts=0,
        talking_warning=None,
        visual_source="ordinary",
    )


def _config():
    return SimpleNamespace(
        task_id="task-runtime",
        talking_material=None,
        talking_avatar_paths=None,
        media_width=1080,
        media_height=1920,
    )


@pytest.mark.parametrize("cue_count", [1, 2])
def test_final_talking_render_failure_retries_with_ordinary_visual(
    tmp_path, monkeypatch, cue_count
):
    module = load_patched_frame_processor(tmp_path, monkeypatch)
    processor = module.FrameProcessor(None)
    frame = _frame(tmp_path, cue_count)
    ordinary_path = frame.video_path
    talking_paths = {}
    for index in range(cue_count):
        path = tmp_path / f"talking-{index}.mp4"
        path.write_bytes(b"talking")
        talking_paths[index] = str(path)

    async def prepare_audio(_frame, _config):
        return None

    async def prepare_talking(_frame, _config):
        _frame.talking_cue_video_paths = dict(talking_paths)
        _frame.visual_source = "talking"
        if cue_count == 1:
            _frame.talking_original_video_path = ordinary_path
            _frame.talking_original_media_type = "video"
            _frame.talking_single_override = True
            _frame.video_path = talking_paths[0]

    processor._prepare_caption_audio = prepare_audio
    processor._prepare_talking_visuals = prepare_talking
    processor._step_generate_media = AsyncMock()
    processor._step_create_video_segment = AsyncMock()

    if cue_count == 1:
        calls = []

        async def compose(_frame, _storyboard, _config):
            calls.append(_frame.video_path)
            if _frame.video_path != ordinary_path:
                raise RuntimeError("talking compose failed")

        processor._step_compose_frame = compose
    else:
        calls = []

        async def carousel(_frame, _storyboard, _config):
            calls.append(dict(_frame.talking_cue_video_paths))
            if _frame.talking_cue_video_paths:
                raise RuntimeError("talking carousel failed")

        processor._create_caption_carousel_segment = carousel

    result = asyncio.run(processor(frame, SimpleNamespace(), _config()))

    assert result is frame
    assert frame.video_path == ordinary_path
    assert frame.talking_cue_video_paths == {}
    assert frame.visual_source == "ordinary"
    assert "talking_material_final_render_failed" in frame.talking_warning
    assert len(calls) == 2
    assert all(not Path(path).exists() for path in talking_paths.values())


def test_partial_cue_slice_is_cleaned_when_later_slice_fails(tmp_path, monkeypatch):
    module = load_patched_frame_processor(tmp_path, monkeypatch)
    processor = module.FrameProcessor(None)
    frame = _frame(tmp_path, 2)
    avatar = tmp_path / "avatar.jpg"
    avatar.write_bytes(b"avatar")
    config = _config()
    config.talking_material = {
        "enabled": True,
        "default_avatar_asset_id": "avatar-1",
        "scenes": [{"scene_id": "scene_01", "enabled": True}],
    }
    config.talking_avatar_paths = {"avatar-1": str(avatar)}

    class FakeTalkingClient:
        async def generate(self, _image, _audio, output, *_args):
            Path(output).write_bytes(b"provider")
            return SimpleNamespace(video_path=output, attempts=1)

    class FailingVideoService:
        def __init__(self):
            self.slice_count = 0

        def concat_audios(self, _inputs, output):
            Path(output).write_bytes(b"window-audio")

        def ensure_video_duration(self, path, _duration):
            return path

        def extract_video_clip(self, _source, _start, _duration, output):
            self.slice_count += 1
            Path(output).write_bytes(b"partial-slice")
            if self.slice_count == 2:
                raise RuntimeError("second slice failed after writing")
            return output

    monkeypatch.setattr(module, "TalkingClient", FakeTalkingClient)
    monkeypatch.setattr(
        module,
        "build_talking_windows",
        lambda *_args, **_kwargs: [
            {"cue_start": 0, "cue_end": 2, "duration": 2.0}
        ],
    )
    monkeypatch.setitem(
        sys.modules,
        "pixelle_video.services.video",
        _module("pixelle_video.services.video", VideoService=FailingVideoService),
    )
    monkeypatch.setitem(
        sys.modules,
        "pixelle_video.utils.os_util",
        _module(
            "pixelle_video.utils.os_util",
            get_task_frame_path=lambda *_args: str(tmp_path / "segment.mp4"),
        ),
    )

    asyncio.run(processor._prepare_talking_visuals(frame, config))

    assert frame.talking_cue_video_paths == {}
    assert frame.visual_source == "ordinary"
    assert "talking_material_processing_failed" in frame.talking_warning
    assert not list(tmp_path.glob("*_talking_cue_*.mp4"))
