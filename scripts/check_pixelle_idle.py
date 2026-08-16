#!/usr/bin/env python3
"""Fail closed unless the local Pixelle task list has no active work."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.request import urlopen


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}


def require_idle(payload: Any) -> None:
    if not isinstance(payload, list):
        raise RuntimeError("Pixelle task response must be a list")

    active_count = 0
    for task in payload:
        if not isinstance(task, dict):
            active_count += 1
            continue
        status = str(task.get("status", "")).strip().lower()
        if status not in TERMINAL_STATUSES:
            active_count += 1
    if active_count:
        raise RuntimeError(f"Pixelle has {active_count} non-terminal task(s)")


def fetch_tasks(url: str, timeout: float) -> Any:
    with urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Pixelle task endpoint returned HTTP {response.status}")
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8103/api/tasks")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    try:
        tasks = fetch_tasks(args.url, args.timeout)
        require_idle(tasks)
    except Exception as exc:
        print(f"Pixelle idle check failed: {exc}", file=sys.stderr)
        return 1
    print("Pixelle has no non-terminal tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
