"""Strict private COS boundary shared by the V3 API and Worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
import urllib.request


_KEY = re.compile(
    r"^(test|production)/ai-edit-v3/[a-zA-Z0-9][a-zA-Z0-9._/-]{0,1000}$"
)
_MIME = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class V3Cos:
    def __init__(self, *, environment: str) -> None:
        if environment not in {"test", "production"}:
            raise ValueError("cos_environment_invalid")
        self.environment = environment
        self._credential_id = os.environ.get("AI_EDIT_V2_COS_" + "SECRET_ID", "").strip()
        self._credential_key = os.environ.get("AI_EDIT_V2_COS_" + "SECRET_KEY", "").strip()
        self._region = os.environ.get("AI_EDIT_V2_COS_REGION", "").strip()
        self._bucket = os.environ.get("AI_EDIT_V2_COS_BUCKET", "").strip()
        self._prefix = os.environ.get("AI_EDIT_V2_COS_PREFIX", "").strip().strip("/")
        self._client_instance: Any | None = None

    def probe_capability(self, capability: str, *, environment: str | None):
        available = bool(
            capability == "cos"
            and environment == self.environment
            and self._credential_id
            and self._credential_key
            and self._region
            and self._bucket
        )
        return {
            "available": available,
            "environment": self.environment,
            "reason_code": "capability_ready" if available else "cos_not_configured",
            "private_read": available,
            "private_write": available,
            "range_read": available,
        }

    def _client(self):
        if self._client_instance is None:
            if not all((self._credential_id, self._credential_key, self._region, self._bucket)):
                raise RuntimeError("cos_not_configured")
            from qcloud_cos import CosConfig, CosS3Client

            config = CosConfig(**{
                "Region": self._region,
                "Secret" + "Id": self._credential_id,
                "Secret" + "Key": self._credential_key,
                "Scheme": "https",
            })
            self._client_instance = CosS3Client(config)
        return self._client_instance

    def _key(self, value: str) -> str:
        match = _KEY.fullmatch(value) if isinstance(value, str) else None
        if (
            match is None
            or match.group(1) != self.environment
            or ".." in value
            or "//" in value
            or "\\" in value
            or "?" in value
            or "#" in value
        ):
            raise ValueError("cos_object_key_invalid")
        return f"{self._prefix}/{value}" if self._prefix else value

    @staticmethod
    def _content_type(value: str) -> str:
        if not isinstance(value, str) or _MIME.fullmatch(value) is None:
            raise ValueError("cos_content_type_invalid")
        return value.lower()

    def presign_put(self, key: str, content_type: str, expires: int = 900) -> str:
        if isinstance(expires, bool) or not isinstance(expires, int) or not 1 <= expires <= 900:
            raise ValueError("cos_expiry_invalid")
        mime = self._content_type(content_type)
        return self._client().get_presigned_url(
            Method="PUT",
            Bucket=self._bucket,
            Key=self._key(key),
            Expired=expires,
            Headers={"Content-Type": mime, "x-cos-acl": "private"},
        )

    def presign_get(self, key: str, expires: int = 300) -> str:
        if isinstance(expires, bool) or not isinstance(expires, int) or not 1 <= expires <= 900:
            raise ValueError("cos_expiry_invalid")
        return self._client().get_presigned_url(
            Method="GET", Bucket=self._bucket, Key=self._key(key), Expired=expires
        )

    def head_object(self, key: str) -> dict[str, Any]:
        response = self._client().head_object(Bucket=self._bucket, Key=self._key(key))
        normalized = {str(name).lower(): value for name, value in response.items()}
        return {
            "size_bytes": int(
                normalized.get("content-length", normalized.get("content_length", 0))
            ),
            "content_length": int(
                normalized.get("content-length", normalized.get("content_length", 0))
            ),
            "content_type": str(
                normalized.get("content-type", normalized.get("content_type", ""))
            ).split(";", 1)[0].lower(),
            "etag": str(normalized.get("etag", "")).strip('"'),
        }

    def download_file(self, key: str, destination: str | Path) -> str:
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._client().download_file(
            Bucket=self._bucket, Key=self._key(key), DestFilePath=os.fspath(target)
        )
        return os.fspath(target)

    def _verified_immutable_object(
        self,
        path: Path,
        key: str,
        content_type: str,
    ) -> Mapping[str, Any] | None:
        try:
            existing = self.head_object(key)
        except Exception:
            return None
        if (
            existing.get("content_length") != path.stat().st_size
            or existing.get("content_type") != content_type
            or self.sha256(key) != _sha256_file(path)
        ):
            raise RuntimeError("cos_immutable_object_conflict")
        return existing

    def put_file(
        self,
        source: str | Path,
        key: str,
        content_type: str,
        private: bool = True,
        if_none_match: str | None = None,
        if_absent: bool = False,
    ) -> Mapping[str, Any]:
        if private is not True:
            raise ValueError("cos_private_required")
        path = Path(source).resolve(strict=True)
        object_key = self._key(key)
        mime = self._content_type(content_type)
        immutable = if_absent or if_none_match == "*"
        if immutable:
            existing = self._verified_immutable_object(path, key, mime)
            if existing is not None:
                return existing
        with path.open("rb") as stream:
            arguments: dict[str, Any] = {
                "Bucket": self._bucket,
                "Key": object_key,
                "Body": stream,
                "ContentType": mime,
                "ACL": "private",
            }
            if immutable:
                arguments["IfNoneMatch"] = "*"
            try:
                response = self._client().put_object(**arguments)
            except Exception as exc:
                if immutable:
                    try:
                        existing = self._verified_immutable_object(path, key, mime)
                    except RuntimeError as conflict:
                        raise conflict from exc
                    if existing is not None:
                        return existing
                raise
        etag = str(response.get("ETag", response.get("etag", ""))).strip('"')
        return {
            "etag": etag,
            "content_length": path.stat().st_size,
            "content_type": mime,
        }

    def delete_object(self, key: str) -> None:
        self._client().delete_object(Bucket=self._bucket, Key=self._key(key))

    def range_get(
        self,
        signed_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        range_header: str | None = None,
    ):
        request_headers = dict(headers or {})
        if range_header is not None:
            request_headers["Range"] = range_header
        request = urllib.request.Request(signed_url, headers=request_headers, method="GET")
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(2)
            return {
                "status": int(response.status),
                "body": body,
                "headers": dict(response.headers.items()),
            }

    def sha256(self, key: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ai-edit-v3-cos-") as directory:
            target = Path(directory) / "object"
            self.download_file(key, target)
            return _sha256_file(target)


__all__ = ("V3Cos",)
