#!/usr/bin/env bash
# Cold-resume guard test (multica-cold-resume) with a fake claude; run by smoke.sh, also runs outside the
# image: GUARD=rootfs/usr/local/bin/multica-cold-resume tests/cold-resume.sh
set -euo pipefail
trap 'echo "FAIL: cold-resume.sh line $LINENO: $BASH_COMMAND" >&2' ERR
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "--- cold resume: $*"; }

GUARD=$(readlink -f "${GUARD:-/usr/local/bin/multica-cold-resume}")
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME=$T/home CLAUDE_CONFIG_DIR=$T/home/.claude TMPDIR=$T/tmp XDG_STATE_HOME=$T/state
mkdir -p "$HOME" "$TMPDIR" "$T/work" "$T/log"
for v in $(compgen -e | grep -E '^(COLD_RESUME|ANTHROPIC_|CLAUDE_CODE_PROMPT_CACHE_TTL|FORCE_PROMPT_CACHING_5M|MULTICA_TASK_ENV_FILE)'); do
  unset "$v"
done
STATE=$XDG_STATE_HOME/multica-cold-resume

# fake claude: records its arguments and stdin
cat > "$T/claude" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_LOG/args"
cat > "$FAKE_LOG/stdin"
EOF
chmod +x "$T/claude"
export COLD_RESUME_NEXT=$T/claude FAKE_LOG=$T/log

SID=0b7c3f0e-1111-4222-8333-444455556666
proj=$CLAUDE_CONFIG_DIR/projects/$(cd "$T/work" && pwd | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$proj"
# transcript <minutes ago> <context tokens> [model]
transcript() {
  python3 - "$proj/$SID.jsonl" "$1" "$2" "${3:-claude-opus-5-5}" <<'EOF'
import datetime as dt, json, sys
path, ago, tokens, model = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
t = lambda m: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
def asst(text, ts, usage=None, mdl=model, **kw):
    m = {"role": "assistant", "model": mdl, "content": text}
    if usage: m["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "message": m, **kw}
L = [
  {"type": "user", "timestamp": t(ago + 60), "message": {"role": "user", "content": "ORIGINAL-TASK: fix the parser"}},
  asst([{"type": "text", "text": "I will edit parser.py"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/parser.py", "old_string": "a", "new_string": "b"}}],
       t(ago + 60), {"input_tokens": 10, "cache_read_input_tokens": tokens - 1000, "cache_creation_input_tokens": 500, "output_tokens": 490}),
  {"type": "user", "timestamp": t(ago + 60), "message": {"role": "user", "content": [
    {"type": "tool_result", "content": "TOOL-OUTPUT-SHOULD-NOT-APPEAR " * 50}]}},
  asst([{"type": "text", "text": "SIDECHAIN"}], t(ago), {"input_tokens": 1}, isSidechain=True),
  {"type": "user", "timestamp": t(ago), "message": {"role": "user", "content": "LATEST-COMMENT please also add tests"}},
  asst([{"type": "text", "text": "LATEST-REPLY done"}], t(ago),
       {"input_tokens": 10, "cache_read_input_tokens": tokens - 500, "output_tokens": 490}),
  asst([{"type": "text", "text": "API Error"}], t(ago), mdl="<synthetic>"),
]
with open(path, "w") as f:
    for e in L: f.write(json.dumps(e) + "\n")
EOF
}
# run <expect: run|decline> [env...] -- [claude args...]: the guard as the daemon starts it
run() {
  local expect=$1 rc=0
  shift
  local envs=()
  while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
  shift
  rm -f "$T/log/"*
  (cd "$T/work" && echo PROMPT | env MULTICA_TASK_ID="${TASK:-task1}" "${envs[@]}" "$GUARD" "$@" 2> "$T/log/stderr") || rc=$?
  case "$expect" in
    run) [ "$rc" = 0 ] && [ "$(cat "$T/log/stdin")" = PROMPT ] || fail "expected a claude run (rc $rc): $(cat "$T/log/stderr")" ;;
    decline)
      [ "$rc" = 1 ] || fail "expected a decline, rc $rc: $(cat "$T/log/stderr")"
      [ ! -e "$T/log/args" ] || fail "claude started although declined"
      tail -n 1 "$T/log/stderr" | grep -qx "No conversation found with session ID: $SID" || fail "no rejection message"
      ;;
  esac
}
has() { grep -qx -- "$1" "$T/log/args"; }
handoff() { sed -n '/^--append-system-prompt-file$/{n;p}' "$T/log/args"; }
R=(-p --output-format stream-json --resume "$SID" --model claude-opus-5-5)

step "warm, small, missing transcript, no task: unchanged"
transcript 5 800000
run run -- "${R[@]}"; has --resume || fail "warm session lost --resume"
transcript 120 50000
run run -- "${R[@]}"; has --resume || fail "small session lost --resume"
run run -- -p --resume 99999999-0000-0000-0000-000000000000; has --resume || fail "missing transcript changed"
transcript 120 800000
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u MULTICA_TASK_ID "$GUARD" "${R[@]}" 2>/dev/null)
has --resume || fail "probe without a task was changed"
run run COLD_RESUME=off -- "${R[@]}"; has --resume || fail "COLD_RESUME=off changed the run"

step "new session without a stored hand-off: unchanged"
TASK=other run run -- -p --model opus
[ "$(tr '\n' ' ' < "$T/log/args")" = "-p --model opus " ] || fail "new session args changed"

step "expired cache: declined, the retry gets the hand-off"
transcript 120 800000
run decline -- "${R[@]}"
grep -q "longer than the prompt cache lasts" "$T/log/stderr" || fail "no reason in the log"
grep -q '"action": "declined"' "$STATE/events.jsonl" || fail "no event"
[ -f "$proj/$SID.jsonl" ] || fail "old transcript removed"
run run -- -p --model claude-opus-5-5
has --resume && fail "retry resumed"
h=$(handoff); case "$h" in "$TMPDIR"/*) ;; *) fail "hand-off not below TMPDIR: $h" ;; esac
for w in ORIGINAL-TASK LATEST-COMMENT LATEST-REPLY; do grep -q "$w" "$h" || fail "digest misses $w"; done
grep -q /repo/parser.py "$h" || fail "edited file missing"
grep -q TOOL-OUTPUT-SHOULD-NOT-APPEAR "$h" && fail "tool output in the hand-off"
grep -q SIDECHAIN "$h" && fail "sidechain in the hand-off"
grep -q 'model "haiku"' "$h" || fail "no haiku subagent instruction"
full=$(grep -o "$TMPDIR/multica-cold-resume/task1-full.md" "$h" | head -n 1)
grep -q LATEST-REPLY "$full" 2>/dev/null || fail "full extract missing: $full"
grep -q TOOL-OUTPUT-SHOULD-NOT-APPEAR "$full" && fail "tool output in the full extract"

step "cache lifetime: 1 h on a subscription, 5 min with an API key or promptCacheTtl=5m, fixed override"
transcript 40 800000
TASK=t2 run run -- "${R[@]}"; has --resume || fail "40 min idle counted as cold on a subscription"
TASK=t2 run decline ANTHROPIC_API_KEY=sk-test -- "${R[@]}"
TASK=t3 run decline CLAUDE_CODE_PROMPT_CACHE_TTL=5m -- "${R[@]}"
echo '{"promptCacheTtl": "5m"}' > "$CLAUDE_CONFIG_DIR/settings.json"
TASK=t4 run decline -- "${R[@]}"
rm "$CLAUDE_CONFIG_DIR/settings.json"
TASK=t5 run decline COLD_RESUME_IDLE_MINUTES=30 -- "${R[@]}"
transcript 3 800000
TASK=t6 run run ANTHROPIC_API_KEY=sk-test -- "${R[@]}"; has --resume || fail "3 min idle counted as cold"

step "model switch: declined even when warm; same model by alias or none given: unchanged"
transcript 5 800000 claude-opus-5-5
TASK=model1 run decline -- -p --resume "$SID" --model claude-sonnet-5-5
grep -q "model changed from claude-opus-5-5 to claude-sonnet-5-5" "$T/log/stderr" || fail "no model reason"
TASK=model2 run decline -- -p --resume "$SID" --model 'sonnet[1m]'
TASK=model3 run run -- -p --resume "$SID" --model opus; has --resume || fail "alias of the same family declined"
TASK=model4 run run -- -p --resume "$SID" --model "claude-opus-5-5[1m]"; has --resume || fail "[1m] suffix counted as a switch"
TASK=model5 run run -- -p --resume "$SID"; has --resume || fail "no --model counted as a switch"
TASK=model6 run decline ANTHROPIC_MODEL=claude-haiku-5-5 -- -p --resume "$SID"
TASK=model7 run decline ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-5 -- -p --resume "$SID" --model opus

step "warm-pool start: task id from MULTICA_TASK_ENV_FILE"
transcript 120 800000
mkdir -p "$T/warm" && echo '{"active":true,"env":{"MULTICA_TASK_ID":"pooled1"}}' > "$T/warm/task-env.json"
rm -f "$T/log/"*
rc=0; (cd "$T/work" && echo PROMPT | env -u MULTICA_TASK_ID MULTICA_TASK_ENV_FILE="$T/warm/task-env.json" "$GUARD" "${R[@]}" 2>/dev/null) || rc=$?
[ "$rc" = 1 ] && [ -f "$STATE/pooled1.json" ] || fail "pooled start not guarded (rc $rc)"
echo '{"active":false}' > "$T/warm/task-env.json"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u MULTICA_TASK_ID MULTICA_TASK_ENV_FILE="$T/warm/task-env.json" "$GUARD" "${R[@]}" 2>/dev/null)
has --resume || fail "inactive overlay was guarded"

step "unwritable state: resumes unchanged"
TASK=s1 run run COLD_RESUME_STATE_DIR=/proc/nonexistent -- "${R[@]}"; has --resume || fail "unstorable hand-off still declined"

step "stale hand-off (> 24 h) is not used"
touch -d '2 days ago' "$STATE/task1.json"
run run -- -p
has --append-system-prompt-file && fail "stale hand-off used"

echo; echo "cold resume: ok"
