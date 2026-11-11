"""Run setup and check commands under ``/bin/sh -c`` in a worktree (design D7)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .childenv import build_child_env
from .ring import RingBuffer

MAX_CAPTURE = 256 * 1024  # keep at most this many trailing bytes of a command's output


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    duration: float
    output: str
    timed_out: bool = False
    interrupted: str | None = None  # "cancel" or "shutdown"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.interrupted


def tail(text: str, limit: int) -> str:
    """The last ``limit`` bytes of ``text`` (on a character boundary)."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    return "[... output trimmed ...]\n" + raw[-limit:].decode("utf-8", "replace")


def run_command(
    command: str,
    cwd: Path,
    timeout: float,
    *,
    env_passthrough: Iterable[str] = (),
    env_source: dict[str, str] | None = None,
    ring: RingBuffer | None = None,
    interrupted: Callable[[], str | None] = lambda: None,
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> CommandResult:
    """Run ``command`` with the scrubbed environment in its own process group.

    ``interrupted()`` is polled; when it returns ``"cancel"`` or ``"shutdown"`` the process
    group is killed. A timeout kills the group as well.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=cwd,
        env=build_child_env(env_passthrough, env_source),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if on_start:
        on_start(proc)
    chunks: list[bytes] = []
    size = [0]

    def pump() -> None:
        assert proc.stdout
        for raw in proc.stdout:
            chunks.append(raw)
            size[0] += len(raw)
            while size[0] > MAX_CAPTURE and len(chunks) > 1:
                size[0] -= len(chunks.pop(0))
            if ring is not None:
                ring.append(raw.decode("utf-8", "replace").rstrip("\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    stop_reason: str | None = None
    deadline = start + timeout
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        stop_reason = interrupted()
        if stop_reason:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
    if proc.poll() is None:
        _kill_group(proc)
    else:
        # The shell exited; make sure nothing it started is left behind.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    reader.join(timeout=5)
    output = b"".join(chunks).decode("utf-8", "replace")
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        duration=time.monotonic() - start,
        output=output,
        timed_out=timed_out,
        interrupted=stop_reason,
    )


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
