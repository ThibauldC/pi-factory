import dataclasses
import logging
import subprocess
import threading
import time

import pytest

from pi_factory import logs
from pi_factory.scheduler import MAX_BACKOFF, Scheduler, StartupError, backoff_delay

from .test_build_phase import GOOD_DESIGN
from .test_design_phase import GOOD

DESIGN_OK = {"text": GOOD}


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def sched(env, **kw):
    return Scheduler(env.deps, **kw)


def tick(env, s):
    assert s.poll_once()
    assert s.wait_idle(60)


def issue_list_calls(env):
    return env.github.calls_to("GET", r"/issues\?")


# --- work-intake ---------------------------------------------------------------------------


def test_poll_on_schedule_no_pi_process_when_nothing_startable(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1, state=None)
    env.seed_issue(2, state="design-review")
    env.seed_issue(3, state="in-review")
    s = sched(env)
    tick(env, s)
    assert env.pi.starts() == [] and s.tasks == {}
    assert len(issue_list_calls(env)) == 4  # one list per polled label for the one repository
    assert s.next_delay() == 120


def test_poll_requests_are_bounded_by_repos_times_labels_plus_tracked_issues(make_env):
    env = make_env(pi_turns=[DESIGN_OK], repos=("pi-relay", "other"))
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)  # now Design Review, tracked through its run record
    env.github.calls.clear()
    assert s.poll_once()
    assert len(issue_list_calls(env)) == 2 * 4
    assert len(env.github.calls_to("GET", r"/issues/\d+$")) == 1  # the tracked issue
    assert env.github.calls_to("GET", r"/comments") == []  # comments only when a phase starts


def test_issue_is_queued_for_the_repository_it_lives_in_and_runs_design(make_env):
    env = make_env(pi_turns=[DESIGN_OK], repos=("pi-relay", "other"))
    env.seed_issue(1, repo="other")
    s = sched(env)
    tick(env, s)
    assert env.github.state(1, "other") == "design-review"
    assert env.pi.starts()[0]["cwd"].endswith("worktrees/other/1")
    assert env.runs.list_issues() == ["other-1"]


def test_issues_of_unconfigured_repositories_are_never_seen(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.github.repos["owner/stranger"] = True
    env.github.add_issue(1, repo="stranger")
    s = sched(env)
    tick(env, s)
    assert env.github.state(1, "stranger") == "ready-for-design"
    assert env.github.calls_to("GET", "stranger") == [] and env.pi.starts() == []


def test_pull_request_with_a_factory_label_is_ignored(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.github.add_pull_request(labels=("factory:ready-for-design",))
    s = sched(env)
    tick(env, s)
    assert env.pi.starts() == [] and env.runs.list_issues() == []


def test_issue_with_two_state_labels_is_ignored_logged_and_shown(make_env, caplog):
    env = make_env(pi_turns=[DESIGN_OK, DESIGN_OK])
    env.seed_issue(1, state="design-review")
    env.github.add_label(1, "factory:ready-for-build")
    env.seed_issue(2)
    s = sched(env)
    caplog.set_level(logging.INFO, logger="pi_factory")
    logs.log.propagate = True
    try:
        tick(env, s)
        tick(env, s)
    finally:
        logs.log.propagate = False
    assert [e["cwd"].rsplit("/", 1)[1] for e in env.pi.starts()] == ["2"]  # only #2 started
    assert env.github.state(1) == "design-review+ready-for-build"
    assert [c["issue"] for c in s.snapshot()["conflicts"]] == ["pi-relay#1"]
    lines = [r.getMessage() for r in caplog.records if "conflicting_state_labels" in r.getMessage()]
    assert len(lines) == 1 and "pi-relay#1" in lines[0]  # logged once, not on every poll


def test_same_repo_busy_second_issue_waits(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    env.seed_issue(2)
    s = sched(env)
    assert s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    assert list(s.tasks) == ["pi-relay-1"]  # same priority -> the older issue first
    assert env.github.state(2) == "ready-for-design"
    s.poll_once()
    assert list(s.tasks) == ["pi-relay-1"] and env.github.state(2) == "ready-for-design"
    for t in list(s.tasks.values()):
        t.cancel()
    s.wait_idle(30)


def test_global_cap_keeps_third_repo_queued_until_a_slot_frees(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}], repos=("a", "b", "c"))
    for repo in ("a", "b", "c"):
        env.seed_issue(1, repo=repo)
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: len(env.pi.starts()) == 2)
    assert sorted(s.tasks) == ["a-1", "b-1"]  # cap of 2
    s.poll_once()
    assert sorted(s.tasks) == ["a-1", "b-1"]
    assert env.github.state(1, "c") == "ready-for-design"
    # free a slot: a human takes a-1 out of Designing; the third repository is served next
    env.github.move(1, None, "a")
    env.pi.set_turns([DESIGN_OK])
    s.poll_once()
    assert wait_for(lambda: "a-1" not in s.tasks)
    s.poll_once()
    assert wait_for(lambda: env.github.state(1, "c") == "design-review")
    for t in list(s.tasks.values()):
        t.cancel()
    assert s.wait_idle(30)


def test_priority_wins_over_age_and_ties_by_age(make_env):
    env = make_env(pi_turns=[DESIGN_OK, DESIGN_OK, DESIGN_OK], max_concurrent=1)
    env.seed_issue(1, labels=("priority:low",), created_at="2026-01-01T00:00:00Z")  # Low, older
    env.seed_issue(2, labels=("priority:urgent",), created_at="2026-02-01T00:00:00Z")  # Urgent
    env.seed_issue(3, labels=("priority:low",), created_at="2025-12-01T00:00:00Z")  # Low, oldest
    s = sched(env)
    order = []
    for _ in range(3):
        before = {n for n in (1, 2, 3) if env.github.state(n) != "ready-for-design"}
        tick(env, s)
        after = {n for n in (1, 2, 3) if env.github.state(n) != "ready-for-design"}
        order += sorted(after - before)
    assert order == [2, 3, 1]


def test_highest_of_several_priority_labels_counts(make_env):
    env = make_env(pi_turns=[DESIGN_OK], max_concurrent=1)
    env.seed_issue(1, labels=("priority:high",), created_at="2025-01-01T00:00:00Z")
    env.seed_issue(2, labels=("priority:low", "priority:urgent"), created_at="2026-01-01T00:00:00Z")
    tick(env, sched(env))
    assert env.github.state(2) == "design-review" and env.github.state(1) == "ready-for-design"


def test_no_priority_label_sorts_last(make_env):
    env = make_env(pi_turns=[DESIGN_OK], max_concurrent=1)
    env.seed_issue(1, created_at="2025-01-01T00:00:00Z")  # no priority label, but older
    env.seed_issue(2, labels=("priority:low",), created_at="2026-01-01T00:00:00Z")
    s = sched(env)
    tick(env, s)
    assert env.github.state(2) == "design-review"
    assert env.github.state(1) == "ready-for-design"


def test_repositories_served_by_oldest_waiting_issue(make_env):
    env = make_env(pi_turns=[DESIGN_OK], repos=("a", "b"))
    env.seed_issue(2, repo="a", labels=("priority:low",))  # low priority, repo a
    env.seed_issue(3, repo="b", labels=("priority:urgent",))  # urgent, repo b
    s = sched(env)
    issues = env.tracker.list_issues()
    s._ready_since.update({"a-2": 5.0, "b-3": 9.0})  # a has been waiting longer
    assert [i.key for i in s._ordered_candidates(issues)] == ["a-2", "b-3"]
    s._ready_since.update({"a-2": 9.0, "b-3": 5.0})
    assert [i.key for i in s._ordered_candidates(issues)] == ["b-3", "a-2"]
    assert [q["priority"] for q in s.queue_snapshot] == ["priority:urgent", "priority:low"]


# --- backoff -------------------------------------------------------------------------------


def test_backoff_is_longer_than_interval_and_capped():
    assert backoff_delay(120, 0) == 120
    delays = [backoff_delay(120, n) for n in (1, 2, 3, 4, 10)]
    assert all(120 < d <= MAX_BACKOFF for d in delays[:3]) and delays[-1] == MAX_BACKOFF
    assert delays == sorted(delays)


def test_github_outage_backs_off_and_recovers(make_env, caplog):
    env = make_env(pi_turns=[DESIGN_OK])
    s = sched(env)
    caplog.set_level(logging.INFO, logger="pi_factory")
    logs.log.propagate = True
    try:
        env.github.fail_next = [500] * 6
        for _ in range(3):
            assert s.poll_once() is False
        assert 120 < s.next_delay() <= MAX_BACKOFF
        assert len(s.poll_errors) == 3 and s.last_poll_ok is False
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "poll_error" in text
        env.github.fail_next = []
        assert s.poll_once() is True
        assert s.next_delay() == 120 and s.failures == 0
        assert "poll_recovered" in " ".join(r.getMessage() for r in caplog.records)
    finally:
        logs.log.propagate = False


def test_rate_limit_counts_as_a_failed_poll_with_backoff_and_logs_the_reset(make_env, caplog):
    env = make_env(pi_turns=[DESIGN_OK])
    s = sched(env)
    caplog.set_level(logging.INFO, logger="pi_factory")
    logs.log.propagate = True
    try:
        env.github.rate_limit_next = [
            {
                "status": 403,
                "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"},
            }
        ]
        assert s.poll_once() is False
        assert s.failures == 1 and s.next_delay() > 120
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "poll_error" in text and "2030-01-01" in text and "ghp_bottoken" not in text
        assert s.poll_once() is True and s.next_delay() == 120
    finally:
        logs.log.propagate = False


def test_running_tasks_continue_during_an_outage(make_env):
    env = make_env(pi_turns=[{"sleep": 1.5, "text": GOOD}])
    env.seed_issue(1)
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())  # the worker is now mid-turn
    env.github.fail_next = [500]
    assert s.poll_once() is False  # a failing poll must not disturb it
    assert s.wait_idle(30)
    assert env.github.state(1) == "design-review"


# --- task-lifecycle ------------------------------------------------------------------------


def test_comment_does_not_trigger_work(make_env):
    env = make_env(pi_turns=[DESIGN_OK, {"edits": {"a": "1"}}])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    assert env.github.state(1) == "design-review"
    env.github.add_comment(1, "go ahead")
    n = len(env.pi.prompts())
    tick(env, s)
    assert len(env.pi.prompts()) == n and env.github.state(1) == "design-review"


def test_label_change_triggers_build_and_full_flow_to_in_review(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue(1, labels=("bug", "priority:high"))
    s = sched(env)
    tick(env, s)
    assert env.github.state(1) == "design-review" and env.github.assignees(1) == ["olivia"]
    env.github.move(1, "ready-for-build")
    tick(env, s)
    assert env.github.state(1) == "in-review"
    # PR open, issue In Review: no further action
    n = len(env.pi.prompts())
    tick(env, s)
    tick(env, s)
    assert len(env.pi.prompts()) == n and len(env.github.open_prs()) == 1
    # the factory made exactly the owned transitions and left other labels alone
    assert [t[1] for t in env.github.transitions] == [
        "factory:designing", "factory:design-review", "factory:building", "factory:in-review",
    ]  # fmt: skip
    assert {"bug", "priority:high"} <= set(env.github.issue(1)["labels"])


def test_factory_never_closes_or_reopens_issues(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    env.github.move(1, "ready-for-build")
    tick(env, s)
    assert env.github.issue(1)["state"] == "open"
    assert not [c for c in env.github.calls if c[0] == "PATCH" and "/issues/" in c[1]]


def test_redesign_runs_new_round_with_previous_design(make_env):
    env = make_env(pi_turns=[DESIGN_OK, {"text": GOOD.replace("round 1", "round 2")}])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    env.github.add_comment(1, "please reconsider")
    env.github.move(1, "ready-for-design")
    tick(env, s)
    assert env.github.comments(1)[-1]["body"].startswith("# Design (round 2)")
    assert "please reconsider" in env.pi.prompts()[1]


def test_retry_after_blocked_treated_like_any_ready_issue(make_env):
    env = make_env(pi_turns=[{"behavior": "error"}, DESIGN_OK])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    assert env.github.state(1) == "blocked"
    env.github.move(1, "ready-for-design")
    tick(env, s)
    assert env.github.state(1) == "design-review"


def test_bot_without_push_access_blocks_with_comment(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.github.repos["owner/pi-relay"] = False  # read-only collaborator
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    assert env.github.state(1) == "blocked"
    assert "lacks push access" in env.github.comments(1)[-1]["body"]
    assert env.pi.starts() == []


def test_low_disk_blocks_before_creating_a_worktree(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.disk_free = lambda path: 1 * 1024**3
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    assert env.github.state(1) == "blocked"
    body = env.github.comments(1)[-1]["body"]
    assert "free disk" in body and "2.0 GiB" in body
    assert not env.worktree(1).exists()


def test_enough_disk_proceeds(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.disk_free = lambda path: 50 * 1024**3
    env.seed_issue(1)
    tick(env, sched(env))
    assert env.github.state(1) == "design-review"


# --- cancellation and cleanup -----------------------------------------------------------------


def test_label_removed_mid_build_stops_everything_posts_nothing_keeps_worktree(make_env):
    env = make_env(
        pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}],
        checks=["sleep 60"],
    )
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    env.github.move(1, "ready-for-build")
    assert s.poll_once()
    assert wait_for(lambda: s.tasks and s.tasks["pi-relay-1"].step == "checks")
    comments_before = len(env.github.comments(1))
    env.github.move(1, None)  # a human removes every factory:* label
    s.poll_once()  # within one poll interval
    assert s.wait_idle(30)
    assert len(env.github.comments(1)) == comments_before
    assert env.github.open_prs() == []
    assert env.worktree(1).exists()
    assert env.github.state(1) == ""  # the factory did not touch it
    assert subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True).returncode != 0


def test_label_replaced_mid_design_cancels_the_run(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    env.github.move(1, "blocked")  # replaced, not removed
    s.poll_once()
    assert s.wait_idle(30)
    assert env.github.comments(1) == [] and env.github.state(1) == "blocked"


def test_closed_as_not_planned_mid_design_stops_run_then_cleans_up(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    wt = env.worktree(1)
    assert wt.exists()
    env.github.close(1, "not_planned")
    s.poll_once()
    assert s.wait_idle(30)
    assert env.github.comments(1) == []
    s.poll_once()  # cleanup rules for closed issues
    assert not wt.exists()
    assert env.runs.list_issues() == [] and len(list(env.cfg.archive_dir.iterdir())) == 1
    archived = env.runs.recent_archived()[0]
    assert archived.close_reason == "not_planned"


def test_merged_pr_closes_the_issue_and_next_poll_cleans_up(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    env.github.move(1, "ready-for-build")
    tick(env, s)
    wt = env.worktree(1)
    assert wt.exists() and env.runs.list_issues() == ["pi-relay-1"]
    (pr,) = env.github.open_prs()
    assert env.github.merge_pr(pr["number"]) == [1]  # GitHub's `Closes #1` on merge
    tick(env, s)
    assert not wt.exists()
    assert env.runs.list_issues() == []
    archived = list(env.cfg.archive_dir.iterdir())
    assert len(archived) == 1 and (archived[0] / "run.json").exists()
    assert env.runs.recent_archived()[0].close_reason == "completed"
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=env.git.bare_path(env.cfg.repos["pi-relay"]),
        capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert "factory/1-add-retry-to-fetch" not in branches


def test_issue_closed_by_hand_as_not_planned_is_cleaned_up_with_that_reason(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    s = sched(env)
    tick(env, s)
    assert env.runs.list_issues() == ["pi-relay-1"] and env.worktree(1).exists()
    env.github.close(1, "not_planned")
    tick(env, s)
    assert not env.worktree(1).exists() and env.runs.list_issues() == []
    assert env.runs.recent_archived()[0].close_reason == "not_planned"


def test_retention_sweep_runs_daily(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    now = [0.0]
    s = sched(env, clock=lambda: now[0])
    called = []
    s.retention_sweep = lambda: called.append(1)
    s.poll_once()
    assert called == []
    now[0] = 25 * 3600
    s.poll_once()
    assert called == [1]


# --- startup (9.3) ---------------------------------------------------------------------------


def test_missing_label_at_startup_is_reported_and_blocks_polling(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.github.labels_of("owner/pi-relay").remove("factory:design-review")
    s = sched(env)
    problems = s.startup_check()
    assert any("owner/pi-relay" in p and "factory:design-review" in p for p in problems)
    with pytest.raises(StartupError, match="factory:design-review"):
        s.require_startup()
    assert issue_list_calls(env) == []  # never polled


def test_startup_ok_with_all_labels(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    sched(env).require_startup()


def test_shutdown_request_stops_a_running_task_leaving_its_record(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    s.stop(join_timeout=30)
    rec = env.runs.load("pi-relay-1")
    assert rec.step == "pi_turn" and rec.outcome == ""
    assert env.github.state(1) == "designing"  # untouched: recovery will resume it
    assert env.github.comments(1) == []


def test_run_forever_polls_until_stopped(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.config = dataclasses.replace(env.cfg, poll_interval_seconds=0.1)
    s = Scheduler(env.deps)
    s.config = env.deps.config
    t = threading.Thread(target=s.run_forever)
    t.start()
    assert wait_for(lambda: len(issue_list_calls(env)) >= 3 * 4)
    s.stop()
    t.join(5)
    assert not t.is_alive()
