# shellcheck shell=bash
# Shared helpers of entrypoint.sh and runtime-doctor.

log() { printf '%s [agent-runtime] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
warn() { log "WARN: $*"; }

# set and not the placeholder CHANGEME (the cluster secrets use it for "unset")
is_set() {
  local val=${!1:-}
  [ -n "$val" ] && [ "$val" != CHANGEME ]
}

# drops empty and CHANGEME values, so tools do not try to use them
unset_placeholders() {
  local v
  for v in MULTICA_TOKEN GH_TOKEN GITEA_URL GITEA_TOKEN GITEA_USER ANTHROPIC_API_KEY ANTHROPIC_BASE_URL \
    CLAUDE_CODE_OAUTH_TOKEN OPENAI_API_KEY LLM_API_KEY GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL \
    GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL; do
    if ! is_set "$v"; then
      unset "$v"
    fi
  done
}

# the runtime environment the entrypoint resolved; sourced by login shells (/etc/profile.d/agent.sh)
AGENT_ENV_FILE="${XDG_RUNTIME_DIR:-/run/agent}/agent.env"
agent_env() {
  local name=$1 value=$2
  export "$name=$value"
  printf 'export %s=%q\n' "$name" "$value" >> "$AGENT_ENV_FILE"
}

# kernel/container facts used by the Docker preflight and runtime-doctor
seccomp_mode() { awk '/^Seccomp:/ {print $2}' /proc/self/status; }
no_new_privs() { awk '/^NoNewPrivs:/ {print $2}' /proc/self/status; }
apparmor_profile() {
  local p
  p=$(tr -d '\0' < /proc/self/attr/current 2>/dev/null) || p=
  echo "${p:-none}"
}
# true when this process is in a user namespace other than the host's (e.g. hostUsers: false)
in_userns() { ! grep -qE '^[[:space:]]*0[[:space:]]+0[[:space:]]+4294967295' /proc/self/uid_map; }
userns_size() { awk '{s += $3} END {print s + 0}' /proc/self/uid_map; }
has_cap() {
  local bit=$1 capeff
  capeff=$(awk '/^CapEff:/ {print $2}' /proc/self/status)
  (( (16#$capeff >> bit) & 1 ))
}
# capability in the bounding set (what a setuid binary like newuidmap can get)
has_bound_cap() {
  local bit=$1 capbnd
  capbnd=$(awk '/^CapBnd:/ {print $2}' /proc/self/status)
  (( (16#$capbnd >> bit) & 1 ))
}
# masked /proc paths (container default) make the kernel refuse a fresh proc mount (rootlesskit)
proc_masked() {
  awk '{print $5}' /proc/self/mountinfo | grep -qE '^/proc/(kcore|keys|timer_list|sched_debug|scsi|acpi|latency_stats)$'
}
