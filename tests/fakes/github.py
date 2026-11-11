"""An in-process fake GitHub REST API: repositories, issues (labels, comments with
``author_association``, assignees, closure), pull requests, and rate limiting.

Issues and pull requests share one number sequence per repository, as on GitHub, and pull
requests also show up in the issues list. Test helpers address a repository by its key in the
factory configuration (``owner/<key>`` on the fake).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
PREFIX = "factory:"
STATE_LABELS = [
    f"{PREFIX}{s}"
    for s in (
        "ready-for-design",
        "designing",
        "design-review",
        "ready-for-build",
        "building",
        "in-review",
        "blocked",
    )
]
PRIORITY_LABELS = ["priority:urgent", "priority:high", "priority:medium", "priority:low"]


class FakeGitHub:
    def __init__(
        self,
        token: str = "ghp_bottoken",
        repos: dict[str, bool] | None = None,
        bot_login: str = "factory-bot",
        owner: str = "olivia",
    ):
        self.token = token
        self.bot_login = bot_login
        self.owner = owner
        # "owner/name" -> bot has push permission. Repos not listed answer 404 (not shared).
        self.repos: dict[str, bool] = repos if repos is not None else {"owner/pi-relay": True}
        self.default_branches: dict[str, str] = {}
        self.has_issues: dict[str, bool] = {}
        self.labels: dict[str, list[str]] = {}
        self.assignable: set[str] = {owner}
        self.issues: dict[tuple[str, int], dict[str, Any]] = {}
        self.prs: dict[tuple[str, int], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.transitions: list[tuple[int, str]] = []  # (issue number, factory label added by API)
        self.mergeable: list[bool | None] = []  # answers for GET pulls/N, consumed in order
        self.default_mergeable: bool | None = True
        self.fail_next: list[int] = []
        self.rate_limit_next: list[dict[str, Any]] = []
        self._seq = 0
        self._clock = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else None
                code, payload, headers = fake._handle(
                    method, self.path, self.headers.get("Authorization", ""), body
                )
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = lambda self: self._serve("GET")  # noqa: E731
            do_POST = lambda self: self._serve("POST")  # noqa: E731
            do_PATCH = lambda self: self._serve("PATCH")  # noqa: E731
            do_DELETE = lambda self: self._serve("DELETE")  # noqa: E731

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> FakeGitHub:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    # -- test helpers ----------------------------------------------------------------------
    @staticmethod
    def slug(repo: str) -> str:
        return repo if "/" in repo else f"owner/{repo}"

    def _now(self) -> str:
        self._clock += 1
        return (BASE_TIME + timedelta(minutes=self._clock)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _next_number(self, slug: str) -> int:
        numbers = [n for (s, n) in (*self.issues, *self.prs) if s == slug]
        return max(numbers, default=0) + 1

    def labels_of(self, slug: str) -> list[str]:
        if slug not in self.labels:
            self.labels[slug] = [*STATE_LABELS, *PRIORITY_LABELS]
        return self.labels[slug]

    def add_issue(
        self,
        number: int | None = None,
        repo: str = "pi-relay",
        title: str = "Add retry to fetch",
        body: str = "Please add retries.",
        state: str | None = "ready-for-design",
        labels: tuple[str, ...] = (),
        created_at: str | None = None,
    ) -> int:
        slug = self.slug(repo)
        with self._lock:
            number = number or self._next_number(slug)
            names = ([f"{PREFIX}{state}"] if state else []) + list(labels)
            self.issues[(slug, number)] = {
                "number": number, "title": title, "body": body, "labels": names,
                "state": "open", "state_reason": None,
                "created_at": created_at or self._now(), "assignees": [], "comments": [],
            }  # fmt: skip
            return number

    def add_pull_request(
        self, repo: str = "pi-relay", labels: tuple[str, ...] = (), title: str = "A PR"
    ) -> int:
        """A pull request that carries labels (it is also listed by the issues endpoint)."""
        slug = self.slug(repo)
        with self._lock:
            number = self._next_number(slug)
            self.prs[(slug, number)] = {
                "number": number, "head": f"branch-{number}", "base": "main", "title": title,
                "body": "", "state": "open", "labels": list(labels), "created_at": self._now(),
            }  # fmt: skip
            return number

    def issue(self, number: int, repo: str = "pi-relay") -> dict[str, Any]:
        return self.issues[(self.slug(repo), number)]

    def factory_labels(self, number: int, repo: str = "pi-relay") -> list[str]:
        return [name for name in self.issue(number, repo)["labels"] if name.startswith(PREFIX)]

    def state(self, number: int, repo: str = "pi-relay") -> str:
        """The factory state without its prefix; several are joined with ``+``."""
        return "+".join(name[len(PREFIX) :] for name in self.factory_labels(number, repo))

    def move(self, number: int, state: str | None, repo: str = "pi-relay") -> None:
        """What a human does: replace every factory:* label with ``factory:<state>``."""
        issue = self.issue(number, repo)
        issue["labels"] = [n for n in issue["labels"] if not n.startswith(PREFIX)]
        if state:
            issue["labels"].append(f"{PREFIX}{state}")

    def add_label(self, number: int, label: str, repo: str = "pi-relay") -> None:
        self.issue(number, repo)["labels"].append(label)

    def close(self, number: int, reason: str = "completed", repo: str = "pi-relay") -> None:
        issue = self.issue(number, repo)
        issue["state"], issue["state_reason"] = "closed", reason

    def add_comment(
        self,
        number: int,
        body: str,
        repo: str = "pi-relay",
        login: str = "olivia",
        association: str = "OWNER",
    ) -> str:
        with self._lock:
            self._seq += 1
            comment = {
                "id": self._seq, "body": body, "created_at": self._now(),
                "login": login, "association": association,
            }  # fmt: skip
            self.issue(number, repo)["comments"].append(comment)
            return str(comment["id"])

    def comments(self, number: int, repo: str = "pi-relay") -> list[dict[str, Any]]:
        return self.issue(number, repo)["comments"]

    def assignees(self, number: int, repo: str = "pi-relay") -> list[str]:
        return self.issue(number, repo)["assignees"]

    def open_prs(self, slug: str = "owner/pi-relay") -> list[dict[str, Any]]:
        slug = self.slug(slug)
        return [p for (s, _), p in self.prs.items() if s == slug and p["state"] == "open"]

    def merge_pr(self, number: int, repo: str = "pi-relay") -> list[int]:
        """Merge a PR into the default branch. ``Closes #N`` closes those issues as completed."""
        slug = self.slug(repo)
        pr = self.prs[(slug, number)]
        pr["state"] = "closed"
        pr["merged"] = True
        closed = []
        for n in re.findall(r"(?i)\bcloses #(\d+)", pr.get("body") or ""):
            if (slug, int(n)) in self.issues:
                self.close(int(n), "completed", repo)
                closed.append(int(n))
        return closed

    def calls_to(self, method: str, pattern: str) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]

    # -- server side -----------------------------------------------------------------------
    def _handle(self, method: str, raw_path: str, auth: str, body: Any):
        parsed = urlparse(raw_path)
        with self._lock:
            self.calls.append((method, raw_path, body))
            if self.rate_limit_next:
                spec = self.rate_limit_next.pop(0)
                return (
                    spec.get("status", 403),
                    {"message": spec.get("message", "API rate limit exceeded")},
                    spec.get("headers", {}),
                )
            if self.fail_next:
                return self.fail_next.pop(0), {"message": "server error"}, {}
            if auth != f"Bearer {self.token}":
                return 401, {"message": "Bad credentials"}, {}
            code, payload = self._route(method, parsed.path, parse_qs(parsed.query), body)
            return code, payload, {}

    def _route(self, method: str, path: str, query: dict[str, list[str]], body: Any):
        if path == "/user":
            return 200, {"login": self.bot_login}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if not m:
            return 404, {"message": "Not Found"}
        slug, rest = m.group(1), m.group(2) or ""
        if slug not in self.repos:
            return 404, {"message": "Not Found"}
        if rest == "":
            return 200, {
                "full_name": slug,
                "permissions": {"push": self.repos[slug]},
                "has_issues": self.has_issues.get(slug, True),
                "default_branch": self.default_branches.get(slug, "main"),
            }
        if rest == "/labels" and method == "GET":
            return 200, self._page([{"name": n} for n in self.labels_of(slug)], query)
        m = re.fullmatch(r"/assignees/([^/]+)", rest)
        if m and method == "GET":
            return (
                (204, None) if unquote(m.group(1)) in self.assignable else (404, {"message": "no"})
            )
        if rest.startswith("/pulls"):
            return self._pulls(method, slug, rest, query, body)
        if rest == "/issues" and method == "GET":
            return self._list_issues(slug, query)
        m = re.fullmatch(r"/issues/(\d+)(/.*)?", rest)
        if m:
            return self._issue_route(method, slug, int(m.group(1)), m.group(2) or "", query, body)
        return 404, {"message": "Not Found"}

    @staticmethod
    def _page(items: list[Any], query: dict[str, list[str]]) -> list[Any]:
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        return items[(page - 1) * per_page : page * per_page]

    # -- issues ----------------------------------------------------------------------------
    @staticmethod
    def _label_nodes(names: list[str]) -> list[dict[str, str]]:
        return [{"name": n} for n in names]

    def _issue_node(self, slug: str, issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": issue["number"], "title": issue["title"], "body": issue["body"],
            "state": issue["state"], "state_reason": issue["state_reason"],
            "created_at": issue["created_at"],
            "html_url": f"https://github.com/{slug}/issues/{issue['number']}",
            "labels": self._label_nodes(issue["labels"]),
            "assignees": [{"login": a} for a in issue["assignees"]],
        }  # fmt: skip

    def _pr_as_issue_node(self, slug: str, pr: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": pr["number"], "title": pr["title"], "body": pr["body"],
            "state": pr["state"], "state_reason": None, "created_at": pr["created_at"],
            "html_url": f"https://github.com/{slug}/pull/{pr['number']}",
            "labels": self._label_nodes(pr.get("labels", [])), "assignees": [],
            "pull_request": {"url": f"https://api.github.com/repos/{slug}/pulls/{pr['number']}"},
        }  # fmt: skip

    def _list_issues(self, slug: str, query: dict[str, list[str]]):
        want_state = query.get("state", ["open"])[0]
        want_labels = [lb.lower() for lb in query.get("labels", [""])[0].split(",") if lb]
        nodes = [self._issue_node(slug, i) for (s, _), i in self.issues.items() if s == slug] + [
            self._pr_as_issue_node(slug, p) for (s, _), p in self.prs.items() if s == slug
        ]
        nodes = [
            n for n in nodes
            if want_state in ("all", n["state"])
            and set(want_labels) <= {lb["name"].lower() for lb in n["labels"]}
        ]  # fmt: skip
        nodes.sort(
            key=lambda n: (n["created_at"], n["number"]), reverse=query.get("direction") == ["desc"]
        )
        return 200, self._page(nodes, query)

    def _issue_route(self, method, slug, number, rest, query, body):
        if (slug, number) in self.prs and (slug, number) not in self.issues:
            if method == "GET" and rest == "":
                return 200, self._pr_as_issue_node(slug, self.prs[(slug, number)])
            return 404, {"message": "Not Found"}
        issue = self.issues.get((slug, number))
        if issue is None:
            return 404, {"message": "Not Found"}
        if rest == "" and method == "GET":
            return 200, self._issue_node(slug, issue)
        if rest == "/labels" and method == "POST":
            for name in body["labels"]:
                if name not in issue["labels"]:
                    issue["labels"].append(name)
                if name.startswith(PREFIX):
                    self.transitions.append((number, name))
            return 200, self._label_nodes(issue["labels"])
        m = re.fullmatch(r"/labels/(.+)", rest)
        if m and method == "DELETE":
            name = unquote(m.group(1))
            if name not in issue["labels"]:
                return 404, {"message": "Label does not exist"}
            issue["labels"].remove(name)
            return 200, self._label_nodes(issue["labels"])
        if rest == "/assignees" and method == "POST":
            for login in body["assignees"]:
                if login in self.assignable and login not in issue["assignees"]:
                    issue["assignees"].append(login)
            return 201, self._issue_node(slug, issue)
        if rest == "/comments" and method == "GET":
            nodes = [self._comment_node(c) for c in issue["comments"]]
            return 200, self._page(nodes, query)
        if rest == "/comments" and method == "POST":
            self._seq += 1
            comment = {
                "id": self._seq, "body": body["body"], "created_at": self._now(),
                "login": self.bot_login, "association": "COLLABORATOR",
            }  # fmt: skip
            issue["comments"].append(comment)
            return 201, self._comment_node(comment)
        return 404, {"message": "Not Found"}

    @staticmethod
    def _comment_node(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": c["id"], "body": c["body"], "created_at": c["created_at"],
            "user": {"login": c["login"]} if c["login"] else None,
            "author_association": c["association"],
        }  # fmt: skip

    # -- pull requests ---------------------------------------------------------------------
    def _pulls(self, method, slug, rest, query, body):
        m = re.fullmatch(r"/pulls(?:/(\d+))?", rest)
        if not m:
            return 404, {"message": "Not Found"}
        number = m.group(1)
        if method == "GET" and number is None:
            head = query.get("head", [""])[0].split(":", 1)[-1]
            return 200, [self._node(slug, p) for p in self.open_prs(slug) if p["head"] == head]
        if method == "POST" and number is None:
            if any(p["head"] == body["head"] for p in self.open_prs(slug)):
                return 422, {"message": "A pull request already exists for this head"}
            n = self._next_number(slug)
            pr = {
                "number": n, "head": body["head"], "base": body["base"], "title": body["title"],
                "body": body["body"], "state": "open", "labels": [], "created_at": self._now(),
            }  # fmt: skip
            self.prs[(slug, n)] = pr
            return 201, self._node(slug, pr)
        pr = self.prs.get((slug, int(number)))
        if pr is None:
            return 404, {"message": "Not Found"}
        if method == "PATCH":
            pr.update({k: v for k, v in body.items() if k in ("body", "title")})
            return 200, self._node(slug, pr)
        node = self._node(slug, pr)
        node["mergeable"] = self.mergeable.pop(0) if self.mergeable else self.default_mergeable
        return 200, node

    @staticmethod
    def _node(slug: str, pr: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": pr["number"], "title": pr["title"], "body": pr["body"], "state": pr["state"],
            "html_url": f"https://github.com/{slug}/pull/{pr['number']}",
            "head": {"ref": pr["head"]}, "base": {"ref": pr["base"]},
        }  # fmt: skip
