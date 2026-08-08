from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status

from api import external_audio


router = APIRouter(tags=["Voice Assets"])


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
    content = await request.body()
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
