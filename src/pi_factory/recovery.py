"""Startup reconciliation: decide what to do with issues left in Designing/Building
(spec run-recovery). The scheduler calls :func:`assess` for every such issue that no worker
owns, both at startup (before any ready issue is started) and when a worker died."""

from __future__ import annotations

from dataclasses import dataclass

from .phases.common import Deps, session_exists
from .runs import RunRecord
from .tracker.base import BUILDING, DESIGNING, Issue

STATE_PHASE = {DESIGNING: "design", BUILDING: "build"}


@dataclass
class Assessment:
    kind: str  # "resume" or "block"
    reason: str = ""
    rec: RunRecord | None = None


def recovery_key(rec: RunRecord) -> str:
    return f"{rec.phase}-{rec.round}"


def assess(deps: Deps, issue: Issue) -> Assessment:
    rec = deps.runs.load(issue.identifier)
    if rec is None:
        return Assessment(
            "block",
            f"{issue.state} but no local run was found on this host "
            "(no run record exists, so there is nothing to resume)",
        )
    phase = STATE_PHASE[issue.state]
    if rec.phase != phase or rec.outcome:
        return Assessment(
            "block",
            f"the issue is in {issue.state}, but the last local run was a {rec.phase} run that "
            f"{'ended as ' + rec.outcome if rec.outcome else 'does not match'}; "
            "nothing is interrupted, so there is nothing to resume",
            rec,
        )
    if rec.recoveries.get(recovery_key(rec), 0) >= 1:
        return Assessment(
            "block",
            f"the {phase} phase (round {rec.round}) was interrupted again after it had already "
            "been resumed once; recovery was already used for this phase",
            rec,
        )
    if rec.step == "pi_turn" and not session_exists(rec):
        return Assessment(
            "block",
            "the pi session file for the interrupted turn is missing or unreadable "
            f"(session {rec.session_ids.get(f'{phase}-{rec.round}', '?')} in {rec.session_dir})",
            rec,
        )
    if rec.repo not in deps.config.repos:
        return Assessment("block", f"repository `{rec.repo}` is no longer configured", rec)
    return Assessment("resume", rec=rec)
