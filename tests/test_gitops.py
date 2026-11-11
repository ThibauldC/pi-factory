import subprocess

import pytest

from pi_factory.gitops import CREDENTIAL_HELPER, TOKEN_ENV, GitError, GitOps

from .gitfix import make_config, make_remote, push_to_remote, run

TOKEN = "ghp_supersecrettoken123"


@pytest.fixture
def env(tmp_path):
    remote = make_remote(tmp_path)
    cfg = make_config(tmp_path, {"pi-relay": remote})
    ops = GitOps(cfg, token=lambda: TOKEN)
    return tmp_path, remote, cfg, ops, cfg.repos["pi-relay"]


def test_first_build_creates_worktree_on_new_branch_from_base(env):
    tmp, remote, cfg, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-123", "thi-123-add-retry")
    assert wt == cfg.worktree_root / "pi-relay" / "THI-123"
    assert run(wt, "rev-parse", "--abbrev-ref", "HEAD") == "thi-123-add-retry"
    assert (wt / "README.md").read_text() == "hello\n"
    assert ops.commits_ahead(wt, repo) == 0 and ops.status(wt) == ""
    assert ops.ensure_worktree(repo, "THI-123", "thi-123-add-retry") == wt  # idempotent


def test_two_repositories_get_separate_worktrees(tmp_path):
    cfg = make_config(
        tmp_path, {"a": make_remote(tmp_path, "ra"), "b": make_remote(tmp_path, "rb")}
    )
    ops = GitOps(cfg)
    wa = ops.ensure_worktree(cfg.repos["a"], "THI-1", "thi-1-x")
    wb = ops.ensure_worktree(cfg.repos["b"], "THI-2", "thi-2-y")
    assert wa != wb and not str(wa).startswith(str(wb)) and not str(wb).startswith(str(wa))


def test_missing_base_branch_is_an_error(tmp_path):
    remote = make_remote(tmp_path)
    cfg = make_config(tmp_path, {"r": remote}, base_branch="develop")
    with pytest.raises(GitError, match="develop"):
        GitOps(cfg).ensure_worktree(cfg.repos["r"], "THI-1", "thi-1-x")


def test_commit_is_authored_by_bot_with_issue_message(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-123", "thi-123-add-retry")
    (wt / "a.txt").write_text("x\n")
    assert ops.commit_all(wt, "THI-123: Add retry to fetch (round 1)") is True
    author, committer, subject = run(wt, "log", "-1", "--format=%an <%ae>|%cn|%s").split("|")
    assert author == "factory-bot <bot@users.noreply.github.com>" and committer == "factory-bot"
    assert subject == "THI-123: Add retry to fetch (round 1)"
    assert ops.commits_ahead(wt, repo) == 1
    assert ops.commit_all(wt, "again") is False  # nothing to commit: replay is safe
    assert ops.commits_ahead(wt, repo) == 1


def test_agent_commits_are_kept_and_factory_commits_on_top(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "agent.txt").write_text("by agent\n")
    run(wt, "add", "-A")
    run(wt, "commit", "--quiet", "-m", "agent's own commit")
    (wt / "rest.txt").write_text("leftover\n")
    assert ops.commit_all(wt, "THI-1: x (round 1)")
    subjects = run(wt, "log", "--format=%s", "-3").splitlines()
    assert subjects[:2] == ["THI-1: x (round 1)", "agent's own commit"]


def test_push_updates_only_the_issue_branch(env):
    tmp, remote, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "f.txt").write_text("1\n")
    ops.commit_all(wt, "THI-1: x (round 1)")
    before = run(remote, "for-each-ref", "--format=%(refname) %(objectname)")
    ops.push_branch(repo, wt, "thi-1-x")
    after = run(remote, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    changed = set(after) - set(before.splitlines())
    assert len(changed) == 1 and next(iter(changed)).startswith("refs/heads/thi-1-x ")
    assert [line for line in after if line.startswith("refs/heads/main")] == [
        line for line in before.splitlines() if line.startswith("refs/heads/main")
    ]


def test_push_of_base_branch_is_refused(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    with pytest.raises(GitError, match="never push to the base branch"):
        ops.push_branch(repo, wt, "main")


def test_token_never_stored_in_config_remote_or_worktree(env):
    tmp, remote, cfg, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "f.txt").write_text("1\n")
    ops.commit_all(wt, "THI-1: x (round 1)")
    ops.push_branch(repo, wt, "thi-1-x")
    bare = ops.bare_path(repo)
    assert TOKEN not in (bare / "config").read_text()
    assert TOKEN not in run(wt, "remote", "-v")
    assert TOKEN not in (wt / ".git").read_text()
    for path in wt.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert TOKEN not in path.read_text(errors="ignore")


def test_credential_helper_reads_token_from_env_only():
    out = subprocess.run(
        ["sh", "-c", CREDENTIAL_HELPER[1:]],
        env={TOKEN_ENV: TOKEN, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )  # fmt: skip
    assert out.stdout.splitlines() == ["username=x-access-token", f"password={TOKEN}"]
    assert TOKEN not in CREDENTIAL_HELPER


def test_token_given_only_to_network_commands_and_not_on_the_command_line(env):
    _, _, cfg, _, repo = env
    seen = []

    def spy(cmd, **kw):
        seen.append((cmd, kw["env"]))
        return subprocess.run(cmd, **kw)

    ops = GitOps(cfg, token=lambda: TOKEN, runner=spy)
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "f").write_text("1")
    ops.commit_all(wt, "m")
    ops.push_branch(repo, wt, "thi-1-x")
    assert seen
    for cmd, e in seen:
        assert TOKEN not in " ".join(cmd)
        network = any(c.startswith("credential.helper=!") for c in cmd)
        assert (TOKEN_ENV in e) == network
        assert not [k for k in e if k.startswith("PI_FACTORY_") and k != TOKEN_ENV]
    assert any(TOKEN_ENV in e for _, e in seen)


def test_hooks_in_the_shared_repo_never_run(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    hooks = ops.bare_path(repo) / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "post-commit", "pre-push"):
        hook = hooks / name
        hook.write_text("#!/bin/sh\ntouch /tmp/pi-factory-hook-ran-$$\nexit 1\n")
        hook.chmod(0o755)
    (wt / "f").write_text("1")
    assert ops.commit_all(wt, "m")
    ops.push_branch(repo, wt, "thi-1-x")


def test_fetch_picks_up_new_base_and_untouched_branch_follows(env):
    tmp, remote, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    push_to_remote(tmp, remote, "new.txt", "n", "base moves on")
    wt2 = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    assert wt2 == wt and (wt / "new.txt").exists()  # no own commits: follows the base


def test_branch_with_own_commits_is_not_moved(env):
    tmp, remote, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "mine.txt").write_text("m")
    ops.commit_all(wt, "mine")
    push_to_remote(tmp, remote, "new.txt", "n", "base moves on")
    ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    assert not (wt / "new.txt").exists() and (wt / "mine.txt").exists()


def test_existing_remote_branch_is_checked_out_when_worktree_is_missing(env):
    tmp, remote, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "f").write_text("1")
    ops.commit_all(wt, "m")
    ops.push_branch(repo, wt, "thi-1-x")
    ops.remove_worktree(repo, "THI-1", "thi-1-x")
    assert not wt.exists()
    wt2 = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    assert (wt2 / "f").read_text() == "1"


def test_remove_worktree_deletes_dir_and_local_branch(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    ops.remove_worktree(repo, "THI-1", "thi-1-x")
    assert not wt.exists()
    assert "thi-1-x" not in run(ops.bare_path(repo), "branch", "--list")
    ops.remove_worktree(repo, "THI-1", "thi-1-x")  # idempotent


def test_discard_changes_cleans_tracked_and_untracked(env):
    _, _, _, ops, repo = env
    wt = ops.ensure_worktree(repo, "THI-1", "thi-1-x")
    (wt / "README.md").write_text("changed")
    (wt / "junk.txt").write_text("junk")
    assert ops.status(wt)
    ops.discard_changes(wt)
    assert ops.status(wt) == ""


def test_git_errors_never_contain_the_token(env):
    tmp, _, cfg, ops, repo = env
    from pi_factory.secrets import register_secret

    register_secret(TOKEN)
    bad = make_config(tmp, {"x": tmp / "does-not-exist.git"})
    with pytest.raises(GitError) as exc:
        GitOps(bad, token=lambda: TOKEN).ensure_clone(bad.repos["x"])
    assert TOKEN not in str(exc.value)
