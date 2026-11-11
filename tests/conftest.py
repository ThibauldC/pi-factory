from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from pi_factory import secrets

HERE = Path(__file__).parent


class FakePi:
    """A scripted stand-in for the pi binary (see tests/fake_pi.py)."""

    def __init__(self, root: Path, turns: list[dict] | None = None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.log_path = root / "fake-pi-log.jsonl"
        self.script_path = root / "fake-pi-script.json"
        self.path = root / "pi"
        self.set_turns(turns or [{}])
        self.path.write_text(
            "#!/bin/sh\n"
            f"FAKE_PI_SCRIPT={shlex.quote(str(self.script_path))} "
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(HERE / "fake_pi.py"))} "$@"\n'
        )
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def set_turns(self, turns: list[dict]) -> None:
        self.script_path.write_text(json.dumps({"log": str(self.log_path), "turns": turns}))

    def entries(self, event: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [r for r in rows if event is None or r.get("event") == event]

    def prompts(self) -> list[str]:
        return [r["message"] for r in self.entries("prompt")]

    def starts(self) -> list[dict]:
        return self.entries("start")


@pytest.fixture
def fake_pi(tmp_path):
    def make(turns: list[dict] | None = None) -> FakePi:
        return FakePi(tmp_path / "fakepi", turns)

    return make


@pytest.fixture(autouse=True)
def _reset_secret_registry():
    secrets.forget_all_secrets()
    yield
    secrets.forget_all_secrets()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def make_env(tmp_path):
    from .harness import Env

    envs = []

    def make(**kw):
        env = Env(tmp_path / f"env{len(envs)}", **kw)
        envs.append(env)
        return env

    yield make
    for env in envs:
        env.close()


@pytest.fixture(autouse=True)
def _restore_logging():
    """Tests (and the CLI) reconfigure the factory logger; put it back afterwards."""
    from pi_factory import logs

    handlers, propagate, level = list(logs.log.handlers), logs.log.propagate, logs.log.level
    yield
    logs.log.handlers[:] = handlers
    logs.log.propagate = propagate
    logs.log.setLevel(level)
