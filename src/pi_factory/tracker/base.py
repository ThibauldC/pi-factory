"""The tracker (task board) interface. GitHub Issues is the only v1 implementation.

The state machine lives in ``factory:*`` labels, one per issue. Done and Canceled are not
labels: a closed issue is finished (``state_reason`` ``completed`` is Done, ``not_planned``
is Canceled).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ..config import RepoConfig
from ..naming import issue_key, issue_ref
from ..secrets import RedactedError

READY_FOR_DESIGN = "factory:ready-for-design"
DESIGNING = "factory:designing"
DESIGN_REVIEW = "factory:design-review"
READY_FOR_BUILD = "factory:ready-for-build"
BUILDING = "factory:building"
IN_REVIEW = "factory:in-review"
BLOCKED = "factory:blocked"

STATE_PREFIX = "factory:"
#: Labels every configured repository must provide. The factory never creates them.
REQUIRED_STATES = (
    READY_FOR_DESIGN,
    DESIGNING,
    DESIGN_REVIEW,
    READY_FOR_BUILD,
    BUILDING,
    IN_REVIEW,
    BLOCKED,
)
#: Labels polled for in every repository.
POLLED_STATES = (READY_FOR_DESIGN, READY_FOR_BUILD, DESIGNING, BUILDING)
ACTIVE_STATES = (DESIGNING, BUILDING)

#: Highest priority first. An issue with none of these ranks below Low.
PRIORITY_LABELS = ("priority:urgent", "priority:high", "priority:medium", "priority:low")
NO_PRIORITY_RANK = len(PRIORITY_LABELS) + 1

#: Comment authors whose text may reach an agent prompt (besides not being the bot).
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def is_state_label(name: str) -> bool:
    return name.lower() in {s.lower() for s in REQUIRED_STATES}


def priority_rank(labels: Iterable[str]) -> int:
    """1 (urgent) to 4 (low); 5 when no priority label is present. The highest wins."""
    lowered = {name.lower() for name in labels}
    for rank, label in enumerate(PRIORITY_LABELS, 1):
        if label in lowered:
            return rank
    return NO_PRIORITY_RANK


class TrackerError(RedactedError):
    """A tracker request failed. ``transient`` is true for network, 5xx and rate-limit errors."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    created_at: str
    author: str = ""
    association: str = ""
    is_bot: bool = False  # written by the machine account
    trusted: bool = False  # not the bot, and OWNER, MEMBER or COLLABORATOR


@dataclass
class Issue:
    repo: str  # the repository key from the configuration
    number: int
    title: str
    description: str
    created_at: str
    labels: list[str] = field(default_factory=list)
    closed: bool = False
    close_reason: str = ""  # "completed" or "not_planned" once closed
    url: str = ""
    branch_name: str = ""
    assignees: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)

    @property
    def ref(self) -> str:
        """``pi-relay#123``, used in logs, comments and on the dashboard."""
        return issue_ref(self.repo, self.number)

    @property
    def key(self) -> str:
        """``pi-relay-123``, used for run directories, session IDs and comment markers."""
        return issue_key(self.repo, self.number)

    @property
    def state_labels(self) -> list[str]:
        return [name for name in self.labels if is_state_label(name)]

    @property
    def state(self) -> str:
        """The single ``factory:*`` state label, or ``""`` when there is none or several."""
        labels = self.state_labels
        return labels[0].lower() if len(labels) == 1 else ""

    @property
    def conflicting(self) -> bool:
        return len(self.state_labels) > 1

    @property
    def priority_rank(self) -> int:
        return priority_rank(self.labels)

    @property
    def priority_label(self) -> str:
        rank = self.priority_rank
        return PRIORITY_LABELS[rank - 1] if rank < NO_PRIORITY_RANK else ""

    @property
    def is_finished(self) -> bool:
        return self.closed


class Tracker(Protocol):
    def check_setup(self, repos: Iterable[RepoConfig]) -> list[str]:
        """Problems that stop the factory: missing ``factory:*`` labels, an owner who cannot
        be assigned, repositories without issues."""

    def setup_warnings(self, repos: Iterable[RepoConfig]) -> list[str]:
        """Things worth fixing that do not stop the factory (missing priority labels)."""

    def list_issues(self, tracked: Iterable[tuple[str, int]] = ()) -> list[Issue]:
        """Open issues carrying a polled state label in every configured repository (pull
        requests excluded), plus one read of each tracked ``(repo key, number)``."""

    def get_issue(self, repo_key: str, number: int) -> Issue: ...

    def set_state(self, issue: Issue, state: str) -> None:
        """Add ``state``, then remove the issue's other ``factory:*`` labels. Other labels
        are never touched. The issue object is updated to match."""

    def remove_state(self, issue: Issue, state: str) -> None:
        """Remove one ``factory:*`` label (used to repair an interrupted swap)."""

    def assign_owner(self, issue: Issue) -> None: ...

    def post_comment(self, issue: Issue, body: str) -> Comment: ...

    def list_comments(self, issue: Issue) -> list[Comment]:
        """Every comment, oldest first, flagged as bot or trusted."""

    def list_trusted_comments(self, issue: Issue) -> list[Comment]:
        """Only comments an agent prompt may contain."""
