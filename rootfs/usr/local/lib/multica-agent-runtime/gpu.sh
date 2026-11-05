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
    [ -s /etc/cdi/nvidia.yaml ] && return 0
    dir="$XDG_RUNTIME_DIR/cdi"
  fi
  mkdir -p "$dir"
  # NVIDIA_CDI_ARGS: extra flags, e.g. --library-search-path=/nix/store/...-nvidia-x11/lib
  # shellcheck disable=SC2086
  if nvidia-ctk cdi generate --output="$dir/nvidia.yaml" ${NVIDIA_CDI_ARGS:-} \
    >"$XDG_RUNTIME_DIR/nvidia-cdi.log" 2>&1; then
    log "GPU: CDI spec $dir/nvidia.yaml"
  else
    warn "GPU: nvidia-ctk cdi generate failed, see $XDG_RUNTIME_DIR/nvidia-cdi.log (NVIDIA_CDI_ARGS)"
  fi
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
