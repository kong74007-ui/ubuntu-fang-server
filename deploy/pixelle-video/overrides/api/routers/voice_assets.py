from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status

from api import external_audio


router = APIRouter(tags=["Voice Assets"])


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="invalid Content-Length")
    return value


async def _read_bounded_body(request: Request) -> bytes:
    declared = _content_length(request)
    if declared is not None and declared > external_audio.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio body exceeds size limit")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > external_audio.MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio body exceeds size limit")
        content.extend(chunk)
    return bytes(content)


@router.get("/voices/public")
async def list_public_voices():
    return {"items": external_audio.public_voice_catalog()}


@router.post("/audio-assets", status_code=status.HTTP_201_CREATED)
async def upload_audio_asset(
    request: Request,
    request_id: str | None = Header(None, alias="X-Request-Id"),
):
    if not request_id:
        raise HTTPException(status_code=400, detail="X-Request-Id is required")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content = await _read_bounded_body(request)
    try:
        return external_audio.store_audio_asset(content, content_type, request_id)
    except external_audio.AudioTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except external_audio.AudioProbeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="audio storage failed") from exc
