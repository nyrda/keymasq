#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/dev.sh"
REPO_ROOT="${KEYMASQ_DEV_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

usage() {
  cat <<'EOF'
Usage: ./scripts/dev.sh [--detached] [switch [PATH]]
       ./scripts/dev.sh restart [both|daemon|session|gui|all]
       ./scripts/dev.sh stop

Open or reattach to the Keymasq tmux workspace. Daemon and session start
immediately; F8 starts the GUI. --detached starts without attaching.
Opening from another worktree gracefully stops the old processes and switches
to this checkout. A running GUI reopens from the new checkout too.

F4 choose worktree (type to filter, Enter to switch, Esc to cancel)
F5 restart both    F6 restart daemon    F7 restart session
F8 start/restart GUI    F9 detach    F10 stop everything

switch opens the picker, or selects PATH directly. Worktrees are newest first,
using filesystem creation time, or .git modification time if unavailable.
Restart and stop operate on the active workspace, from any checkout.
The header buttons perform the same actions. A switch waits for all old
processes to exit; if one will not stop, the switch is cancelled.
The workspace reuses its initial Nix dev shell across worktrees. Stop and
reopen the workspace after changing Nix dependencies.
EOF
}

DETACHED=0
if [[ "${1:-}" == --detached ]]; then
  DETACHED=1
  shift
fi
ACTION="${1:-open}"
TARGET="${2:-both}"
case "${ACTION}" in
  -h|--help) usage; exit 0 ;;
  open|stop)
    if (( $# > 1 )); then usage >&2; exit 2; fi
    ;;
  switch)
    if (( $# > 2 )); then usage >&2; exit 2; fi
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
  # Preserve --detached after parsing it above.
  args=()
  if (( DETACHED )); then args+=(--detached); fi
  exec nix develop "${REPO_ROOT}" -c "${SCRIPT_PATH}" "${args[@]}" "$@"
fi

if [[ "${ACTION}" == open || "${ACTION}" == switch ]] \
  && (( ! DETACHED )) && [[ ! -t 0 || ! -t 1 ]]; then
  echo "Open dev.sh in a terminal, or use --detached." >&2
  exit 1
fi

for tool in tmux flock timeout fzf; do
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

success() {
  printf '%s\n' "$*"
  tmux_dev set-option -t "${SESSION}" @dev_message ''
}

fail() {
  message "$*" >&2
  exit 1
}

choose_worktree() {
  local field path="" branch="" head="" active created modified row selection index
  local -a paths=() rows=()
  active="$(tmux_dev show-option -qv -t "${SESSION}" @dev_root 2>/dev/null || true)"
  while IFS= read -r -d '' field; do
    case "${field}" in
      'worktree '*) path="${field#worktree }" ;;
      'branch '*) branch="${field#branch refs/heads/}" ;;
      'HEAD '*) head="${field#HEAD }" ;;
      '')
        if [[ -f "${path}/flake.nix" && -e "${path}/.git" ]]; then
          read -r created modified < <(stat -c '%W %Y' -- "${path}/.git")
          if (( created == 0 )); then created="${modified}"; fi
          # Keep raw paths in an array, separate from escaped display text.
          printf -v row '%s\t%s\t%s %s\t%q' "${created}" "${#paths[@]}" \
            "$([[ "${path}" == "${active}" ]] && printf '*' || printf ' ')" \
            "${branch:-detached @ ${head:0:8}}" "${path}"
          rows+=("${row}")
          paths+=("${path}")
        fi
        path=""; branch=""; head=""
        ;;
    esac
  done < <(git -C "${REPO_ROOT}" worktree list --porcelain -z)

  (( ${#paths[@]} )) || fail "No available worktrees found."
  if ! selection="$(printf '%s\0' "${rows[@]}" | sort -z -t $'\t' -k1,1nr \
    | FZF_DEFAULT_OPTS='' FZF_DEFAULT_OPTS_FILE='' fzf --read0 --no-sort \
      --delimiter=$'\t' --with-nth=3.. --layout=reverse --border \
      --prompt='Worktree > ' --header='Newest first | * active | Enter switch | Esc cancel')"; then
    exit 0
  fi
  selection="${selection#*$'\t'}"
  index="${selection%%$'\t'*}"
  REPO_ROOT="${paths[index]}"
}

if [[ "${ACTION}" == switch ]]; then
  if [[ -n "${2:-}" ]]; then
    requested_root="$(cd -- "$2" && pwd)" || fail "Worktree not found: $2"
    common="$(git -C "${REPO_ROOT}" rev-parse --path-format=absolute --git-common-dir)"
    requested_common="$(git -C "${requested_root}" rev-parse --path-format=absolute --git-common-dir)" \
      || fail "Not a Git worktree: ${requested_root}"
    [[ "${common}" == "${requested_common}" ]] || fail "Choose a worktree of this repository."
    REPO_ROOT="${requested_root}"
  else
    choose_worktree
  fi
fi

# Choose first so leaving a picker open does not block restart/stop controls.
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
  # Reuse the tmux server's Nix dev shell. Only the source checkout changes;
  # remove its old Python imports before the launcher sets the new path.
  tmux_dev respawn-pane -t "$(pane_for "${role}")" -c "${REPO_ROOT}" \
    env -u PYTHONPATH REPO_ROOT="${REPO_ROOT}" \
    PYTHONUNBUFFERED=1 KEYMASQ_SESSION_RESTART_ON_DAEMON_DISCONNECT=0 \
    "${BASH}" "${REPO_ROOT}/scripts/${launcher}" "${args[@]}"
}

validate_worktree() {
  local file
  for file in flake.nix scripts/dev-keymasqd.sh scripts/dev-session.sh scripts/dev-gui.sh; do
    [[ -f "${REPO_ROOT}/${file}" ]] || fail "Missing ${file} in ${REPO_ROOT}."
  done
}

configure_workspace() {
  local controller control branch file
  tmux_dev source-file "${SCRIPT_DIR}/dev.tmux.conf"
  tmux_dev set-option -g default-shell "${BASH}"
  tmux_dev set-option -t "${SESSION}" @dev_root "${REPO_ROOT}"
  branch="$(git -C "${REPO_ROOT}" symbolic-ref --short -q HEAD \
    || git -C "${REPO_ROOT}" rev-parse --short HEAD)"
  tmux_dev set-option -t "${SESSION}" @dev_branch "${branch}"
  # Keep the controller outside Git worktrees: the previous checkout may be
  # deleted, and an older selected checkout may not have a worktree picker.
  controller="$(tmux_dev show-option -qv -t "${SESSION}" @dev_controller)"
  if [[ -z "${controller}" ]]; then
    controller="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/keymasq-dev-control.XXXXXXXX")"
    tmux_dev set-option -t "${SESSION}" @dev_controller "${controller}"
  fi
  if [[ "${controller}" != "${SCRIPT_DIR}" ]]; then
    for file in dev.sh dev.tmux.conf; do
      cp -- "${SCRIPT_DIR}/${file}" "${controller}/${file}.new"
      mv -- "${controller}/${file}.new" "${controller}/${file}"
    done
  fi
  # Retain the controller's tools without depending on its checkout's flake.
  # Replace files atomically so a running picker can finish during an update.
  printf '#!/usr/bin/env bash\nexec env PATH=%s IN_NIX_SHELL=impure KEYMASQ_DEV_ROOT=%s %s %s "$@"\n' \
    "$(shell_quote "${PATH}")" "$(shell_quote "${REPO_ROOT}")" \
    "$(shell_quote "${BASH}")" "$(shell_quote "${controller}/dev.sh")" \
    > "${controller}/control.sh.new"
  mv -- "${controller}/control.sh.new" "${controller}/control.sh"
  control="$(shell_quote "${BASH}") $(shell_quote "${controller}/control.sh")"
  tmux_dev set-option -t "${SESSION}" @dev_control "${control}"
  tmux_dev set-option -t "${SESSION}" @dev_scripts "${controller}"
}

create_workspace() {
  local daemon_pane session_pane gui_pane
  daemon_pane="$(tmux_dev new-session -d -P -F '#{pane_id}' \
    -s "${SESSION}" -n runtime -x 120 -y 36 -c "${REPO_ROOT}" \
    "${BASH}" -c 'printf "Starting daemon...\n"')"
  configure_workspace

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
  success "Daemon and session launched. F8 starts the GUI."
}

if tmux_dev has-session -t "${SESSION}" 2>/dev/null; then
  owner="$(tmux_dev show-option -qv -t "${SESSION}" @dev_root)"
  if [[ "${ACTION}" == restart || "${ACTION}" == stop ]]; then
    REPO_ROOT="${owner}"
  elif [[ "${owner}" != "${REPO_ROOT}" ]]; then
    validate_worktree
    restart_gui=0
    if ! pane_dead "$(pane_for gui)"; then restart_gui=1; fi
    message "Stopping old processes before switching..."
    stop_panes gui session daemon
    configure_workspace
    start_pane daemon
    start_pane session
    if (( restart_gui )); then start_pane gui; fi
    success "Switched worktree. Check panes for startup errors."
  else
    configure_workspace
  fi
elif [[ "${ACTION}" == open || "${ACTION}" == switch ]]; then
  validate_worktree
  create_workspace
elif [[ "${ACTION}" == stop ]]; then
  message "No dev workspace is running."
  exit 0
else
  fail "No dev workspace is running. Start ./scripts/dev.sh first."
fi

case "${ACTION}" in
  open|switch)
    if (( DETACHED )); then exit 0; fi
    flock -u 9
    exec 9>&-
    # Explicit socket targeting lets this also work from inside another tmux.
    unset TMUX
    exec tmux -L "${TMUX_NAME}" attach-session -t "${SESSION}"
    ;;
  restart)
    message "Restarting ${TARGET}..."
    case "${TARGET}" in
      both) roles=(session daemon); starts=(daemon session) ;;
      all) roles=(gui session daemon); starts=(daemon session gui) ;;
      *) roles=("${TARGET}"); starts=("${TARGET}") ;;
    esac
    stop_panes "${roles[@]}"
    for role in "${starts[@]}"; do start_pane "${role}"; done
    success "Launched ${TARGET}. Check panes for startup errors."
    ;;
  stop)
    message "Stopping GUI, session, and daemon..."
    stop_panes gui session daemon
    controller="$(tmux_dev show-option -qv -t "${SESSION}" @dev_controller)"
    tmux_dev kill-session -t "${SESSION}"
    if [[ -n "${controller}" ]]; then
      rm -f -- "${controller}/dev.sh" "${controller}/dev.tmux.conf" "${controller}/control.sh"
      rmdir -- "${controller}"
    fi
    ;;
esac
