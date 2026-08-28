#!/usr/bin/env python3
"""Persistent root compute MCP for one long-lived VPS.

The server deliberately exposes one MCP tool: ``terminal``.  It is not a
DevOps API and it does not create/reset machines.  Every command runs on the
same host as root, so the filesystem, installed software, containers, systemd
services, databases, and deployed projects persist across MCP calls and chats.
"""

from __future__ import annotations

import errno
import fcntl
import hmac
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse, PlainTextResponse


SERVER_VERSION = "2.1.0"


def env_text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_text(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


TOKEN = env_text("MCP_TOKEN")
HOST = env_text("MCP_HOST", "127.0.0.1")
PORT = env_int("MCP_PORT", 8765)
SHELL = env_text("MCP_SHELL", "/bin/bash")
DEFAULT_CWD = env_text("MCP_DEFAULT_CWD", "/root")
MAX_SESSIONS = max(1, env_int("MCP_MAX_SESSIONS", 12))
MAX_WAIT_SECONDS = max(1, env_int("MCP_MAX_WAIT_SECONDS", 30))
MAX_OUTPUT_CHARS = max(1000, env_int("MCP_MAX_OUTPUT_CHARS", 20000))
MAX_BUFFER_BYTES = max(65536, env_int("MCP_MAX_BUFFER_BYTES", 2_000_000))
FINISHED_RETENTION_SECONDS = max(
    60, env_int("MCP_FINISHED_RETENTION_SECONDS", 3600)
)
STATE_DIR = Path(env_text("MCP_STATE_DIR", "/var/lib/mcp-compute"))
LOG_DIR = Path(env_text("MCP_LOG_DIR", "/var/log/mcp-compute"))
AUDIT_LOG = LOG_DIR / "audit.jsonl"
ALLOWED_ORIGINS = {
    value.strip()
    for value in env_text(
        "MCP_ALLOWED_ORIGINS",
        "https://notion.so,https://www.notion.so,https://notion.com,https://www.notion.com,https://app.notion.com",
    ).split(",")
    if value.strip()
}


ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
ANSI_CSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SECRET_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
]
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|bot[_-]?token|secret|password|passwd|pwd)"
    r"\s*[:=]\s*['\"]?([^\s'\"]{6,})"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def audit(action: str, **details: Any) -> None:
    """Record metadata only. Commands and terminal input are never persisted."""
    try:
        ensure_dirs()
        record = {"ts": now_iso(), "action": action, **details}
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        AUDIT_LOG.chmod(0o600)
    except Exception:
        pass


def clean_output(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_CSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    if TOKEN:
        text = text.replace(TOKEN, "***MASKED***")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("***MASKED***", text)

    def replace_assignment(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(2), "***MASKED***")

    return ASSIGNMENT_SECRET_RE.sub(replace_assignment, text)


def child_environment() -> dict[str, str]:
    """Provide a normal root shell without leaking the MCP bearer token."""
    return {
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "SHELL": SHELL,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin",
        "LANG": env_text("LANG", "C.UTF-8"),
        "LC_ALL": env_text("LC_ALL", "C.UTF-8"),
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }


def resolve_cwd(cwd: Optional[str]) -> str:
    raw = cwd or DEFAULT_CWD
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(DEFAULT_CWD) / path
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Working directory is not a directory: {path}")
    return str(path)


@dataclass
class TerminalSession:
    session_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    cwd: str
    mode: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    pending: bytearray = field(default_factory=bytearray)
    dropped_bytes: int = 0
    eof: bool = False

    @property
    def exit_code(self) -> Optional[int]:
        return self.process.poll()


SESSIONS: dict[str, TerminalSession] = {}
SESSIONS_LOCK = threading.RLock()


def set_pty_size(fd: int, columns: int = 140, rows: int = 40) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def append_pending(session: TerminalSession, chunk: bytes) -> None:
    session.pending.extend(chunk)
    overflow = len(session.pending) - MAX_BUFFER_BYTES
    if overflow > 0:
        del session.pending[:overflow]
        session.dropped_bytes += overflow


def drain_session(session: TerminalSession, wait_seconds: float = 0.0) -> None:
    wait_seconds = max(0.0, min(float(wait_seconds), float(MAX_WAIT_SECONDS)))
    deadline = time.monotonic() + wait_seconds

    while not session.eof:
        remaining = max(0.0, deadline - time.monotonic())
        timeout = min(0.10, remaining) if wait_seconds else 0.0
        try:
            ready, _, _ = select.select([session.master_fd], [], [], timeout)
        except (OSError, ValueError):
            session.eof = True
            break

        if ready:
            while True:
                try:
                    chunk = os.read(session.master_fd, 65536)
                    if not chunk:
                        session.eof = True
                        break
                    append_pending(session, chunk)
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        session.eof = True
                        break
                    raise

        exit_code = session.process.poll()
        if exit_code is not None:
            if session.finished_at is None:
                session.finished_at = time.time()
            if not ready:
                break

        if not wait_seconds or time.monotonic() >= deadline:
            break

    # A PTY can report EOF just before waitpid() observes the child's exit.
    # Reap that short race so a completed command is not exposed as running.
    if session.eof and session.process.poll() is None:
        try:
            session.process.wait(timeout=0.20)
        except subprocess.TimeoutExpired:
            pass

    session.last_active = time.time()


def take_output(session: TerminalSession, max_chars: int) -> tuple[str, bool]:
    max_chars = max(1000, min(int(max_chars), MAX_OUTPUT_CHARS))
    take_bytes = min(len(session.pending), max_chars)
    raw = bytes(session.pending[:take_bytes])
    del session.pending[:take_bytes]
    prefix = ""
    if session.dropped_bytes:
        prefix = f"[terminal buffer dropped {session.dropped_bytes} older bytes]\n"
        session.dropped_bytes = 0
    output = prefix + clean_output(raw.decode("utf-8", errors="replace"))
    return output, bool(session.pending)


def public_session(
    session: TerminalSession,
    output: str = "",
    more_output: bool = False,
) -> dict[str, Any]:
    exit_code = session.exit_code
    return {
        "ok": True,
        "session_id": session.session_id,
        "state": "running" if exit_code is None else "exited",
        "exit_code": exit_code,
        "mode": session.mode,
        "cwd": session.cwd,
        "output": output,
        "more_output": more_output,
        "started_at": datetime.fromtimestamp(
            session.created_at, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "hint": (
            "Call terminal(action='read', session_id=...) to continue reading; "
            "use write for interactive input or interrupt for Ctrl-C."
            if exit_code is None
            else "Read again while more_output is true; deployed systemd/Docker services persist independently."
        ),
    }


def cleanup_sessions() -> None:
    now = time.time()
    stale: list[str] = []
    for session_id, session in list(SESSIONS.items()):
        drain_session(session, 0)
        exit_code = session.exit_code
        if exit_code is not None and session.finished_at is None:
            session.finished_at = now
        if (
            exit_code is not None
            and session.finished_at is not None
            and now - session.finished_at > FINISHED_RETENTION_SECONDS
        ):
            stale.append(session_id)

    for session_id in stale:
        session = SESSIONS.pop(session_id, None)
        if session is not None:
            try:
                os.close(session.master_fd)
            except OSError:
                pass


def start_terminal(command: Optional[str], cwd: Optional[str], interactive: bool) -> TerminalSession:
    cleanup_sessions()
    running_count = sum(1 for session in SESSIONS.values() if session.exit_code is None)
    if running_count >= MAX_SESSIONS:
        raise RuntimeError(
            f"Maximum concurrent terminal sessions reached ({MAX_SESSIONS}). "
            "Close or stop an existing session first."
        )

    resolved_cwd = resolve_cwd(cwd)
    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd)
    os.set_blocking(master_fd, False)

    if interactive:
        argv = [SHELL, "-l"]
        mode = "interactive-shell"
    else:
        if not command or not command.strip():
            os.close(master_fd)
            os.close(slave_fd)
            raise ValueError("command is required for action='run'")
        argv = [SHELL, "-lc", command]
        mode = "command"

    try:
        process = subprocess.Popen(
            argv,
            cwd=resolved_cwd,
            env=child_environment(),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)

    session_id = f"term_{uuid.uuid4().hex[:16]}"
    session = TerminalSession(
        session_id=session_id,
        process=process,
        master_fd=master_fd,
        cwd=resolved_cwd,
        mode=mode,
    )
    SESSIONS[session_id] = session
    audit("terminal_start", session_id=session_id, mode=mode, cwd=resolved_cwd, pid=process.pid)
    return session


mcp = FastMCP(
    name="persistent-root-compute",
    instructions=(
        "This MCP is the user's one permanent Debian VPS, equivalent to logging in over SSH as root. "
        "It is not a disposable sandbox and never creates or resets a machine. Use the single terminal "
        "tool for all work: inspect the existing host, install tools, edit code, test, and deploy with "
        "normal Linux commands. Files, packages, containers, databases, and services persist across chats. "
        "Run finished projects through systemd or Docker Compose so they remain active after the terminal "
        "ends and after reboot. Clean only task-specific temporary files and test resources; never perform "
        "a broad host/Docker cleanup that could damage other projects."
    ),
)


@mcp.tool
def terminal(
    action: Literal["run", "open", "read", "write", "interrupt", "close", "list"],
    command: Optional[str] = None,
    session_id: Optional[str] = None,
    input_text: Optional[str] = None,
    cwd: Optional[str] = None,
    wait_seconds: float = 10.0,
    max_output_chars: int = 12000,
    press_enter: bool = False,
) -> dict[str, Any]:
    """Use the root terminal on the one persistent VPS.

    This is the complete computer interface. Do not look for separate file,
    package, Git, Docker, or systemd tools: run their normal Linux commands
    here. The VPS itself and everything installed/deployed on it persist across
    chats. Individual terminal processes are represented by ``session_id``.

    Actions:
    - ``run``: start ``bash -lc <command>`` in a root PTY. It may finish during
      this call or keep running; poll it with ``read``.
    - ``open``: open an interactive root login shell. Use ``write`` and ``read``
      to operate it, preserving cd/export state for that shell.
    - ``read``: wait briefly and return new output from an existing session.
    - ``write``: send ``input_text`` to an interactive command; set
      ``press_enter`` to append a newline.
    - ``interrupt``: send Ctrl-C/SIGINT to the session process group.
    - ``close``: terminate the terminal session. This does not stop properly
      deployed systemd services or Docker containers.
    - ``list``: list recent terminal sessions without exposing their commands.

    ``cwd`` defaults to /root and is unrestricted. This tool is real root
    access, not a path-limited sandbox.
    """
    with SESSIONS_LOCK:
        cleanup_sessions()

        if action == "list":
            sessions = []
            for item in sorted(SESSIONS.values(), key=lambda value: value.created_at, reverse=True):
                sessions.append(
                    {
                        "session_id": item.session_id,
                        "state": "running" if item.exit_code is None else "exited",
                        "exit_code": item.exit_code,
                        "mode": item.mode,
                        "cwd": item.cwd,
                        "started_at": datetime.fromtimestamp(
                            item.created_at, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                    }
                )
            return {"ok": True, "host": os.uname().nodename, "user": "root", "sessions": sessions}

        if action in {"run", "open"}:
            session = start_terminal(command, cwd, interactive=action == "open")
            drain_session(session, wait_seconds)
            output, more = take_output(session, max_output_chars)
            return public_session(session, output, more)

        if not session_id:
            raise ValueError(f"session_id is required for action={action!r}")
        session = SESSIONS.get(session_id)
        if session is None:
            raise ValueError(
                f"Unknown or expired terminal session: {session_id}. "
                "Use action='list' to inspect retained sessions."
            )

        if action == "read":
            drain_session(session, wait_seconds)
            output, more = take_output(session, max_output_chars)
            return public_session(session, output, more)

        if action == "write":
            if session.exit_code is not None:
                drain_session(session, 0)
                output, more = take_output(session, max_output_chars)
                result = public_session(session, output, more)
                result["ok"] = False
                result["error"] = "Terminal process has already exited"
                return result
            if input_text is None:
                raise ValueError("input_text is required for action='write'")
            payload = input_text + ("\n" if press_enter else "")
            os.write(session.master_fd, payload.encode("utf-8"))
            session.last_active = time.time()
            audit("terminal_write", session_id=session_id, bytes=len(payload.encode("utf-8")))
            drain_session(session, wait_seconds)
            output, more = take_output(session, max_output_chars)
            return public_session(session, output, more)

        if action == "interrupt":
            if session.exit_code is None:
                os.killpg(session.process.pid, signal.SIGINT)
                audit("terminal_interrupt", session_id=session_id, pid=session.process.pid)
            drain_session(session, min(wait_seconds, 3.0))
            output, more = take_output(session, max_output_chars)
            return public_session(session, output, more)

        if action == "close":
            if session.exit_code is None:
                try:
                    os.killpg(session.process.pid, signal.SIGTERM)
                    session.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(session.process.pid, signal.SIGKILL)
                    session.process.wait(timeout=2)
                audit("terminal_close", session_id=session_id, pid=session.process.pid)
            drain_session(session, 0)
            output, more = take_output(session, max_output_chars)
            return public_session(session, output, more)

        raise ValueError(f"Unsupported action: {action}")


PUBLIC_PATHS = {"/healthz"}


class SecurityMiddleware:
    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = {key.lower(): value for key, value in (scope.get("headers") or [])}
        origin = headers.get(b"origin", b"").decode("latin-1").strip()
        if origin and origin not in ALLOWED_ORIGINS:
            response = JSONResponse({"error": "origin_not_allowed"}, status_code=403)
            return await response(scope, receive, send)

        provided = headers.get(b"authorization", b"").decode("latin-1").strip()
        if not hmac.compare_digest(provided, self.expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):  # noqa: ANN001
    return PlainTextResponse("ok")


def build_app():
    return SecurityMiddleware(mcp.http_app(path="/mcp"), TOKEN)


def main() -> None:
    if not TOKEN or len(TOKEN) < 24:
        raise SystemExit("MCP_TOKEN is required and must contain at least 24 characters")
    if os.geteuid() != 0:
        raise SystemExit("persistent-root-compute must run as root")
    ensure_dirs()
    audit("server_start", version=SERVER_VERSION, host=HOST, port=PORT)
    uvicorn.run(
        build_app(),
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
