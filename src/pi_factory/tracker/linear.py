"""Linear GraphQL implementation of :class:`Tracker` (design D4), standard library only."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

from .. import logs
from ..config import LinearConfig
from ..secrets import Secrets, register_secret
from .base import (
    POLLED_STATES,
    REPO_LABEL_GROUP,
    REQUIRED_STATES,
    Comment,
    Issue,
    Label,
    TrackerError,
)

API_URL = "https://api.linear.app/graphql"
TOKEN_URL = "https://api.linear.app/oauth/token"
TOKEN_REFRESH_MARGIN = 60.0  # refresh this many seconds before the token expires

ISSUE_FIELDS = """
  id identifier title description priority createdAt branchName url
  state { id name type }
  assignee { id }
  labels { nodes { name parent { name } } }
  comments(first: 100) { nodes { id body createdAt user { name } } }
"""

Q_ISSUES = (
    """
query Issues($teamKey: String!, $stateNames: [String!]!, $ids: [ID!]!, $after: String) {
  issues(first: 50, after: $after, filter: {
    team: { key: { eq: $teamKey } },
    or: [ { state: { name: { in: $stateNames } } }, { id: { in: $ids } } ]
  }) {
    pageInfo { hasNextPage endCursor }
    nodes {"""
    + ISSUE_FIELDS
    + """}
  }
}"""
)
Q_ISSUE_STATES = """
query IssueStates($ids: [ID!]!) {
  issues(first: 100, filter: { id: { in: $ids } }) { nodes { id state { name } } }
}"""
Q_COMMENTS = """
query IssueComments($id: String!) {
  issue(id: $id) { comments(first: 100) { nodes { id body createdAt user { name } } } }
}"""
Q_SETUP = """
query Setup($teamKey: String!) {
  teams(filter: { key: { eq: $teamKey } }) {
    nodes { id key states { nodes { id name type } } labels { nodes { name parent { name } } } }
  }
  users(first: 250) { nodes { id name displayName email active } }
}"""
M_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}"""
M_COMMENT = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id body createdAt } }
}"""


def _issue_from_node(node: dict[str, Any]) -> Issue:
    state = node.get("state") or {}
    labels = [
        Label(n["name"], (n.get("parent") or {}).get("name"))
        for n in (node.get("labels") or {}).get("nodes", [])
    ]
    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        title=node.get("title") or "",
        description=node.get("description") or "",
        priority=int(node.get("priority") or 0),
        created_at=node.get("createdAt") or "",
        state=state.get("name", ""),
        state_type=state.get("type", ""),
        branch_name=node.get("branchName") or "",
        url=node.get("url") or "",
        labels=labels,
        comments=_comments(node.get("comments")),
        assignee_id=(node.get("assignee") or {}).get("id"),
    )


def _comments(conn: dict[str, Any] | None) -> list[Comment]:
    out = [
        Comment(
            id=n["id"],
            body=n.get("body") or "",
            created_at=n.get("createdAt") or "",
            author=(n.get("user") or {}).get("name") or "",
        )
        for n in (conn or {}).get("nodes", [])
    ]
    return sorted(out, key=lambda c: c.created_at)


class _AuthError(Exception):
    pass


class LinearTracker:
    def __init__(
        self,
        config: LinearConfig,
        secrets: Secrets,
        *,
        api_url: str = API_URL,
        token_url: str = TOKEN_URL,
        timeout: float = 30.0,
        clock=time.monotonic,
    ):
        self.config = config
        self.secrets = secrets
        self.api_url = api_url
        self.token_url = token_url
        self.timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self._state_ids: dict[str, str] = {}
        self._state_types: dict[str, str] = {}
        self._owner_id: str | None = None

    # -- authentication ---------------------------------------------------------------------
    def _auth_header(self) -> str:
        if self.config.auth == "api_key":
            return self.secrets.linear_api_key()  # personal keys are sent without "Bearer"
        with self._lock:
            if self._token is None or self._clock() >= self._token_expires - TOKEN_REFRESH_MARGIN:
                self._fetch_token()
            return f"Bearer {self._token}"

    def _fetch_token(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.secrets.linear_client_id(),
                "client_secret": self.secrets.linear_client_secret(),
                "scope": "read,write",
            }
        ).encode()
        req = urllib.request.Request(
            self.token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        data = self._send(req)
        token = data.get("access_token")
        if not token:
            raise TrackerError("Linear token response had no access_token")
        register_secret(token)
        self._token = token
        self._token_expires = self._clock() + float(data.get("expires_in") or 3600)
        logs.event("linear_token_refreshed", expires_in=data.get("expires_in"))

    def _invalidate_token(self) -> None:
        with self._lock:
            self._token = None

    # -- transport --------------------------------------------------------------------------
    def _send(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (401, 403):
                raise _AuthError(f"HTTP {exc.code}") from exc
            if exc.code == 400 and "authentication" in text.lower():
                raise _AuthError(f"HTTP {exc.code}") from exc
            raise TrackerError(
                f"Linear HTTP {exc.code}: {text}", transient=exc.code >= 500 or exc.code == 429
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TrackerError(f"Linear request failed: {exc}", transient=True) from exc

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    self.api_url,
                    data=json.dumps({"query": query, "variables": variables}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": self._auth_header(),
                    },
                    method="POST",
                )
                payload = self._send(req)
                errors = payload.get("errors")
                if errors:
                    if any(
                        "authentication" in json.dumps(e.get("extensions", {})).lower()
                        for e in errors
                    ):
                        raise _AuthError("GraphQL authentication error")
                    msg = "; ".join(str(e.get("message")) for e in errors)[:300]
                    raise TrackerError(f"Linear GraphQL error: {msg}")
                return payload.get("data") or {}
            except _AuthError as exc:
                if self.config.auth != "oauth" or attempt == 2:
                    raise TrackerError(f"Linear authentication failed ({exc})") from exc
                logs.event("linear_auth_error_refreshing_token")
                self._invalidate_token()
        raise AssertionError("unreachable")  # pragma: no cover

    # -- setup ------------------------------------------------------------------------------
    def _setup_data(self) -> dict[str, Any]:
        return self._graphql(Q_SETUP, {"teamKey": self.config.team})

    def _resolve_owner(self, users: list[dict[str, Any]]) -> str | None:
        want = self.config.owner.strip().lower()
        for u in users:
            if u.get("active") is False:
                continue
            if want in (
                str(u.get("id", "")).lower(),
                str(u.get("email") or "").lower(),
                str(u.get("displayName") or "").lower(),
                str(u.get("name") or "").lower(),
            ):
                return u["id"]
        return None

    def check_setup(self, repo_keys: Iterable[str]) -> list[str]:
        """Look up and cache state ids and the owner; return a list of problems."""
        data = self._setup_data()
        teams = (data.get("teams") or {}).get("nodes", [])
        if not teams:
            return [
                f"Linear team '{self.config.team}' was not found "
                "(OAuth apps only see public teams; make the team public or use api_key auth)"
            ]
        team = teams[0]
        problems: list[str] = []
        states = (team.get("states") or {}).get("nodes", [])
        by_name = {s["name"]: s for s in states}
        missing = [name for name in REQUIRED_STATES if name not in by_name]
        if missing:
            problems.append("missing Linear workflow states: " + ", ".join(missing))
        if not any(s.get("type") == "completed" for s in states):
            problems.append("the team has no completed (Done) workflow state")
        if not any(s.get("type") == "canceled" for s in states):
            problems.append("the team has no canceled workflow state")
        labels = {
            lb["name"]
            for lb in (team.get("labels") or {}).get("nodes", [])
            if (lb.get("parent") or {}).get("name") == REPO_LABEL_GROUP
        }
        missing_labels = [k for k in repo_keys if k not in labels]
        if missing_labels:
            problems.append(
                f"missing Linear labels in the '{REPO_LABEL_GROUP}' group: "
                + ", ".join(missing_labels)
            )
        owner = self._resolve_owner((data.get("users") or {}).get("nodes", []))
        if owner is None:
            problems.append(f"Linear owner '{self.config.owner}' was not found")
        with self._lock:
            self._state_ids = {s["name"]: s["id"] for s in states}
            self._state_types = {s["name"]: s.get("type", "") for s in states}
            self._owner_id = owner
        return problems

    def _state_id(self, name: str) -> str:
        if name not in self._state_ids:
            self.check_setup(())
        try:
            return self._state_ids[name]
        except KeyError:
            raise TrackerError(f"Linear workflow state '{name}' does not exist") from None

    # -- Tracker protocol -------------------------------------------------------------------
    def list_issues(self, tracked_ids: Iterable[str] = ()) -> list[Issue]:
        variables: dict[str, Any] = {
            "teamKey": self.config.team,
            "stateNames": list(POLLED_STATES),
            "ids": sorted(set(tracked_ids)),
            "after": None,
        }
        issues: list[Issue] = []
        while True:
            conn = self._graphql(Q_ISSUES, variables).get("issues") or {}
            issues.extend(_issue_from_node(n) for n in conn.get("nodes", []))
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return issues
            variables["after"] = page.get("endCursor")

    def get_issue_states(self, issue_ids: Iterable[str]) -> dict[str, str]:
        ids = sorted(set(issue_ids))
        if not ids:
            return {}
        nodes = (self._graphql(Q_ISSUE_STATES, {"ids": ids}).get("issues") or {}).get("nodes", [])
        return {n["id"]: (n.get("state") or {}).get("name", "") for n in nodes}

    def set_state(self, issue_id: str, state: str) -> None:
        self._mutate(M_UPDATE, {"id": issue_id, "input": {"stateId": self._state_id(state)}})

    def assign_owner(self, issue_id: str) -> None:
        if self._owner_id is None:
            self.check_setup(())
        if self._owner_id is None:
            raise TrackerError(f"Linear owner '{self.config.owner}' was not found")
        self._mutate(M_UPDATE, {"id": issue_id, "input": {"assigneeId": self._owner_id}})

    def post_comment(self, issue_id: str, body: str) -> Comment:
        data = self._graphql(M_COMMENT, {"input": {"issueId": issue_id, "body": body}})
        result = data.get("commentCreate") or {}
        node = result.get("comment")
        if not result.get("success") or not node:
            raise TrackerError("Linear did not create the comment")
        return Comment(node["id"], node.get("body", body), node.get("createdAt", ""), "factory")

    def list_comments(self, issue_id: str) -> list[Comment]:
        data = self._graphql(Q_COMMENTS, {"id": issue_id})
        return _comments((data.get("issue") or {}).get("comments"))

    def _mutate(self, query: str, variables: dict[str, Any]) -> None:
        data = self._graphql(query, variables)
        if not (data.get("issueUpdate") or {}).get("success"):
            raise TrackerError("Linear did not apply the issue update")
