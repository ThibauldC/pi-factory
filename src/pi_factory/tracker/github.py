"""GitHub Issues implementation of :class:`Tracker` (design D4), standard library only.

State is a set of ``factory:*`` labels. A poll sends at most ``repos x 4`` list requests plus
one read per tracked issue that the lists did not already return.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Iterable
from typing import Any

from .. import logs
from ..config import FactoryConfig, RepoConfig
from ..githubapi import GitHubAPIError, GitHubClient
from ..naming import branch_name
from ..secrets import Secrets
from .base import (
    POLLED_STATES,
    PRIORITY_LABELS,
    REQUIRED_STATES,
    TRUSTED_ASSOCIATIONS,
    Comment,
    Issue,
    TrackerError,
    is_state_label,
)


def _tracker_error(exc: GitHubAPIError) -> TrackerError:
    return TrackerError(str(exc), transient=exc.retryable)


class GitHubTracker:
    def __init__(
        self,
        config: FactoryConfig,
        secrets: Secrets,
        *,
        api_url: str | None = None,
        timeout: float = 30.0,
    ):
        self.config = config
        self.client = GitHubClient(secrets, api_url or config.github.api_url, timeout)
        self.warnings: list[str] = []
        self._bot: str | None = None
        self._lock = threading.Lock()

    # -- plumbing --------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            return self.client.ok(method, path, body)
        except GitHubAPIError as exc:
            raise _tracker_error(exc) from exc

    def _repo(self, key: str) -> RepoConfig:
        try:
            return self.config.repos[key]
        except KeyError:
            raise TrackerError(f"repository '{key}' is not configured") from None

    @staticmethod
    def _base(repo: RepoConfig) -> str:
        return f"/repos/{repo.owner}/{repo.name}"

    def _issue_path(self, issue: Issue) -> str:
        return f"{self._base(self._repo(issue.repo))}/issues/{issue.number}"

    def bot_login(self) -> str:
        """The machine account's login, from ``GET /user`` (cached)."""
        with self._lock:
            if self._bot is None:
                node = self._call("GET", "/user") or {}
                login = str(node.get("login") or "")
                if not login:
                    raise TrackerError("GitHub did not report the machine account's login")
                self._bot = login
            return self._bot

    def _issue(self, repo: RepoConfig, node: dict[str, Any]) -> Issue:
        closed = node.get("state") == "closed"
        reason = str(node.get("state_reason") or "")
        if closed and reason not in ("completed", "not_planned"):
            reason = "completed"
        number = int(node["number"])
        title = node.get("title") or ""
        return Issue(
            repo=repo.key,
            number=number,
            title=title,
            description=node.get("body") or "",
            created_at=node.get("created_at") or "",
            labels=[
                (lb.get("name") if isinstance(lb, dict) else str(lb)) or ""
                for lb in node.get("labels") or []
            ],
            closed=closed,
            close_reason=reason if closed else "",
            url=node.get("html_url") or "",
            branch_name=branch_name(number, title),
            assignees=[(a or {}).get("login", "") for a in node.get("assignees") or []],
        )

    def _comment(self, node: dict[str, Any]) -> Comment:
        login = str((node.get("user") or {}).get("login") or "ghost")
        association = str(node.get("author_association") or "NONE")
        is_bot = login.lower() == self.bot_login().lower()
        return Comment(
            id=str(node["id"]),
            body=node.get("body") or "",
            created_at=node.get("created_at") or "",
            author=login,
            association=association,
            is_bot=is_bot,
            trusted=not is_bot and association in TRUSTED_ASSOCIATIONS,
        )

    # -- setup -----------------------------------------------------------------------------
    def check_setup(self, repos: Iterable[RepoConfig]) -> list[str]:
        problems: list[str] = []
        self.warnings = []
        required = {name.lower(): name for name in REQUIRED_STATES}
        owner = self.config.github.owner
        for repo in repos:
            try:
                nodes = self.client.paginate(f"{self._base(repo)}/labels")
            except GitHubAPIError as exc:
                if exc.kind in ("access", "not_found"):
                    problems.append(f"cannot read the labels of {repo.repo}: {exc}")
                    continue
                raise _tracker_error(exc) from exc
            existing = {str(n.get("name", "")).lower() for n in nodes}
            missing = [name for low, name in required.items() if low not in existing]
            if missing:
                problems.append(f"{repo.repo} is missing the labels: " + ", ".join(missing))
            absent = [p for p in PRIORITY_LABELS if p not in existing]
            if absent:
                self.warnings.append(
                    f"{repo.repo} has no priority labels {', '.join(absent)} "
                    "(optional; issues without one are queued last)"
                )
            try:
                self.client.ok("GET", f"{self._base(repo)}/assignees/{urllib.parse.quote(owner)}")
            except GitHubAPIError as exc:
                if exc.kind in ("not_found", "access"):
                    problems.append(f"the owner '{owner}' cannot be assigned in {repo.repo}")
                else:
                    raise _tracker_error(exc) from exc
        return problems

    def setup_warnings(self, repos: Iterable[RepoConfig]) -> list[str]:
        return list(self.warnings)

    # -- reading issues --------------------------------------------------------------------
    def list_issues(self, tracked: Iterable[tuple[str, int]] = ()) -> list[Issue]:
        found: dict[tuple[str, int], Issue] = {}
        try:
            for repo in self.config.repos.values():
                for label in POLLED_STATES:
                    nodes = self.client.paginate(
                        f"{self._base(repo)}/issues",
                        {
                            "state": "open",
                            "labels": label,
                            "sort": "created",
                            "direction": "asc",
                        },
                    )
                    for node in nodes:
                        if "pull_request" in node:
                            continue  # the issues endpoint also lists pull requests
                        issue = self._issue(repo, node)
                        found.setdefault((issue.repo, issue.number), issue)
        except GitHubAPIError as exc:
            raise _tracker_error(exc) from exc
        for key, number in sorted(set(tracked)):
            if (key, number) in found or key not in self.config.repos:
                continue
            issue = self._read_tracked(key, number)
            if issue is not None:
                found[(key, number)] = issue
        return list(found.values())

    def _read_tracked(self, key: str, number: int) -> Issue | None:
        repo = self._repo(key)
        try:
            node = self.client.ok("GET", f"{self._base(repo)}/issues/{number}")
        except GitHubAPIError as exc:
            if exc.kind == "not_found":
                logs.event("tracked_issue_missing", f"{key}#{number}")
                return None
            raise _tracker_error(exc) from exc
        if "pull_request" in node:
            return None
        return self._issue(repo, node)

    def get_issue(self, repo_key: str, number: int) -> Issue:
        repo = self._repo(repo_key)
        node = self._call("GET", f"{self._base(repo)}/issues/{number}")
        return self._issue(repo, node)

    # -- label swaps and assignment --------------------------------------------------------
    def set_state(self, issue: Issue, state: str) -> None:
        path = self._issue_path(issue)
        self._call("POST", f"{path}/labels", {"labels": [state]})  # add the new one first
        stale = [name for name in issue.state_labels if name.lower() != state.lower()]
        for name in stale:
            self._delete_label(path, name)
        issue.labels = [lb for lb in issue.labels if not is_state_label(lb)] + [state]

    def remove_state(self, issue: Issue, state: str) -> None:
        self._delete_label(self._issue_path(issue), state)
        issue.labels = [lb for lb in issue.labels if lb.lower() != state.lower()]

    def _delete_label(self, issue_path: str, name: str) -> None:
        if not is_state_label(name):
            raise TrackerError(f"refusing to remove the label '{name}': not a state label")
        try:
            self.client.ok("DELETE", f"{issue_path}/labels/{urllib.parse.quote(name, safe='')}")
        except GitHubAPIError as exc:
            if exc.kind != "not_found":  # already gone is fine
                raise _tracker_error(exc) from exc

    def assign_owner(self, issue: Issue) -> None:
        owner = self.config.github.owner
        if owner.lower() in {a.lower() for a in issue.assignees}:
            return
        self._call("POST", f"{self._issue_path(issue)}/assignees", {"assignees": [owner]})
        issue.assignees.append(owner)

    # -- comments --------------------------------------------------------------------------
    def post_comment(self, issue: Issue, body: str) -> Comment:
        node = self._call("POST", f"{self._issue_path(issue)}/comments", {"body": body})
        if not isinstance(node, dict) or "id" not in node:
            raise TrackerError("GitHub did not create the comment")
        return self._comment(node)

    def list_comments(self, issue: Issue) -> list[Comment]:
        try:
            nodes = self.client.paginate(f"{self._issue_path(issue)}/comments")
        except GitHubAPIError as exc:
            raise _tracker_error(exc) from exc
        return sorted((self._comment(n) for n in nodes), key=lambda c: c.created_at)

    def list_trusted_comments(self, issue: Issue) -> list[Comment]:
        return [c for c in self.list_comments(issue) if c.trusted]
