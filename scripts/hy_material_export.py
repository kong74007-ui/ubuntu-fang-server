"""Forced-SSH read-only export: manifest, blob SHA, or idle. No arbitrary paths."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

SHA = re.compile(r"[0-9a-f]{64}\Z")
SUFFIXES = {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"}


def connection(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def inventory(config, now=None):
    now = int(time.time() if now is None else now)
    owners = {}
    with closing(connection(config["jobs_db"])) as db:
        for username, raw in db.execute(
            "SELECT username,payload FROM jobs WHERE kind='matrix_template_video' "
            "AND coalesce(deleted,0)=0"
        ):
            if not username:
                continue
            try:
                items = json.loads(raw).get("user_materials") or []
            except (ValueError, TypeError, AttributeError):
                continue
            for item in items:
                sha = item.get("sha256") if isinstance(item, dict) else None
                if isinstance(sha, str) and SHA.fullmatch(sha):
                    owners.setdefault(sha, set()).add(hashlib.sha256(username.encode()).hexdigest())
    root = Path(config["asset_root"]).resolve()
    records = []
    for path in sorted(root.iterdir()):
        if path.suffix not in SUFFIXES or not SHA.fullmatch(path.stem):
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            continue
        info = path.stat()
        deadline = int(info.st_mtime) + int(config["retention_seconds"])
        if deadline <= now or not 0 < info.st_size <= 256 * 1024 * 1024:
            continue
        for owner in sorted(owners.get(path.stem, [])):
            records.append({"owner_hash": owner, "sha256": path.stem,
                            "suffix": path.suffix, "bytes": info.st_size,
                            "source_mtime_ns": info.st_mtime_ns, "expires_at": deadline})
    if len(records) > 50000:
        raise ValueError("inventory_limit")
    return {"version": 1, "records": records}


def idle(config):
    with closing(connection(config["jobs_db"])) as db:
        main = db.execute("SELECT count(*) FROM jobs WHERE kind='matrix_template_video' "
                          "AND status IN ('pending','running')").fetchone()[0]
    with closing(connection(config["relay_db"])) as db:
        relay = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
    return {"idle": main == 0 and relay == 0, "main_active": main, "relay_active": relay}


def blob(config, sha, output):
    if not SHA.fullmatch(sha):
        raise ValueError("invalid_sha")
    rows = [r for r in inventory(config)["records"] if r["sha256"] == sha]
    if not rows or len({r["suffix"] for r in rows}) != 1:
        raise ValueError("asset_not_authorized")
    row = rows[0]
    path = Path(config["asset_root"]).resolve() / (sha + row["suffix"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != row["bytes"]:
            raise ValueError("asset_changed")
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("asset_changed")
        if digest.hexdigest() != sha:
            raise ValueError("asset_hash_invalid")
        source.seek(0)
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            output.write(chunk)


def dispatch(config, command, output):
    if command == "manifest":
        value = inventory(config)
    elif command == "idle":
        value = idle(config)
    elif re.fullmatch(r"blob [0-9a-f]{64}", command):
        return blob(config, command[5:], output)
    else:
        raise ValueError("command_denied")
    output.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


if __name__ == "__main__":
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        dispatch(config, os.environ.get("SSH_ORIGINAL_COMMAND", ""), sys.stdout.buffer)
    except Exception:
        print("material_export_denied_or_unavailable", file=sys.stderr)
        sys.exit(1)
