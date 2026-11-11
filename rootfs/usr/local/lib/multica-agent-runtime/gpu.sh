# shellcheck shell=bash
# GPU for nested containers (cuda image): a CDI spec of the devices and driver libraries this
# container sees (also below /nix/store on NixOS nodes), generated on every start. Root writes
# /etc/cdi, the agent $XDG_RUNTIME_DIR/cdi; dockerd reads both (cdi-spec-dirs).
# Use: docker run --device nvidia.com/gpu=all ...   (or --gpus all through the nvidia runtime)
gpu_setup() {
  command -v nvidia-ctk >/dev/null || return 0
  if [ ! -e /dev/nvidiactl ]; then
    log "GPU: none visible, no CDI spec"
    return 0
  fi
  local dir
  if [ "$(id -u)" = 0 ]; then
    dir=/etc/cdi
  else
    if [ -s /etc/cdi/nvidia.yaml ]; then
      agent_env GPU_CDI_SPEC /etc/cdi/nvidia.yaml
      return 0
    fi
    dir="$XDG_RUNTIME_DIR/cdi"
  fi
  mkdir -p "$dir"
  # NVIDIA_CDI_ARGS: extra flags, e.g. --library-search-path=/nix/store/...-nvidia-x11/lib
  # shellcheck disable=SC2086
  if nvidia-ctk cdi generate --output="$dir/nvidia.yaml" ${NVIDIA_CDI_ARGS:-} \
    >"$XDG_RUNTIME_DIR/nvidia-cdi.log" 2>&1; then
    gpu_cdi_drop_overflow_ids "$dir/nvidia.yaml"
    gpu_cdi_add_nix_deps "$dir/nvidia.yaml"
    log "GPU: CDI spec $dir/nvidia.yaml"
    [ "$(id -u)" = 0 ] || agent_env GPU_CDI_SPEC "$dir/nvidia.yaml"
  else
    rm -f "$dir/nvidia.yaml"
    [ "$(id -u)" = 0 ] || agent_env GPU_CDI_SPEC failed
    warn "GPU: nvidia-ctk cdi generate failed, see $XDG_RUNTIME_DIR/nvidia-cdi.log (NVIDIA_CDI_ARGS)"
  fi
}

# In a user-namespaced pod the device nodes belong to the overflow ids (65534, "nobody": host owner not
# mapped), and nvidia-ctk copies that into the spec as uid/gid and additionalGids. Rootless dockerd maps
# only 0..64535 into nested containers, so runc fails: "unable to setup user: setgroups: invalid
# argument". The overflow ids carry no access anyway (the nodes are 0666), so they are dropped.
gpu_cdi_drop_overflow_ids() {
  local spec=$1 uid gid tmp
  uid=$(cat /proc/sys/kernel/overflowuid 2>/dev/null || echo 65534)
  gid=$(cat /proc/sys/kernel/overflowgid 2>/dev/null || echo 65534)
  tmp=$(mktemp "$spec.XXXXXX")
  # shellcheck disable=SC2016
  if yq -y --argjson ou "$uid" --argjson og "$gid" '
      walk(
        if type == "object" then
          (if .uid == $ou then del(.uid) else . end)
          | (if .gid == $og then del(.gid) else . end)
          | (if has("additionalGids") then
               (.additionalGids |= map(select(. != $og)))
               | (if (.additionalGids | length) == 0 then del(.additionalGids) else . end)
             else . end)
        else . end
      )' "$spec" >"$tmp"; then
    chmod 0644 "$tmp"
    mv "$tmp" "$spec"
  else
    rm -f "$tmp"
    warn "GPU: could not drop the overflow ids from $spec (nested GPU containers may fail: setgroups)"
  fi
}

# NixOS nodes: the driver tools in the spec (nvidia-smi, ...) are Nix binaries whose ELF interpreter and
# RUNPATH point into /nix/store (glibc). nvidia-ctk mounts only the NVIDIA files, so in a nested
# container `nvidia-smi` fails with "exec ...: no such file or directory". The store directories they
# reference (and that this container has) are added to the spec as read-only mounts.
gpu_cdi_add_nix_deps() {
  local spec=$1 f d tmp
  local -A dirs=()
  command -v readelf >/dev/null || return 0
  while read -r f; do
    [ -f "$f" ] && [ -x "$f" ] || continue
    while read -r d; do
      d=$(cut -d/ -f1-4 <<<"$d")
      [ -d "$d" ] && dirs[$d]=1
    done < <(
      readelf -l "$f" 2>/dev/null | sed -n 's|.*Requesting program interpreter: \(/nix/store/[^]]*\)].*|\1|p'
      readelf -d "$f" 2>/dev/null | sed -n 's|.*(RUNPATH\|RPATH).*\[\(.*\)\]|\1|p' | tr ':' '\n' | grep '^/nix/store/'
    )
  done < <(yq -r '.. | .hostPath? // empty' "$spec" | grep -E '/bin/' | sort -u)
  (( ${#dirs[@]} )) || return 0
  tmp=$(mktemp "$spec.XXXXXX")
  # shellcheck disable=SC2016
  if printf '%s\n' "${!dirs[@]}" | sort | jq -R . | jq -s . >"$tmp.dirs" \
    && yq -y --slurpfile dirs "$tmp.dirs" '
      .containerEdits.mounts = ((.containerEdits.mounts // []) + ($dirs[0] | map(
        {hostPath: ., containerPath: ., options: ["ro", "nosuid", "nodev", "rbind", "rprivate"]})))
    ' "$spec" >"$tmp"; then
    chmod 0644 "$tmp"
    mv "$tmp" "$spec"
    log "GPU: CDI spec mounts the Nix store paths of the driver tools: ${!dirs[*]}"
  else
    rm -f "$tmp"
    warn "GPU: could not add the Nix store paths to $spec (nvidia-smi in nested containers may fail)"
  fi
  rm -f "$tmp.dirs"
}

# The NVIDIA container hook (runtimeClassName nvidia, legacy mode) mounts a tmpfs file over
# /proc/driver/nvidia/params (sets ModifyDeviceFiles: 0). Any mount on top of /proc makes the kernel
# refuse new proc mounts, so every nested container fails (runc: error mounting "proc": operation not
# permitted, also with --privileged). Root of the container (also inside a user-namespaced pod) can
# remove it; the device nodes are already there, so the driver has nothing to create.
gpu_release_proc() {
  [ "$(id -u)" = 0 ] || return 0
  local m
  while read -r m; do
    case "$m" in
      /proc/driver/nvidia/*)
        if umount "$m" 2>/dev/null; then
          log "GPU: removed the NVIDIA hook's mount on $m (nested containers need a clean /proc)"
        else
          warn "GPU: cannot unmount $m: nested containers will fail to mount /proc"
        fi
        ;;
    esac
  done < <(proc_overmounts)
}
