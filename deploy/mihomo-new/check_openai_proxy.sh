#!/usr/bin/env bash
set -euo pipefail

OPENAI_STATUS=""
for _ in 1 2 3; do
  OPENAI_STATUS="$(curl --proxy http://127.0.0.1:7999 \
    --silent --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 15 --max-time 30 \
    https://api.openai.com/v1/models || true)"
  [[ "${OPENAI_STATUS}" == "401" ]] && exit 0
  sleep 2
done

echo "Mihomo cannot reach the OpenAI API through 127.0.0.1:7999 (HTTP ${OPENAI_STATUS:-none})" >&2
exit 1
