#!/usr/bin/env bash

pixelle_require_service_stopped() {
  local service_name="$1"
  local active_state

  if ! systemctl stop "${service_name}"; then
    echo "failed to stop ${service_name}; refusing source switch" >&2
    return 1
  fi
  if ! active_state="$(systemctl show --property=ActiveState --value "${service_name}")"; then
    echo "failed to verify ${service_name} state; refusing source switch" >&2
    return 1
  fi
  if [[ "${active_state}" != "inactive" ]]; then
    echo "${service_name} is ${active_state}, not inactive; refusing source switch" >&2
    return 1
  fi
}

pixelle_run_with_service_stopped() {
  local service_name="$1"
  local callback="$2"
  shift 2

  pixelle_require_service_stopped "${service_name}" || return 1
  "${callback}" "$@"
}
