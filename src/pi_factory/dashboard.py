"""The read-only status dashboard (design D11): server-rendered HTML, GET only.

It listens on a loopback address and is exposed to the tailnet with ``tailscale serve``. It
shows scheduler state and run records; it never changes anything and never shows secrets.
"""

from __future__ import annotations

import html
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .config import FactoryConfig
from .runs import RunRecord, RunStore
from .scheduler import Scheduler
from .secrets import redact

REFRESH_SECONDS = 10
TAIL_LINES = 200
RECENT_RUNS = 20

CSS = """
body{font:14px/1.45 system-ui,sans-serif;margin:1.2rem;color:#1b1f23;background:#fff}
h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:1.6rem;border-bottom:1px solid #ddd}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.25rem .6rem;
border-bottom:1px solid #eee;vertical-align:top}th{color:#555;font-weight:600}
pre{background:#f6f8fa;padding:.6rem;overflow:auto;max-height:28rem;font-size:12px}
.muted{color:#666}.err{color:#b42318}.ok{color:#067647}code{background:#f3f4f6;padding:0 .25rem}
@media(prefers-color-scheme:dark){body{background:#111;color:#e6e6e6}th,.muted{color:#aaa}
td,th,h2{border-color:#333}pre,code{background:#1c1c1c}a{color:#7ab7ff}}
"""


def esc(value: object) -> str:
    return html.escape(redact(str(value)), quote=True)


def link(url: str, text: str) -> str:
    """An anchor for http(s) URLs only (never ``javascript:`` or other schemes)."""
    if re.match(r"^https?://", url or ""):
        return f'<a href="{esc(url)}">{esc(text)}</a>'
    return esc(text)


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def fmt_elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def token_totals(rec: RunRecord) -> tuple[int, int]:
    total_in = total_out = 0
    for per_model in rec.usage.values():
        for counts in per_model.values():
            total_in += counts.get("input", 0)
            total_out += counts.get("output", 0)
    return total_in, total_out


def page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"
    ).encode()


class Dashboard:
    def __init__(
        self,
        scheduler: Scheduler,
        runs: RunStore,
        config: FactoryConfig,
        host: str | None = None,
        port: int | None = None,
    ):
        self.scheduler = scheduler
        self.runs = runs
        self.config = config
        handler = _make_handler(self)
        self.server = ThreadingHTTPServer(
            (host or config.dashboard.host, config.dashboard.port if port is None else port),
            handler,
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.1),
            daemon=True,
            name="dashboard",
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> Dashboard:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- data ------------------------------------------------------------------------------
    def recent_runs(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        for key in self.runs.list_issues():
            rec = self.runs.load(key)
            if rec and rec.outcome:
                records.append(rec)
        records.extend(self.runs.recent_archived(RECENT_RUNS))
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:RECENT_RUNS]

    # -- pages -----------------------------------------------------------------------------
    def overview(self) -> bytes:
        snap = self.scheduler.snapshot()
        now = time.time()
        by_repo = {t["repo"]: t for t in snap["tasks"]}
        rows = []
        for key in sorted(self.config.repos):
            t = by_repo.get(key)
            if t:
                issue = f'<a href="/issue/{esc(t["key"])}">{esc(t["issue"])}</a> {esc(t["title"])}'
                rows.append(
                    f"<tr><td><code>{esc(key)}</code></td><td>{issue}</td><td>{esc(t['phase'])}</td>"
                    f"<td>{esc(t['step'] or '-')}</td><td>{esc(t['round'])}</td>"
                    f"<td>{fmt_elapsed(now - t['started_at'])}</td></tr>"
                )
            else:
                rows.append(
                    f"<tr><td><code>{esc(key)}</code></td><td class='muted' colspan='5'>idle</td></tr>"
                )
        repos = (
            "<table><tr><th>Repository</th><th>Active issue</th><th>Phase</th><th>Step</th>"
            "<th>Round</th><th>Elapsed</th></tr>" + "".join(rows) + "</table>"
        )

        queue = "".join(
            f"<tr><td>{n}</td><td>{link(q['url'], q['issue'])} {esc(q['title'])}</td>"
            f"<td><code>{esc(q['repo'])}</code></td><td>{esc(q['phase'])}</td>"
            f"<td>{esc(q['priority'] or '-')}</td></tr>"
            for n, q in enumerate(snap["queue"], 1)
        )
        queue_html = (
            "<table><tr><th>#</th><th>Issue</th><th>Repository</th><th>Next phase</th>"
            f"<th>Priority</th></tr>{queue}</table>"
            if queue
            else "<p class='muted'>nothing queued</p>"
        )

        recent = []
        for rec in self.recent_runs():
            tin, tout = token_totals(rec)
            pr = link(rec.pr_url, f"PR #{rec.pr_number}") if rec.pr_url else "-"
            cls = "err" if rec.outcome == "blocked" else "ok"
            closed = f" (issue closed: {esc(rec.close_reason)})" if rec.close_reason else ""
            recent.append(
                f"<tr><td>{link(rec.issue_url, rec.ref)} {esc(rec.title)}</td>"
                f"<td><code>{esc(rec.repo)}</code></td><td>{esc(rec.phase)} r{esc(rec.round)}</td>"
                f"<td class='{cls}'>{esc(rec.outcome)}"
                f"{' – ' + esc(rec.outcome_detail[:120]) if rec.outcome_detail else ''}{closed}</td>"
                f"<td>{pr}</td><td>{tin:,} in / {tout:,} out</td><td>{esc(rec.updated_at)}</td></tr>"
            )
        recent_html = (
            "<table><tr><th>Issue</th><th>Repo</th><th>Phase</th><th>Outcome</th><th>PR</th>"
            "<th>Tokens</th><th>Updated</th></tr>" + "".join(recent) + "</table>"
            if recent
            else "<p class='muted'>no runs yet</p>"
        )

        if snap.get("conflicts"):
            rows_c = "".join(
                f"<tr><td>{link(c['url'], c['issue'])} {esc(c['title'])}</td>"
                f"<td>{', '.join(f'<code>{esc(lb)}</code>' for lb in c['labels'])}</td></tr>"
                for c in snap["conflicts"]
            )
            conflicts_html = (
                "<p class='muted'>These issues carry more than one factory:* state label and "
                "are ignored until a human fixes them.</p>"
                f"<table><tr><th>Issue</th><th>State labels</th></tr>{rows_c}</table>"
            )
        else:
            conflicts_html = "<p class='muted'>none</p>"
        if snap["poll_errors"]:
            errs = "".join(
                f"<li class='err'>{fmt_time(ts)} – {esc(text)}</li>"
                for ts, text in reversed(snap["poll_errors"][-5:])
            )
            errors = f"<ul>{errs}</ul>"
        else:
            errors = "<p class='muted'>no poll errors</p>"
        status = {True: "ok", False: "failed", None: "not yet"}[snap["last_poll_ok"]]
        body = (
            "<h1>pi-factory</h1>"
            f"<p class='muted'>Last poll: {fmt_time(snap['last_poll_at'])} ({status}); "
            f"consecutive failures: {esc(snap['failures'])}; refreshes every {REFRESH_SECONDS}s</p>"
            f"<h2>Repositories</h2>{repos}<h2>Queue</h2>{queue_html}"
            f"<h2>Recent runs</h2>{recent_html}"
            f"<h2>Ignored issues (conflicting labels)</h2>{conflicts_html}"
            f"<h2>Poll errors</h2>{errors}"
        )
        return page("pi-factory", body)

    def issue_page(self, key: str) -> bytes | None:
        with self.scheduler._lock:
            task = self.scheduler.tasks.get(key)
        rec = self.runs.load(key)
        if task is None and rec is None:
            return None
        ident = task.ref if task is not None else rec.ref
        parts = [f"<h1>{esc(ident)}</h1><p><a href='/'>&larr; overview</a></p>"]
        if task is not None:
            snap = task.snapshot()
            parts.append(
                f"<p>{link(snap['url'], snap['issue'])} {esc(snap['title'])} &middot; "
                f"<code>{esc(snap['repo'])}</code> &middot; phase <b>{esc(snap['phase'])}</b>, "
                f"step <b>{esc(snap['step'] or '-')}</b>, round {esc(snap['round'])}, "
                f"elapsed {fmt_elapsed(time.time() - snap['started_at'])}</p>"
            )
            agent = task.agent_ring.snapshot()[-TAIL_LINES:]
            checks = task.check_ring.snapshot()[-TAIL_LINES:]
            parts.append(
                f"<h2>Agent output (last {TAIL_LINES} lines)</h2><pre>{esc(chr(10).join(agent))}</pre>"
            )
            parts.append(
                f"<h2>Check / setup output (last {TAIL_LINES} lines)</h2>"
                f"<pre>{esc(chr(10).join(checks))}</pre>"
            )
        else:
            parts.append("<p class='muted'>not running</p>")
        if rec is not None:
            tin, tout = token_totals(rec)
            parts.append(
                "<h2>Run record</h2><table>"
                f"<tr><th>Repository</th><td><code>{esc(rec.repo)}</code></td></tr>"
                f"<tr><th>Phase / round / step</th><td>{esc(rec.phase)} / {esc(rec.round)} / "
                f"{esc(rec.step or '-')}</td></tr>"
                f"<tr><th>Outcome</th><td>{esc(rec.outcome or '-')} {esc(rec.outcome_detail)}</td></tr>"
                f"<tr><th>Pull request</th><td>{link(rec.pr_url, rec.pr_url) if rec.pr_url else '-'}"
                "</td></tr>"
                f"<tr><th>Tokens</th><td>{tin:,} in / {tout:,} out</td></tr>"
                f"<tr><th>Updated</th><td>{esc(rec.updated_at)}</td></tr></table>"
            )
        return page(ident, "".join(parts))


def _make_handler(dash: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pi-factory"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the journal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = unquote(urlparse(self.path).path).rstrip("/") or "/"
            try:
                if path == "/":
                    self._send(200, dash.overview())
                    return
                m = re.fullmatch(r"/issue/([A-Za-z0-9_.-]{1,64})", path)
                if m:
                    body = dash.issue_page(m.group(1))
                    if body is not None:
                        self._send(200, body)
                        return
                self._send(404, page("Not found", "<h1>Not found</h1>"))
            except Exception:  # noqa: BLE001 - never leak internals to the client
                self._send(500, page("Error", "<h1>Internal error</h1>"))

        def _method_not_allowed(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _method_not_allowed  # noqa: N815

    return Handler
