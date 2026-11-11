"""Opt-in smoke test against the real, installed pi binary (PI_FACTORY_SMOKE=1).

It runs in a scratch directory with its own session dir, sends trivial prompts (a few
thousand tokens in total) and confirms that the protocol assumptions in design D6 still hold
for the installed pi version. Nothing under ~/factory or the production pi sessions is used.
"""

import json
import os
import signal
import time
from pathlib import Path

import pytest

from pi_factory.pi_rpc import PiSession

PI = Path(os.environ.get("PI_FACTORY_PI", "~/.pi/agent/bin/pi")).expanduser()

pytestmark = pytest.mark.skipif(
    os.environ.get("PI_FACTORY_SMOKE") != "1" or not PI.exists(),
    reason="set PI_FACTORY_SMOKE=1 (and have pi installed) to run the real-pi smoke test",
)

TOOLS = ["read", "grep", "find", "ls"]


def make(tmp_path, sid="smoke-1", tools=None):
    cwd = tmp_path / "wt"
    cwd.mkdir(exist_ok=True)
    return PiSession(PI, cwd, tmp_path / "sessions", sid, thinking="minimal", tools=tools)


def session_entries(tmp_path):
    files = list((tmp_path / "sessions").glob("*.jsonl"))
    assert len(files) == 1, files
    return files[0], [json.loads(line) for line in files[0].read_text().splitlines()]


def test_new_session_then_reopen_and_continue(tmp_path):
    s = make(tmp_path, tools=TOOLS)
    s.start()
    first = s.prompt_and_wait("Reply with exactly the single word OK. Do not use any tools.", 180)
    s.close()
    assert first.ok, first.error
    assert "OK" in first.text
    assert first.usage and sum(u["input"] for u in first.usage.values()) > 0
    file1, _ = session_entries(tmp_path)
    assert first.session_file == str(file1)

    s2 = make(tmp_path, tools=TOOLS)
    s2.start()
    second = s2.prompt_and_wait("What single word did you reply with earlier? Just that word.", 180)
    s2.close()
    assert second.ok, second.error
    assert "OK" in second.text  # the reopened session remembers the first turn
    file2, _ = session_entries(tmp_path)  # still exactly one session file
    assert file2 == file1


def test_read_only_tool_allowlist_hides_bash_edit_write(tmp_path):
    s = make(tmp_path, tools=TOOLS)
    s.start()
    assert s.prompt_and_wait("Reply with the word OK. Do not use any tools.", 180).ok
    s.close()
    _, entries = session_entries(tmp_path)
    system = [e["message"] for e in entries if e.get("message", {}).get("role") == "system"][0]
    names = {t["name"] for t in system["toolsAdded"]}
    assert names == set(TOOLS)
    assert not names & {"bash", "edit", "write"}


def test_session_killed_mid_turn_can_be_reopened_and_continued(tmp_path):
    s = make(tmp_path, sid="smoke-kill", tools=TOOLS)
    s.start()
    pid = s.pid
    import threading

    threading.Thread(
        target=lambda: (time.sleep(6), os.killpg(pid, signal.SIGKILL)), daemon=True
    ).start()
    res = s.prompt_and_wait("Write the integers from 1 to 20000, one per line, no other text.", 120)
    assert not res.ok and res.failure == "exited"
    s.close()

    s2 = make(tmp_path, sid="smoke-kill", tools=TOOLS)
    s2.start()
    res2 = s2.prompt_and_wait(
        "Your previous turn was interrupted. Ignore it and reply with exactly the word RESUMED.",
        180,
    )
    s2.close()
    assert res2.ok, res2.error
    assert "RESUMED" in res2.text
    assert len(list((tmp_path / "sessions").glob("*.jsonl"))) == 1
