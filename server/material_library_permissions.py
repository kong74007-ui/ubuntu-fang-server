#!/usr/bin/env python3
"""Normalize permissions for atomically replaced material-library metadata."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows only imports this module for static tests.
    grp = None
    pwd = None


METADATA_NAMES = ("index.jsonl", "index.csv", "stats.json")
METADATA_MODE = 0o644
MAX_STABILIZE_ROUNDS = 8
STABILIZE_SLEEP_SECONDS = 0.025


class MetadataPermissionError(RuntimeError):
    pass


def _directory_fd(root: Path) -> int:
    root = Path(os.path.abspath(root))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise MetadataPermissionError(f"material library root is unavailable: {root}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise MetadataPermissionError(f"material library root is unsafe: {root}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    return os.open(root, flags)


def _open_metadata(directory_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MetadataPermissionError(f"material metadata is unavailable or unsafe: {name}") from exc
    current = os.fstat(descriptor)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        os.close(descriptor)
        raise MetadataPermissionError(f"material metadata must be a single regular file: {name}")
    return descriptor


def _matches(current: os.stat_result, uid: int, gid: int) -> bool:
    return (
        current.st_uid == uid
        and current.st_gid == gid
        and stat.S_IMODE(current.st_mode) == METADATA_MODE
    )


def normalize_metadata_permissions(root: Path, uid: int, gid: int) -> list[dict[str, object]]:
    """Normalize only the three canonical metadata paths without following links."""
    if not hasattr(os, "fchown"):
        raise MetadataPermissionError("metadata permission normalization requires POSIX fchown")
    directory_fd = _directory_fd(root)
    try:
        for _round in range(MAX_STABILIZE_ROUNDS):
            results: list[dict[str, object]] = []
            for name in METADATA_NAMES:
                descriptor = _open_metadata(directory_fd, name)
                try:
                    current = os.fstat(descriptor)
                    changed = False
                    if current.st_uid != uid or current.st_gid != gid:
                        os.fchown(descriptor, uid, gid)
                        changed = True
                    current = os.fstat(descriptor)
                    if stat.S_IMODE(current.st_mode) != METADATA_MODE:
                        os.fchmod(descriptor, METADATA_MODE)
                        changed = True
                    if changed:
                        os.fsync(descriptor)
                    normalized = os.fstat(descriptor)
                    published = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    stable = (
                        normalized.st_dev == published.st_dev
                        and normalized.st_ino == published.st_ino
                        and _matches(published, uid, gid)
                    )
                    results.append({
                        "name": name,
                        "inode": published.st_ino,
                        "uid": published.st_uid,
                        "gid": published.st_gid,
                        "mode": format(stat.S_IMODE(published.st_mode), "04o"),
                        "changed": changed,
                    })
                finally:
                    os.close(descriptor)
                if not stable:
                    break
            else:
                os.fsync(directory_fd)
                return results
            time.sleep(STABILIZE_SLEEP_SECONDS)
    finally:
        os.close(directory_fd)
    raise MetadataPermissionError("material metadata kept changing during permission normalization")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get(
            "MATERIAL_LIBRARY_ROOT", "/home/ubuntu/material-libraries/huangque-media"
        )),
    )
    parser.add_argument("--owner", default=os.environ.get("MATERIAL_LIBRARY_OWNER", "ubuntu"))
    parser.add_argument("--group", default=os.environ.get("MATERIAL_LIBRARY_GROUP", "ubuntu"))
    args = parser.parse_args()
    if pwd is None or grp is None:
        raise SystemExit("metadata permission normalization requires POSIX user and group lookup")
    uid = pwd.getpwnam(args.owner).pw_uid
    gid = grp.getgrnam(args.group).gr_gid
    result = normalize_metadata_permissions(args.root, uid, gid)
    print(json.dumps({"ok": True, "files": result}, separators=(",", ":")))


if __name__ == "__main__":
    main()
