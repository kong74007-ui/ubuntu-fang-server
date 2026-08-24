from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy/pixelle-video/bin/run-novix-tunnel"
CHECKER = ROOT / "deploy/pixelle-video/bin/check-novix-openai-proxy"
COMMAND_CHECKER = ROOT / "deploy/pixelle-video/bin/check-novix-command-denied"
REMOTE_FORWARD_CHECKER = ROOT / "deploy/pixelle-video/bin/check-novix-remote-forward-denied"
KEY_RENDERER = ROOT / "deploy/pixelle-video/bin/render-novix-authorized-key"
PRODUCTION_INSTALLER = ROOT / "deploy/pixelle-video/install-novix-production-sshd.sh"
PRODUCTION_ACCOUNT_CHECKER = ROOT / "deploy/pixelle-video/bin/check-novix-production-account"
SSHD_MATCH = ROOT / "deploy/sshd/60-huangque-pixelle-novix.conf"
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
        "PIXELLE_NOVIX_SSH_TARGET": "pixelle_tunnel@129.204.166.13",
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
    assert args[-1] == "pixelle_tunnel@129.204.166.13"


@pytest.mark.parametrize(("name", "value"), [
        ("PIXELLE_NOVIX_SSH_TARGET", "ubuntu@host;touch /tmp/unsafe"),
        ("PIXELLE_NOVIX_SSH_TARGET", "ubuntu@129.204.166.13"),
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
    fake_sleep = fake_bin / "sleep"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "count=0\n"
        "if [ -f \"${FAKE_CURL_MARKER}\" ]; then count=$(cat \"${FAKE_CURL_MARKER}\"); fi\n"
        "count=$((count + 1)); printf '%s' \"${count}\" > \"${FAKE_CURL_MARKER}\"\n"
        "if [ -n \"${FAKE_HTTP_SEQUENCE:-}\" ]; then\n"
        "  status=$(printf '%s' \"${FAKE_HTTP_SEQUENCE}\" | cut -d, -f\"${count}\")\n"
        "  if [ -z \"${status}\" ]; then status=$(printf '%s' \"${FAKE_HTTP_SEQUENCE}\" | awk -F, '{print $NF}'); fi\n"
        "  printf '%s' \"${status}\"\n"
        "else\n"
        "  printf '%s' \"${FAKE_HTTP_STATUS}\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    fake_sleep.chmod(0o755)
    checker = tmp_path / "check-novix-openai-proxy"
    checker.write_text(
        CHECKER.read_text(encoding="utf-8")
        .replace("curl --silent", fake_curl.as_posix() + " --silent")
        .replace("/usr/bin/sleep", fake_sleep.as_posix()),
        encoding="utf-8",
    )
    marker = tmp_path / "curl-count"
    env = dict(os.environ, FAKE_HTTP_STATUS=status, FAKE_CURL_MARKER=marker.as_posix())
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == expected
    assert int(marker.read_text(encoding="utf-8")) == (1 if expected == 0 else 6)


def test_readiness_checker_waits_for_delayed_tunnel_listener(tmp_path):
    fake_curl = tmp_path / "curl"
    fake_sleep = tmp_path / "sleep"
    marker = tmp_path / "curl-count"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "count=0; if [ -f \"${FAKE_CURL_MARKER}\" ]; then count=$(cat \"${FAKE_CURL_MARKER}\"); fi\n"
        "count=$((count + 1)); printf '%s' \"${count}\" > \"${FAKE_CURL_MARKER}\"\n"
        "case \"${count}\" in 1|2) printf 000 ;; *) printf 401 ;; esac\n",
        encoding="utf-8",
    )
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    fake_sleep.chmod(0o755)
    checker = tmp_path / "checker"
    checker.write_text(
        CHECKER.read_text(encoding="utf-8")
        .replace("curl --silent", fake_curl.as_posix() + " --silent")
        .replace("/usr/bin/sleep", fake_sleep.as_posix()),
        encoding="utf-8",
    )
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=dict(os.environ, FAKE_CURL_MARKER=marker.as_posix()),
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "3"
    assert "attempt 3" in result.stdout


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


def test_authorized_key_renderer_forces_false_and_limits_forward(tmp_path):
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        pytest.skip("ssh-keygen is required")
    key = tmp_path / "id_ed25519"
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    result = subprocess.run(
        [find_bash(), KEY_RENDERER.as_posix(), key.with_suffix(".pub").as_posix()],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        'restrict,port-forwarding,command="/usr/bin/false",permitopen="127.0.0.1:10810" '
    )
    assert "ssh-ed25519 " in result.stdout
    assert result.stdout.rstrip().endswith("pixelle-novix-tunnel")


@pytest.mark.parametrize(("status", "output", "expected"), [
    (1, "", 0),
    (0, "ubuntu", 1),
    (255, "", 1),
    (1, "unexpected", 1),
])
def test_command_denial_checker_requires_exact_forced_false_result(
        tmp_path, status, output, expected):
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("key", encoding="utf-8")
    known_hosts.write_text("host", encoding="utf-8")
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"${FAKE_SSH_OUTPUT:-}\" ]; then printf '%s\\n' \"${FAKE_SSH_OUTPUT}\"; fi\n"
        "exit \"${FAKE_SSH_STATUS}\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    checker = tmp_path / "check-command-denied"
    checker.write_text(
        COMMAND_CHECKER.read_text(encoding="utf-8").replace(
            "/usr/bin/ssh", fake_ssh.as_posix()),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        PIXELLE_NOVIX_SSH_TARGET="pixelle_tunnel@129.204.166.13",
        PIXELLE_NOVIX_SSH_KEY=key.as_posix(),
        PIXELLE_NOVIX_SSH_KNOWN_HOSTS=known_hosts.as_posix(),
        FAKE_SSH_STATUS=str(status),
        FAKE_SSH_OUTPUT=output,
    )
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == expected


@pytest.mark.parametrize(("status", "error", "expected"), [
    (255, "Error: remote port forwarding failed for listen port 0", 0),
    (255, "administratively prohibited", 0),
    (124, "", 1),
    (255, "Permission denied (publickey)", 1),
    (0, "", 1),
])
def test_remote_forward_checker_requires_explicit_sshd_denial(
        tmp_path, status, error, expected):
    key = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("key", encoding="utf-8")
    known_hosts.write_text("host", encoding="utf-8")
    fake_ssh = tmp_path / "ssh"
    fake_timeout = tmp_path / "timeout"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"${FAKE_SSH_ERROR:-}\" ]; then printf '%s\\n' \"${FAKE_SSH_ERROR}\" >&2; fi\n"
        "exit \"${FAKE_SSH_STATUS}\"\n",
        encoding="utf-8",
    )
    fake_timeout.write_text(
        "#!/usr/bin/env bash\nshift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_timeout.chmod(0o755)
    checker = tmp_path / "check-remote-forward-denied"
    checker.write_text(
        REMOTE_FORWARD_CHECKER.read_text(encoding="utf-8")
        .replace("/usr/bin/timeout", fake_timeout.as_posix())
        .replace("/usr/bin/ssh", fake_ssh.as_posix()),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        PIXELLE_NOVIX_SSH_TARGET="pixelle_tunnel@129.204.166.13",
        PIXELLE_NOVIX_SSH_KEY=key.as_posix(),
        PIXELLE_NOVIX_SSH_KNOWN_HOSTS=known_hosts.as_posix(),
        FAKE_SSH_STATUS=str(status),
        FAKE_SSH_ERROR=error,
    )
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=env,
    )
    assert result.returncode == expected


def test_units_and_installer_keep_credentials_out_of_repository():
    unit = UNIT.read_text(encoding="utf-8")
    pixelle = PIXELLE_UNIT.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    readme = (ROOT / "deploy/pixelle-video/README.md").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/huangque/pixelle-novix-tunnel.env" in unit
    assert "ExecStartPost=/usr/local/libexec/huangque/check-pixelle-novix-openai" in unit
    assert "ExecStartPost=/usr/local/libexec/huangque/check-pixelle-novix-command-denied" in unit
    assert "ExecStartPost=/usr/local/libexec/huangque/check-pixelle-novix-remote-forward-denied" in unit
    assert "TimeoutStartSec=45" in unit
    assert "User=admin" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "PIXELLE_NOVIX_SSH_TARGET=" not in unit
    checker = CHECKER.read_text(encoding="utf-8")
    assert "MAX_ATTEMPTS=6" in checker
    assert "--connect-timeout 2" in checker
    assert "--max-time 3" in checker
    assert "PIXELLE_NOVIX_READINESS_ATTEMPTS" not in checker
    assert "Requires=huangque-pixelle-novix-tunnel.service" in pixelle
    assert "Environment=HTTPS_PROXY=http://127.0.0.1:10811" in pixelle
    assert "127.0.0.1:7999" not in pixelle
    assert "/etc/huangque/pixelle-novix-tunnel.env" in installer
    assert "enable --now" in installer
    assert "check-pixelle-novix-command-denied" in installer
    assert "check-pixelle-novix-remote-forward-denied" in installer
    assert "PIXELLE_NOVIX_SSH_KEY=" not in installer
    assert 'command="/usr/bin/false",permitopen="127.0.0.1:10810"' in readme
    assert "status=1" in readme


def test_production_match_user_policy_is_local_forward_only():
    config = SSHD_MATCH.read_text(encoding="utf-8")
    installer = PRODUCTION_INSTALLER.read_text(encoding="utf-8")
    assert "Match User pixelle_tunnel" in config
    assert "AllowTcpForwarding local" in config
    assert "PermitOpen 127.0.0.1:10810" in config
    assert "PermitListen none" in config
    assert "ForceCommand /usr/bin/false" in config
    assert "PermitTTY no" in config
    assert "AllowAgentForwarding no" in config
    assert "X11Forwarding no" in config
    assert config.rstrip().endswith("Match all")
    validate = installer.index("/usr/sbin/sshd -t")
    reload_ssh = installer.index("systemctl reload ssh.service", validate)
    assert validate < reload_ssh
    for expected in (
        "allowtcpforwarding local", "permitopen 127.0.0.1:10810",
        "permitlisten none", "forcecommand /usr/bin/false",
    ):
        assert expected in installer
    assert "refusing to replace non-managed" in installer
    assert "openssl rand -base64 48" in installer
    assert "openssl passwd -6 -stdin" in installer
    assert "/usr/sbin/chpasswd -e" in installer
    assert '!= "P"' in installer
    assert "ORIGINAL_PASSWORD_HASH" in installer
    assert "unset RANDOM_PASSWORD" in installer
    assert "${ACCOUNT_CHECKER}" in installer


@pytest.mark.parametrize(("password_state", "drop_restriction", "expected"), [
    ("P", "", 0),
    ("L", "", 1),
    ("P", "passwordauthentication no", 1),
])
def test_production_account_checker_requires_unlocked_publickey_only_account(
        tmp_path, password_state, drop_restriction, expected):
    fake_id = tmp_path / "id"
    fake_getent = tmp_path / "getent"
    fake_passwd = tmp_path / "passwd"
    fake_sshd = tmp_path / "sshd"
    fake_id.write_text("#!/usr/bin/env bash\nprintf '0\\n'\n", encoding="utf-8")
    fake_getent.write_text(
        "#!/usr/bin/env bash\nprintf 'pixelle_tunnel:x:997:997::/var/lib/huangque-pixelle-tunnel:/usr/bin/false\\n'\n",
        encoding="utf-8",
    )
    fake_passwd.write_text(
        "#!/usr/bin/env bash\nprintf 'pixelle_tunnel %s 2026-08-24 0 99999 7 -1\\n' \"${FAKE_PASSWORD_STATE}\"\n",
        encoding="utf-8",
    )
    effective = [
        "authenticationmethods publickey", "passwordauthentication no",
        "kbdinteractiveauthentication no", "allowtcpforwarding local",
        "permitopen 127.0.0.1:10810", "permitlisten none",
        "forcecommand /usr/bin/false", "permittty no",
        "allowagentforwarding no", "x11forwarding no",
    ]
    if drop_restriction:
        effective.remove(drop_restriction)
    fake_sshd.write_text(
        "#!/usr/bin/env bash\ncat <<'EOF'\n" + "\n".join(effective) + "\nEOF\n",
        encoding="utf-8",
    )
    for path in (fake_id, fake_getent, fake_passwd, fake_sshd):
        path.chmod(0o755)
    checker = tmp_path / "check-production-account"
    checker.write_text(
        PRODUCTION_ACCOUNT_CHECKER.read_text(encoding="utf-8")
        .replace("id -u", fake_id.as_posix() + " -u")
        .replace("getent passwd", fake_getent.as_posix() + " passwd")
        .replace("passwd -S", fake_passwd.as_posix() + " -S")
        .replace("/usr/sbin/sshd", fake_sshd.as_posix()),
        encoding="utf-8",
    )
    result = subprocess.run(
        [find_bash(), checker.as_posix()], check=False, capture_output=True,
        text=True, env=dict(os.environ, FAKE_PASSWORD_STATE=password_state),
    )
    assert result.returncode == expected


def test_deployment_files_contain_no_private_key_material():
    for path in (RUNNER, CHECKER, COMMAND_CHECKER, REMOTE_FORWARD_CHECKER,
                 KEY_RENDERER, PRODUCTION_ACCOUNT_CHECKER, INSTALLER, PRODUCTION_INSTALLER, UNIT,
                 PIXELLE_UNIT, SSHD_MATCH):
        text = path.read_text(encoding="utf-8")
        assert "BEGIN OPENSSH PRIVATE KEY" not in text
        assert "BEGIN PRIVATE KEY" not in text
