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
