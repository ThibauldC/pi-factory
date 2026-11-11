"""Preflight checks (design D14), shared by ``pi-factory check`` and ``pi-factory run``."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .childenv import build_child_env
from .codehost.base import CodeHost, CodeHostError
from .config import FactoryConfig, RepoConfig
from .secrets import Secrets, SecretsError
from .tracker.base import Tracker, TrackerError


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def pi_version(pi_path: Path, timeout: float = 30.0) -> str:
    """The installed pi's version string. Raises OSError/RuntimeError when it cannot run."""
    proc = subprocess.run(
        [str(pi_path), "--version"],
        env=build_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:200] or f"exit {proc.returncode}")
    return (proc.stdout.strip().splitlines() or ["unknown"])[0]


def check_pi(cfg: FactoryConfig) -> CheckResult:
    path = cfg.pi_path
    if not path.is_file() or not os.access(path, os.X_OK):
        return CheckResult("pi binary", False, f"{path} does not exist or is not executable")
    try:
        return CheckResult("pi binary", True, f"{path} ({pi_version(path)})")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return CheckResult("pi binary", False, f"{path} --version failed: {exc}")


def check_secrets(cfg: FactoryConfig, secrets: Secrets) -> CheckResult:
    try:
        secrets.github_token()
    except SecretsError as exc:
        return CheckResult("secrets", False, str(exc))
    return CheckResult("secrets", True, "the GitHub token is present")


def check_directories(cfg: FactoryConfig) -> CheckResult:
    for directory in (cfg.runs_dir, cfg.archive_dir, cfg.worktree_root, cfg.repos_root):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError as exc:
            return CheckResult("directories", False, f"{directory} is not writable: {exc.strerror}")
    return CheckResult("directories", True, "state, worktree and clone directories are writable")


def check_tracker(cfg: FactoryConfig, tracker: Tracker) -> list[CheckResult]:
    """The factory:* labels in every repository and the owner. Missing priority labels are
    only a warning."""
    repos = list(cfg.repos.values())
    try:
        problems = tracker.check_setup(repos)
        warnings = tracker.setup_warnings(repos)
    except (TrackerError, SecretsError) as exc:
        return [CheckResult("github issues", False, f"cannot query GitHub issues: {exc}")]
    if problems:
        results = [CheckResult("github issues", False, "; ".join(problems))]
    else:
        results = [CheckResult("github issues", True, "factory:* labels and the owner exist")]
    results.extend(CheckResult("github issues", True, f"warning: {w}") for w in warnings)
    return results


def check_github(cfg: FactoryConfig, codehost: CodeHost) -> list[CheckResult]:
    results = []
    for key, repo in cfg.repos.items():
        try:
            problem = codehost.check_access(repo)
        except (CodeHostError, SecretsError) as exc:
            problem = f"cannot query GitHub: {exc}"
        results.append(
            CheckResult(
                f"github:{key}", problem is None, problem or "bot has push access, issues enabled"
            )
        )
        if problem is None:
            results.append(check_base_branch(key, repo, codehost))
    return results


def check_base_branch(key: str, repo: RepoConfig, codehost: CodeHost) -> CheckResult:
    """`Closes #N` only fires when the PR merges into the default branch: warn otherwise."""
    name = f"base-branch:{key}"
    try:
        default = codehost.default_branch(repo)
    except (CodeHostError, SecretsError) as exc:
        return CheckResult(name, False, f"cannot read the default branch: {exc}")
    if default and default != repo.base_branch:
        return CheckResult(
            name,
            True,
            f"warning: base_branch '{repo.base_branch}' is not the default branch '{default}', "
            "so `Closes #N` in a PR will not close the issue when it merges",
        )
    return CheckResult(name, True, f"'{repo.base_branch}' is the default branch")


def run_checks(
    cfg: FactoryConfig,
    secrets: Secrets,
    tracker: Tracker,
    codehost: CodeHost,
    pi_checker: Callable[[FactoryConfig], CheckResult] = check_pi,
) -> list[CheckResult]:
    """Run every check (no early exit, so one pass reports everything that is wrong)."""
    results = [check_secrets(cfg, secrets), pi_checker(cfg), check_directories(cfg)]
    if results[0].ok:
        results.extend(check_tracker(cfg, tracker))
        results.extend(check_github(cfg, codehost))
    return results
