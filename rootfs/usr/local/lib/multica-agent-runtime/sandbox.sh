# shellcheck shell=bash
# Per-task sandbox (bubblewrap) for the agent CLIs the daemon starts. AGENT_SANDBOX:
#   off (default)  the daemon starts the CLIs directly
#   on             MULTICA_<CLI>_PATH -> sandbox shims; if bwrap cannot create namespaces here, log it
#                  and run unsandboxed
#   required       as on, but the daemon does not start without a working sandbox
# A MULTICA_<CLI>_PATH that is already set is kept (that CLI is then not sandboxed).
SANDBOX_SHIMS=/usr/local/lib/multica-agent-runtime/sandbox-bin

# the namespaces agent-sandbox needs: user (implicit), pid, ipc, uts, a fresh /proc
sandbox_preflight() {
  bwrap --ro-bind / / --unshare-pid --unshare-ipc --unshare-uts --proc /proc --dev /dev true 2>&1
}

setup_sandbox() {
  local mode=${AGENT_SANDBOX:-off} out cli var active=()
  case "$mode" in
    off | 0 | false | no) return 0 ;;
    on | 1 | true | yes) mode=on ;;
    required) ;;
    *)
      warn "AGENT_SANDBOX=$mode unknown (off, on, required): sandbox off"
      return 0
      ;;
  esac
  if ! out=$(sandbox_preflight); then
    if [ "$mode" = required ]; then
      log "sandbox: bwrap cannot create namespaces here: $out"
      log "AGENT_SANDBOX=required: not starting the daemon"
      exit 1
    fi
    warn "sandbox: bwrap cannot create namespaces here ($out): agents run unsandboxed"
    agent_env AGENT_SANDBOX_ACTIVE none
    return 0
  fi
  for cli in claude codex pi omp; do
    command -v "$cli" >/dev/null || continue
    var=MULTICA_${cli^^}_PATH
    if is_set "$var"; then
      warn "sandbox: $var is set, $cli runs unsandboxed"
      continue
    fi
    export "$var=$SANDBOX_SHIMS/$cli"
    active+=("$cli")
  done
  mkdir -p "${AGENT_SANDBOX_HOMES:-/data/agent-homes}"
  agent_env AGENT_SANDBOX_ACTIVE "${active[*]:-none}"
  log "sandbox: ${active[*]:-no CLI} in bubblewrap per task (homes ${AGENT_SANDBOX_HOMES:-/data/agent-homes}, pass-through: ${AGENT_SANDBOX_PASS_ENV:-none})"
}
