"""Git operations: bare cached clones, per-issue worktrees, bot commits, scoped pushes (D5).

The GitHub token reaches git only through an inline credential helper that reads it from an
environment variable set on that one git child process. It is never written into a remote
URL, ``.git/config`` or a worktree file. Factory git commands also run with hooks disabled,
so nothing planted in the shared repository can run with the token in its environment.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import logs
from .childenv import build_child_env
from .config import FactoryConfig, RepoConfig
from .secrets import RedactedError

TOKEN_ENV = "PI_FACTORY_GIT_TOKEN"
# Reads the token from the environment at call time; contains no secret itself.
CREDENTIAL_HELPER = f'!f() {{ echo username=x-access-token; echo "password=${TOKEN_ENV}"; }}; f'
GIT_TIMEOUT = 600.0


class GitError(RedactedError):
    pass


class GitOps:
    def __init__(
        self,
        config: FactoryConfig,
        token: Callable[[], str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ):
        self.config = config
        self._token = token
        self._run = runner or subprocess.run

    # -- paths -----------------------------------------------------------------------------
    def bare_path(self, repo: RepoConfig) -> Path:
        return self.config.repos_root / f"{repo.key}.git"

    def worktree_path(self, repo: RepoConfig, issue: str) -> Path:
        return self.config.worktree_root / repo.key / issue

    # -- running git -----------------------------------------------------------------------
    def _env(self, with_token: bool) -> dict[str, str]:
        env = build_child_env()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if with_token and self._token is not None:
            env[TOKEN_ENV] = self._token()
        return env

    def git(
        self,
        cwd: Path | None,
        *args: str,
        network: bool = False,
        identity: bool = False,
        check: bool = True,
        timeout: float = GIT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-c", "core.hooksPath=/dev/null"]
        if network:
            cmd += ["-c", "credential.helper=", "-c", f"credential.helper={CREDENTIAL_HELPER}"]
        if identity:
            cmd += [
                "-c",
                f"user.name={self.config.github.bot_name}",
                "-c",
                f"user.email={self.config.github.bot_email}",
            ]
        cmd += list(args)
        try:
            proc = self._run(
                cmd,
                cwd=cwd,
                env=self._env(network),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise GitError(f"cannot run git: {exc.strerror or exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise GitError(f"git {args[0]} failed ({proc.returncode}): {detail}")
        return proc

    # -- clone cache -----------------------------------------------------------------------
    def ensure_clone(self, repo: RepoConfig) -> Path:
        """Create the bare cache if needed, then fetch every remote branch into
        ``refs/remotes/origin/*``."""
        bare = self.bare_path(repo)
        if not (bare / "HEAD").exists():
            # init + remote add (not `clone --bare`) so no stale local heads are copied: remote
            # branches live only in refs/remotes/origin/*, local heads are the issue branches.
            bare.parent.mkdir(parents=True, exist_ok=True)
            self.git(None, "init", "--bare", "--quiet", str(bare))
            self.git(bare, "remote", "add", "origin", repo.url)
        self.fetch(repo)
        return bare

    def fetch(self, repo: RepoConfig) -> None:
        self.git(self.bare_path(repo), "fetch", "--quiet", "--prune", "origin", network=True)

    def has_ref(self, bare: Path, ref: str) -> bool:
        return self.git(bare, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0

    # -- worktrees -------------------------------------------------------------------------
    def ensure_worktree(self, repo: RepoConfig, issue: str, branch: str) -> Path:
        """Return the issue's worktree, creating it (and the branch) when needed."""
        bare = self.ensure_clone(repo)
        wt = self.worktree_path(repo, issue)
        base_ref = f"refs/remotes/origin/{repo.base_branch}"
        if not self.has_ref(bare, base_ref):
            raise GitError(f"base branch '{repo.base_branch}' not found in {repo.url}")
        if (wt / ".git").exists():
            self._advance_untouched_branch(wt, base_ref)
            return wt
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(bare, "worktree", "prune")
        if self.has_ref(bare, f"refs/heads/{branch}"):
            self.git(bare, "worktree", "add", "--quiet", str(wt), branch)
        elif self.has_ref(bare, f"refs/remotes/origin/{branch}"):
            self.git(bare, "worktree", "add", "--quiet", "-b", branch, str(wt), f"origin/{branch}")
        else:
            self.git(bare, "worktree", "add", "--quiet", "-b", branch, str(wt), base_ref)
        logs.event("worktree_ready", issue, path=wt, branch=branch)
        return wt

    def _advance_untouched_branch(self, wt: Path, base_ref: str) -> None:
        """A branch with no commits of its own and a clean tree follows the latest base."""
        ahead = self.git(wt, "rev-list", "--count", f"{base_ref}..HEAD").stdout.strip()
        if ahead == "0" and not self.status(wt):
            self.git(wt, "reset", "--hard", "--quiet", base_ref)

    def remove_worktree(self, repo: RepoConfig, issue: str, branch: str) -> None:
        bare = self.bare_path(repo)
        wt = self.worktree_path(repo, issue)
        if bare.exists():
            self.git(bare, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
        if bare.exists():
            self.git(bare, "worktree", "prune", check=False)
            if branch:
                self.git(bare, "branch", "-D", branch, check=False)

    # -- working tree state ----------------------------------------------------------------
    def status(self, wt: Path) -> str:
        return self.git(wt, "status", "--porcelain").stdout.strip()

    def discard_changes(self, wt: Path) -> None:
        self.git(wt, "reset", "--hard", "--quiet")
        self.git(wt, "clean", "-fdq")

    def head(self, wt: Path) -> str:
        return self.git(wt, "rev-parse", "HEAD").stdout.strip()

    def commits_ahead(self, wt: Path, repo: RepoConfig) -> int:
        base = f"refs/remotes/origin/{repo.base_branch}"
        return int(self.git(wt, "rev-list", "--count", f"{base}..HEAD").stdout.strip() or 0)

    # -- commit and push -------------------------------------------------------------------
    def commit_all(self, wt: Path, message: str) -> bool:
        """Commit every change as the bot. Returns False when there was nothing to commit
        (which also makes replaying a crashed commit step safe)."""
        if not self.status(wt):
            return False
        self.git(wt, "add", "-A")
        self.git(wt, "commit", "--quiet", "--no-gpg-sign", "-m", message, identity=True)
        return True

    def push_branch(self, repo: RepoConfig, wt: Path, branch: str) -> None:
        """Push the issue branch. Only that ref is named; the base branch is refused and
        nothing is ever forced."""
        if branch == repo.base_branch or not branch or branch.startswith("-"):
            raise GitError(f"refusing to push '{branch}': never push to the base branch")
        ref = f"refs/heads/{branch}"
        self.git(wt, "push", "--quiet", "origin", f"{ref}:{ref}", network=True)
