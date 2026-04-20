#!/usr/bin/env bash

normalize_dev_shell_for_pkexec() {
  local current_shell="${SHELL:-}"
  local login_shell=""
  local candidate=""

  if [[ -n "${current_shell}" ]] \
    && [[ -x "${current_shell}" ]] \
    && grep -Fxq "${current_shell}" /etc/shells 2>/dev/null; then
    return
  fi

  if command -v getent >/dev/null 2>&1; then
    login_shell="$(getent passwd "$(id -un)" | cut -d: -f7 || true)"
  fi

  for candidate in \
    "${login_shell}" \
    /run/current-system/sw/bin/bash \
    /run/current-system/sw/bin/sh \
    /bin/bash \
    /bin/sh; do
    if [[ -n "${candidate}" ]] \
      && [[ -x "${candidate}" ]] \
      && grep -Fxq "${candidate}" /etc/shells 2>/dev/null; then
      export SHELL="${candidate}"
      return
    fi
  done
}
