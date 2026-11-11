"""The scheduler: poll Linear, reconcile, dispatch, cancel and clean up (design D9)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from . import logs, recovery
from .config import RepoConfig
from .phases.common import Deps, Task, post_comment_once
from .secrets import redact
from .tracker.base import (
    BLOCKED,
    BUILDING,
    DESIGNING,
    READY_FOR_BUILD,
    READY_FOR_DESIGN,
    Issue,
    TrackerError,
)
from .worker import run_task

MAX_BACKOFF = 900.0  # 15 minutes
RETENTION_SWEEP_INTERVAL = 24 * 3600.0
PHASE_STATE = {"design": DESIGNING, "build": BUILDING}
READY_PHASE = {READY_FOR_DESIGN: "design", READY_FOR_BUILD: "build"}


class StartupError(Exception):
    """A prerequisite is missing; the factory must exit with a non-zero status."""


def backoff_delay(interval: float, failures: int) -> float:
    """Delay before the next poll: the normal interval, doubled per consecutive failure,
    capped at 15 minutes."""
    if failures <= 0:
        return interval
    return min(MAX_BACKOFF, max(interval, interval * 2**failures))


class Scheduler:
    def __init__(self, deps: Deps, clock=time.monotonic, wall=time.time):
        self.deps = deps
        self.config = deps.config
        self._clock = clock
        self._wall = wall
        self._lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._finished_at: dict[str, float] = {}
        self._ready_since: dict[str, float] = {}
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.failures = 0
        self.last_poll_at: float | None = None
        self.last_poll_ok: bool | None = None
        self.poll_errors: deque[tuple[float, str]] = deque(maxlen=20)
        self.queue_snapshot: list[dict[str, Any]] = []
        self._last_sweep = clock()
        self._last_issues: list[Issue] = []

    # -- startup ---------------------------------------------------------------------------
    def startup_check(self) -> list[str]:
        """Linear prerequisites: workflow states, repo labels and the owner."""
        return self.deps.tracker.check_setup(list(self.config.repos))

    def require_startup(self) -> None:
        problems = self.startup_check()
        if problems:
            for problem in problems:
                logs.event("startup_problem", problem=problem)
            raise StartupError("; ".join(problems))

    # -- polling ---------------------------------------------------------------------------
    def _tracked_ids(self) -> list[str]:
        ids = []
        for ident in self.deps.runs.list_issues():
            rec = self.deps.runs.load(ident)
            if rec and rec.issue_id:
                ids.append(rec.issue_id)
        return ids

    def next_delay(self) -> float:
        return backoff_delay(self.config.poll_interval_seconds, self.failures)

    def poll_once(self) -> bool:
        """One scheduler tick. Returns False when the poll failed (backoff applies)."""
        started = self._clock()
        self.last_poll_at = self._wall()
        try:
            issues = self.deps.tracker.list_issues(self._tracked_ids())
        except TrackerError as exc:
            self.failures += 1
            self.last_poll_ok = False
            text = redact(str(exc))
            self.poll_errors.append((self._wall(), text))
            logs.event(
                "poll_error",
                error=text,
                consecutive=self.failures,
                retry_in_s=int(self.next_delay()),
            )
            return False
        if self.failures:
            logs.event("poll_recovered", after_failures=self.failures)
        self.failures = 0
        self.last_poll_ok = True
        self._last_issues = issues
        self.reconcile(issues, started)
        self._maybe_sweep()
        return True

    # -- reconcile -------------------------------------------------------------------------
    def reconcile(self, issues: list[Issue], poll_started: float | None = None) -> None:
        poll_started = self._clock() if poll_started is None else poll_started
        by_ident = {i.identifier: i for i in issues}
        self._cancel_departed(by_ident)
        self._cleanup_finished(issues)
        self._recover_orphans(issues, poll_started)
        ready = self._validate_ready(issues)
        self._dispatch(ready)

    def _cancel_departed(self, by_ident: dict[str, Issue]) -> None:
        with self._lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            if not task.active or task.finishing or task.cancel_event.is_set():
                continue
            issue = by_ident.get(task.identifier)
            expected = PHASE_STATE[task.phase]
            if issue is None or issue.state != expected:
                now = issue.state if issue else "missing"
                logs.event("cancel_requested", task.identifier, phase=task.phase, state=now)
                task.cancel()

    def _cleanup_finished(self, issues: list[Issue]) -> None:
        for issue in issues:
            if not issue.is_finished:
                continue
            with self._lock:
                if issue.identifier in self.tasks:
                    continue  # still being stopped; clean up on a later tick
            rec = self.deps.runs.load(issue.identifier)
            if rec is None:
                continue
            repo = self.config.repos.get(rec.repo)
            try:
                if repo is not None:
                    self.deps.git.remove_worktree(repo, issue.identifier, rec.branch)
                self.deps.runs.archive(issue.identifier)
                logs.event("cleaned_up", issue.identifier, state=issue.state)
            except Exception as exc:  # noqa: BLE001
                logs.event("cleanup_failed", issue.identifier, error=redact(str(exc)))

    def _recover_orphans(self, issues: list[Issue], poll_started: float) -> None:
        """Issues in Designing/Building that no worker owns: after a restart, or because a
        worker died. Each is resumed (once per phase) or blocked."""
        for issue in issues:
            if issue.state not in recovery.STATE_PHASE or self.stop_event.is_set():
                continue
            with self._lock:
                if issue.identifier in self.tasks:
                    continue
                if self._finished_at.get(issue.identifier, -1.0) >= poll_started:
                    continue  # the poll predates that worker's final transition
            verdict = recovery.assess(self.deps, issue)
            if verdict.kind == "block":
                self._block_orphan(issue, verdict)
                continue
            rec = verdict.rec
            with self._lock:
                if len(self.tasks) >= self.config.max_concurrent:
                    continue
                if any(t.repo.key == rec.repo for t in self.tasks.values()):
                    continue
            rec.recoveries[recovery.recovery_key(rec)] = (
                rec.recoveries.get(recovery.recovery_key(rec), 0) + 1
            )
            self.deps.runs.save(rec)
            logs.event(
                "recovering", issue.identifier, phase=rec.phase, round=rec.round, step=rec.step
            )
            self.start_task(issue, self.config.repos[rec.repo], rec.phase, resume=True)

    def _block_orphan(self, issue: Issue, verdict: recovery.Assessment) -> None:
        body = (
            f"**Blocked:** {verdict.reason}.\n\n"
            "To retry, move this issue back to Ready for Design or Ready for Build."
        )
        try:
            if verdict.rec is not None:
                from .phases.common import block_issue

                block_issue(self.deps, issue, verdict.rec, verdict.reason)
            else:
                self._block_untracked(issue, "no-run", body)
        except Exception as exc:  # noqa: BLE001
            logs.event("block_failed", issue.identifier, error=redact(str(exc)))

    def _block_untracked(self, issue: Issue, key: str, body: str) -> None:
        post_comment_once(self.deps.tracker, issue, issue.identifier, key, body)
        self.deps.tracker.set_state(issue.id, BLOCKED)
        self.deps.tracker.assign_owner(issue.id)
        logs.event("blocked", issue.identifier, reason=key)

    def _validate_ready(self, issues: list[Issue]) -> list[Issue]:
        """Keep issues with exactly one configured repo label; block the rest."""
        ready_now: set[str] = set()
        valid: list[Issue] = []
        keys = sorted(self.config.repos)
        for issue in issues:
            if issue.state not in READY_PHASE:
                continue
            with self._lock:
                if issue.identifier in self.tasks:
                    continue
            labels = issue.repo_labels()
            if len(labels) != 1 or labels[0] not in self.config.repos:
                self._block_bad_label(issue, labels, keys)
                continue
            ready_now.add(issue.identifier)
            self._ready_since.setdefault(issue.identifier, self._clock())
            valid.append(issue)
        for ident in list(self._ready_since):
            if ident not in ready_now:
                del self._ready_since[ident]
        return valid

    def _block_bad_label(self, issue: Issue, labels: list[str], keys: list[str]) -> None:
        if not labels:
            why = "has no `repo` label"
        elif len(labels) > 1:
            why = f"has more than one `repo` label ({', '.join(labels)})"
        else:
            why = f"has the `repo` label `{labels[0]}`, which is not a configured repository"
        body = (
            f"**Blocked:** this issue {why}. Give it exactly one label from the `repo` group; "
            f"the configured repositories are: {', '.join(f'`{k}`' for k in keys)}.\n\n"
            "Then move it back to Ready for Design or Ready for Build."
        )
        try:
            post_comment_once(
                self.deps.tracker, issue, issue.identifier, f"label-{issue.state}", body
            )
            self.deps.tracker.set_state(issue.id, BLOCKED)
            self.deps.tracker.assign_owner(issue.id)
            logs.event("blocked", issue.identifier, reason="repo label", labels=",".join(labels))
        except TrackerError as exc:
            logs.event("block_failed", issue.identifier, error=redact(str(exc)))

    # -- dispatch --------------------------------------------------------------------------
    def _ordered_candidates(self, ready: list[Issue]) -> list[Issue]:
        """Per repository the best issue (priority, then age); repositories ordered by when
        their oldest waiting issue became ready."""
        by_repo: dict[str, list[Issue]] = {}
        for issue in ready:
            by_repo.setdefault(issue.repo_labels()[0], []).append(issue)
        ordering = []
        for repo, items in by_repo.items():
            items.sort(key=lambda i: (i.priority_rank, i.created_at))
            oldest_ready = min(self._ready_since.get(i.identifier, 0.0) for i in items)
            ordering.append((oldest_ready, repo, items))
        ordering.sort(key=lambda t: (t[0], t[1]))
        self.queue_snapshot = [
            {
                "issue": i.identifier,
                "title": i.title,
                "repo": repo,
                "phase": READY_PHASE[i.state],
                "priority": i.priority,
                "url": i.url,
            }
            for _, repo, items in ordering
            for i in items
        ]
        return [items[0] for _, _, items in ordering]

    def _dispatch(self, ready: list[Issue]) -> None:
        candidates = self._ordered_candidates(ready)
        for issue in candidates:
            if self.stop_event.is_set():
                return
            repo_key = issue.repo_labels()[0]
            with self._lock:
                if len(self.tasks) >= self.config.max_concurrent:
                    return
                if any(t.repo.key == repo_key for t in self.tasks.values()):
                    continue
                if issue.identifier in self.tasks:
                    continue
            self.start_task(issue, self.config.repos[repo_key], READY_PHASE[issue.state])

    def start_task(
        self, issue: Issue, repo: RepoConfig, phase: str, *, resume: bool = False
    ) -> Task:
        task = Task(issue, repo, phase)
        with self._lock:
            self.tasks[issue.identifier] = task
            thread = threading.Thread(
                target=self._run_worker,
                args=(task, resume),
                name=f"worker-{issue.identifier}",
                daemon=True,
            )
            self._threads[issue.identifier] = thread
        self._ready_since.pop(issue.identifier, None)
        logs.event("task_start", issue.identifier, repo=repo.key, phase=phase, resume=resume)
        thread.start()
        return task

    def _run_worker(self, task: Task, resume: bool) -> None:
        outcome = "failed"
        try:
            outcome = run_task(self.deps, task, resume=resume)
        finally:
            with self._lock:
                self.tasks.pop(task.identifier, None)
                self._threads.pop(task.identifier, None)
                self._finished_at[task.identifier] = self._clock()
            logs.event("task_end", task.identifier, phase=task.phase, outcome=outcome)
            if not self.stop_event.is_set():
                self.wake.set()

    # -- housekeeping ----------------------------------------------------------------------
    def _maybe_sweep(self) -> None:
        if self._clock() - self._last_sweep >= RETENTION_SWEEP_INTERVAL:
            self.retention_sweep()

    def retention_sweep(self) -> None:
        self._last_sweep = self._clock()
        removed = self.deps.runs.prune(self.config.retention_days)
        for path in removed:
            logs.event("retention_pruned", path=path.name)

    # -- lifecycle -------------------------------------------------------------------------
    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Wait until no worker thread is running (tests and shutdown)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                threads = list(self._threads.values())
            if not threads:
                return True
            threads[0].join(timeout=0.05)
        return False

    def run_forever(self) -> None:
        """Poll until :meth:`stop` is called."""
        while not self.stop_event.is_set():
            self.wake.clear()
            self.poll_once()
            self.wake.wait(self.next_delay())

    def stop(self, join_timeout: float = 25.0) -> None:
        """Stop taking work; ask running tasks to shut down and leave their records as-is."""
        self.stop_event.set()
        self.wake.set()
        with self._lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            task.request_shutdown()
        self.wait_idle(join_timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = [t.snapshot() for t in self.tasks.values()]
        return {
            "tasks": tasks,
            "queue": list(self.queue_snapshot),
            "last_poll_at": self.last_poll_at,
            "last_poll_ok": self.last_poll_ok,
            "poll_errors": list(self.poll_errors),
            "failures": self.failures,
        }


def install_signal_handlers(scheduler: Scheduler) -> None:
    """SIGTERM/SIGINT: stop taking work and wake the poll loop. The caller then runs
    :meth:`Scheduler.stop`, which closes pi's stdin and leaves run records at their step so
    the next start can resume them."""
    import signal

    def handler(signum: int, frame: object) -> None:
        scheduler.stop_event.set()
        scheduler.wake.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
