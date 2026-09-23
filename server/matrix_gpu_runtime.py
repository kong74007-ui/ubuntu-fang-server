"""Verified, opt-in GPU runtime contract shared by template render entry points."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

CONTRACT_VERSION = 1
RUNTIME_FILES = ("package.json", "package-lock.json", "render.mjs", "browser.mjs",
                 "geometry.mjs", "compositor.mjs", "probe.mjs", "compile.mjs", "contract.json", "decode-plan.mjs")

SUPERVISOR = Path(__file__).with_name("matrix_gpu_supervisor.py")


def supervised_command(command, output_directory):
    if os.name == "nt":
        return command
    if not sys.platform.startswith("linux") or not SUPERVISOR.is_file():
        raise RuntimeError("Required Linux GPU supervisor is unavailable")
    receipt = Path(output_directory).resolve() / ("gpu-reaped-" + uuid.uuid4().hex + ".json")
    return [sys.executable, str(SUPERVISOR), "--matrix-gpu-receipt", str(receipt), "--", *command]


class GpuRuntime:
    @staticmethod
    def guard(process):
        return GpuProcessGuard(process)

    def __init__(self, root, browser, *, node="node", adapter=""):
        self.root = Path(root).resolve()
        self.browser = Path(browser).resolve()
        self.node, self.adapter = node, adapter
        if not self.browser.is_file():
            raise ValueError("GPU geometry browser is unavailable")
        self.fingerprint = self.digest()
        contract = json.loads((self.root / "contract.json").read_text(encoding="utf-8"))
        if contract.get("version") != CONTRACT_VERSION or not isinstance(contract.get("templates"), list):
            raise ValueError("Invalid GPU template contract")
        self.templates = frozenset(contract["templates"])
        text_version = contract.get("text_controls_contract_version", 0)
        self.text_controls_contract_version = text_version if type(text_version) is int and text_version == 1 else 0
        if os.name != "nt":
            subprocess.run([sys.executable, str(SUPERVISOR), "--check"],
                           capture_output=True, timeout=10, check=True)
        args = [node, str(self.root / "probe.mjs")]
        if adapter:
            args += ["--adapter", adapter]
        process = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                 timeout=45, check=True)
        result = json.loads(process.stdout.strip())
        if (result.get("ok") is not True or result.get("contract_version") != CONTRACT_VERSION
                or result.get("encoder") != "hevc_nvenc"
                or result.get("compositor") != "webgpu-native"
                or result.get("adapter", {}).get("isFallbackAdapter") is not False):
            raise ValueError("GPU runtime did not prove hardware composition and encoding")
        self.evidence = result

    def digest(self):
        value = hashlib.sha256()
        for name in RUNTIME_FILES:
            value.update(name.encode("ascii"))
            value.update((self.root / name).read_bytes())
        return value.hexdigest()

    def command(self, project, output, variables, remaining):
        if self.digest() != self.fingerprint:
            raise ValueError("GPU runtime changed after startup; restart with a verified release")
        if remaining <= 0:
            raise TimeoutError("GPU render deadline exceeded")
        result = [self.node, str(self.root / "render.mjs"), "--project", str(project),
                  "--output", str(output), "--variables", str(variables),
                  "--browser", str(self.browser), "--timeout", str(remaining)]
        if self.adapter:
            result += ["--adapter", self.adapter]
        return supervised_command(result, Path(output).parent)

    def public(self, templates):
        try:
            ready = self.digest() == self.fingerprint and set(templates) <= self.templates
        except OSError:
            ready = False
        return {"contract_version": CONTRACT_VERSION, "ready": ready,
                "text_controls_contract_version": getattr(self, "text_controls_contract_version", 0),
                "compositor": "webgpu-native", "encoder": "hevc_nvenc",
                "runtime_sha256": self.fingerprint,
                "adapter": self.evidence["adapter"], "templates": sorted(templates)}

    def result(self, output):
        report = json.loads(Path(str(output) + ".json").read_text(encoding="utf-8"))
        if (report.get("compositor") != "webgpu-native"
                or report.get("adapter", {}).get("vendor") != "nvidia"
                or report.get("adapter", {}).get("isFallbackAdapter") is not False):
            raise ValueError("Missing hardware GPU rendering evidence")
        return {"contract_version": CONTRACT_VERSION, "compositor": "webgpu-native",
                "encoder": "hevc_nvenc" if report["mode"] != "sdr" else "h264_nvenc",
                "runtime_sha256": self.fingerprint, "adapter": report["adapter"],
                "render_seconds": report["renderSeconds"], "frames": report["frames"]}


class GpuProcessGuard:
    """Contain descendants so scratch can be removed after an abrupt renderer exit."""

    def __init__(self, process):
        self.process = process
        self.job = None
        self.stopped = False
        if os.name != "nt":
            args = process.args
            if (not isinstance(args, (list, tuple)) or len(args) < 6
                    or Path(args[1]).resolve() != SUPERVISOR.resolve()
                    or args[2] != "--matrix-gpu-receipt" or args[4] != "--"):
                raise ValueError("Refusing an unsupervised POSIX GPU process")
            self.receipt = Path(args[3])
            return
        import ctypes
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active_limit", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("memory", ctypes.c_size_t * 4)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]

        self.ctypes, self.Accounting = ctypes, Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(job, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.kernel.CloseHandle(job)
            raise
        self.job = job

    def stop(self):
        if self.stopped:
            return
        if self.job is not None:
            if not self.kernel.TerminateJobObject(self.job, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            deadline = time.monotonic() + 5
            while True:
                info = self.Accounting()
                if not self.kernel.QueryInformationJobObject(
                    self.job, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None,
                ):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                if not info.active:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("GPU process tree did not exit; retaining scratch")
                time.sleep(0.01)
            self.kernel.CloseHandle(self.job)
            self.job = None
        else:
            if self.process.poll() is None:
                # Do not signal Python before its supervisor handlers are installed.
                deadline = time.monotonic() + 10
                ready = self.receipt.with_suffix(".ready")
                while self.process.poll() is None:
                    try:
                        initialized = ready.read_text(encoding="ascii") == str(self.process.pid)
                    except FileNotFoundError:
                        initialized = False
                    if initialized:
                        try:
                            self.process.send_signal(signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("GPU supervisor did not initialize; retaining scratch")
                    time.sleep(.02)
            self.process.wait(timeout=15)
            try:
                proof = json.loads(self.receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("GPU supervisor did not prove tree exit; retaining scratch") from exc
            if (proof.get("version") != 1 or proof.get("supervisor_pid") != self.process.pid
                    or proof.get("all_children_reaped") is not True):
                raise RuntimeError("Invalid GPU tree-exit receipt; retaining scratch")
        self.process.wait(timeout=5)
        self.stopped = True
