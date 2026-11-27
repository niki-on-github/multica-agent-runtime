#!/usr/bin/env bash
# Cold-resume guard test (claude-resume-guard) with a fake claude; run by smoke.sh, also runs outside the
# image: GUARD=rootfs/usr/local/bin/claude-resume-guard tests/resume-guard.sh
set -euo pipefail
trap 'echo "FAIL: resume-guard.sh line $LINENO: $BASH_COMMAND" >&2' ERR
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "--- resume guard: $*"; }

GUARD=${GUARD:-/usr/local/bin/claude-resume-guard}
GUARD=$(readlink -f "$GUARD")
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME=$T/home CLAUDE_CONFIG_DIR=$T/home/.claude TMPDIR=$T/tmp XDG_STATE_HOME=$T/state
mkdir -p "$HOME" "$TMPDIR" "$T/work" "$T/log"
unset CLAUDE_RESUME_GUARD CLAUDE_RESUME_GUARD_FALLBACK CLAUDE_RESUME_GUARD_MIN_TOKENS CLAUDE_RESUME_GUARD_IDLE_MINUTES \
  CLAUDE_RESUME_GUARD_TIMEOUT
# most cases check the hand-off in the same run (replace); the default strategy (reject) is tested at the end
export CLAUDE_RESUME_GUARD_STRATEGY=replace

# fake claude: the summary call (--no-session-persistence) answers per FAKE_SUMMARY, the main run records
# its arguments and stdin
cat > "$T/claude" <<'EOF'
#!/usr/bin/env bash
log=$FAKE_LOG
if printf '%s\n' "$@" | grep -qx -- --no-session-persistence; then
  cat > "$log/summary-stdin"
  printf '%s\n' "$@" > "$log/summary-args"
  pwd > "$log/summary-cwd"
  case "${FAKE_SUMMARY:-ok}" in
    ok) body=$(printf '## 1. Task\\nFix the parser.%.0s ' $(seq 40))
        printf '{"type":"result","subtype":"success","is_error":false,"result":"%s"}\n' "$body" ;;
    limit) printf '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"result":"Claude AI usage limit reached"}\n' ;;
    fail) echo "boom" >&2; exit 1 ;;
    slow) sleep 5 ;;
  esac
  exit 0
fi
printf '%s\n' "$@" > "$log/main-args"
cat > "$log/main-stdin"
EOF
chmod +x "$T/claude"
export CLAUDE_RESUME_GUARD_NEXT=$T/claude FAKE_LOG=$T/log

SID=0b7c3f0e-1111-4222-8333-444455556666
proj=$CLAUDE_CONFIG_DIR/projects/$(cd "$T/work" && pwd | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$proj"
# transcript <minutes ago> <context tokens> [compact]: a main thread with tool output, a sidechain entry
transcript() {
  python3 - "$proj/$SID.jsonl" "$1" "$2" "${3:-}" <<'EOF'
import datetime as dt, json, sys
path, ago, tokens, compact = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ago)).isoformat().replace("+00:00", "Z")
old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ago + 60)).isoformat().replace("+00:00", "Z")
L = [
  {"type": "user", "timestamp": old, "message": {"role": "user", "content": "ORIGINAL-TASK: fix the parser"}},
  {"type": "assistant", "timestamp": old, "message": {"role": "assistant", "content": [
    {"type": "text", "text": "I will edit parser.py"},
    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/parser.py", "old_string": "a", "new_string": "b"}}],
    "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 1000, "cache_creation_input_tokens": 500, "output_tokens": 490}}},
  {"type": "user", "timestamp": old, "message": {"role": "user", "content": [
    {"type": "tool_result", "content": "TOOL-OUTPUT-SHOULD-NOT-APPEAR " * 50}]}},
  {"type": "assistant", "isSidechain": True, "timestamp": old, "message": {"role": "assistant", "content": [{"type": "text", "text": "SIDECHAIN"}],
    "usage": {"input_tokens": 1, "output_tokens": 1}}},
]
if compact:
  L += [{"type": "system", "subtype": "compact_boundary", "timestamp": old},
        {"type": "user", "isCompactSummary": True, "timestamp": old, "message": {"role": "user", "content": "COMPACT-SUMMARY-TEXT"}}]
L += [{"type": "user", "timestamp": ts, "message": {"role": "user", "content": "LATEST-COMMENT please also add tests"}},
      {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [{"type": "text", "text": "LATEST-REPLY done"}]}}]
with open(path, "w") as f:
  for e in L: f.write(json.dumps(e) + "\n")
EOF
}
# run [args...]: the guard as the daemon starts it (task env, work dir, a prompt on stdin)
run() {
  rm -f "$T/log/"*
  (cd "$T/work" && echo PROMPT | env MULTICA_TASK_ID="${TASK:-task1}" "$@" "$GUARD" -p --output-format stream-json \
    --resume "$SID" --model opus 2> "$T/log/stderr") || fail "guard exited $?: $(cat "$T/log/stderr")"
  [ "$(cat "$T/log/main-stdin")" = PROMPT ] || fail "stdin did not reach claude"
}
has() { grep -qx -- "$1" "$T/log/main-args"; }
handoff() { sed -n '/^--append-system-prompt-file$/{n;p}' "$T/log/main-args"; }

step "warm session: unchanged"
transcript 5 800000
run
has --resume || fail "warm session lost --resume"
has --append-system-prompt-file && fail "warm session got a hand-off"
[ ! -e "$T/log/summary-args" ] || fail "summary call for a warm session"

step "small cold session: unchanged"
transcript 120 50000
run
has --resume || fail "small session lost --resume"

step "no task (probe) and no --resume: unchanged"
transcript 120 800000
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u MULTICA_TASK_ID "$GUARD" -p --resume "$SID" 2>/dev/null)
has --resume || fail "probe without MULTICA_TASK_ID was changed"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | MULTICA_TASK_ID=other "$GUARD" -p --model opus 2>/dev/null)
has --append-system-prompt-file && fail "a new session got a hand-off"
[ "$(tr '\n' ' ' < "$T/log/main-args")" = "-p --model opus " ] || fail "new session args changed"

step "missing transcript: unchanged"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | MULTICA_TASK_ID=task1 "$GUARD" -p --resume 99999999-0000-0000-0000-000000000000 2>/dev/null)
has --resume || fail "missing transcript lost --resume"

step "cold big session: summary hand-off instead of --resume"
transcript 120 800000
run
has --resume && fail "cold session still resumed"
{ has --model && has opus; } || fail "other args lost"
h=$(handoff); [ -s "$h" ] || fail "no hand-off file"
grep -q "Fix the parser" "$h" || fail "summary not in hand-off"
grep -q "$SID" "$h" || fail "hand-off does not name the old session"
case "$h" in "$TMPDIR"/*) ;; *) fail "hand-off not below TMPDIR: $h" ;; esac
grep -qx haiku "$T/log/summary-args" || fail "summary not on haiku"
grep -q ORIGINAL-TASK "$T/log/summary-stdin" || fail "task missing from digest"
grep -q LATEST-COMMENT "$T/log/summary-stdin" || fail "latest comment missing from digest"
grep -q /repo/parser.py "$T/log/summary-stdin" || fail "edited file missing from digest"
grep -q TOOL-OUTPUT-SHOULD-NOT-APPEAR "$T/log/summary-stdin" && fail "tool output in digest"
grep -q SIDECHAIN "$T/log/summary-stdin" && fail "sidechain in digest"
case "$(cat "$T/log/summary-cwd")" in "$TMPDIR"/claude-resume-guard-*) ;; *) fail "summary not in a temp dir" ;; esac
[ -f "$proj/$SID.jsonl" ] || fail "old transcript removed"
grep -q 'fresh session with a hand-off' "$T/log/stderr" || fail "no log line"
grep -q '"action": "handoff"' "$XDG_STATE_HOME/claude-resume-guard/events.jsonl" || fail "no event"

step "same task retried without --resume: hand-off again"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | MULTICA_TASK_ID=task1 "$GUARD" -p --model opus 2>/dev/null)
grep -q "Fix the parser" "$(handoff)" || fail "retry without hand-off"

step "idle threshold is configurable"
transcript 20 800000
run
has --resume || fail "20 min idle counted as cold with the 30 min default"
run CLAUDE_RESUME_GUARD_IDLE_MINUTES=10
has --resume && fail "CLAUDE_RESUME_GUARD_IDLE_MINUTES=10 ignored"

step "summary hits a usage limit: unchanged resume"
transcript 300 800000
run FAKE_SUMMARY=limit
has --resume || fail "limit did not fall back to resume"
grep -q 'usage or rate limit' "$T/log/stderr" || fail "no limit log line"

step "summary fails: transcript extract (or resume with FALLBACK=resume)"
TASK=task2 run FAKE_SUMMARY=fail
has --resume && fail "failed summary still resumed"
h=$(handoff); grep -q "mechanical extract" "$h" || fail "no extract hand-off"
grep -q LATEST-REPLY "$h" || fail "extract misses the latest reply"
TASK=task3 run FAKE_SUMMARY=fail CLAUDE_RESUME_GUARD_FALLBACK=resume
has --resume || fail "FALLBACK=resume ignored"
TASK=task4 run FAKE_SUMMARY=slow CLAUDE_RESUME_GUARD_TIMEOUT=1
grep -q "mechanical extract" "$(handoff)" || fail "timeout did not fall back to the extract"

step "digest mode: no summary call"
TASK=task5 run CLAUDE_RESUME_GUARD=digest
[ ! -e "$T/log/summary-args" ] || fail "summary call in digest mode"
grep -q "mechanical extract" "$(handoff)" || fail "digest mode without extract"

step "compacted after the last usage: size estimated, summary of the compaction kept"
transcript 300 800000 compact
TASK=task6 run CLAUDE_RESUME_GUARD=digest
has --resume || fail "compacted (small) session was replaced"
TASK=task6 run CLAUDE_RESUME_GUARD=digest CLAUDE_RESUME_GUARD_MIN_TOKENS=10
grep -q COMPACT-SUMMARY-TEXT "$(handoff)" || fail "compaction summary missing"

step "an existing --append-system-prompt-file is merged"
transcript 300 800000
echo OWN-APPEND > "$T/own.md"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | MULTICA_TASK_ID=task7 CLAUDE_RESUME_GUARD=digest "$GUARD" -p --resume "$SID" \
  --append-system-prompt-file "$T/own.md" 2>/dev/null)
[ "$(grep -cx -- --append-system-prompt-file "$T/log/main-args")" = 1 ] || fail "flag twice"
h=$(handoff); { grep -q OWN-APPEND "$h" && grep -q "mechanical extract" "$h"; } || fail "not merged"

step "off"
TASK=task8 run CLAUDE_RESUME_GUARD=off
has --resume || fail "CLAUDE_RESUME_GUARD=off changed the run"

step "reject (default): decline like a missing session, the daemon's retry gets the hand-off"
transcript 300 800000
rm -f "$T/log/"*
rc=0
(cd "$T/work" && echo PROMPT | env -u CLAUDE_RESUME_GUARD_STRATEGY MULTICA_TASK_ID=task9 "$GUARD" -p \
  --resume "$SID" --model opus 2> "$T/log/stderr") || rc=$?
[ "$rc" = 1 ] || fail "reject exited $rc"
[ ! -e "$T/log/main-args" ] || fail "claude started although the resume was declined"
tail -n 1 "$T/log/stderr" | grep -qx "No conversation found with session ID: $SID" || fail "no rejection message: $(cat "$T/log/stderr")"
grep -q 'declining the resume' "$T/log/stderr" || fail "no reject log line"
grep -q "Fix the parser" "$XDG_STATE_HOME/claude-resume-guard/pending/task9.md" || fail "hand-off not stored"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u CLAUDE_RESUME_GUARD_STRATEGY MULTICA_TASK_ID=task9 "$GUARD" -p --model opus 2>/dev/null)
has --resume && fail "retry resumed"
grep -q "Fix the parser" "$(handoff)" || fail "retry without hand-off"
[ "$(cat "$T/log/main-stdin")" = PROMPT ] || fail "retry stdin"

step "reject: summary limit still resumes unchanged; unstorable hand-off falls back to replace"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u CLAUDE_RESUME_GUARD_STRATEGY MULTICA_TASK_ID=task10 FAKE_SUMMARY=limit "$GUARD" \
  -p --resume "$SID" 2>/dev/null) || fail "limit case exited $?"
has --resume || fail "limit did not resume"
rm -f "$T/log/"*
(cd "$T/work" && echo PROMPT | env -u CLAUDE_RESUME_GUARD_STRATEGY MULTICA_TASK_ID=task11 \
  CLAUDE_RESUME_GUARD_STATE_DIR=/proc/nonexistent "$GUARD" -p --resume "$SID" 2>/dev/null) || fail "replace fallback exited $?"
{ ! has --resume && grep -q "Fix the parser" "$(handoff)"; } || fail "no replace fallback"

echo; echo "resume guard: ok"
