# shellcheck shell=python
"""Resource usage, processes and agent status for the management web UI (standard library only).

A background sampler reads /proc and the container's cgroup every `WEBUI_SAMPLE_SECONDS` (2 s) and
keeps `WEBUI_HISTORY` samples (180, 6 min) for the charts:

- host:       /proc/stat, /proc/meminfo, /proc/loadavg (the node, unless lxcfs virtualises them)
- cgroup:     /sys/fs/cgroup cpu.stat, cpu.max, memory.current, memory.max (this container)
- processes:  every process visible in this PID namespace, with CPU and RSS - with a shared PID
              namespace (`shareProcessNamespace: true`) that is the whole pod: daemon, agents, tools
- network:    /proc/net/dev (the pod's network namespace)

On demand: filesystems (statvfs), GPUs (nvidia-smi), Docker containers (docker ps), and the agent
tasks: processes of the agent CLIs (claude, codex, opencode, pi, omp) grouped by task directory.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any

import workspace_store as store

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
AGENT_CLIS = {"claude", "codex", "opencode", "opencode2", "pi", "omp"}
CGROUP = "/sys/fs/cgroup"


def _read(path: str, limit: int = 1 << 16) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def _boot_time() -> float:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return time.time() - float(_read("/proc/uptime").split()[0] or 0)


BOOT_TIME = _boot_time()


# --- host and cgroup ---------------------------------------------------------------------------


def _host_cpu_ticks() -> tuple[int, int] | None:
    line = _read("/proc/stat").split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return None
    values = [int(value) for value in line[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values[:8]), idle


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            info[name] = int(parts[0]) * 1024
    return info


def _cgroup() -> dict[str, Any]:
    """cgroup v2 of this container (absent on cgroup v1 hosts)."""
    if not os.path.exists(os.path.join(CGROUP, "cgroup.controllers")):
        return {"available": False}
    usage_usec = None
    for line in _read(os.path.join(CGROUP, "cpu.stat")).splitlines():
        if line.startswith("usage_usec "):
            usage_usec = _int(line.split()[1])
    quota = _read(os.path.join(CGROUP, "cpu.max")).split()
    cpu_limit = None
    if len(quota) == 2 and quota[0] != "max":
        try:
            cpu_limit = int(quota[0]) / int(quota[1])
        except (ValueError, ZeroDivisionError):
            cpu_limit = None
    mem_max = _read(os.path.join(CGROUP, "memory.max")).strip()
    return {
        "available": True,
        "usage_usec": usage_usec,
        "cpu_limit_cores": cpu_limit,
        "memory_current": _int(_read(os.path.join(CGROUP, "memory.current"))),
        "memory_max": _int(mem_max) if mem_max and mem_max != "max" else None,
        "pids_current": _int(_read(os.path.join(CGROUP, "pids.current"))),
    }


def _net_totals() -> tuple[int, int]:
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name.strip() == "lo" or len(parts) < 9:
            continue
        rx += int(parts[0])
        tx += int(parts[8])
    return rx, tx


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


# --- processes ---------------------------------------------------------------------------------


def _proc_stat(pid: str) -> dict[str, Any] | None:
    raw = _read(f"/proc/{pid}/stat", 4096)
    if not raw:
        return None
    try:
        head, tail = raw.rsplit(")", 1)
        comm = head.split("(", 1)[1]
        fields = tail.split()
        return {
            "pid": int(pid),
            "comm": comm,
            "state": fields[0],
            "ppid": int(fields[1]),
            "ticks": int(fields[11]) + int(fields[12]),
            "threads": int(fields[17]),
            "start": int(fields[19]),
            "rss": int(fields[21]) * PAGE_SIZE,
        }
    except (IndexError, ValueError):
        return None


def _argv(pid: int) -> list[str]:
    raw = _read(f"/proc/{pid}/cmdline", 4096)
    return [part for part in raw.split("\0") if part]


def _uid(pid: int) -> int | None:
    for line in _read(f"/proc/{pid}/status", 4096).splitlines():
        if line.startswith("Uid:"):
            return _int(line.split()[1])
    return None


class Sampler:
    """Periodic samples of host, cgroup, process and network counters (thread-safe snapshot)."""

    def __init__(self, interval: float, history: int) -> None:
        self.interval = max(0.5, interval)
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=max(10, history))
        self.lock = threading.Lock()
        self.processes: dict[int, dict[str, Any]] = {}
        self.latest: dict[str, Any] = {}
        self._prev: dict[str, Any] = {}
        self._argv_cache: dict[tuple[int, int], list[str]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is None:
            self.sample()
            self._thread = threading.Thread(target=self._run, name="webui-sampler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 - a bad sample must not stop the sampler
                print(f"[webui] sampler: {exc}", flush=True)

    def sample(self) -> None:
        now = time.monotonic()
        prev = self._prev
        elapsed = now - prev["at"] if prev else 0.0

        host = _host_cpu_ticks()
        host_cpu = None
        if host and prev.get("host") and host[0] > prev["host"][0]:
            total = host[0] - prev["host"][0]
            idle = host[1] - prev["host"][1]
            host_cpu = max(0.0, min(100.0, 100.0 * (total - idle) / total))

        cgroup = _cgroup()
        cgroup_cores = None
        if cgroup.get("usage_usec") is not None and prev.get("cg_usage") is not None and elapsed > 0:
            cgroup_cores = max(0.0, (cgroup["usage_usec"] - prev["cg_usage"]) / 1e6 / elapsed)

        rx, tx = _net_totals()
        rx_rate = tx_rate = None
        if prev.get("net") and elapsed > 0:
            rx_rate = max(0.0, (rx - prev["net"][0]) / elapsed)
            tx_rate = max(0.0, (tx - prev["net"][1]) / elapsed)

        processes: dict[int, dict[str, Any]] = {}
        prev_ticks: dict[tuple[int, int], int] = prev.get("ticks", {})
        ticks: dict[tuple[int, int], int] = {}
        try:
            pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            info = _proc_stat(pid)
            if info is None:
                continue
            key = (info["pid"], info["start"])
            ticks[key] = info["ticks"]
            before = prev_ticks.get(key)
            info["cpu"] = (
                max(0.0, 100.0 * (info["ticks"] - before) / CLK_TCK / elapsed) if before is not None and elapsed > 0 else 0.0
            )
            if key not in self._argv_cache:
                self._argv_cache[key] = _argv(info["pid"])
            info["argv"] = self._argv_cache[key]
            info["started_at"] = BOOT_TIME + info["start"] / CLK_TCK
            processes[info["pid"]] = info
        self._argv_cache = {key: value for key, value in self._argv_cache.items() if key in ticks}

        mem = _meminfo()
        mem_total = mem.get("MemTotal")
        mem_used = mem_total - mem.get("MemAvailable", mem.get("MemFree", 0)) if mem_total else None
        procs_cpu = sum(p["cpu"] for p in processes.values()) / 100.0
        procs_rss = sum(p["rss"] for p in processes.values())
        point = {
            "t": time.time(),
            "host_cpu": host_cpu,
            "cgroup_cores": cgroup_cores,
            "procs_cores": procs_cpu if elapsed > 0 else None,
            "procs_rss": procs_rss,
            "mem_used": mem_used,
            "cgroup_mem": cgroup.get("memory_current"),
            "rx": rx_rate,
            "tx": tx_rate,
        }
        load = _read("/proc/loadavg").split()
        latest = {
            "host": {
                "cpu_percent": host_cpu,
                "cpus": os.cpu_count() or 1,
                "cpus_usable": _cpu_count(),
                "load": [float(value) for value in load[:3]] if len(load) >= 3 else None,
                "mem_total": mem_total,
                "mem_used": mem_used,
                "mem_available": mem.get("MemAvailable"),
                "swap_total": mem.get("SwapTotal"),
                "swap_used": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) if mem.get("SwapTotal") else 0,
                "uptime_seconds": max(0.0, time.time() - BOOT_TIME),
            },
            "cgroup": {**cgroup, "cpu_cores": cgroup_cores},
            "processes": {"count": len(processes), "cpu_cores": point["procs_cores"], "rss": procs_rss},
            "network": {"rx_rate": rx_rate, "tx_rate": tx_rate, "rx_total": rx, "tx_total": tx},
        }
        with self.lock:
            self.processes = processes
            self.latest = latest
            if elapsed > 0:
                self.history.append(point)
        self._prev = {"at": now, "host": host, "cg_usage": cgroup.get("usage_usec"), "net": (rx, tx), "ticks": ticks}

    def snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, dict[str, Any]]]:
        with self.lock:
            return dict(self.latest), list(self.history), dict(self.processes)


_sampler: Sampler | None = None
_sampler_lock = threading.Lock()


def sampler() -> Sampler:
    global _sampler  # noqa: PLW0603 - one sampler per process
    with _sampler_lock:
        if _sampler is None:
            _sampler = Sampler(store._env_float("WEBUI_SAMPLE_SECONDS", 2.0), store._env_int("WEBUI_HISTORY", 180))
            _sampler.start()
        return _sampler


# --- filesystems, GPU, Docker ------------------------------------------------------------------


def filesystems() -> list[dict[str, Any]]:
    raw = store._env("WEBUI_DISKS") or "/data,/home/agent,/nix,/tmp"
    seen: set[int] = set()
    result = []
    for path in [part.strip() for part in raw.split(",") if part.strip()]:
        try:
            st = os.stat(path)
            vfs = os.statvfs(path)
        except OSError:
            continue
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        total = vfs.f_blocks * vfs.f_frsize
        free = vfs.f_bavail * vfs.f_frsize
        used = total - vfs.f_bfree * vfs.f_frsize
        if total <= 0:
            continue
        result.append(
            {
                "path": path,
                "total": total,
                "used": used,
                "free": free,
                "percent": round(100.0 * used / max(1, used + free), 1),
                "inodes_percent": round(100.0 * (vfs.f_files - vfs.f_ffree) / vfs.f_files, 1) if vfs.f_files else None,
            }
        )
    return result


_cached: dict[str, tuple[float, Any]] = {}
_cached_lock = threading.Lock()


def _memo(name: str, ttl: float, fn):  # noqa: ANN001, ANN202 - small cache helper
    with _cached_lock:
        hit = _cached.get(name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cached_lock:
        _cached[name] = (time.monotonic(), value)
    return value


def _gpus() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue

        def num(value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                return None

        gpus.append(
            {
                "index": parts[0],
                "name": parts[1],
                "utilization": num(parts[2]),
                "memory_used": (num(parts[3]) or 0) * 1024 * 1024,
                "memory_total": (num(parts[4]) or 0) * 1024 * 1024,
                "temperature": num(parts[5]),
                "power": num(parts[6]) if len(parts) > 6 else None,
            }
        )
    return gpus


def gpus() -> list[dict[str, Any]]:
    return _memo("gpus", 5, _gpus)


def _docker() -> dict[str, Any]:
    if not store._env_bool("WEBUI_DOCKER", True) or not shutil.which("docker"):
        return {"available": False, "containers": []}
    host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if host.startswith("unix://") and not os.path.exists(host[7:]):
        return {"available": False, "containers": [], "error": f"no Docker socket at {host[7:]}"}
    try:
        proc = subprocess.run(
            ["docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "containers": [], "error": str(exc)[:200]}
    if proc.returncode != 0:
        return {"available": False, "containers": [], "error": proc.stderr.decode("utf-8", "replace").strip()[:200]}
    containers = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        containers.append(
            {
                "id": str(item.get("ID", ""))[:12],
                "name": item.get("Names", ""),
                "image": item.get("Image", ""),
                "state": item.get("State", ""),
                "status": item.get("Status", ""),
                "ports": item.get("Ports", ""),
                "created": item.get("RunningFor", ""),
            }
        )
    return {"available": True, "containers": containers}


def docker() -> dict[str, Any]:
    return _memo("docker", 8, _docker)


# --- agents ------------------------------------------------------------------------------------


def _agent_cli(argv: list[str]) -> str | None:
    """The agent CLI a command line runs (`claude`, `node /usr/local/bin/pi`, ...), else None."""
    if not argv or os.path.basename(argv[0]) == "bwrap":
        return None
    candidates = [argv[0]]
    if os.path.basename(argv[0]) in ("node", "bun", "deno") and len(argv) > 1:
        candidates.append(argv[1])
    for candidate in candidates:
        base = os.path.basename(candidate)
        for suffix in (".exe", ".js", ".mjs", ".cjs"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        if base in AGENT_CLIS:
            return base
        # npm installs: .../node_modules/@openai/codex/bin/codex.js, .../@anthropic-ai/claude-code/cli.js
        for name, needle in (("claude", "/@anthropic-ai/claude-code/"), ("codex", "/@openai/codex/"),
                             ("opencode", "/@opencode/"), ("pi", "/pi-coding-agent/")):
            if needle in candidate:
                return name
    return None


def _children(processes: dict[int, dict[str, Any]]) -> dict[int, list[int]]:
    tree: dict[int, list[int]] = collections.defaultdict(list)
    for pid, info in processes.items():
        tree[info["ppid"]].append(pid)
    return tree


def _subtree(pid: int, tree: dict[int, list[int]]) -> list[int]:
    out, stack = [], [pid]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(tree.get(current, []))
    return out


def _task_of(path: str, root: str) -> tuple[str, str] | None:
    if not path.startswith(root + os.sep):
        return None
    parts = path[len(root) + 1 :].split(os.sep)
    if len(parts) >= 2 and not parts[0].startswith(".") and not parts[1].startswith("."):
        return parts[0], parts[1]
    if parts and not parts[0].startswith("."):
        return parts[0], ""
    return None


def agent_tasks(processes: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Running agent CLIs (topmost process of each), with the summed usage of their process tree."""
    root = os.path.realpath(store.workspaces_root())
    tree = _children(processes)
    agents = {pid: cli for pid, info in processes.items() if (cli := _agent_cli(info["argv"]))}
    tasks = []
    for pid, cli in agents.items():
        parent = processes[pid]["ppid"]
        nested = False
        for _ in range(64):  # a CLI started by another agent CLI belongs to that one
            if parent in agents:
                nested = True
                break
            if parent not in processes:
                break
            parent = processes[parent]["ppid"]
        if nested:
            continue
        info = processes[pid]
        members = [member for member in _subtree(pid, tree) if member in processes]
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        where = _task_of(os.path.realpath(cwd) if cwd else "", root)
        meta: dict[str, Any] = {}
        if where and where[1]:
            task_path = os.path.join(root, where[0], where[1])
            meta = store._read_json(os.path.join(task_path, ".managed_env.json"))
            meta.update({k: v for k, v in store._read_json(os.path.join(task_path, ".task_owner")).items() if v})
        argv = info["argv"]
        model = ""
        for flag in ("--model", "-m"):
            if flag in argv[:-1]:
                model = argv[argv.index(flag) + 1]
                break
        tasks.append(
            {
                "pid": pid,
                "cli": cli,
                "model": model,
                "cwd": cwd,
                "workspace_dir": where[0] if where else "",
                "task_dir": where[1] if where else "",
                "issue_id": str(meta.get("issue_id") or ""),
                "agent_id": str(meta.get("agent_id") or ""),
                "task_id": str(meta.get("task_id") or ""),
                "workspace_id": str(meta.get("workspace_id") or ""),
                "started_at": info["started_at"],
                "running_seconds": max(0.0, time.time() - info["started_at"]),
                "cpu": round(sum(processes[m]["cpu"] for m in members), 1),
                "rss": sum(processes[m]["rss"] for m in members),
                "process_count": len(members),
                "children": sorted({processes[m]["comm"] for m in members if m != pid})[:12],
            }
        )
    tasks.sort(key=lambda item: item["started_at"])
    return tasks


def _daemon_process(processes: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    for pid, info in processes.items():
        argv = info["argv"]
        if argv and os.path.basename(argv[0]) == "multica" and "daemon" in argv[1:3]:
            tree = _children(processes)
            members = [member for member in _subtree(pid, tree) if member in processes]
            return {
                "pid": pid,
                "cpu": round(info["cpu"], 1),
                "rss": info["rss"],
                "tree_cpu": round(sum(processes[m]["cpu"] for m in members), 1),
                "tree_rss": sum(processes[m]["rss"] for m in members),
                "running_seconds": max(0.0, time.time() - info["started_at"]),
            }
    return None


def top_processes(processes: dict[int, dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    items = sorted(processes.values(), key=lambda p: (p["cpu"], p["rss"]), reverse=True)[:limit]
    return [
        {
            "pid": p["pid"],
            "ppid": p["ppid"],
            "name": p["comm"],
            "cmd": " ".join(p["argv"])[:240] or f"[{p['comm']}]",
            "state": p["state"],
            "cpu": round(p["cpu"], 1),
            "rss": p["rss"],
            "threads": p["threads"],
            "uid": _uid(p["pid"]),
            "running_seconds": max(0.0, time.time() - p["started_at"]),
        }
        for p in items
    ]


# --- API payloads ------------------------------------------------------------------------------


def system_payload() -> dict[str, Any]:
    latest, history, processes = sampler().snapshot()
    return {
        "generated_at": time.time(),
        "interval_seconds": sampler().interval,
        **latest,
        "filesystems": filesystems(),
        "gpus": gpus(),
        "history": history,
        "top": top_processes(processes),
    }


def agents_payload() -> dict[str, Any]:
    _latest, _history, processes = sampler().snapshot()
    daemon = store.daemon_status()
    return {
        "generated_at": time.time(),
        "daemon": daemon,
        "daemon_process": _daemon_process(processes),
        "process_view": store.process_view(daemon),
        "tasks": agent_tasks(processes),
        "docker": docker(),
    }
