#!/usr/bin/env bash

material_restore_authorized_key() {
  local authorized_keys="$1" keys_existed="$2" user_created="$3"
  local ssh_dir_existed="$4" ssh_dir="$5" backup_key="$6"
  local tunnel_user="$7"
  if [[ "${keys_existed}" -eq 1 ]]; then
    install -o "${tunnel_user}" -g "${tunnel_user}" -m 0600 \
      "${backup_key}" "${authorized_keys}"
  elif [[ "${user_created}" -eq 0 ]]; then
    rm -f "${authorized_keys}"
    if [[ "${ssh_dir_existed}" -eq 0 ]]; then
      rmdir "${ssh_dir}" 2>/dev/null || true
    fi
  fi
}

material_restore_release() {
  local source_link="$1" previous_source="$2" legacy_source="$3"
  local unit_existed="$4" backup_unit="$5" unit_path="$6"
  local env_created="$7" env_file="$8" release_dir="$9"
  rm -f "${source_link}"
  if [[ -n "${previous_source}" ]]; then
    ln -s "${previous_source}" "${source_link}"
  elif [[ -n "${legacy_source}" && -d "${legacy_source}" ]]; then
    mv "${legacy_source}" "${source_link}"
  fi
  if [[ "${unit_existed}" -eq 1 ]]; then
    install -m 0644 "${backup_unit}" "${unit_path}"
  else
    rm -f "${unit_path}"
  fi
  if [[ "${env_created}" -eq 1 ]]; then
    rm -f "${env_file}"
  fi
  if [[ -n "${release_dir}" && -d "${release_dir}" ]]; then
    rm -rf "${release_dir}"
  fi
}
