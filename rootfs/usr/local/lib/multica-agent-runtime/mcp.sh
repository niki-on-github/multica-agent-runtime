# shellcheck shell=bash
# shellcheck disable=SC2016  # jq filters in single quotes
# Browser MCP servers (Playwright MCP, chrome-devtools-mcp) in the user-level config of every installed
# agent CLI. Runs on every start: names that already exist are left alone, so own edits win and a
# second run changes nothing. These are the files the Multica daemon reads when it merges the servers
# managed in its web UI into a task (Claude, Codex, OpenCode), or that the CLI reads itself next to
# them (Pi, omp).
#   BROWSER_MCP=0                 register nothing
#   BROWSER_MCP_ARGS_PLAYWRIGHT   arguments of playwright-mcp (default below)
#   BROWSER_MCP_ARGS_DEVTOOLS     arguments of chrome-devtools-mcp (default below)

# --isolated: a throwaway browser profile per session, so parallel tasks do not share a profile lock
BROWSER_MCP_DEFAULT_ARGS_PLAYWRIGHT="--browser chrome --headless --isolated"
BROWSER_MCP_DEFAULT_ARGS_DEVTOOLS="--headless --isolated"

# {"<name>": {"command": "...", "args": [...]}} of the servers to register
browser_mcp_servers() {
  local pw_args=${BROWSER_MCP_ARGS_PLAYWRIGHT-$BROWSER_MCP_DEFAULT_ARGS_PLAYWRIGHT}
  local dt_args=${BROWSER_MCP_ARGS_DEVTOOLS-$BROWSER_MCP_DEFAULT_ARGS_DEVTOOLS}
  jq -n --arg pw "$pw_args" --arg dt "$dt_args" '
    def split_args: [splits("[[:space:]]+") | select(. != "")];
    {
      "playwright": {command: "playwright-mcp", args: ($pw | split_args)},
      "chrome-devtools": {command: "chrome-devtools-mcp", args: ($dt | split_args)}
    }'
}

# mcp_json_merge <file> <jq filter>: the filter gets the server map as $servers and must keep every
# existing entry; written via a temp file and rename, so logins kept in the same file are never lost
mcp_json_merge() {
  local file=$1 filter=$2 tmp
  mkdir -p "$(dirname "$file")"
  if [ -s "$file" ] && ! jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
    warn "browser MCP: $file is not a JSON object, left unchanged"
    return 0
  fi
  tmp=$(mktemp "$file.XXXXXX")
  if { [ -s "$file" ] && cat "$file" || echo '{}'; } \
    | jq --argjson servers "$BROWSER_MCP_SERVERS" "$filter" > "$tmp"; then
    if [ -f "$file" ] && cmp -s "$tmp" "$file"; then
      rm -f "$tmp"
    else
      chmod 0600 "$tmp"
      mv -f "$tmp" "$file"
    fi
  else
    rm -f "$tmp"
    warn "browser MCP: could not update $file"
  fi
}

# JSON files with a "mcpServers" map in the Claude Code format (Claude, Pi, omp); entries get the
# fields of $1 (e.g. {"type":"stdio"}) in front
MCP_SERVERS_FILTER='.mcpServers = ((.mcpServers // {}) as $have
  | $have + ($servers | with_entries(select(.key as $k | $have | has($k) | not)) | map_values($extra + .)))'
mcp_register_mcpservers() {
  local file=$1 extra=${2:-'{}'}
  mcp_json_merge "$file" "$extra as \$extra | $MCP_SERVERS_FILTER"
}

mcp_register_claude() {
  local dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude} file
  file="$dir/.claude.json"
  # With CLAUDE_CONFIG_DIR, Claude Code keeps its user config in $CLAUDE_CONFIG_DIR/.claude.json, but
  # the Multica daemon merges the servers of ~/.claude.json into a task with managed MCP servers (and
  # runs it with --strict-mcp-config). The link makes both read the same file; Claude writes the target.
  if [ "$file" != "$HOME/.claude.json" ]; then
    if [ -L "$HOME/.claude.json" ] || [ ! -e "$HOME/.claude.json" ]; then
      ln -sfn "$file" "$HOME/.claude.json"
    else
      warn "browser MCP: $HOME/.claude.json is a regular file, not linked to $file (Multica reads the former)"
    fi
  fi
  mcp_register_mcpservers "$file" '{"type":"stdio"}'
}

mcp_register_opencode() {
  mcp_json_merge "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json" '
    (if has("$schema") then . else {"$schema": "https://opencode.ai/config.json"} + . end)
    | .mcp = ((.mcp // {}) as $have
      | $have + ($servers
        | with_entries(select(.key as $k | $have | has($k) | not))
        | map_values({type: "local", command: ([.command] + .args), enabled: true})))'
}

mcp_register_codex() {
  local file=${CODEX_HOME:-$HOME/.codex}/config.toml name
  mkdir -p "$(dirname "$file")"
  touch "$file"
  chmod 0600 "$file"
  for name in $(jq -r 'keys[]' <<<"$BROWSER_MCP_SERVERS"); do
    # the table header, quoted or bare (the names are [A-Za-z0-9_-], valid bare keys)
    if grep -qE "^[[:space:]]*\[mcp_servers\.(\"$name\"|$name)\]" "$file"; then
      continue
    fi
    jq -r --arg n "$name" '.[$n] | "\n[mcp_servers.\($n)]\ncommand = \(.command | tojson)\nargs = \(.args | tojson)"' \
      <<<"$BROWSER_MCP_SERVERS" >> "$file"
  done
}

register_browser_mcps() {
  if [ "${BROWSER_MCP:-1}" = 0 ]; then
    return 0
  fi
  BROWSER_MCP_SERVERS=$(browser_mcp_servers) || { warn "browser MCP: bad server arguments"; return 0; }
  if command -v claude >/dev/null; then mcp_register_claude; fi
  if command -v codex >/dev/null; then mcp_register_codex; fi
  if command -v opencode >/dev/null; then mcp_register_opencode; fi
  # Pi and omp: a server in these files wins over a same-named one Multica registers for a task
  if command -v pi >/dev/null; then mcp_register_mcpservers "$HOME/.pi/agent/mcp.json"; fi
  if command -v omp >/dev/null; then mcp_register_mcpservers "$HOME/.omp/agent/mcp.json"; fi
  return 0
}
