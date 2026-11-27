# shellcheck shell=python
"""Discovery, in-use detection and deletion of Multica workspace data directories.

The Multica daemon keeps the data of every workspace under one root
(`MULTICA_WORKSPACES_ROOT`, default `/data/workspaces`):

    /data/workspaces/<slug>-<last 12 hex of the workspace id>/   one directory per workspace
      home-<issue>-<token>/     one directory per task (run): .task_lock, .task_owner, workdir/ ...
    /data/workspaces/.repos/    git repo cache, shared by all workspaces (never listed)

Deleting a task directory, or a whole workspace directory, is safe while no task in it runs: the
daemon creates new directories for the next task. Everything in this module is aimed at one rule:

    never delete anything that is (or looks) in use.

Signals, collected per task directory (a workspace is in use when one of its tasks is):

1. a live process has its working directory (or an open file) inside it - a running agent task;
2. its `.task_lock` is locked at this moment (`/proc/locks`);
3. it was modified within `WEBUI_ACTIVITY_GRACE` seconds;
4. "blind" mode: when this process cannot see the daemon's processes (another container without
   a shared PID namespace, another pod), 1 and 2 see nothing. Then, unless the daemon reports that
   no task runs, everything modified within `WEBUI_BLIND_GRACE` seconds (6 h) counts as in use.

Nothing here needs the Multica server. Names come from the optional `multica workspace list`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# --- configuration -----------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return "" if value == "CHANGEME" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


DEFAULT_ROOT = "/data/workspaces"
INFRA_DIRS = {".repos", ".webui-trash"}


# root directory of the workspace data (WEBUI_WORKSPACES_ROOT wins over the daemon's variable)
def workspaces_root() -> str:
    root = _env("WEBUI_WORKSPACES_ROOT") or _env("MULTICA_WORKSPACES_ROOT") or DEFAULT_ROOT
    return os.path.abspath(root)


def config() -> dict[str, Any]:
    return {
        "root": workspaces_root(),
        "activity_grace_seconds": _env_int("WEBUI_ACTIVITY_GRACE", 300),
        "blind_grace_seconds": _env_int("WEBUI_BLIND_GRACE", 6 * 3600),
        "scan_budget_seconds": _env_int("WEBUI_SCAN_BUDGET", 120),
        "cache_seconds": _env_int("WEBUI_CACHE_SECONDS", 8),
        "trash": _env_bool("WEBUI_TRASH", True),
        "keep_trash_hours": _env_float("WEBUI_KEEP_TRASH_HOURS", 0.0),
        "protect": sorted(protected_workspaces()),
        "daemon_url": daemon_url(),
        "resolve_names": _env_bool("WEBUI_RESOLVE_NAMES", True),
    }


def protected_workspaces() -> set[str]:
    raw = _env("WEBUI_PROTECT_WORKSPACES")
    return {part.strip() for part in raw.split(",") if part.strip()}


def daemon_url() -> str:
    url = _env("WEBUI_DAEMON_URL")
    if url:
        return url.rstrip("/")
    port = _env("MULTICA_DAEMON_PORT") or "19514"
    return f"http://127.0.0.1:{port}"


def trash_dir(root: str) -> str:
    return os.path.join(root, ".webui-trash")


# --- helpers -----------------------------------------------------------------------------------

_BYTES_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(num: float) -> str:
    value = float(num)
    for unit in _BYTES_UNITS:
        if abs(value) < 1024 or unit == _BYTES_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def _dev_id(st: os.stat_result) -> str:
    """Device of a stat result as /proc/locks prints it: hex `major:minor`, two digits each."""
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"


@dataclass
class TaskDir:
    name: str
    path: str
    size_bytes: int = 0
    newest_mtime: float = 0.0
    created: float = 0.0
    locked: bool = False
    task_id: str = ""
    issue_id: str = ""
    agent_id: str = ""
    processes: list[dict[str, Any]] = field(default_factory=list)
    in_use: bool = False
    reasons: list[str] = field(default_factory=list)
    protected: bool = False

    @property
    def deletable(self) -> bool:
        return not self.in_use and not self.protected

    def as_dict(self) -> dict[str, Any]:
        age = max(0.0, time.time() - self.newest_mtime) if self.newest_mtime else None
        return {
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "size": human_bytes(self.size_bytes),
            "newest_mtime": self.newest_mtime,
            "created": self.created,
            "last_activity_seconds": age,
            "locked": self.locked,
            "task_id": self.task_id,
            "issue_id": self.issue_id,
            "agent_id": self.agent_id,
            "processes": self.processes,
            "in_use": self.in_use,
            "protected": self.protected,
            "deletable": self.deletable,
            "reasons": self.reasons,
            "has_workdir": os.path.isdir(os.path.join(self.path, "workdir")),
        }


@dataclass
class Workspace:
    dir_name: str
    path: str
    workspace_id: str = ""
    slug: str = ""
    name: str = ""
    size_bytes: int = 0
    newest_mtime: float = 0.0
    oldest_mtime: float = 0.0
    own_newest_mtime: float = 0.0  # files of the workspace directory outside the task directories
    partial: bool = False
    tasks: list[TaskDir] = field(default_factory=list)
    processes: list[dict[str, Any]] = field(default_factory=list)
    in_use: bool = False
    reasons: list[str] = field(default_factory=list)
    protected: bool = False

    @property
    def deletable(self) -> bool:
        return not self.in_use and not self.protected

    def as_dict(self) -> dict[str, Any]:
        age = max(0.0, time.time() - self.newest_mtime) if self.newest_mtime else None
        return {
            "dir": self.dir_name,
            "path": self.path,
            "workspace_id": self.workspace_id,
            "id_short": self.workspace_id[:8] if self.workspace_id else "",
            "slug": self.slug,
            "name": self.name or self.slug or self.dir_name,
            "size_bytes": self.size_bytes,
            "size": human_bytes(self.size_bytes),
            "task_count": len(self.tasks),
            "tasks": [task.as_dict() for task in self.tasks],
            "tasks_in_use": sum(1 for task in self.tasks if task.in_use),
            "tasks_deletable": sum(1 for task in self.tasks if task.deletable),
            "tasks_deletable_size_bytes": sum(task.size_bytes for task in self.tasks if task.deletable),
            "newest_mtime": self.newest_mtime,
            "last_activity_seconds": age,
            "partial": self.partial,
            "processes": self.processes,
            "in_use": self.in_use,
            "protected": self.protected,
            "deletable": self.deletable,
            "reasons": self.reasons,
        }


# --- scanning ----------------------------------------------------------------------------------


def _walk_size(path: str, deadline: float) -> tuple[int, float, bool]:
    """Size and newest mtime below `path`; symlinks are never followed."""
    size = 0
    newest = 0.0
    partial = False
    stack = [path]
    while stack:
        current = stack.pop()
        if time.monotonic() > deadline:
            partial = True
            break
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_mtime > newest:
                        newest = st.st_mtime
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                    elif stat.S_ISREG(st.st_mode):
                        size += st.st_size
        except OSError:
            partial = partial or current != path
    try:
        st = os.stat(path, follow_symlinks=False)
        newest = max(newest, st.st_mtime)
    except OSError:
        pass
    return size, newest, partial


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1 << 20).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _skip_process_pids() -> set[int]:
    """The webui itself and its ancestors (a webui started from a workspace shell)."""
    pids = {0}
    pid = os.getpid()
    for _ in range(32):
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        if pid <= 1:
            pids.add(pid)
            break
    return pids


def _own_terminal_pids() -> set[int]:
    """Process trees of the webui's own terminal sessions (they are the user, not a task)."""
    try:
        import terminal  # noqa: PLC0415 - optional, avoids an import cycle

        return terminal.session_pids()
    except Exception:  # noqa: BLE001 - terminal support is optional
        return set()


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read(1024)
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _proc_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
            return handle.read(64).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _scan_processes(roots: Iterable[str]) -> list[dict[str, Any]]:
    """Processes whose working directory or open files lie inside one of `roots`."""
    roots = [os.path.realpath(root) for root in roots if root]
    if not roots:
        return []
    skip = _skip_process_pids()
    own_terminals = _own_terminal_pids()
    found: list[dict[str, Any]] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        paths: list[str] = []
        try:
            paths.append(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            pass
        try:
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir)[:4096]:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target.startswith("/"):
                    paths.append(target)
        except OSError:
            pass
        match = next(
            (path for path in paths if any(path == root or path.startswith(root + os.sep) for root in roots)),
            None,
        )
        if match:
            found.append(
                {
                    "pid": pid,
                    "name": _proc_name(pid),
                    "cmd": _proc_cmdline(pid)[:200],
                    "path": match,
                    "terminal": pid in own_terminals,
                }
            )
    return found


_LOCK_DEV_INODE = re.compile(r"^([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)$")


def _locked_inodes() -> tuple[set[tuple[str, int]], set[int]]:
    """Locks held right now, from /proc/locks: ({(major:minor, inode)}, {inode}).

    A line looks like `40: FLOCK  ADVISORY  WRITE 443 00:9b:279660 0 EOF` (waiters carry an extra
    `->`); every lock type (FLOCK, POSIX, OFDLCK, LEASE) and mode counts. The device is printed in
    hex. Only locks of processes in this PID namespace are listed by the kernel.
    """
    locked: set[tuple[str, int]] = set()
    inodes: set[int] = set()
    try:
        with open("/proc/locks", "rb") as handle:
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return locked, inodes
    for line in lines:
        for token in line.split():
            match = _LOCK_DEV_INODE.match(token)
            if match:
                dev = f"{int(match.group(1), 16):02x}:{int(match.group(2), 16):02x}"
                inode = int(match.group(3))
                locked.add((dev, inode))
                inodes.add(inode)
                break
    return locked, inodes


def _workspace_id_from_dir(dir_name: str, ids_by_suffix: dict[str, str]) -> str:
    suffix = dir_name.rsplit("-", 1)[-1] if "-" in dir_name else ""
    return ids_by_suffix.get(suffix.lower(), "")


_names_cache: dict[str, Any] = {}
_names_lock = threading.Lock()


def _workspace_names() -> dict[str, dict[str, str]]:
    """id -> {name, slug} from the Multica server, empty when unavailable (offline, no login)."""
    with _names_lock:
        cache = _names_cache.get("data")
        if cache is not None and time.monotonic() - cache["at"] < 300:
            return cache["value"]
        value: dict[str, dict[str, str]] = {}
        if _env_bool("WEBUI_RESOLVE_NAMES", True):
            try:
                proc = subprocess.run(
                    ["multica", "workspace", "list", "--output", "json"],
                    capture_output=True,
                    timeout=_env_int("WEBUI_NAMES_TIMEOUT", 15),
                    check=False,
                )
                if proc.returncode == 0:
                    items = json.loads(proc.stdout.decode("utf-8", "replace"))
                    for item in items if isinstance(items, list) else []:
                        if isinstance(item, dict) and item.get("id"):
                            value[str(item["id"])] = {
                                "name": str(item.get("name") or ""),
                                "slug": str(item.get("slug") or ""),
                            }
            except (OSError, ValueError, subprocess.SubprocessError):
                value = {}
        _names_cache["data"] = {"at": time.monotonic(), "value": value}
        return value


_daemon_cache: dict[str, Any] = {}


def daemon_status(timeout: float = 2.0, fresh: bool = False) -> dict[str, Any]:
    """The daemon's local /health endpoint, when it is reachable from here."""
    url = daemon_url()
    cached = _daemon_cache.get("data")
    if not fresh and cached is not None and time.monotonic() - cached["at"] < 3:
        return cached["value"]
    status: dict[str, Any] = {"reachable": False, "url": url}
    if url:
        try:
            import urllib.request  # noqa: PLC0415

            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:  # noqa: S310 - local URL
                payload = json.loads(response.read(1 << 20).decode("utf-8", "replace"))
            if isinstance(payload, dict):
                status.update(
                    {
                        "reachable": True,
                        "status": payload.get("status"),
                        "daemon_id": payload.get("daemon_id"),
                        "device_name": payload.get("device_name"),
                        "uptime": payload.get("uptime"),
                        "cli_version": payload.get("cli_version"),
                        "server_url": payload.get("server_url"),
                        "pid": payload.get("pid"),
                        "agents": payload.get("agents") or [],
                        "running_task_count": payload.get("running_task_count"),
                        "active_task_count": payload.get("active_task_count"),
                        "resource_wait_task_count": payload.get("resource_wait_task_count"),
                        "workspaces": payload.get("workspaces") or [],
                    }
                )
        except Exception as exc:  # noqa: BLE001 - any failure just means "not reachable"
            status["error"] = str(exc)[:200]
    _daemon_cache["data"] = {"at": time.monotonic(), "value": status}
    return status


def _daemon_process_visible() -> bool:
    """True when the Multica daemon's process is visible in this PID namespace."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    own = os.getpid()
    for entry in entries:
        if not entry.isdigit() or int(entry) == own:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read(512).split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b"multica" and b"daemon" in argv[1:3]:
            return True
    return False


def process_view(daemon: dict[str, Any] | None = None) -> dict[str, Any]:
    """Whether processes and locks of running tasks are visible from here (see module docstring)."""
    daemon = daemon if daemon is not None else daemon_status()
    visible = _daemon_process_visible()
    running = daemon.get("running_task_count") if daemon.get("reachable") else None
    if visible:
        mode = "full"
    elif _env_bool("WEBUI_ASSUME_SHARED_PID", False):
        mode = "full"
    else:
        mode = "blind"
    # blind and nothing tells us that no task runs: fall back to the long grace period
    strict = mode == "blind" and not (isinstance(running, int) and running == 0)
    return {
        "mode": mode,
        "daemon_visible": visible,
        "daemon_running_tasks": running,
        "strict": strict,
        "blind_grace_seconds": _env_int("WEBUI_BLIND_GRACE", 6 * 3600),
    }


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _detect_in_use(
    workspace: Workspace,
    programs: list[dict[str, Any]],
    locks: tuple[set[tuple[str, int]], set[int]],
    cfg: dict[str, Any],
    view: dict[str, Any],
) -> None:
    locked, locked_inodes = locks
    grace = int(cfg["activity_grace_seconds"])
    blind_grace = int(cfg["blind_grace_seconds"]) if view.get("strict") else 0
    now = time.time()

    def activity_reasons(newest: float) -> list[str]:
        if not newest:
            return []
        age = now - newest
        if grace > 0 and age < grace:
            return [f"modified {max(0, int(age))}s ago (grace {grace}s)"]
        if blind_grace > 0 and age < blind_grace:
            return [
                f"modified {int(age // 60)} min ago and running tasks are not visible from here "
                f"(blind grace {blind_grace // 3600:g} h)"
            ]
        return []

    def process_reason(procs: list[dict[str, Any]]) -> list[str]:
        if not procs:
            return []
        names = sorted({p["name"] or str(p["pid"]) for p in procs})
        label = "web terminal" if all(p.get("terminal") for p in procs) else "running process"
        return [f"{label} ({', '.join(names[:4])})"]

    workspace_procs = [p for p in programs if _under(str(p["path"]), workspace.path)]
    workspace.processes = workspace_procs
    for task in workspace.tasks:
        reasons: list[str] = []
        task.processes = [p for p in workspace_procs if _under(str(p["path"]), task.path)]
        reasons += process_reason(task.processes)
        try:
            st = os.stat(os.path.join(task.path, ".task_lock"))
            key = (_dev_id(st), st.st_ino)
        except OSError:
            key = None
        # the device of an inode can differ between stat() and /proc/locks (btrfs subvolumes,
        # overlay): an inode-only hit counts too - a false "in use" is the safe direction
        if key and (key in locked or key[1] in locked_inodes):
            task.locked = True
            reasons.append("task lock held")
        reasons += activity_reasons(task.newest_mtime)
        task.reasons = reasons
        task.in_use = bool(reasons)
        task.protected = workspace.protected

    reasons: list[str] = []
    loose = [p for p in workspace_procs if not any(_under(str(p["path"]), t.path) for t in workspace.tasks)]
    reasons += process_reason(loose)
    busy = [task for task in workspace.tasks if task.in_use]
    for task in busy[:3]:
        reasons.append(f"{task.name}: {task.reasons[0]}")
    if len(busy) > 3:
        reasons.append(f"{len(busy) - 3} more tasks in use")
    reasons += activity_reasons(workspace.own_newest_mtime)
    workspace.reasons = reasons
    workspace.in_use = bool(reasons)


def _names_index() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    names = _workspace_names()
    ids_by_suffix: dict[str, str] = {}
    for workspace_id in names:
        ids_by_suffix[workspace_id.replace("-", "").lower()[-12:]] = workspace_id
    return names, ids_by_suffix


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _scan_task(name: str, path: str, deadline: float) -> tuple[TaskDir, bool]:
    task = TaskDir(name=name, path=path)
    task.size_bytes, task.newest_mtime, partial = _walk_size(path, deadline)
    try:
        st = os.stat(path)
        task.created = min(st.st_mtime, st.st_ctime)
    except OSError:
        pass
    owner = _read_json(os.path.join(path, ".task_owner"))
    env = _read_json(os.path.join(path, ".managed_env.json"))
    task.task_id = str(owner.get("task_id") or "")
    task.issue_id = str(env.get("issue_id") or "")
    task.agent_id = str(env.get("agent_id") or "")
    return task, partial


def _scan_workspace(
    entry: str,
    cfg: dict[str, Any],
    deadline: float,
    names: dict[str, dict[str, str]],
    ids_by_suffix: dict[str, str],
    only_task: str | None = None,
) -> Workspace | None:
    """Size, tasks and identity of one workspace directory (no in-use detection)."""
    path = os.path.join(cfg["root"], entry)
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return None
    except OSError:
        return None

    workspace = Workspace(dir_name=entry, path=path)
    children = _listdir(path)
    workspace.workspace_id = _workspace_id_from_dir(entry, ids_by_suffix)
    if not workspace.workspace_id:
        for task_name in children:
            if task_name.startswith(".") or task_name in INFRA_DIRS:
                continue
            owner = _read_json(os.path.join(path, task_name, ".task_owner"))
            if owner.get("workspace_id"):
                workspace.workspace_id = str(owner["workspace_id"])
                break
    meta = names.get(workspace.workspace_id, {})
    workspace.name = meta.get("name", "")
    workspace.slug = meta.get("slug", "") or (entry.rsplit("-", 1)[0] if "-" in entry else entry)

    try:
        workspace.own_newest_mtime = os.stat(path, follow_symlinks=False).st_mtime
    except OSError:
        pass
    created: list[float] = []
    for child in children:
        if only_task is not None and child != only_task:
            continue
        child_path = os.path.join(path, child)
        try:
            is_dir = stat.S_ISDIR(os.lstat(child_path).st_mode)
        except OSError:
            continue
        if child.startswith(".") or child in INFRA_DIRS or not is_dir:
            size, newest, partial = _walk_size(child_path, deadline)
            workspace.size_bytes += size
            workspace.own_newest_mtime = max(workspace.own_newest_mtime, newest)
            workspace.partial = workspace.partial or partial
            continue
        task, partial = _scan_task(child, child_path, deadline)
        workspace.partial = workspace.partial or partial
        workspace.size_bytes += task.size_bytes
        workspace.newest_mtime = max(workspace.newest_mtime, task.newest_mtime)
        if task.created:
            created.append(task.created)
        workspace.tasks.append(task)
    workspace.newest_mtime = max(workspace.newest_mtime, workspace.own_newest_mtime)
    workspace.oldest_mtime = min(created) if created else workspace.newest_mtime
    workspace.tasks.sort(key=lambda item: item.newest_mtime, reverse=True)

    identifiers = {
        entry,
        workspace.workspace_id,
        workspace.slug,
        f"{workspace.slug}-{(workspace.workspace_id or '').replace('-', '')[-12:]}",
    }
    workspace.protected = bool(identifiers & set(cfg["protect"]))
    return workspace


_cache: dict[str, Any] = {}
_scan_lock = threading.Lock()


def snapshot(force: bool = False) -> dict[str, Any]:
    """All workspaces with size, activity and in-use state (cached for `WEBUI_CACHE_SECONDS`)."""
    cfg = config()
    with _scan_lock:  # one scan at a time; concurrent requests get the fresh result
        cached = _cache.get("data")
        if not force and cached is not None and time.monotonic() - cached["at"] < cfg["cache_seconds"]:
            return cached["value"]

        root = cfg["root"]
        started = time.monotonic()
        deadline = started + max(5, int(cfg["scan_budget_seconds"]))
        names, ids_by_suffix = _names_index()

        workspaces: list[Workspace] = []
        for entry in _listdir(root):
            if entry.startswith(".") or entry in INFRA_DIRS:
                continue
            workspace = _scan_workspace(entry, cfg, deadline, names, ids_by_suffix)
            if workspace is not None:
                workspaces.append(workspace)

        daemon = daemon_status()
        view = process_view(daemon)
        programs = _scan_processes([workspace.path for workspace in workspaces])
        locks = _locked_inodes()
        for workspace in workspaces:
            _detect_in_use(workspace, programs, locks, cfg, view)

        workspaces.sort(key=lambda item: item.size_bytes, reverse=True)
        deletable = [workspace for workspace in workspaces if workspace.deletable]
        tasks = [task for workspace in workspaces for task in workspace.tasks]
        idle_tasks = [task for task in tasks if task.deletable]
        payload = {
            "root": root,
            "generated_at": time.time(),
            "scan_seconds": round(time.monotonic() - started, 3),
            "grace_seconds": cfg["activity_grace_seconds"],
            "protect": sorted(cfg["protect"]),
            "daemon": daemon,
            "process_view": view,
            "workspaces": [workspace.as_dict() for workspace in workspaces],
            "totals": {
                "workspace_count": len(workspaces),
                "task_count": len(tasks),
                "size_bytes": sum(workspace.size_bytes for workspace in workspaces),
                "size": human_bytes(sum(workspace.size_bytes for workspace in workspaces)),
                "in_use_count": sum(1 for workspace in workspaces if workspace.in_use),
                "tasks_in_use": sum(1 for task in tasks if task.in_use),
                "deletable_count": len(deletable),
                "deletable_size_bytes": sum(workspace.size_bytes for workspace in deletable),
                "deletable_size": human_bytes(sum(workspace.size_bytes for workspace in deletable)),
                "deletable_task_count": len(idle_tasks),
                "deletable_task_size_bytes": sum(task.size_bytes for task in idle_tasks),
                "deletable_task_size": human_bytes(sum(task.size_bytes for task in idle_tasks)),
            },
        }
        _cache["data"] = {"at": time.monotonic(), "value": payload}
        return payload


def _fresh_state(dir_name: str, task: str | None = None) -> Workspace | None:
    """One workspace (or one of its tasks), scanned on its own right before deleting."""
    cfg = config()
    names, ids_by_suffix = _names_index()
    deadline = time.monotonic() + max(5, int(cfg["scan_budget_seconds"]))
    workspace = _scan_workspace(dir_name, cfg, deadline, names, ids_by_suffix, only_task=task)
    if workspace is None:
        return None
    view = process_view(daemon_status(fresh=True))
    _detect_in_use(workspace, _scan_processes([workspace.path]), _locked_inodes(), cfg, view)
    return workspace


def workspace_state(dir_name: str) -> dict[str, Any] | None:
    """State of a single workspace directory, scanned on its own (used right before deleting)."""
    workspace = _fresh_state(dir_name)
    return workspace.as_dict() if workspace else None


def invalidate() -> None:
    _cache.pop("data", None)
    _daemon_cache.pop("data", None)


def find_task_dir(dir_name: str, task: str) -> str:
    """Validated absolute path of `<root>/<dir>/<task>` (for the terminal's working directory)."""
    _validate_name(dir_name, "workspace directory")
    _validate_name(task, "task directory")
    path = os.path.join(workspaces_root(), dir_name, task)
    if not os.path.isdir(path) or os.path.islink(path):
        raise DeleteError("task directory does not exist", status=404)
    return path


# --- deletion ----------------------------------------------------------------------------------


class DeleteError(Exception):
    def __init__(self, message: str, status: int = 400, reasons: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.reasons = reasons or []


def _validate_name(name: Any, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise DeleteError(f"{what} is required")
    if name in (".", "..") or name.startswith(".") or "/" in name or "\\" in name or "\0" in name:
        raise DeleteError(f"invalid {what}")


def _validate_dir(parent: str, name: str, what: str) -> str:
    _validate_name(name, what)
    path = os.path.join(parent, name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(parent):
        raise DeleteError(f"{what} is outside the workspaces root")
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        raise DeleteError(f"{what} does not exist", status=404) from None
    if not stat.S_ISDIR(mode):
        raise DeleteError("not a directory")
    return path


def _validate_dir_name(dir_name: str) -> None:
    _validate_dir(workspaces_root(), dir_name, "workspace directory")


def _rmtree_force(path: str) -> list[str]:
    """Remove a tree, fixing permissions of our own directories; returns paths that survived.

    Symlinks are removed, never followed or chmod()ed. Files a nested (rootful) container created
    as root cannot be removed by the agent user; they stay in the trash and are reported instead of
    failing the whole request.
    """
    surviving: list[str] = []
    uid = os.getuid()

    def fix_dir(target: str) -> None:
        try:
            st = os.lstat(target)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == uid and (st.st_mode & 0o700) != 0o700:
                os.chmod(target, stat.S_IMODE(st.st_mode) | 0o700)
        except OSError:
            pass

    # top-down: make our directories listable and writable so their content can be removed
    fix_dir(path)
    for current, dirs, _files in os.walk(path, topdown=True, onerror=lambda _exc: None):
        for name in dirs:
            fix_dir(os.path.join(current, name))

    for current, dirs, files in os.walk(path, topdown=False, onerror=lambda _exc: None):
        for name in files:
            target = os.path.join(current, name)
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
            except OSError:
                surviving.append(target)
        for name in dirs:
            target = os.path.join(current, name)
            try:
                if os.path.islink(target):
                    os.unlink(target)
                else:
                    os.rmdir(target)
            except FileNotFoundError:
                pass
            except OSError:
                surviving.append(target)
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError:
        surviving.append(path)
    return surviving[:20]


def _purge_trash(root: str, keep_hours: float) -> None:
    """Drop trash entries; `keep_hours` 0 removes them right away."""
    path = trash_dir(root)
    now = time.time()
    for entry in _listdir(path):
        entry_path = os.path.join(path, entry)
        try:
            age_hours = (now - os.lstat(entry_path).st_mtime) / 3600
        except OSError:
            continue
        if age_hours >= keep_hours:
            try:
                _rmtree_force(entry_path)
            except OSError:
                continue


def _remove(path: str, label: str, cfg: dict[str, Any], result: dict[str, Any]) -> None:
    root = cfg["root"]
    staging = path
    if cfg["trash"]:
        trash = trash_dir(root)
        staging = os.path.join(trash, f"{label}.{time.strftime('%Y%m%d-%H%M%S')}.{secrets.token_hex(3)}")
        try:
            os.makedirs(trash, mode=0o700, exist_ok=True)
            os.rename(path, staging)  # atomic: the daemon never sees a half-deleted directory
            os.utime(staging, None)
            result["trashed"] = True
        except OSError as exc:
            raise DeleteError(f"could not move the directory into the trash: {exc.strerror}", status=500) from exc
    result["leftovers"] = _rmtree_force(staging)
    if cfg["trash"]:
        _purge_trash(root, float(cfg["keep_trash_hours"]))
    invalidate()


def delete_workspace(dir_name: str, confirm: str, dry_run: bool = False) -> dict[str, Any]:
    """Delete one workspace's data directory. Raises DeleteError when that is not safe."""
    path = _validate_dir(workspaces_root(), dir_name, "workspace directory")
    if not isinstance(confirm, str) or confirm.strip() != dir_name:
        raise DeleteError("confirmation does not match the workspace directory")

    cfg = config()
    current = _fresh_state(dir_name)
    if current is None:
        raise DeleteError("workspace directory does not exist", status=404)
    if current.protected:
        raise DeleteError("workspace is protected by WEBUI_PROTECT_WORKSPACES", status=403)
    if current.in_use:
        raise DeleteError("workspace is in use", status=409, reasons=current.reasons)

    result = {
        "dir": dir_name,
        "task": None,
        "size_bytes": current.size_bytes,
        "size": human_bytes(current.size_bytes),
        "dry_run": dry_run,
        "trashed": False,
        "leftovers": [],
    }
    if not dry_run:
        _remove(path, dir_name, cfg, result)
    return result


def delete_task(dir_name: str, task: str, confirm: str, dry_run: bool = False) -> dict[str, Any]:
    """Delete one task directory of a workspace. Raises DeleteError when that is not safe."""
    workspace_path = _validate_dir(workspaces_root(), dir_name, "workspace directory")
    path = _validate_dir(workspace_path, task, "task directory")
    if not isinstance(confirm, str) or confirm.strip() != task:
        raise DeleteError("confirmation does not match the task directory")

    cfg = config()
    current = _fresh_state(dir_name, task)
    entry = next((item for item in current.tasks if item.name == task), None) if current else None
    if current is None or entry is None:
        raise DeleteError("task directory does not exist", status=404)
    if entry.protected:
        raise DeleteError("workspace is protected by WEBUI_PROTECT_WORKSPACES", status=403)
    if entry.in_use:
        raise DeleteError("task is in use", status=409, reasons=entry.reasons)

    result = {
        "dir": dir_name,
        "task": task,
        "size_bytes": entry.size_bytes,
        "size": human_bytes(entry.size_bytes),
        "dry_run": dry_run,
        "trashed": False,
        "leftovers": [],
    }
    if not dry_run:
        _remove(path, f"{dir_name}__{task}", cfg, result)
    return result


_delete_lock = threading.Lock()


def delete_many(items: Any, dry_run: bool = False) -> dict[str, Any]:
    """Delete workspaces (`{"dir"}`) or task directories (`{"dir", "task"}`); per-item results."""
    if not isinstance(items, list) or not items:
        raise DeleteError("nothing selected")
    if len(items) > 200:
        raise DeleteError("too many items selected at once")
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # one deletion at a time keeps the scan cache and /proc view consistent
    with _delete_lock:
        for item in items:
            if not isinstance(item, dict):
                failed.append({"dir": "", "task": None, "error": "invalid request item", "status": 400})
                continue
            dir_name = str(item.get("dir") or "")
            task = item.get("task")
            task = str(task) if task else None
            confirm = str(item.get("confirm") or "")
            try:
                if task is None:
                    result = delete_workspace(dir_name, confirm, dry_run=dry_run)
                else:
                    result = delete_task(dir_name, task, confirm, dry_run=dry_run)
                deleted.append(result)
            except DeleteError as exc:
                entry = {"dir": dir_name, "task": task, "error": str(exc), "status": exc.status, "reasons": exc.reasons}
                (skipped if exc.status in (403, 409) else failed).append(entry)
    return {"deleted": deleted, "failed": failed, "skipped": skipped, "dry_run": dry_run}
