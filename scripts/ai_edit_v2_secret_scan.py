#!/usr/bin/env python3
"""Scan Git tracked and untracked files for provider credentials without echoing values."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ASSIGNMENTS = {
    "dashscope": "DASHSCOPE_API_KEY",
    "shotstack": "SHOTSTACK_API_KEY",
    "cos_secret_id": "AI_EDIT_V2_COS_SECRET_ID",
    "cos_secret_key": "AI_EDIT_V2_COS_SECRET_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}
VALUE = r"[A-Za-z0-9_+./=-]{20,}"
PATTERNS = {
    secret_type: re.compile(
        rf'["\']?{re.escape(name)}["\']?\s*[:=]\s*["\']?({VALUE})', re.IGNORECASE
    )
    for secret_type, name in ASSIGNMENTS.items()
}
PATTERNS["openai"] = re.compile(r"\bsk[-_](?:proj[-_])?[A-Za-z0-9_-]{20,}")
PATTERNS["private_key"] = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def _placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith(("test-", "test_", "replace-", "replace_", "example", "dummy", "fake"))


def _git_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    )
    return [root / item.decode("utf-8", "surrogateescape")
            for item in completed.stdout.split(b"\0") if item]


def scan(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    findings: set[tuple[str, str]] = set()
    errors: list[str] = []
    for path in _git_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_bytes()
            if b"\0" in content:
                continue
            text = content.decode("utf-8", "ignore")
        except OSError:
            errors.append(relative)
            continue
        for secret_type, pattern in PATTERNS.items():
            matches = list(pattern.finditer(text))
            if secret_type in ASSIGNMENTS:
                matches = [match for match in matches if not _placeholder(match.group(1))]
            if matches:
                findings.add((relative, secret_type))
    return sorted(findings), sorted(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan AI Edit V2 files for provider secrets")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        findings, errors = scan(args.root.resolve())
    except (OSError, subprocess.CalledProcessError):
        print("repository:scan_error", file=sys.stderr)
        return 2
    for path, secret_type in findings:
        print(f"{path}:{secret_type}")
    for path in errors:
        print(f"{path}:scan_error", file=sys.stderr)
    if errors:
        return 2
    if findings:
        return 1
    print("secret_scan=clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
