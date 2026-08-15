from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status

from api import avatar_assets


router = APIRouter(tags=["Avatar Assets"])


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
    if declared is not None and declared > avatar_assets.MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="avatar body exceeds size limit")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > avatar_assets.MAX_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="avatar body exceeds size limit")
        content.extend(chunk)
    return bytes(content)


@router.post("/avatar-assets", status_code=status.HTTP_201_CREATED)
async def upload_avatar_asset(
    request: Request,
    request_id: str | None = Header(None, alias="X-Request-Id"),
):
    if not request_id:
        raise HTTPException(status_code=400, detail="X-Request-Id is required")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content = await _read_bounded_body(request)
    try:
        return avatar_assets.store_avatar_asset(content, content_type, request_id)
    except avatar_assets.AvatarTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="avatar storage failed") from exc
