#!/usr/bin/env bash
# Prints the component versions as JSON (build time: /etc/multica-agent-runtime/versions.json).
set -uo pipefail
HOME=$(mktemp -d)
export HOME
trap 'rm -rf "$HOME"' EXIT

# first output line, without colour codes
first_line() { "$@" 2>/dev/null | head -n1 | sed -E 's/\x1b\[[0-9;]*m//g; s/[[:space:]]+$//' | tr -d '\r'; }

declare -A v=(
  [variant]="${VARIANT:-unknown}"
  [multica]="$(first_line multica version)"
  [claude-code]="$(first_line claude --version)"
  [codex]="$(first_line codex --version)"
  [pi]="$(first_line pi --version)"
  [omp]="$(first_line omp --version)"
  [node]="$(first_line node --version)"
  [npm]="$(first_line npm --version)"
  [pnpm]="$(first_line pnpm --version)"
  [python]="$(first_line python3 --version)"
  [uv]="$(first_line uv --version)"
  [nix]="$(first_line /nix/var/nix/profiles/default/bin/nix --version)"
  [git]="$(first_line git --version)"
  [gh]="$(first_line gh --version)"
  [tea]="$(first_line tea --version)"
  [ttyd]="$(first_line ttyd --version)"
  [tmux]="$(first_line tmux -V)"
  [vim]="$(first_line vim --version)"
  [docker]="$(first_line docker --version)"
  [compose]="$(first_line docker compose version)"
  [buildx]="$(first_line docker buildx version)"
  [rootlesskit]="$(first_line rootlesskit --version)"
  [chrome]="$(first_line /opt/chrome-for-testing/chrome --version)"
  [playwright]="$(first_line playwright --version)"
  [playwright-mcp]="$(npm ls -g --prefix /usr/local --depth=0 --json 2>/dev/null | jq -r '.dependencies["@playwright/mcp"].version // empty')"
  [chrome-devtools-mcp]="$(npm ls -g --prefix /usr/local --depth=0 --json 2>/dev/null | jq -r '.dependencies["chrome-devtools-mcp"].version // empty')"
  [os]="$(sed -nE 's/^PRETTY_NAME="?([^"]*)"?$/\1/p' /etc/os-release)"
)
if command -v nvidia-ctk >/dev/null; then
  v[nvidia-container-toolkit]="$(first_line nvidia-ctk --version)"
  v[cuda]="${CUDA_VERSION:-}"
fi

json='{}'
for k in "${!v[@]}"; do
  json=$(jq --arg k "$k" --arg v "${v[$k]}" '. + {($k): $v}' <<<"$json")
done
jq -S . <<<"$json"
