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
for k in multica claude-code opencode codex pi chrome playwright docker gh tea ttyd; do
  [ -n "$(jq -r --arg k "$k" '.[$k] // empty' /etc/multica-agent-runtime/versions.json)" ] || fail "no version for $k"
done

step "CLIs"
for c in multica claude opencode codex pi gh tea git git-lfs vim tmux ttyd node npm pnpm uv uvx python3 \
  jq yq rg fd docker playwright playwright-mcp chrome-devtools-mcp bwrap socat nix; do
  command -v "$c" >/dev/null || fail "$c not on PATH"
done
multica version
claude --version
opencode --version
codex --version
pi --version
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

step "doctor"
runtime-doctor

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

echo
echo "smoke test passed ($VARIANT, Docker ${DIND_ACTIVE_MODE:-off})"
