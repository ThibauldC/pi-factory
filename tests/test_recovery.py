import os
import signal
import threading
import time

from pi_factory.scheduler import Scheduler, install_signal_handlers

from .test_scheduler import DESIGN_OK, wait_for

EDIT = {"edits": {"feature.txt": "done\n"}}


def interrupt(env, s, n_starts=1, step="pi_turn"):
    """Start work, wait until pi is running, then shut the scheduler down mid-turn."""
    s.poll_once()
    assert wait_for(lambda: len(env.pi.starts()) >= n_starts)
    assert wait_for(lambda: all(t.step == step for t in s.tasks.values()))
    t0 = time.monotonic()
    s.stop(join_timeout=30)
    assert time.monotonic() - t0 < 10  # exits well within systemd's 30 s


def restart(env, turns):
    env.pi.set_turns(turns)
    return Scheduler(env.deps)


def test_interrupted_build_turn_resumes_same_session_and_completes(make_env):
    env = make_env(pi_turns=[{"behavior": "hang", "edits": {"partial.txt": "half\n"}}])
    env.seed_issue(1, state="ready-for-build")
    interrupt(env, Scheduler(env.deps))
    rec = env.runs.load("pi-relay-1")
    assert rec.step == "pi_turn" and rec.phase == "build" and rec.recoveries == {}
    session_id = rec.session_ids["build-1"]
    wt = env.git.worktree_path(env.cfg.repos["pi-relay"], 1)
    assert (wt / "partial.txt").exists()  # uncommitted changes survive the restart

    s2 = restart(env, [EDIT])
    assert s2.poll_once() and s2.wait_idle(60)
    starts = env.pi.starts()
    assert [e["resumed"] for e in starts] == [False, True]
    argv = starts[1]["argv"]
    assert argv[argv.index("--session-id") + 1] == session_id
    resume_prompt = env.pi.prompts()[1]
    assert "interrupted" in resume_prompt and "git status" in resume_prompt
    assert env.github.state(1) == "in-review"
    assert (wt / "partial.txt").exists() and (wt / "feature.txt").exists()
    assert env.runs.load("pi-relay-1").recoveries == {"build-1": 1}
    assert len(env.github.open_prs()) == 1


def test_interrupted_design_resumes_and_posts_design(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    interrupt(env, Scheduler(env.deps))
    s2 = restart(env, [DESIGN_OK])
    s2.poll_once()
    assert s2.wait_idle(60)
    assert "read-only design task" in env.pi.prompts()[1]
    assert env.github.state(1) == "design-review"
    assert env.github.comments(1)[-1]["body"].startswith("# Design (round 1)")


def test_design_comment_already_posted_before_the_crash_is_adopted_without_pi(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    # Pretend the crash happened after the comment but before the state transition.
    env.github.move(1, "designing")
    rec = env.runs.load("pi-relay-1")
    rec.outcome, rec.step = "", "pi_turn"
    env.runs.save(rec)
    n_prompts = len(env.pi.prompts())
    s2 = Scheduler(env.deps)
    s2.poll_once()
    assert s2.wait_idle(30)
    assert len(env.pi.prompts()) == n_prompts  # pi not involved
    assert env.github.state(1) == "design-review"
    assert len([c for c in env.github.comments(1) if "design-1" in c["body"]]) == 1


def test_crash_during_checks_records_the_checks_step_then_checks_rerun(make_env):
    env = make_env(
        pi_turns=[EDIT],
        checks=["test -f ../ok || { touch ../ok; sleep 60; }"],
    )
    env.seed_issue(1, state="ready-for-build")
    interrupt(env, Scheduler(env.deps), step="checks")
    assert env.runs.load("pi-relay-1").step == "checks"  # the record on disk names the step
    prompts_before = len(env.pi.prompts())
    s2 = restart(env, [EDIT])
    s2.poll_once()
    assert s2.wait_idle(60)
    assert len(env.pi.prompts()) == prompts_before  # re-run without involving pi
    assert env.github.state(1) == "in-review"


def _finish_build(env):
    env.seed_issue(1, state="ready-for-build")
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(60)
    assert env.github.state(1) == "in-review"
    return env.git.worktree_path(env.cfg.repos["pi-relay"], 1)


def _pretend_crashed_at(env, step):
    env.github.move(1, "building")
    rec = env.runs.load("pi-relay-1")
    rec.step, rec.outcome, rec.recoveries = step, "", {}
    env.runs.save(rec)


def test_interrupted_during_pr_after_push_finds_existing_pr_not_a_second(make_env):
    env = make_env(pi_turns=[EDIT])
    _finish_build(env)
    _pretend_crashed_at(env, "pr")
    prompts = len(env.pi.prompts())
    s2 = Scheduler(env.deps)
    s2.poll_once()
    assert s2.wait_idle(60)
    assert len(env.github.open_prs()) == 1
    assert len(env.github.calls_to("POST", "/pulls")) == 1
    assert len(env.pi.prompts()) == prompts
    assert env.github.state(1) == "in-review"
    assert len([c for c in env.github.comments(1) if "build-1-in-review" in c["body"]]) == 1


def test_interrupted_during_push_and_commit_create_no_duplicate_commits(make_env):
    from .gitfix import run as git

    env = make_env(pi_turns=[EDIT])
    wt = _finish_build(env)
    head = git(wt, "rev-parse", "HEAD")
    for step in ("commit", "push"):
        _pretend_crashed_at(env, step)
        s = Scheduler(env.deps)
        s.poll_once()
        assert s.wait_idle(60)
        assert git(wt, "rev-parse", "HEAD") == head  # no new commit
        assert git(env.remotes["pi-relay"], "rev-parse", "factory/1-add-retry-to-fetch") == head
        assert env.github.state(1) == "in-review"
        assert len(env.github.open_prs()) == 1


def test_interrupted_setup_is_rerun(make_env):
    from pi_factory.phases.common import begin_phase

    env = make_env(pi_turns=[EDIT], setup="echo again >> ../setup-runs")
    env.seed_issue(1, state="ready-for-build")
    task = env.task(1, "build")
    rec = begin_phase(env.deps, task)
    env.runs.set_step(rec, "setup")  # crashed right after recording the step
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(60)
    wt = env.git.worktree_path(env.cfg.repos["pi-relay"], 1)
    assert (wt.parent / "setup-runs").read_text() == "again\n"
    assert env.github.state(1) == "in-review"


def test_two_interrupted_runs_are_recovered_before_any_ready_issue_starts(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}], repos=("a", "b", "c"))
    env.seed_issue(1, state="ready-for-build", repo="a")
    env.seed_issue(2, state="ready-for-build", repo="b")
    interrupt(env, Scheduler(env.deps), n_starts=2)
    env.seed_issue(3, state="ready-for-design", repo="c", labels=("priority:urgent",))  # urgent!
    s2 = restart(env, [{"behavior": "hang"}])
    s2.poll_once()
    assert wait_for(lambda: len(env.pi.starts()) == 4)
    assert sorted(s2.tasks) == ["a-1", "b-2"]  # recovered ones count toward the cap
    assert env.github.state(3, "c") == "ready-for-design"
    assert [e["resumed"] for e in env.pi.starts()[2:]] == [True, True]
    for t in list(s2.tasks.values()):
        t.cancel()
    s2.wait_idle(30)


def test_active_issue_without_run_record_is_blocked(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1, state="building")
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    assert env.github.state(1) == "blocked" and env.github.assignees(1) == ["olivia"]
    assert "no local run was found" in env.github.comments(1)[-1]["body"]
    assert env.pi.starts() == []


def test_second_interruption_of_the_same_phase_is_blocked(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, state="ready-for-build")
    interrupt(env, Scheduler(env.deps))
    s2 = restart(env, [{"behavior": "hang"}])
    interrupt(env, s2, n_starts=2)  # resumed once, interrupted again
    assert env.runs.load("pi-relay-1").recoveries == {"build-1": 1}
    s3 = restart(env, [EDIT])
    s3.poll_once()
    assert s3.wait_idle(30)
    assert env.github.state(1) == "blocked"
    assert "recovery was already used" in env.github.comments(1)[-1]["body"]
    assert len(env.pi.starts()) == 2  # no third launch
    wt = env.git.worktree_path(env.cfg.repos["pi-relay"], 1)
    assert wt.exists() and env.runs.run_dir("pi-relay-1").exists()  # kept for inspection


def test_corrupt_session_file_blocks_with_pis_error(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, state="ready-for-build")
    interrupt(env, Scheduler(env.deps))
    rec = env.runs.load("pi-relay-1")
    (session_file,) = list(__import__("pathlib").Path(rec.session_dir).glob("*.jsonl"))
    session_file.write_text("CORRUPT")
    s2 = restart(env, [EDIT])
    s2.poll_once()
    assert s2.wait_idle(30)
    assert env.github.state(1) == "blocked"
    assert "failed to load session" in env.github.comments(1)[-1]["body"]


def test_missing_session_file_blocks_and_keeps_worktree(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, state="ready-for-build")
    interrupt(env, Scheduler(env.deps))
    rec = env.runs.load("pi-relay-1")
    for f in __import__("pathlib").Path(rec.session_dir).glob("*.jsonl"):
        f.unlink()
    s2 = restart(env, [EDIT])
    s2.poll_once()
    assert s2.wait_idle(30)
    assert env.github.state(1) == "blocked"
    assert "session file" in env.github.comments(1)[-1]["body"]
    assert env.git.worktree_path(env.cfg.repos["pi-relay"], 1).exists()


def test_issue_moved_manually_to_an_active_state_is_not_resumed(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    env.github.move(1, "building")  # a human did this; nothing was interrupted
    n = len(env.pi.starts())
    s2 = Scheduler(env.deps)
    s2.poll_once()
    assert s2.wait_idle(30)
    assert len(env.pi.starts()) == n and env.github.state(1) == "blocked"


def test_worker_that_ended_after_the_poll_started_is_not_recovered(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    stale = env.tracker.list_issues([("pi-relay", 1)])  # pre-finish poll
    stale[0].labels = ["factory:designing"]
    s._finished_at["pi-relay-1"] = 100.0
    s._recover_orphans(stale, poll_started=50.0)
    assert s.tasks == {} and len(env.pi.starts()) == 1


def test_sigterm_handler_sets_stop_and_wakes_the_loop(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    s = Scheduler(env.deps)
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    install_signal_handlers(s)
    try:
        runner = threading.Thread(target=s.run_forever)
        runner.start()
        assert wait_for(lambda: env.github.calls_to("GET", r"/issues\?"))
        os.kill(os.getpid(), signal.SIGTERM)
        runner.join(10)
        assert not runner.is_alive() and s.stop_event.is_set()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])


def test_sigterm_mid_turn_then_restart_resumes_the_session(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, state="ready-for-build")
    s = Scheduler(env.deps)
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    install_signal_handlers(s)
    try:
        runner = threading.Thread(target=s.run_forever)
        runner.start()
        assert wait_for(lambda: s.tasks and next(iter(s.tasks.values())).step == "pi_turn")
        assert wait_for(lambda: env.pi.starts())
        t0 = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)  # what systemd sends
        runner.join(10)
        s.stop()  # what the service does next: close pi's stdin, leave records at their step
        assert time.monotonic() - t0 < 30 and not runner.is_alive()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    rec = env.runs.load("pi-relay-1")
    assert rec.step == "pi_turn" and rec.outcome == ""
    assert env.github.state(1) == "building"
    assert env.github.comments(1) == []
    s2 = restart(env, [EDIT])
    s2.poll_once()
    assert s2.wait_idle(60)
    assert [e["resumed"] for e in env.pi.starts()] == [False, True]
    assert env.github.state(1) == "in-review"


# --- label swaps interrupted by a crash ----------------------------------------------------------


def test_crash_during_a_label_swap_removes_the_stale_label_and_continues_the_build(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, state="ready-for-build", labels=("bug",))
    interrupt(env, Scheduler(env.deps))
    # Pretend the crash came after adding `factory:building` but before removing the old label.
    env.github.add_label(1, "factory:ready-for-build")
    rec = env.runs.load("pi-relay-1")
    rec.swap = {"from": "factory:ready-for-build", "to": "factory:building"}
    env.runs.save(rec)
    assert env.github.state(1) == "building+ready-for-build"
    s2 = restart(env, [EDIT])
    s2.poll_once()
    assert s2.wait_idle(60)
    assert env.github.state(1) == "in-review"  # repaired, resumed and finished
    assert [e["resumed"] for e in env.pi.starts()] == [False, True]
    assert "bug" in env.github.issue(1)["labels"]
    assert env.runs.load("pi-relay-1").swap is None


def test_human_made_label_conflict_is_left_alone(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1, state="design-review")
    env.github.add_label(1, "factory:ready-for-build")
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    assert env.github.state(1) == "design-review+ready-for-build"
    assert env.pi.starts() == [] and env.github.comments(1) == []
    assert [c["issue"] for c in s.snapshot()["conflicts"]] == ["pi-relay#1"]
    assert not env.github.calls_to("DELETE", "/labels/")


def test_swap_repair_never_removes_a_label_the_factory_did_not_add(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)  # design-review, run record exists
    rec = env.runs.load("pi-relay-1")
    rec.swap = {"from": "factory:designing", "to": "factory:design-review"}  # a stale marker
    env.runs.save(rec)
    env.github.add_label(1, "factory:blocked")  # a human adds an unrelated state label
    s2 = Scheduler(env.deps)
    s2.poll_once()
    assert env.github.state(1) == "design-review+blocked"  # labels differ from the swap: untouched
    assert env.runs.load("pi-relay-1").swap == rec.swap


def test_finished_swap_marker_is_cleared_when_only_the_new_label_remains(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = Scheduler(env.deps)
    s.poll_once()
    assert s.wait_idle(30)
    rec = env.runs.load("pi-relay-1")
    rec.swap = {"from": "factory:designing", "to": "factory:design-review"}  # crash after removal
    env.runs.save(rec)
    Scheduler(env.deps).poll_once()
    assert env.runs.load("pi-relay-1").swap is None and env.github.state(1) == "design-review"


def test_swap_is_recorded_in_the_run_record_before_the_label_is_added(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    seen = {}
    real = env.tracker.set_state

    def spy(issue, state):
        seen["swap"] = env.runs.load("pi-relay-1").swap
        real(issue, state)

    env.tracker.set_state = spy
    task = env.task(1, "design")
    from pi_factory.phases.common import begin_phase

    rec = begin_phase(env.deps, task)
    assert seen["swap"] == {"from": "factory:ready-for-design", "to": "factory:designing"}
    assert rec.swap is None and env.runs.load("pi-relay-1").swap is None
