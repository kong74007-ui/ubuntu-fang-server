#!/usr/bin/env bash

pixelle_backup_managed_file() {
  local source_path="$1"
  local backup_path="$2"
  local marker_name="$3"

  if [[ -e "${source_path}" || -L "${source_path}" ]]; then
    cp -a -- "${source_path}" "${backup_path}"
    printf -v "${marker_name}" '%s' 1
  fi
}

pixelle_restore_managed_file() {
  local target_path="$1"
  local backup_path="$2"
  local existed="$3"

  if [[ "${existed}" -eq 1 ]]; then
    cp -a -- "${backup_path}" "${target_path}.restore.$$"
    mv -f "${target_path}.restore.$$" "${target_path}"
  else
    rm -f -- "${target_path}"
  fi
}

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
