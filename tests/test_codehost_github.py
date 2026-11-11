import pytest

from pi_factory.codehost.base import CodeHostError
from pi_factory.codehost.github import GitHub
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
    )
    yield fake, client, cfg.repos["pi-relay"]
    fake.stop()


def test_check_access_ok_and_missing_and_readonly(gh):
    fake, client, repo = gh
    assert client.check_access(repo) is None
    fake.repos["owner/pi-relay"] = False
    assert "lacks push access" in client.check_access(repo)
    fake.repos["owner/pi-relay"] = True
    fake.has_issues["owner/pi-relay"] = False
    assert "issues are disabled" in client.check_access(repo)
    del fake.repos["owner/pi-relay"]
    problem = client.check_access(repo)
    assert "lacks access" in problem and "owner/pi-relay" in problem


def test_default_branch(gh):
    fake, client, repo = gh
    assert client.default_branch(repo) == "main"
    fake.default_branches["owner/pi-relay"] = "trunk"
    assert client.default_branch(repo) == "trunk"


def test_create_find_update_pr(gh):
    fake, client, repo = gh
    assert client.find_open_pr(repo, "factory/1-x") is None
    pr = client.create_pr(repo, "factory/1-x", "#1: Add retry", "body 1")
    assert pr.number == 1 and pr.head == "factory/1-x" and pr.base == "main"
    assert pr.url == "https://github.com/owner/pi-relay/pull/1"
    assert client.find_open_pr(repo, "factory/1-x").number == 1
    client.update_pr(repo, 1, "body 2")
    assert fake.prs[("owner/pi-relay", 1)]["body"] == "body 2"
    assert fake.prs[("owner/pi-relay", 1)]["title"] == "#1: Add retry"


def test_second_create_attempt_finds_the_existing_pr(gh):
    fake, client, repo = gh
    first = client.create_pr(repo, "factory/1-x", "t", "b")
    second = client.create_pr(repo, "factory/1-x", "t", "b")
    assert second.number == first.number and len(fake.open_prs()) == 1
    assert len(fake.calls_to("POST", "/pulls")) == 1  # found first, never posted twice


def test_create_race_422_falls_back_to_the_existing_pr(gh):
    fake, client, repo = gh
    created = client.create_pr(repo, "factory/1-x", "t", "b")
    # Simulate the lookup missing the PR (index lag) but the create being refused.
    original = client.find_open_pr
    calls = {"n": 0}

    def lagging(r, branch):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(r, branch)

    client.find_open_pr = lagging
    assert client.create_pr(repo, "factory/1-x", "t", "b").number == created.number


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
    )
    with pytest.raises(CodeHostError) as exc2:
        bad.find_open_pr(repo, "b")
    assert exc2.value.kind == "access" and "ghp_wrongtoken1" not in str(exc2.value)


def test_rate_limit_is_a_transient_error_with_the_reset_time(gh):
    fake, client, repo = gh
    fake.rate_limit_next = [
        {
            "status": 403,
            "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"},
        }
    ]
    with pytest.raises(CodeHostError) as exc:
        client.find_open_pr(repo, "b")
    assert exc.value.kind == "transient" and "2030-01-01" in str(exc.value)
