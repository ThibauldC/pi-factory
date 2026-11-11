"""A complete in-process factory environment built from the fakes (Linear, GitHub, pi, git)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from pi_factory.codehost.github import GitHub
from pi_factory.config import FactoryConfig
from pi_factory.gitops import GitOps
from pi_factory.phases.common import Deps, Task
from pi_factory.runs import RunStore
from pi_factory.secrets import Secrets
from pi_factory.tracker.linear import LinearTracker

from .conftest import FakePi
from .fakes.github import FakeGitHub
from .fakes.linear import FakeLinear
from .gitfix import make_config, make_remote

SECRET_VALUES = {
    "PI_FACTORY_LINEAR_CLIENT_ID": "cid",
    "PI_FACTORY_LINEAR_CLIENT_SECRET": "csecret",
    "PI_FACTORY_LINEAR_API_KEY": "lin_api_secretkey",
    "PI_FACTORY_GITHUB_TOKEN": "ghp_bottoken",
}


class Env:
    def __init__(
        self,
        root: Path,
        pi_turns: list[dict] | None = None,
        repos: tuple[str, ...] = ("pi-relay",),
        checks: list[str] | None = None,
        setup: str | None = None,
        **config_overrides,
    ):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.linear = FakeLinear(repo_labels=repos).start()
        self.github = FakeGitHub(repos={f"owner/{r}": True for r in repos}).start()
        self.remotes = {r: make_remote(root, r) for r in repos}
        self.pi = FakePi(root / "fakepi", pi_turns)
        extra: dict = {}
        if checks is not None:
            extra["checks"] = checks
        if setup is not None:
            extra["setup"] = setup
        cfg = make_config(root, self.remotes, **extra)
        design = dataclasses.replace(cfg.design, timeout_seconds=30)
        build = dataclasses.replace(cfg.build, timeout_seconds=30, command_timeout_seconds=30)
        cfg = dataclasses.replace(cfg, pi_path=self.pi.path, design=design, build=build)
        self.cfg: FactoryConfig = dataclasses.replace(cfg, **config_overrides)
        secrets = Secrets(SECRET_VALUES)
        self.tracker = LinearTracker(
            self.cfg.linear, secrets, api_url=self.linear.api_url, token_url=self.linear.token_url
        )
        self.codehost = GitHub(
            secrets,
            api_url=self.github.api_url,
            mergeable_retry_delay=0,
            repo_slug=lambda r: ("owner", r.key),
        )
        self.git = GitOps(self.cfg, token=secrets.github_token)
        self.runs = RunStore(self.cfg.runs_dir, self.cfg.archive_dir)
        self.deps = Deps(self.cfg, self.tracker, self.codehost, self.git, self.runs)

    def close(self) -> None:
        self.linear.stop()
        self.github.stop()

    # -- helpers ---------------------------------------------------------------------------
    def fetch_issue(self, identifier: str):
        issues = self.tracker.list_issues([i["id"] for i in self.linear.issues.values()])
        return next(i for i in issues if i.identifier == identifier)

    def task(self, identifier: str, phase: str, repo: str = "pi-relay") -> Task:
        return Task(self.fetch_issue(identifier), self.cfg.repos[repo], phase)

    def seed_issue(self, identifier="THI-1", state="Ready for Design", **kw) -> str:
        return self.linear.add_issue(identifier, state=state, **kw)
