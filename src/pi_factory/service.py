"""Wire the real adapters together and run the factory service."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from . import logs, preflight
from .codehost.github import GitHub
from .config import FactoryConfig
from .dashboard import Dashboard
from .gitops import GitOps
from .phases.common import Deps
from .runs import RunStore
from .scheduler import Scheduler, install_signal_handlers
from .secrets import Secrets
from .tracker.linear import LinearTracker


@dataclass
class Services:
    deps: Deps
    secrets: Secrets


def build_services(cfg: FactoryConfig, secrets: Secrets, **overrides: Any) -> Services:
    """Real adapters. ``overrides`` (``linear_api_url``, ``linear_token_url``,
    ``github_api_url``) exist only so tests can point at fakes."""
    linear_kw = {
        k: overrides[v]
        for k, v in (("api_url", "linear_api_url"), ("token_url", "linear_token_url"))
        if v in overrides
    }
    tracker = LinearTracker(cfg.linear, secrets, **linear_kw)
    github_kw = {"api_url": overrides["github_api_url"]} if "github_api_url" in overrides else {}
    codehost = GitHub(secrets, **github_kw)
    if "repo_slug" in overrides:
        codehost._slug = overrides["repo_slug"]
    git = GitOps(cfg, token=secrets.github_token)
    runs = RunStore(cfg.runs_dir, cfg.archive_dir)
    return Services(Deps(cfg, tracker, codehost, git, runs), secrets)


def report(results: list[preflight.CheckResult], out=None) -> bool:
    out = out or sys.stdout
    ok = True
    for r in results:
        print(f"[{'ok' if r.ok else 'FAIL'}] {r.name}: {r.detail}", file=out)
        ok = ok and r.ok
    print(f"[note] {preflight.GITHUB_INTEGRATION_REMINDER}", file=out)
    return ok


def run_service(services: Services) -> int:
    """Preflight, then recovery + scheduling + dashboard until SIGTERM/SIGINT."""
    deps = services.deps
    cfg = deps.config
    results = preflight.run_checks(cfg, services.secrets, deps.tracker, deps.codehost)
    for r in results:
        logs.event("preflight", check=r.name, ok=r.ok, detail=r.detail)
    if not all(r.ok for r in results):
        return 1
    scheduler = Scheduler(deps)
    dashboard = Dashboard(scheduler, deps.runs, cfg).start()
    install_signal_handlers(scheduler)
    logs.event(
        "factory_started",
        repos=",".join(cfg.repos),
        dashboard=f"{cfg.dashboard.host}:{dashboard.port}",
        max_concurrent=cfg.max_concurrent,
    )
    try:
        scheduler.run_forever()  # recovery of interrupted runs happens in the first poll
    finally:
        logs.event("factory_stopping")
        scheduler.stop()
        dashboard.stop()
    logs.event("factory_stopped")
    return 0
