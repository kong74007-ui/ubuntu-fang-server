#!/usr/bin/env python3
"""Dedicated localhost HTTP entrypoint for the isolated AI Edit V3 API."""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys


SERVER_ROOT = Path(__file__).resolve().parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from server.content_domains import core


PORT = int(os.environ.get("AI_EDIT_V3_API_PORT", "8113"))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), core.H)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
