"""A complete in-process factory environment built from the fakes (GitHub, pi, git)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from pi_factory.codehost.github import GitHub
from pi_factory.config import FactoryConfig
from pi_factory.gitops import GitOps
from pi_factory.phases.common import Deps, Task
from pi_factory.runs import RunStore
from pi_factory.secrets import Secrets
from pi_factory.tracker.github import GitHubTracker

from .conftest import FakePi
from .fakes.github import FakeGitHub
from .gitfix import make_config, make_remote

SECRET_VALUES = {"PI_FACTORY_GITHUB_TOKEN": "ghp_bottoken"}


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
        self.tracker = GitHubTracker(self.cfg, secrets, api_url=self.github.api_url)
        self.codehost = GitHub(secrets, api_url=self.github.api_url, mergeable_retry_delay=0)
        self.git = GitOps(self.cfg, token=secrets.github_token)
        self.runs = RunStore(self.cfg.runs_dir, self.cfg.archive_dir)
        self.deps = Deps(self.cfg, self.tracker, self.codehost, self.git, self.runs)

    def close(self) -> None:
        self.github.stop()

    # -- helpers ---------------------------------------------------------------------------
    def key(self, number: int = 1, repo: str = "pi-relay") -> str:
        return f"{repo}-{number}"

    def fetch_issue(self, number: int = 1, repo: str = "pi-relay"):
        return self.tracker.get_issue(repo, number)

    def task(self, number: int = 1, phase: str = "design", repo: str = "pi-relay") -> Task:
        return Task(self.fetch_issue(number, repo), self.cfg.repos[repo], phase)

    def seed_issue(self, number: int = 1, state: str | None = "ready-for-design", **kw) -> int:
        if "description" in kw:
            kw["body"] = kw.pop("description")
        return self.github.add_issue(number, state=state, **kw)

    def worktree(self, number: int = 1, repo: str = "pi-relay") -> Path:
        return self.git.worktree_path(self.cfg.repos[repo], number)
