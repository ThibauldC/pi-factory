import pytest

from pi_factory.cli import build_parser


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_parser_accepts_run_and_check():
    assert build_parser().parse_args(["run", "--config", "x.toml"]).config == "x.toml"
    assert build_parser().parse_args(["check"]).command == "check"


# --- preflight and run (12.1) ----------------------------------------------------------------

import os  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import urllib.request  # noqa: E402

from pi_factory.cli import main  # noqa: E402

from .test_scheduler import DESIGN_OK, wait_for  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Setup:
    """A config file, secrets file and overrides pointing the CLI at the fakes."""

    def __init__(self, env, tmp_path):
        self.env = env
        self.port = free_port()
        self.config = tmp_path / "factory.toml"
        self.secrets = tmp_path / "secrets.env"
        self.write_config()
        self.write_secrets()
        self.overrides = {
            "linear_api_url": env.linear.api_url,
            "linear_token_url": env.linear.token_url,
            "github_api_url": env.github.api_url,
            "repo_slug": lambda r: ("owner", r.key),
        }

    def write_config(self, extra_repo="", pi_path=None, state_root=None):
        cfg = self.env.cfg
        self.config.write_text(
            f"""
[linear]
team = "THI"
owner = "owner@example.com"
[factory]
pi_path = "{pi_path or self.env.pi.path}"
state_root = "{state_root or cfg.state_root}"
worktree_root = "{cfg.worktree_root}"
repos_root = "{cfg.repos_root}"
poll_interval_seconds = 0.2
dashboard_listen = "127.0.0.1:{self.port}"
[repos.pi-relay]
url = "{self.env.remotes["pi-relay"]}"
checks = ["true"]
{extra_repo}
"""
        )

    def write_secrets(self, text=None, mode=0o600):
        self.secrets.write_text(
            text
            if text is not None
            else "PI_FACTORY_LINEAR_CLIENT_ID=cid\nPI_FACTORY_LINEAR_CLIENT_SECRET=csecret\n"
            "PI_FACTORY_GITHUB_TOKEN=ghp_bottoken\n"
        )
        os.chmod(self.secrets, mode)

    def argv(self, command):
        return [command, "--config", str(self.config), "--secrets", str(self.secrets)]

    def main(self, command):
        return main(self.argv(command), **self.overrides)


@pytest.fixture
def cli(make_env, tmp_path):
    env = make_env(pi_turns=[DESIGN_OK])
    return Setup(env, tmp_path)


def test_check_passes_when_every_prerequisite_is_in_place(cli, capsys):
    assert cli.main("check") == 0
    out = capsys.readouterr().out
    for name in ("secrets", "pi binary", "directories", "linear", "github:pi-relay"):
        assert f"[ok] {name}" in out
    assert "fake-pi 1.2.3" in out  # the pi version is reported
    assert "GitHub integration" in out  # reminder about what cannot be verified
    assert cli.env.linear.ops("Issues") == []  # check never polls for work


def test_check_fails_for_a_missing_secrets_file(cli, capsys):
    cli.secrets.unlink()
    assert cli.main("check") == 1
    assert "cannot read secrets file" in capsys.readouterr().err


def test_check_fails_for_open_secrets_permissions(cli, capsys):
    cli.write_secrets(mode=0o644)
    assert cli.main("check") == 1
    assert "0600" in capsys.readouterr().err


def test_check_fails_for_a_missing_required_secret(cli, capsys):
    cli.write_secrets("PI_FACTORY_GITHUB_TOKEN=ghp_bottoken\n")
    assert cli.main("check") == 1
    assert "[FAIL] secrets" in capsys.readouterr().out


def test_check_fails_for_a_config_typo_naming_the_key(cli, capsys):
    cli.write_config(extra_repo="chekcs = ['x']")
    assert cli.main("check") == 1
    assert "repos.pi-relay.chekcs" in capsys.readouterr().err


def test_check_fails_for_a_missing_pi_binary(cli, capsys, tmp_path):
    cli.write_config(pi_path=tmp_path / "no-such-pi")
    assert cli.main("check") == 1
    assert "[FAIL] pi binary" in capsys.readouterr().out


def test_check_fails_for_a_missing_linear_state(cli, capsys):
    cli.env.linear.states = [s for s in cli.env.linear.states if s["name"] != "Blocked"]
    assert cli.main("check") == 1
    out = capsys.readouterr().out
    assert "[FAIL] linear" in out and "Blocked" in out


def test_check_fails_for_a_missing_repo_label_and_unknown_owner(cli, capsys):
    cli.env.linear.labels.clear()
    cli.env.linear.users.clear()
    assert cli.main("check") == 1
    out = capsys.readouterr().out
    assert "pi-relay" in out and "owner@example.com" in out


def test_check_fails_when_the_bot_cannot_access_a_repo(cli, capsys):
    cli.env.github.repos.clear()
    assert cli.main("check") == 1
    assert "[FAIL] github:pi-relay" in capsys.readouterr().out


def test_check_fails_when_the_state_directory_is_not_writable(cli, capsys, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    cli.write_config(state_root=blocker / "state")
    assert cli.main("check") == 1
    assert "[FAIL] directories" in capsys.readouterr().out


def test_check_reports_every_failure_in_one_pass(cli, capsys, tmp_path):
    cli.write_config(pi_path=tmp_path / "no-such-pi")
    cli.env.github.repos.clear()
    assert cli.main("check") == 1
    out = capsys.readouterr().out
    assert "[FAIL] pi binary" in out and "[FAIL] github:pi-relay" in out


def test_run_exits_nonzero_without_polling_when_a_prerequisite_is_missing(cli):
    cli.env.linear.states = [s for s in cli.env.linear.states if s["name"] != "Design Review"]
    assert cli.main("run") == 1
    assert cli.env.linear.ops("Issues") == []


def test_run_polls_serves_dashboard_and_exits_cleanly_on_sigterm(cli):
    env = cli.env
    env.seed_issue("THI-1")
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    seen = {}

    def watcher():
        wait_for(lambda: env.linear.issue("THI-1")["state"] == "Design Review", timeout=30)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cli.port}/", timeout=5) as r:
                seen["dashboard"] = r.read().decode()
        except OSError as exc:
            seen["dashboard"] = f"ERROR {exc}"
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=watcher)
    t.start()
    try:
        code = cli.main("run")
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    t.join(10)
    assert code == 0
    assert env.linear.issue("THI-1")["state"] == "Design Review"
    assert "pi-factory" in seen["dashboard"] and "pi-relay" in seen["dashboard"]
