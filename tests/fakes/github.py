"""An in-process fake GitHub REST API: repo access, and pull-request find/create/update."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class FakeGitHub:
    def __init__(self, token: str = "ghp_bottoken", repos: dict[str, bool] | None = None):
        self.token = token
        # "owner/name" -> bot has push permission. Repos not listed answer 404 (not shared).
        self.repos: dict[str, bool] = repos if repos is not None else {"owner/pi-relay": True}
        self.prs: dict[tuple[str, int], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.mergeable: list[bool | None] = []  # answers for GET pulls/N, consumed in order
        self.default_mergeable: bool | None = True
        self.fail_next: list[int] = []
        self._n = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else None
                code, payload = fake._handle(
                    method, self.path, self.headers.get("Authorization", ""), body
                )
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = lambda self: self._serve("GET")  # noqa: E731
            do_POST = lambda self: self._serve("POST")  # noqa: E731
            do_PATCH = lambda self: self._serve("PATCH")  # noqa: E731

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )

    def start(self) -> FakeGitHub:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def open_prs(self, slug: str = "owner/pi-relay") -> list[dict[str, Any]]:
        return [p for (s, _), p in self.prs.items() if s == slug and p["state"] == "open"]

    def calls_to(self, method: str, pattern: str) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]

    def _handle(self, method: str, raw_path: str, auth: str, body: Any):
        parsed = urlparse(raw_path)
        with self._lock:
            self.calls.append((method, raw_path, body))
            if self.fail_next:
                return self.fail_next.pop(0), {"message": "server error"}
            if auth != f"Bearer {self.token}":
                return 401, {"message": "Bad credentials"}
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)(?:/pulls(?:/(\d+))?)?", parsed.path)
            if not m:
                return 404, {"message": "Not Found"}
            slug, number = m.group(1), m.group(2)
            if slug not in self.repos:
                return 404, {"message": "Not Found"}
            is_pulls = "/pulls" in parsed.path
            if not is_pulls:
                return 200, {"full_name": slug, "permissions": {"push": self.repos[slug]}}
            if method == "GET" and number is None:
                head = parse_qs(parsed.query).get("head", [""])[0].split(":", 1)[-1]
                return 200, [self._node(slug, p) for p in self.open_prs(slug) if p["head"] == head]
            if method == "POST" and number is None:
                if any(p["head"] == body["head"] for p in self.open_prs(slug)):
                    return 422, {"message": "A pull request already exists for this head"}
                self._n += 1
                pr = {"number": self._n, "head": body["head"], "base": body["base"],
                      "title": body["title"], "body": body["body"], "state": "open"}  # fmt: skip
                self.prs[(slug, self._n)] = pr
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
