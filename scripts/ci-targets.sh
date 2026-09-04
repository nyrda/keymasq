# Sourced by GitHub Actions jobs to keep targeted CI path lists in one place.

KEYMASQ_KEYMASQD_TEST_TARGETS=(
  tests/keymasqd
)

KEYMASQ_SESSION_TEST_TARGETS=(
  tests/session
)

KEYMASQ_GUI_TEST_TARGETS=(
  tests/gui
)

KEYMASQ_KEYMASQD_TYPECHECK_TARGETS=(
  keymasq/keymasqd
  "${KEYMASQ_KEYMASQD_TEST_TARGETS[@]}"
)

KEYMASQ_SESSION_TYPECHECK_TARGETS=(
  keymasq/session
  "${KEYMASQ_SESSION_TEST_TARGETS[@]}"
)

KEYMASQ_GUI_TYPECHECK_TARGETS=(
  keymasq/gui
  "${KEYMASQ_GUI_TEST_TARGETS[@]}"
)

keymasq_ci_append_unique() {
  local -n target_list_ref="$1"
  local -n seen_ref="$2"
  shift 2

  local target
  for target in "$@"; do
    if [[ -z "${seen_ref[$target]+x}" ]]; then
      target_list_ref+=("$target")
      seen_ref["$target"]=1
    fi
  done
}

keymasq_ci_validate_targets() {
  local target
  for target in "$@"; do
    if [[ ! -e "$target" ]]; then
      echo "missing CI target: $target" >&2
      return 1
    fi
  done
}

keymasq_ci_append_pytest_workers() {
  local -n pytest_args_ref="$1"
  local workers_env_name="$2"
  local default_workers="$3"
  local pytest_workers="${!workers_env_name:-$default_workers}"

  if [[ "$pytest_workers" != "0" && "$pytest_workers" != "1" ]]; then
    pytest_args_ref+=(-n "$pytest_workers")
  fi
}
