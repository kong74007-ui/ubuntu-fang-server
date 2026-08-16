#!/usr/bin/env python3
"""Render a private Mihomo config from a root-readable subscription env file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key[0].isalpha() or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def validate_subscription_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise ValueError("GRAYFOX_SUBSCRIPTION_URL is missing")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("GRAYFOX_SUBSCRIPTION_URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("GRAYFOX_SUBSCRIPTION_URL is invalid")
    return url


def render(values: dict[str, str]) -> str:
    subscription_url = validate_subscription_url(
        values.get("GRAYFOX_SUBSCRIPTION_URL", "")
    )
    return f"""mixed-port: 7999
allow-lan: false
bind-address: "127.0.0.1"
mode: rule
log-level: warning
ipv6: false

dns:
  enable: true
  ipv6: false
  enhanced-mode: "redir-host"
  nameserver:
    - "https://1.12.12.12/dns-query"
  proxy-server-nameserver:
    - "https://1.12.12.12/dns-query"
  direct-nameserver:
    - "https://1.12.12.12/dns-query"

proxy-providers:
  grayfox:
    type: http
    url: {quoted(subscription_url)}
    path: ./providers/grayfox.yaml
    interval: 3600
    header:
      User-Agent: ["clash.meta"]
    health-check:
      enable: true
      url: "https://www.gstatic.com/generate_204"
      interval: 300

proxy-groups:
  - name: GRAYFOX_AUTO
    type: url-test
    use:
      - grayfox
    url: "https://www.gstatic.com/generate_204"
    interval: 300
    tolerance: 100

rules:
  - MATCH,GRAYFOX_AUTO
"""


def write_private_config(output: Path, text: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    old_umask = os.umask(0o077)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        os.umask(old_umask)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    write_private_config(args.output, render(parse_env(args.env)))
    print(f"rendered private Mihomo config at {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
