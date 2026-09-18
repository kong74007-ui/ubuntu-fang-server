"""Linux per-render subreaper: own detached browser descendants until reaped."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def enable_subreaper():
    if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
        raise RuntimeError("GPU supervision requires Linux procfs and pidfds")
    if not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("GPU supervision requires pidfd signals")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot enable render subreaper")
    fd = os.pidfd_open(os.getpid())
    os.close(fd)
    Path(f"/proc/{os.getpid()}/stat").read_text()


def process_identity(pid):
    try:
        # comm may contain spaces or parentheses; fields after the last ')' are stable.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19])  # ppid, starttime
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def descendants():
    records = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            pid = int(path.name)
            identity = process_identity(pid)
            if identity is not None:
                records[pid] = identity
    owned = {os.getpid()}
    while True:
        children = {pid for pid, (parent, _) in records.items() if parent in owned}
        expanded = owned | children
        if expanded == owned:
            break
        owned = expanded
    owned.remove(os.getpid())
    return {pid: records[pid] for pid in owned}


def kill_owned(pid, identity):
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if process_identity(pid) == identity:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def drain_children(timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        for pid, identity in descendants().items():
            kill_owned(pid, identity)
        # Orphans from setsid/double-fork are adopted by this dedicated subreaper.
        # ECHILD, not the renderer's exit, is the completion proof.
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if not pid:
                break
        if time.monotonic() >= deadline:
            raise TimeoutError("GPU descendants did not all exit; no cleanup receipt")
        time.sleep(.02)


def run(command, receipt):
    enable_subreaper()
    cancelled = False

    def request_stop(_signal, _frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.exists():
        raise RuntimeError("Refusing an existing GPU supervision receipt")
    with receipt.with_suffix(".ready").open("x", encoding="ascii") as handle:
        handle.write(str(os.getpid()))
    child = None
    status = 125
    try:
        child = subprocess.Popen(command, start_new_session=True)
        while child.poll() is None and not cancelled:
            time.sleep(.02)
        status = 143 if cancelled else child.returncode
    finally:
        drain_children()
        if child is not None and child.returncode is None:
            child.returncode = -signal.SIGKILL  # waitpid already reaped it above.
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump({"version": 1, "supervisor_pid": os.getpid(), "all_children_reaped": True}, handle)
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        enable_subreaper()
    elif len(sys.argv) >= 5 and sys.argv[1] == "--matrix-gpu-receipt" and sys.argv[3] == "--":
        sys.exit(run(sys.argv[4:], Path(sys.argv[2])))
    else:
        raise SystemExit("Expected --matrix-gpu-receipt <new-file> -- <renderer command>")
