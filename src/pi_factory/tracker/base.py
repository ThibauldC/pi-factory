"""The tracker (task board) interface. Linear is the only v1 implementation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ..secrets import RedactedError

READY_FOR_DESIGN = "Ready for Design"
DESIGNING = "Designing"
DESIGN_REVIEW = "Design Review"
READY_FOR_BUILD = "Ready for Build"
BUILDING = "Building"
IN_REVIEW = "In Review"
BLOCKED = "Blocked"

#: States the factory finds by name (the completed and canceled states are found by type).
REQUIRED_STATES = (
    READY_FOR_DESIGN,
    DESIGNING,
    DESIGN_REVIEW,
    READY_FOR_BUILD,
    BUILDING,
    IN_REVIEW,
    BLOCKED,
)
#: States an issue is polled in when the factory is not yet tracking it.
POLLED_STATES = (READY_FOR_DESIGN, DESIGNING, READY_FOR_BUILD, BUILDING)
ACTIVE_STATES = (DESIGNING, BUILDING)
REPO_LABEL_GROUP = "repo"


class TrackerError(RedactedError):
    """A tracker request failed. ``transient`` is true for network and 5xx errors."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class Label:
    name: str
    group: str | None = None


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    created_at: str
    author: str = ""


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    description: str
    priority: int  # Linear: 0 none, 1 urgent, 2 high, 3 medium, 4 low
    created_at: str
    state: str
    state_type: str = ""
    branch_name: str = ""
    url: str = ""
    labels: list[Label] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    assignee_id: str | None = None

    def repo_labels(self) -> list[str]:
        return [lb.name for lb in self.labels if lb.group == REPO_LABEL_GROUP]

    @property
    def is_finished(self) -> bool:
        return self.state_type in ("completed", "canceled")

    @property
    def priority_rank(self) -> int:
        """Sort key: Urgent before High before Medium before Low before no priority."""
        return self.priority if self.priority else 5


class Tracker(Protocol):
    def check_setup(self, repo_keys: Iterable[str]) -> list[str]:
        """Return a list of problems: missing workflow states, repo labels or owner."""

    def list_issues(self, tracked_ids: Iterable[str] = ()) -> list[Issue]:
        """Issues in the polled states plus every issue whose id is in ``tracked_ids``."""

    def get_issue_states(self, issue_ids: Iterable[str]) -> dict[str, str]:
        """Map issue id -> current state name."""

    def set_state(self, issue_id: str, state: str) -> None: ...

    def assign_owner(self, issue_id: str) -> None: ...

    def post_comment(self, issue_id: str, body: str) -> Comment: ...

    def list_comments(self, issue_id: str) -> list[Comment]: ...
