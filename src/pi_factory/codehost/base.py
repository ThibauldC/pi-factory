"""The code host interface. GitHub is the only v1 implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import RepoConfig
from ..secrets import RedactedError


class CodeHostError(RedactedError):
    """A code host request failed. ``kind`` is ``access``, ``transient`` or ``other``."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head: str
    base: str
    title: str = ""


class CodeHost(Protocol):
    def check_access(self, repo: RepoConfig) -> str | None:
        """Return None when the bot can push to the repository, else a problem description."""

    def default_branch(self, repo: RepoConfig) -> str:
        """The repository's default branch (closing keywords only fire when merged there)."""

    def find_open_pr(self, repo: RepoConfig, branch: str) -> PullRequest | None: ...

    def create_pr(self, repo: RepoConfig, branch: str, title: str, body: str) -> PullRequest:
        """Create a PR for ``branch``; if one already exists for it, return that one."""

    def update_pr(
        self, repo: RepoConfig, number: int, body: str, title: str | None = None
    ) -> None: ...

    def is_mergeable(self, repo: RepoConfig, number: int) -> bool | None:
        """True/False once the host knows; None if it is still undecided."""
