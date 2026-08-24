from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy/pixelle-video/bin/run-novix-tunnel"
CHECKER = ROOT / "deploy/pixelle-video/bin/check-novix-openai-proxy"
INSTALLER = ROOT / "deploy/pixelle-video/install-novix-tunnel.sh"
UNIT = ROOT / "deploy/systemd/huangque-pixelle-novix-tunnel.service"
PIXELLE_UNIT = ROOT / "deploy/systemd/huangque-pixelle-video.service"


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
    key.chmod(0o600)
    known_hosts.chmod(0o644)
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_ssh.chmod(0o755)
    fake_stat = tmp_path / "stat"
    fake_stat.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${@: -1}\" in *known_hosts) printf '644\\n' ;; *) printf '600\\n' ;; esac\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    runner = tmp_path / "run-novix-tunnel"
    runner.write_text(
        RUNNER.read_text(encoding="utf-8")
        .replace("/usr/bin/ssh", fake_ssh.as_posix())
        .replace("stat -c", fake_stat.as_posix() + " -c"),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "PIXELLE_NOVIX_SSH_TARGET": "ubuntu@129.204.166.13",
        "PIXELLE_NOVIX_SSH_KEY": key.as_posix(),
        "PIXELLE_NOVIX_SSH_KNOWN_HOSTS": known_hosts.as_posix(),
    })
    env.update(overrides)
    return subprocess.run(
        [find_bash(), runner.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )


def test_runner_builds_restricted_novix_forward(tmp_path):
    result = run_runner(tmp_path)
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:2] == ["-F", "/dev/null"]
    assert "ExitOnForwardFailure=yes" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "127.0.0.1:10811:127.0.0.1:10810" in args
    assert args[-1] == "ubuntu@129.204.166.13"


@pytest.mark.parametrize(("name", "value"), [
    ("PIXELLE_NOVIX_SSH_TARGET", "ubuntu@host;touch /tmp/unsafe"),
    ("PIXELLE_NOVIX_LOCAL_PORT", "80"),
    ("PIXELLE_NOVIX_REMOTE_HOST", "0.0.0.0"),
    ("PIXELLE_NOVIX_REMOTE_PORT", "not-a-port"),
    ("PIXELLE_NOVIX_LOCAL_PORT", "10810"),
])
def test_runner_rejects_unsafe_configuration(tmp_path, name, value):
    result = run_runner(tmp_path, **{name: value})
    assert result.returncode == 2


def test_runner_rejects_symlinked_credentials(tmp_path):
    real_key = tmp_path / "real-key"
    real_key.write_text("private-key-placeholder", encoding="utf-8")
    key_link = tmp_path / "key-link"
    try:
        key_link.symlink_to(real_key)
    except OSError:
        pytest.skip("symlinks are unavailable")
    result = run_runner(tmp_path, PIXELLE_NOVIX_SSH_KEY=key_link.as_posix())
    assert result.returncode == 2


@pytest.mark.parametrize(("status", "expected"), [("401", 0), ("200", 0), ("000", 1)])
def test_readiness_checker_accepts_only_reachable_openai(tmp_path, status, expected):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"${FAKE_CURL_MARKER:-}\" ]; then printf called > \"${FAKE_CURL_MARKER}\"; fi\n"
        "printf '%s' \"${FAKE_HTTP_STATUS}\"\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    checker = tmp_path / "check-novix-openai-proxy"
    checker.write_text(
        CHECKER.read_text(encoding="utf-8").replace(
            "curl --silent", fake_curl.as_posix() + " --silent"),
        encoding="utf-8",
    )
    env = dict(os.environ, FAKE_HTTP_STATUS=status)
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == expected


@pytest.mark.parametrize("override", [
    "http://attacker.invalid/health",
    "https://example.com/v1/models",
    "https://user:pass@api.openai.com/v1/models",
    "https://api.openai.com/v1/models?probe=1",
    "https://api.openai.com/v1/models#probe",
])
def test_readiness_checker_rejects_openai_url_override_before_curl(tmp_path, override):
    marker = tmp_path / "curl-called"
    env = dict(os.environ, PIXELLE_NOVIX_OPENAI_PROBE_URL=override,
               FAKE_CURL_MARKER=str(marker))
    result = subprocess.run(
        [find_bash(), CHECKER.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == 2
    assert not marker.exists()


def test_readiness_checker_rejects_proxy_override_before_curl(tmp_path):
    marker = tmp_path / "curl-called"
    env = dict(os.environ, PIXELLE_NOVIX_PROXY_URL="http://127.0.0.1:7999",
               FAKE_CURL_MARKER=str(marker))
    result = subprocess.run(
        [find_bash(), CHECKER.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == 2
    assert not marker.exists()


def test_units_and_installer_keep_credentials_out_of_repository():
    unit = UNIT.read_text(encoding="utf-8")
    pixelle = PIXELLE_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/huangque/pixelle-novix-tunnel.env" in unit
    assert "ExecStartPost=/usr/local/libexec/huangque/check-pixelle-novix-openai" in unit
    assert "User=admin" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "PIXELLE_NOVIX_SSH_TARGET=" not in unit
    assert "Requires=huangque-pixelle-novix-tunnel.service" in pixelle
    assert "Environment=HTTPS_PROXY=http://127.0.0.1:10811" in pixelle
    assert "127.0.0.1:7999" not in pixelle
    assert "/etc/huangque/pixelle-novix-tunnel.env" in installer
    assert "enable --now" in installer
    assert "PIXELLE_NOVIX_SSH_KEY=" not in installer


def test_deployment_files_contain_no_private_key_material():
    for path in (RUNNER, CHECKER, INSTALLER, UNIT, PIXELLE_UNIT):
        text = path.read_text(encoding="utf-8")
        assert "BEGIN OPENSSH PRIVATE KEY" not in text
        assert "BEGIN PRIVATE KEY" not in text
