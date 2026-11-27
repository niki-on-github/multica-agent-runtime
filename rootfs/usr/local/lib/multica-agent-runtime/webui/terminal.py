# shellcheck shell=python
"""Browser terminal of the management web UI: a WebSocket (RFC 6455) bridged to a PTY.

Standard library only. One WebSocket = one PTY session running web-shell.sh (a login shell, or
`tmux` for the shared tmux session `main`). Protocol:

    client -> server   binary frame: keyboard input (raw bytes)
                       text frame:   {"type": "resize", "cols": 120, "rows": 40}
    server -> client   binary frame: terminal output
                       text frame:   {"type": "exit", "code": 0} before the close frame

The server checks authentication and the Origin header before it upgrades (a WebSocket is not
covered by CORS: without the Origin check any site could open a shell with cached credentials).
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

_sessions: dict[int, "Session"] = {}
_sessions_lock = threading.Lock()


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


def session_pids() -> set[int]:
    """PIDs of the terminal sessions' process trees (excluded from "in use" as tasks)."""
    with _sessions_lock:
        roots = {session.pid for session in _sessions.values() if session.pid}
    if not roots:
        return set()
    parents: dict[int, int] = {}
    try:
        entries = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return roots
    for entry in entries:
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                parents[int(entry)] = int(handle.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    result = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def sessions_info() -> list[dict[str, Any]]:
    with _sessions_lock:
        return [
            {"pid": s.pid, "mode": s.mode, "cwd": s.cwd, "started_at": s.started_at, "client": s.client}
            for s in _sessions.values()
        ]


class WebSocketError(Exception):
    pass


class WebSocket:
    """Minimal server side of RFC 6455 on a connected socket (no extensions, no compression)."""

    def __init__(self, sock: socket.socket, rfile) -> None:  # noqa: ANN001 - buffered reader
        self.sock = sock
        self.rfile = rfile
        self.send_lock = threading.Lock()
        self.closed = False

    @staticmethod
    def accept_key(key: str) -> str:
        return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455

    def _read_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.rfile.read(size - len(data))
            if not chunk:
                raise WebSocketError("connection closed")
            data += chunk
        return data

    def recv(self) -> tuple[int, bytes]:
        """Next complete message (opcode, payload); control frames are returned as they come."""
        message_op = None
        parts: list[bytes] = []
        while True:
            head = self._read_exact(2)
            fin = head[0] & 0x80
            opcode = head[0] & 0x0F
            masked = head[1] & 0x80
            length = head[1] & 0x7F
            if head[0] & 0x70:
                raise WebSocketError("reserved bits set")
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if not masked:
                raise WebSocketError("client frames must be masked")
            if length > MAX_FRAME:
                raise WebSocketError("frame too large")
            mask = self._read_exact(4)
            payload = bytearray(self._read_exact(length))
            for index in range(length):
                payload[index] ^= mask[index & 3]
            if opcode >= 0x8:  # control frame, may interleave a fragmented message
                return opcode, bytes(payload)
            if opcode != OP_CONT:
                message_op = opcode
            elif message_op is None:
                raise WebSocketError("continuation without a message")
            parts.append(bytes(payload))
            if sum(len(part) for part in parts) > MAX_FRAME:
                raise WebSocketError("message too large")
            if fin:
                return message_op, b"".join(parts)

    def send(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, length)
        elif length < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, length)
        with self.send_lock:
            if self.closed:
                return
            self.sock.sendall(header + payload)

    def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            self.send(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120])
        except OSError:
            pass
        with self.send_lock:
            self.closed = True


def _shell_script() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.environ.get("WEBUI_SHELL_SCRIPT", ""), os.path.join(here, "web-shell.sh")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols = max(2, min(int(cols), 1000))
    rows = max(2, min(int(rows), 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Session:
    def __init__(self, mode: str, cwd: str, cols: int, rows: int, client: str) -> None:
        self.mode = mode
        self.cwd = cwd
        self.client = client
        self.started_at = time.time()
        self.master, slave = os.openpty()
        _set_size(slave, cols, rows)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WEBUI_AUTH")}
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "MULTICA_WEBUI_TERMINAL": "1"})
        env.pop("TMUX", None)
        script = _shell_script()
        if script:
            argv = [script, "tmux"] if mode == "tmux" else [script]
        else:
            argv = ["tmux", "new", "-A", "-s", "main"] if mode == "tmux" else ["bash", "-l"]
        # setsid -c: own session with the PTY as controlling terminal (job control, ^C, SIGHUP)
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv
            ["setsid", "-c", *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self.pid = self.proc.pid
        os.set_blocking(self.master, False)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + 5
        while view:
            try:
                written = os.write(self.master, view)
                view = view[written:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return
                select.select([], [self.master], [], 0.2)

    def resize(self, cols: int, rows: int) -> None:
        _set_size(self.master, cols, rows)
        try:
            os.killpg(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def terminate(self) -> int | None:
        for sig, wait in ((signal.SIGHUP, 1.5), (signal.SIGTERM, 1.5), (signal.SIGKILL, 2.0)):
            if self.proc.poll() is not None:
                break
            try:
                os.killpg(self.pid, sig)
            except OSError:
                try:
                    self.proc.send_signal(sig)
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        try:
            os.close(self.master)
        except OSError:
            pass
        return self.proc.poll()


def serve(handler, mode: str, cwd: str, cols: int, rows: int, max_sessions: int) -> None:  # noqa: ANN001
    """Run one terminal session on an already upgraded connection (blocks until it ends)."""
    ws = WebSocket(handler.connection, handler.rfile)
    with _sessions_lock:
        if len(_sessions) >= max_sessions:
            full = True
        else:
            full = False
    if full:
        ws.send(OP_BINARY, f"\r\n\x1b[31mtoo many terminal sessions (WEBUI_SHELL_MAX={max_sessions})\x1b[0m\r\n".encode())
        ws.close(1013, "too many sessions")
        return
    try:
        session = Session(mode, cwd, cols, rows, handler.address_string())
    except OSError as exc:
        ws.send(OP_BINARY, f"\r\n\x1b[31mcannot start the shell: {exc}\x1b[0m\r\n".encode())
        ws.close(1011, "spawn failed")
        return
    with _sessions_lock:
        _sessions[session.pid] = session
    handler.log_message("terminal %s started (pid %d, %s)", mode, session.pid, cwd)
    stop = threading.Event()

    def pump() -> None:
        try:
            while not stop.is_set():
                ready, _, _ = select.select([session.master], [], [], 0.5)
                if not ready:
                    if session.proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(session.master, 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:  # all slave ends closed: the shell is gone
                        break
                    raise
                if not data:
                    break
                ws.send(OP_BINARY, data)
        except OSError:
            pass
        finally:
            stop.set()
            code = session.proc.poll()
            try:
                ws.send(OP_TEXT, json.dumps({"type": "exit", "code": code}).encode())
            except OSError:
                pass
            ws.close(1000, "shell exited")
            try:
                handler.connection.shutdown(socket.SHUT_RD)  # wake the reader below
            except OSError:
                pass

    reader = threading.Thread(target=pump, name=f"webui-pty-{session.pid}", daemon=True)
    reader.start()
    try:
        while not stop.is_set():
            opcode, payload = ws.recv()
            if opcode == OP_BINARY:
                session.write(payload)
            elif opcode == OP_TEXT:
                try:
                    message = json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("type") == "resize":
                    try:
                        session.resize(int(message.get("cols", 80)), int(message.get("rows", 24)))
                    except (TypeError, ValueError, OSError):
                        pass
                elif isinstance(message, dict) and message.get("type") == "input":
                    session.write(str(message.get("data", "")).encode("utf-8"))
            elif opcode == OP_PING:
                ws.send(OP_PONG, payload)
            elif opcode == OP_CLOSE:
                break
    except (WebSocketError, OSError):
        pass
    finally:
        stop.set()
        code = session.terminate()
        reader.join(timeout=3)
        ws.close()
        with _sessions_lock:
            _sessions.pop(session.pid, None)
        handler.log_message("terminal %s ended (pid %d, exit %s)", mode, session.pid, code)
