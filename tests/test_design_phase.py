import pytest

from pi_factory.phases.common import Cancelled, begin_phase
from pi_factory.phases.design import ensure_heading, run_design, validate_design

GOOD = """# Design (round 1)

## Approach
Add a retry loop with backoff.

## Files likely touched
- src/fetch.ts: add retry

## Acceptance checks
- fetch retries 3 times

## Open questions
1. Should we retry on 4xx? **Recommended:** no, only 5xx.
"""


def run(env, identifier="THI-1", resume=False):
    task = env.task(identifier, "design")
    rec = begin_phase(env.deps, task, resume=resume)
    run_design(env.deps, task, rec, resume=resume)
    return task, rec


def test_well_formed_design_is_posted_and_moves_to_design_review(make_env):
    env = make_env(pi_turns=[{"text": GOOD, "usage": {"input": 50, "output": 20}}])
    env.seed_issue("THI-1", description="Please add retries.")
    env.linear.add_comment("THI-1", "Mind the rate limit.")
    task, rec = run(env)
    issue = env.linear.issue("THI-1")
    assert issue["state"] == "Design Review" and issue["assignee"] == "u-owner"
    comments = env.linear.comments("THI-1")
    posted = comments[-1]["body"]
    assert posted.startswith("# Design (round 1)") and "## Approach" in posted
    assert "pi-factory `THI-1/design-1`" in posted
    assert rec.design_comment_id == comments[-1]["id"] and rec.outcome == "designed"
    assert rec.usage["design-1"]["fake-model"]["input"] == 50
    assert env.runs.load("THI-1").design_comment_id == rec.design_comment_id
    # Designing was set before pi ran.
    assert ("THI-1", "Designing") == env.linear.transitions[0]
    assert (env.runs.run_dir("THI-1") / "posted" / "design-1.md").read_text() == posted


def test_design_uses_read_only_tools_and_trusted_session(make_env):
    env = make_env(pi_turns=[{"text": GOOD}])
    env.seed_issue("THI-1")
    run(env)
    argv = env.pi.starts()[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-approve" in argv
    assert argv[argv.index("--session-id") + 1] == "THI-1-design-1"
    assert env.pi.starts()[0]["cwd"].endswith("/pi-relay/THI-1")


def test_first_design_prompt_has_title_description_and_human_comments(make_env):
    env = make_env(pi_turns=[{"text": GOOD}])
    env.seed_issue("THI-1", title="Add retry to fetch", description="Use exponential backoff.")
    env.linear.add_comment("THI-1", "Please keep it small.")
    run(env)
    prompt = env.pi.prompts()[0]
    assert "THI-1" in prompt and "Add retry to fetch" in prompt
    assert "Use exponential backoff." in prompt and "Please keep it small." in prompt
    assert "Design (round 1)" in prompt and "`true`" in prompt  # repo checks are listed
    assert "previous design" not in prompt.lower()


def test_redesign_prompt_has_previous_design_and_marked_feedback(make_env):
    env = make_env(pi_turns=[{"text": GOOD}, {"text": GOOD.replace("round 1", "round 2")}])
    env.seed_issue("THI-1")
    env.linear.add_comment("THI-1", "early comment")
    run(env)
    env.linear.add_comment("THI-1", "Use 5 retries, not 3.")
    env.linear.move("THI-1", "Ready for Design")
    task, rec = run(env)
    assert rec.round == 2
    prompt = env.pi.prompts()[1]
    assert "Add a retry loop with backoff." in prompt  # previous design
    feedback = prompt.split("Feedback to address")[1]
    assert "Use 5 retries, not 3." in feedback and "early comment" not in feedback
    assert "early comment" in prompt.split("Feedback to address")[0]
    assert "pi-factory `THI-1/" not in prompt  # factory's own comments are not "human"
    assert env.linear.comments("THI-1")[-1]["body"].startswith("# Design (round 2)")
    assert env.linear.issue("THI-1")["state"] == "Design Review"


def test_missing_heading_is_added_by_the_factory(make_env):
    body = GOOD.replace("# Design (round 1)\n\n", "")
    env = make_env(pi_turns=[{"text": body}])
    env.seed_issue("THI-1")
    run(env)
    assert env.linear.comments("THI-1")[-1]["body"].startswith("# Design (round 1)\n\n## Approach")


def test_malformed_design_gets_one_format_fix_turn_in_the_same_session(make_env):
    bad = "# Design (round 1)\n\n## Approach\nstuff\n"
    env = make_env(pi_turns=[{"text": bad}, {"text": GOOD}])
    env.seed_issue("THI-1")
    run(env)
    assert len(env.pi.starts()) == 1 and len(env.pi.prompts()) == 2  # same live session
    assert "missing section: Files likely touched" in env.pi.prompts()[1]
    assert env.linear.issue("THI-1")["state"] == "Design Review"


def test_still_malformed_after_fix_goes_blocked_with_raw_output(make_env):
    bad = "# Design (round 1)\n\n## Approach\nstuff only\n"
    env = make_env(pi_turns=[{"text": bad}])
    env.seed_issue("THI-1")
    task, rec = run(env)
    issue = env.linear.issue("THI-1")
    assert issue["state"] == "Blocked" and issue["assignee"] == "u-owner"
    body = env.linear.comments("THI-1")[-1]["body"]
    assert "still malformed" in body and "stuff only" in body and "Ready for Design" in body
    assert rec.outcome == "blocked"
    assert len(env.pi.prompts()) == 2


def test_open_question_without_recommendation_is_rejected():
    text = GOOD.replace(" **Recommended:** no, only 5xx.", "")
    assert any("no recommended answer" in p for p in validate_design(text))
    assert validate_design(GOOD) == []
    assert (
        validate_design(
            GOOD.replace("1. Should we retry on 4xx? **Recommended:** no, only 5xx.", "None")
        )
        == []
    )
    assert any("numbered" in p for p in validate_design(GOOD.replace("1. Should", "Should")))


def test_ensure_heading_variants():
    assert ensure_heading("## Design (round 2)\nx", 2).startswith("## Design (round 2)")
    assert ensure_heading("x", 2).startswith("# Design (round 2)\n\nx")


def test_worktree_modified_during_design_is_discarded_and_blocked(make_env):
    env = make_env(pi_turns=[{"text": GOOD, "edits": {"oops.txt": "changed"}}])
    env.seed_issue("THI-1")
    task, rec = run(env)
    wt = env.git.worktree_path(task.repo, "THI-1")
    assert env.git.status(wt) == ""  # discarded
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    assert "modified files" in env.linear.comments("THI-1")[-1]["body"]
    assert not any("Design (round" in c["body"] for c in env.linear.comments("THI-1")[:-1])


def test_design_timeout_blocks_and_names_the_limit(make_env):
    import dataclasses

    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.deps.config = dataclasses.replace(
        env.cfg, design=dataclasses.replace(env.cfg.design, timeout_seconds=0.5)
    )
    env.seed_issue("THI-1")
    task, rec = run(env)
    body = env.linear.comments("THI-1")[-1]["body"]
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    assert "design timeout" in body and "0.0083 minutes" not in body


def test_provider_error_text_in_blocked_comment_and_record(make_env):
    env = make_env(pi_turns=[{"behavior": "error", "error_message": "429 quota exceeded"}])
    env.seed_issue("THI-1")
    task, rec = run(env)
    assert "429 quota exceeded" in env.linear.comments("THI-1")[-1]["body"]
    assert "429 quota exceeded" in rec.outcome_detail


def test_cancel_before_pi_posts_nothing(make_env):
    env = make_env(pi_turns=[{"text": GOOD}])
    env.seed_issue("THI-1")
    task = env.task("THI-1", "design")
    rec = begin_phase(env.deps, task)
    task.cancel()
    with pytest.raises(Cancelled):
        run_design(env.deps, task, rec)
    assert env.linear.comments("THI-1") == []
    assert env.pi.starts() == []


def test_comment_is_not_posted_twice_on_replay(make_env):
    env = make_env(pi_turns=[{"text": GOOD}])
    env.seed_issue("THI-1")
    task, rec = run(env)
    from pi_factory.phases.common import post_comment

    again = post_comment(env.deps, task.issue, rec, "design-1", "whatever")
    assert again.id == rec.design_comment_id
    assert len([c for c in env.linear.comments("THI-1") if "design-1" in c["body"]]) == 1
