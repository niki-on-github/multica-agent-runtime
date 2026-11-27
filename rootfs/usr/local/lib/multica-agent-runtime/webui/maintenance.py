# shellcheck shell=python
"""Cleanup jobs of the management web UI: old workspace data, Docker prune, Nix garbage collection.

Jobs run on demand ("Run now", optionally as a preview) or on a schedule (daily at a time, or every
N hours) with settings that are stored in `$WEBUI_STATE_DIR/maintenance.json`
(default `~/.local/state/multica-webui`, i.e. on the home volume). Environment variables only give
the defaults for a fresh state file:

    WEBUI_AUTO_CLEANUP=1            schedule on (default off)
    WEBUI_CLEANUP_AT=04:00          daily at (container time, UTC unless TZ is set)
    WEBUI_CLEANUP_EVERY_HOURS=0     instead of daily: every N hours
    WEBUI_CLEANUP_WORKSPACE_DAYS=7  task directories unused for N days (never ones in use)
    WEBUI_CLEANUP_DOCKER_HOURS=72   Docker objects older than N hours
    WEBUI_CLEANUP_NIX_DAYS=14       Nix profile generations older than N days, then GC

Steps:

- workspaces: every task directory that is deletable (the same in-use checks as a manual delete,
  re-checked right before) and unused for N days; then workspace directories left without tasks
- docker: `docker container|image|network|builder prune --filter until=<N>h` (+ volumes, off by
  default: they have no age filter); "images: unused" also removes tagged images no container uses
- nix: `nix-collect-garbage --delete-older-than <N>d` in this container's /nix
- trash: empties `<root>/.webui-trash`
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

import workspace_store as store

STEPS = ("workspaces", "docker", "nix", "trash")
HISTORY = 40
_lock = threading.Lock()  # one job at a time
_state_lock = threading.Lock()
_status: dict[str, Any] = {"running": False}


# --- settings ----------------------------------------------------------------------------------


def state_dir() -> str:
    default = os.path.join(os.environ.get("HOME") or "/tmp", ".local", "state", "multica-webui")
    return store._env("WEBUI_STATE_DIR") or default


def _state_path() -> str:
    return os.path.join(state_dir(), "maintenance.json")


def default_settings() -> dict[str, Any]:
    every = store._env_int("WEBUI_CLEANUP_EVERY_HOURS", 0)
    return {
        "enabled": store._env_bool("WEBUI_AUTO_CLEANUP", False),
        "schedule": {
            "mode": "interval" if every > 0 else "daily",
            "time": store._env("WEBUI_CLEANUP_AT") or "04:00",
            "every_hours": every if every > 0 else 24,
        },
        "workspaces": {
            "enabled": True,
            "older_than_days": store._env_int("WEBUI_CLEANUP_WORKSPACE_DAYS", 7),
            "empty_workspaces": True,
        },
        "docker": {
            "enabled": True,
            "older_than_hours": store._env_int("WEBUI_CLEANUP_DOCKER_HOURS", 72),
            "containers": True,
            "images": "dangling",  # dangling | unused
            "build_cache": True,
            "networks": True,
            "volumes": False,
        },
        "nix": {"enabled": True, "older_than_days": store._env_int("WEBUI_CLEANUP_NIX_DAYS", 14)},
        "trash": {"enabled": True},
    }


def _num(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def validate(raw: Any) -> dict[str, Any]:
    """Settings from a client, clamped to sane ranges; unknown keys are dropped."""
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    base = default_settings()
    out = copy.deepcopy(base)
    out["enabled"] = bool(raw.get("enabled", base["enabled"]))
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    mode = schedule.get("mode", base["schedule"]["mode"])
    out["schedule"]["mode"] = mode if mode in ("daily", "interval") else "daily"
    at = str(schedule.get("time", base["schedule"]["time"]))
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("schedule time must be HH:MM")
    out["schedule"]["time"] = at
    out["schedule"]["every_hours"] = int(_num(schedule.get("every_hours"), 1, 24 * 30, 24))
    for step in ("workspaces", "docker", "nix", "trash"):
        section = raw.get(step) if isinstance(raw.get(step), dict) else {}
        out[step]["enabled"] = bool(section.get("enabled", base[step]["enabled"]))
    ws = raw.get("workspaces") if isinstance(raw.get("workspaces"), dict) else {}
    out["workspaces"]["older_than_days"] = _num(ws.get("older_than_days"), 0.5, 3650, base["workspaces"]["older_than_days"])
    out["workspaces"]["empty_workspaces"] = bool(ws.get("empty_workspaces", True))
    docker = raw.get("docker") if isinstance(raw.get("docker"), dict) else {}
    out["docker"]["older_than_hours"] = int(_num(docker.get("older_than_hours"), 1, 24 * 3650, base["docker"]["older_than_hours"]))
    for key in ("containers", "build_cache", "networks", "volumes"):
        out["docker"][key] = bool(docker.get(key, base["docker"][key]))
    out["docker"]["images"] = docker.get("images") if docker.get("images") in ("dangling", "unused", "none") else "dangling"
    nix = raw.get("nix") if isinstance(raw.get("nix"), dict) else {}
    out["nix"]["older_than_days"] = int(_num(nix.get("older_than_days"), 0, 3650, base["nix"]["older_than_days"]))
    return out


def _load_state() -> dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp, _state_path())


def settings() -> dict[str, Any]:
    with _state_lock:
        stored = _load_state().get("settings")
    try:
        return validate(stored) if isinstance(stored, dict) else default_settings()
    except ValueError:
        return default_settings()


def save_settings(raw: Any) -> dict[str, Any]:
    clean = validate(raw)
    with _state_lock:
        state = _load_state()
        previous = state.get("settings") or {}
        state["settings"] = clean
        # (re)enabling or changing the schedule starts counting from now, not from the last run
        if clean["enabled"] and (not previous.get("enabled") or previous.get("schedule") != clean["schedule"]):
            state["schedule_anchor"] = time.time()
        _save_state(state)
    return clean


def history() -> list[dict[str, Any]]:
    with _state_lock:
        return list(_load_state().get("history") or [])


def next_run(cfg: dict[str, Any] | None = None, now: float | None = None) -> float | None:
    cfg = cfg or settings()
    if not cfg["enabled"]:
        return None
    now = time.time() if now is None else now
    with _state_lock:
        state = _load_state()
        if not state.get("schedule_anchor") and not state.get("last_scheduled_run"):
            # enabled through the environment only: count from the first time we see it
            state["schedule_anchor"] = now
            try:
                _save_state(state)
            except OSError:
                pass
    last = float(state.get("last_scheduled_run") or 0)
    anchor = max(last, float(state.get("schedule_anchor") or 0))
    if cfg["schedule"]["mode"] == "interval":
        hours = cfg["schedule"]["every_hours"]
        return (anchor or now) + hours * 3600
    hour, minute = (int(part) for part in cfg["schedule"]["time"].split(":"))
    base = dt.datetime.fromtimestamp(anchor or now)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= (anchor or now):
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


# --- helpers -----------------------------------------------------------------------------------

_SIZE = re.compile(r"([\d.]+)\s*([kKMGTP]?i?B)")
_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15,
          "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5}


def parse_size(text: str) -> int:
    match = _SIZE.search(text or "")
    if not match:
        return 0
    return int(float(match.group(1)) * _UNITS.get(match.group(2), 1))


def docker_reclaimed(output: str) -> int:
    """Sum of every `Total reclaimed space: 1.2GB` line of docker prune output."""
    return sum(parse_size(line.split(":", 1)[1]) for line in output.splitlines() if "reclaimed space" in line.lower())


def nix_freed(output: str) -> tuple[int, int]:
    """(store paths deleted, bytes freed) from nix-collect-garbage's last line."""
    match = re.search(r"(\d+) store paths deleted, ([\d.]+\s*[KMGTP]?i?B) freed", output or "")
    if not match:
        return 0, 0
    return int(match.group(1)), parse_size(match.group(2))


def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return proc.returncode, output


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    return not host.startswith("unix://") or os.path.exists(host[7:])


def docker_df() -> dict[str, Any]:
    if not docker_available():
        return {"available": False, "rows": []}
    code, output = _run(["docker", "system", "df", "--format", "{{json .}}"], 30)
    if code != 0:
        return {"available": False, "rows": [], "error": _tail(output, 3)}
    rows = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        rows.append(
            {
                "type": item.get("Type", ""),
                "total": item.get("TotalCount", ""),
                "active": item.get("Active", ""),
                "size": item.get("Size", ""),
                "size_bytes": parse_size(item.get("Size", "")),
                "reclaimable": item.get("Reclaimable", ""),
                "reclaimable_bytes": parse_size(item.get("Reclaimable", "")),
            }
        )
    return {"available": True, "rows": rows}


def nix_info() -> dict[str, Any]:
    """Nix availability, /nix usage, and whether this /nix is the daemon's (see module docstring)."""
    available = bool(shutil.which("nix-collect-garbage")) and os.path.isdir("/nix/store")
    info: dict[str, Any] = {"available": available, "shared_with_daemon": None}
    try:
        vfs = os.statvfs("/nix")
        info["fs_total"] = vfs.f_blocks * vfs.f_frsize
        info["fs_free"] = vfs.f_bavail * vfs.f_frsize
    except OSError:
        pass
    daemon_pid = _daemon_pid()
    if daemon_pid:
        try:
            own = os.stat("/nix/store")
            theirs = os.stat(f"/proc/{daemon_pid}/root/nix/store")
            info["shared_with_daemon"] = (own.st_dev, own.st_ino) == (theirs.st_dev, theirs.st_ino)
        except OSError:
            info["shared_with_daemon"] = None
    return info


def _daemon_pid() -> int | None:
    try:
        entries = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return None
    for entry in entries:
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv = handle.read(512).split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b"multica" and b"daemon" in argv[1:3]:
            return int(entry)
    return None


# --- steps -------------------------------------------------------------------------------------


def workspace_candidates(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Task directories (and empty workspaces) the workspace step would delete right now."""
    limit = float(cfg["workspaces"]["older_than_days"]) * 86400
    snap = store.snapshot(force=True)
    out = []
    for ws in snap["workspaces"]:
        for task in ws["tasks"]:
            age = task["last_activity_seconds"]
            if task["deletable"] and age is not None and age >= limit:
                out.append({"dir": ws["dir"], "task": task["name"], "size_bytes": task["size_bytes"], "age_seconds": age})
        age = ws["last_activity_seconds"]
        if cfg["workspaces"]["empty_workspaces"] and not ws["tasks"] and ws["deletable"] and age is not None and age >= limit:
            out.append({"dir": ws["dir"], "task": None, "size_bytes": ws["size_bytes"], "age_seconds": age})
    return out


def step_workspaces(cfg: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    candidates = workspace_candidates(cfg)
    if dry_run:
        return {
            "ok": True,
            "freed_bytes": sum(item["size_bytes"] for item in candidates),
            "detail": f"{len(candidates)} director{'y' if len(candidates) == 1 else 'ies'} unused for "
            f"{cfg['workspaces']['older_than_days']:g}+ days would be deleted",
            "items": [f"{item['dir']}/{item['task']}" if item["task"] else item["dir"] for item in candidates][:50],
        }
    items = [
        {"dir": item["dir"], "task": item["task"], "confirm": item["task"] or item["dir"]} for item in candidates
    ]
    if not items:
        return {"ok": True, "freed_bytes": 0, "detail": "nothing old enough", "items": []}
    result = store.delete_many(items)
    # task directories first, then the workspaces they emptied (the next run catches the rest)
    deleted = result["deleted"]
    return {
        "ok": not result["failed"],
        "freed_bytes": sum(item["size_bytes"] for item in deleted),
        "detail": f"{len(deleted)} deleted, {len(result['skipped'])} skipped (in use), {len(result['failed'])} failed",
        "items": [f"{item['dir']}/{item['task']}" if item.get("task") else item["dir"] for item in deleted][:50],
    }


def docker_commands(cfg: dict[str, Any]) -> list[list[str]]:
    docker = cfg["docker"]
    until = f"until={int(docker['older_than_hours'])}h"
    commands = []
    if docker["containers"]:
        commands.append(["docker", "container", "prune", "--force", "--filter", until])
    if docker["images"] != "none":
        commands.append(["docker", "image", "prune", "--force", "--filter", until] + (["--all"] if docker["images"] == "unused" else []))
    if docker["networks"]:
        commands.append(["docker", "network", "prune", "--force", "--filter", until])
    if docker["build_cache"]:
        commands.append(["docker", "builder", "prune", "--all", "--force", "--filter", until])
    if docker["volumes"]:
        commands.append(["docker", "volume", "prune", "--force"])  # anonymous volumes; no age filter exists
    return commands


def step_docker(cfg: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    if not docker_available():
        return {"ok": True, "skipped": True, "freed_bytes": 0, "detail": "no Docker daemon reachable"}
    if dry_run:
        df = docker_df()
        reclaimable = sum(row["reclaimable_bytes"] for row in df["rows"])
        return {
            "ok": True,
            "freed_bytes": 0,
            "detail": f"Docker reports {store.human_bytes(reclaimable)} reclaimable in total "
            f"(the age filter of {cfg['docker']['older_than_hours']} h applies on the real run)",
            "items": [" ".join(command) for command in docker_commands(cfg)],
        }
    freed = 0
    outputs = []
    ok = True
    for command in docker_commands(cfg):
        code, output = _run(command, 1800)
        ok = ok and code == 0
        freed += docker_reclaimed(output)
        outputs.append(f"$ {' '.join(command)}\n{_tail(output, 4)}")
    return {"ok": ok, "freed_bytes": freed, "detail": f"{store.human_bytes(freed)} reclaimed", "output": "\n".join(outputs)}


def step_nix(cfg: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    info = nix_info()
    if not info["available"]:
        return {"ok": True, "skipped": True, "freed_bytes": 0, "detail": "Nix not installed here"}
    days = int(cfg["nix"]["older_than_days"])
    command = ["nix-collect-garbage", "--delete-older-than", f"{days}d"] if days > 0 else ["nix-collect-garbage", "-d"]
    note = "" if info["shared_with_daemon"] is not False else " (this container's /nix - not the daemon's, see the docs)"
    if dry_run:
        code, output = _run(["nix-store", "--gc", "--print-dead"], 600)
        dead = len([line for line in output.splitlines() if line.startswith("/nix/store/")]) if code == 0 else 0
        return {
            "ok": code == 0,
            "freed_bytes": 0,
            "detail": f"{dead} store paths are unreachable now; old generations add more{note}",
            "items": [" ".join(command)],
        }
    code, output = _run(command, 3600)
    paths, freed = nix_freed(output)
    return {
        "ok": code == 0,
        "freed_bytes": freed,
        "detail": f"{paths} store paths deleted, {store.human_bytes(freed)} freed{note}",
        "output": _tail(output, 6),
    }


def step_trash(cfg: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    root = store.workspaces_root()
    path = store.trash_dir(root)
    entries = store._listdir(path)
    size = 0
    for entry in entries:
        size += store._walk_size(os.path.join(path, entry), time.monotonic() + 30)[0]
    if dry_run or not entries:
        return {"ok": True, "freed_bytes": size if dry_run else 0, "detail": f"{len(entries)} entries in .webui-trash"}
    store._purge_trash(root, 0)
    left = store._listdir(path)
    return {
        "ok": True,
        "freed_bytes": size,
        "detail": f"{len(entries) - len(left)} trash entries removed" + (f", {len(left)} left (root-owned files)" if left else ""),
    }


STEP_FUNCS = {"workspaces": step_workspaces, "docker": step_docker, "nix": step_nix, "trash": step_trash}


# --- jobs --------------------------------------------------------------------------------------


def status() -> dict[str, Any]:
    with _state_lock:
        return copy.deepcopy(_status)


def run(steps: list[str] | None, dry_run: bool, trigger: str, wait: bool = False) -> dict[str, Any]:
    """Start a job (in a thread unless `wait`); raises RuntimeError when one is running already."""
    cfg = settings()
    wanted = [step for step in (steps or [s for s in STEPS if cfg[s]["enabled"]]) if step in STEP_FUNCS]
    if not wanted:
        raise ValueError("no cleanup step selected")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("a cleanup job is already running")
    with _state_lock:
        _status.clear()
        _status.update({"running": True, "steps": wanted, "current": None, "dry_run": dry_run, "trigger": trigger, "started_at": time.time()})

    def job() -> dict[str, Any]:
        record = {"started_at": time.time(), "trigger": trigger, "dry_run": dry_run, "steps": {}}
        try:
            for step in wanted:
                with _state_lock:
                    _status["current"] = step
                started = time.monotonic()
                try:
                    result = STEP_FUNCS[step](cfg, dry_run)
                except Exception as exc:  # noqa: BLE001 - one failing step must not stop the others
                    result = {"ok": False, "freed_bytes": 0, "detail": f"error: {exc}"}
                result["seconds"] = round(time.monotonic() - started, 1)
                record["steps"][step] = result
        finally:
            record["finished_at"] = time.time()
            record["freed_bytes"] = sum(item.get("freed_bytes", 0) for item in record["steps"].values()) if not dry_run else 0
            record["ok"] = all(item.get("ok") for item in record["steps"].values())
            with _state_lock:
                state = _load_state()
                state["history"] = ([record] + list(state.get("history") or []))[:HISTORY]
                if trigger == "schedule":
                    state["last_scheduled_run"] = record["started_at"]
                try:
                    _save_state(state)
                except OSError as exc:
                    print(f"[webui] cannot save maintenance state: {exc}", flush=True)
                _status.clear()
                _status.update({"running": False, "last": record})
            store.invalidate()
            _lock.release()
        return record

    if wait:
        return job()
    threading.Thread(target=job, name="webui-maintenance", daemon=True).start()
    return status()


class Scheduler:
    """Starts the enabled steps when `next_run()` is due (checked every 30 s)."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="webui-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                due = next_run()
                if due is not None and time.time() >= due and not status().get("running"):
                    print("[webui] scheduled cleanup starting", flush=True)
                    run(None, dry_run=False, trigger="schedule")
            except Exception as exc:  # noqa: BLE001 - the scheduler must survive anything
                print(f"[webui] scheduler: {exc}", flush=True)


def payload() -> dict[str, Any]:
    cfg = settings()
    due = next_run(cfg)
    return {
        "settings": cfg,
        "next_run": due,
        "status": status(),
        "history": history()[:20],
        "docker": docker_df(),
        "nix": nix_info(),
        "trash_entries": len(store._listdir(store.trash_dir(store.workspaces_root()))),
        "state_file": _state_path(),
    }
