#!/usr/bin/env python3
# shellcheck shell=python
"""Functional test of the management web UI (standard library only).

    python3 tests/webui.py            # from the repo, or /tests/webui.py inside the image

It builds a throw-away workspaces root, so it never touches real data: an idle workspace, a
protected one and one with a live process in it; plus the terminal WebSocket and the status
endpoints. Run by tests/smoke.sh.
"""

from __future__ import annotations

import base64
import fcntl
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def webui_dir() -> str:
    candidates = [
        os.environ.get("WEBUI_DIR", ""),
        "/usr/local/lib/multica-agent-runtime/webui",
        os.path.join(HERE, "..", "rootfs", "usr", "local", "lib", "multica-agent-runtime", "webui"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "server.py")):
            return os.path.abspath(candidate)
    raise SystemExit("webui/server.py not found (set WEBUI_DIR)")


WEBUI = webui_dir()
sys.path.insert(0, WEBUI)

ROOT = tempfile.mkdtemp(prefix="webui-test-")
STATE = tempfile.mkdtemp(prefix="webui-state-")
os.environ.update(
    {
        "WEBUI_STATE_DIR": STATE,
        "WEBUI_USAGE_CACHE_SECONDS": "0",
        "WEBUI_WORKSPACES_ROOT": ROOT,
        "WEBUI_ACTIVITY_GRACE": "300",
        "WEBUI_RESOLVE_NAMES": "0",
        "WEBUI_DAEMON_URL": "http://127.0.0.1:9",  # closed port: daemon status stays "offline"
        "WEBUI_CACHE_SECONDS": "0",
        "WEBUI_KEEP_TRASH_HOURS": "0",
        "WEBUI_SCAN_BUDGET": "30",
        "WEBUI_PROTECT_WORKSPACES": "beta-222222222222",
    }
)

import agents_info  # noqa: E402
import env_vars  # noqa: E402
import maintenance  # noqa: E402
import server as webui_server  # noqa: E402
import system_stats  # noqa: E402
import workspace_store as store  # noqa: E402

OLD = time.time() - 3 * 86400


def age_tree(path: str, mtime: float = OLD) -> None:
    """Give everything below `path` (and the directories) an old mtime."""
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(current, d))]:
            os.utime(os.path.join(current, name), (mtime, mtime), follow_symlinks=False)
        os.utime(current, (mtime, mtime))


def root_dir() -> str:
    """The root the store currently scans (tests switch it per class)."""
    return os.environ["WEBUI_WORKSPACES_ROOT"]


def make_workspace(dir_name: str, task: str = "home-1-0123456789ab", payload: int = 4096) -> str:
    workspace = os.path.join(root_dir(), dir_name)
    workdir = os.path.join(workspace, task, "workdir")
    os.makedirs(os.path.join(workspace, task, "logs"), exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workspace, task, ".task_owner"), "w", encoding="utf-8") as handle:
        json.dump({"workspace_id": f"00000000-0000-0000-0000-{dir_name.rsplit('-', 1)[-1]}", "task_id": "task"}, handle)
    with open(os.path.join(workspace, task, ".task_lock"), "w", encoding="utf-8"):
        pass
    with open(os.path.join(workdir, "data.bin"), "wb") as handle:
        handle.write(b"x" * payload)
    age_tree(workspace)
    return workspace


class WorkspaceStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["WEBUI_WORKSPACES_ROOT"] = ROOT
        store.invalidate()
        cls.alpha = make_workspace("alpha-111111111111")
        cls.beta = make_workspace("beta-222222222222")
        cls.gamma = make_workspace("gamma-333333333333")
        os.makedirs(os.path.join(ROOT, ".repos", "cache"), exist_ok=True)
        with open(os.path.join(ROOT, ".repos", "cache", "x"), "w", encoding="utf-8") as handle:
            handle.write("repo cache")
        # a live process in gamma: this is what a running task looks like
        cls.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=os.path.join(cls.gamma, "home-1-0123456789ab", "workdir"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if cls.proc.poll() is None:
                break
            time.sleep(0.1)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(ROOT, ignore_errors=True)
        shutil.rmtree(STATE, ignore_errors=True)

    def by_dir(self, snapshot: dict) -> dict[str, dict]:
        return {item["dir"]: item for item in snapshot["workspaces"]}

    def test_snapshot_lists_workspaces(self) -> None:
        snapshot = store.snapshot(force=True)
        items = self.by_dir(snapshot)
        self.assertEqual(sorted(items), ["alpha-111111111111", "beta-222222222222", "gamma-333333333333"])
        self.assertNotIn(".repos", items)
        self.assertEqual(snapshot["totals"]["workspace_count"], 3)
        self.assertGreaterEqual(snapshot["totals"]["size_bytes"], 3 * 4096)
        self.assertEqual(items["alpha-111111111111"]["task_count"], 1)
        self.assertEqual(items["alpha-111111111111"]["workspace_id"], "00000000-0000-0000-0000-111111111111")

    def test_in_use_detection(self) -> None:
        items = self.by_dir(store.snapshot(force=True))
        self.assertFalse(items["alpha-111111111111"]["in_use"])
        self.assertTrue(items["alpha-111111111111"]["deletable"])
        self.assertTrue(items["gamma-333333333333"]["in_use"])
        self.assertFalse(items["gamma-333333333333"]["deletable"])
        self.assertTrue(any("process" in reason for reason in items["gamma-333333333333"]["reasons"]))
        self.assertTrue(items["gamma-333333333333"]["processes"])
        self.assertEqual(items["gamma-333333333333"]["processes"][0]["pid"], self.proc.pid)
        self.assertTrue(items["beta-222222222222"]["protected"])
        self.assertFalse(items["beta-222222222222"]["deletable"])

    def test_recent_activity_blocks_deletion(self) -> None:
        path = os.path.join(ROOT, "delta-444444444444", "home-2-aaaaaaaaaaaa", "workdir")
        os.makedirs(path, exist_ok=True)
        try:
            with open(os.path.join(path, "fresh.txt"), "w", encoding="utf-8") as handle:
                handle.write("just written")
            item = self.by_dir(store.snapshot(force=True))["delta-444444444444"]
            self.assertTrue(item["in_use"])
            self.assertTrue(any("seconds ago" in reason or "s ago" in reason for reason in item["reasons"]))
            with self.assertRaises(store.DeleteError) as raised:
                store.delete_workspace("delta-444444444444", "delta-444444444444")
            self.assertEqual(raised.exception.status, 409)
        finally:
            shutil.rmtree(os.path.join(ROOT, "delta-444444444444"), ignore_errors=True)

    def test_refusals(self) -> None:
        cases = [
            ("gamma-333333333333", "gamma-333333333333", 409),  # in use
            ("beta-222222222222", "beta-222222222222", 403),  # protected
            ("alpha-111111111111", "wrong", 400),  # confirmation mismatch
            ("../etc", "../etc", 400),  # traversal
            (".repos", ".repos", 400),  # infrastructure directory
            ("missing-000000000000", "missing-000000000000", 404),
        ]
        for dir_name, confirm, status in cases:
            with self.subTest(dir=dir_name):
                with self.assertRaises(store.DeleteError) as raised:
                    store.delete_workspace(dir_name, confirm)
                self.assertEqual(raised.exception.status, status)
        self.assertTrue(os.path.isdir(os.path.join(ROOT, ".repos")))

    def test_dry_run_and_delete(self) -> None:
        target = make_workspace("zeta-555555555555")
        dry = store.delete_workspace("zeta-555555555555", "zeta-555555555555", dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertGreater(dry["size_bytes"], 0)
        self.assertTrue(os.path.isdir(target))

        result = store.delete_workspace("zeta-555555555555", "zeta-555555555555")
        self.assertEqual(result["dir"], "zeta-555555555555")
        self.assertFalse(os.path.exists(target))
        self.assertEqual(result["leftovers"], [])
        self.assertEqual(os.listdir(os.path.join(ROOT, ".webui-trash")), [])
        self.assertNotIn("zeta-555555555555", self.by_dir(store.snapshot(force=True)))

    def test_delete_many_reports_per_item(self) -> None:
        one = make_workspace("one-666666666666")
        result = store.delete_many(
            [
                {"dir": "one-666666666666", "confirm": "one-666666666666"},
                {"dir": "gamma-333333333333", "confirm": "gamma-333333333333"},
                {"dir": "../etc", "confirm": "../etc"},
            ]
        )
        self.assertEqual([item["dir"] for item in result["deleted"]], ["one-666666666666"])
        self.assertEqual([item["dir"] for item in result["skipped"]], ["gamma-333333333333"])
        self.assertEqual([item["dir"] for item in result["failed"]], ["../etc"])
        self.assertFalse(os.path.exists(one))
        with self.assertRaises(store.DeleteError):
            store.delete_many([])


    def test_lock_parsing_and_detection(self) -> None:
        target = make_workspace("lock-121212121212")
        lock_path = os.path.join(target, "home-1-0123456789ab", ".task_lock")
        # this process holds the lock (and is skipped by the process scan): only the lock signal fires
        with open(lock_path, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            st = os.stat(lock_path)
            locked, inodes = store._locked_inodes()
            self.assertIn((store._dev_id(st), st.st_ino), locked)
            self.assertIn(st.st_ino, inodes)
            item = self.by_dir(store.snapshot(force=True))["lock-121212121212"]
            self.assertTrue(item["in_use"])
            self.assertTrue(item["tasks"][0]["locked"])
            self.assertIn("task lock held", item["tasks"][0]["reasons"])
            with self.assertRaises(store.DeleteError) as raised:
                store.delete_task("lock-121212121212", "home-1-0123456789ab", "home-1-0123456789ab")
            self.assertEqual(raised.exception.status, 409)
            fcntl.flock(handle, fcntl.LOCK_UN)
        item = self.by_dir(store.snapshot(force=True))["lock-121212121212"]
        self.assertFalse(item["in_use"], item["reasons"])
        shutil.rmtree(target, ignore_errors=True)

    def test_task_deletion(self) -> None:
        target = make_workspace("multi-131313131313", task="home-1-111111111111")
        make_workspace("multi-131313131313", task="home-2-222222222222")
        busy_dir = os.path.join(target, "home-2-222222222222", "workdir")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=busy_dir)
        try:
            time.sleep(0.3)
            item = self.by_dir(store.snapshot(force=True))["multi-131313131313"]
            tasks = {task["name"]: task for task in item["tasks"]}
            self.assertTrue(item["in_use"])
            self.assertEqual(item["tasks_in_use"], 1)
            self.assertTrue(tasks["home-1-111111111111"]["deletable"])
            self.assertFalse(tasks["home-2-222222222222"]["deletable"])
            with self.assertRaises(store.DeleteError) as raised:
                store.delete_workspace("multi-131313131313", "multi-131313131313")
            self.assertEqual(raised.exception.status, 409)
            with self.assertRaises(store.DeleteError):
                store.delete_task("multi-131313131313", "home-1-111111111111", "wrong")
            with self.assertRaises(store.DeleteError):
                store.delete_task("multi-131313131313", "../alpha-111111111111", "../alpha-111111111111")
            result = store.delete_many(
                [
                    {"dir": "multi-131313131313", "task": "home-1-111111111111", "confirm": "home-1-111111111111"},
                    {"dir": "multi-131313131313", "task": "home-2-222222222222", "confirm": "home-2-222222222222"},
                ]
            )
            self.assertEqual([entry["task"] for entry in result["deleted"]], ["home-1-111111111111"])
            self.assertEqual([entry["task"] for entry in result["skipped"]], ["home-2-222222222222"])
            self.assertFalse(os.path.exists(os.path.join(target, "home-1-111111111111")))
            self.assertTrue(os.path.isdir(busy_dir))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            shutil.rmtree(target, ignore_errors=True)

    def test_blind_mode_uses_long_grace(self) -> None:
        target = make_workspace("blind-141414141414")
        age_tree(target, time.time() - 3600)  # an hour old: past the normal grace, within the blind one
        try:
            view = store.process_view()
            self.assertEqual(view["mode"], "blind")  # no daemon process in this test
            self.assertTrue(view["strict"])  # and no daemon that reports 0 running tasks
            item = self.by_dir(store.snapshot(force=True))["blind-141414141414"]
            self.assertTrue(item["in_use"])
            self.assertTrue(any("blind grace" in reason for reason in item["tasks"][0]["reasons"]))
            os.environ["WEBUI_ASSUME_SHARED_PID"] = "1"
            item = self.by_dir(store.snapshot(force=True))["blind-141414141414"]
            self.assertTrue(item["deletable"], item["reasons"])
        finally:
            os.environ.pop("WEBUI_ASSUME_SHARED_PID", None)
            shutil.rmtree(target, ignore_errors=True)

    def test_delete_never_follows_symlinks(self) -> None:
        outside = tempfile.mkdtemp(prefix="webui-outside-")
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("keep me")
        os.chmod(secret, 0o400)
        target = make_workspace("link-151515151515")
        workdir = os.path.join(target, "home-1-0123456789ab", "workdir")
        os.symlink(secret, os.path.join(workdir, "file-link"))
        os.symlink(outside, os.path.join(workdir, "dir-link"))
        locked_dir = os.path.join(workdir, "readonly")
        os.makedirs(locked_dir)
        with open(os.path.join(locked_dir, "x"), "w", encoding="utf-8") as handle:
            handle.write("x")
        os.chmod(locked_dir, 0o500)
        age_tree(target)
        try:
            result = store.delete_workspace("link-151515151515", "link-151515151515")
            self.assertEqual(result["leftovers"], [])
            self.assertFalse(os.path.exists(target))
            self.assertTrue(os.path.isfile(secret))
            self.assertEqual(os.stat(secret).st_mode & 0o777, 0o400)
        finally:
            os.chmod(secret, 0o600)
            shutil.rmtree(outside, ignore_errors=True)


    def test_cleanup_workspace_step(self) -> None:
        old = make_workspace("cleanup-161616161616", task="home-1-0000000000aa")
        make_workspace("cleanup-161616161616", task="home-2-0000000000bb")
        age_tree(os.path.join(old, "home-1-0000000000aa"), time.time() - 10 * 86400)
        age_tree(os.path.join(old, "home-2-0000000000bb"), time.time() - 2 * 86400)
        busy = make_workspace("cleanup-171717171717", task="home-3-0000000000cc")
        age_tree(busy, time.time() - 30 * 86400)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=os.path.join(busy, "home-3-0000000000cc"))
        try:
            time.sleep(0.3)
            cfg = maintenance.validate({"workspaces": {"enabled": True, "older_than_days": 7, "empty_workspaces": True}})
            targets = {(item["dir"], item["task"]) for item in maintenance.workspace_candidates(cfg)}
            self.assertIn(("cleanup-161616161616", "home-1-0000000000aa"), targets)
            self.assertNotIn(("cleanup-161616161616", "home-2-0000000000bb"), targets)  # only 2 days old
            self.assertNotIn(("cleanup-171717171717", "home-3-0000000000cc"), targets)  # in use
            preview = maintenance.step_workspaces(cfg, dry_run=True)
            self.assertTrue(os.path.isdir(os.path.join(old, "home-1-0000000000aa")))
            self.assertIn("cleanup-161616161616/home-1-0000000000aa", preview["items"])
            result = maintenance.step_workspaces(cfg, dry_run=False)
            self.assertTrue(result["ok"], result)
            self.assertFalse(os.path.exists(os.path.join(old, "home-1-0000000000aa")))
            self.assertTrue(os.path.isdir(os.path.join(old, "home-2-0000000000bb")))
            self.assertTrue(os.path.isdir(os.path.join(busy, "home-3-0000000000cc")))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(busy, ignore_errors=True)


class MaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        for name in os.listdir(STATE):
            os.unlink(os.path.join(STATE, name))

    def test_parsers(self) -> None:
        output = "Deleted Images:\nuntagged: x\n\nTotal reclaimed space: 1.5GB\nTotal reclaimed space: 512kB\n"
        self.assertEqual(maintenance.docker_reclaimed(output), 1_500_000_000 + 512_000)
        self.assertEqual(maintenance.docker_reclaimed("Total reclaimed space: 0B"), 0)
        self.assertEqual(maintenance.nix_freed("deleting ...\n1234 store paths deleted, 2.50 GiB freed\n"), (1234, int(2.5 * 1024**3)))
        self.assertEqual(maintenance.nix_freed("0 store paths deleted, 0.00 MiB freed"), (0, 0))

    def test_settings_validation_and_persistence(self) -> None:
        with self.assertRaises(ValueError):
            maintenance.validate({"schedule": {"time": "25:00"}})
        cfg = maintenance.validate(
            {"enabled": True, "schedule": {"mode": "interval", "every_hours": 0}, "docker": {"older_than_hours": -5, "images": "everything"}}
        )
        self.assertEqual(cfg["schedule"]["every_hours"], 1)  # clamped
        self.assertEqual(cfg["docker"]["older_than_hours"], 1)
        self.assertEqual(cfg["docker"]["images"], "dangling")
        saved = maintenance.save_settings(cfg)
        self.assertEqual(maintenance.settings(), saved)
        self.assertAlmostEqual(maintenance.next_run(saved), time.time() + 3600, delta=5)

    def test_daily_schedule(self) -> None:
        cfg = maintenance.validate({"enabled": True, "schedule": {"mode": "daily", "time": "04:00"}})
        maintenance.save_settings(cfg)
        due = maintenance.next_run(cfg)
        local = time.localtime(due)
        self.assertEqual((local.tm_hour, local.tm_min), (4, 0))
        self.assertTrue(time.time() < due <= time.time() + 86400 + 60)
        self.assertIsNone(maintenance.next_run(maintenance.validate({"enabled": False})))

    def test_docker_commands_use_age_filter(self) -> None:
        cfg = maintenance.validate({"docker": {"older_than_hours": 48, "images": "unused", "volumes": True}})
        commands = maintenance.docker_commands(cfg)
        joined = [" ".join(command) for command in commands]
        self.assertIn("docker image prune --force --filter until=48h --all", joined)
        self.assertIn("docker builder prune --all --force --filter until=48h", joined)
        self.assertIn("docker volume prune --force", joined)
        for command in commands:
            if "volume" not in command:
                self.assertIn("until=48h", command)

    def test_job_runs_and_records_history(self) -> None:
        record = maintenance.run(["trash"], dry_run=False, trigger="manual", wait=True)
        self.assertTrue(record["ok"], record)
        self.assertIn("trash", record["steps"])
        self.assertEqual(maintenance.history()[0]["trigger"], "manual")
        self.assertFalse(maintenance.status()["running"])
        with self.assertRaises(ValueError):
            maintenance.run(["bogus"], dry_run=True, trigger="manual")


class AgentsInfoTest(unittest.TestCase):
    def test_claude_usage_from_transcripts(self) -> None:
        base = tempfile.mkdtemp(prefix="webui-claude-")
        project = os.path.join(base, "projects", "-data-workspaces-x")
        os.makedirs(project)
        now = time.time()

        def line(ts: float, msg: str, req: str, out: int, model: str = "claude-opus-5-5") -> str:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))
            usage = {"input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}
            return json.dumps({"type": "assistant", "timestamp": stamp, "sessionId": "s1", "requestId": req,
                               "message": {"id": msg, "model": model, "usage": usage}}) + "\n"

        with open(os.path.join(project, "s1.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            handle.write(line(now - 20 * 3600, "m0", "r0", 5))  # an earlier block
            handle.write(line(now - 600, "m1", "r1", 50))
            handle.write(line(now - 600, "m1", "r1", 50))  # the same response written twice
            handle.write(line(now - 60, "m2", "r2", 70, model="claude-sonnet-5-5"))
            handle.write("not json\n")
        os.environ["WEBUI_CLAUDE_DIRS"] = base
        try:
            usage = agents_info.claude_usage()
            self.assertEqual(usage["windows"]["30d"]["messages"], 3)
            self.assertEqual(usage["windows"]["30d"]["output"], 125)
            self.assertEqual(usage["windows"]["30d"]["total"], 3 * 1110 + 125)
            self.assertIsNotNone(usage["block"])
            self.assertEqual(usage["block"]["messages"], 2)  # the 20 h old one is another block
            self.assertEqual({m["model"] for m in usage["models_7d"]}, {"claude-opus-5-5", "claude-sonnet-5-5"})
            self.assertEqual(len(usage["daily"]), 14)
        finally:
            os.environ.pop("WEBUI_CLAUDE_DIRS", None)
            shutil.rmtree(base, ignore_errors=True)

    def test_login_detection(self) -> None:
        home = tempfile.mkdtemp(prefix="webui-home-")
        saved = {name: os.environ.get(name) for name in ("HOME", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY", "LLM_API_KEY",
                                                          "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
        try:
            for name in saved:
                os.environ.pop(name, None)
            os.environ["HOME"] = home
            self.assertFalse(agents_info._login("codex")["logged_in"])
            os.makedirs(os.path.join(home, ".codex"))
            with open(os.path.join(home, ".codex", "auth.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.assertTrue(agents_info._login("codex")["logged_in"])
            os.makedirs(os.path.join(home, ".claude"))
            with open(os.path.join(home, ".claude", ".credentials.json"), "w", encoding="utf-8") as handle:
                json.dump({"claudeAiOauth": {"accessToken": "secret", "subscriptionType": "max"}}, handle)
            login = agents_info.claude_login()
            self.assertTrue(login["logged_in"])
            self.assertEqual(login["subscription"], "max")
            self.assertNotIn("secret", json.dumps(login))
            names = [item["cli"] for item in agents_info.agents()]
            self.assertEqual(names, ["claude", "codex", "opencode", "pi", "omp"])
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            shutil.rmtree(home, ignore_errors=True)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="webui-http-")
        os.environ["WEBUI_WORKSPACES_ROOT"] = cls.root
        store.invalidate()
        cls.keep = make_workspace("keep-888888888888")
        cls.auth = ("admin", "s3cret")
        cls.server = webui_server.Server(("127.0.0.1", 0), cls.auth, False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.root, ignore_errors=True)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=hdrs)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheaders(), data

    @staticmethod
    def basic(user: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_requires_auth(self) -> None:
        status, headers, _ = self.request("GET", "/api/workspaces")
        self.assertEqual(status, 401)
        self.assertTrue(any(name.lower() == "www-authenticate" for name, _ in headers))
        status, _, _ = self.request("GET", "/api/workspaces", headers=self.basic("admin", "wrong"))
        self.assertEqual(status, 401)

    def test_index_and_assets(self) -> None:
        status, headers, body = self.request("GET", "/", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        self.assertIn(b"Runtime management", body)
        csp = dict((name.lower(), value) for name, value in headers)["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        assets = (
            ("/app.js", b"registerView"),
            ("/workspaces.js", b"runDelete"),
            ("/overview.js", b"sparkline"),
            ("/terminal.js", b"WebSocket"),
            ("/env.js", b"revealValue"),
            ("/maintenance.js", b"mntForm"),
            ("/style.css", b"--accent"),
            ("/favicon.svg", b"<svg"),
        )
        for asset, needle in assets:
            status, _, body = self.request("GET", asset, headers=self.basic("admin", "s3cret"))
            self.assertEqual(status, 200)
            self.assertIn(needle, body)
        status, _, _ = self.request("GET", "/../../etc/passwd", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 404)

    def test_workspaces_endpoint(self) -> None:
        status, _, body = self.request("GET", "/api/workspaces", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["root"], self.root)
        self.assertIn("keep-888888888888", [item["dir"] for item in payload["workspaces"]])
        self.assertIn("totals", payload)
        self.assertFalse(payload["config"]["read_only"])

        status, _, body = self.request("GET", "/api/health", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_delete_needs_csrf_header(self) -> None:
        body = {"items": [{"dir": "keep-888888888888", "confirm": "keep-888888888888"}]}
        status, _, _ = self.request("POST", "/api/workspaces/delete", body, self.basic("admin", "s3cret"))
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/workspaces/delete",
            body,
            {**self.basic("admin", "s3cret"), "X-WebUI": "1", "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertTrue(os.path.isdir(self.keep))

    def test_delete_flow(self) -> None:
        workspace = make_workspace("http-777777777777")
        headers = {**self.basic("admin", "s3cret"), "X-WebUI": "1"}
        # mismatched confirmation is refused server-side
        status, _, body = self.request(
            "POST", "/api/workspaces/delete", {"items": [{"dir": "http-777777777777", "confirm": "nope"}]}, headers
        )
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(workspace))

        status, _, body = self.request(
            "POST",
            "/api/workspaces/delete",
            {"items": [{"dir": "http-777777777777", "confirm": "http-777777777777"}]},
            headers,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted"][0]["dir"], "http-777777777777")
        self.assertFalse(os.path.exists(workspace))

        # second attempt: gone
        status, _, _ = self.request(
            "POST",
            "/api/workspaces/delete",
            {"items": [{"dir": "http-777777777777", "confirm": "http-777777777777"}]},
            headers,
        )
        self.assertIn(status, (207, 404))

    def test_status_endpoints(self) -> None:
        system_stats.sampler().sample()
        status, _, body = self.request("GET", "/api/system", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        for key in ("host", "cgroup", "processes", "network", "filesystems", "history", "top"):
            self.assertIn(key, payload)
        self.assertGreater(payload["processes"]["count"], 0)
        self.assertTrue(payload["history"])
        status, _, body = self.request("GET", "/api/agents", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        for key in ("daemon", "process_view", "tasks", "docker", "terminals"):
            self.assertIn(key, payload)
        self.assertFalse(payload["daemon"]["reachable"])

    def test_env_masking_rules(self) -> None:
        secret = {
            "MULTICA_TOKEN": "mul_abcdef",
            "GH_TOKEN": "ghp_" + "a" * 36,
            "OPENAI_API_KEY": "sk-x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "DB_PASSWORD": "x",
            "WEBUI_AUTH": "admin:pw",
            "WEBUI_AUTH_FILE": "/run/secret",
            "SOMETHING": "ghp_" + "b" * 36,  # token-looking value under a harmless name
        }
        plain = {"PWD": "/home/agent", "PATH": "/usr/bin", "SSH_AUTH_SOCK": "/run/ssh", "GIT_SSH_KEY_PATH": "/k", "EMPTY_TOKEN": ""}
        for name, value in secret.items():
            with self.subTest(name=name):
                is_secret, shown = env_vars.classify(name, value)
                self.assertTrue(is_secret)
                self.assertNotIn(value, shown)
        for name, value in plain.items():
            with self.subTest(name=name):
                self.assertEqual(env_vars.classify(name, value), (False, value))
        is_secret, shown = env_vars.classify("PIP_INDEX_URL", "https://user:hunter2@proxy.lan:8443/simple")
        self.assertTrue(is_secret)
        self.assertNotIn("hunter2", shown)
        self.assertIn("user:", shown)
        self.assertIn("proxy.lan:8443/simple", shown)

    def test_env_view_and_reveal(self) -> None:
        os.environ["DEMO_API_TOKEN"] = "s3cr3t-value"
        os.environ["DEMO_PLAIN"] = "visible"
        os.environ["WEBUI_AUTH"] = "admin:s3cret"  # what the real server runs with
        try:
            auth = self.basic("admin", "s3cret")
            status, _, body = self.request("GET", "/api/env/sources", headers=auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["sources"][0]["id"], "webui")
            status, _, body = self.request("GET", "/api/env?source=webui", headers=auth)
            self.assertEqual(status, 200)
            self.assertNotIn(b"s3cr3t-value", body)
            self.assertNotIn(b"admin:s3cret", body)
            variables = {item["name"]: item for item in json.loads(body)["variables"]}
            self.assertTrue(variables["DEMO_API_TOKEN"]["secret"])
            self.assertEqual(variables["DEMO_PLAIN"]["value"], "visible")

            reveal = {"source": "webui", "name": "DEMO_API_TOKEN"}
            status, _, _ = self.request("POST", "/api/env/reveal", reveal, auth)  # no CSRF header
            self.assertEqual(status, 403)
            headers = {**auth, "X-WebUI": "1"}
            status, _, body = self.request("POST", "/api/env/reveal", reveal, headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["value"], "s3cr3t-value")
            status, _, _ = self.request("POST", "/api/env/reveal", {"source": "webui", "name": "WEBUI_AUTH"}, headers)
            self.assertEqual(status, 403)
            # only the daemon and agent tasks, never an arbitrary process
            status, _, _ = self.request("GET", f"/api/env?source=pid:{os.getpid()}", headers=auth)
            self.assertEqual(status, 404)
            status, _, _ = self.request("POST", "/api/env/reveal", {"source": "pid:1", "name": "PATH"}, headers)
            self.assertEqual(status, 404)
        finally:
            for name in ("DEMO_API_TOKEN", "DEMO_PLAIN", "WEBUI_AUTH"):
                os.environ.pop(name, None)

    def test_maintenance_api(self) -> None:
        auth = self.basic("admin", "s3cret")
        status, _, body = self.request("GET", "/api/maintenance", headers=auth)
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        for key in ("settings", "status", "history", "docker", "nix", "next_run"):
            self.assertIn(key, payload)
        settings = payload["settings"]
        settings["enabled"] = True
        status, _, _ = self.request("POST", "/api/maintenance/settings", {"settings": settings}, auth)
        self.assertEqual(status, 403)  # no CSRF header
        headers = {**auth, "X-WebUI": "1"}
        status, _, body = self.request("POST", "/api/maintenance/settings", {"settings": settings}, headers)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["next_run"])
        status, _, body = self.request("POST", "/api/maintenance/settings", {"settings": {"schedule": {"time": "x"}}}, headers)
        self.assertEqual(status, 400)
        status, _, body = self.request("POST", "/api/maintenance/run", {"steps": ["trash"], "dry_run": True}, headers)
        self.assertEqual(status, 200, body)
        for _ in range(100):
            if not maintenance.status().get("running"):
                break
            time.sleep(0.1)
        self.assertTrue(maintenance.history()[0]["dry_run"])
        maintenance.save_settings({**settings, "enabled": False})
        status, _, body = self.request("GET", "/api/usage", headers=auth)
        self.assertEqual(status, 200, body)
        self.assertIn("windows", json.loads(body))
        status, _, body = self.request("GET", "/api/agents", headers=auth)
        self.assertEqual([item["cli"] for item in json.loads(body)["clis"]], ["claude", "codex", "opencode", "pi", "omp"])

    def test_agent_cli_detection(self) -> None:
        cases = {
            ("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe", "-p"): "claude",
            ("node", "/usr/local/bin/pi", "--mode", "rpc"): "pi",
            ("node", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js", "exec"): "codex",
            ("/usr/local/bin/omp",): "omp",
            ("bwrap", "--die-with-parent", "--", "claude"): None,
            ("node", "/usr/local/bin/playwright-mcp"): None,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(system_stats._agent_cli(list(argv)), expected)

    # --- terminal ------------------------------------------------------------------------------

    def ws_connect(self, origin: str | None, port: int | None = None, auth: bool = True) -> tuple[socket.socket, bytes]:
        port = port or self.port
        sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET /api/terminal?mode=shell&cols=100&rows=30 HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        if auth:
            lines.append("Authorization: " + self.basic("admin", "s3cret")["Authorization"])
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        return sock, head

    @staticmethod
    def ws_send(sock: socket.socket, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(payload)
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length) if length < 126 else struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        sock.sendall(header + mask + masked)

    @staticmethod
    def ws_read_until(sock: socket.socket, needle: bytes, timeout: float = 15) -> bytes:
        data = b""
        deadline = time.time() + timeout
        while needle not in data and time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data

    def test_terminal_requires_same_origin(self) -> None:
        sock, head = self.ws_connect(origin=None)
        sock.close()
        self.assertIn(b" 403 ", head.split(b"\r\n", 1)[0])
        sock, head = self.ws_connect(origin="http://evil.example")
        sock.close()
        self.assertIn(b" 403 ", head.split(b"\r\n", 1)[0])
        sock, head = self.ws_connect(origin=f"http://127.0.0.1:{self.port}", auth=False)
        sock.close()
        self.assertIn(b" 401 ", head.split(b"\r\n", 1)[0])

    @unittest.skipUnless(shutil.which("setsid") and shutil.which("bash"), "needs setsid and bash")
    def test_terminal_session(self) -> None:
        sock, head = self.ws_connect(origin=f"http://127.0.0.1:{self.port}")
        try:
            self.assertIn(b" 101 ", head.split(b"\r\n", 1)[0], head)
            self.ws_send(sock, 0x1, json.dumps({"type": "resize", "cols": 90, "rows": 20}).encode())
            self.ws_send(sock, 0x2, b"echo webui-$((6*7)); stty size\n")
            data = self.ws_read_until(sock, b"20 90")
            self.assertIn(b"webui-42", data)
            self.assertIn(b"20 90", data)
            self.ws_send(sock, 0x2, b"exit\n")
            data = self.ws_read_until(sock, b'"exit"')
            self.assertIn(b'"type": "exit"', data)
        finally:
            sock.close()

    def test_read_only_server(self) -> None:
        server = webui_server.Server(("127.0.0.1", 0), None, True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                "/api/workspaces/delete",
                body=json.dumps({"items": [{"dir": "x", "confirm": "x"}]}),
                headers={"Content-Type": "application/json", "X-WebUI": "1"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request(
                "POST",
                "/api/env/reveal",
                body=json.dumps({"source": "webui", "name": "PATH"}),
                headers={"Content-Type": "application/json", "X-WebUI": "1"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)  # read-only: masked listing only
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            connection.request(
                "POST",
                "/api/maintenance/run",
                body=json.dumps({"steps": ["trash"]}),
                headers={"Content-Type": "application/json", "X-WebUI": "1"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)  # read-only: no cleanup
            sock, head = self.ws_connect(origin=f"http://127.0.0.1:{port}", port=port, auth=False)
            sock.close()
            self.assertIn(b" 403 ", head.split(b"\r\n", 1)[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
