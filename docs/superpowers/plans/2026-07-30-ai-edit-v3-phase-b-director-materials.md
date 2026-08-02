# AI 智能剪辑 V3 Phase B Director and Materials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase A 的任务、账务和协议基础上，完成五类输入的媒体标准化与准确文本时间线，接入固定 Qwen3.7-Max 多模态导演，并严格实现“仅本次图片优先、缺图才调用网站生图、语义不符不填空”的素材解析闭环。

**Architecture:** Phase B 由纯函数媒体/文本层、无状态 Provider 适配层和 pipeline 检查点组成。平台原文或用户 TTS 文案始终是权威文本；外部媒体只允许确定性标点断句清理。Qwen 输入经过脱敏和能力冻结，只能生成 `edit-plan 2.0`；Schema 与事实校验失败最多由同一模型修复一次。素材解析器只消费本任务不可变图片记录，先匹配后按 required 槽位调用现有生图服务。

**Tech Stack:** Python 3、FFprobe/FFmpeg、fun-asr、DashScope `qwen3.7-max-2026-06-08`、网站现有 TTS 与图片生成适配接口、JSON Schema 2020-12、SQLite/WAL、腾讯云 COS、`unittest`。

## Global Constraints

- [ ] 先确认 Phase A gate 通过：三份 Schema 哈希冻结、V3 Store/lease/fencing 可用、共享 ledger query 与 asset publication Saga 已存在、V2 回归为绿。
- [ ] 本阶段不创建 UI、Node renderer、HyperFrames 组件、ElevenLabs 音频或测试部署；只交付可被 Phase C 消费的冻结时间线、合法 edit-plan 和 resolved materials。
- [ ] Provider 适配器不得访问 V3 数据库、修改任务状态或读取其他任务素材；pipeline 是唯一阶段转换者，store 是唯一持久层。
- [ ] 所有网络调用前先持久化不可变 intent 和请求指纹；已知 request ID 恢复查询，结果未知时不得盲目重提。
- [ ] 平台口播和 TTS 模式以权威原文为准；Qwen 不能改字。外部媒体清理前后去除标点后的字符序列必须一致。
- [ ] 只读取本任务 `material_asset_ids` 指向的最多 10 张图片；严禁查询用户历史素材、公共素材或其他视频作为 B-roll。
- [ ] 先写失败测试、观察预期失败、做最小实现、跑定向与 V2 回归、检查 diff，再提交；每项任务一个 commit。
- [ ] 每个 Task 的 `Required RED anchor` 必须先写入其 `Files` 中声明的测试文件，并运行该 Task 列出的第一条定向命令：实现前只允许因目标模块/函数/断言缺失而非 fixture 或语法错误失败；最小实现后重跑同一命令必须 exit `0`，这就是该 Task 的 GREEN 证据。

---

### Task 1: Freeze media inspection and normalization contracts

**Files:**

- Create: `server/content_domains/ai_edit_v3/media.py`
- Create: `tests/test_ai_edit_v3_media.py`
- Create: `tests/fixtures/ai_edit_v3/media/manifest.json`

**Interfaces:**

```python
@dataclass(frozen=True)
class MediaProbe:
    media_type: Literal["video", "audio", "image"]
    duration_ms: int
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    rotation: int
    codecs: tuple[str, ...]
    streams: tuple[Mapping[str, Any], ...]

@dataclass(frozen=True)
class NormalizedMedia:
    relative_path: str
    sha256: str
    duration_ms: int
    ratio: Literal["16:9", "9:16"] | None
    time_base_num: int
    time_base_den: int
    audit: Mapping[str, Any]

def probe_media(path: Path, *, timeout_seconds: int = 30) -> MediaProbe: ...
def validate_primary_media(probe: MediaProbe, *, input_type: str) -> None: ...
def normalize_primary_media(source: Path, output_root: Path,
                            *, input_type: str, deadline_at: float) -> NormalizedMedia: ...
def decode_and_normalize_image(source: Path, output_root: Path,
                               *, deadline_at: float) -> NormalizedImage: ...
def extract_director_keyframes(video: Path, output_root: Path,
                               *, max_frames: int = 12) -> tuple[Keyframe, ...]: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.media import (
    MediaProbe,
    MediaValidationError,
    validate_primary_media,
)


class MediaContractTests(unittest.TestCase):
    def test_uploaded_audio_rejects_video_stream(self) -> None:
        probe = MediaProbe(
            media_type="video",
            duration_ms=3_000,
            width=1080,
            height=1920,
            fps_num=30,
            fps_den=1,
            rotation=0,
            codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )

        with self.assertRaisesRegex(MediaValidationError, "media_type_mismatch"):
            validate_primary_media(probe, input_type="uploaded_audio")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_media.py`; create `tests/fixtures/ai_edit_v3/media/manifest.json` with exactly `{}` so discovery never fails because a declared fixture path is absent.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_media.MediaContractTests.test_uploaded_audio_rejects_video_stream -v`. Expected: `ERROR` with `ModuleNotFoundError: No module named 'server.content_domains.ai_edit_v3.media'`; if the module was created by another branch, expected `ImportError` for `MediaProbe`, `MediaValidationError`, or `validate_primary_media`. A syntax, JSON, or fixture error is not valid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/media.py` with exactly this executable baseline:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MediaProbe:
    media_type: Literal["video", "audio", "image"]
    duration_ms: int
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    rotation: int
    codecs: tuple[str, ...]
    streams: tuple[Mapping[str, Any], ...]


def validate_primary_media(probe: MediaProbe, *, input_type: str) -> None:
    expected_type = {
        "platform_talking_head": "video",
        "uploaded_video": "video",
        "existing_audio": "audio",
        "uploaded_audio": "audio",
        "script_to_audio_video": "audio",
    }.get(input_type)
    if expected_type is None:
        raise MediaValidationError("input_type_invalid")
    if probe.media_type != expected_type:
        raise MediaValidationError("media_type_mismatch")
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_media.MediaContractTests.test_uploaded_audio_rejects_video_stream -v`. Expected: `Ran 1 test` and `OK` with exit code `0`.
- [ ] **Step 5: Extend by TDD, one named behavior at a time.** Add a failing test, run that one test, add the minimum implementation, and rerun it for each of: 3-second and 10-minute boundaries; video/audio mutual exclusion; 4096-pixel and 60-fps limits; physical rotation before ratio calculation; VFR-to-CFR at time base `1/30`; monotonic timestamps; 30-second probe timeout; JPEG/PNG/WebP only; 25 MiB, 80 MP and 12000-pixel image limits; EXIF orientation; ten-second image decode deadline; compressed-image bomb rejection; FFmpeg argument-list construction; local `file`/`pipe` protocol allowlist; signed-query redaction; process-group termination; first/last plus scene/uniform keyframes; maximum 12 JPEGs; long edge 640; quality 80; deterministic names and SHA-256.
- [ ] **Step 6: Complete the declared module interfaces.** Keep `validate_primary_media` above and add `NormalizedMedia`, `NormalizedImage`, `Keyframe`, `probe_media`, `normalize_primary_media`, `decode_and_normalize_image`, and `extract_director_keyframes` with the exact signatures in **Interfaces**. The implementation must normalize video to H.264/yuv420p/CFR, audio to a 48 kHz lossless intermediate, physically apply rotation, and pass every test added in Step 5; no function may remain as `pass`, `...`, or `NotImplementedError`.
- [ ] **Step 7: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_media -v`; expected all tests `OK`. Then run `python -m unittest tests.test_ai_edit_v2_media -v`; expected all tests `OK`.
- [ ] **Step 8: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/media.py tests/test_ai_edit_v3_media.py tests/fixtures/ai_edit_v3/media/manifest.json`, then `git diff --cached --check`, then `git commit -m "feat(ai-edit-v3): normalize director media inputs"`.

### Task 2: Implement five-input source preparation and website TTS boundary

**Files:**

- Create: `server/content_domains/ai_edit_v3/providers/tts.py`
- Create: `server/content_domains/ai_edit_v3/source.py`
- Create: `tests/test_ai_edit_v3_source.py`
- Create: `tests/test_ai_edit_v3_tts.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class PreparedSource:
    input_type: str
    authoritative_text: str | None
    media: NormalizedMedia
    source_asset_id: str | None
    source_upload_id: str | None
    provider_request_id: str | None
    source_fingerprint: str

class TtsProvider(Protocol):
    def submit(self, *, owner: str, text: str, voice_id: str,
               idempotency_key: str, deadline_at: float) -> ProviderResult: ...
    def query(self, request_id: str, *, deadline_at: float) -> ProviderResult: ...

def prepare_source(job: Mapping[str, Any], deps: SourceDependencies,
                   context: StageContext) -> PreparedSource: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.source import SourceError, prepare_source


class FakeVoices:
    def get_active_for_owner(self, owner: str, voice_id: str) -> object | None:
        if (owner, voice_id) == ("alice", "voice-1"):
            return SimpleNamespace(id="voice-1")
        return None


class FakeTts:
    def __init__(self) -> None:
        self.submissions: list[str] = []

    def submit(self, **kwargs: object) -> SimpleNamespace:
        self.submissions.append(str(kwargs["idempotency_key"]))
        return SimpleNamespace(request_id="tts-request-1", media=SimpleNamespace())


class SourceContractTests(unittest.TestCase):
    def test_script_to_audio_freezes_owner_voice_and_request_identity(self) -> None:
        tts = FakeTts()
        deps = SimpleNamespace(voices=FakeVoices(), tts=tts)
        context = SimpleNamespace(deadline_at=100.0)
        job = {
            "id": "j1",
            "owner": "alice",
            "input_type": "script_to_audio_video",
            "authoritative_text": "价格是 298 元",
            "voice_id": "voice-1",
        }

        source = prepare_source(job, deps, context)

        self.assertEqual(source.authoritative_text, "价格是 298 元")
        self.assertEqual(source.provider_request_id, "tts-request-1")
        self.assertEqual(tts.submissions, ["ai-edit-v3:j1:tts"])
        with self.assertRaisesRegex(SourceError, "voice_not_found"):
            prepare_source({**job, "owner": "mallory"}, deps, context)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_source.py`. Create `tests/test_ai_edit_v3_tts.py` with `import unittest` and a `TtsContractTests(unittest.TestCase)` class containing `test_protocol_module_imports`, which imports `TtsProvider` from `server.content_domains.ai_edit_v3.providers.tts` and asserts it is not `None`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_source.SourceContractTests.test_script_to_audio_freezes_owner_voice_and_request_identity -v`. Expected: `ERROR` with `ModuleNotFoundError` or `ImportError` naming `source`, `SourceError`, or `prepare_source`; fixture or syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/source.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedSource:
    input_type: str
    authoritative_text: str | None
    media: Any
    source_asset_id: str | None
    source_upload_id: str | None
    provider_request_id: str | None
    source_fingerprint: str


def prepare_source(job: Mapping[str, Any], deps: Any, context: Any) -> PreparedSource:
    input_type = str(job["input_type"])
    if input_type != "script_to_audio_video":
        raise SourceError("input_type_not_implemented")
    owner = str(job["owner"])
    voice_id = str(job["voice_id"])
    if deps.voices.get_active_for_owner(owner, voice_id) is None:
        raise SourceError("voice_not_found")
    text = str(job["authoritative_text"])
    result = deps.tts.submit(
        owner=owner,
        text=text,
        voice_id=voice_id,
        idempotency_key=f"ai-edit-v3:{job['id']}:tts",
        deadline_at=context.deadline_at,
    )
    return PreparedSource(
        input_type=input_type,
        authoritative_text=text,
        media=result.media,
        source_asset_id=None,
        source_upload_id=None,
        provider_request_id=str(result.request_id),
        source_fingerprint=sha256(text.encode("utf-8")).hexdigest(),
    )
```

Create `server/content_domains/ai_edit_v3/providers/tts.py` with:

```python
from __future__ import annotations

from typing import Any, Protocol


class TtsProvider(Protocol):
    def submit(self, *, owner: str, text: str, voice_id: str,
               idempotency_key: str, deadline_at: float) -> Any:
        raise NotImplementedError

    def query(self, request_id: str, *, deadline_at: float) -> Any:
        raise NotImplementedError
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_source.SourceContractTests.test_script_to_audio_freezes_owner_voice_and_request_identity tests.test_ai_edit_v3_tts.TtsContractTests.test_protocol_module_imports -v`. Expected: `Ran 2 tests` and `OK`.
- [ ] **Step 5: Extend by TDD.** For each case, write one named failing test, run it alone, implement only that behavior, and rerun it: owner-scoped `platform_talking_head` script; scriptless `uploaded_video`; cross-owner asset/upload denial; audio-only `existing_audio` and `uploaded_audio`; frozen text hash; active owner-authorized voice; intent persistence before TTS submit; lost submit response; query-by-request-ID restart; trusted TTS timestamps; ASR fallback without timestamps. Use fake repositories/providers only—no live provider calls.
- [ ] **Step 6: Finish the five-input implementation.** Replace `input_type_not_implemented` with exhaustive branches for all five input types. Use only V3 adapters over existing owner-checked digital-IP, audio, voice, and TTS services. Persist stable asset/upload IDs, provider IDs, text hashes, and V3 COS keys; tests must reject short URLs and clone-provider internals in serialized state. Do not import V2 store or V2 COS modules.
- [ ] **Step 7: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_source tests.test_ai_edit_v3_tts -v`, then `python -m unittest tests.test_audio_lists tests.test_ai_edit_v2_api -v`; both commands must end with `OK` and exit code `0`.
- [ ] **Step 8: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/providers/tts.py server/content_domains/ai_edit_v3/source.py tests/test_ai_edit_v3_source.py tests/test_ai_edit_v3_tts.py`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): prepare video audio and tts sources"`.

### Task 3: Normalize ASR and build authoritative text timelines

**Files:**

- Create: `server/content_domains/ai_edit_v3/providers/asr.py`
- Create: `server/content_domains/ai_edit_v3/transcript.py`
- Create: `tests/test_ai_edit_v3_asr.py`
- Create: `tests/test_ai_edit_v3_transcript.py`
- Create: `tests/fixtures/ai_edit_v3/transcripts/`

**Interfaces:**

```python
@dataclass(frozen=True)
class AsrWord:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None

@dataclass(frozen=True)
class TextTimeline:
    duration_ms: int
    captions: tuple[Caption, ...]
    source_segments: tuple[SourceSegment, ...]
    authoritative_text_sha256: str | None
    alignment_coverage: float

MIN_ALIGNMENT_COVERAGE = 0.85

def normalize_asr_result(payload: Mapping[str, Any]) -> NormalizedTranscript: ...
def align_authoritative_text(text: str,
                             words: Sequence[AsrWord]) -> AlignmentResult: ...
def normalize_external_punctuation(text: str) -> str: ...
def validate_punctuation_only(source: str, cleaned: str) -> None: ...
def build_text_timeline(source: PreparedSource,
                        asr: NormalizedTranscript) -> TextTimeline: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.transcript import (
    TranscriptError,
    normalize_external_punctuation,
    validate_punctuation_only,
)


class TranscriptContractTests(unittest.TestCase):
    def test_external_cleanup_may_change_punctuation_but_not_price(self) -> None:
        cleaned = normalize_external_punctuation("价格是298元今天下单")
        validate_punctuation_only("价格是298元今天下单", cleaned)
        self.assertEqual(cleaned, "价格是298元，今天下单。")
        with self.assertRaisesRegex(TranscriptError, "external_text_changed"):
            validate_punctuation_only("价格是298元", "价格是299元。")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED tests.** Save the code above as `tests/test_ai_edit_v3_transcript.py`. Create `tests/test_ai_edit_v3_asr.py` with `class AsrContractTests(unittest.TestCase)` and method `test_normalizes_one_word`; it imports `AsrWord` and `normalize_asr_result`, calls `normalize_asr_result({"words": [{"text": "你", "start_ms": 0, "end_ms": 100}]})`, and asserts the first item equals `AsrWord("你", 0, 100, None)`. Include the standard `if __name__ == "__main__": unittest.main()` entry point. Create `tests/fixtures/ai_edit_v3/transcripts/empty.json` containing exactly `{}`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_transcript.TranscriptContractTests.test_external_cleanup_may_change_punctuation_but_not_price tests.test_ai_edit_v3_asr.AsrContractTests.test_normalizes_one_word -v`. Expected: both tests report `ERROR` with missing `transcript`/`asr` modules or missing imported symbols; syntax/fixture errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/transcript.py` with:

```python
from __future__ import annotations

import re


class TranscriptError(ValueError):
    pass


_PUNCTUATION_RE = re.compile(r"[，。！？；：、,.!?;:\\s]+")


def _without_punctuation(value: str) -> str:
    return _PUNCTUATION_RE.sub("", value)


def normalize_external_punctuation(text: str) -> str:
    compact = _without_punctuation(text)
    if compact == "价格是298元今天下单":
        return "价格是298元，今天下单。"
    return compact + "。"


def validate_punctuation_only(source: str, cleaned: str) -> None:
    if _without_punctuation(source) != _without_punctuation(cleaned):
        raise TranscriptError("external_text_changed")
```

- [ ] In the same GREEN step, create `server/content_domains/ai_edit_v3/providers/asr.py` with this executable baseline:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class AsrResultError(ValueError):
    pass


@dataclass(frozen=True)
class AsrWord:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None


def normalize_asr_result(payload: Mapping[str, Any]) -> tuple[AsrWord, ...]:
    raw_words = payload.get("words")
    if not isinstance(raw_words, list):
        raise AsrResultError("asr_words_invalid")
    return tuple(
        AsrWord(
            text=str(raw["text"]),
            start_ms=int(raw["start_ms"]),
            end_ms=int(raw["end_ms"]),
            confidence=(
                None if raw.get("confidence") is None
                else float(raw["confidence"])
            ),
        )
        for raw in raw_words
    )
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_transcript.TranscriptContractTests.test_external_cleanup_may_change_punctuation_but_not_price tests.test_ai_edit_v3_asr.AsrContractTests.test_normalizes_one_word -v`. Expected: `Ran 2 tests` and `OK`.
- [ ] **Step 5: Extend ASR normalization by TDD.** Extend the Step 3 `server/content_domains/ai_edit_v3/providers/asr.py` baseline; add complete failing tests and one-test RED/GREEN cycles for `AsrWord`, `normalize_asr_result`, malformed/negative/overlapping word timestamps, non-monotonic sentences, empty transcript, duplicate result binding, and `SubmissionUnknown` recovery without blind resubmit. Keep transport injected and network-free.
- [ ] **Step 6: Extend authoritative alignment by TDD.** Add complete tests and RED/GREEN cycles for exact platform/TTS text when ASR mishears brands, prices, or digits; coverage below `MIN_ALIGNMENT_COVERAGE = 0.85`; impossible monotonic mappings; repeated text; ambiguous sentence boundaries; and millisecond-only captions. Low coverage or impossible mappings must raise a stable error and must never substitute ASR text for authoritative text.
- [ ] **Step 7: Generalize punctuation-only cleanup.** Replace the anchor-specific sentence branch with deterministic Chinese punctuation and sentence splitting. Tests must prove deletion, substitution, inserted claims, changed digits, changed negation, and reordered non-punctuation characters raise `external_text_changed`; punctuation and whitespace alone may differ.
- [ ] **Step 8: Complete declared interfaces.** Implement `NormalizedTranscript`, `AlignmentResult`, `Caption`, `SourceSegment`, `TextTimeline`, `align_authoritative_text`, and `build_text_timeline` with the exact fields/signatures in **Interfaces**. All tests from Steps 5–7 must pass; no branch may contain anchor-specific product text.
- [ ] **Step 9: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_asr tests.test_ai_edit_v3_transcript -v`, then `python -m unittest tests.test_ai_edit_v2_alignment tests.test_tikhub_asr -v`; expect `OK` and exit `0` for both.
- [ ] **Step 10: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/providers/asr.py server/content_domains/ai_edit_v3/transcript.py tests/test_ai_edit_v3_asr.py tests/test_ai_edit_v3_transcript.py tests/fixtures/ai_edit_v3/transcripts`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): build authoritative transcript timelines"`.

### Task 4: Compile semantic keep/cut decisions into a single source map

**Files:**

- Modify: `server/content_domains/ai_edit_v3/transcript.py`
- Create: `server/content_domains/ai_edit_v3/source_map.py`
- Create: `tests/test_ai_edit_v3_source_map.py`

**Interfaces:**

```python
def build_candidate_segments(timeline: TextTimeline,
                             pauses: Sequence[Pause]) -> tuple[CandidateSegment, ...]: ...
def compile_keep_decisions(timeline: TextTimeline,
                           requested_ids: Sequence[str]) -> tuple[SourceSegment, ...]: ...
def map_source_ms_to_output_ms(segments: Sequence[SourceSegment],
                               source_ms: int) -> int: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.source_map import (
    SourceMapError,
    compile_keep_decisions,
)


class SourceMapContractTests(unittest.TestCase):
    def test_keep_decisions_cannot_reorder_segments(self) -> None:
        timeline = SimpleNamespace(
            source_segments=(
                SimpleNamespace(id="segment_01", start_ms=0, end_ms=1000, protected=True),
                SimpleNamespace(id="segment_02", start_ms=1000, end_ms=2000, protected=False),
                SimpleNamespace(id="segment_03", start_ms=2000, end_ms=3000, protected=True),
            )
        )

        with self.assertRaisesRegex(SourceMapError, "source_order_invalid"):
            compile_keep_decisions(timeline, ["segment_03", "segment_01"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_source_map.py`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_source_map.SourceMapContractTests.test_keep_decisions_cannot_reorder_segments -v`. Expected: `ERROR` with missing `source_map` module or missing `SourceMapError`/`compile_keep_decisions`; unrelated fixture or syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/source_map.py` with:

```python
from __future__ import annotations

from typing import Any, Sequence


class SourceMapError(ValueError):
    pass


def compile_keep_decisions(timeline: Any, requested_ids: Sequence[str]) -> tuple[Any, ...]:
    by_id = {segment.id: segment for segment in timeline.source_segments}
    try:
        selected = tuple(by_id[segment_id] for segment_id in requested_ids)
    except KeyError as exc:
        raise SourceMapError("source_segment_unknown") from exc
    starts = [segment.start_ms for segment in selected]
    if starts != sorted(starts):
        raise SourceMapError("source_order_invalid")
    return selected
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_source_map.SourceMapContractTests.test_keep_decisions_cannot_reorder_segments -v`. Expected: `Ran 1 test` and `OK`.
- [ ] **Step 5: Extend by TDD.** Add one complete failing test and execute an individual RED/GREEN cycle for exact semantic cuts, unknown IDs, duplicate IDs, source order, source overlap/gap rejection, output continuity from zero, no time stretching, protected whole sentences containing brands/products/digits/prices, a 60-second source retained or shortened by complete segments, and rejection of fragment cuts used only to hit 30 seconds.
- [ ] **Step 6: Add deterministic mapping properties.** Use a fixed seeded case table—not a nondeterministic property library—to prove source and output positions remain monotonic; captions, picture, and voice use the same map; repeated phrases bind by segment ID; and millisecond rounding preserves endpoints. Each row is first added as a failing subtest and then made green.
- [ ] **Step 7: Complete declared interfaces.** Implement `CandidateSegment`, `Pause`, `build_candidate_segments`, the final typed `compile_keep_decisions`, and `map_source_ms_to_output_ms` with the exact signatures in **Interfaces**. Return new contiguous output segments rather than mutating timeline input; remove any anchor-only assumptions.
- [ ] **Step 8: Run task suites.** Run `python -m unittest tests.test_ai_edit_v3_source_map tests.test_ai_edit_v3_transcript -v`; expected all tests `OK` and exit code `0`.
- [ ] **Step 9: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/transcript.py server/content_domains/ai_edit_v3/source_map.py tests/test_ai_edit_v3_source_map.py`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): compile adaptive source segment maps"`.

### Task 5: Implement the fixed DashScope multimodal provider

**Files:**

- Create: `server/content_domains/ai_edit_v3/providers/dashscope.py`
- Modify: `server/content_domains/ai_edit_v3/providers/base.py`
- Create: `tests/test_ai_edit_v3_dashscope.py`
- Create: `tests/fixtures/ai_edit_v3/providers/dashscope/`

**Interfaces:**

```python
QWEN_MODEL = "qwen3.7-max-2026-06-08"
WORKSPACE_ID_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")

@dataclass(frozen=True, repr=False)
class SecretValue:
    value: str
    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

class DashScopeMultimodalClient:
    def __init__(self, *, api_key: SecretValue, workspace_id: str,
                 http: HttpClient) -> None: ...
    def preflight(self, *, deadline_at: float) -> CapabilityResult: ...
    def generate_plan(self, request: DirectorRequest,
                      *, purpose: Literal["initial", "repair"],
                      idempotency_key: str,
                      deadline_at: float) -> ProviderResult: ...
    def analyze_images(self, request: MaterialAnalysisRequest,
                       *, idempotency_key: str,
                       deadline_at: float) -> ProviderResult: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.providers.base import SecretValue
from server.content_domains.ai_edit_v3.providers.dashscope import (
    DashScopeMultimodalClient,
)


class FakeHttp:
    def __init__(self) -> None:
        self.requests: list[SimpleNamespace] = []

    def post(self, *, url: str, json: dict[str, object],
             headers: dict[str, str], deadline_at: float) -> dict[str, object]:
        self.requests.append(SimpleNamespace(url=url, json=json, headers=headers))
        return {"request_id": "qwen-1", "output": {"choices": []}}


class DashScopeContractTests(unittest.TestCase):
    def test_client_freezes_workspace_endpoint_and_model(self) -> None:
        http = FakeHttp()
        client = DashScopeMultimodalClient(
            api_key=SecretValue("test-only"), workspace_id="ws-123", http=http,
        )

        client.generate_plan(
            {"input": "safe-test"},
            purpose="initial",
            idempotency_key="director-1",
            deadline_at=100.0,
        )

        sent = http.requests[0]
        self.assertEqual(sent.json["model"], "qwen3.7-max-2026-06-08")
        self.assertEqual(
            sent.url,
            "https://ws-123.cn-beijing.maas.aliyuncs.com/api/v1/services/"
            "aigc/multimodal-generation/generation",
        )
        self.assertEqual(repr(SecretValue("secret")), "SecretValue([REDACTED])")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_dashscope.py`. Create `tests/fixtures/ai_edit_v3/providers/dashscope/empty-choices.json` containing `{"request_id":"qwen-1","output":{"choices":[]}}` on one line.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_dashscope.DashScopeContractTests.test_client_freezes_workspace_endpoint_and_model -v`. Expected: `ERROR` naming missing `SecretValue`, `dashscope`, or `DashScopeMultimodalClient`; network access, fixture errors, and syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Append this concrete value object to `server/content_domains/ai_edit_v3/providers/base.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class SecretValue:
    value: str

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"
```

Create `server/content_domains/ai_edit_v3/providers/dashscope.py` with:

```python
from __future__ import annotations

import re
from typing import Any, Literal

from .base import SecretValue

QWEN_MODEL = "qwen3.7-max-2026-06-08"
WORKSPACE_ID_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class DashScopeConfigurationError(ValueError):
    pass


class DashScopeMultimodalClient:
    def __init__(self, *, api_key: SecretValue, workspace_id: str, http: Any) -> None:
        if WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
            raise DashScopeConfigurationError("workspace_id_invalid")
        self._api_key = api_key
        self._http = http
        self._endpoint = (
            f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/"
            "aigc/multimodal-generation/generation"
        )

    def generate_plan(self, request: Any, *, purpose: Literal["initial", "repair"],
                      idempotency_key: str, deadline_at: float) -> Any:
        return self._http.post(
            url=self._endpoint,
            json={"model": QWEN_MODEL, "input": request, "purpose": purpose},
            headers={
                "Authorization": f"Bearer {self._api_key.value}",
                "Idempotency-Key": idempotency_key,
            },
            deadline_at=deadline_at,
        )
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_dashscope.DashScopeContractTests.test_client_freezes_workspace_endpoint_and_model -v`. Expected: `Ran 1 test`, `OK`, exit `0`, and zero real HTTP requests.
- [ ] **Step 5: Freeze configuration by TDD.** Add table-driven tests for the exact `WORKSPACE_ID_RE`: lowercase ASCII letters, digits, and interior hyphens only; length 1–63; reject leading/trailing hyphen, dot, underscore, port, slash, Unicode, endpoint override variables, V2 text-generation URLs, `qwen-plus`, `qwen3.7-plus`, and constructor/model overrides. Run each new test RED, implement the validation, and rerun GREEN.
- [ ] **Step 6: Complete provider semantics by TDD.** Add complete fake-transport tests for thinking mode, terminal-content-only aggregation, bounded request size, absolute deadline propagation, redacted errors, 429/clearly-not-accepted retry, request-body-sent `SubmissionUnknown`, wrong region/workspace/model preflight, missing credential, and a minimal no-user-data multimodal preflight. Add `preflight` and `analyze_images` with the exact signatures in **Interfaces**; both methods must use the same `_endpoint` and `QWEN_MODEL`.
- [ ] **Step 7: Prove secret and media redaction.** Capture fake logger records and assert no Authorization value, raw image bytes, signed URL query string, transcript body, or style prompt appears in logs/exceptions. The client remains stateless and receives its transport by injection.
- [ ] **Step 8: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_dashscope -v`, then `python -m unittest tests.test_ai_edit_v2_dashscope -v`; expect `OK` and exit `0`.
- [ ] **Step 9: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/providers/base.py server/content_domains/ai_edit_v3/providers/dashscope.py tests/test_ai_edit_v3_dashscope.py tests/fixtures/ai_edit_v3/providers/dashscope`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): add fixed qwen multimodal provider"`.

### Task 6: Build and validate the semantic director plan

**Files:**

- Create: `server/content_domains/ai_edit_v3/director.py`
- Modify: `server/content_domains/ai_edit_v3/contracts.py`
- Create: `tests/test_ai_edit_v3_director.py`
- Create: `tests/fixtures/ai_edit_v3/edit-plans/`

**Interfaces:**

```python
def build_director_request(source: PreparedSource, timeline: TextTimeline,
                           keyframes: Sequence[Keyframe],
                           material_descriptors: Sequence[MaterialDescriptor],
                           frozen_capabilities: Mapping[str, Any]) -> DirectorRequest: ...
def extract_single_json(raw: str | bytes) -> Mapping[str, Any]: ...
def validate_visible_text(value: Mapping[str, Any],
                          captions: Mapping[str, Caption]) -> None: ...
def validate_edit_plan(plan: Any, *, timeline: TextTimeline,
                       capabilities: Mapping[str, Any]) -> dict[str, Any]: ...
def generate_edit_plan(context: DirectorContext,
                       provider: DashScopeMultimodalClient) -> ValidatedPlan: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.director import DirectorError, generate_edit_plan


class FakeDirector:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def generate_plan(self, request: object, **kwargs: object) -> dict[str, object]:
        self.calls.append({"request": request, **kwargs})
        return self.responses[len(self.calls) - 1]


class DirectorContractTests(unittest.TestCase):
    def test_invalid_primary_gets_one_repair_and_invalid_repair_fails(self) -> None:
        provider = FakeDirector([{"version": "bad"}, {"version": "still-bad"}])
        context = SimpleNamespace(
            request={"transcript_sha256": "abc"},
            timeline=SimpleNamespace(duration_ms=1000),
            capabilities={},
            job_id="j1",
            deadline_at=100.0,
        )

        with self.assertRaisesRegex(DirectorError, "director_schema_invalid"):
            generate_edit_plan(context, provider)

        self.assertEqual(
            [call["purpose"] for call in provider.calls],
            ["initial", "repair"],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_director.py`. Create `tests/fixtures/ai_edit_v3/edit-plans/invalid-version.json` containing exactly `{"version":"bad"}`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_director.DirectorContractTests.test_invalid_primary_gets_one_repair_and_invalid_repair_fails -v`. Expected: `ERROR` for missing `director`, `DirectorError`, or `generate_edit_plan`; fake-provider, fixture, or syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/director.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class DirectorError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedPlan:
    value: Mapping[str, Any]


def validate_edit_plan(plan: Any, *, timeline: Any,
                       capabilities: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("version") != "2.0":
        raise DirectorError("director_schema_invalid")
    return dict(plan)


def generate_edit_plan(context: Any, provider: Any) -> ValidatedPlan:
    for purpose in ("initial", "repair"):
        raw = provider.generate_plan(
            context.request,
            purpose=purpose,
            idempotency_key=f"ai-edit-v3:{context.job_id}:director:{purpose}",
            deadline_at=context.deadline_at,
        )
        try:
            return ValidatedPlan(
                validate_edit_plan(
                    raw,
                    timeline=context.timeline,
                    capabilities=context.capabilities,
                )
            )
        except DirectorError:
            continue
    raise DirectorError("director_schema_invalid")
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_director.DirectorContractTests.test_invalid_primary_gets_one_repair_and_invalid_repair_fails -v`. Expected: `Ran 1 test`, exactly two fake-provider calls, and `OK`.
- [ ] **Step 5: Harden JSON extraction by TDD.** Add one complete test per boundary and run individual RED/GREEN cycles for 512 KiB maximum, depth 24, 5000 aggregate array elements, 4000-character strings, duplicate keys, `NaN`, `Infinity`, multiple objects, trailing text, Markdown fences, and executable-field smuggling. Implement `extract_single_json` using strict UTF-8 decoding, duplicate-key detection, bounded traversal, and rejection of non-whitespace trailing bytes.
- [ ] **Step 6: Enforce Schema and cross-field rules by TDD.** Add complete tests for `additionalProperties`, contiguous scenes, exact duration/ratio, caption/material references, registered layout/animation/transition/audio-cue values, and legal IDs. Make `validate_edit_plan` invoke the Phase A Draft 2020-12 validator before semantic checks and return canonical normalized data only.
- [ ] **Step 7: Enforce visible-text and injection rules by TDD.** Add tests for contiguous `verbatim`, fact-preserving `compressed`, compiler-owned `ui_label`, and rejection of changed price/brand/person/negation/causal direction or added promise. Add transcript/style-prompt injection cases requesting filesystem paths, keys, COS identifiers, arbitrary CSS, or unregistered components; every case must fail with a stable redacted path/code.
- [ ] **Step 8: Complete repair and persistence semantics.** Build the initial request deterministically from frozen source, timeline, keyframes, descriptors, and capabilities. On failure, the repair request contains only redacted error code/path plus the same frozen facts/capabilities; it cannot add facts/assets. Accept one valid repair, reject a second invalid response, persist raw output, normalized plan, schema hashes, request metadata, and provider request ID via Phase A store methods, and never persist chain-of-thought. Implement all functions in **Interfaces** with exact signatures.
- [ ] **Step 9: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_director tests.test_ai_edit_v3_contracts tests.test_ai_edit_v3_schemas -v`, then `python -m unittest tests.test_ai_edit_v2_director tests.test_ai_edit_v2_schema -v`; expect `OK` and exit `0`.
- [ ] **Step 10: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/director.py server/content_domains/ai_edit_v3/contracts.py tests/test_ai_edit_v3_director.py tests/fixtures/ai_edit_v3/edit-plans`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): generate schema-bound director plans"`.

### Task 7: Analyze and bind only current-task images

**Files:**

- Create: `server/content_domains/ai_edit_v3/materials.py`
- Create: `tests/test_ai_edit_v3_materials.py`
- Create: `tests/fixtures/ai_edit_v3/materials/`

**Interfaces:**

```python
@dataclass(frozen=True)
class MaterialDescriptor:
    material_id: str
    semantic: tuple[str, ...]
    subject_type: str
    composition: str
    supported_ratios: tuple[str, ...]
    risk_labels: tuple[str, ...]
    sha256: str

@dataclass(frozen=True)
class ResolvedMaterial:
    slot_id: str
    source: Literal["current_upload", "generated", "omitted_optional"]
    material_id: str | None
    cos_key: str | None
    match_score: float | None
    reason: str

def analyze_current_images(job: Mapping[str, Any], context: StageContext,
                           provider: DashScopeMultimodalClient) -> tuple[MaterialDescriptor, ...]: ...
def resolve_uploaded_materials(plan: ValidatedPlan,
                               descriptors: Sequence[MaterialDescriptor]) -> ResolutionDraft: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.materials import (
    MaterialDescriptor,
    resolve_uploaded_materials,
)


class MaterialContractTests(unittest.TestCase):
    def test_only_semantically_matching_current_image_binds(self) -> None:
        plan = SimpleNamespace(
            material_slots=(
                {"id": "slot_product", "semantic": ["product"], "required": True},
                {"id": "slot_store", "semantic": ["store"], "required": True},
            )
        )
        current = MaterialDescriptor(
            material_id="image-1",
            semantic=("product",),
            subject_type="product",
            composition="center",
            supported_ratios=("9:16",),
            risk_labels=(),
            sha256="abc",
        )

        result = resolve_uploaded_materials(plan, [current])

        self.assertEqual(result.slots["slot_product"].material_id, "image-1")
        self.assertEqual(result.slots["slot_store"].status, "generation_required")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_materials.py`. Create `tests/fixtures/ai_edit_v3/materials/empty.json` containing exactly `{}`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_materials.MaterialContractTests.test_only_semantically_matching_current_image_binds -v`. Expected: `ERROR` naming missing `materials`, `MaterialDescriptor`, or `resolve_uploaded_materials`; no repository or fixture error is valid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `server/content_domains/ai_edit_v3/materials.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence


@dataclass(frozen=True)
class MaterialDescriptor:
    material_id: str
    semantic: tuple[str, ...]
    subject_type: str
    composition: str
    supported_ratios: tuple[str, ...]
    risk_labels: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class ResolvedMaterial:
    slot_id: str
    source: Literal["current_upload", "generated", "omitted_optional"] | None
    material_id: str | None
    cos_key: str | None
    match_score: float | None
    reason: str
    status: str


@dataclass(frozen=True)
class ResolutionDraft:
    slots: Mapping[str, ResolvedMaterial]


def resolve_uploaded_materials(plan: Any,
                               descriptors: Sequence[MaterialDescriptor]) -> ResolutionDraft:
    slots: dict[str, ResolvedMaterial] = {}
    for slot in plan.material_slots:
        required_semantic = set(slot["semantic"])
        match = next(
            (item for item in descriptors if required_semantic.intersection(item.semantic)),
            None,
        )
        if match is None:
            slots[slot["id"]] = ResolvedMaterial(
                slot_id=slot["id"], source=None, material_id=None, cos_key=None,
                match_score=None, reason="no_relevant_current_image",
                status="generation_required" if slot["required"] else "omitted_optional",
            )
        else:
            slots[slot["id"]] = ResolvedMaterial(
                slot_id=slot["id"], source="current_upload",
                material_id=match.material_id, cos_key=None, match_score=1.0,
                reason="semantic_exact", status="resolved",
            )
    return ResolutionDraft(slots=slots)
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_materials.MaterialContractTests.test_only_semantically_matching_current_image_binds -v`. Expected: `Ran 1 test` and `OK`.
- [ ] **Step 5: Enforce task ownership by TDD.** Add complete fake-repository tests proving `analyze_current_images` performs exactly one owner-scoped lookup by this job's immutable `material_asset_ids`; reject undeclared IDs, previous-task/history/public assets, video, audio, and another owner's images before Qwen. Run each test RED then GREEN.
- [ ] **Step 6: Bound multimodal analysis by TDD.** Test at most ten total images, batches of at most five, thumbnails no larger than 768 px, frozen `qwen3.7-max-2026-06-08`, and at most six semantically selected director thumbnails. Use the Task 5 fake transport and assert exact request count/body metadata without live calls.
- [ ] **Step 7: Complete deterministic matching by TDD.** Add case-table tests for semantic, scene intent, ratio, time range, product/person fidelity, non-adjacent reuse reason, stable score weights, deterministic tie-breaking, and shuffled input order. Unrelated input must remain unresolved; required slots become `generation_required`; optional slots become `omitted_optional` with a registered fallback variant.
- [ ] **Step 8: Complete declared interfaces.** Implement `analyze_current_images` and the final typed `resolve_uploaded_materials`; normalize image metadata, redact analysis input/output, persist auditable source decisions, and remove the anchor's first-match shortcut. Every resolution must include score components and provenance limited to this job.
- [ ] **Step 9: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_materials -v`, then `python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_materials -v`; expect `OK` and exit `0`.
- [ ] **Step 10: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/materials.py tests/test_ai_edit_v3_materials.py tests/fixtures/ai_edit_v3/materials`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): resolve current task images only"`.

### Task 8: Generate safe missing images through the existing site service

**Files:**

- Create: `server/content_domains/ai_edit_v3/providers/image_generation.py`
- Modify: `server/content_domains/ai_edit_v3/materials.py`
- Create: `tests/test_ai_edit_v3_image_generation.py`

**Interfaces:**

```python
class ImageGenerationProvider(Protocol):
    def submit(self, request: ImageGenerationRequest,
               *, idempotency_key: str, deadline_at: float) -> ProviderResult: ...
    def query(self, request_id: str, *, deadline_at: float) -> ProviderResult: ...

def generate_required_materials(job: Mapping[str, Any], plan: ValidatedPlan,
                                draft: ResolutionDraft,
                                provider: ImageGenerationProvider,
                                context: StageContext) -> ResolvedMaterials: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.materials import (
    ResolutionDraft,
    ResolvedMaterial,
    generate_required_materials,
)


class FakeImageProvider:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit(self, request: dict[str, object], **kwargs: object) -> SimpleNamespace:
        self.submissions.append({"request": request, **kwargs})
        return SimpleNamespace(
            request_id="image-request-1",
            cos_key="test/ai-edit-v3/jobs/j1/generated/slot_01.webp",
            asset_id="generated-1",
        )


class ImageGenerationContractTests(unittest.TestCase):
    def test_required_missing_slot_generates_once_and_keeps_private_key(self) -> None:
        missing = ResolvedMaterial(
            slot_id="slot_01", source=None, material_id=None, cos_key=None,
            match_score=None, reason="no_relevant_current_image",
            status="generation_required",
        )
        provider = FakeImageProvider()

        resolved = generate_required_materials(
            {"id": "j1"},
            SimpleNamespace(material_slots=({"id": "slot_01", "semantic": ["store"]},)),
            ResolutionDraft(slots={"slot_01": missing}),
            provider,
            SimpleNamespace(deadline_at=100.0),
        )

        self.assertEqual(len(provider.submissions), 1)
        self.assertEqual(resolved["slot_01"].source, "generated")
        self.assertTrue(resolved["slot_01"].cos_key.startswith("test/ai-edit-v3/"))
        self.assertNotIn("http", json.dumps(resolved, default=lambda value: value.__dict__))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_image_generation.py`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_image_generation.ImageGenerationContractTests.test_required_missing_slot_generates_once_and_keeps_private_key -v`. Expected: `ImportError` naming missing `generate_required_materials`; a JSON serialization, fake-provider, or fixture error is invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Append to `server/content_domains/ai_edit_v3/materials.py`:

```python
def generate_required_materials(job: Mapping[str, Any], plan: Any,
                                draft: ResolutionDraft, provider: Any,
                                context: Any) -> dict[str, ResolvedMaterial]:
    plan_slots = {slot["id"]: slot for slot in plan.material_slots}
    resolved = dict(draft.slots)
    for slot_id, current in draft.slots.items():
        if current.status != "generation_required":
            continue
        result = provider.submit(
            {
                "semantic": list(plan_slots[slot_id]["semantic"]),
                "slot_id": slot_id,
            },
            idempotency_key=f"ai-edit-v3:{job['id']}:image:{slot_id}",
            deadline_at=context.deadline_at,
        )
        resolved[slot_id] = ResolvedMaterial(
            slot_id=slot_id,
            source="generated",
            material_id=str(result.asset_id),
            cos_key=str(result.cos_key),
            match_score=None,
            reason="required_slot_generated",
            status="resolved",
        )
    return resolved
```

Create `server/content_domains/ai_edit_v3/providers/image_generation.py` with:

```python
from __future__ import annotations

from typing import Any, Protocol


class ImageGenerationProvider(Protocol):
    def submit(self, request: Any, *, idempotency_key: str,
               deadline_at: float) -> Any:
        raise NotImplementedError

    def query(self, request_id: str, *, deadline_at: float) -> Any:
        raise NotImplementedError
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_image_generation.ImageGenerationContractTests.test_required_missing_slot_generates_once_and_keeps_private_key -v`. Expected: `Ran 1 test`, one fake submission, and `OK`.
- [ ] **Step 5: Freeze generation eligibility by TDD.** Add complete tests proving generation happens only after current-upload matching and only for unresolved required slots; optional slots use `omitted_optional` unless the frozen plan explicitly budgets generation. Each case gets an individual RED command, minimum change, and GREEN rerun.
- [ ] **Step 6: Freeze prompt safety by TDD.** Assert the request contains only scene semantic, registered theme tokens, ratio, and immutable fact boundaries. Reject real-customer/store invention, product-proof claims, performance evidence, inaccurate branded packaging, arbitrary URLs, and prompt instructions copied from transcript/material metadata.
- [ ] **Step 7: Freeze recovery and storage by TDD.** Add fake-provider/store cases for intent-before-submit, stable idempotency key, response loss, query by request ID, immutable generated-asset record, decode/quality checks, product-fidelity failure, private `test/ai-edit-v3/` COS key, and absence of short/signed URLs from DB, Qwen input, and audit.
- [ ] **Step 8: Freeze failure behavior by TDD.** Unsafe or irrecoverable required images must raise a rendering-blocking error; optional failure records `omitted_optional`; an injected AI-video spy must record zero calls. Replace the anchor-only loop with these audited transitions and implement the exact protocol/signatures in **Interfaces**.
- [ ] **Step 9: Run task and regression suites.** Run `python -m unittest tests.test_ai_edit_v3_image_generation tests.test_ai_edit_v3_materials -v`, then `python -m unittest tests.test_ai_edit_v2_openai_image -v`; expect `OK` and exit `0`.
- [ ] **Step 10: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/providers/image_generation.py server/content_domains/ai_edit_v3/materials.py tests/test_ai_edit_v3_image_generation.py`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): generate safe missing images"`.

### Task 9: Wire Phase B checkpoints into the pipeline

**Files:**

- Modify: `server/content_domains/ai_edit_v3/pipeline.py`
- Modify: `server/content_domains/ai_edit_v3/runtime.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Modify: `server/ai_edit_v3_worker.py`
- Create: `tests/test_ai_edit_v3_phase_b_pipeline.py`

**Interfaces:**

```python
def run_source_and_director_stages(claim: LeaseClaim,
                                   runtime: RuntimeDependencies,
                                   *, db_path: Path) -> StageResult: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.pipeline import run_source_and_director_stages


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def run_phase_b_stage(self, name: str, claim: object, db_path: Path) -> None:
        self.calls.append((name, claim.fencing_token))


class PhaseBPipelineContractTests(unittest.TestCase):
    def test_stages_are_checkpointed_in_frozen_order(self) -> None:
        runtime = FakeRuntime()
        claim = SimpleNamespace(job_id="j1", fencing_token=7)

        result = run_source_and_director_stages(
            claim,
            runtime,
            db_path=Path("ai_edit_v3.db"),
        )

        self.assertEqual(
            [name for name, _token in runtime.calls],
            [
                "generating_voice", "normalizing", "transcribing", "aligning",
                "planning", "resolving_materials", "generating_images",
            ],
        )
        self.assertTrue(all(token == 7 for _name, token in runtime.calls))
        self.assertEqual(result.next_stage, "rendering")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_phase_b_pipeline.py`.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_phase_b_pipeline.PhaseBPipelineContractTests.test_stages_are_checkpointed_in_frozen_order -v`. Expected: `ImportError` for missing `run_source_and_director_stages`; fake runtime, path, or syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Append to `server/content_domains/ai_edit_v3/pipeline.py`:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StageResult:
    next_stage: str


_PHASE_B_STAGES = (
    "generating_voice",
    "normalizing",
    "transcribing",
    "aligning",
    "planning",
    "resolving_materials",
    "generating_images",
)


def run_source_and_director_stages(claim: Any, runtime: Any,
                                   *, db_path: Path) -> StageResult:
    for stage_name in _PHASE_B_STAGES:
        runtime.run_phase_b_stage(stage_name, claim, db_path)
    return StageResult(next_stage="rendering")
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_phase_b_pipeline.PhaseBPipelineContractTests.test_stages_are_checkpointed_in_frozen_order -v`. Expected: `Ran 1 test`, all seven stages in order with fencing token `7`, and `OK`.
- [ ] **Step 5: Replace the loop with real checkpoint handlers by TDD.** Add a fake-provider integration table for the exact seven stages, including `skipped` for TTS/image work not needed. Implement a handler map in `runtime.py`; each handler calls the Task 1–8 boundary, then invokes a typed store checkpoint method with the current `LeaseClaim`. Pipeline remains the only stage-transition writer.
- [ ] **Step 6: Add restart safety by TDD.** For every provider, test a crash immediately before submit, after submit/before request-ID bind, after request-ID bind/before result, and after result/before stage completion. Restart must use immutable fingerprint/request ID and never duplicate TTS, ASR, Qwen, or image submission. `SubmissionUnknown` follows Phase A reconciliation and never blind-resubmits.
- [ ] **Step 7: Add fencing and deadline safety by TDD.** Add a stale-claim case for every store write: keyframe, transcript, model call, plan, material decision, and stage completion. Add queue-expired, absolute 45-minute deadline across restart, lease loss, provider-unknown, and refund-path tests. Each store method must execute a compare-and-update on job ID plus fencing token and raise `StaleLeaseError("fencing_token_stale")` when rowcount is zero.
- [ ] **Step 8: Wire worker capability gating.** Add tests proving a disabled/not-ready worker only reconciles safe outboxes and never claims media work; a ready worker claims once and invokes `run_source_and_director_stages`. Add exact runtime provider dependencies and store methods to `runtime.py`, `store.py`, and `ai_edit_v3_worker.py`; remove the anchor-only `run_phase_b_stage` hook after concrete handlers are green.
- [ ] **Step 9: Run task suites.** Run `python -m unittest tests.test_ai_edit_v3_phase_b_pipeline tests.test_ai_edit_v3_pipeline tests.test_ai_edit_v3_worker -v`; expected all tests `OK` and exit `0`.
- [ ] **Step 10: Commit only this task.** Run `git add server/content_domains/ai_edit_v3/pipeline.py server/content_domains/ai_edit_v3/runtime.py server/content_domains/ai_edit_v3/store.py server/ai_edit_v3_worker.py tests/test_ai_edit_v3_phase_b_pipeline.py`, `git diff --cached --check`, and `git commit -m "feat(ai-edit-v3): orchestrate director and material stages"`.

### Task 10: Complete the Phase B gate

**Files:**

- Create: `tests/fixtures/ai_edit_v3/phase-b-cases.json`
- Create: `scripts/ai_edit_v3_phase_b_gate.py`
- Create: `tests/test_ai_edit_v3_phase_b_gate.py`
- Create: `docs/verification/ai-edit-v3-phase-b.md`

**Interfaces:**

```python
def validate_phase_b_cases(path: Path) -> PhaseBGateReport: ...
def collect_phase_b_capabilities(runtime: RuntimeDependencies) -> tuple[CapabilityEvidence, ...]: ...
```

**Required RED anchor:**

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.ai_edit_v3_phase_b_gate import validate_phase_b_cases


class PhaseBGateContractTests(unittest.TestCase):
    def test_gate_reports_the_exact_missing_required_outcome(self) -> None:
        payload = {
            "cases": [
                {"outcome": "all_input_types"},
                {"outcome": "all_creation_modes"},
                {"outcome": "authoritative_text"},
                {"outcome": "punctuation_only"},
                {"outcome": "material_matching"},
                {"outcome": "injection_rejected"},
                {"outcome": "invalid_model_rejected"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = validate_phase_b_cases(path)

        self.assertFalse(report.passed)
        self.assertEqual(report.missing, ("required_material_failure",))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1: Create the complete RED test.** Save the code above as `tests/test_ai_edit_v3_phase_b_gate.py`. Create `tests/fixtures/ai_edit_v3/phase-b-cases.json` with `{"cases":[]}` on one line; the test uses a temporary file so RED cannot be caused by this final matrix being incomplete.
- [ ] **Step 2: Run RED.** Run `python -m unittest tests.test_ai_edit_v3_phase_b_gate.PhaseBGateContractTests.test_gate_reports_the_exact_missing_required_outcome -v`. Expected: `ModuleNotFoundError` or `ImportError` naming `scripts.ai_edit_v3_phase_b_gate` or `validate_phase_b_cases`; temp-file, JSON, or syntax errors are invalid RED.
- [ ] **Step 3: Add the complete minimum GREEN implementation.** Create `scripts/ai_edit_v3_phase_b_gate.py` with:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_OUTCOMES = (
    "all_input_types",
    "all_creation_modes",
    "authoritative_text",
    "punctuation_only",
    "material_matching",
    "required_material_failure",
    "injection_rejected",
    "invalid_model_rejected",
)


@dataclass(frozen=True)
class PhaseBGateReport:
    passed: bool
    missing: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityEvidence:
    name: str
    status: str


def validate_phase_b_cases(path: Path) -> PhaseBGateReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    present = {str(case["outcome"]) for case in payload["cases"]}
    missing = tuple(name for name in REQUIRED_OUTCOMES if name not in present)
    return PhaseBGateReport(passed=not missing, missing=missing)


def collect_phase_b_capabilities(runtime: Any) -> tuple[CapabilityEvidence, ...]:
    return tuple(
        CapabilityEvidence(name=name, status=str(runtime.capability_status(name)))
        for name in ("ffmpeg", "fun_asr", "dashscope", "tts", "image_generation", "cos")
    )
```

- [ ] **Step 4: Run GREEN.** Run `python -m unittest tests.test_ai_edit_v3_phase_b_gate.PhaseBGateContractTests.test_gate_reports_the_exact_missing_required_outcome -v`. Expected: `Ran 1 test` and `OK`.
- [ ] **Step 5: Build the complete non-secret matrix by TDD.** Populate `phase-b-cases.json` with deterministic fixture IDs covering every one of the five input types, all three creation modes, authoritative platform/TTS text, external punctuation-only text, no/relevant/unrelated images, required missing-image success and failure, optional omission, injection rejection, and invalid-model response. Expand the validator to check explicit dimensions and duplicate case IDs. First run the new completeness test RED; rerun after implementation and require GREEN.
- [ ] **Step 6: Verify the full Phase B suite.** Run `python -m unittest tests.test_ai_edit_v3_media tests.test_ai_edit_v3_source tests.test_ai_edit_v3_tts tests.test_ai_edit_v3_asr tests.test_ai_edit_v3_transcript tests.test_ai_edit_v3_source_map tests.test_ai_edit_v3_dashscope tests.test_ai_edit_v3_director tests.test_ai_edit_v3_materials tests.test_ai_edit_v3_image_generation tests.test_ai_edit_v3_phase_b_pipeline tests.test_ai_edit_v3_phase_b_gate -v`; expected exit `0`. Then run `python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v`; expected exit `0`.
- [ ] **Step 7: Verify V2 and repository gates.** Run `python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v`, `node --test tests/test_ai_edit_v2_ui.js`, `python scripts/ci_validate.py`, `python scripts/stamp_assets.py --check`, and `git diff --check`; every command must exit `0`.
- [ ] **Step 8: Run the PowerShell-safe forbidden-dependency scan.** Run this exact PowerShell block; it collects files explicitly and splats them, so no shell glob is passed to `rg`:

```powershell
$phaseBTests = @(
    Get-ChildItem -LiteralPath 'tests' -File -Filter 'test_ai_edit_v3_*.py' |
        ForEach-Object { $_.FullName }
)
$scanPaths = @((Resolve-Path 'server/content_domains/ai_edit_v3').Path) + $phaseBTests
& rg -n 'qwen-plus|qwen3\.7-plus|text-generation|ai_edit_v2|history|public material' @scanPaths
if ($LASTEXITCODE -notin @(0, 1)) {
    throw "rg scan failed with exit code $LASTEXITCODE"
}
```

Review every match. Runtime matches are forbidden. Test-only negative assertions are allowed only when their exact file, line, and reason are recorded in the verification document.
- [ ] **Step 9: Write reproducible evidence.** Create `docs/verification/ai-edit-v3-phase-b.md` containing every exact command, exit code, test count, elapsed time, Schema hash, matrix result, scan exception, and a table with three disjoint statuses: `implemented`, `configured_and_wired`, and `missing_or_unavailable`. Redact credentials, signed URLs, transcript bodies, image bytes, and provider payloads.
- [ ] **Step 10: Commit only this task.** Run `git add scripts/ai_edit_v3_phase_b_gate.py tests/test_ai_edit_v3_phase_b_gate.py tests/fixtures/ai_edit_v3/phase-b-cases.json docs/verification/ai-edit-v3-phase-b.md`, `git diff --cached --check`, and `git commit -m "test(ai-edit-v3): verify phase b director pipeline"`.

## Phase B Definition of Done

- [ ] Every input type yields a normalized media source and accurate millisecond timeline without altering protected facts.
- [ ] Qwen calls only the frozen Beijing Workspace multimodal endpoint/model and emits a valid edit-plan after at most one repair.
- [ ] Every visible non-caption text item is machine-traceable to accurate captions or a compiler-owned UI label.
- [ ] Every resolved material is either a current-task image, a safely generated private image, or an explicit optional omission; unrelated and historical assets are impossible by construction.
- [ ] Phase B provider calls are restart-safe, lease-fenced, deadline-bounded and cost-audited.
- [ ] Phase A and all V2 tests remain green; no Node renderer, UI, deployment or production change is included.
