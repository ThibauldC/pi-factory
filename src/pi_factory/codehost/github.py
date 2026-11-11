"""GitHub REST implementation of :class:`CodeHost` (design D5), standard library only."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ..config import RepoConfig
from ..secrets import Secrets
from .base import CodeHostError, PullRequest

API_URL = "https://api.github.com"
_URL_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


def parse_github_url(url: str) -> tuple[str, str]:
    m = _URL_RE.match(url.strip())
    if not m:
        raise CodeHostError(f"not a GitHub repository URL: {url}")
    return m["owner"], m["name"]


class GitHub:
    def __init__(
        self,
        secrets: Secrets,
        *,
        api_url: str = API_URL,
        timeout: float = 30.0,
        mergeable_retry_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        repo_slug: Callable[[RepoConfig], tuple[str, str]] = lambda r: parse_github_url(r.url),
    ):
        self.secrets = secrets
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.mergeable_retry_delay = mergeable_retry_delay
        self._sleep = sleep
        self._slug = repo_slug

    # -- transport -------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.api_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secrets.github_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pi-factory",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                payload: Any = json.loads(text)
            except ValueError:
                payload = {"message": text[:300]}
            if exc.code >= 500 or exc.code == 429:
                raise CodeHostError(
                    f"GitHub HTTP {exc.code} on {method} {path}", "transient"
                ) from exc
            if exc.code in (401, 403, 404):
                raise CodeHostError(
                    f"GitHub HTTP {exc.code} on {method} {path}: "
                    f"{str(payload.get('message'))[:200]}",
                    "access",
                ) from exc
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise CodeHostError(f"GitHub request failed: {exc}", "transient") from exc

    def _ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, payload = self._request(method, path, body)
        if status >= 300:
            message = (payload or {}).get("message") if isinstance(payload, dict) else payload
            raise CodeHostError(f"GitHub HTTP {status} on {method} {path}: {str(message)[:300]}")
        return payload

    def _repo_path(self, repo: RepoConfig) -> str:
        owner, name = self._slug(repo)
        return f"/repos/{owner}/{name}"

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
    def check_access(self, repo: RepoConfig) -> str | None:
        try:
            info = self._ok("GET", self._repo_path(repo))
        except CodeHostError as exc:
            if exc.kind == "access":
                return f"the GitHub bot account lacks access to {repo.url}: {exc}"
            raise
        perms = (info or {}).get("permissions") or {}
        if not (perms.get("push") or perms.get("admin") or perms.get("maintain")):
            return f"the GitHub bot account lacks push access to {repo.url}"
        return None

    def find_open_pr(self, repo: RepoConfig, branch: str) -> PullRequest | None:
        owner, _ = self._slug(repo)
        query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
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
