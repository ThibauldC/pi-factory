"""Drive one ``pi --mode rpc`` child process (design D6).

One :class:`PiSession` is one child process: spawn it, send one or more prompts (the first
turn and any repair or resume turns), and close it. A turn finishes at ``agent_settled``;
whether it *succeeded* is decided from pi's structured events, not from the exit code.

Stopping follows the escalation in D6: ``abort`` -> close stdin and SIGTERM the process group
-> SIGKILL, with a grace period at each step.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import logs
from .childenv import build_child_env
from .ring import RingBuffer
from .secrets import RedactedError

_EOF = object()
FAILING_STOP_REASONS = ("error", "aborted", "length")


class PiError(RedactedError):
    """pi could not be started or spoke an unexpected protocol."""


@dataclass
class PiTurnResult:
    ok: bool
    text: str = ""
    # failure kinds: timeout, cancelled, shutdown, aborted, provider_error, length, exited,
    # rejected
    failure: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    session_file: str | None = None
    stderr_tail: list[str] = field(default_factory=list)


def build_command(
    pi_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    session_id: str,
    model: str | None = None,
    thinking: str | None = None,
    tools: Iterable[str] | None = None,
) -> list[str]:
    """The pi command line from design D6."""
    cmd = [
        str(pi_path),
        "--mode",
        "rpc",
        "--no-approve",
        "--session-dir",
        str(session_dir),
        "--session-id",
        session_id,
    ]
    if model:
        cmd += ["--model", model]
    if thinking:
        cmd += ["--thinking", thinking]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    return cmd


def _add_usage(total: dict[str, dict[str, int]], model: str, usage: Mapping[str, Any]) -> None:
    bucket = total.setdefault(
        model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    )
    bucket["input"] += int(usage.get("input") or 0)
    bucket["output"] += int(usage.get("output") or 0)
    bucket["cache_read"] += int(usage.get("cacheRead") or 0)
    bucket["cache_write"] += int(usage.get("cacheWrite") or 0)
    bucket["total"] += int(usage.get("totalTokens") or 0)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _summarize(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


class PiSession:
    """A running ``pi --mode rpc`` child. Not thread-safe except :meth:`cancel` and
    :meth:`request_shutdown`, which may be called from any thread."""

    def __init__(
        self,
        pi_path: str | os.PathLike[str],
        cwd: str | os.PathLike[str],
        session_dir: str | os.PathLike[str],
        session_id: str,
        *,
        model: str | None = None,
        thinking: str | None = None,
        tools: Iterable[str] | None = None,
        env_passthrough: Iterable[str] = (),
        env_source: Mapping[str, str] | None = None,
        ring: RingBuffer | None = None,
        issue: str | None = None,
        abort_grace: float = 10.0,
        term_grace: float = 10.0,
    ):
        self.pi_path = pi_path
        self.cwd = Path(cwd)
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        self.command = build_command(pi_path, session_dir, session_id, model, thinking, tools)
        self.env = build_child_env(env_passthrough, env_source)
        self.ring = ring if ring is not None else RingBuffer()
        self.issue = issue
        self.abort_grace = abort_grace
        self.term_grace = term_grace
        self.session_file: str | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._records: queue.Queue[Any] = queue.Queue()
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._responses: dict[str, dict[str, Any]] = {}
        # per-turn state
        self._settled = False
        self._aborted = False
        self._eof = False
        self._assistant: dict[str, Any] | None = None
        self._usage: dict[str, dict[str, int]] = {}

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.proc = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise PiError(f"cannot start pi at {self.pi_path}: {exc.strerror or exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True, name="pi-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="pi-stderr").start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:  # binary mode: splits on LF only, as the protocol requires
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                self._records.put(json.loads(line))
            except ValueError:
                self.ring.append(f"[pi stdout, not json] {line[:200].decode('utf-8', 'replace')}")
        self._records.put(_EOF)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.ring.append(f"[stderr] {text}")
            self._stderr_tail.append(text)
            del self._stderr_tail[:-50]

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- external controls ------------------------------------------------------------------
    def cancel(self) -> None:
        """Stop the current turn: abort, then terminate. Callable from any thread."""
        self._cancel.set()

    def request_shutdown(self) -> None:
        """Factory is stopping: close stdin only and let recovery resume this turn later."""
        self._shutdown.set()
        self._close_stdin()

    # -- low level --------------------------------------------------------------------------
    def _send(self, obj: dict[str, Any]) -> bool:
        if not self.proc or not self.proc.stdin:
            return False
        try:
            self.proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def _close_stdin(self) -> None:
        if self.proc and self.proc.stdin:
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()

    def _new_id(self, kind: str) -> str:
        self._next_id += 1
        return f"{kind}-{self._next_id}"

    def _handle(self, rec: dict[str, Any]) -> None:
        kind = rec.get("type")
        if kind == "response":
            if rec.get("id"):
                self._responses[str(rec["id"])] = rec
        elif kind == "extension_ui_request":
            self._answer_ui(rec)
        elif kind == "agent_settled":
            self._settled = True
            self._aborted = bool(rec.get("aborted"))
        elif kind == "message_update":
            event = rec.get("assistantMessageEvent") or {}
            if event.get("type") == "text_delta":
                self.ring.write(str(event.get("delta", "")))
        elif kind == "message_end":
            message = rec.get("message") or {}
            if message.get("role") == "assistant":
                self.ring.flush()
                self._assistant = message
                _add_usage(
                    self._usage, str(message.get("model") or "unknown"), message.get("usage") or {}
                )
        elif kind == "tool_execution_start":
            self.ring.flush()
            self.ring.append(f"> {rec.get('toolName')} {_summarize(rec.get('args'))}")
        elif kind == "tool_execution_end":
            status = "error" if rec.get("isError") else "ok"
            self.ring.append(f"< {rec.get('toolName')} {status} {_summarize(rec.get('result'))}")

    def _answer_ui(self, req: dict[str, Any]) -> None:
        """Nothing may block on a missing human: confirm -> false, dialogs -> cancelled."""
        method = req.get("method")
        rid = req.get("id")
        if method == "confirm":
            self._send({"type": "extension_ui_response", "id": rid, "confirmed": False})
        elif method in ("select", "input", "editor"):
            self._send({"type": "extension_ui_response", "id": rid, "cancelled": True})
        elif method == "notify":
            msg = _summarize(req.get("message"))
            self.ring.append(f"[extension {req.get('notifyType', 'info')}] {msg}")
            logs.event("extension_notify", self.issue, message=msg)
        elif method == "setStatus":
            logs.event("extension_status", self.issue, text=_summarize(req.get("statusText")))
        if method in ("confirm", "select", "input", "editor"):
            logs.event("extension_ui_auto_answer", self.issue, method=method)

    def _pump(self, done: Callable[[], bool], deadline: float, *, watch_cancel: bool = True) -> str:
        """Process records until ``done()``. Returns ok | timeout | cancel | shutdown | eof."""
        while not done():
            if self._shutdown.is_set():
                return "shutdown"
            if watch_cancel and self._cancel.is_set():
                return "cancel"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                rec = self._records.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if rec is _EOF:
                self._eof = True
                self._records.put(_EOF)  # keep later waits from blocking forever
                return "shutdown" if self._shutdown.is_set() else "eof"
            self._handle(rec)
        return "ok"

    def _command(self, kind: str, timeout: float = 30.0, **fields: Any) -> dict[str, Any] | None:
        """Send a command and wait for its response. Returns None if there was none."""
        cid = self._new_id(kind)
        if not self._send({"id": cid, "type": kind, **fields}):
            return None
        deadline = time.monotonic() + timeout
        self._pump(lambda: cid in self._responses, deadline, watch_cancel=False)
        return self._responses.pop(cid, None)

    # -- turns ------------------------------------------------------------------------------
    def prompt_and_wait(self, message: str, timeout: float) -> PiTurnResult:
        """Send ``message`` and wait for ``agent_settled``; classify the outcome."""
        if not self.proc:
            raise PiError("pi session was not started")
        self._settled = self._aborted = False
        self._assistant = None
        self._usage = {}
        self.ring.append(f"--- prompt: {_summarize(message, 100)}")
        deadline = time.monotonic() + timeout
        cid = self._new_id("prompt")
        if not self._send({"id": cid, "type": "prompt", "message": message}):
            return self._result_after_exit("exited", "pi was not accepting input")

        def finished() -> bool:
            if self._settled:
                return True
            resp = self._responses.get(cid)
            return bool(
                resp
                and (
                    not resp.get("success")
                    or (resp.get("data") or {}).get("disposition") == "handled"
                )
            )

        status = self._pump(finished, deadline)
        resp = self._responses.pop(cid, None)
        if status == "timeout":
            self._stop_turn()
            return self._result_after_exit("timeout", f"turn exceeded {timeout:g}s")
        if status == "cancel":
            self._stop_turn()
            return self._result_after_exit("cancelled", "turn was cancelled")
        if status == "shutdown":
            return self._result_after_exit("shutdown", "factory is shutting down")
        if status == "eof":
            return self._result_after_exit("exited", "pi exited before the turn finished")
        if resp is not None and not self._settled:
            if not resp.get("success"):
                return PiTurnResult(
                    False, failure="rejected", error=str(resp.get("error") or "prompt rejected")
                )
            return PiTurnResult(
                False, failure="rejected", error="prompt was handled without starting a run"
            )
        return self._finish_turn()

    def _finish_turn(self) -> PiTurnResult:
        assistant = self._assistant or {}
        stop = assistant.get("stopReason")
        text = _message_text(assistant) if assistant else ""
        last = self._command("get_last_assistant_text", timeout=10)
        if last and last.get("success") and (last.get("data") or {}).get("text"):
            text = str(last["data"]["text"])
        state = self._command("get_state", timeout=10)
        if state and state.get("success"):
            self.session_file = (state.get("data") or {}).get("sessionFile") or self.session_file
        result = PiTurnResult(
            True,
            text=text,
            stop_reason=stop,
            usage=self._usage,
            session_file=self.session_file,
        )
        if self._aborted:
            result.ok, result.failure, result.error = False, "aborted", "the turn was aborted"
        elif stop in FAILING_STOP_REASONS:
            kind = {"error": "provider_error", "aborted": "aborted", "length": "length"}[stop]
            detail = assistant.get("errorMessage") or f"stop reason: {stop}"
            result.ok, result.failure, result.error = False, kind, str(detail)
        elif not assistant:
            result.ok, result.failure, result.error = (
                False,
                "provider_error",
                "pi produced no assistant message",
            )
        return result

    def _result_after_exit(self, failure: str, error: str) -> PiTurnResult:
        if self.proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=0.5)
        if failure == "exited" and self._stderr_tail:
            error = f"{error}: {self._stderr_tail[-1]}"
        return PiTurnResult(
            False,
            text=_message_text(self._assistant) if self._assistant else "",
            failure=failure,
            error=error,
            stop_reason=(self._assistant or {}).get("stopReason"),
            usage=self._usage,
            session_file=self.session_file,
            stderr_tail=list(self._stderr_tail[-10:]),
        )

    # -- stopping ---------------------------------------------------------------------------
    def _stop_turn(self) -> None:
        """Escalate: abort -> stdin close + SIGTERM -> SIGKILL (design D6)."""
        logs.event("pi_stopping", self.issue, pid=self.pid)
        if self._send({"type": "abort"}):
            deadline = time.monotonic() + self.abort_grace
            self._pump(lambda: self._settled, deadline, watch_cancel=False)
        self.close()

    def close(self) -> None:
        """Stop the child: close stdin, SIGTERM the group, then SIGKILL. Idempotent."""
        if not self.proc:
            return
        self._close_stdin()
        try:
            self.proc.wait(timeout=self.term_grace if self._settled else 0.2)
            self._reap_group()
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(signal.SIGTERM)
        try:
            self.proc.wait(timeout=self.term_grace)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        self._reap_group()

    def _signal_group(self, sig: int) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, sig)

    def _reap_group(self) -> None:
        """Make sure no process of the child's group survives the child itself."""
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
