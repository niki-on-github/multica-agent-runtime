# shellcheck shell=bash
# $HOME is a PVC in the cluster: seed it from /etc/agent-skel, missing files only, so logins and own
# edits survive restarts and image updates.
seed_home() {
  mkdir -p "$HOME"
  cp -r --preserve=mode,timestamps --update=none /etc/agent-skel/. "$HOME/"
  mkdir -p "$HOME/.local/bin" "$HOME/.config" "$HOME/.cache" "$CLAUDE_CONFIG_DIR"
  mkdir -p "${MULTICA_WORKSPACES_ROOT:-/data/workspaces}" "${MULTICA_AGENT_TEMP_BASE:-/data/tmp}"
}
