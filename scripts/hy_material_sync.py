"""HY-native periodic material mirror. No provider, job, billing or service mutations."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

SHA = re.compile(r"[0-9a-f]{64}\Z")


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2).encode())


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\\\0\r\n:"):
        raise ValueError("unsafe_relative_path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value or value.startswith("-"):
        raise ValueError("unsafe_relative_path")
    return value


def contained(root, value):
    p = Path(root).joinpath(*PurePosixPath(relative(value)).parts)
    if p.is_symlink() or not p.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("path_escape")
    return p


def rows_from_bytes(raw):
    if not 0 < len(raw) <= 64 * 1024 * 1024:
        raise ValueError("index_size_invalid")
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if not 0 < len(rows) <= 20000:
        raise ValueError("index_count_invalid")
    paths = {}
    for row in rows:
        value = row.get("SHA256", row.get("sha256"))
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError("index_sha_invalid")
        if row.get("sha256", value) != value or row.get("SHA256", value) != value:
            raise ValueError("index_sha_conflict")
        path = relative(row.get("server_relative_path"))
        if path in paths and paths[path] != value:
            raise ValueError("index_path_conflict")
        paths[path] = value
    return rows


def wsl_path(path):
    p = PureWindowsPath(path)
    if not re.fullmatch(r"[A-Za-z]:", p.drive):
        raise ValueError("not_windows_drive_path")
    return "/mnt/" + p.drive[0].lower() + "/" + "/".join(p.parts[1:])


def command(args, output=None, timeout=600):
    if output is None:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
    else:
        with Path(output).open("wb") as f:
            r = subprocess.run(args, stdout=f, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode:
        raise RuntimeError("transport_command_failed")
    return r.stdout if output is None else None


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0); f.write(b"0"); f.flush(); f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close(); raise RuntimeError("sync_already_running")
    try:
        yield
    finally:
        f.close()


class Sync:
    def __init__(self, config):
        self.c = config
        self.base = Path(config["base"]).resolve()
        self.root = Path(config["shared_root"]).resolve()
        self.private = Path(config["private_root"]).resolve()
        if not self.root.is_relative_to(self.base) or not self.private.is_relative_to(self.base):
            raise ValueError("root_scope_invalid")
        self.state = self.base / "scheduled-sync"
        self.state.mkdir(exist_ok=True)
        self.run = self.state / "staging" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.run.mkdir(parents=True)

    def export(self, operation, dest=None):
        value = command(self.c["wsl_prefix"] + self.c["export_ssh"] + [operation], dest)
        if dest is None:
            if len(value) > 16 * 1024 * 1024:
                raise ValueError("export_response_limit")
            return json.loads(value)

    def rsync(self, remote, destination, file_list=None):
        args = self.c["wsl_prefix"] + ["rsync", "-rt", "--safe-links", "--no-links", "--timeout=90", "-e", self.c["public_ssh"]]
        if file_list:
            args += ["--from0", "--files-from=" + wsl_path(file_list)]
        args += [self.c["public_remote"] + remote, wsl_path(destination)]
        command(args, timeout=1200)

    def shared(self):
        index = self.root / "index.jsonl"
        cache = Path(self.c["prepared_cache"])
        before = index.read_bytes(); old_rows = rows_from_bytes(before)
        old_by_sha = {r.get("SHA256", r.get("sha256")): r for r in old_rows}
        source = self.run / "source.jsonl"
        self.rsync("index.jsonl", source)
        raw = source.read_bytes(); rows = rows_from_bytes(raw)
        # Preserve only validated operator-approved repair metadata, never accept a source-supplied override.
        overrides = json.loads(Path(self.c["repair_map"]).read_text())
        missing = []
        for row in rows:
            sha = row.get("SHA256", row.get("sha256"))
            override = overrides.get(sha)
            if override:
                fixed = contained(self.root, override["server_relative_path"])
                if hash_file(fixed) != override["SHA256"]:
                    raise ValueError("repair_derivative_changed")
                row.update(override)
                if "sha256" in row: row["sha256"] = override["SHA256"]
            elif sha in old_by_sha:
                row["server_relative_path"] = old_by_sha[sha]["server_relative_path"]
                if not contained(self.root, row["server_relative_path"]).is_file():
                    raise ValueError("local_material_missing")
            else:
                missing.append((row, sha, row["server_relative_path"]))
        if missing:
            folder = self.run / "download"
            folder.mkdir()
            names = self.run / "files.list"
            names.write_bytes(b"".join(p.encode("utf-8") + b"\0" for _, _, p in missing))
            self.rsync("./", folder, names)
            for row, sha, path in missing:
                incoming = contained(folder, path)
                if not incoming.is_file() or hash_file(incoming) != sha:
                    raise ValueError("download_sha_invalid")
                relative_new = "files/_sync/" + sha + Path(path).suffix.lower()
                target = contained(self.root, relative_new)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if hash_file(target) != sha: raise ValueError("existing_sha_invalid")
                else:
                    os.replace(incoming, target)
                row["server_relative_path"] = relative_new
        again = self.run / "source-again.jsonl"
        self.rsync("index.jsonl", again)
        if again.read_bytes() != raw: raise ValueError("source_index_changed")
        candidate_raw = ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode()
        if rows == old_rows:
            return {"state": "unchanged", "records": len(rows), "downloaded": 0}
        candidate = self.run / "candidate.jsonl"
        candidate.write_bytes(candidate_raw)
        sys.path.insert(0, self.c["library_code"])
        from material_library import MaterialLibrary
        lib = MaterialLibrary(self.root, prepared_cache_path=cache)
        lib.index_path = candidate
        lib.refresh()
        incompatible = 0
        for material in lib._materials:
            if not lib._is_available(material): raise ValueError("candidate_unavailable")
            if not lib._video_color_compatible(material, [1, time.monotonic() + 15]): incompatible += 1
        candidate_cache = self.run / "prepared.json"
        lib.write_prepared_cache(candidate_cache)
        if not self.export("idle")["idle"]:
            return {"state": "deferred_busy", "records": len(rows), "downloaded": len(missing)}
        if index.read_bytes() != before: raise ValueError("live_index_changed")
        old_cache = cache.read_bytes()
        backup = self.state / "backups" / self.run.name
        backup.mkdir(parents=True)
        (backup / "index.jsonl").write_bytes(before)
        (backup / "prepared.json").write_bytes(old_cache)
        try:
            atomic_bytes(cache, candidate_cache.read_bytes())
            atomic_bytes(index, candidate_raw)
            health = json.load(urllib.request.urlopen("http://127.0.0.1:18110/health", timeout=15))
            if not health.get("ok") or health["records"] != len(lib._materials):
                raise RuntimeError("shared_health_failed")
        except BaseException:
            atomic_bytes(cache, old_cache); atomic_bytes(index, before); raise
        return {"state": "updated", "records": len(lib._materials), "downloaded": len(missing),
                "incompatible_filtered": incompatible, "index_sha256": hash_file(index), "backup": str(backup)}

    def users(self):
        manifest = self.export("manifest")
        records = validate_private(manifest)
        current = self.private / "current.json"
        previous = json.loads(current.read_text()) if current.exists() else None
        if previous == manifest:
            return {"state": "unchanged", "files": len(records), "downloaded": 0}
        downloaded = 0
        for r in records:
            name = "owners/" + r["owner_hash"] + "/" + r["sha256"] + r["suffix"]
            path = contained(self.private, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.stat().st_size != r["bytes"] or hash_file(path) != r["sha256"]:
                    raise ValueError("private_existing_corrupt")
                continue
            seeds = list((self.private / "snapshots").glob("*/owners/" + r["owner_hash"] + "/" + r["sha256"] + r["suffix"]))
            temp = path.with_name(path.name + ".part")
            if seeds:
                if seeds[0].is_symlink() or not seeds[0].resolve().is_relative_to(self.private): raise ValueError("private_seed_escape")
                shutil.copyfile(seeds[0], temp)
            else:
                self.export("blob " + r["sha256"], temp); downloaded += 1
            if temp.stat().st_size != r["bytes"] or hash_file(temp) != r["sha256"]:
                temp.unlink(missing_ok=True); raise ValueError("private_download_invalid")
            os.replace(temp, path)
        again = self.export("manifest")
        validate_private(again)
        if again != manifest: raise ValueError("private_source_changed")
        if previous is not None:
            atomic_json(self.private / "manifests" / (self.run.name + ".json"), previous)
        atomic_json(current, manifest)
        return {"state": "updated", "files": len(records), "owners": len({r['owner_hash'] for r in records}), "downloaded": downloaded}


def validate_private(value):
    if not isinstance(value, dict) or set(value) != {"version", "records"} or value["version"] != 1:
        raise ValueError("private_contract_invalid")
    rows = value["records"]
    if not isinstance(rows, list) or len(rows) > 50000: raise ValueError("private_count_invalid")
    seen = set()
    for r in rows:
        if not isinstance(r, dict) or set(r) != {"owner_hash", "sha256", "suffix", "bytes", "source_mtime_ns", "expires_at"}: raise ValueError("private_fields_invalid")
        if not all(isinstance(r[k], str) and SHA.fullmatch(r[k]) for k in ("sha256", "owner_hash")): raise ValueError("private_identity_invalid")
        if r['suffix'] not in {'.mp4','.mov','.jpg','.jpeg','.png','.webp'}: raise ValueError("private_suffix_invalid")
        if any(type(r[k]) is not int or r[k] <= 0 for k in ('bytes','source_mtime_ns','expires_at')) or r['bytes'] > 256*1024*1024: raise ValueError("private_numbers_invalid")
        key = r['owner_hash'], r['sha256']
        if key in seen: raise ValueError("private_duplicate")
        seen.add(key)
    return rows


def run(config):
    base = Path(config['base']).resolve()
    with exclusive(base / 'scheduled-sync' / 'run.lock'):
        if shutil.disk_usage(base).free < 8 * 1024**3: raise RuntimeError('disk_space_low')
        if config.get('path'): os.environ['PATH'] = config['path']
        sync = Sync(config)
        result = {'started_at': int(time.time()), 'shared': {}, 'users': {}}
        for name in ('shared','users'):
            try: result[name] = getattr(sync,name)()
            except Exception as e:
                code=str(e) if re.fullmatch('[a-z_]{1,80}',str(e)) else type(e).__name__
                result[name] = {'state':'failed','error_type':type(e).__name__,'code':code}
        result['finished_at'] = int(time.time())
        result['ok'] = all(result[n]['state'] != 'failed' for n in ('shared','users'))
        atomic_json(base / 'scheduled-sync' / 'last-run.json', result)
        # Exact per-run staging root only; never remove source assets, user objects, usage or backups.
        staging_root = (base / 'scheduled-sync' / 'staging').resolve()
        if sync.run.resolve().parent != staging_root or sync.run.is_symlink(): raise ValueError('cleanup_scope_invalid')
        shutil.rmtree(sync.run)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result['ok'] else 1


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    try: sys.exit(run(json.loads(Path(args.config).read_text(encoding='utf-8-sig'))))
    except Exception as e:
        print(json.dumps({'ok':False,'error_type':type(e).__name__}));sys.exit(1)
