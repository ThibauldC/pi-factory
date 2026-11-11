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
from .config import FactoryConfig
from .secrets import Secrets, SecretsError
from .tracker.base import Tracker, TrackerError

GITHUB_INTEGRATION_REMINDER = (
    "Linear's GitHub integration cannot be verified from here: make sure it is installed for "
    "the factory repositories, or issues will stay In Review after a PR is merged."
)


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
    needed = [secrets.github_token]
    if cfg.linear.auth == "api_key":
        needed.append(secrets.linear_api_key)
    else:
        needed += [secrets.linear_client_id, secrets.linear_client_secret]
    for accessor in needed:
        try:
            accessor()
        except SecretsError as exc:
            return CheckResult("secrets", False, str(exc))
    return CheckResult("secrets", True, f"{len(needed)} required secret(s) present")


def check_directories(cfg: FactoryConfig) -> CheckResult:
    for directory in (cfg.runs_dir, cfg.archive_dir, cfg.worktree_root, cfg.repos_root):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError as exc:
            return CheckResult("directories", False, f"{directory} is not writable: {exc.strerror}")
    return CheckResult("directories", True, "state, worktree and clone directories are writable")


def check_linear(cfg: FactoryConfig, tracker: Tracker) -> CheckResult:
    try:
        problems = tracker.check_setup(list(cfg.repos))
    except (TrackerError, SecretsError) as exc:
        return CheckResult("linear", False, f"cannot query Linear: {exc}")
    if problems:
        return CheckResult("linear", False, "; ".join(problems))
    return CheckResult("linear", True, "workflow states, repo labels and owner exist")


def check_github(cfg: FactoryConfig, codehost: CodeHost) -> list[CheckResult]:
    results = []
    for key, repo in cfg.repos.items():
        try:
            problem = codehost.check_access(repo)
        except (CodeHostError, SecretsError) as exc:
            problem = f"cannot query GitHub: {exc}"
        results.append(
            CheckResult(f"github:{key}", problem is None, problem or "bot has push access")
        )
    return results


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
        results.append(check_linear(cfg, tracker))
        results.extend(check_github(cfg, codehost))
    return results
