"""GitHub REST implementation of :class:`CodeHost` (design D5), standard library only."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from ..config import DEFAULT_API_URL, RepoConfig
from ..githubapi import GitHubAPIError, GitHubClient
from ..secrets import Secrets
from .base import CodeHostError, PullRequest


def _code_host_error(exc: GitHubAPIError) -> CodeHostError:
    if exc.retryable:
        return CodeHostError(str(exc), "transient")
    if exc.kind in ("access", "not_found"):
        return CodeHostError(str(exc), "access")
    return CodeHostError(str(exc))


class GitHub:
    def __init__(
        self,
        secrets: Secrets,
        *,
        api_url: str = DEFAULT_API_URL,
        timeout: float = 30.0,
        mergeable_retry_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = GitHubClient(secrets, api_url, timeout)
        self.mergeable_retry_delay = mergeable_retry_delay
        self._sleep = sleep

    # -- transport -------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        try:
            return self.client.request(method, path, body)
        except GitHubAPIError as exc:
            raise _code_host_error(exc) from exc

    def _ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            return self.client.ok(method, path, body)
        except GitHubAPIError as exc:
            raise _code_host_error(exc) from exc

    @staticmethod
    def _repo_path(repo: RepoConfig) -> str:
        return f"/repos/{repo.owner}/{repo.name}"

    @staticmethod
    def _pr(node: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=int(node["number"]),
            url=node.get("html_url", ""),
            head=(node.get("head") or {}).get("ref", ""),
            base=(node.get("base") or {}).get("ref", ""),
            title=node.get("title", ""),
        )

    # -- CodeHost protocol -----------------------------------------------------------------
    def _repo_info(self, repo: RepoConfig) -> dict[str, Any]:
        return self._ok("GET", self._repo_path(repo)) or {}

    def check_access(self, repo: RepoConfig) -> str | None:
        try:
            info = self._repo_info(repo)
        except CodeHostError as exc:
            if exc.kind == "access":
                return f"the GitHub bot account lacks access to {repo.repo}: {exc}"
            raise
        perms = info.get("permissions") or {}
        if not (perms.get("push") or perms.get("admin") or perms.get("maintain")):
            return f"the GitHub bot account lacks push access to {repo.repo}"
        if info.get("has_issues") is False:
            return f"issues are disabled in {repo.repo}"
        return None

    def default_branch(self, repo: RepoConfig) -> str:
        return str(self._repo_info(repo).get("default_branch") or "")

    def find_open_pr(self, repo: RepoConfig, branch: str) -> PullRequest | None:
        query = urllib.parse.urlencode({"head": f"{repo.owner}:{branch}", "state": "open"})
        nodes = self._ok("GET", f"{self._repo_path(repo)}/pulls?{query}") or []
        return self._pr(nodes[0]) if nodes else None

    def create_pr(self, repo: RepoConfig, branch: str, title: str, body: str) -> PullRequest:
        existing = self.find_open_pr(repo, branch)
        if existing:
            return existing
        status, payload = self._request(
            "POST",
            f"{self._repo_path(repo)}/pulls",
            {"title": title, "head": branch, "base": repo.base_branch, "body": body},
        )
        if status == 422:  # e.g. another attempt created it in the meantime
            existing = self.find_open_pr(repo, branch)
            if existing:
                return existing
        if status >= 300:
            raise CodeHostError(f"GitHub HTTP {status} creating PR: {str(payload)[:300]}")
        return self._pr(payload)

    def update_pr(self, repo: RepoConfig, number: int, body: str, title: str | None = None) -> None:
        fields: dict[str, Any] = {"body": body}
        if title is not None:
            fields["title"] = title
        self._ok("PATCH", f"{self._repo_path(repo)}/pulls/{number}", fields)

    def is_mergeable(self, repo: RepoConfig, number: int) -> bool | None:
        """GitHub computes ``mergeable`` lazily: retry once when it is still null."""
        for attempt in (1, 2):
            node = self._ok("GET", f"{self._repo_path(repo)}/pulls/{number}")
            value = node.get("mergeable")
            if value is not None or attempt == 2:
                return value
            self._sleep(self.mergeable_retry_delay)
        return None  # pragma: no cover
