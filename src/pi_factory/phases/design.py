"""The design phase: a read-only pi session posts a design for human review (spec design-phase)."""

from __future__ import annotations

import re

from .. import logs
from ..tracker.base import DESIGN_REVIEW
from .common import (
    Blocked,
    Deps,
    Task,
    block_issue,
    close_session,
    format_comments,
    human_comments,
    marker,
    open_session,
    post_comment,
    render_prompt,
    run_turn,
    session_exists,
    set_step,
    strip_marker,
    swap_state,
)

SECTIONS = ("Approach", "Files likely touched", "Acceptance checks", "Open questions")


def _section_bodies(text: str) -> dict[str, str]:
    """Split Markdown into sections keyed by canonical section name. A heading is a line that,
    without ``#``/bold markers and a trailing colon, is exactly a section name."""
    names = {s.lower(): s for s in SECTIONS}
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        bare = re.sub(r"^\s*#{1,6}\s*", "", line).strip().strip("*_ ").rstrip(":").strip("*_ ")
        if bare.lower() in names:
            current = names[bare.lower()]
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return {k: "\n".join(v).strip() for k, v in found.items()}


def validate_design(text: str) -> list[str]:
    """Return a list of format problems (empty when the design is well formed)."""
    problems: list[str] = []
    bodies = _section_bodies(text)
    for section in SECTIONS:
        if section not in bodies:
            problems.append(f"missing section: {section}")
        elif not bodies[section]:
            problems.append(f"section is empty: {section}")
    questions = bodies.get("Open questions")
    if questions and not re.match(r"^\W*(none|no open questions|n/a)\b", questions, re.I):
        items = re.split(r"(?m)^\s*(?=\d+[.)]\s)", questions)
        items = [i for i in items if re.match(r"\d+[.)]\s", i.lstrip())]
        if not items:
            problems.append("open questions must be numbered (1., 2., ...) or say None")
        for item in items:
            if "recommend" not in item.lower():
                number = re.match(r"(\d+)", item.lstrip()).group(1)
                problems.append(f"open question {number} has no recommended answer")
    return problems


def ensure_heading(text: str, round_: int) -> str:
    """The posted comment must start with a heading that names the round."""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if re.match(rf"^\s*#*\s*\**\s*Design \(round {round_}\)", first, re.I):
        return text.strip()
    return f"# Design (round {round_})\n\n{text.strip()}"


def build_prompt(deps: Deps, task: Task, rec) -> str:
    comments = human_comments(task.issue.comments, rec)
    previous = ""
    if rec.design_comment_id and rec.design_text:
        after = [c for c in comments if c.created_at > rec.design_comment_at]
        before = [c for c in comments if c.created_at <= rec.design_comment_at]
        previous = (
            "\n## Your previous design (it was reviewed and sent back)\n\n"
            f"{rec.design_text.strip()}\n\n"
            "## Feedback to address (comments posted after that design)\n\n"
            f"{format_comments(after)}\n"
        )
        comments = before
    return render_prompt(
        "design",
        repo=task.repo.key,
        base_branch=task.repo.base_branch,
        identifier=task.issue.ref,
        title=task.issue.title,
        description=task.issue.description or "(no description)",
        comments=format_comments(comments),
        previous_design_block=previous,
        round=str(rec.round),
        check_list="\n".join(f"  - `{c}`" for c in task.repo.checks),
    )


def _require_clean_worktree(deps: Deps, wt) -> None:
    if deps.git.status(wt):
        deps.git.discard_changes(wt)
        raise Blocked("the design round modified files in the worktree (changes were discarded)")


def run_design(deps: Deps, task: Task, rec, *, resume: bool = False) -> None:
    """Run (or, with ``resume``, continue) one design round for ``task``."""
    issue = task.issue
    logs.event("phase_start", issue.ref, phase="design", round=rec.round, resume=resume)
    task.check_interrupt()
    if resume and _adopt_posted_design(deps, task, rec):
        _finish(deps, task, rec)
        return
    wt = deps.git.ensure_worktree(task.repo, task.worktree_name, issue.branch_name)

    set_step(deps, task, rec, "pi_turn")
    session = open_session(deps, task, rec, tools=deps.config.design.tools)
    blocked: Blocked | None = None
    text = ""
    try:
        timeout = deps.config.design.timeout_seconds
        if resume and session_exists(rec):
            message = render_prompt("resume_design", round=str(rec.round))
        else:
            message = build_prompt(deps, task, rec)
        result = run_turn(deps, task, rec, session, message, timeout)
        _require_clean_worktree(deps, wt)
        text = result.text
        problems = validate_design(text)
        if problems:
            logs.event("design_format_fix", issue.ref, problems="; ".join(problems))
            fix = render_prompt(
                "format_fix",
                problems="\n".join(f"- {p}" for p in problems),
                round=str(rec.round),
            )
            result = run_turn(deps, task, rec, session, fix, timeout)
            _require_clean_worktree(deps, wt)
            text = result.text
            problems = validate_design(text)
            if problems:
                raise Blocked(
                    "the design is still malformed after one correction: " + "; ".join(problems),
                    text,
                )
    except Blocked as exc:
        blocked = exc
    finally:
        close_session(task, session)

    if blocked is not None:
        task.check_interrupt()
        block_issue(deps, issue, rec, blocked.reason, blocked.details, task)
        return

    task.check_interrupt()
    design = ensure_heading(text, rec.round)
    comment = post_comment(deps, issue, rec, f"design-{rec.round}", design)
    _record_design(deps, rec, comment.id, comment.created_at, design)
    _finish(deps, task, rec)


def _record_design(deps: Deps, rec, comment_id: str, created_at: str, body: str) -> None:
    rec.design_comment_id = comment_id
    rec.design_comment_at = created_at
    rec.design_text = body
    deps.runs.save(rec)


def _adopt_posted_design(deps: Deps, task: Task, rec) -> bool:
    """Recovery: the design comment for this round was already posted before the restart."""
    tag = marker(rec.issue, f"design-{rec.round}")
    for c in deps.tracker.list_comments(task.issue):
        if c.is_bot and tag in c.body:
            _record_design(deps, rec, c.id, c.created_at, strip_marker(c.body))
            if c.id not in rec.factory_comment_ids:
                rec.factory_comment_ids.append(c.id)
            return True
    return False


def _finish(deps: Deps, task: Task, rec) -> None:
    issue = task.issue
    task.finishing = True
    swap_state(deps, rec, issue, DESIGN_REVIEW)
    deps.tracker.assign_owner(issue)
    rec.outcome = "designed"
    rec.step = ""
    deps.runs.save(rec)
    logs.event("phase_end", issue.ref, phase="design", round=rec.round, outcome="designed")
