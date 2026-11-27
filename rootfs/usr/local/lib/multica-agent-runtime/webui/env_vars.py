# shellcheck shell=python
"""Environment variables of the runtime for the management web UI (standard library only).

Sources: this container (the webui process), the Multica daemon and every running agent task -
the latter two from /proc/<pid>/environ, so they need a shared PID namespace and the same uid.
That is the environment the process was started with (what the agent CLI actually got, after the
task sandbox's env filter), not later changes inside it.

Secrets are masked in every listing: names like *TOKEN*, *KEY*, *SECRET*, *PASSWORD*, *AUTH*, values
that look like tokens, and the password of URLs with credentials. Revealing one value is a separate
POST with the same CSRF guard as deletions (`WEBUI_ENV=masked` or read-only mode turn that off,
`WEBUI_ENV=0` the whole view). The web UI's own login (`WEBUI_AUTH*`) is never revealed.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from typing import Any

import system_stats
import workspace_store as store

SECRET_NAME = re.compile(
    r"(TOKEN|SECRET|PASSW|PASSPHRASE|API_?KEY|ACCESS_?KEY|PRIVATE|CREDENTIAL|COOKIE|SESSION|SIGNATURE|"
    r"(^|_)AUTH($|_)|(^|_)KEY($|_)|_PAT$|^PAT_|WEBHOOK|DSN$)",
    re.IGNORECASE,
)
NOT_SECRET_NAME = re.compile(r"^(PWD|OLDPWD|SSH_AUTH_SOCK|XAUTHORITY|.*_KEY_?(PATH|FILE|ID|NAME)S?)$", re.IGNORECASE)
SECRET_VALUE = re.compile(
    r"^(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{20,}|sk-[\w-]{20,}|xox[abpors]-[\w-]+|"
    r"AKIA[0-9A-Z]{16}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
NEVER_REVEAL = re.compile(r"^WEBUI_AUTH", re.IGNORECASE)
MASK = "••••••••"


def mode() -> str:
    """`full` (masked listing + reveal), `masked` (listing only) or `off`."""
    raw = store._env("WEBUI_ENV", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw == "masked":
        return "masked"
    return "full"


def _url_with_credentials(value: str) -> str | None:
    """The URL with its password masked, when `value` is a URL that carries credentials."""
    if "://" not in value or "@" not in value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if not parts.password:
        return None
    netloc = f"{parts.username or ''}:{MASK}@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def classify(name: str, value: str) -> tuple[bool, str]:
    """(is_secret, display value)."""
    if NEVER_REVEAL.match(name):
        return True, MASK
    url = _url_with_credentials(value)
    if url is not None:
        return True, url
    if value and not NOT_SECRET_NAME.match(name) and SECRET_NAME.search(name):
        return True, MASK
    if SECRET_VALUE.match(value):
        return True, MASK
    return False, value


def _read_environ(pid: int) -> dict[str, str] | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read(4 << 20)
    except OSError:
        return None
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        name, _, value = item.partition(b"=")
        env[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def sources() -> list[dict[str, Any]]:
    """Where environments can be read from right now."""
    _latest, _history, processes = system_stats.sampler().snapshot()
    result: list[dict[str, Any]] = [
        {"id": "webui", "kind": "webui", "label": "This container (webui)", "detail": f"pid {os.getpid()}"}
    ]
    daemon = system_stats._daemon_process(processes)
    if daemon:
        result.append(
            {"id": f"pid:{daemon['pid']}", "kind": "daemon", "label": "Multica daemon", "detail": f"pid {daemon['pid']}"}
        )
    for task in system_stats.agent_tasks(processes):
        where = task["task_dir"] or task["cwd"] or "unknown directory"
        result.append(
            {
                "id": f"pid:{task['pid']}",
                "kind": "task",
                "label": f"{task['cli']} · {where}",
                "detail": f"pid {task['pid']}" + (f" · issue {task['issue_id'][:8]}" if task["issue_id"] else ""),
                "pid": task["pid"],
            }
        )
    return result


class EnvError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _environment(source_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    if source_id in ("", "webui"):
        return sources()[0], dict(os.environ)
    allowed = {item["id"]: item for item in sources()}
    source = allowed.get(source_id)
    if source is None:
        # only the daemon and agent tasks, not arbitrary processes
        raise EnvError("unknown or finished source", status=404)
    env = _read_environ(int(source_id.split(":", 1)[1]))
    if env is None:
        raise EnvError("environment not readable (another user, or the process ended)", status=403)
    return source, env


def listing(source_id: str) -> dict[str, Any]:
    source, env = _environment(source_id)
    reference = dict(os.environ) if source["kind"] != "webui" else None
    variables = []
    for name in sorted(env, key=str.upper):
        secret, display = classify(name, env[name])
        item = {"name": name, "value": display, "secret": secret, "length": len(env[name])}
        if reference is not None:
            item["differs"] = reference.get(name) != env[name]
        variables.append(item)
    return {
        "generated_at": time.time(),
        "source": source,
        "mode": mode(),
        "count": len(variables),
        "secret_count": sum(1 for item in variables if item["secret"]),
        "variables": variables,
    }


def reveal(source_id: str, name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not name:
        raise EnvError("variable name is required")
    if NEVER_REVEAL.match(name):
        raise EnvError("the web UI's own credentials are never shown", status=403)
    _source, env = _environment(source_id if isinstance(source_id, str) else "")
    if name not in env:
        raise EnvError("no such variable", status=404)
    return {"name": name, "value": env[name]}
