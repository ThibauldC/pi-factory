import pytest

from pi_factory.codehost.base import CodeHostError
from pi_factory.codehost.github import GitHub, parse_github_url
from pi_factory.secrets import Secrets

from .fakes.github import FakeGitHub
from .gitfix import make_config


@pytest.fixture
def gh(tmp_path):
    fake = FakeGitHub().start()
    cfg = make_config(tmp_path, {"pi-relay": "https://github.com/owner/pi-relay.git"})
    client = GitHub(
        Secrets({"PI_FACTORY_GITHUB_TOKEN": fake.token}),
        api_url=fake.api_url,
        mergeable_retry_delay=0,
        repo_slug=lambda r: ("owner", r.key),
    )
    yield fake, client, cfg.repos["pi-relay"]
    fake.stop()


def test_parse_github_url_forms():
    for url in (
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git",
    ):
        assert parse_github_url(url) == ("o", "r")
    with pytest.raises(CodeHostError):
        parse_github_url("https://example.com/o/r.git")


def test_check_access_ok_and_missing_and_readonly(gh):
    fake, client, repo = gh
    assert client.check_access(repo) is None
    fake.repos["owner/pi-relay"] = False
    assert "lacks push access" in client.check_access(repo)
    del fake.repos["owner/pi-relay"]
    assert "lacks access" in client.check_access(repo)


def test_create_find_update_pr(gh):
    fake, client, repo = gh
    assert client.find_open_pr(repo, "thi-1-x") is None
    pr = client.create_pr(repo, "thi-1-x", "THI-1: Add retry", "body 1")
    assert pr.number == 1 and pr.head == "thi-1-x" and pr.base == "main"
    assert client.find_open_pr(repo, "thi-1-x").number == 1
    client.update_pr(repo, 1, "body 2")
    assert fake.prs[("owner/pi-relay", 1)]["body"] == "body 2"
    assert fake.prs[("owner/pi-relay", 1)]["title"] == "THI-1: Add retry"


def test_second_create_attempt_finds_the_existing_pr(gh):
    fake, client, repo = gh
    first = client.create_pr(repo, "thi-1-x", "t", "b")
    second = client.create_pr(repo, "thi-1-x", "t", "b")
    assert second.number == first.number and len(fake.open_prs()) == 1
    assert len(fake.calls_to("POST", "/pulls")) == 1  # found first, never posted twice


def test_create_race_422_falls_back_to_the_existing_pr(gh):
    fake, client, repo = gh
    created = client.create_pr(repo, "thi-1-x", "t", "b")
    # Simulate the lookup missing the PR (index lag) but the create being refused.
    original = client.find_open_pr
    calls = {"n": 0}

    def lagging(r, branch):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(r, branch)

    client.find_open_pr = lagging
    assert client.create_pr(repo, "thi-1-x", "t", "b").number == created.number


def test_mergeable_retried_once_when_null(gh):
    fake, client, repo = gh
    client.create_pr(repo, "b", "t", "x")
    fake.mergeable = [None, False]
    assert client.is_mergeable(repo, 1) is False
    fake.mergeable = [None, None]
    assert client.is_mergeable(repo, 1) is None
    assert len(fake.calls_to("GET", r"/pulls/1$")) == 4
    fake.mergeable = [True]
    assert client.is_mergeable(repo, 1) is True


def test_errors_kinds_and_no_token_leak(gh):
    fake, client, repo = gh
    fake.fail_next = [502]
    with pytest.raises(CodeHostError) as exc:
        client.find_open_pr(repo, "b")
    assert exc.value.kind == "transient"
    bad = GitHub(
        Secrets({"PI_FACTORY_GITHUB_TOKEN": "ghp_wrongtoken1"}),
        api_url=fake.api_url,
        repo_slug=lambda r: ("owner", r.key),
    )
    with pytest.raises(CodeHostError) as exc2:
        bad.find_open_pr(repo, "b")
    assert exc2.value.kind == "access" and "ghp_wrongtoken1" not in str(exc2.value)
