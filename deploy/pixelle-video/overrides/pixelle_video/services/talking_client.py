"""Loopback-only client for Huangque's internal talking-clip bridge."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable


DEFAULT_ENDPOINT = "http://127.0.0.1:8096/api/internal/pixelle/talking-clip"
BRIDGE_PATH = "/api/internal/pixelle/talking-clip"
SOCKET_TIMEOUT_SECONDS = 20 * 60
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2, 5)

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


class TalkingResult:
    def __init__(
        self,
        video_path: str,
        provider_video_id: str,
        attempts: int,
        warnings: list[str] | None = None,
    ) -> None:
        self.video_path = video_path
        self.provider_video_id = provider_video_id
        self.attempts = attempts
        self.warnings = list(warnings or [])


class TalkingClipError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool,
        billed: bool,
        attempts: int,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable
        self.billed = billed
        self.attempts = attempts


class TalkingClient:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        opener: Callable | None = None,
        sleeper: Callable | None = None,
        process_runner: Callable | None = None,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        resolved_endpoint = str(
            endpoint
            if endpoint is not None
            else os.environ.get("PIXELLE_TALKING_ENDPOINT", DEFAULT_ENDPOINT)
        ).strip()
        parsed = urllib.parse.urlsplit(resolved_endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("talking bridge endpoint must be loopback HTTP")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("talking bridge endpoint has an invalid port") from error
        if parsed.path != BRIDGE_PATH:
            raise ValueError("talking bridge endpoint has an invalid path")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("talking bridge endpoint must not contain credentials or parameters")
        internal_token = str(
            token if token is not None else os.environ.get("PIXELLE_TALKING_INTERNAL_TOKEN", "")
        ).strip()
        if not internal_token:
            raise ValueError("talking bridge internal token is required")

        self.endpoint = resolved_endpoint
        self.token = internal_token
        self._opener = opener or urllib.request.urlopen
        self._sleeper = sleeper or asyncio.sleep
        self._process_runner = process_runner or subprocess.run
        self._ffmpeg_path = ffmpeg_path

    async def generate(
        self,
        image_path: str,
        audio_path: str,
        output_path: str,
        request_id: str,
        ratio: str,
    ) -> TalkingResult:
        image = Path(image_path)
        audio = Path(audio_path)
        output = Path(output_path)
        payload = self._build_payload(image, audio, request_id, ratio)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                provider_bytes, provider_video_id = await asyncio.to_thread(
                    self._request_once, payload
                )
                staged_output = output.with_name(
                    f".{output.name}.{uuid.uuid4().hex}.staged.mp4"
                )
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self._write_silent_video,
                        provider_bytes,
                        staged_output,
                    )
                )
                try:
                    await asyncio.shield(worker)
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError
                    os.replace(staged_output, output)
                except asyncio.CancelledError:
                    def cleanup_cancelled_output(_worker) -> None:
                        staged_output.unlink(missing_ok=True)
                        output.unlink(missing_ok=True)

                    worker.add_done_callback(cleanup_cancelled_output)
                    try:
                        await asyncio.shield(worker)
                    except Exception:
                        pass
                    staged_output.unlink(missing_ok=True)
                    output.unlink(missing_ok=True)
                    raise
                finally:
                    staged_output.unlink(missing_ok=True)
                return TalkingResult(
                    str(output), provider_video_id, attempt, warnings=[]
                )
            except TalkingClipError as error:
                error.attempts = attempt
                if not error.retryable or error.billed or attempt >= MAX_ATTEMPTS:
                    raise
                await self._sleeper(RETRY_DELAYS_SECONDS[attempt - 1])

        raise AssertionError("unreachable talking retry state")

    def _build_payload(
        self,
        image_path: Path,
        audio_path: Path,
        request_id: str,
        ratio: str,
    ) -> bytes:
        image_data = image_path.read_bytes()
        audio_data = audio_path.read_bytes()
        if not image_data or not audio_data:
            raise ValueError("talking image and audio must not be empty")
        body = {
            "request_id": str(request_id),
            "image_data": self._data_url(image_path, image_data),
            "audio_data": self._data_url(audio_path, audio_data),
            "image_sha256": hashlib.sha256(image_data).hexdigest(),
            "resolution": "1080p",
            "ratio": str(ratio),
            "motion": "medium",
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _data_url(path: Path, data: bytes) -> str:
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
        if mime is None:
            mime = mimetypes.guess_type(path.name)[0]
        if mime not in set(_MIME_BY_SUFFIX.values()):
            raise ValueError(f"unsupported talking media type: {path.suffix}")
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _request_once(self, payload: bytes) -> tuple[bytes, str]:
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-HQ-Pixelle-Token": self.token,
            },
            method="POST",
        )
        try:
            with self._opener(
                request,
                timeout=SOCKET_TIMEOUT_SECONDS,
            ) as response:
                provider_bytes = response.read()
                provider_video_id = str(
                    response.headers.get("X-Provider-Video-Id", "")
                ).strip()
        except urllib.error.HTTPError as error:
            raise self._http_error(error) from error
        except Exception as error:
            raise TalkingClipError(
                "talking_bridge_transport",
                "talking bridge request failed",
                retryable=False,
                billed=False,
                attempts=1,
            ) from error

        if not provider_bytes or not provider_video_id:
            raise TalkingClipError(
                "talking_bridge_invalid_response",
                "talking bridge returned an incomplete generated result",
                retryable=False,
                billed=True,
                attempts=1,
            )
        return provider_bytes, provider_video_id

    @staticmethod
    def _http_error(error: urllib.error.HTTPError) -> TalkingClipError:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except Exception:
            payload = {}
        retryable = payload.get("retryable") is True
        billed = payload.get("billed") is True
        code = str(payload.get("code") or f"talking_bridge_http_{error.code}")
        detail = str(payload.get("detail") or "talking bridge rejected the request")
        return TalkingClipError(
            code,
            detail,
            retryable=retryable and not billed,
            billed=billed,
            attempts=1,
        )

    def _write_silent_video(self, provider_bytes: bytes, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        provider_path = output.with_name(f".{output.name}.{token}.provider.mp4")
        silent_path = output.with_name(f".{output.name}.{token}.silent.mp4")
        try:
            provider_path.write_bytes(provider_bytes)
            command = [
                self._ffmpeg_path,
                "-y",
                "-i",
                str(provider_path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-movflags",
                "+faststart",
                str(silent_path),
            ]
            try:
                self._process_runner(command, check=True, capture_output=True)
            except Exception as error:
                raise TalkingClipError(
                    "talking_clip_audio_strip_failed",
                    "generated talking clip could not be made silent",
                    retryable=False,
                    billed=True,
                    attempts=1,
                ) from error
            if not silent_path.is_file() or silent_path.stat().st_size <= 0:
                raise TalkingClipError(
                    "talking_clip_audio_strip_failed",
                    "generated talking clip did not produce a silent video",
                    retryable=False,
                    billed=True,
                    attempts=1,
                )
            os.replace(silent_path, output)
        finally:
            provider_path.unlink(missing_ok=True)
            silent_path.unlink(missing_ok=True)
