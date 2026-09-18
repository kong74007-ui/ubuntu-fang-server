"""Verified, opt-in GPU runtime contract shared by template render entry points."""
import hashlib
import json
from pathlib import Path
import subprocess

CONTRACT_VERSION = 1
RUNTIME_FILES = ("package.json", "package-lock.json", "render.mjs", "browser.mjs",
                 "geometry.mjs", "compositor.mjs", "probe.mjs", "compile.mjs", "contract.json")


class GpuRuntime:
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
        return result

    def public(self, templates):
        try:
            ready = self.digest() == self.fingerprint and set(templates) <= self.templates
        except OSError:
            ready = False
        return {"contract_version": CONTRACT_VERSION, "ready": ready,
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
