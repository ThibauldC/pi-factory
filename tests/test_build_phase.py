import dataclasses

from pi_factory.phases.build import run_build
from pi_factory.phases.common import begin_phase
from pi_factory.phases.design import run_design

from .gitfix import run as git

GOOD_DESIGN = """# Design (round 1)

## Approach
Add retries with backoff to fetch.

## Files likely touched
- src/fetch.ts

## Acceptance checks
- retries happen

## Open questions
1. Retry on 4xx? **Recommended:** no.
"""


def design(env, ident="THI-1"):
    task = env.task(ident, "design")
    rec = begin_phase(env.deps, task)
    run_design(env.deps, task, rec)
    env.linear.move(ident, "Ready for Build")
    return rec


def build(env, ident="THI-1"):
    task = env.task(ident, "build")
    rec = begin_phase(env.deps, task)
    run_build(env.deps, task, rec)
    return task, rec


def setup_env(make_env, build_turns, **kw):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, *build_turns], **kw)
    env.seed_issue("THI-1", title="Add retry to fetch")
    design(env)
    return env


def remote_refs(env):
    return git(env.remotes["pi-relay"], "for-each-ref", "--format=%(refname)").splitlines()


def test_setup_runs_in_worktree_before_pi_is_launched(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"feature.txt": "x", "../pi-started": "1"}}],
        setup="test ! -e ../pi-started && echo ok > ../setup-ok && pwd > ../setup-cwd",
    )
    task, rec = build(env)
    wt = env.git.worktree_path(task.repo, "THI-1")
    assert (wt.parent / "setup-ok").exists()  # ran, and before pi created ../pi-started
    assert (wt.parent / "setup-cwd").read_text().strip() == str(wt)
    assert (env.runs.run_dir("THI-1") / "commands" / "build-1-setup.txt").exists()
    assert rec.outcome == "in_review"


def test_setup_failure_blocks_with_trimmed_output_and_never_starts_pi(make_env):
    env = setup_env(make_env, [{"edits": {"a": "1"}}], setup="echo install exploded; exit 7")
    starts_before = len(env.pi.starts())
    task, rec = build(env)
    assert len(env.pi.starts()) == starts_before  # pi never launched for the build
    body = env.linear.comments("THI-1")[-1]["body"]
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    assert "setup command" in body and "install exploded" in body and "exit code 7" in body


def test_implementer_edits_become_a_commit_after_checks_pass(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"src/retry.txt": "retry\n"}}],
        checks=["test -f src/retry.txt", "true"],
    )
    task, rec = build(env)
    wt = env.git.worktree_path(task.repo, "THI-1")
    assert (
        git(wt, "log", "-1", "--format=%an|%s") == "factory-bot|THI-1: Add retry to fetch (round 1)"
    )
    assert (wt / "src" / "retry.txt").exists() and env.git.status(wt) == ""
    assert [c["exit_code"] for c in rec.checks["1"][0]] == [0, 0]
    out = env.runs.run_dir("THI-1") / "commands" / "build-1-attempt-0-check-1.txt"
    assert out.exists()


def test_build_prompt_has_issue_design_comments_checks_and_no_commit_rule(make_env):
    env = make_env(
        pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a": "1"}}], checks=["pnpm test", "true"]
    )
    env.seed_issue("THI-1", title="Add retry to fetch", description="Use backoff please.")
    env.linear.add_comment("THI-1", "before design")
    design(env)
    env.linear.add_comment("THI-1", "Answer 1: no 4xx retries.")
    task = env.task("THI-1", "build")
    rec = begin_phase(env.deps, task)
    # checks fail on purpose (pnpm missing) -> only look at the prompt.
    run_build(env.deps, task, rec)
    prompt = env.pi.prompts()[1]
    assert "THI-1" in prompt and "Add retry to fetch" in prompt and "Use backoff please." in prompt
    assert "Add retries with backoff to fetch." in prompt  # recorded design
    assert "Answer 1: no 4xx retries." in prompt  # reply posted after the design
    assert "before design" not in prompt.split("posted after the design")[1]
    assert "`pnpm test`" in prompt and "`true`" in prompt
    assert "Do not commit" in prompt or "do not commit" in prompt.lower()
    argv = env.pi.starts()[1]["argv"]
    assert "--tools" not in argv  # build gets pi's default tools


def test_failed_check_stops_remaining_checks_and_starts_a_repair_round(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"a.txt": "1"}}, {"edits": {"fixed.txt": "1"}}],
        checks=["echo BOOM-OUTPUT; test -f fixed.txt", "touch ../second-ran"],
    )
    task, rec = build(env)
    wt = env.git.worktree_path(task.repo, "THI-1")
    assert rec.repairs_used == 1 and rec.outcome == "in_review"
    repair_prompt = env.pi.prompts()[2]
    assert "test -f fixed.txt" in repair_prompt and "BOOM-OUTPUT" in repair_prompt
    # Same live implementer session: design process + one build process, 3 prompts in total.
    assert len(env.pi.starts()) == 2
    first_attempt = rec.checks["1"][0]
    assert (
        len(first_attempt) == 1 and first_attempt[0]["exit_code"] != 0
    )  # stopped at first failure
    assert [c["exit_code"] for c in rec.checks["1"][1]] == [0, 0]
    assert (wt.parent / "second-ran").exists()  # ran only in the passing attempt
    pr = env.github.open_prs()[0]
    assert "Repair rounds: 1" in pr["body"]


def test_repairs_exhausted_blocks_and_pushes_nothing(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"a.txt": "1"}}],
        checks=["echo failing-excerpt-here; false"],
    )
    task, rec = build(env)
    assert len(env.pi.prompts()) == 1 + 1 + 2  # design + implementer + 2 repair turns
    body = env.linear.comments("THI-1")[-1]["body"]
    assert env.linear.issue("THI-1")["state"] == "Blocked" and env.linear.issue("THI-1")["assignee"]
    assert "echo failing-excerpt-here; false" in body and "failing-excerpt-here" in body
    assert "thi-1-add-retry-to-fetch" not in " ".join(remote_refs(env))
    assert env.github.open_prs() == []


def test_repair_round_limit_is_configurable(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a": "1"}}], checks=["false"])
    env.deps.config = dataclasses.replace(
        env.cfg, build=dataclasses.replace(env.cfg.build, repair_rounds=0)
    )
    env.seed_issue("THI-1")
    design(env)
    build(env)
    assert len(env.pi.prompts()) == 2  # design + implementer, no repair
    assert env.linear.issue("THI-1")["state"] == "Blocked"


def test_new_pr_with_body_sections_in_review_comment_and_only_branch_pushed(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"a.txt": "1"}, "usage": {"input": 1234, "output": 56}}],
        checks=["true", "echo ok"],
    )
    main_before = git(env.remotes["pi-relay"], "rev-parse", "refs/heads/main")
    task, rec = build(env)
    refs = remote_refs(env)
    assert sorted(refs) == ["refs/heads/main", "refs/heads/thi-1-add-retry-to-fetch"]
    assert git(env.remotes["pi-relay"], "rev-parse", "refs/heads/main") == main_before
    prs = env.github.open_prs()
    assert len(prs) == 1
    pr = prs[0]
    assert pr["title"] == "THI-1: Add retry to fetch"
    assert pr["head"] == "thi-1-add-retry-to-fetch" and pr["base"] == "main"
    body = pr["body"]
    assert "https://linear.example/THI-1" in body
    assert "Add retries with backoff to fetch." in body  # design summary
    assert "`true`" in body and "`echo ok`" in body and "passed" in body
    assert "Repair rounds: 0" in body
    assert "build-1" in body and "1,234" in body and "fake-model" in body
    issue = env.linear.issue("THI-1")
    assert issue["state"] == "In Review"
    comment = env.linear.comments("THI-1")[-1]["body"]
    assert "https://github.com/owner/pi-relay/pull/1" in comment
    assert rec.pr_number == 1 and rec.last_build_comment_at
    assert "(round 1)" in git(
        env.remotes["pi-relay"], "log", "-1", "--format=%s", "thi-1-add-retry-to-fetch"
    )
    assert rec.usage["build-1"]["fake-model"]["input"] == 1234


def test_conflicting_pr_is_noted_without_rewriting_history(make_env):
    env = setup_env(make_env, [{"edits": {"a.txt": "1"}}])
    env.github.default_mergeable = False
    task, rec = build(env)
    comment = env.linear.comments("THI-1")[-1]["body"]
    assert "Merge conflict" in comment and "never rebases" in comment
    assert env.linear.issue("THI-1")["state"] == "In Review"
    assert rec.outcome == "in_review"


def test_mergeable_unknown_then_clean_has_no_conflict_note(make_env):
    env = setup_env(make_env, [{"edits": {"a.txt": "1"}}])
    env.github.mergeable = [None, True]
    build(env)
    assert "Merge conflict" not in env.linear.comments("THI-1")[-1]["body"]


def test_no_changes_at_all_blocks(make_env):
    env = setup_env(make_env, [{}])
    task, rec = build(env)
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    assert "no changes" in env.linear.comments("THI-1")[-1]["body"]
    assert env.github.open_prs() == []


def test_agent_commits_are_kept_with_factory_commit_on_top(make_env):
    env = setup_env(
        make_env,
        [
            {"edits": {"a.txt": "1"}},
        ],
        checks=["true"],
    )
    task = env.task("THI-1", "build")
    wt = env.git.ensure_worktree(task.repo, "THI-1", task.issue.branch_name)
    (wt / "early.txt").write_text("agent made this")
    git(wt, "add", "-A")
    git(wt, "commit", "--quiet", "-m", "agent commit")
    rec = begin_phase(env.deps, task)
    run_build(env.deps, task, rec)
    subjects = git(wt, "log", "--format=%s", "-3").splitlines()
    assert subjects[0].startswith("THI-1:") and subjects[1] == "agent commit"


def test_continuation_after_review_adds_commit_to_same_pr(make_env):
    env = setup_env(
        make_env,
        [{"edits": {"flag.txt": "--retries\n"}}, {"edits": {"flag.txt": "--retry\n"}}],
    )
    task1, rec1 = build(env)
    assert env.linear.issue("THI-1")["state"] == "In Review"
    env.linear.add_comment("THI-1", "rename the flag to --retry")
    env.linear.move("THI-1", "Ready for Build")
    task2, rec2 = build(env)
    assert rec2.round == 2
    prompt = env.pi.prompts()[-1]
    assert "rename the flag to --retry" in prompt and "continuation" in prompt
    assert "before design" not in prompt
    # same PR, updated; new commit on the same branch
    assert len(env.github.open_prs()) == 1 and rec2.pr_number == rec1.pr_number
    wt = env.git.worktree_path(task2.repo, "THI-1")
    subjects = git(wt, "log", "--format=%s", "-2").splitlines()
    assert subjects == [
        "THI-1: Add retry to fetch (round 2)",
        "THI-1: Add retry to fetch (round 1)",
    ]
    assert env.github.prs[("owner/pi-relay", 1)]["body"].count("build round 2") == 1
    assert env.linear.issue("THI-1")["state"] == "In Review"
    # the earlier round's session id differs from the new one
    argvs = [s["argv"] for s in env.pi.starts()]
    ids = [a[a.index("--session-id") + 1] for a in argvs]
    assert ids == ["THI-1-design-1", "THI-1-build-1", "THI-1-build-2"]
    assert (env.runs.run_dir("THI-1") / "posted" / "pr-body-round-2.md").exists()


def test_continuation_ignores_comments_older_than_last_build(make_env):
    env = setup_env(make_env, [{"edits": {"a": "1"}}, {"edits": {"b": "1"}}])
    build(env)
    env.linear.add_comment("THI-1", "LATE-FEEDBACK")
    env.linear.move("THI-1", "Ready for Build")
    build(env)
    assert "LATE-FEEDBACK" in env.pi.prompts()[-1]


def test_cancel_during_checks_stops_without_posting(make_env):
    import threading

    env = setup_env(make_env, [{"edits": {"a": "1"}}], checks=["sleep 30"])
    task = env.task("THI-1", "build")
    rec = begin_phase(env.deps, task)
    comments_before = len(env.linear.comments("THI-1"))
    threading.Timer(1.0, task.cancel).start()
    import pytest

    from pi_factory.phases.common import Cancelled

    with pytest.raises(Cancelled):
        run_build(env.deps, task, rec)
    assert len(env.linear.comments("THI-1")) == comments_before
    assert env.github.open_prs() == []
    assert env.git.worktree_path(task.repo, "THI-1").exists()  # worktree kept


def test_check_timeout_counts_as_failure(make_env):
    env = setup_env(make_env, [{"edits": {"a": "1"}}], checks=["sleep 30"])
    env.deps.config = dataclasses.replace(
        env.cfg,
        build=dataclasses.replace(env.cfg.build, command_timeout_seconds=0.5, repair_rounds=0),
    )
    task, rec = build(env)
    assert rec.checks["1"][0][0]["timed_out"] is True
    assert "timed out" in env.linear.comments("THI-1")[-1]["body"]
