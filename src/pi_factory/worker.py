"""Run one task (one issue in one phase) and turn every outcome into the right GitHub effect."""

from __future__ import annotations

import time

from . import logs
from .codehost.base import CodeHostError
from .phases.build import run_build
from .phases.common import (
    Blocked,
    Cancelled,
    Deps,
    ShuttingDown,
    Task,
    begin_phase,
    block_issue,
)
from .phases.design import run_design
from .runs import RunRecord
from .secrets import redact

BLOCK_RETRIES = 3
BLOCK_RETRY_DELAY = 5.0


def preflight(deps: Deps, task: Task) -> None:
    """Checks that must pass before a worktree is created: the bot can reach the repository,
    and there is enough free disk."""
    problem = deps.codehost.check_access(task.repo)
    if problem:
        raise Blocked(problem)
    wt = deps.git.worktree_path(task.repo, task.worktree_name)
    if not (wt / ".git").exists():
        root = deps.config.worktree_root
        root.mkdir(parents=True, exist_ok=True)
        free = deps.disk_free(root)
        need = deps.config.min_free_disk_bytes
        if free < need:
            raise Blocked(
                f"not enough free disk space to create a worktree "
                f"({free / 1024**3:.1f} GiB free, {need / 1024**3:.1f} GiB required)"
            )


def _try_block(
    deps: Deps, task: Task, rec: RunRecord | None, reason: str, details: str = ""
) -> None:
    """Block the issue, retrying a few times: GitHub may be having the same bad moment."""
    if rec is None:
        return
    for attempt in range(1, BLOCK_RETRIES + 1):
        try:
            block_issue(deps, task.issue, rec, reason, details, task)
            return
        except Exception as exc:  # noqa: BLE001 - nothing more we can do but log and retry
            logs.event("block_failed", task.ref, attempt=attempt, error=redact(str(exc)))
            if attempt < BLOCK_RETRIES:
                time.sleep(BLOCK_RETRY_DELAY)


def run_task(deps: Deps, task: Task, *, resume: bool = False) -> str:
    """Run the task to its end. Returns ``done``, ``blocked``, ``stopped`` or ``failed``."""
    rec: RunRecord | None = None
    try:
        rec = begin_phase(deps, task, resume=resume)
        preflight(deps, task)
        if task.phase == "design":
            run_design(deps, task, rec, resume=resume)
        else:
            run_build(deps, task, rec, resume=resume)
        return "blocked" if rec.outcome == "blocked" else "done"
    except Cancelled:
        logs.event("task_canceled", task.ref, phase=task.phase)
        return "stopped"
    except ShuttingDown:
        logs.event("task_interrupted_by_shutdown", task.ref, phase=task.phase)
        return "stopped"
    except Blocked as exc:
        if task.interrupted():
            return "stopped"
        _try_block(deps, task, rec, exc.reason, exc.details)
        return "blocked"
    except CodeHostError as exc:
        if task.interrupted():
            return "stopped"
        reason = f"the GitHub bot account could not access {task.repo.repo}: {exc}"
        if exc.kind != "access":
            reason = f"GitHub request failed: {exc}"
        _try_block(deps, task, rec, reason)
        return "blocked"
    except Exception as exc:  # noqa: BLE001
        if task.interrupted():
            return "stopped"
        logs.event("task_failed", task.ref, error=redact(f"{type(exc).__name__}: {exc}"))
        _try_block(deps, task, rec, f"internal error: {type(exc).__name__}: {redact(str(exc))}")
        return "failed"
