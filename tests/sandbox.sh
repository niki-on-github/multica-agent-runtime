#!/usr/bin/env bash
# Task sandbox test (agent-sandbox), run by smoke.sh as uid 1000 after the entrypoint's setup.
# Simulates the daemon: a task environment, a second task, the runner home, a bare repo cache with a
# worktree. With SMOKE_DIND=1 also Docker from inside the sandbox.
# shellcheck disable=SC2016  # commands for the sandbox, expanded inside
set -euo pipefail
trap 'echo "FAIL: sandbox.sh line $LINENO: $BASH_COMMAND" >&2' ERR
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "--- sandbox: $*"; }

out=$(bwrap --ro-bind / / --unshare-pid --unshare-ipc --unshare-uts --proc /proc --dev /dev true 2>&1) \
  || fail "bwrap cannot create namespaces: $out"

step "fake task"
WS=/data/workspaces
root=$WS/ws1/task1
other=$WS/ws1/task2
mkdir -p "$root/workdir" "$other/workdir" /data/tmp/task1 "$root/config"
echo own > "$root/workdir/marker"
echo next-to-workdir > "$root/settings.json"
echo secret-of-task2 > "$other/workdir/file"
echo runner-only > "$HOME/runner-file"
mkdir -p "$WS/.repos/ws1"
git init -q --bare "$WS/.repos/ws1/r.git"
git -C "$WS/.repos/ws1/r.git" -c user.email=t@t -c user.name=t commit-tree -m init \
  "$(git -C "$WS/.repos/ws1/r.git" hash-object -t tree -w /dev/null)" > /tmp/commit
git -C "$WS/.repos/ws1/r.git" update-ref refs/heads/main "$(cat /tmp/commit)"
git -C "$WS/.repos/ws1/r.git" worktree add -q "$root/workdir/repo" main
mkdir -p /tmp/multica-mcp-test && echo '{}' > /tmp/multica-mcp-test/mcp.json

task_env=(MULTICA_TASK_ID=task1 MULTICA_AGENT_ID=agent1 MULTICA_WORKSPACE_ID=ws1
  "MULTICA_TASK_WORKSPACES_ROOT=$WS" "MULTICA_TASK_CONFIG_ROOT=$root/config" TMPDIR=/data/tmp/task1
  MULTICA_TOKEN=mat_task GH_TOKEN=gh-secret LLM_API_KEY=llm-secret PLAIN_VAR=plain)
S() { (cd "$root/workdir" && env "${task_env[@]}" agent-sandbox bash -c "$1"); }
blocked() { if S "$1" >/dev/null 2>&1; then fail "reachable from the sandbox: $1"; fi; }

step "isolation"
env FAKE_DAEMON=1 sleep 3299 &
fake_daemon=$!
blocked "pgrep -f 'sleep 3299'"
kill "$fake_daemon"
blocked "grep -qa gh-secret /proc/1/environ /proc/self/environ"
blocked '[ -n "${GH_TOKEN:-}${LLM_API_KEY:-}" ]'
blocked "cat $other/workdir/file"
blocked "cat $HOME/runner-file"
blocked "ls $HOME/.bashrc"
S '[ "$PLAIN_VAR" = plain ] && [ "$MULTICA_TOKEN" = mat_task ]' || fail "task variables not passed"
S '[ "$HOME" = /data/agent-homes/agent1 ] && [ -f "$HOME/.bashrc" ]' || fail "per-agent home not set up"
S '[ "$(cat marker)" = own ] && [ -s ../settings.json ] && touch "$TMPDIR/x" ../config/y' || fail "task paths"
S 'echo persisted >> "$HOME/.bashrc"'
grep -q persisted /data/agent-homes/agent1/.bashrc || fail "home write did not persist"
grep -q persisted "$HOME/.bashrc" && fail "home write reached the runner home"
S 'jq -e ".mcpServers | has(\"playwright\")" "$HOME/.claude/.claude.json" >/dev/null' \
  || fail "browser MCPs not registered in the per-agent home"
S 'cd repo && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m sandbox' || fail "worktree commit"
(cd "$root/workdir" && env "${task_env[@]}" agent-sandbox bash -c 'cat /tmp/multica-mcp-test/mcp.json' \
  --mcp-config /tmp/multica-mcp-test/mcp.json) >/dev/null || fail "daemon temp file from the arguments not visible"
S 'nix-store --add "$TMPDIR/x" >/dev/null' || fail "/nix not writable"

step "pass-through"
task_env+=('AGENT_SANDBOX_PASS_ENV=GH_TOKEN,MULTICA_FOO')
S '[ "$GH_TOKEN" = gh-secret ] && [ -z "${LLM_API_KEY:-}" ]' || fail "AGENT_SANDBOX_PASS_ENV"
unset 'task_env[-1]'

step "refusals and probes"
(cd "$HOME" && env "${task_env[@]}" agent-sandbox true 2>/dev/null) && fail "task outside /data was not refused"
(cd "$root/workdir" && env "${task_env[@]}" MULTICA_AGENT_ID=../x agent-sandbox true 2>/dev/null) \
  && fail "bad agent id was not refused"
probe=$(mktemp -d)
(cd "$probe" && env -u MULTICA_TASK_ID MULTICA_TOKEN=mul_daemon agent-sandbox bash -c \
  '[ "$HOME" = /home/agent ] && [ ! -e /home/agent/runner-file ] && [ -z "${MULTICA_TOKEN:-}" ]') \
  || fail "probe mode"
for c in claude codex pi omp; do
  (cd "$probe" && "/usr/local/lib/multica-agent-runtime/sandbox-bin/$c" --version) || fail "$c --version through the shim"
done

step "cancellation"
(cd "$root/workdir" && env "${task_env[@]}" agent-sandbox sleep 3301) &
pid=$!
sleep 2
kill -TERM "$pid"
sleep 2
pgrep -f 'sleep 3301' >/dev/null && fail "sandboxed process survived TERM of the wrapper"

if [ "${SMOKE_DIND:-0}" = 1 ]; then
  step "docker from inside"
  img=docker.io/library/alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
  S "docker run --rm -v \"\$PWD:/src\" $img cat /src/marker | grep -qx own" || fail "docker bind mount of the work dir"
  S "echo h > \"\$HOME/h\" && docker run --rm -v \"\$HOME:/h\" $img cat /h/h | grep -qx h" || fail "docker bind mount of HOME"
  S 'docker run -d --name sbx-web -p 127.0.0.1:18090:8080 smoke-web >/dev/null
     for _ in $(seq 30); do curl -fsS http://127.0.0.1:18090/ 2>/dev/null | grep -q "nested container" && exit 0; sleep 1; done
     exit 1' || fail "published port from inside the sandbox"
  S 'timeout 90 /opt/google/chrome/chrome --headless=new --dump-dom http://127.0.0.1:18090/ 2>/dev/null | grep -q "nested container"' \
    || fail "Chrome in the sandbox against the published port"
  docker rm -f sbx-web >/dev/null
  (cd "$root/workdir" && env "${task_env[@]}" AGENT_SANDBOX_DOCKER=0 agent-sandbox bash -c 'docker info' >/dev/null 2>&1) \
    && fail "AGENT_SANDBOX_DOCKER=0 still has Docker"
fi

echo
echo "sandbox test passed"
