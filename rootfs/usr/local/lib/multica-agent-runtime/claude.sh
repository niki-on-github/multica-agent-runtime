# shellcheck shell=bash
# Claude Code defaults for the tasks: an earlier auto-compact window and the cold-resume guard (multica-cold-resume).
GUARD_SHIMS=/usr/local/lib/multica-agent-runtime/guard-bin
CLAUDE_DEFAULT_AUTO_COMPACT_WINDOW=400000

# CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000 unless the variable or a Claude settings file already sets the
# window: compacting at 400k instead of near 1M keeps every later (cold) resume affordable. Set the variable
# to `off` (or empty) to keep Claude's own default.
claude_settings_window() {
  local f
  for f in "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" /etc/claude-code/managed-settings.json; do
    [ -r "$f" ] || continue
    if jq -e '(.autoCompactWindow // .env.CLAUDE_CODE_AUTO_COMPACT_WINDOW // empty) | tostring | length > 0' \
      "$f" >/dev/null 2>&1; then
      echo "$f"
      return 0
    fi
  done
  return 1
}

setup_claude_defaults() {
  command -v claude >/dev/null || return 0
  local src
  if [ "${CLAUDE_CODE_AUTO_COMPACT_WINDOW+set}" = set ]; then
    case "$CLAUDE_CODE_AUTO_COMPACT_WINDOW" in
      "" | off | CHANGEME)
        unset CLAUDE_CODE_AUTO_COMPACT_WINDOW
        log "claude: auto-compact window: Claude's default (CLAUDE_CODE_AUTO_COMPACT_WINDOW=off)"
        ;;
      *) agent_env CLAUDE_CODE_AUTO_COMPACT_WINDOW "$CLAUDE_CODE_AUTO_COMPACT_WINDOW" ;;
    esac
  elif src=$(claude_settings_window); then
    log "claude: auto-compact window from $src"
  else
    agent_env CLAUDE_CODE_AUTO_COMPACT_WINDOW "$CLAUDE_DEFAULT_AUTO_COMPACT_WINDOW"
  fi
}

# COLD_RESUME (default on, `off` disables): MULTICA_CLAUDE_PATH -> guard shim, which execs what
# MULTICA_CLAUDE_PATH was before (the sandbox shim, a custom path or the real claude). Runs after setup_sandbox.
setup_cold_resume() {
  command -v claude >/dev/null || return 0
  if [ -n "${CLAUDE_RESUME_GUARD:-}" ]; then
    warn "CLAUDE_RESUME_GUARD is replaced by COLD_RESUME (on/off)"
    case "$CLAUDE_RESUME_GUARD" in off | 0 | false | no) : "${COLD_RESUME:=off}" ;; esac
  fi
  case "${COLD_RESUME:-on}" in
    off | 0 | false | no)
      agent_env COLD_RESUME off
      return 0
      ;;
  esac
  local next=${MULTICA_CLAUDE_PATH:-}
  if [ "$next" = "$GUARD_SHIMS/claude" ]; then
    return 0 # already wrapped (the entrypoint ran twice)
  fi
  [ -n "$next" ] || next=$(command -v claude)
  export COLD_RESUME_NEXT=$next MULTICA_CLAUDE_PATH=$GUARD_SHIMS/claude
  agent_env COLD_RESUME on
  local lifetime="Claude's cache lifetime"
  [ -z "${COLD_RESUME_IDLE_MINUTES:-}" ] || lifetime="$COLD_RESUME_IDLE_MINUTES min idle"
  log "claude: cold-resume guard on (from ${COLD_RESUME_MIN_TOKENS:-150000} tokens, after $lifetime or a model switch; then $next)"
}
