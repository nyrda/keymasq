#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/dev.sh"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/dev.sh [--detached]
       ./scripts/dev.sh restart [both|daemon|session|gui|all]
       ./scripts/dev.sh stop

Open or reattach to the Keymasq tmux workspace. Daemon and session start
immediately; F8 starts the GUI. --detached starts without attaching.

F5 restart both    F6 restart daemon    F7 restart session
F8 start/restart GUI    F9 detach    F10 stop everything

The header buttons perform the same actions. Only one checkout can own the
workspace at a time because the dev processes use the installed sockets.
EOF
}

ACTION="${1:-open}"
TARGET="${2:-both}"
case "${ACTION}" in
  -h|--help) usage; exit 0 ;;
  open|--detached|stop)
    if (( $# > 1 )); then usage >&2; exit 2; fi
    ;;
  restart)
    if (( $# > 2 )); then usage >&2; exit 2; fi
    case "${TARGET}" in
      both|daemon|session|gui|all) ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  *) usage >&2; exit 2 ;;
esac

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec nix develop "${REPO_ROOT}" -c "${SCRIPT_PATH}" "$@"
fi

if [[ "${ACTION}" == open && ( ! -t 0 || ! -t 1 ) ]]; then
  echo "Open dev.sh in a terminal, or use --detached." >&2
  exit 1
fi

for tool in tmux flock timeout; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "${tool} is missing. Re-enter the updated Nix dev shell." >&2
    exit 1
  fi
done

# A separate server keeps the layout and bindings local to Keymasq. Use one
# server per user, since even different worktrees share the runtime sockets.
TMUX_NAME="keymasq-dev"
SESSION="keymasq"
tmux_dev() {
  tmux -L "${TMUX_NAME}" -f "${SCRIPT_DIR}/dev.tmux.conf" "$@" 9>&-
}

message() {
  printf '%s\n' "$*"
  tmux_dev set-option -t "${SESSION}" @dev_message "$*" 2>/dev/null || true
}

fail() {
  message "$*" >&2
  exit 1
}

# Ignore overlapping clicks instead of queuing a series of unwanted restarts.
# The server and pane children must not inherit this lock.
exec 9>"${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/keymasq-dev-${UID}.lock"
flock -n 9 || fail "Another dev action is still running."

pane_for() {
  tmux_dev show-option -qv -t "${SESSION}" "@dev_$1"
}

pane_dead() {
  [[ "$(tmux_dev display-message -p -t "$1" '#{pane_dead}')" == 1 ]]
}

shell_quote() {
  local value="${1//\'/\'\\\'\'}"
  printf "'%s'" "${value}"
}

stop_panes() {
  local role pane channel
  local -a waiting=()
  for role in "$@"; do
    pane="$(pane_for "${role}")"
    [[ -n "${pane}" ]] || fail "Missing ${role} pane."
    if pane_dead "${pane}"; then
      continue
    fi

    # Install the hook before checking again, so a concurrent exit cannot be
    # missed. Each operation gets its own channel, including after a timeout.
    channel="keymasq-exit-$$-${role}"
    tmux_dev set-hook -p -t "${pane}" pane-died "wait-for -S ${channel}"
    waiting+=("${role}:${pane}:${channel}")
    if pane_dead "${pane}"; then
      tmux_dev wait-for -S "${channel}"
      continue
    fi
    if [[ "$(tmux_dev display-message -p -t "${pane}" '#{pane_in_mode}')" == 1 ]]; then
      tmux_dev send-keys -t "${pane}" -X cancel
    fi
    # Deliver Ctrl+C through the terminal, just like the manual dev flow.
    # This also reaches the daemon behind sudo's terminal handling.
    tmux_dev send-keys -t "${pane}" C-c
  done

  local entry
  for entry in "${waiting[@]}"; do
    IFS=: read -r role pane channel <<<"${entry}"
    # Consume the notification even when the pane has already exited.
    if ! timeout 15 tmux -L "${TMUX_NAME}" wait-for "${channel}" 9>&-; then
      fail "${role} did not stop within 15s. Check its pane, then retry."
    fi
    pane_dead "${pane}" || fail "${role} is still running; restart cancelled."
    tmux_dev set-hook -pu -t "${pane}" pane-died
  done
}

start_pane() {
  local role="$1"
  local launcher
  local -a args=()
  case "${role}" in
    daemon) launcher=dev-keymasqd.sh; args=(-v) ;;
    session) launcher=dev-session.sh; args=(-v) ;;
    gui) launcher=dev-gui.sh ;;
  esac
  # Respawn only exited panes. Reusing the launcher refreshes the daemon's
  # staged source and preserves the established user and Nix environment.
  tmux_dev respawn-pane -t "$(pane_for "${role}")" -c "${REPO_ROOT}" \
    "${BASH}" "${SCRIPT_DIR}/${launcher}" "${args[@]}"
}

create_workspace() {
  local daemon_pane session_pane gui_pane control
  export REPO_ROOT
  export PYTHONUNBUFFERED=1
  export KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=0

  daemon_pane="$(tmux_dev new-session -d -P -F '#{pane_id}' \
    -s "${SESSION}" -n runtime -x 120 -y 36 -c "${REPO_ROOT}" \
    "${BASH}" -c 'printf "Starting daemon...\n"')"
  tmux_dev set-option -g default-shell "${BASH}"
  tmux_dev set-option -t "${SESSION}" @dev_root "${REPO_ROOT}"
  # run-shell uses /bin/sh. Quote each argument for that shell, including
  # checkout paths containing spaces, quotes, or shell metacharacters.
  control="$(shell_quote "${BASH}") $(shell_quote "${SCRIPT_PATH}")"
  tmux_dev set-option -t "${SESSION}" @dev_control "${control}"

  gui_pane="$(tmux_dev split-window -d -v -l 25% -P -F '#{pane_id}' \
    -t "${daemon_pane}" -c "${REPO_ROOT}" \
    "${BASH}" -c 'printf "Press F8 or click GUI to launch the GTK window.\n"')"
  session_pane="$(tmux_dev split-window -d -h -l 50% -P -F '#{pane_id}' \
    -t "${daemon_pane}" -c "${REPO_ROOT}" \
    "${BASH}" -c 'printf "Starting session...\n"')"

  tmux_dev set-option -t "${SESSION}" @dev_daemon "${daemon_pane}"
  tmux_dev set-option -t "${SESSION}" @dev_session "${session_pane}"
  tmux_dev set-option -t "${SESSION}" @dev_gui "${gui_pane}"
  tmux_dev select-pane -t "${daemon_pane}" -T keymasqd
  tmux_dev select-pane -t "${session_pane}" -T keymasq-session
  tmux_dev select-pane -t "${gui_pane}" -T GUI
  tmux_dev select-pane -t "${daemon_pane}"
  stop_panes session daemon
  start_pane daemon
  start_pane session
  message "Daemon and session launched. F8 starts the GUI."
}

if tmux_dev has-session -t "${SESSION}" 2>/dev/null; then
  owner="$(tmux_dev show-option -qv -t "${SESSION}" @dev_root)"
  if [[ "${owner}" != "${REPO_ROOT}" ]]; then
    fail "Dev workspace belongs to ${owner:-another checkout}. Stop it from that checkout first."
  fi
elif [[ "${ACTION}" == open || "${ACTION}" == --detached ]]; then
  create_workspace
elif [[ "${ACTION}" == stop ]]; then
  message "No dev workspace is running."
  exit 0
else
  fail "No dev workspace is running. Start ./scripts/dev.sh first."
fi

case "${ACTION}" in
  open)
    flock -u 9
    exec 9>&-
    # Explicit socket targeting lets this also work from inside another tmux.
    unset TMUX
    exec tmux -L "${TMUX_NAME}" attach-session -t "${SESSION}"
    ;;
  --detached) ;;
  restart)
    message "Restarting ${TARGET}..."
    case "${TARGET}" in
      both) roles=(session daemon); starts=(daemon session) ;;
      all) roles=(gui session daemon); starts=(daemon session gui) ;;
      *) roles=("${TARGET}"); starts=("${TARGET}") ;;
    esac
    stop_panes "${roles[@]}"
    for role in "${starts[@]}"; do start_pane "${role}"; done
    message "Launched ${TARGET}. Check panes for startup errors."
    ;;
  stop)
    message "Stopping GUI, session, and daemon..."
    stop_panes gui session daemon
    tmux_dev kill-session -t "${SESSION}"
    ;;
esac
