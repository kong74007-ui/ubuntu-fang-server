#!/usr/bin/env python3
"""Loopback-only HTTP API for approved Huangque material assets."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfileobj
from urllib.parse import urlsplit

try:
    from .material_library import (
        MaterialLibrary,
        MaterialLibraryError,
        MaterialShortageError,
        content_type_for,
        sha256_file,
    )
except ImportError:
    from material_library import (
        MaterialLibrary,
        MaterialLibraryError,
        MaterialShortageError,
        content_type_for,
        sha256_file,
    )


MAX_BODY_BYTES = 512 * 1024
SHA_PATH_RE = re.compile(r"^/v1/assets/([0-9a-f]{64})$")


class MaterialHandler(BaseHTTPRequestHandler):
    server_version = "HuangqueMaterialLibrary/1.0"

    @property
    def library(self) -> MaterialLibrary:
        return self.server.library  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        print("[material-library] " + fmt % args, flush=True)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        value = self.headers.get("Authorization", "")
        supplied = value[7:].strip() if value.lower().startswith("bearer ") else ""
        return bool(self.token and supplied and hmac.compare_digest(supplied, self.token))

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            try:
                self._json(200, {"ok": True, **self.library.stats()})
            except Exception:
                self._json(503, {"ok": False})
            return
        if path == "/v1/ping":
            if not self._require_auth():
                return
            try:
                self._json(200, {"ok": True, **self.library.stats()})
            except Exception:
                self._json(503, {"ok": False})
            return
        match = SHA_PATH_RE.fullmatch(path)
        if not match:
            self._json(404, {"error": "not_found"})
            return
        if not self._require_auth():
            return
        try:
            material, file_path = self.library.resolve(match.group(1))
            if sha256_file(file_path) != material.sha256:
                raise MaterialLibraryError("material checksum mismatch")
            size = file_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type_for(file_path))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=3600, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with file_path.open("rb") as handle:
                copyfileobj(handle, self.wfile, length=1024 * 1024)
        except KeyError:
            self._json(404, {"error": "asset_not_found"})
        except MaterialLibraryError as exc:
            self._json(409, {"error": "asset_unavailable", "detail": str(exc)})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/v1/select":
            self._json(404, {"error": "not_found"})
            return
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            result = self.library.select(
                payload.get("scenes") or [],
                orientation=payload.get("orientation") or "portrait",
                seed=str(payload.get("seed") or ""),
                used_sha256=payload.get("used_sha256") or [],
            )
            self._json(200, result)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": "invalid_request", "detail": str(exc)})
        except MaterialShortageError as exc:
            self._json(409, {"error": "material_shortage", "detail": str(exc)})
        except MaterialLibraryError as exc:
            self._json(503, {"error": "library_unavailable", "detail": str(exc)})


def build_server(host: str, port: int, root: Path, api_token: str) -> ThreadingHTTPServer:
    if not api_token:
        raise SystemExit("MATERIAL_LIBRARY_API_TOKEN is required")
    server = ThreadingHTTPServer((host, port), MaterialHandler)
    server.library = MaterialLibrary(root)  # type: ignore[attr-defined]
    server.api_token = api_token  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MATERIAL_LIBRARY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MATERIAL_LIBRARY_PORT", "8110")))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("MATERIAL_LIBRARY_ROOT", "/home/ubuntu/material-libraries/huangque-media")),
    )
    args = parser.parse_args()
    token = os.environ.get("MATERIAL_LIBRARY_API_TOKEN", "").strip()
    with build_server(args.host, args.port, args.root, token) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
