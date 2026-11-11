import json
import os
import time

import pytest

from pi_factory import logs, runs
from pi_factory.runs import RunStore


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs", tmp_path / "archive")


def test_create_and_reload(store):
    rec = store.create("THI-1", "pi-relay", phase="build", round=1)
    store.set_step(rec, "setup")
    loaded = store.load("THI-1")
    assert loaded.step == "setup" and loaded.phase == "build" and loaded.round == 1
    assert store.list_issues() == ["THI-1"]
    assert store.load("THI-2") is None


def test_step_must_be_known(store):
    rec = store.create("THI-1", "r")
    with pytest.raises(ValueError):
        store.set_step(rec, "bogus")


def test_atomic_write_survives_simulated_crash(store, monkeypatch):
    rec = store.create("THI-1", "r", phase="build")
    store.set_step(rec, "checks")

    def boom(src, dst):
        raise KeyboardInterrupt("killed before rename")

    monkeypatch.setattr(os, "replace", boom)
    rec.step = "commit"
    with pytest.raises(KeyboardInterrupt):
        store.save(rec)
    monkeypatch.undo()
    on_disk = json.loads(store.record_path("THI-1").read_text())  # valid, parseable
    assert on_disk["step"] == "checks"  # the previous record survived
    assert store.load("THI-1").step == "checks"


def test_atomic_write_never_exposes_partial_file(store):
    rec = store.create("THI-1", "r")
    store.save(rec)
    leftovers = [p.name for p in store.run_dir("THI-1").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_unknown_fields_in_record_are_ignored(store):
    store.create("THI-1", "r")
    data = json.loads(store.record_path("THI-1").read_text())
    data["future_field"] = 1
    store.record_path("THI-1").write_text(json.dumps(data))
    assert store.load("THI-1").issue == "THI-1"


def test_outputs_and_posted_texts_saved(store):
    store.create("THI-1", "r")
    p = store.write_output("THI-1", "build-1-check-1.txt", "ok")
    q = store.write_posted("THI-1", "design-1.md", "# Design")
    assert p.read_text() == "ok" and q.read_text() == "# Design"
    assert p.parent == store.run_dir("THI-1") / "commands"


def test_archive_moves_run_dir(store):
    store.create("THI-1", "r")
    dest = store.archive("THI-1")
    assert dest is not None and (dest / "run.json").is_file()
    assert not store.run_dir("THI-1").exists()
    assert store.list_issues() == []
    assert store.archive("THI-1") is None


def test_prune_after_31_days_keeps_recent(store):
    now = time.time()
    store.create("OLD-1", "r")
    store.create("NEW-1", "r")
    old = store.archive("OLD-1", now=now - 31 * 86400)
    new = store.archive("NEW-1", now=now - 5 * 86400)
    removed = store.prune(30, now=now)
    assert removed == [old]
    assert not old.exists() and new.exists()


def test_blocked_run_never_pruned(store):
    rec = store.create("THI-9", "r")
    rec.outcome = "blocked"
    store.save(rec)
    old = time.time() - 60 * 86400
    os.utime(store.run_dir("THI-9"), (old, old))
    os.utime(store.record_path("THI-9"), (old, old))
    assert store.prune(30) == []
    assert store.run_dir("THI-9").is_dir()


def test_repeated_archive_of_same_issue_does_not_clash(store):
    store.create("THI-1", "r")
    a = store.archive("THI-1", now=1_700_000_000)
    store.create("THI-1", "r")
    b = store.archive("THI-1", now=1_700_000_000)
    assert a != b and a.exists() and b.exists()


def test_structured_logging_lines():
    import io

    buf = io.StringIO()
    logs.setup_logging(buf)
    logs.event("phase_start", "THI-1", phase="build", round=1)
    logs.event("check_result", "THI-1", command="pnpm test", exit_code=0, duration_s=3.2)
    logs.event("push", "THI-1", branch="thi-1-x")
    logs.event("pr_created", "THI-1", number=7, note="has space")
    lines = [line.removeprefix("INFO ") for line in buf.getvalue().splitlines()]
    assert lines[0] == "phase_start issue=THI-1 phase=build round=1"
    assert 'check_result issue=THI-1 command="pnpm test" exit_code=0' in lines[1]
    assert lines[2] == "push issue=THI-1 branch=thi-1-x"
    assert lines[3] == 'pr_created issue=THI-1 number=7 note="has space"'


def test_log_lines_are_redacted_and_go_to_stdout(capsys):
    import io

    from pi_factory.secrets import forget_all_secrets, register_secret

    register_secret("ghp_topsecret")
    buf = io.StringIO()
    logs.setup_logging(buf)
    logs.event("push_failed", "THI-1", error="401 ghp_topsecret")
    assert "ghp_topsecret" not in buf.getvalue()
    assert buf.getvalue().startswith("INFO push_failed issue=THI-1")
    forget_all_secrets()
    assert runs.STEPS == ("setup", "pi_turn", "checks", "commit", "push", "pr")
