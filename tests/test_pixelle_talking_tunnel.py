from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "pixelle-video" / "bin" / "run-talking-tunnel"
UNIT = ROOT / "deploy" / "systemd" / "huangque-pixelle-talking-tunnel.service"


def find_bash() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    for candidate in (Path("D:/Git/bin/bash.exe"), Path("C:/Program Files/Git/bin/bash.exe")):
        if candidate.exists():
            return str(candidate)
    raise pytest.skip.Exception("bash is required for tunnel tests")


def run_runner(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("private-key-placeholder", encoding="utf-8")
    known_hosts.write_text("host-key-placeholder", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_ssh.chmod(0o755)

    runner = tmp_path / "run-talking-tunnel"
    runner.write_text(
        RUNNER.read_text(encoding="utf-8").replace("/usr/bin/ssh", fake_ssh.as_posix()),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PIXELLE_TALKING_SSH_TARGET": "ubuntu@129.204.166.13",
            "PIXELLE_TALKING_SSH_KEY": key.as_posix(),
            "PIXELLE_TALKING_SSH_KNOWN_HOSTS": known_hosts.as_posix(),
        }
    )
    env.update(overrides)
    return subprocess.run(
        [find_bash(), runner.as_posix()],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_runner_builds_restricted_loopback_forward(tmp_path):
    result = run_runner(tmp_path)

    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:2] == ["-F", "/dev/null"]
    assert "PermitLocalCommand=no" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "127.0.0.1:8097:127.0.0.1:8096" in args
    assert args[-1] == "ubuntu@129.204.166.13"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PIXELLE_TALKING_SSH_TARGET", "ubuntu@host;touch /tmp/unsafe"),
        ("PIXELLE_TALKING_LOCAL_PORT", "80"),
        ("PIXELLE_TALKING_REMOTE_HOST", "0.0.0.0"),
        ("PIXELLE_TALKING_REMOTE_PORT", "not-a-port"),
    ],
)
def test_runner_rejects_unsafe_forward_configuration(tmp_path, name, value):
    result = run_runner(tmp_path, **{name: value})

    assert result.returncode == 2


def test_systemd_unit_keeps_credentials_out_of_repository():
    unit = UNIT.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/huangque/pixelle-talking-tunnel.env" in unit
    assert "User=admin" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "PIXELLE_TALKING_SSH_TARGET=" not in unit
    assert "PIXELLE_TALKING_SSH_KEY=" not in unit
