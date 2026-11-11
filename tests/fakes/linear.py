"""An in-process fake Linear: OAuth token endpoint plus the GraphQL operations the factory uses."""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

DEFAULT_STATES = [
    ("Backlog", "backlog"),
    ("Ready for Design", "unstarted"),
    ("Designing", "started"),
    ("Design Review", "started"),
    ("Ready for Build", "started"),
    ("Building", "started"),
    ("In Review", "started"),
    ("Blocked", "started"),
    ("Done", "completed"),
    ("Canceled", "canceled"),
]
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class FakeLinear:
    def __init__(
        self,
        team_key: str = "THI",
        repo_labels: tuple[str, ...] = ("pi-relay", "other"),
        states: list[tuple[str, str]] | None = None,
        auth: str = "oauth",
    ):
        self.team_key = team_key
        self.auth = auth
        self.states = [
            {"id": f"st-{i}", "name": n, "type": t}
            for i, (n, t) in enumerate(states or DEFAULT_STATES)
        ]
        self.labels = [{"name": n, "parent": {"name": "repo"}} for n in repo_labels]
        self.users = [
            {"id": "u-owner", "name": "Olivia Owner", "displayName": "olivia",
             "email": "owner@example.com", "active": True}
        ]  # fmt: skip
        self.issues: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.transitions: list[tuple[str, str]] = []
        self.client_id, self.client_secret = "cid", "csecret"
        self.api_key = "lin_api_secretkey"
        self.token_ttl = 3600
        self.token_requests = 0
        self.valid_tokens: set[str] = set()
        self.fail_next: list[int] = []  # HTTP statuses for upcoming GraphQL calls
        self.page_size = 50
        self._seq = 0
        self._clock = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                if self.path == "/oauth/token":
                    code, payload = fake._token(parse_qs(body))
                elif self.path == "/graphql":
                    code, payload = fake._graphql(
                        self.headers.get("Authorization", ""), json.loads(body)
                    )
                else:
                    code, payload = 404, {}
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )

    # -- lifecycle -------------------------------------------------------------------------
    def start(self) -> FakeLinear:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/graphql"

    @property
    def token_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/oauth/token"

    # -- test helpers ----------------------------------------------------------------------
    def _now(self) -> str:
        self._clock += 1
        return (BASE_TIME + timedelta(minutes=self._clock)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def state_by_name(self, name: str) -> dict[str, str]:
        return next(s for s in self.states if s["name"] == name)

    def add_issue(
        self,
        identifier: str,
        title: str = "Add retry to fetch",
        description: str = "Please add retries.",
        state: str = "Ready for Design",
        labels: tuple[str, ...] = ("pi-relay",),
        priority: int = 0,
        created_at: str | None = None,
    ) -> str:
        with self._lock:
            self._seq += 1
            iid = f"iss-{self._seq}"
            self.issues[iid] = {
                "id": iid,
                "identifier": identifier,
                "title": title,
                "description": description,
                "priority": priority,
                "createdAt": created_at or self._now(),
                "branchName": f"{identifier.lower()}-{slug(title)}",
                "url": f"https://linear.example/{identifier}",
                "state": state,
                "assignee": None,
                "labels": list(labels),
                "comments": [],
            }
            return iid

    def issue(self, identifier: str) -> dict[str, Any]:
        return next(i for i in self.issues.values() if i["identifier"] == identifier)

    def move(self, identifier: str, state: str) -> None:
        self.issue(identifier)["state"] = state

    def add_comment(self, identifier: str, body: str, user: str | None = "Olivia Owner") -> str:
        with self._lock:
            self._seq += 1
            cid = f"c-{self._seq}"
            self.issue(identifier)["comments"].append(
                {"id": cid, "body": body, "createdAt": self._now(), "user": user}
            )
            return cid

    def comments(self, identifier: str) -> list[dict[str, Any]]:
        return self.issue(identifier)["comments"]

    def ops(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["op"] == name]

    # -- server side -----------------------------------------------------------------------
    def _token(self, form: dict[str, list[str]]):
        self.token_requests += 1
        if (
            form.get("grant_type") != ["client_credentials"]
            or form.get("client_id") != [self.client_id]
            or form.get("client_secret") != [self.client_secret]
        ):
            return 401, {"error": "invalid_client"}
        token = f"tok-{self.token_requests}"
        self.valid_tokens.add(token)
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": self.token_ttl}

    def _authorized(self, header: str) -> bool:
        if self.auth == "api_key":
            return header == self.api_key
        return header.startswith("Bearer ") and header[7:] in self.valid_tokens

    def _graphql(self, auth_header: str, req: dict[str, Any]):
        op = re.search(r"(?:query|mutation)\s+(\w+)", req["query"]).group(1)
        variables = req.get("variables") or {}
        with self._lock:
            self.calls.append({"op": op, "variables": variables, "auth": auth_header})
            if self.fail_next:
                return self.fail_next.pop(0), {"errors": [{"message": "boom"}]}
            if not self._authorized(auth_header):
                return 401, {"errors": [{"message": "unauthorized"}]}
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                return 200, {"errors": [{"message": f"unknown operation {op}"}]}
            return 200, {"data": handler(variables)}

    def _issue_node(self, i: dict[str, Any]) -> dict[str, Any]:
        st = self.state_by_name(i["state"])
        return {
            **{k: i[k] for k in ("id", "identifier", "title", "description", "priority")},
            "createdAt": i["createdAt"], "branchName": i["branchName"], "url": i["url"],
            "state": st, "assignee": {"id": i["assignee"]} if i["assignee"] else None,
            "labels": {"nodes": [self._label_node(lb) for lb in i["labels"]]},
            "comments": {"nodes": [self._comment_node(c) for c in i["comments"]]},
        }  # fmt: skip

    @staticmethod
    def _label_node(label) -> dict[str, Any]:
        """A label is a name (in the ``repo`` group) or a ``(name, group)`` pair."""
        name, group = (label, "repo") if isinstance(label, str) else label
        return {"name": name, "parent": {"name": group} if group else None}

    @staticmethod
    def _comment_node(c: dict[str, Any]) -> dict[str, Any]:
        return {"id": c["id"], "body": c["body"], "createdAt": c["createdAt"],
                "user": {"name": c["user"]} if c["user"] else None}  # fmt: skip

    def _op_Issues(self, v):
        matching = [
            i for i in self.issues.values()
            if i["state"] in v["stateNames"] or i["id"] in v["ids"]
        ]  # fmt: skip
        matching.sort(key=lambda i: i["id"])
        start = int(v["after"]) if v.get("after") else 0
        page = matching[start : start + self.page_size]
        more = start + self.page_size < len(matching)
        return {"issues": {
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page_size)},
            "nodes": [self._issue_node(i) for i in page],
        }}  # fmt: skip

    def _op_IssueStates(self, v):
        nodes = [{"id": i["id"], "state": {"name": i["state"]}}
                 for i in self.issues.values() if i["id"] in v["ids"]]  # fmt: skip
        return {"issues": {"nodes": nodes}}

    def _op_IssueComments(self, v):
        i = self.issues.get(v["id"])
        return {"issue": {"comments": {"nodes": [self._comment_node(c) for c in i["comments"]]}}}

    def _op_Setup(self, v):
        if v["teamKey"] != self.team_key:
            return {"teams": {"nodes": []}, "users": {"nodes": self.users}}
        return {
            "teams": {"nodes": [{
                "id": "team-1", "key": self.team_key,
                "states": {"nodes": self.states}, "labels": {"nodes": self.labels},
            }]},
            "users": {"nodes": self.users},
        }  # fmt: skip

    def _op_IssueUpdate(self, v):
        issue = self.issues[v["id"]]
        inp = v["input"]
        if "stateId" in inp:
            state = next(s for s in self.states if s["id"] == inp["stateId"])
            issue["state"] = state["name"]
            self.transitions.append((issue["identifier"], state["name"]))
        if "assigneeId" in inp:
            issue["assignee"] = inp["assigneeId"]
        return {"issueUpdate": {"success": True}}

    def _op_CommentCreate(self, v):
        inp = v["input"]
        issue = self.issues[inp["issueId"]]
        self._seq += 1
        comment = {
            "id": f"c-{self._seq}",
            "body": inp["body"],
            "createdAt": self._now(),
            "user": None,
        }
        issue["comments"].append(comment)
        return {"commentCreate": {"success": True, "comment": self._comment_node(comment)}}
