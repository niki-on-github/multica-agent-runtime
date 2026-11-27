# shellcheck shell=bash
# Claude Code defaults for the tasks: an earlier auto-compact window and the cold-resume guard.
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

# CLAUDE_RESUME_GUARD (summarize | digest | off, default summarize): MULTICA_CLAUDE_PATH -> guard shim, which
# execs what MULTICA_CLAUDE_PATH was before (the sandbox shim, a custom path or the real claude). Runs after
# setup_sandbox.
setup_resume_guard() {
  command -v claude >/dev/null || return 0
  local mode=${CLAUDE_RESUME_GUARD:-summarize}
  case "$mode" in
    off | 0 | false | no)
      agent_env CLAUDE_RESUME_GUARD off
      return 0
      ;;
    summarize | digest) ;;
    *)
      warn "CLAUDE_RESUME_GUARD=$mode unknown (summarize, digest, off): using summarize"
      mode=summarize
      ;;
  esac
  local next=${MULTICA_CLAUDE_PATH:-}
  if [ "$next" = "$GUARD_SHIMS/claude" ]; then
    return 0 # already wrapped (the entrypoint ran twice)
  fi
  [ -n "$next" ] || next=$(command -v claude)
  export CLAUDE_RESUME_GUARD=$mode CLAUDE_RESUME_GUARD_NEXT=$next MULTICA_CLAUDE_PATH=$GUARD_SHIMS/claude
  agent_env CLAUDE_RESUME_GUARD "$mode"
  log "claude: resume guard $mode (cold after ${CLAUDE_RESUME_GUARD_IDLE_MINUTES:-30} min, from ${CLAUDE_RESUME_GUARD_MIN_TOKENS:-150000} tokens; then $next)"
}
