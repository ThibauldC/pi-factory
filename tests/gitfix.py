"""Helpers: a temporary bare 'remote' repository and a factory config pointing at it."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pi_factory.config import FactoryConfig, parse_config


def run(cwd, *args, check=True):
    proc = subprocess.run(
        ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if check and proc.returncode:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_remote(root: Path, name: str = "remote") -> Path:
    """A bare repository with one commit on ``main``."""
    bare = root / f"{name}.git"
    run(root, "init", "--bare", "-b", "main", "--quiet", str(bare))
    seed = root / f"{name}-seed"
    run(root, "clone", "--quiet", str(bare), str(seed))
    (seed / "README.md").write_text("hello\n")
    run(seed, "add", "-A")
    run(seed, "commit", "--quiet", "-m", "initial")
    run(seed, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    return bare


def push_to_remote(root: Path, remote: Path, filename: str, content: str, message: str) -> None:
    """Add a commit to the remote's main (simulating the base branch moving on)."""
    work = root / f"{remote.stem}-work"
    if not work.exists():
        run(root, "clone", "--quiet", str(remote), str(work))
    run(work, "pull", "--quiet", "origin", "main")
    (work / filename).write_text(content)
    run(work, "add", "-A")
    run(work, "commit", "--quiet", "-m", message)
    run(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")


def make_config(root: Path, repos: dict[str, Path | str], **repo_extra) -> FactoryConfig:
    data = {
        "linear": {"team": "THI", "owner": "owner@example.com"},
        "github": {"bot_name": "factory-bot", "bot_email": "bot@users.noreply.github.com"},
        "factory": {
            "state_root": str(root / "state"),
            "worktree_root": str(root / "worktrees"),
            "repos_root": str(root / "repos"),
            "pi_path": "/bin/true",
        },
        "repos": {
            key: {"url": str(url), "checks": ["true"], **repo_extra} for key, url in repos.items()
        },
    }
    return parse_config(data)
