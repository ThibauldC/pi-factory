import dataclasses
import logging
import threading
import time

import pytest

from pi_factory import logs
from pi_factory.scheduler import MAX_BACKOFF, Scheduler, StartupError, backoff_delay
from pi_factory.tracker.base import TrackerError

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


# --- work-intake ---------------------------------------------------------------------------


def test_poll_on_schedule_no_pi_process_when_nothing_startable(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue("THI-1", state="Backlog")
    env.seed_issue("THI-2", state="Design Review")
    s = sched(env)
    tick(env, s)
    assert env.pi.starts() == [] and s.tasks == {}
    assert len(env.linear.ops("Issues")) == 1  # a single query per poll
    assert s.next_delay() == 120


def test_mapped_label_queues_issue_for_its_repo_and_runs_design(make_env):
    env = make_env(pi_turns=[DESIGN_OK], repos=("pi-relay", "other"))
    env.seed_issue("THI-1", labels=("pi-relay",))
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Design Review"
    assert env.pi.starts()[0]["cwd"].endswith("worktrees/pi-relay/THI-1")


@pytest.mark.parametrize("labels", [(), ("ghost",), ("pi-relay", "other")])
def test_missing_or_unknown_repo_label_blocks_and_names_configured_keys(make_env, labels):
    env = make_env(pi_turns=[DESIGN_OK], repos=("pi-relay", "other"))
    env.linear.labels.append({"name": "ghost", "parent": {"name": "repo"}})
    env.seed_issue("THI-1", labels=labels)
    s = sched(env)
    tick(env, s)
    issue = env.linear.issue("THI-1")
    assert issue["state"] == "Blocked" and issue["assignee"] == "u-owner"
    body = env.linear.comments("THI-1")[-1]["body"]
    assert "`other`" in body and "`pi-relay`" in body and "repo" in body
    assert env.pi.starts() == []
    tick(env, s)  # Blocked is no longer ready: no second comment
    assert len(env.linear.comments("THI-1")) == 1


def test_a_label_outside_the_repo_group_does_not_count(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue("THI-1", labels=(("bug", None), "pi-relay"))  # one repo label + an unrelated one
    env.seed_issue("THI-2", labels=(("bug", None),))  # no repo label at all
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Design Review"
    assert env.linear.issue("THI-2")["state"] == "Blocked"


def test_same_repo_busy_second_issue_waits(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue("THI-1")
    env.seed_issue("THI-2")
    s = sched(env)
    assert s.poll_once()
    deadline = time.time() + 10
    while not env.pi.starts() and time.time() < deadline:
        time.sleep(0.05)
    assert list(s.tasks) == ["THI-1"]  # same created order -> THI-1 first
    assert env.linear.issue("THI-2")["state"] == "Ready for Design"
    s.poll_once()
    assert list(s.tasks) == ["THI-1"] and env.linear.issue("THI-2")["state"] == "Ready for Design"
    for t in list(s.tasks.values()):
        t.cancel()
    s.wait_idle(30)


def test_global_cap_keeps_third_repo_queued_until_a_slot_frees(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}], repos=("a", "b", "c"))
    for n, repo in enumerate(("a", "b", "c"), 1):
        env.seed_issue(f"THI-{n}", labels=(repo,))
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: len(env.pi.starts()) == 2)
    assert sorted(s.tasks) == ["THI-1", "THI-2"]  # cap of 2
    s.poll_once()
    assert sorted(s.tasks) == ["THI-1", "THI-2"]
    assert env.linear.issue("THI-3")["state"] == "Ready for Design"
    # free a slot: a human moves THI-1 out of Designing; the third repository is served next
    env.linear.move("THI-1", "Backlog")
    env.pi.set_turns([DESIGN_OK])
    s.poll_once()
    assert wait_for(lambda: "THI-1" not in s.tasks)
    s.poll_once()
    assert wait_for(lambda: env.linear.issue("THI-3")["state"] == "Design Review")
    for t in list(s.tasks.values()):
        t.cancel()
    assert s.wait_idle(30)


def test_priority_wins_over_age_and_ties_by_age(make_env):
    env = make_env(pi_turns=[DESIGN_OK, DESIGN_OK, DESIGN_OK], max_concurrent=1)
    env.seed_issue("THI-1", priority=4, created_at="2026-01-01T00:00:00.000Z")  # Low, older
    env.seed_issue("THI-2", priority=1, created_at="2026-02-01T00:00:00.000Z")  # Urgent
    env.seed_issue("THI-3", priority=4, created_at="2025-12-01T00:00:00.000Z")  # Low, oldest
    s = sched(env)
    order = []
    for _ in range(3):
        before = {
            i["identifier"] for i in env.linear.issues.values() if i["state"] != "Ready for Design"
        }
        tick(env, s)
        after = {
            i["identifier"] for i in env.linear.issues.values() if i["state"] != "Ready for Design"
        }
        order += sorted(after - before)
    assert order == ["THI-2", "THI-3", "THI-1"]


def test_no_priority_sorts_last(make_env):
    env = make_env(pi_turns=[DESIGN_OK], max_concurrent=1)
    env.seed_issue("THI-1", priority=0, created_at="2025-01-01T00:00:00.000Z")
    env.seed_issue("THI-2", priority=4, created_at="2026-01-01T00:00:00.000Z")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-2")["state"] == "Design Review"
    assert env.linear.issue("THI-1")["state"] == "Ready for Design"


def test_repositories_served_by_oldest_waiting_issue(make_env):
    env = make_env(pi_turns=[DESIGN_OK], repos=("a", "b"))
    env.seed_issue("THI-2", labels=("a",), priority=4)  # low priority, repo a
    env.seed_issue("THI-3", labels=("b",), priority=1)  # urgent, repo b
    s = sched(env)
    issues = [i for i in env.tracker.list_issues()]
    s._ready_since.update({"THI-2": 5.0, "THI-3": 9.0})  # a has been waiting longer
    assert [i.identifier for i in s._ordered_candidates(issues)] == ["THI-2", "THI-3"]
    s._ready_since.update({"THI-2": 9.0, "THI-3": 5.0})
    assert [i.identifier for i in s._ordered_candidates(issues)] == ["THI-3", "THI-2"]


# --- backoff -------------------------------------------------------------------------------


def test_backoff_is_longer_than_interval_and_capped():
    assert backoff_delay(120, 0) == 120
    delays = [backoff_delay(120, n) for n in (1, 2, 3, 4, 10)]
    assert all(120 < d <= MAX_BACKOFF for d in delays[:3]) and delays[-1] == MAX_BACKOFF
    assert delays == sorted(delays)


def test_linear_outage_backs_off_and_recovers(make_env, caplog):
    env = make_env(pi_turns=[DESIGN_OK])
    s = sched(env)
    caplog.set_level(logging.INFO, logger="pi_factory")
    logs.log.propagate = True
    try:
        env.linear.fail_next = [500] * 6
        for _ in range(3):
            assert s.poll_once() is False
        assert 120 < s.next_delay() <= MAX_BACKOFF
        assert len(s.poll_errors) == 3 and s.last_poll_ok is False
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "poll_error" in text
        env.linear.fail_next = []
        assert s.poll_once() is True
        assert s.next_delay() == 120 and s.failures == 0
        assert "poll_recovered" in " ".join(r.getMessage() for r in caplog.records)
    finally:
        logs.log.propagate = False


def test_running_tasks_continue_during_an_outage(make_env):
    env = make_env(pi_turns=[{"sleep": 1.5, "text": GOOD}])
    env.seed_issue("THI-1")
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())  # the worker is now mid-turn
    env.linear.fail_next = [500]
    assert s.poll_once() is False  # a failing poll must not disturb it
    assert s.wait_idle(30)
    assert env.linear.issue("THI-1")["state"] == "Design Review"


# --- task-lifecycle ------------------------------------------------------------------------


def test_comment_does_not_trigger_work(make_env):
    env = make_env(pi_turns=[DESIGN_OK, {"edits": {"a": "1"}}])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Design Review"
    env.linear.add_comment("THI-1", "go ahead")
    n = len(env.pi.prompts())
    tick(env, s)
    assert len(env.pi.prompts()) == n and env.linear.issue("THI-1")["state"] == "Design Review"


def test_state_change_triggers_build_and_full_flow_to_in_review(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Design Review"
    env.linear.move("THI-1", "Ready for Build")
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "In Review"
    # PR open, issue In Review: no further action
    n = len(env.pi.prompts())
    tick(env, s)
    tick(env, s)
    assert len(env.pi.prompts()) == n and len(env.github.open_prs()) == 1
    # factory transitions were exactly the owned ones
    assert [t[1] for t in env.linear.transitions] == [
        "Designing", "Design Review", "Building", "In Review",
    ]  # fmt: skip


def test_factory_never_moves_issues_to_done_or_canceled(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    env.linear.move("THI-1", "Ready for Build")
    tick(env, s)
    assert not {"Done", "Canceled"} & {t[1] for t in env.linear.transitions}


def test_redesign_runs_new_round_with_previous_design(make_env):
    env = make_env(pi_turns=[DESIGN_OK, {"text": GOOD.replace("round 1", "round 2")}])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    env.linear.add_comment("THI-1", "please reconsider")
    env.linear.move("THI-1", "Ready for Design")
    tick(env, s)
    assert env.linear.comments("THI-1")[-1]["body"].startswith("# Design (round 2)")
    assert "please reconsider" in env.pi.prompts()[1]


def test_retry_after_blocked_treated_like_any_ready_issue(make_env):
    env = make_env(pi_turns=[{"behavior": "error"}, DESIGN_OK])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    env.linear.move("THI-1", "Ready for Design")
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Design Review"


def test_bot_without_repo_access_blocks_with_comment(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.github.repos.clear()
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    assert "lacks access" in env.linear.comments("THI-1")[-1]["body"]
    assert env.pi.starts() == []


def test_low_disk_blocks_before_creating_a_worktree(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.disk_free = lambda path: 1 * 1024**3
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    assert env.linear.issue("THI-1")["state"] == "Blocked"
    body = env.linear.comments("THI-1")[-1]["body"]
    assert "free disk" in body and "2.0 GiB" in body
    assert not (env.cfg.worktree_root / "pi-relay" / "THI-1").exists()


def test_enough_disk_proceeds(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.disk_free = lambda path: 50 * 1024**3
    env.seed_issue("THI-1")
    tick(env, sched(env))
    assert env.linear.issue("THI-1")["state"] == "Design Review"


# --- cancellation and cleanup -----------------------------------------------------------------


def test_moved_to_backlog_mid_build_stops_everything_posts_nothing_keeps_worktree(make_env):
    env = make_env(
        pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}],
        checks=["sleep 60"],
    )
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    env.linear.move("THI-1", "Ready for Build")
    assert s.poll_once()
    assert wait_for(lambda: s.tasks and s.tasks["THI-1"].step == "checks")
    comments_before = len(env.linear.comments("THI-1"))
    env.linear.move("THI-1", "Backlog")
    s.poll_once()  # within one poll interval
    assert s.wait_idle(30)
    assert len(env.linear.comments("THI-1")) == comments_before
    assert env.github.open_prs() == []
    assert env.git.worktree_path(env.cfg.repos["pi-relay"], "THI-1").exists()
    assert env.linear.issue("THI-1")["state"] == "Backlog"  # factory did not touch it
    import subprocess

    assert subprocess.run(["pgrep", "-f", "sleep 60"], capture_output=True).returncode != 0


def test_canceled_mid_design_stops_run_then_cleans_up(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue("THI-1")
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    pid_alive_marker = env.git.worktree_path(env.cfg.repos["pi-relay"], "THI-1")
    assert pid_alive_marker.exists()
    env.linear.move("THI-1", "Canceled")
    s.poll_once()
    assert s.wait_idle(30)
    assert env.linear.comments("THI-1") == []
    s.poll_once()  # cleanup rules for Canceled issues
    assert not pid_alive_marker.exists()
    assert env.runs.list_issues() == [] and len(list(env.cfg.archive_dir.iterdir())) == 1


def test_merged_pr_moves_issue_to_done_and_next_poll_cleans_up(make_env):
    env = make_env(pi_turns=[{"text": GOOD_DESIGN}, {"edits": {"a.txt": "1"}}])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)
    env.linear.move("THI-1", "Ready for Build")
    tick(env, s)
    wt = env.git.worktree_path(env.cfg.repos["pi-relay"], "THI-1")
    assert wt.exists() and env.runs.list_issues() == ["THI-1"]
    env.linear.move("THI-1", "Done")  # Linear's GitHub integration after the merge
    tick(env, s)
    assert not wt.exists()
    assert env.runs.list_issues() == []
    archived = list(env.cfg.archive_dir.iterdir())
    assert len(archived) == 1 and (archived[0] / "run.json").exists()
    assert "thi-1-add-retry-to-fetch" not in (
        __import__("subprocess")
        .run(["git", "branch", "--list"], cwd=env.git.bare_path(env.cfg.repos["pi-relay"]),
             capture_output=True, text=True).stdout
    )  # fmt: skip


def test_tracked_issues_are_polled_by_id_even_in_unpolled_states(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue("THI-1")
    s = sched(env)
    tick(env, s)  # now Design Review: not one of the polled state names
    env.linear.calls.clear()
    s.poll_once()
    ids = env.linear.ops("Issues")[0]["variables"]["ids"]
    assert env.linear.issue("THI-1")["id"] in ids


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


def test_missing_state_at_startup_is_reported_and_blocks_polling(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.linear.states = [st for st in env.linear.states if st["name"] != "Design Review"]
    s = sched(env)
    assert any("Design Review" in p for p in s.startup_check())
    with pytest.raises(StartupError, match="Design Review"):
        s.require_startup()
    assert env.linear.ops("Issues") == []  # never polled


def test_startup_ok_with_all_states(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    sched(env).require_startup()


def test_shutdown_request_stops_a_running_task_leaving_its_record(make_env):
    env = make_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue("THI-1")
    s = sched(env)
    s.poll_once()
    assert wait_for(lambda: env.pi.starts())
    s.stop(join_timeout=30)
    rec = env.runs.load("THI-1")
    assert rec.step == "pi_turn" and rec.outcome == ""
    assert env.linear.issue("THI-1")["state"] == "Designing"  # untouched: recovery will resume it
    assert env.linear.comments("THI-1") == []


def test_run_forever_polls_until_stopped(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.deps.config = dataclasses.replace(env.cfg, poll_interval_seconds=0.1)
    s = Scheduler(env.deps)
    s.config = env.deps.config
    t = threading.Thread(target=s.run_forever)
    t.start()
    assert wait_for(lambda: len(env.linear.ops("Issues")) >= 3)
    s.stop()
    t.join(5)
    assert not t.is_alive()


def test_tracker_errors_during_label_blocking_do_not_crash_the_poll(make_env):
    env = make_env(pi_turns=[DESIGN_OK])
    env.seed_issue("THI-1", labels=())
    s = sched(env)
    orig = env.tracker.set_state

    def boom(*a):
        raise TrackerError("down", transient=True)

    env.tracker.set_state = boom
    assert s.poll_once() is True
    env.tracker.set_state = orig
