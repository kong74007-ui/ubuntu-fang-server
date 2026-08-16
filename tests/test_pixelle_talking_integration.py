from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
import time
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = ROOT / "server"
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
TALKING_CLIENT_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "talking_client.py"
)
TALKING_MATERIAL_PATH = TALKING_CLIENT_PATH.with_name("talking_material.py")

DEFAULT_AVATAR_ID = "avatar_" + "a" * 32
OVERRIDE_AVATAR_ID = "avatar_" + "b" * 32
TALKING_CONFIG = {
    "enabled": True,
    "ratio": 0.3,
    "default_avatar_asset_id": DEFAULT_AVATAR_ID,
    "scenes": [
        {"scene_id": "scene_01", "enabled": True},
        {
            "scene_id": "scene_04",
            "enabled": True,
            "avatar_asset_id": OVERRIDE_AVATAR_ID,
        },
        {"scene_id": "scene_08", "enabled": True},
    ],
}


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        module_path.write_bytes(archive.read(member))


def _load_patched_frame_processor(tmp_path: Path, monkeypatch, video_service_class):
    source_root = tmp_path / "patched"
    _extract_frame_processor_fixture(source_root)
    patch_path = tmp_path / "frame_processor.patch"
    patch_path.write_text(_frame_patch_text(), encoding="utf-8")
    completed = subprocess.run(
        ["git", "-C", str(source_root), "apply", "--unidiff-zero", str(patch_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    class ProgressEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

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
        "pixelle_video.services.video": _module(
            "pixelle_video.services.video", VideoService=video_service_class
        ),
        "pixelle_video.utils.os_util": _module(
            "pixelle_video.utils.os_util",
            get_task_frame_path=lambda task_id, index, _kind: str(
                tmp_path / f"{task_id}-{index}-segment.mp4"
            ),
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    talking_client = _load_module(
        "pixelle_video.services.talking_client", TALKING_CLIENT_PATH
    )
    talking_material = _load_module(
        "pixelle_video.services.talking_material", TALKING_MATERIAL_PATH
    )
    monkeypatch.setitem(
        sys.modules, "pixelle_video.services.talking_client", talking_client
    )
    monkeypatch.setitem(
        sys.modules, "pixelle_video.services.talking_material", talking_material
    )

    module_path = source_root / "pixelle_video" / "services" / "frame_processor.py"
    spec = importlib.util.spec_from_file_location(
        f"pixelle_frame_processor_integration_{tmp_path.name}", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, talking_client


def _provider_video(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for talking integration verification")
    output = tmp_path / "provider-with-audio.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=0.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.4",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return output


def _has_audio(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is required for talking integration verification")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip())


class _BridgeResponse:
    def __init__(self, bridge, result):
        self._bridge = bridge
        self._result = result
        self._path = bridge.resolve_video_path(result)
        self.headers = {"X-Provider-Video-Id": result["video_id"]}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._bridge._cleanup_result_artifacts(
            self._result, self._path, "integration-response"
        )

    def read(self):
        return self._path.read_bytes()


def _frame(tmp_path: Path, index: int, scene_id: str, duration: float):
    ordinary = tmp_path / f"ordinary-{scene_id}.mp4"
    ordinary.write_bytes(b"ordinary")
    audio = tmp_path / f"audio-{scene_id}.mp3"
    audio.write_bytes(b"ID3" + scene_id.encode("ascii"))
    cue = SimpleNamespace(
        text=f"caption-{scene_id}",
        audio_path=str(audio),
        duration=duration,
        start_time=0.0,
        end_time=duration,
    )
    return SimpleNamespace(
        index=index,
        scene_id=scene_id,
        image_path=None,
        image_prompt=None,
        video_path=str(ordinary),
        media_type="video",
        duration=duration,
        caption_cues=[cue],
        talking_cue_video_paths={},
        talking_original_video_path=None,
        talking_original_media_type=None,
        talking_single_override=False,
        talking_attempts=0,
        talking_warning=None,
        visual_source="ordinary",
    )


def test_three_talking_scenes_reuse_two_images_and_preserve_authoritative_audio(
    tmp_path, monkeypatch
):
    if str(SERVER_ROOT) not in sys.path:
        sys.path.insert(0, str(SERVER_ROOT))
    from content_domains import core, pixelle_talking as bridge, video

    provider_video = _provider_video(tmp_path)
    bridge_root = tmp_path / "bridge-output"
    bridge_root.mkdir()
    monkeypatch.setattr(bridge, "OUT_DIR", bridge_root)
    monkeypatch.setattr(core, "OUT_DIR", bridge_root)
    monkeypatch.setattr(video, "OUT_DIR", bridge_root)
    bridge._IMAGE_ASSET_CACHE.clear()
    bridge._IMAGE_UPLOADS.clear()

    uploads = []
    provider_calls = []
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def upload(_image_file):
        asset_id = f"asset-{len(uploads) + 1}"
        uploads.append(asset_id)
        time.sleep(0.03)
        return asset_id

    def generate(
        _image_file,
        _audio_file,
        _resolution,
        _ratio,
        _motion,
        image_asset_id=None,
        internal=False,
        internal_output_file=None,
    ):
        nonlocal active, maximum_active
        assert internal is True
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.08)
            output = bridge_root / internal_output_file
            output.write_bytes(provider_video.read_bytes())
            call_number = len(provider_calls) + 1
            provider_calls.append(image_asset_id)
            return {
                "video_id": f"video-{call_number}",
                "image_asset_id": image_asset_id,
                "video_file": internal_output_file,
            }
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", generate)

    class VideoService:
        def concat_audios(self, inputs, output):
            Path(output).write_bytes(b"".join(Path(path).read_bytes() for path in inputs))

        def ensure_video_duration(self, path, _duration):
            return path

        def extract_video_clip(self, source, _start, _duration, output):
            shutil.copyfile(source, output)
            return output

    frame_module, talking_client = _load_patched_frame_processor(
        tmp_path, monkeypatch, VideoService
    )

    requests = []

    def opener(request, timeout):
        assert timeout == talking_client.SOCKET_TIMEOUT_SECONDS
        requests.append(json.loads(request.data.decode("utf-8")))
        return _BridgeResponse(bridge, bridge.generate_clip(requests[-1]))

    def client_factory():
        return talking_client.TalkingClient(token="integration-token", opener=opener)

    monkeypatch.setattr(frame_module, "TalkingClient", client_factory)

    default_avatar = tmp_path / "default.png"
    override_avatar = tmp_path / "override.png"
    default_avatar.write_bytes(b"\x89PNG\r\n\x1a\ndefault-avatar")
    override_avatar.write_bytes(b"\x89PNG\r\n\x1a\noverride-avatar")
    config = SimpleNamespace(
        task_id="task-integration",
        talking_material=TALKING_CONFIG,
        talking_avatar_paths={
            DEFAULT_AVATAR_ID: str(default_avatar),
            OVERRIDE_AVATAR_ID: str(override_avatar),
        },
        media_width=1080,
        media_height=1920,
    )
    frames = [
        _frame(tmp_path, 0, "scene_01", 5.6),
        _frame(tmp_path, 3, "scene_04", 6.4),
        _frame(tmp_path, 7, "scene_08", 5.9),
    ]
    caption_end_times = [frame.caption_cues[-1].end_time for frame in frames]
    audio_prepare_calls = []
    composed_silent_paths = []

    async def run_frame(frame):
        processor = frame_module.FrameProcessor(None)

        async def prepare_caption_audio(current, _config):
            audio_prepare_calls.append(current.scene_id)

        async def compose(current, _storyboard, _config):
            talking_path = Path(current.video_path)
            assert talking_path.is_file()
            assert not _has_audio(talking_path)
            composed_silent_paths.append(talking_path.name)

        processor._prepare_caption_audio = prepare_caption_audio
        processor._step_generate_media = AsyncMock()
        processor._step_compose_frame = compose
        processor._step_create_video_segment = AsyncMock()
        return await processor(frame, SimpleNamespace(), config)

    async def run_all_frames():
        return await asyncio.gather(*(run_frame(frame) for frame in frames))

    asyncio.run(run_all_frames())

    assert len(requests) == 3
    assert len(provider_calls) == 3
    assert len(uploads) == 2
    assert len({request["image_sha256"] for request in requests}) == 2
    assert sorted(audio_prepare_calls) == ["scene_01", "scene_04", "scene_08"]
    assert maximum_active == 2
    assert len(composed_silent_paths) == 3
    assert [frame.caption_cues[-1].end_time for frame in frames] == caption_end_times
    assert all(frame.visual_source == "talking" for frame in frames)
    assert all(frame.talking_warning is None for frame in frames)


def test_talking_failure_keeps_the_existing_ordinary_visual(tmp_path, monkeypatch):
    class VideoService:
        def concat_audios(self, _inputs, output):
            Path(output).write_bytes(b"audio")

        def ensure_video_duration(self, path, _duration):
            return path

        def extract_video_clip(self, source, _start, _duration, output):
            shutil.copyfile(source, output)
            return output

    frame_module, talking_client = _load_patched_frame_processor(
        tmp_path, monkeypatch, VideoService
    )

    class FailingClient:
        async def generate(self, *_args, **_kwargs):
            raise talking_client.TalkingClipError(
                "provider_unavailable",
                "provider unavailable",
                retryable=False,
                billed=False,
                attempts=1,
            )

    monkeypatch.setattr(frame_module, "TalkingClient", FailingClient)
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(b"\x89PNG\r\n\x1a\navatar")
    frame = _frame(tmp_path, 0, "scene_01", 6.2)
    ordinary = frame.video_path
    config = SimpleNamespace(
        task_id="task-fallback",
        talking_material=TALKING_CONFIG,
        talking_avatar_paths={DEFAULT_AVATAR_ID: str(avatar)},
        media_width=1080,
        media_height=1920,
    )

    asyncio.run(frame_module.FrameProcessor(None)._prepare_talking_visuals(frame, config))

    assert frame.video_path == ordinary
    assert frame.visual_source == "ordinary"
    assert frame.talking_cue_video_paths == {}
    assert frame.talking_warning == "provider_unavailable after 1 attempt(s)"
