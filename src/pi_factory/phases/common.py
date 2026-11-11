"""Shared building blocks of the design and build phases."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from .. import logs
from ..codehost.base import CodeHost
from ..commands import CommandResult, run_command
from ..config import FactoryConfig, RepoConfig
from ..gitops import GitOps
from ..pi_rpc import PiSession, PiTurnResult
from ..ring import RingBuffer
from ..runs import RunRecord, RunStore
from ..tracker.base import (
    BLOCKED,
    BUILDING,
    DESIGNING,
    READY_FOR_BUILD,
    READY_FOR_DESIGN,
    Comment,
    Issue,
    Tracker,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
COMMENT_LOOKBACK = 40


@dataclass
class Deps:
    """Everything a phase needs from the outside world."""

    config: FactoryConfig
    tracker: Tracker
    codehost: CodeHost
    git: GitOps
    runs: RunStore
    disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free


class TaskStop(Exception):
    """The task must stop without posting anything."""


class Cancelled(TaskStop):
    """A human moved the issue out of the active state."""


class ShuttingDown(TaskStop):
    """The factory is stopping; leave the run record at its current step."""


class Blocked(Exception):
    """The phase gave up: move the issue to Blocked and explain why."""

    def __init__(self, reason: str, details: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass
class Task:
    """A running unit of work: one issue in one phase. Shared with the scheduler and the
    dashboard, so mutable bits sit behind a lock."""

    issue: Issue
    repo: RepoConfig
    phase: str
    round: int = 0
    step: str = ""
    pr_url: str = ""
    active: bool = False  # the Designing/Building state is in place
    finishing: bool = False  # set just before the final state transition
    started_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    shutdown_event: threading.Event = field(default_factory=threading.Event)
    agent_ring: RingBuffer = field(default_factory=lambda: RingBuffer(200))
    check_ring: RingBuffer = field(default_factory=lambda: RingBuffer(200))
    _session: PiSession | None = None
    _proc: subprocess.Popen[bytes] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def key(self) -> str:
        """The issue key (``pi-relay-123``): run directory, scheduler slot, session IDs."""
        return self.issue.key

    @property
    def ref(self) -> str:
        """The issue reference (``pi-relay#123``) for logs and the dashboard."""
        return self.issue.ref

    @property
    def worktree_name(self) -> str:
        return str(self.issue.number)

    # -- control from other threads -----------------------------------------------------
    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            session, proc = self._session, self._proc
        if session:
            session.cancel()
        if proc:
            _signal_group(proc)

    def request_shutdown(self) -> None:
        self.shutdown_event.set()
        with self._lock:
            session = self._session
        if session:
            session.request_shutdown()

    def interrupted(self) -> str | None:
        if self.shutdown_event.is_set():
            return "shutdown"
        if self.cancel_event.is_set():
            return "cancel"
        return None

    def check_interrupt(self) -> None:
        why = self.interrupted()
        if why == "shutdown":
            raise ShuttingDown()
        if why == "cancel":
            raise Cancelled()

    def attach_session(self, session: PiSession | None) -> None:
        with self._lock:
            self._session = session

    def attach_proc(self, proc: subprocess.Popen[bytes] | None) -> None:
        with self._lock:
            self._proc = proc

    def snapshot(self) -> dict[str, Any]:
        return {
            "issue": self.ref,
            "key": self.key,
            "title": self.issue.title,
            "url": self.issue.url,
            "repo": self.repo.key,
            "phase": self.phase,
            "round": self.round,
            "step": self.step,
            "pr_url": self.pr_url,
            "started_at": self.started_at,
        }


def _signal_group(proc: subprocess.Popen[bytes]) -> None:
    import os
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)


# -- prompts ---------------------------------------------------------------------------------
def render_prompt(name: str, **values: str) -> str:
    template = Template((PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8"))
    return template.safe_substitute(**values).strip() + "\n"


def format_comments(comments: list[Comment]) -> str:
    if not comments:
        return "(none)"
    return "\n\n".join(
        f"--- {c.author or 'someone'} wrote at {c.created_at} ---\n{c.body.strip()}"
        for c in comments
    )


# -- factory comments --------------------------------------------------------------------------
def marker(issue_key: str, key: str) -> str:
    return f"pi-factory `{issue_key}/{key}`"


def strip_marker(body: str) -> str:
    """The comment text without its trailing ``— pi-factory `KEY/step``` marker line."""
    lines = body.rstrip().splitlines()
    if lines and lines[-1].startswith("— pi-factory `"):
        lines.pop()
    return "\n".join(lines).rstrip()


def is_factory_comment(comment: Comment, rec: RunRecord) -> bool:
    return (
        comment.is_bot
        or comment.id in rec.factory_comment_ids
        or f"pi-factory `{rec.issue}/" in comment.body
    )


def human_comments(comments: list[Comment], rec: RunRecord) -> list[Comment]:
    """Comments the factory did not write and that come from a trusted author (the issue's
    owner, a member or a collaborator): the only text it reads, and only as context."""
    return [c for c in comments if c.trusted and not is_factory_comment(c, rec)]


def post_comment_once(tracker: Tracker, issue: Issue, key: str, body: str) -> Comment:
    """Post ``body`` once. A marker line (run + step) lets a replay find its earlier post."""
    tag = marker(issue.key, key)
    for existing in tracker.list_comments(issue)[-COMMENT_LOOKBACK:]:
        if existing.is_bot and tag in existing.body:
            return existing
    return tracker.post_comment(issue, f"{body.rstrip()}\n\n— {tag}")


def post_comment(deps: Deps, issue: Issue, rec: RunRecord, key: str, body: str) -> Comment:
    """Post a run comment once and keep its id and text in the run record."""
    comment = post_comment_once(deps.tracker, issue, key, body)
    if comment.id not in rec.factory_comment_ids:
        rec.factory_comment_ids.append(comment.id)
        deps.runs.save(rec)
        deps.runs.write_posted(rec.issue, f"{key}.md", comment.body)
        logs.event("comment_posted", rec.ref, key=key, comment=comment.id)
    return comment


def swap_state(deps: Deps, rec: RunRecord, issue: Issue, new: str) -> None:
    """Move the issue to ``new``: the intended swap is written to the run record first, so a
    crash between adding the new label and removing the old one can be repaired."""
    old = issue.state_labels
    rec.swap = {"from": old[0].lower(), "to": new} if len(old) == 1 else None
    deps.runs.save(rec)
    deps.tracker.set_state(issue, new)
    rec.swap = None
    deps.runs.save(rec)


def block_issue(
    deps: Deps,
    issue: Issue,
    rec: RunRecord,
    reason: str,
    details: str = "",
    task: Task | None = None,
) -> None:
    """Move to Blocked, assign the owner, and explain how to retry."""
    retry = READY_FOR_DESIGN if rec.phase == "design" else READY_FOR_BUILD
    body = f"**Blocked ({rec.phase} phase, round {rec.round}):** {reason}"
    if details.strip():
        body += f"\n\n```\n{details.strip()}\n```"
    body += (
        f"\n\nTo retry, replace the `factory:blocked` label with `{retry}` "
        f"(or with `{READY_FOR_DESIGN}` to redesign)."
    )
    post_comment(deps, issue, rec, f"blocked-{rec.phase}-{rec.round}", body)
    if task is not None:
        task.finishing = True
    swap_state(deps, rec, issue, BLOCKED)
    deps.tracker.assign_owner(issue)
    rec.outcome = "blocked"
    rec.outcome_detail = reason[:500]
    deps.runs.save(rec)
    logs.event("blocked", rec.ref, phase=rec.phase, round=rec.round, reason=reason[:200])


# -- pi sessions ---------------------------------------------------------------------------------
def session_id_for(issue: str, phase: str, round_: int) -> str:
    return f"{issue}-{phase}-{round_}"


def open_session(
    deps: Deps,
    task: Task,
    rec: RunRecord,
    *,
    tools: tuple[str, ...] | None,
    **kw: Any,
) -> PiSession:
    """Start the phase's pi process. The session id and directory are recorded first."""
    sid = session_id_for(rec.issue, rec.phase, rec.round)
    sdir = deps.runs.sessions_dir(rec.issue)
    rec.session_ids[f"{rec.phase}-{rec.round}"] = sid
    rec.session_dir = str(sdir)
    deps.runs.save(rec)
    settings = deps.config.phase_settings(task.repo.key, rec.phase)
    session = PiSession(
        deps.config.pi_path,
        deps.git.worktree_path(task.repo, task.worktree_name),
        sdir,
        sid,
        model=settings.model,
        thinking=settings.thinking,
        tools=tools,
        env_passthrough=task.repo.env_passthrough,
        ring=task.agent_ring,
        issue=rec.ref,
        **kw,
    )
    task.attach_session(session)
    session.start()
    logs.event("pi_started", rec.ref, pid=session.pid, session=sid, phase=rec.phase)
    return session


def close_session(task: Task, session: PiSession | None) -> None:
    if session is not None:
        session.close()
    task.attach_session(None)


def session_exists(rec: RunRecord) -> bool:
    """Is there a pi session file on disk for the record's current phase and round?"""
    sid = rec.session_ids.get(f"{rec.phase}-{rec.round}")
    if not sid or not rec.session_dir:
        return False
    return any(Path(rec.session_dir).glob(f"*_{sid}.jsonl"))


def add_usage(rec: RunRecord, key: str, usage: dict[str, dict[str, int]]) -> None:
    bucket = rec.usage.setdefault(key, {})
    for model, counts in usage.items():
        into = bucket.setdefault(model, {k: 0 for k in counts})
        for name, value in counts.items():
            into[name] = into.get(name, 0) + value


def run_turn(
    deps: Deps,
    task: Task,
    rec: RunRecord,
    session: PiSession,
    message: str,
    timeout: float,
) -> PiTurnResult:
    """One prompt turn. Success returns the result; every failure becomes an exception the
    worker understands (stop, shut down, or Blocked)."""
    task.check_interrupt()
    result = session.prompt_and_wait(message, timeout)
    add_usage(rec, f"{rec.phase}-{rec.round}", result.usage)
    if result.session_file:
        rec.session_file = result.session_file
    deps.runs.save(rec)
    logs.event(
        "pi_turn_end",
        rec.ref,
        phase=rec.phase,
        ok=result.ok,
        failure=result.failure,
        stop_reason=result.stop_reason,
    )
    if result.ok:
        return result
    if result.failure == "cancelled":
        raise Cancelled()
    if result.failure == "shutdown":
        raise ShuttingDown()
    if result.failure == "timeout":
        minutes = timeout / 60
        raise Blocked(
            f"the {rec.phase} timeout ({minutes:g} minutes) was reached; pi was stopped",
            result.error or "",
        )
    detail = result.error or ""
    if result.stderr_tail and result.failure == "exited":
        detail += "\n" + "\n".join(result.stderr_tail[-5:])
    raise Blocked(f"pi failed ({result.failure}): {result.error or 'unknown error'}", detail)


def run_logged_command(
    deps: Deps,
    task: Task,
    command: str,
    cwd: Path,
    timeout: float,
) -> CommandResult:
    task.check_interrupt()
    result = run_command(
        command,
        cwd,
        timeout,
        env_passthrough=task.repo.env_passthrough,
        ring=task.check_ring,
        interrupted=task.interrupted,
        on_start=task.attach_proc,
    )
    task.attach_proc(None)
    task.check_interrupt()
    return result


def set_step(deps: Deps, task: Task, rec: RunRecord, step: str) -> None:
    task.step = step
    deps.runs.set_step(rec, step)


def begin_phase(deps: Deps, task: Task, *, resume: bool = False) -> RunRecord:
    """Open (or, when recovering, reload) the run record, read the issue's comments, and for
    a fresh phase entry move the issue to Designing/Building *before* any pi process is
    launched."""
    issue = task.issue
    rec = deps.runs.load(issue.key)
    if rec is None:
        rec = deps.runs.create(issue.key, task.repo.key)
    rec.repo = task.repo.key
    rec.issue_number = issue.number
    if rec.branch:
        issue.branch_name = rec.branch  # the branch name stays stable if the title changes
    else:
        rec.branch = issue.branch_name
    rec.title = issue.title
    rec.issue_url = issue.url
    issue.comments = deps.tracker.list_comments(issue)  # comments are read when a phase starts
    if not resume:
        rec.phase = task.phase
        rec.rounds[task.phase] = rec.rounds.get(task.phase, 0) + 1
        rec.round = rec.rounds[task.phase]
        rec.step = ""
        rec.outcome = ""
        rec.outcome_detail = ""
        rec.swap = None
        rec.repairs_used = 0
        deps.runs.save(rec)
        swap_state(deps, rec, issue, DESIGNING if task.phase == "design" else BUILDING)
        logs.event("state_change", issue.ref, to=task.phase)
    else:
        deps.runs.save(rec)
    task.round = rec.round
    task.step = rec.step
    task.active = True
    return rec
