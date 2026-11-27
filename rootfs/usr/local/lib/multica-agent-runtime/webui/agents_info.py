# shellcheck shell=python
"""Agent CLIs (installed, version, login, running tasks) and Claude Code token usage.

Logins are detected like runtime-doctor does (credential files in $HOME, API keys in the
environment); nothing is sent anywhere and no secret leaves this module.

Claude Code usage is computed locally from its session transcripts
(`<config dir>/projects/**/*.jsonl`, one `usage` record per API response), deduplicated per
message and request - the same data `/cost`-style tools read. Sources: `$CLAUDE_CONFIG_DIR` (or
`~/.claude`) and the per-agent homes of the task sandbox (`/data/agent-homes/*/.claude`), or
`WEBUI_CLAUDE_DIRS` (comma separated). Plan limits (the "% of your 5-hour limit" of `/usage`) are
not exposed in these files; the 5-hour blocks below are computed from the timestamps the way the
limit windows work (a block starts with the first message after the previous one expired).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import threading
import time
from typing import Any

import system_stats
import workspace_store as store

VERSIONS_FILE = "/etc/multica-agent-runtime/versions.json"
AGENTS = (
    ("claude", "Claude Code", "claude-code"),
    ("codex", "Codex", "codex"),
    ("opencode", "OpenCode", "opencode"),
    ("pi", "Pi", "pi"),
    ("omp", "Oh My Pi", "omp"),
)
BLOCK_SECONDS = 5 * 3600


def _home() -> str:
    return os.environ.get("HOME") or "/home/agent"


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _set(name: str) -> bool:
    value = os.environ.get(name, "")
    return bool(value) and value != "CHANGEME"


def _claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(_home(), ".claude")


def claude_login() -> dict[str, Any]:
    creds = os.path.join(_claude_dir(), ".credentials.json")
    if _nonempty(creds):
        info: dict[str, Any] = {"logged_in": True, "method": "subscription login"}
        try:
            with open(creds, "r", encoding="utf-8") as handle:
                oauth = (json.load(handle) or {}).get("claudeAiOauth") or {}
            info["subscription"] = oauth.get("subscriptionType") or ""
            info["rate_limit_tier"] = oauth.get("rateLimitTier") or ""
            expires = oauth.get("expiresAt")
            if isinstance(expires, (int, float)):
                info["token_expires_at"] = expires / 1000 if expires > 1e12 else expires
        except (OSError, ValueError, AttributeError):
            pass
        return info
    for name, method in (
        ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        ("ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL"),
    ):
        if _set(name):
            return {"logged_in": True, "method": method}
    return {"logged_in": False, "method": "", "hint": "Terminal: claude, then /login"}


def _login(cli: str) -> dict[str, Any]:
    home = _home()
    if cli == "claude":
        return claude_login()
    if cli == "codex":
        if _nonempty(os.path.join(home, ".codex", "auth.json")):
            return {"logged_in": True, "method": "~/.codex/auth.json"}
        if _set("OPENAI_API_KEY"):
            return {"logged_in": True, "method": "OPENAI_API_KEY"}
        return {"logged_in": False, "hint": "Terminal: codex login --device-auth"}
    if cli == "opencode":
        data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "opencode")
        if _nonempty(os.path.join(data, "opencode.db")) or _nonempty(os.path.join(data, "auth.json")):
            return {"logged_in": True, "method": "~/.local/share/opencode"}
        if _set("ANTHROPIC_API_KEY") or _set("OPENAI_API_KEY"):
            return {"logged_in": True, "method": "provider API key"}
        return {"logged_in": False, "hint": "Terminal: opencode, then /connect"}
    if cli in ("pi", "omp"):
        base = os.path.join(home, f".{cli}", "agent")
        for name in ("auth.json", "models.json", "models.yml", "agent.db"):
            if _nonempty(os.path.join(base, name)):
                return {"logged_in": True, "method": f"~/.{cli}/agent/{name}"}
        if _set("LLM_API_KEY"):
            return {"logged_in": True, "method": "LLM_API_KEY"}
        return {"logged_in": False, "hint": f"Terminal: {cli}, then /login"}
    return {"logged_in": False}


def _versions() -> dict[str, str]:
    try:
        with open(VERSIONS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def agents() -> list[dict[str, Any]]:
    """Every agent CLI the image knows, with version, login, daemon registration and running tasks."""
    versions = _versions()
    daemon = store.daemon_status()
    registered = set(daemon.get("agents") or []) if daemon.get("reachable") else None
    _latest, _history, processes = system_stats.sampler().snapshot()
    running = system_stats.agent_tasks(processes)
    result = []
    for cli, label, version_key in AGENTS:
        tasks = [task for task in running if task["cli"] in (cli, f"{cli}2")]
        result.append(
            {
                "cli": cli,
                "label": label,
                "installed": bool(shutil.which(cli)),
                "version": versions.get(version_key, ""),
                "login": _login(cli),
                "registered": (cli in registered) if registered is not None else None,
                "running": len(tasks),
                "cpu": round(sum(task["cpu"] for task in tasks), 1),
                "rss": sum(task["rss"] for task in tasks),
            }
        )
    return result


# --- Claude Code usage -------------------------------------------------------------------------

_files: dict[str, tuple[float, int, list[tuple]]] = {}
_usage_cache: dict[str, Any] = {}
_usage_lock = threading.Lock()


def claude_dirs() -> list[str]:
    raw = store._env("WEBUI_CLAUDE_DIRS")
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    dirs = [_claude_dir()]
    dirs += sorted(glob.glob("/data/agent-homes/*/.claude"))
    seen, out = set(), []
    for path in dirs:
        real = os.path.realpath(path)
        if real not in seen and os.path.isdir(os.path.join(path, "projects")):
            seen.add(real)
            out.append(path)
    return out


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        import datetime as dt  # noqa: PLC0415

        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_file(path: str) -> list[tuple]:
    """(timestamp, dedupe key, model, input, output, cache write, cache read, session) per response."""
    events = []
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed below
    except OSError:
        return events
    with handle:
        for raw in handle:
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) else None
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            stamp = _parse_time(entry.get("timestamp"))
            if stamp is None:
                continue
            model = str(message.get("model") or "unknown")
            if model == "<synthetic>":
                continue
            key = f"{message.get('id') or ''}:{entry.get('requestId') or ''}"
            events.append(
                (
                    stamp,
                    key,
                    model,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                    int(usage.get("cache_read_input_tokens") or 0),
                    str(entry.get("sessionId") or ""),
                )
            )
    return events


def _events(days: int) -> list[tuple]:
    cutoff = time.time() - days * 86400
    seen_paths = set()
    for base in claude_dirs():
        for path in glob.glob(os.path.join(base, "projects", "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            seen_paths.add(path)
            cached = _files.get(path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                continue
            _files[path] = (st.st_mtime, st.st_size, _parse_file(path))
    for path in list(_files):
        if path not in seen_paths:
            del _files[path]
    unique: dict[str, tuple] = {}
    for _mtime, _size, events in _files.values():
        for event in events:
            if event[0] < cutoff:
                continue
            key = event[1] if event[1] != ":" else f"{event[0]}:{event[7]}"
            unique[key] = event  # streaming writes the same response several times
    return sorted(unique.values())


def _totals(events: list[tuple]) -> dict[str, Any]:
    total = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "messages": len(events)}
    for event in events:
        total["input"] += event[3]
        total["output"] += event[4]
        total["cache_write"] += event[5]
        total["cache_read"] += event[6]
    total["total"] = total["input"] + total["output"] + total["cache_write"] + total["cache_read"]
    total["sessions"] = len({event[7] for event in events if event[7]})
    return total


def _blocks(events: list[tuple]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for event in events:
        if not blocks or event[0] >= blocks[-1]["start"] + BLOCK_SECONDS:
            start = event[0] - (event[0] % 3600)  # windows start on the full hour
            blocks.append({"start": start, "end": start + BLOCK_SECONDS, "events": []})
        blocks[-1]["events"].append(event)
    return blocks


def claude_usage(days: int = 30) -> dict[str, Any]:
    with _usage_lock:
        cached = _usage_cache.get("data")
        if cached and time.monotonic() - cached["at"] < store._env_int("WEBUI_USAGE_CACHE_SECONDS", 30):
            return cached["value"]
        started = time.monotonic()
        events = _events(days)
        now = time.time()
        today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        blocks = _blocks(events)
        current = blocks[-1] if blocks and blocks[-1]["end"] > now else None
        daily = []
        for offset in range(13, -1, -1):
            day_start = today - offset * 86400
            day_events = [event for event in events if day_start <= event[0] < day_start + 86400]
            daily.append({"day": day_start, **_totals(day_events)})
        models: dict[str, list[tuple]] = {}
        for event in events:
            if event[0] >= now - 7 * 86400:
                models.setdefault(event[2], []).append(event)
        value = {
            "generated_at": now,
            "login": claude_login(),
            "sources": claude_dirs(),
            "scan_seconds": round(time.monotonic() - started, 3),
            "block": (
                {"start": current["start"], "end": current["end"], **_totals(current["events"])} if current else None
            ),
            "windows": {
                "today": _totals([event for event in events if event[0] >= today]),
                "7d": _totals([event for event in events if event[0] >= now - 7 * 86400]),
                "30d": _totals(events),
            },
            "daily": daily,
            "models_7d": sorted(
                ({"model": model, **_totals(items)} for model, items in models.items()),
                key=lambda item: item["total"],
                reverse=True,
            ),
            "last_activity": events[-1][0] if events else None,
        }
        _usage_cache["data"] = {"at": time.monotonic(), "value": value}
        return value
