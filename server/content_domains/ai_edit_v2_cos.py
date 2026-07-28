"""Dedicated private Tencent COS adapter for AI Edit V2."""

from __future__ import annotations

import os
import re

_SECRET_ID = os.environ.get("AI_EDIT_V2_COS_SECRET_ID", "").strip()
_SECRET_KEY = os.environ.get("AI_EDIT_V2_COS_SECRET_KEY", "").strip()
_REGION = os.environ.get("AI_EDIT_V2_COS_REGION", "").strip()
_BUCKET = os.environ.get("AI_EDIT_V2_COS_BUCKET", "").strip()
_PREFIX = os.environ.get("AI_EDIT_V2_COS_PREFIX", "").strip().strip("/")
_client_singleton = None
_REL_KEY_RE = re.compile(r"^ai-edit-v2/[0-9a-f]{16,64}/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/[A-Za-z0-9._/-]+$")
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def enabled() -> bool:
    return bool(_SECRET_ID and _SECRET_KEY and _REGION and _BUCKET)


def _client():
    global _client_singleton
    if _client_singleton is None:
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(Region=_REGION, SecretId=_SECRET_ID, SecretKey=_SECRET_KEY, Scheme="https")
        _client_singleton = CosS3Client(config)
    return _client_singleton


def _object_key(rel_key: str) -> str:
    if (not isinstance(rel_key, str) or rel_key.startswith(("/", "\\")) or "\\" in rel_key
            or "?" in rel_key or "#" in rel_key or ":" in rel_key
            or any(part in ("", ".", "..") for part in rel_key.split("/"))
            or not _REL_KEY_RE.fullmatch(rel_key)):
        raise ValueError("COS object key is outside the AI Edit V2 scope")
    return f"{_PREFIX}/{rel_key}" if _PREFIX else rel_key


def _require_enabled() -> None:
    if not enabled():
        raise RuntimeError("AI Edit V2 private COS is not configured")


def presign_put(rel_key: str, content_type: str, expires: int = 900) -> str:
    _require_enabled()
    if not isinstance(expires, int) or isinstance(expires, bool) or not 1 <= expires <= 900:
        raise ValueError("PUT signature expiry must be between 1 and 900 seconds")
    if not isinstance(content_type, str) or not _CONTENT_TYPE_RE.fullmatch(content_type):
        raise ValueError("invalid Content-Type")
    return _client().get_presigned_url(Method="PUT", Bucket=_BUCKET, Key=_object_key(rel_key),
        Expired=expires, Headers={"Content-Type": content_type, "x-cos-acl": "private"})


def presign_get(rel_key: str, expires: int = 300) -> str:
    _require_enabled()
    if not isinstance(expires, int) or isinstance(expires, bool) or not 1 <= expires <= 900:
        raise ValueError("GET signature expiry must be between 1 and 900 seconds")
    return _client().get_presigned_url(
        Method="GET", Bucket=_BUCKET, Key=_object_key(rel_key), Expired=expires
    )


def head_object(rel_key: str) -> dict[str, object]:
    _require_enabled()
    response = _client().head_object(Bucket=_BUCKET, Key=_object_key(rel_key))
    normalized = {str(key).lower(): value for key, value in response.items()}
    return {"content_length": int(normalized.get("content-length", normalized.get("content_length"))),
        "content_type": str(normalized.get("content-type", normalized.get("content_type")) or ""),
        "etag": str(normalized.get("etag") or "").strip('"')}


def download_file(rel_key: str, destination) -> str:
    _require_enabled()
    _client().download_file(Bucket=_BUCKET, Key=_object_key(rel_key), DestFilePath=os.fspath(destination))
    return os.fspath(destination)


def put_bytes(content: bytes, rel_key: str, content_type: str, private: bool = True):
    _require_enabled()
    if not isinstance(content, bytes) or not content:
        raise ValueError("COS upload content must be non-empty bytes")
    if not isinstance(content_type, str) or not _CONTENT_TYPE_RE.fullmatch(content_type):
        raise ValueError("invalid Content-Type")
    if private is not True:
        raise ValueError("AI Edit V2 COS objects must be private")
    return _client().put_object(
        Bucket=_BUCKET,
        Key=_object_key(rel_key),
        Body=content,
        ContentType=content_type,
        ACL="private",
    )


def put_file(source, rel_key: str, content_type: str, private: bool = True):
    """Upload a normalized local file through the same private COS boundary."""
    if private is not True:
        raise ValueError("AI Edit V2 COS objects must be private")
    with open(os.fspath(source), "rb") as handle:
        content = handle.read()
    return put_bytes(content, rel_key, content_type, private=True)


def delete_object(rel_key: str):
    _require_enabled()
    return _client().delete_object(Bucket=_BUCKET, Key=_object_key(rel_key))
