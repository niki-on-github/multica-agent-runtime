#!/usr/bin/env python3
# shellcheck shell=python
"""Management web UI of multica-agent-runtime: agent status, resources, workspace cleanup, shell.

Serves one page (static/) and a small JSON API:

    GET  /api/agents              daemon status, running agent tasks, Docker containers
    GET  /api/system              CPU / memory / network history, filesystems, GPUs, top processes
    GET  /api/workspaces          workspaces and their task directories: size, activity, in-use state
    POST /api/workspaces/delete   {"items":[{"dir":..., "task":..., "confirm":...}], "dry_run":bool}
    GET  /api/usage               Claude Code token usage (local transcripts) and login
    GET  /api/maintenance         cleanup settings, status, history, Docker / Nix usage
    POST /api/maintenance/run     {"steps":[...], "dry_run":bool}: start a cleanup job
    POST /api/maintenance/settings {"settings":{...}}: auto cleanup schedule and options
    GET  /api/env/sources         environments that can be shown (webui, daemon, agent tasks)
    GET  /api/env?source=..       variables of one source, secrets masked
    POST /api/env/reveal          {"source":..., "name":...}: one unmasked value
    GET  /api/terminal            WebSocket: browser terminal (?mode=shell|tmux&dir=..&task=..)
    GET  /api/health              liveness plus effective configuration

Runs as an unprivileged agent user and needs nothing but Python's standard library (the terminal
page uses xterm.js from the image's node_modules). It never talks to the Multica server; the daemon
status comes from the daemon's local /health endpoint.

    WEBUI_BIND=0.0.0.0 WEBUI_PORT=8090 WEBUI_AUTH=user:secret multica-webui
"""

from __future__ import annotations

import argparse
import base64
import hmac
import ipaddress
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents_info  # noqa: E402
import env_vars  # noqa: E402
import maintenance  # noqa: E402
import system_stats  # noqa: E402
import terminal  # noqa: E402
import workspace_store as store  # noqa: E402

VERSION = "2.0.0"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/overview.js": ("overview.js", "text/javascript; charset=utf-8"),
    "/workspaces.js": ("workspaces.js", "text/javascript; charset=utf-8"),
    "/terminal.js": ("terminal.js", "text/javascript; charset=utf-8"),
    "/env.js": ("env.js", "text/javascript; charset=utf-8"),
    "/maintenance.js": ("maintenance.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
# xterm.js for the terminal, from node_modules (installed by the Dockerfile)
VENDOR_FILES = {
    "/vendor/xterm.js": ("@xterm/xterm/lib/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("@xterm/xterm/css/xterm.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.js": ("@xterm/addon-fit/lib/addon-fit.js", "text/javascript; charset=utf-8"),
}


def node_modules_dir() -> str:
    candidates = [os.environ.get("WEBUI_XTERM_DIR", ""), "/usr/local/lib/node_modules"]
    candidates += os.environ.get("NODE_PATH", "").split(os.pathsep)
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, VENDOR_FILES["/vendor/xterm.js"][0])):
            return candidate
    return ""


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return "" if value == "CHANGEME" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def credentials() -> tuple[str, str] | None:
    """`WEBUI_AUTH=user:password`, or `WEBUI_AUTH_FILE` with one `user:password` line."""
    raw = _env("WEBUI_AUTH")
    path = _env("WEBUI_AUTH_FILE")
    if not raw and path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            raw = lines[0] if lines else ""
        except (OSError, IndexError):
            raw = ""
    if not raw or ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


class Handler(BaseHTTPRequestHandler):
    server_version = f"multica-webui/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing ------------------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401 - base class signature
        sys.stderr.write("%s [webui] %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        headers = dict(extra or {})
        if not getattr(self, "_body_read", True):
            # an unread request body would be parsed as the next request line on a kept-alive socket
            headers["Connection"] = "close"
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            # xterm.js injects <style> elements; scripts stay 'self' only
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _error(self, status: int, message: str, **extra: Any) -> None:
        payload: dict[str, Any] = {"error": message}
        payload.update(extra)
        self._json(status, payload)

    # --- authentication ------------------------------------------------------------------------

    def _authorized(self) -> bool:
        expected = self.server.auth  # type: ignore[attr-defined]
        if expected is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return False
        user, _, password = decoded.partition(":")
        return hmac.compare_digest(user, expected[0]) and hmac.compare_digest(password, expected[1])

    def _same_origin(self, required: bool = False) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return not required
        host = self.headers.get("Host", "")
        # a reverse proxy may rewrite Host: WEBUI_ALLOWED_ORIGINS lists the public origins
        allowed = {item.strip().rstrip("/") for item in _env("WEBUI_ALLOWED_ORIGINS").split(",") if item.strip()}
        return origin.rstrip("/") in allowed or (bool(host) and origin.split("://", 1)[-1] == host)

    # --- routing -------------------------------------------------------------------------------

    def _unauthorized(self) -> None:
        self._send(
            HTTPStatus.UNAUTHORIZED,
            json.dumps({"error": "authentication required"}).encode(),
            "application/json; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="multica webui", charset="UTF-8"', "Cache-Control": "no-store"},
        )

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._body_read = True
        if not self._authorized():
            self._unauthorized()
            return
        path, _, query = self.path.partition("?")
        if path in STATIC_FILES:
            self._serve_static(os.path.join(STATIC_DIR, STATIC_FILES[path][0]), STATIC_FILES[path][1])
            return
        if path in VENDOR_FILES:
            base = node_modules_dir()
            if not base:
                self._error(HTTPStatus.NOT_FOUND, "xterm.js is not installed (WEBUI_XTERM_DIR)")
                return
            self._serve_static(os.path.join(base, VENDOR_FILES[path][0]), VENDOR_FILES[path][1], cache=True)
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok", "version": VERSION, "config": self._public_config()})
            return
        if path == "/api/terminal":
            self._terminal(query)
            return
        if path in ("/api/usage", "/api/maintenance"):
            try:
                payload = agents_info.claude_usage() if path == "/api/usage" else maintenance.payload()
            except Exception as exc:  # noqa: BLE001
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"collection failed: {exc}")
                return
            if path == "/api/maintenance":
                payload["read_only"] = self.server.read_only  # type: ignore[attr-defined]
            self._json(HTTPStatus.OK, payload)
            return
        if path in ("/api/env", "/api/env/sources"):
            if env_vars.mode() == "off":
                self._error(HTTPStatus.FORBIDDEN, "environment view disabled (WEBUI_ENV=0)")
                return
            try:
                if path == "/api/env/sources":
                    payload = {"sources": env_vars.sources(), "mode": self._env_mode()}
                else:
                    payload = env_vars.listing((urllib.parse.parse_qs(query).get("source") or ["webui"])[0])
                    payload["mode"] = self._env_mode()
            except env_vars.EnvError as exc:
                self._error(exc.status, str(exc))
                return
            self._json(HTTPStatus.OK, payload)
            return
        if path in ("/api/system", "/api/agents"):
            try:
                payload = system_stats.system_payload() if path == "/api/system" else system_stats.agents_payload()
            except Exception as exc:  # noqa: BLE001 - report collection failures instead of dying
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"collection failed: {exc}")
                return
            if path == "/api/agents":
                payload["terminals"] = terminal.sessions_info()
                payload["clis"] = agents_info.agents()
            self._json(HTTPStatus.OK, payload)
            return
        if path == "/api/workspaces":
            try:
                payload = store.snapshot(force="refresh=1" in query.split("&"))
            except Exception as exc:  # noqa: BLE001 - report scan failures instead of dying
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"scan failed: {exc}")
                return
            payload["config"] = self._public_config()
            self._json(HTTPStatus.OK, payload)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._body_read = False
        if not self._authorized():
            self._unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if path not in ("/api/workspaces/delete", "/api/env/reveal", "/api/maintenance/run", "/api/maintenance/settings"):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if path == "/api/env/reveal" and self._env_mode() != "full":
            self._error(HTTPStatus.FORBIDDEN, "revealing values is disabled (WEBUI_ENV / read-only mode)")
            return
        if path != "/api/env/reveal" and self.server.read_only:  # type: ignore[attr-defined]
            self._error(HTTPStatus.FORBIDDEN, "read-only mode: deletion and cleanup are disabled")
            return
        # a cross-site form/fetch cannot set this header without a preflight, and the preflight fails
        # for a foreign origin: this is the CSRF guard, Basic credentials alone are not one
        if self.headers.get("X-WebUI") != "1" or not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "missing or foreign request header")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return
        try:
            raw = self.rfile.read(length)
            self._body_read = True
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return
        if path in ("/api/maintenance/run", "/api/maintenance/settings"):
            body = payload if isinstance(payload, dict) else {}
            try:
                if path == "/api/maintenance/settings":
                    result = {"settings": maintenance.save_settings(body.get("settings")), "next_run": None}
                    result["next_run"] = maintenance.next_run(result["settings"])
                else:
                    steps = body.get("steps")
                    if steps is not None and not isinstance(steps, list):
                        raise ValueError("steps must be a list")
                    result = maintenance.run(
                        [str(step) for step in steps] if steps else None, dry_run=bool(body.get("dry_run")), trigger="manual"
                    )
                    self.log_message("cleanup started: %s%s", ",".join(result.get("steps") or []), " (preview)" if body.get("dry_run") else "")
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except RuntimeError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
                return
            except OSError as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot save settings: {exc}")
                return
            self._json(HTTPStatus.OK, result)
            return
        if path == "/api/env/reveal":
            body = payload if isinstance(payload, dict) else {}
            try:
                result = env_vars.reveal(str(body.get("source") or "webui"), str(body.get("name") or ""))
            except env_vars.EnvError as exc:
                self._error(exc.status, str(exc))
                return
            self.log_message("revealed %s of %s", result["name"], body.get("source") or "webui")
            self._json(HTTPStatus.OK, result)
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        dry_run = bool(payload.get("dry_run")) if isinstance(payload, dict) else False
        try:
            result = store.delete_many(items, dry_run=dry_run)
        except store.DeleteError as exc:
            self._error(exc.status, str(exc), reasons=exc.reasons)
            return
        except Exception as exc:  # noqa: BLE001 - a failed delete must not take the server down
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"delete failed: {exc}")
            return
        if isinstance(items, list) and len(items) == 1 and not result["deleted"]:
            # one request, one verdict: pass the real status instead of a generic 207
            entry = (result["skipped"] or result["failed"])[0]
            self._error(int(entry.get("status") or 400), entry.get("error") or "delete failed", reasons=entry.get("reasons") or [])
            return
        status = HTTPStatus.OK if not result["failed"] else HTTPStatus.MULTI_STATUS
        self._json(status, result)

    # --- helpers -------------------------------------------------------------------------------

    def _env_mode(self) -> str:
        current = env_vars.mode()
        if current == "full" and self.server.read_only:  # type: ignore[attr-defined]
            return "masked"
        return current

    def _public_config(self) -> dict[str, Any]:
        cfg = store.config()
        return {
            "root": cfg["root"],
            "activity_grace_seconds": cfg["activity_grace_seconds"],
            "protect": cfg["protect"],
            "trash": cfg["trash"],
            "keep_trash_hours": cfg["keep_trash_hours"],
            "resolve_names": cfg["resolve_names"],
            "refresh_seconds": _env_int("WEBUI_REFRESH_SECONDS", 15),
            "auth": self.server.auth is not None,  # type: ignore[attr-defined]
            "version": VERSION,
            "read_only": self.server.read_only,  # type: ignore[attr-defined]
            "blind_grace_seconds": cfg["blind_grace_seconds"],
            "shell": self.server.shell_enabled(),  # type: ignore[attr-defined]
            "shell_reason": self.server.shell_reason(),  # type: ignore[attr-defined]
            "xterm": bool(node_modules_dir()),
            "env": self._env_mode(),
            "home": os.environ.get("HOME", ""),
        }

    def _serve_static(self, path: str, content_type: str, cache: bool = False) -> None:
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "missing static file")
            return
        self._send(HTTPStatus.OK, body, content_type, {"Cache-Control": "max-age=3600" if cache else "no-store"})

    def _terminal(self, query: str) -> None:
        """Upgrade to a WebSocket and run a PTY session (see terminal.py)."""
        if not self.server.shell_enabled():  # type: ignore[attr-defined]
            self._error(HTTPStatus.FORBIDDEN, f"terminal disabled: {self.server.shell_reason()}")  # type: ignore[attr-defined]
            return
        # a WebSocket is not subject to CORS: the Origin must be present and ours (CSWSH guard)
        if not self._same_origin(required=True):
            self._error(HTTPStatus.FORBIDDEN, "missing or foreign Origin")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if (
            "websocket" not in self.headers.get("Upgrade", "").lower()
            or "upgrade" not in self.headers.get("Connection", "").lower()
            or self.headers.get("Sec-WebSocket-Version") != "13"
            or not key
        ):
            self._error(HTTPStatus.BAD_REQUEST, "WebSocket upgrade expected")
            return
        params = urllib.parse.parse_qs(query)

        def param(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        mode = "tmux" if param("mode") == "tmux" else "shell"
        cwd = os.environ.get("HOME") or "/"
        if param("dir") and param("task"):
            try:
                task_path = store.find_task_dir(param("dir"), param("task"))
            except store.DeleteError as exc:
                self._error(exc.status, str(exc))
                return
            workdir = os.path.join(task_path, "workdir")
            cwd = workdir if os.path.isdir(workdir) else task_path
        try:
            cols, rows = int(param("cols", "120")), int(param("rows", "32"))
        except ValueError:
            cols, rows = 120, 32
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", terminal.WebSocket.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        terminal.serve(self, mode, cwd, cols, rows, _env_int("WEBUI_SHELL_MAX", 8))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], auth: tuple[str, str] | None, read_only: bool, shell: bool | None = None
    ):
        self.auth = auth
        self.read_only = read_only
        # the terminal is a full shell as the agent: on by default, off with WEBUI_SHELL=0 or read-only
        self.shell = shell if shell is not None else _env("WEBUI_SHELL", "1").strip().lower() not in ("0", "false", "no", "off")
        super().__init__(address, Handler)

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001 - socketserver API
        # a browser closing a kept-alive connection is not an error worth a traceback
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def shell_enabled(self) -> bool:
        return self.shell and not self.read_only

    def shell_reason(self) -> str:
        if self.read_only:
            return "read-only mode (WEBUI_READ_ONLY)"
        if not self.shell:
            return "disabled by WEBUI_SHELL=0"
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="multica-agent-runtime management web UI")
    parser.add_argument("--bind", default=_env("WEBUI_BIND", "0.0.0.0"), help="listen address")
    parser.add_argument("--port", type=int, default=_env_int("WEBUI_PORT", 8090), help="listen port")
    parser.add_argument("--root", default="", help="workspaces root (else WEBUI_WORKSPACES_ROOT)")
    parser.add_argument("--version", action="version", version=f"multica-webui {VERSION}")
    args = parser.parse_args(argv)

    if args.root:
        os.environ["WEBUI_WORKSPACES_ROOT"] = os.path.abspath(args.root)

    auth = credentials()
    if auth is None and not is_loopback(args.bind) and _env("WEBUI_INSECURE") != "1":
        sys.stderr.write(
            "[webui] refusing to listen on %s without authentication.\n"
            "        set WEBUI_AUTH=user:password (or WEBUI_AUTH_FILE), bind WEBUI_BIND=127.0.0.1,\n"
            "        or set WEBUI_INSECURE=1 when an authenticating reverse proxy is in front.\n" % args.bind
        )
        return 2

    read_only = _env("WEBUI_READ_ONLY").strip().lower() in ("1", "true", "yes", "on")
    if read_only:
        sys.stderr.write("[webui] read-only mode: deletion and the terminal are disabled\n")

    root = store.workspaces_root()
    if not os.path.isdir(root):
        sys.stderr.write(f"[webui] warning: workspaces root {root} does not exist yet\n")

    server = Server((args.bind, args.port), auth, read_only)
    system_stats.sampler()  # start collecting resource history right away
    if not read_only:
        maintenance.Scheduler().start()

    def stop(signum, _frame):  # noqa: ANN001 - signal handler signature
        sys.stderr.write(f"[webui] signal {signum}, stopping\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sys.stderr.write(
        f"[webui] multica-agent-runtime web UI {VERSION} on http://{args.bind}:{server.server_address[1]}/ "
        f"(root {root}, auth {'on' if auth else 'off'}, terminal {'on' if server.shell_enabled() else 'off'}"
        f"{', read-only' if read_only else ''}, xterm {node_modules_dir() or 'missing'})\n"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
