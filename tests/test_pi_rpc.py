import json
import time

import pytest

from pi_factory.pi_rpc import PiError, PiSession, build_command
from pi_factory.ring import RingBuffer

from .conftest import pid_alive


def session(fake, tmp_path, **kw):
    cwd = tmp_path / "wt"
    cwd.mkdir(exist_ok=True)
    kw.setdefault("abort_grace", 0.5)
    kw.setdefault("term_grace", 0.5)
    return PiSession(
        fake.path, cwd, tmp_path / "sessions", kw.pop("session_id", "pi-relay-1-build-1"), **kw
    )


def test_command_line_per_design_d6(tmp_path):
    cmd = build_command("/p/pi", "/s", "pi-relay-1-design-1", "m", "high", ["read", "grep"])
    assert cmd == [
        "/p/pi", "--mode", "rpc", "--no-approve", "--session-dir", "/s",
        "--session-id", "pi-relay-1-design-1", "--model", "m", "--thinking", "high",
        "--tools", "read,grep",
    ]  # fmt: skip


def test_nothing_configured_means_no_model_flags():
    cmd = build_command("/p/pi", "/s", "id")
    assert "--model" not in cmd and "--thinking" not in cmd and "--tools" not in cmd
    assert "--no-approve" in cmd  # project-local resources are not trusted


def test_success_turn_text_usage_and_session_file(fake_pi, tmp_path):
    fake = fake_pi([{"text": "hello", "usage": {"input": 100, "output": 7}}])
    s = session(fake, tmp_path)
    s.start()
    res = s.prompt_and_wait("do it", timeout=10)
    s.close()
    assert res.ok and res.text == "hello" and res.stop_reason == "stop"
    assert res.usage == {
        "fake-model": {"input": 100, "output": 7, "cache_read": 0, "cache_write": 0, "total": 107}
    }
    assert res.session_file and res.session_file.endswith("pi-relay-1-build-1.jsonl")
    assert (tmp_path / "sessions").is_dir()
    assert fake.prompts() == ["do it"]
    argv = fake.starts()[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == "pi-relay-1-build-1"
    assert fake.starts()[0]["cwd"] == str(tmp_path / "wt")


def test_idle_after_close_no_child_running(fake_pi, tmp_path):
    s = session(fake_pi(), tmp_path)
    s.start()
    pid = s.pid
    assert s.prompt_and_wait("x", 10).ok
    s.close()
    assert not s.alive() and not pid_alive(pid)


def test_second_turn_on_same_live_process(fake_pi, tmp_path):
    fake = fake_pi([{"text": "first"}, {"text": "second"}])
    s = session(fake, tmp_path)
    s.start()
    assert s.prompt_and_wait("a", 10).text == "first"
    assert s.prompt_and_wait("b", 10).text == "second"
    s.close()
    assert len(fake.starts()) == 1 and fake.prompts() == ["a", "b"]


def test_provider_error_is_a_failure_with_text(fake_pi, tmp_path):
    fake = fake_pi([{"behavior": "error", "error_message": "429 rate limited"}])
    s = session(fake, tmp_path)
    s.start()
    res = s.prompt_and_wait("x", 10)
    s.close()
    assert not res.ok and res.failure == "provider_error"
    assert res.error == "429 rate limited" and res.stop_reason == "error"


def test_length_stop_is_a_failure(fake_pi, tmp_path):
    fake = fake_pi([{"behavior": "error", "stop_reason": "length", "error_message": ""}])
    s = session(fake, tmp_path)
    s.start()
    res = s.prompt_and_wait("x", 10)
    s.close()
    assert not res.ok and res.failure == "length"


def test_process_exit_mid_turn_is_a_failure(fake_pi, tmp_path):
    s = session(fake_pi([{"behavior": "crash"}]), tmp_path)
    s.start()
    res = s.prompt_and_wait("x", 10)
    s.close()
    assert not res.ok and res.failure == "exited"


def test_timeout_aborts_and_terminates_process(fake_pi, tmp_path):
    s = session(fake_pi([{"behavior": "hang"}]), tmp_path)
    s.start()
    pid = s.pid
    t0 = time.monotonic()
    res = s.prompt_and_wait("x", timeout=0.5)
    assert not res.ok and res.failure == "timeout" and "0.5s" in res.error
    assert time.monotonic() - t0 < 5
    assert not pid_alive(pid)


def test_deaf_process_is_killed_after_escalation(fake_pi, tmp_path):
    s = session(fake_pi([{"behavior": "hang_deaf"}]), tmp_path, abort_grace=0.3, term_grace=0.3)
    s.start()
    pid = s.pid
    res = s.prompt_and_wait("x", timeout=0.4)
    assert res.failure == "timeout"
    assert not pid_alive(pid)


def test_cancel_from_another_thread(fake_pi, tmp_path):
    import threading

    s = session(fake_pi([{"behavior": "hang"}]), tmp_path)
    s.start()
    pid = s.pid
    threading.Timer(0.3, s.cancel).start()
    res = s.prompt_and_wait("x", timeout=30)
    assert not res.ok and res.failure == "cancelled"
    assert not pid_alive(pid)


def test_shutdown_request_closes_stdin_and_reports_shutdown(fake_pi, tmp_path):
    import threading

    s = session(fake_pi([{"behavior": "hang"}]), tmp_path)
    s.start()
    threading.Timer(0.3, s.request_shutdown).start()
    res = s.prompt_and_wait("x", timeout=30)
    assert res.failure == "shutdown"
    s.proc.wait(timeout=5)  # fake exits when stdin closes
    s.close()


def test_extension_ui_requests_get_automatic_answers(fake_pi, tmp_path):
    fake = fake_pi([{"behavior": "ui_confirm"}, {"behavior": "ui_select"}])
    ring = RingBuffer()
    s = session(fake, tmp_path, ring=ring)
    s.start()
    res = s.prompt_and_wait("x", 10)
    assert res.ok and '"confirmed": false' in res.text  # confirm -> false
    res = s.prompt_and_wait("y", 10)
    assert res.ok and '"cancelled": true' in res.text  # other dialogs -> cancelled
    s.close()
    assert any("hello" in line for line in ring.snapshot())  # notify is logged


def test_no_factory_secret_reaches_child(fake_pi, tmp_path):
    fake = fake_pi([{}])
    source = {
        "HOME": "/home/x", "PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "XDG_DATA_HOME": "/d",
        "PI_FACTORY_GITHUB_TOKEN": "ghp_secret", "PI_FACTORY_OTHER_SECRET": "shh",
        "SOME_API_KEY": "nope", "FOO": "bar", "KEEP": "me",
    }  # fmt: skip
    s = session(
        fake, tmp_path, env_source=source, env_passthrough=["KEEP", "PI_FACTORY_GITHUB_TOKEN"]
    )
    s.start()
    assert s.prompt_and_wait("env", 10).ok
    s.close()
    env = fake.starts()[0]["env"]
    assert not [k for k in env if k.startswith("PI_FACTORY_")]
    assert "ghp_secret" not in json.dumps(env) and "shh" not in json.dumps(env)
    assert env["CI"] == "1" and env["TERM"] == "dumb" and env["KEEP"] == "me"
    assert "FOO" not in env and "SOME_API_KEY" not in env
    assert env["LC_ALL"] == "C" and env["HOME"] == "/home/x"
    assert env["PATH"].startswith("/usr/bin:") and "pi-node/current/bin" in env["PATH"]


def test_output_ring_buffer_gets_text_and_tools(fake_pi, tmp_path):
    ring = RingBuffer(200)
    s = session(fake_pi([{"text": "line one", "tool_calls": ["read"]}]), tmp_path, ring=ring)
    s.start()
    s.prompt_and_wait("x", 10)
    s.close()
    lines = ring.snapshot()
    assert any(line.startswith("> read") for line in lines)
    assert any(line.startswith("< read ok") for line in lines)
    assert "line one" in lines


def test_missing_binary_is_a_pi_error(tmp_path):
    s = PiSession(tmp_path / "nope", tmp_path, tmp_path / "s", "id")
    with pytest.raises(PiError, match="cannot start pi"):
        s.start()


def test_resume_reopens_same_session_file(fake_pi, tmp_path):
    fake = fake_pi([{"behavior": "crash"}, {"text": "resumed ok"}])
    s1 = session(fake, tmp_path)
    s1.start()
    assert s1.prompt_and_wait("work", 10).failure == "exited"
    s1.close()
    s2 = session(fake, tmp_path)  # same session dir and id
    s2.start()
    res = s2.prompt_and_wait("you were interrupted", 10)
    s2.close()
    assert res.ok and res.text == "resumed ok"
    assert [e["resumed"] for e in fake.starts()] == [False, True]
    assert len(list((tmp_path / "sessions").glob("*.jsonl"))) == 1


def test_corrupt_session_file_reports_pi_error_text(fake_pi, tmp_path):
    fake = fake_pi()
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / "20260101_pi-relay-1-build-1.jsonl").write_text("CORRUPT")
    s = session(fake, tmp_path)
    s.start()
    res = s.prompt_and_wait("x", 10)
    s.close()
    assert not res.ok and res.failure == "exited"
    assert "failed to load session" in res.error
