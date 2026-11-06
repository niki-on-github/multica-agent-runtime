# shellcheck shell=bash
# Docker-in-Docker with the real dockerd, mode chosen at runtime (DIND_MODE):
#   auto      existing daemon at DOCKER_HOST -> external; root + privileged -> rootful;
#             rootless prerequisites met -> rootless; otherwise off (the reasons are logged)
#   rootless  dockerd-rootless.sh as the agent (rootlesskit + slirp4netns)
#   rootful   dockerd as root (privileged container), the agent uses it through the docker group
#   external  only the CLI, against DOCKER_HOST (sidecar, or the daemon container of the same pod)
#   off       no Docker
# Both daemons listen on DOCKER_HOST (unix:///run/agent/docker.sock), so a second container that
# shares /run/agent (emptyDir) uses the same daemon.

DIND_SOCK=${DOCKER_HOST#unix://}
DIND_LOG="${XDG_RUNTIME_DIR}/dockerd.log"
DIND_CONFIG="${XDG_RUNTIME_DIR}/docker-daemon.json"
DIND_DATA_ROOT=${DIND_DATA_ROOT:-/data/docker}

dind_ping() { timeout 5 docker version --format '{{.Server.Version}}' >/dev/null 2>&1; }

# one line per reason why rootless Docker will not start; empty output = looks fine
dind_rootless_blockers() {
  if [ "$(seccomp_mode)" != 0 ]; then
    echo "seccomp filter active (pod: securityContext.seccompProfile.type Unconfined)"
  fi
  local aa
  aa=$(apparmor_profile)
  case "$aa" in
    none | unconfined | rootlesskit* | *'(unconfined)'*) ;;
    *) echo "AppArmor profile '$aa' (pod: securityContext.appArmorProfile.type Unconfined)" ;;
  esac
  if proc_masked; then
    echo "/proc is masked (pod: securityContext.procMount Unmasked, requires hostUsers: false)"
  elif [ -n "$(proc_overmounts)" ]; then
    echo "mounts on top of /proc ($(proc_overmounts | tr '\n' ' ')): nested containers cannot mount /proc (start as root so the entrypoint can remove them)"
  fi
  if [ ! -c /dev/net/tun ]; then
    echo "/dev/net/tun missing (rootlesskit's network driver needs it: device plugin or hostPath CharDevice)"
  fi
  if [ "$(no_new_privs)" != 0 ]; then
    echo "no_new_privs is set, setuid newuidmap cannot work (pod: allowPrivilegeEscalation: true)"
  fi
  # newuidmap needs CAP_SYS_ADMIN in the bounding set to write a multi-range map (tested on 6.18);
  # the agent itself runs without effective capabilities
  if ! has_bound_cap 21; then
    echo "CAP_SYS_ADMIN not in the bounding set, newuidmap cannot write the id map (pod: capabilities.add: [SYS_ADMIN])"
  fi
  if [ ! -u /usr/bin/newuidmap ]; then
    echo "/usr/bin/newuidmap is not setuid"
  fi
  if [ "$(id -u)" != 0 ] && in_userns && (( $(userns_size) < 165536 )) && grep -q '^agent:100000:' /etc/subuid; then
    echo "user namespace has only $(userns_size) ids: start the container as root (runAsUser: 0) so the entrypoint can fit /etc/subuid"
  fi
}

dind_resolve() {
  case "$DIND_MODE" in
    auto)
      if dind_ping; then
        DIND_MODE=external
      elif [ "$(id -u)" = 0 ] && ! in_userns && has_cap 21 && [ "$(seccomp_mode)" = 0 ]; then
        DIND_MODE=rootful
      else
        local blockers line
        blockers=$(dind_rootless_blockers)
        if [ -z "$blockers" ]; then
          DIND_MODE=rootless
        else
          warn "Docker disabled, rootless Docker is blocked by:"
          while IFS= read -r line; do warn "  - $line"; done <<<"$blockers"
          warn "DIND_MODE=rootless tries anyway; DIND_MODE=rootful needs a privileged container (README)"
          DIND_MODE=off
        fi
      fi
      ;;
    rootless | rootful | external | off) ;;
    *)
      warn "unknown DIND_MODE=$DIND_MODE, Docker disabled"
      DIND_MODE=off
      ;;
  esac
  export DIND_MODE
}

# inside a small user namespace (hostUsers: false gives 65536 ids) the default 100000:65536 range does
# not exist: use the ids above the agent's own uid (root only, /etc/subuid is root-owned)
dind_fit_subids() {
  in_userns || return 0
  local size count
  size=$(userns_size)
  (( size < 165536 )) || return 0
  count=$((size - 1001))
  if (( count < 1 )); then
    warn "user namespace has only $size ids, no room for subordinate ids"
    return 0
  fi
  echo "agent:1001:$count" > /etc/subuid
  echo "agent:1001:$count" > /etc/subgid
  log "user namespace with $size ids: subordinate ids agent:1001:$count (nested uids >= $count cannot be mapped)"
}

dind_write_config() {
  local mirrors='[]' cfg
  if is_set DOCKER_REGISTRY_MIRRORS; then
    mirrors=$(tr -s ', ' '\n' <<<"$DOCKER_REGISTRY_MIRRORS" | sed '/^$/d' | jq -Rn '[inputs]')
  fi
  cfg=$(jq -n --argjson mirrors "$mirrors" --arg cdi "$XDG_RUNTIME_DIR/cdi" \
    '{"registry-mirrors": $mirrors, features: {cdi: true}, "cdi-spec-dirs": ["/etc/cdi", "/var/run/cdi", $cdi]}')
  if command -v nvidia-container-runtime >/dev/null; then
    cfg=$(jq '. + {runtimes: {nvidia: {path: "nvidia-container-runtime"}}}' <<<"$cfg")
  fi
  if is_set DIND_STORAGE_DRIVER; then
    cfg=$(jq --arg d "$DIND_STORAGE_DRIVER" '. + {"storage-driver": $d}' <<<"$cfg")
  fi
  if is_set DIND_DAEMON_JSON; then
    cfg=$(jq --argjson extra "$DIND_DAEMON_JSON" '. * $extra' <<<"$cfg")
  fi
  printf '%s\n' "$cfg" > "$DIND_CONFIG"
}

# State of a previous daemon in $XDG_RUNTIME_DIR (an emptyDir survives container restarts). Files a
# rootless daemon created belong to its subordinate ids, which only root of this container may remove:
# the root phase cleans up first, the agent's own pass is best effort.
dind_clean_state() {
  rm -f "$DIND_SOCK" "$XDG_RUNTIME_DIR/docker.pid" 2>/dev/null || :
  rm -rf "$XDG_RUNTIME_DIR/dockerd-rootless" "$XDG_RUNTIME_DIR/docker" 2>/dev/null \
    || warn "could not remove the state of a previous dockerd in $XDG_RUNTIME_DIR"
}

dind_prepare() {
  dind_clean_state
  : > "$DIND_LOG"
  mkdir -p "$DIND_DATA_ROOT"
  if [ "$(stat -f -c %T "$DIND_DATA_ROOT" 2>/dev/null)" = overlayfs ]; then
    warn "$DIND_DATA_ROOT is on the container's overlay filesystem: mount a volume at /data (nested overlays fail)"
  fi
  dind_write_config
}

dind_start_rootful() {
  # cgroup v2 nesting, as moby's hack/dind: move this container's processes into a child cgroup,
  # then delegate all controllers to dockerd's containers
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers \
      > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || warn "could not enable the cgroup controllers"
  fi
  if [ -d /sys/kernel/security ] && ! mountpoint -q /sys/kernel/security; then
    mount -t securityfs none /sys/kernel/security 2>/dev/null || :
  fi
  if [ "${DIND_IPTABLES:-}" = legacy ]; then
    update-alternatives --set iptables /usr/sbin/iptables-legacy >/dev/null
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy >/dev/null
  fi
  mkdir -p "$DIND_DATA_ROOT/rootful"
  dockerd --config-file "$DIND_CONFIG" --host "$DOCKER_HOST" --group docker \
    --data-root "$DIND_DATA_ROOT/rootful" >>"$DIND_LOG" 2>&1 &
  log "Docker: starting rootful dockerd (log $DIND_LOG)"
}

dind_start_rootless() {
  export DOCKERD_ROOTLESS_ROOTLESSKIT_NET=${DOCKERD_ROOTLESS_ROOTLESSKIT_NET:-slirp4netns}
  mkdir -p "$DIND_DATA_ROOT/rootless"
  dockerd-rootless.sh --config-file "$DIND_CONFIG" --host "$DOCKER_HOST" \
    --data-root "$DIND_DATA_ROOT/rootless" >>"$DIND_LOG" 2>&1 &
  DIND_PID=$!
  log "Docker: starting rootless dockerd, network $DOCKERD_ROOTLESS_ROOTLESSKIT_NET (log $DIND_LOG)"
}

dind_wait() {
  local timeout=${DIND_START_TIMEOUT:-60} i
  for ((i = 0; i < timeout; i++)); do
    if dind_ping; then
      return 0
    fi
    # started here and already gone: no point in waiting
    if [ -n "${DIND_PID:-}" ] && ! kill -0 "$DIND_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  warn "Docker is not answering (mode $DIND_MODE, waited up to ${timeout}s); last log lines:"
  tail -n 25 "$DIND_LOG" >&2 2>/dev/null || :
  return 1
}

# container started as root: resolve the mode, fit the subordinate ids, start a rootful daemon
dind_root_phase() {
  dind_resolve
  case "$DIND_MODE" in
    rootless)
      dind_clean_state
      dind_fit_subids
      ;;
    rootful)
      dind_prepare
      dind_start_rootful
      ;;
  esac
}

# as the agent: start a rootless daemon, wait for whichever daemon is used, record the result
dind_agent_phase() {
  if [ -z "${DIND_RESOLVED:-}" ]; then
    dind_resolve
  fi
  local active=$DIND_MODE
  case "$DIND_MODE" in
    off) log "Docker: off" ;;
    external)
      if dind_ping; then
        log "Docker: using the daemon at $DOCKER_HOST"
      else
        log "Docker: no daemon at $DOCKER_HOST yet (external)"
      fi
      ;;
    rootful)
      dind_wait || active=failed
      ;;
    rootless)
      dind_prepare
      dind_start_rootless
      dind_wait || active=failed
      ;;
  esac
  if [ "$active" != failed ] && [ "$active" != off ] && dind_ping; then
    log "Docker: $(docker version --format 'server {{.Server.Version}}' 2>/dev/null) ($active)"
  fi
  agent_env DIND_ACTIVE_MODE "$active"
}
