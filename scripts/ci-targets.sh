# Sourced by GitHub Actions jobs to keep targeted CI path lists in one place.

KEYMASQ_KEYMASQD_TEST_TARGETS=(
  tests/keymasqd
  tests/test_capture_manager.py
  tests/test_grabbed_device.py
  tests/test_macro_file.py
  tests/test_macro_store.py
  tests/test_macro_store_internal.py
  tests/test_output_helpers.py
  tests/test_profile_handoff.py
  tests/test_record_cli.py
  tests/test_recording_extended.py
  tests/test_socket_server.py
  tests/test_superkey_state.py
  tests/test_superkeys.py
)

KEYMASQ_SESSION_TEST_TARGETS=(
  tests/session
  tests/test_action_handler.py
  tests/test_analog_controls.py
  tests/test_base_listener.py
  tests/test_compositor.py
  tests/test_config_loading.py
  tests/test_conflicts.py
  tests/test_gnome_listener.py
  tests/test_gnome_shell.py
  tests/test_hyprland_listener.py
  tests/test_keymasqd_client.py
  tests/test_listener_slurp_cursor.py
  tests/test_listener_socket_helpers.py
  tests/test_niri_listener.py
  tests/test_session_clients.py
  tests/test_session_config_files.py
  tests/test_session_hardware.py
  tests/test_session_manager_compositor.py
  tests/test_session_manager_core.py
  tests/test_session_manager_events.py
  tests/test_session_manager_recording.py
  tests/test_session_support.py
  tests/test_slurp.py
  tests/test_toml.py
  tests/test_wayland_protocol_trackers.py
  tests/test_wayland_toplevel_listener.py
  tests/test_wayland_wlr_listener.py
  tests/test_x11_listener.py
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
