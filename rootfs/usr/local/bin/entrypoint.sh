#!/usr/bin/env bash
# Entrypoint of multica-agent-runtime, started by `tini -g` (signals reach dockerd and the main process).
#
#   daemon (default)  Multica daemon in the foreground; Docker per DIND_MODE (default auto)
#   shell             web terminal (ttyd) on $TTYD_PORT: login shell, `?arg=tmux` for the tmux
#                     session `main`; uses an existing
#                     Docker daemon (the daemon container of the same pod), never starts one
#   doctor [--gpu]    runtime-doctor
#   <command>         runs the command after the same setup (DIND_MODE default off)
#
# Started as root (rootful Docker, or rootless inside a user-namespaced pod) it prepares what needs
# root, then re-executes itself as the agent (uid 1000) with setpriv.
set -euo pipefail

LIB=/usr/local/lib/multica-agent-runtime
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/common.sh
. "$LIB/common.sh"
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/home.sh
. "$LIB/home.sh"
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/env.sh
. "$LIB/env.sh"
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/git-auth.sh
. "$LIB/git-auth.sh"
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/dind.sh
. "$LIB/dind.sh"
# shellcheck source=SCRIPTDIR/../lib/multica-agent-runtime/gpu.sh
. "$LIB/gpu.sh"

cmd=${1:-daemon}
case "$cmd" in
  daemon) : "${DIND_MODE:=auto}" ;;
  shell) DIND_MODE=external ;;
  *) : "${DIND_MODE:=off}" ;;
esac
export DIND_MODE
unset_placeholders

# --- root phase --------------------------------------------------------------------------------
if [ "$(id -u)" = 0 ]; then
  install -d -o 1000 -g 1000 -m 0700 "$XDG_RUNTIME_DIR"
  for d in "$HOME" /data; do
    if [ -d "$d" ] && [ "$(stat -c %u "$d")" != 1000 ]; then
      chown 1000:1000 "$d"
    fi
  done
  gpu_release_proc
  gpu_setup
  dind_root_phase
  export DIND_RESOLVED=1
  exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all -- "$0" "$@"
fi

# --- agent phase -------------------------------------------------------------------------------
# agent.env (sourced by login shells) belongs to the daemon container; a shell container in the same
# pod shares /run/agent and reads it, so other commands write their own file.
if [ "$cmd" != daemon ]; then
  AGENT_ENV_FILE="$XDG_RUNTIME_DIR/agent-$(basename "$cmd" | tr -cd 'a-zA-Z0-9_-').env"
fi
if ! : > "$AGENT_ENV_FILE" 2>/dev/null; then
  AGENT_ENV_FILE=$(mktemp)
fi
seed_home
setup_ca_env
# containers of one pod share $HOME and /run/agent: serialize the git/tea config writes
(
  flock -w 30 9 || warn "setup lock busy, continuing"
  setup_git || warn "git/tea setup incomplete"
) 9>"$XDG_RUNTIME_DIR/setup.lock"
gpu_setup
dind_agent_phase

run_daemon() {
  if ! is_set MULTICA_TOKEN; then
    log "MULTICA_TOKEN is not set: create a personal access token in Multica (Settings -> API Tokens),"
    log "put it into the runner secret and restart. Waiting."
    exec sleep infinity
  fi
  if ! is_set MULTICA_SERVER_URL; then
    warn "MULTICA_SERVER_URL is not set: the CLI uses its default server"
  fi
  multica version || true
  # registers the token and the workspaces it can see in $HOME/.multica
  multica login --token "$MULTICA_TOKEN"
  exec multica daemon start --foreground "$@"
}

run_shell() {
  log "web terminal on :$TTYD_PORT (login shell; ?arg=tmux for the tmux session main)"
  # TTYD_ARGS: extra ttyd flags, split on whitespace. --url-arg: web-shell.sh only accepts `tmux`.
  # shellcheck disable=SC2086
  exec ttyd --writable --port "$TTYD_PORT" --check-origin --url-arg ${TTYD_ARGS:-} "$LIB/web-shell.sh"
}

case "$cmd" in
  daemon)
    shift || true
    run_daemon "$@"
    ;;
  shell) run_shell ;;
  doctor)
    shift
    exec runtime-doctor "$@"
    ;;
  *) exec "$@" ;;
esac
