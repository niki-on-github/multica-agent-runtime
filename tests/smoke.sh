#!/usr/bin/env bash
# Smoke test, run as the image command (after the entrypoint's setup) as uid 1000 with an empty
# $HOME. See .github/workflows/build.yml for the container options of each run.
#   SMOKE_DIND=1   Docker must work: build + run a nested container, test it with Chrome
#   SMOKE_OUT=dir  screenshots / Playwright output (CI artifact)
set -euo pipefail
trap 'echo "FAIL: line $LINENO: $BASH_COMMAND" >&2' ERR

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "--- $*"; }
OUT=${SMOKE_OUT:-/tmp/smoke-out}
mkdir -p "$OUT"
VARIANT=$(jq -r .variant /etc/multica-agent-runtime/versions.json)

step "identity and home seed"
[ "$(id -u)" = 1000 ] || fail "not uid 1000"
[ "$(whoami)" = agent ] || fail "user is not agent"
[ "$HOME" = /home/agent ] || fail "HOME is $HOME"
for f in .bashrc .profile .tmux.conf .vimrc; do
  [ -f "$HOME/$f" ] || fail "$f not seeded"
done
echo "# local edit" >> "$HOME/.vimrc"
cp -r --preserve=mode,timestamps --update=none /etc/agent-skel/. "$HOME/"
grep -q '# local edit' "$HOME/.vimrc" || fail "seed overwrote an existing file"
[ -w /data ] || fail "/data not writable"

step "versions"
jq . /etc/multica-agent-runtime/versions.json
for k in multica claude-code codex opencode pi omp chrome playwright docker gh tea ttyd; do
  [ -n "$(jq -r --arg k "$k" '.[$k] // empty' /etc/multica-agent-runtime/versions.json)" ] || fail "no version for $k"
done

step "CLIs"
for c in multica claude codex opencode opencode2 pi omp gh tea git git-lfs vim tmux ttyd node npm pnpm uv uvx python3 \
  jq yq rg fd docker playwright playwright-mcp chrome-devtools-mcp bwrap socat nix; do
  command -v "$c" >/dev/null || fail "$c not on PATH"
done
multica version
claude --version
codex --version
opencode --version
# @opencode/cli installs both names; Multica detects `opencode`, `opencode2` is the v2 alias
opencode2 --version >/dev/null || fail "opencode2 not runnable"
pi --version
omp --version
gh --version | head -n1
tea --version
bash -lc 'command -v nix && command -v claude' >/dev/null || fail "login shell PATH"

step "nix"
[ -w /nix/store ] || fail "/nix/store not writable for the agent"
nix eval --expr '1 + 1'
nix run nixpkgs#hello
NIX_CONFIG='extra-substituters = https://example.invalid' nix config show substituters \
  | grep -q example.invalid || fail "NIX_CONFIG not honoured"

step "chrome (headless screenshot)"
/opt/google/chrome/chrome --headless --disable-gpu --screenshot="$OUT/chrome-data-url.png" --window-size=800,600 \
  'data:text/html,<h1 style="font-family:sans-serif">multica-agent-runtime</h1>' 2>/dev/null
[ -s "$OUT/chrome-data-url.png" ] || fail "no chrome screenshot"
timeout 20 playwright-mcp --help >/dev/null || fail "playwright-mcp --help"
timeout 20 chrome-devtools-mcp --help >/dev/null || fail "chrome-devtools-mcp --help"

step "browser MCP servers registered for every agent"
for f in "$HOME/.claude/.claude.json" "$HOME/.pi/agent/mcp.json" "$HOME/.omp/agent/mcp.json"; do
  jq -e '.mcpServers | has("playwright") and has("chrome-devtools")' "$f" >/dev/null || fail "browser MCPs missing in $f"
done
[ "$(readlink "$HOME/.claude.json")" = "$HOME/.claude/.claude.json" ] || fail "$HOME/.claude.json not linked to $HOME/.claude/.claude.json"
for n in playwright chrome-devtools; do
  grep -qE "^\[mcp_servers\.(\"$n\"|$n)\]" "$HOME/.codex/config.toml" || fail "$n missing in codex config.toml"
done
# the CLIs read them (listing may connect to the servers, so only the names are checked)
timeout 120 claude mcp list > "$OUT/claude-mcp-list.txt" 2>&1 || true
grep -q playwright "$OUT/claude-mcp-list.txt" || fail "claude mcp list: no playwright ($OUT/claude-mcp-list.txt)"
timeout 120 codex mcp list > "$OUT/codex-mcp-list.txt" 2>&1 || true
grep -q chrome-devtools "$OUT/codex-mcp-list.txt" || fail "codex mcp list: no chrome-devtools ($OUT/codex-mcp-list.txt)"
timeout 120 pi mcp list > "$OUT/pi-mcp-list.txt" 2>&1 || true
grep -q playwright "$OUT/pi-mcp-list.txt" || fail "pi mcp list: no playwright ($OUT/pi-mcp-list.txt)"
# own entries win, a second run changes nothing
jq '.mcpServers.playwright.args = ["--own"]' "$HOME/.pi/agent/mcp.json" > "$OUT/pi-mcp.json"
cp "$OUT/pi-mcp.json" "$HOME/.pi/agent/mcp.json"
mcp_sums() {
  sha256sum "$HOME/.claude/.claude.json" "$HOME/.pi/agent/mcp.json" "$HOME/.omp/agent/mcp.json" \
    "$HOME/.codex/config.toml"
}
before=$(mcp_sums)
bash -c '. /usr/local/lib/multica-agent-runtime/common.sh; . /usr/local/lib/multica-agent-runtime/mcp.sh; register_browser_mcps'
[ "$before" = "$(mcp_sums)" ] || fail "browser MCP registration not idempotent or overwrote an own entry"

step "Claude defaults and cold-resume guard"
[ "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" = 400000 ] || fail "auto-compact window default not set"
claude_lib='. /usr/local/lib/multica-agent-runtime/common.sh; AGENT_ENV_FILE=/dev/null; . /usr/local/lib/multica-agent-runtime/claude.sh'
[ "$(env CLAUDE_CODE_AUTO_COMPACT_WINDOW=250000 bash -c "$claude_lib; setup_claude_defaults; echo \$CLAUDE_CODE_AUTO_COMPACT_WINDOW")" = 250000 ] \
  || fail "own CLAUDE_CODE_AUTO_COMPACT_WINDOW overridden"
[ -z "$(env CLAUDE_CODE_AUTO_COMPACT_WINDOW=off bash -c "$claude_lib; setup_claude_defaults; echo \${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" 2>/dev/null)" ] \
  || fail "CLAUDE_CODE_AUTO_COMPACT_WINDOW=off not honoured"
cfg=$(mktemp -d) && echo '{"autoCompactWindow": 300000}' > "$cfg/settings.json"
[ -z "$(env -u CLAUDE_CODE_AUTO_COMPACT_WINDOW CLAUDE_CONFIG_DIR="$cfg" bash -c "$claude_lib; setup_claude_defaults; echo \${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" 2>/dev/null)" ] \
  || fail "autoCompactWindow in settings.json overridden"
out=$(env -u MULTICA_CLAUDE_PATH bash -c "$claude_lib; setup_cold_resume 2>/dev/null; echo \$MULTICA_CLAUDE_PATH \$COLD_RESUME_NEXT")
[ "$out" = "/usr/local/lib/multica-agent-runtime/guard-bin/claude $(command -v claude)" ] || fail "guard wiring: $out"
out=$(env MULTICA_CLAUDE_PATH=/usr/local/lib/multica-agent-runtime/sandbox-bin/claude bash -c "$claude_lib; setup_cold_resume 2>/dev/null; echo \$COLD_RESUME_NEXT")
[ "$out" = /usr/local/lib/multica-agent-runtime/sandbox-bin/claude ] || fail "guard does not chain the sandbox shim: $out"
[ -z "$(env -u MULTICA_CLAUDE_PATH COLD_RESUME=off bash -c "$claude_lib; setup_cold_resume; echo \${MULTICA_CLAUDE_PATH:-}")" ] \
  || fail "COLD_RESUME=off still wraps claude"
/usr/local/lib/multica-agent-runtime/guard-bin/claude --version | grep -q 'Claude Code' || fail "guard shim --version"
/tests/cold-resume.sh 2>&1 | tail -n 1

step "doctor"
runtime-doctor

step "webui (management UI)"
python3 /tests/webui.py 2>&1 | tail -n 4
WEBUI_ROOT=$OUT/webui-root
mkdir -p "$WEBUI_ROOT/demo-123456789abc/home-1-0123456789ab/workdir" \
  "$WEBUI_ROOT/old-777777777777/home-2-aaaaaaaaaaaa/workdir"
dd if=/dev/zero of="$WEBUI_ROOT/demo-123456789abc/home-1-0123456789ab/workdir/blob.bin" bs=1M count=2 status=none
dd if=/dev/zero of="$WEBUI_ROOT/old-777777777777/home-2-aaaaaaaaaaaa/workdir/blob.bin" bs=1M count=1 status=none
touch -d '3 days ago' "$WEBUI_ROOT/old-777777777777/home-2-aaaaaaaaaaaa/workdir/blob.bin" \
  "$WEBUI_ROOT/old-777777777777/home-2-aaaaaaaaaaaa/workdir" \
  "$WEBUI_ROOT/old-777777777777/home-2-aaaaaaaaaaaa" "$WEBUI_ROOT/old-777777777777"
WEBUI_WORKSPACES_ROOT=$WEBUI_ROOT WEBUI_DAEMON_URL=http://127.0.0.1:9 WEBUI_RESOLVE_NAMES=0 \
  python3 /usr/local/lib/multica-agent-runtime/webui/server.py --bind 127.0.0.1 --port 18090 &
webui_pid=$!
for _ in $(seq 30); do curl -fsS -o /dev/null http://127.0.0.1:18090/api/health && break; sleep 0.5; done
curl -fsS -o "$OUT/webui-index.html" http://127.0.0.1:18090/ || fail "webui is not reachable"
grep -q 'Runtime management' "$OUT/webui-index.html" || fail "webui does not serve the page"
curl -fsS -o /dev/null http://127.0.0.1:18090/vendor/xterm.js || fail "webui does not serve xterm.js"
curl -fsS http://127.0.0.1:18090/api/system | jq -e '.processes.count > 0' >/dev/null || fail "webui /api/system"
curl -fsS http://127.0.0.1:18090/api/agents | jq -e 'has("tasks") and has("process_view")' >/dev/null || fail "webui /api/agents"
curl -fsS 'http://127.0.0.1:18090/api/env?source=webui' | jq -e '.count > 0' >/dev/null || fail "webui /api/env"
curl -fsS http://127.0.0.1:18090/api/maintenance | jq -e '.docker and .nix.available and (.settings.enabled | not)' >/dev/null \
  || fail "webui /api/maintenance"
curl -fsS http://127.0.0.1:18090/api/usage | jq -e '.windows["30d"] and (.daily | length == 14)' >/dev/null || fail "webui /api/usage"
curl -fsS -o "$OUT/webui-workspaces.json" http://127.0.0.1:18090/api/workspaces || fail "webui API is not reachable"
jq -e '.workspaces | length == 2 and (map(.deletable) | any) and (map(.in_use) | any)' \
  "$OUT/webui-workspaces.json" >/dev/null || fail "webui API does not report the expected workspaces"
/opt/google/chrome/chrome --headless --disable-gpu --hide-scrollbars --window-size=1400,900 \
  --virtual-time-budget=5000 --screenshot="$OUT/webui.png" http://127.0.0.1:18090/ 2>/dev/null || true
[ -s "$OUT/webui.png" ] || fail "no webui screenshot"
/opt/google/chrome/chrome --headless --disable-gpu --hide-scrollbars --window-size=1400,900 \
  --virtual-time-budget=5000 --screenshot="$OUT/webui-workspaces.png" 'http://127.0.0.1:18090/#workspaces' 2>/dev/null || true
kill "$webui_pid" 2>/dev/null || true
wait "$webui_pid" 2>/dev/null || true

if [ "$VARIANT" = cuda ]; then
  step "cuda image"
  nvidia-ctk --version
  command -v nvidia-container-runtime >/dev/null || fail "nvidia-container-runtime missing"
  ls /usr/local/cuda/lib64/libcudart.so* >/dev/null || fail "CUDA runtime libs missing"
fi

if [ "${SMOKE_DIND:-0}" = 1 ]; then
  step "docker (${DIND_ACTIVE_MODE:-?})"
  docker info --format '{{.ServerVersion}} {{.Driver}} {{.SecurityOptions}}'
  docker compose version
  docker run --rm docker.io/library/alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a true

  step "build and run a web container, test it with Chrome"
  docker build -q -t smoke-web /tests/e2e/web
  docker run -d --name smoke-web -p 127.0.0.1:18080:8080 smoke-web
  for _ in $(seq 30); do curl -fsS -o /dev/null http://127.0.0.1:18080/ && break; sleep 1; done
  curl -fsS http://127.0.0.1:18080/ | grep -q 'nested container' || fail "web container not reachable on 127.0.0.1:18080"
  (cd /tests/e2e && E2E_URL=http://127.0.0.1:18080/ E2E_OUT="$OUT" playwright test --config playwright.config.js)
  docker rm -f smoke-web >/dev/null

  if [ "$VARIANT" = cuda ]; then
    step "build a CUDA image"
    docker build -q -t smoke-cuda /tests/e2e/cuda
    docker run --rm smoke-cuda
  fi
fi

if [ "${SMOKE_SANDBOX:-0}" = 1 ]; then
  /tests/sandbox.sh
else
  step "sandbox (not tested in this run)"
  bwrap --ro-bind / / --unshare-pid --proc /proc --dev /dev true 2>&1 || true
fi

echo
echo "smoke test passed ($VARIANT, Docker ${DIND_ACTIVE_MODE:-off}, sandbox ${SMOKE_SANDBOX:-0})"
