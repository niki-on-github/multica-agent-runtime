# shellcheck shell=sh
# Login shells (web shell, `kubectl exec -- bash -l`): the agent's own bin dirs, CUDA paths and the
# runtime environment the entrypoint resolved (Docker mode, CA variables; no secrets).
for p in /usr/local/cuda/bin /usr/local/nvidia/bin; do
  if [ -d "$p" ]; then
    case ":$PATH:" in
      *":$p:"*) ;;
      *) PATH="$PATH:$p" ;;
    esac
  fi
done
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH
if [ -r "${XDG_RUNTIME_DIR:-/run/agent}/agent.env" ]; then
  # shellcheck disable=SC1091
  . "${XDG_RUNTIME_DIR:-/run/agent}/agent.env"
fi
