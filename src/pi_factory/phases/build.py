"""The build phase: implement the approved design, run checks, commit, push, open the PR
(spec build-phase)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import logs
from ..commands import tail
from ..runs import STEPS, RunRecord
from ..tracker.base import IN_REVIEW
from .common import (
    Blocked,
    Deps,
    Task,
    block_issue,
    close_session,
    format_comments,
    human_comments,
    open_session,
    post_comment,
    render_prompt,
    run_logged_command,
    run_turn,
    session_exists,
    set_step,
    strip_marker,
)
from .design import _section_bodies

REPAIR_TAIL_BYTES = 16 * 1024
BLOCK_EXCERPT_BYTES = 3000


# -- inputs --------------------------------------------------------------------------------
def recorded_design(task: Task, rec: RunRecord) -> tuple[str, str]:
    """The design text and the time it was posted. Falls back to the factory's design
    comment in the issue if this host has no local copy."""
    if rec.design_text:
        return rec.design_text, rec.design_comment_at
    designs = [c for c in task.issue.comments if f"pi-factory `{rec.issue}/design-" in c.body]
    if designs:
        latest = designs[-1]
        rec.design_comment_id, rec.design_comment_at = latest.id, latest.created_at
        rec.design_text = strip_marker(latest.body)
        return rec.design_text, latest.created_at
    return "", ""


def build_prompt(deps: Deps, task: Task, rec: RunRecord, continuation: bool) -> str:
    design, design_at = recorded_design(task, rec)
    since = (rec.last_build_comment_at if continuation else None) or design_at
    humans = human_comments(task.issue.comments, rec)
    newer = [c for c in humans if not since or c.created_at > since]
    if design:
        comments_block = (
            "\n## Comments posted after the design (answers and instructions)\n\n"
            f"{format_comments(newer)}\n"
        )
    else:
        comments_block = f"\n## Human comments\n\n{format_comments(humans)}\n"
    continuation_block = ""
    if continuation:
        continuation_block = (
            "\n## This is a continuation\n\n"
            "A pull request already exists for this branch and the work from earlier rounds is "
            "already in the working tree (committed). A reviewer asked for changes: address the "
            "comments above, which were posted since the previous build.\n"
        )
    return render_prompt(
        "build",
        repo=task.repo.key,
        branch=task.issue.branch_name,
        base_branch=task.repo.base_branch,
        identifier=task.issue.identifier,
        title=task.issue.title,
        description=task.issue.description or "(no description)",
        design=design or "(no design was recorded; implement the issue description above)",
        comments_block=comments_block,
        continuation_block=continuation_block,
        check_list="\n".join(f"  {i}. `{c}`" for i, c in enumerate(task.repo.checks, 1)),
    )


# -- PR body -------------------------------------------------------------------------------
def design_summary(design: str, limit: int = 1500) -> str:
    approach = _section_bodies(design).get("Approach") or design
    approach = approach.strip()
    return approach if len(approach) <= limit else approach[:limit].rstrip() + " ..."


def usage_table(rec: RunRecord) -> str:
    rows = [
        "| Phase | Model | Input | Output | Cache read | Cache write |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for key in sorted(rec.usage, key=lambda k: (k.split("-")[0], int(k.split("-")[1]))):
        for model, u in sorted(rec.usage[key].items()):
            rows.append(
                f"| {key} | {model} | {u.get('input', 0):,} | {u.get('output', 0):,} | "
                f"{u.get('cache_read', 0):,} | {u.get('cache_write', 0):,} |"
            )
    return "\n".join(rows) if len(rows) > 2 else "_no usage reported_"


def pr_body(task: Task, rec: RunRecord) -> str:
    design, _ = recorded_design(task, rec)
    attempts = rec.checks.get(str(rec.round), [])
    final = attempts[-1] if attempts else []
    check_rows = ["| Check | Result | Duration |", "|---|---|---:|"]
    for c in final:
        mark = "passed" if c["exit_code"] == 0 else f"failed ({c['exit_code']})"
        check_rows.append(f"| `{c['command']}` | {mark} | {c['duration']:.0f}s |")
    return "\n".join(
        [
            f"Linear issue: [{task.issue.identifier}]({task.issue.url or task.issue.identifier})",
            "",
            "## Design",
            design_summary(design) if design else "_No design was recorded for this issue._",
            "",
            f"## Checks (build round {rec.round})",
            "\n".join(check_rows),
            "",
            f"Repair rounds: {rec.repairs_used}",
            "",
            "## Token usage",
            usage_table(rec),
            "",
            "_Opened by pi-factory. It never merges; a human reviews and merges this PR._",
        ]
    )


# -- steps ---------------------------------------------------------------------------------
def _record_check(deps: Deps, rec: RunRecord, attempt: int, index: int, result) -> dict[str, Any]:
    name = f"build-{rec.round}-attempt-{attempt}-check-{index}.txt"
    deps.runs.write_output(rec.issue, name, f"$ {result.command}\n{result.output}")
    entry = {
        "command": result.command,
        "exit_code": result.exit_code,
        "duration": round(result.duration, 2),
        "timed_out": result.timed_out,
        "output_file": name,
    }
    rec.checks.setdefault(str(rec.round), [])
    while len(rec.checks[str(rec.round)]) <= attempt:
        rec.checks[str(rec.round)].append([])
    rec.checks[str(rec.round)][attempt].append(entry)
    return entry


def run_checks(deps: Deps, task: Task, rec: RunRecord, wt: Path):
    """Run the checks in order, stopping at the first failure. Returns the failing
    :class:`CommandResult` or None when every check passed."""
    attempt = rec.repairs_used
    rec.checks.setdefault(str(rec.round), [])
    if len(rec.checks[str(rec.round)]) > attempt:  # a replayed attempt starts over
        rec.checks[str(rec.round)][attempt] = []
    for index, command in enumerate(task.repo.checks, 1):
        result = run_logged_command(
            deps, task, command, wt, deps.config.build.command_timeout_seconds
        )
        _record_check(deps, rec, attempt, index, result)
        deps.runs.save(rec)
        logs.event(
            "check_result",
            rec.issue,
            command=command,
            exit_code=result.exit_code,
            duration_s=f"{result.duration:.1f}",
            timed_out=result.timed_out or None,
        )
        if not result.ok:
            return result
    return None


def _failure_text(result) -> str:
    if result.timed_out:
        return "timed out"
    return f"exit code {result.exit_code}"


def run_build(deps: Deps, task: Task, rec: RunRecord, *, resume: bool = False) -> None:
    """Run (or, with ``resume``, continue from ``rec.step``) one build round."""
    issue = task.issue
    cfg = deps.config.build
    start = STEPS.index(rec.step) if resume and rec.step in STEPS else 0
    logs.event("phase_start", issue.identifier, phase="build", round=rec.round, resume=resume)

    wt = deps.git.ensure_worktree(task.repo, issue.identifier, issue.branch_name)
    task.check_interrupt()
    existing_pr = deps.codehost.find_open_pr(task.repo, issue.branch_name)
    continuation = existing_pr is not None or rec.pr_number is not None

    session = None
    try:
        # setup ---------------------------------------------------------------------------
        if start <= STEPS.index("setup"):
            set_step(deps, task, rec, "setup")
            if task.repo.setup:
                res = run_logged_command(
                    deps, task, task.repo.setup, wt, cfg.command_timeout_seconds
                )
                deps.runs.write_output(
                    rec.issue, f"build-{rec.round}-setup.txt", f"$ {task.repo.setup}\n{res.output}"
                )
                logs.event("setup_result", rec.issue, exit_code=res.exit_code)
                if not res.ok:
                    raise Blocked(
                        f"the setup command `{task.repo.setup}` failed ({_failure_text(res)})",
                        tail(res.output, BLOCK_EXCERPT_BYTES),
                    )

        # implementer turn (and repair turns) -------------------------------------------------
        if start <= STEPS.index("checks"):
            if start <= STEPS.index("pi_turn"):
                set_step(deps, task, rec, "pi_turn")
                session = open_session(deps, task, rec, tools=None)
                if resume and start == STEPS.index("pi_turn") and session_exists(rec):
                    message = render_prompt("resume")
                else:
                    message = build_prompt(deps, task, rec, continuation)
                run_turn(deps, task, rec, session, message, cfg.timeout_seconds)

            # checks, with bounded repair rounds on the same session ---------------------
            while True:
                set_step(deps, task, rec, "checks")
                failed = run_checks(deps, task, rec, wt)
                if failed is None:
                    break
                if rec.repairs_used >= cfg.repair_rounds:
                    raise Blocked(
                        f"checks still fail after {rec.repairs_used} repair round(s): "
                        f"`{failed.command}` ({_failure_text(failed)})",
                        tail(failed.output, BLOCK_EXCERPT_BYTES),
                    )
                rec.repairs_used += 1
                set_step(deps, task, rec, "pi_turn")
                if session is None:
                    session = open_session(deps, task, rec, tools=None)
                repair = render_prompt(
                    "repair",
                    command=failed.command,
                    result=_failure_text(failed),
                    output=tail(failed.output, REPAIR_TAIL_BYTES),
                )
                logs.event(
                    "repair_round", rec.issue, number=rec.repairs_used, command=failed.command
                )
                run_turn(deps, task, rec, session, repair, cfg.timeout_seconds)
    except Blocked as exc:
        close_session(task, session)
        task.check_interrupt()
        block_issue(deps, issue, rec, exc.reason, exc.details, task)
        return
    except BaseException:
        close_session(task, session)
        raise
    close_session(task, session)

    task.check_interrupt()
    # commit ------------------------------------------------------------------------------
    if start <= STEPS.index("commit"):
        set_step(deps, task, rec, "commit")
        message = f"{issue.identifier}: {issue.title} (round {rec.round})"
        committed = deps.git.commit_all(wt, message)
        logs.event("commit", rec.issue, created=committed, head=deps.git.head(wt)[:10])
        if deps.git.commits_ahead(wt, task.repo) == 0:
            block_issue(
                deps, issue, rec, "the implementer made no changes to the repository", "", task
            )
            return

    # push --------------------------------------------------------------------------------
    task.check_interrupt()
    if start <= STEPS.index("push"):
        set_step(deps, task, rec, "push")
        deps.git.push_branch(task.repo, wt, issue.branch_name)
        logs.event("push", rec.issue, branch=issue.branch_name)

    # pr ----------------------------------------------------------------------------------
    task.check_interrupt()
    set_step(deps, task, rec, "pr")
    title = f"{issue.identifier}: {issue.title}"
    body = pr_body(task, rec)
    deps.runs.write_posted(rec.issue, f"pr-body-round-{rec.round}.md", body)
    pr = deps.codehost.find_open_pr(task.repo, issue.branch_name)
    if pr is None:
        pr = deps.codehost.create_pr(task.repo, issue.branch_name, title, body)
        logs.event("pr_created", rec.issue, number=pr.number, url=pr.url)
    else:
        deps.codehost.update_pr(task.repo, pr.number, body, title)
        logs.event("pr_updated", rec.issue, number=pr.number, url=pr.url)
    rec.pr_number, rec.pr_url = pr.number, pr.url
    task.pr_url = pr.url
    deps.runs.save(rec)

    mergeable = deps.codehost.is_mergeable(task.repo, pr.number)
    lines = [
        f"Pull request ready for review: {pr.url}",
        "",
        f"All {len(task.repo.checks)} checks passed "
        f"({rec.repairs_used} repair round{'s' if rec.repairs_used != 1 else ''}), "
        f"build round {rec.round}.",
    ]
    if mergeable is False:
        lines += [
            "",
            f"**Merge conflict:** GitHub reports that this branch no longer merges cleanly into "
            f"`{task.repo.base_branch}`. The factory never rebases or force-pushes; please "
            "resolve the conflicts yourself.",
        ]
    comment = post_comment(deps, issue, rec, f"build-{rec.round}-in-review", "\n".join(lines))
    rec.last_build_comment_at = comment.created_at
    deps.runs.save(rec)
    task.finishing = True
    deps.tracker.set_state(issue.id, IN_REVIEW)
    rec.outcome = "in_review"
    rec.step = ""
    deps.runs.save(rec)
    logs.event("phase_end", issue.identifier, phase="build", round=rec.round, outcome="in_review")
