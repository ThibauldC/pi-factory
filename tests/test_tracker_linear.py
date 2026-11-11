import pytest

from pi_factory.config import LinearConfig
from pi_factory.secrets import Secrets, forget_all_secrets
from pi_factory.tracker.base import (
    BLOCKED,
    BUILDING,
    DESIGN_REVIEW,
    READY_FOR_BUILD,
    READY_FOR_DESIGN,
    TrackerError,
)
from pi_factory.tracker.linear import LinearTracker

from .fakes.linear import DEFAULT_STATES, FakeLinear

SECRETS = {
    "PI_FACTORY_LINEAR_CLIENT_ID": "cid",
    "PI_FACTORY_LINEAR_CLIENT_SECRET": "csecret",
    "PI_FACTORY_LINEAR_API_KEY": "lin_api_secretkey",
}


@pytest.fixture
def linear():
    fake = FakeLinear().start()
    yield fake
    fake.stop()
    forget_all_secrets()


def make(fake, auth="oauth", owner="owner@example.com", clock=None, **kw):
    cfg = LinearConfig(team="THI", owner=owner, auth=auth)
    extra = {"clock": clock} if clock else {}
    return LinearTracker(
        cfg, Secrets(SECRETS), api_url=fake.api_url, token_url=fake.token_url, **extra, **kw
    )


# --- contract: Tracker protocol against the fake ---------------------------------------------


def test_list_issues_returns_polled_states_and_tracked_ids(linear):
    a = linear.add_issue("THI-1", state=READY_FOR_DESIGN, labels=("pi-relay",), priority=2)
    linear.add_issue("THI-2", state="Backlog")
    linear.add_issue("THI-3", state="Design Review")
    done = linear.add_issue("THI-4", state="Done")
    linear.add_comment("THI-1", "hello there")
    t = make(linear)
    issues = {i.identifier: i for i in t.list_issues([done])}
    assert set(issues) == {"THI-1", "THI-4"}
    one = issues["THI-1"]
    assert one.id == a and one.priority == 2 and one.state == READY_FOR_DESIGN
    assert one.repo_labels() == ["pi-relay"]
    assert one.branch_name == "thi-1-add-retry-to-fetch" and one.url
    assert [c.body for c in one.comments] == ["hello there"]
    assert issues["THI-4"].is_finished
    # Exactly one query per poll.
    assert len(linear.ops("Issues")) == 1


def test_list_issues_paginates(linear):
    linear.page_size = 2
    for n in range(5):
        linear.add_issue(f"THI-{n}", state=READY_FOR_DESIGN)
    issues = make(linear).list_issues()
    assert len(issues) == 5 and len(linear.ops("Issues")) == 3


def test_get_issue_states(linear):
    a = linear.add_issue("THI-1", state=BUILDING)
    b = linear.add_issue("THI-2", state="Backlog")
    assert make(linear).get_issue_states([a, b]) == {a: BUILDING, b: "Backlog"}
    assert make(linear).get_issue_states([]) == {}


def test_set_state_and_assign(linear):
    a = linear.add_issue("THI-1", state=READY_FOR_DESIGN)
    t = make(linear)
    assert t.check_setup(["pi-relay"]) == []
    t.set_state(a, DESIGN_REVIEW)
    t.assign_owner(a)
    assert linear.issue("THI-1")["state"] == DESIGN_REVIEW
    assert linear.issue("THI-1")["assignee"] == "u-owner"
    with pytest.raises(TrackerError, match="does not exist"):
        t.set_state(a, "Nonexistent")


def test_state_ids_are_looked_up_lazily_if_setup_not_run(linear):
    a = linear.add_issue("THI-1")
    t = make(linear)
    t.set_state(a, BLOCKED)
    assert linear.issue("THI-1")["state"] == BLOCKED


def test_post_and_list_comments(linear):
    a = linear.add_issue("THI-1")
    linear.add_comment("THI-1", "human first")
    t = make(linear)
    posted = t.post_comment(a, "factory says hi")
    assert posted.id and posted.body == "factory says hi"
    comments = t.list_comments(a)
    assert [c.body for c in comments] == ["human first", "factory says hi"]
    assert comments[1].id == posted.id
    assert comments[0].created_at < comments[1].created_at


def test_check_setup_reports_missing_states_labels_owner(linear):
    fake = FakeLinear(
        states=[(n, t) for n, t in DEFAULT_STATES if n != "Design Review"],
        repo_labels=("pi-relay",),
    ).start()  # fmt: skip
    try:
        t = make(fake, owner="nobody@example.com")
        problems = " | ".join(t.check_setup(["pi-relay", "ghost"]))
        assert (
            "Design Review" in problems and "ghost" in problems and "nobody@example.com" in problems
        )
    finally:
        fake.stop()


def test_check_setup_unknown_team_mentions_public(linear):
    cfg = LinearConfig(team="NOPE", owner="owner@example.com")
    t = LinearTracker(cfg, Secrets(SECRETS), api_url=linear.api_url, token_url=linear.token_url)
    problems = t.check_setup([])
    assert len(problems) == 1 and "public" in problems[0]


def test_owner_can_be_matched_by_name(linear):
    t = make(linear, owner="olivia")
    assert t.check_setup(["pi-relay"]) == []


# --- service-credentials scenarios ---------------------------------------------------------


def test_app_credentials_fetch_a_token_and_cache_it(linear):
    t = make(linear)
    t.list_issues()
    t.list_issues()
    assert linear.token_requests == 1
    assert all(c["auth"].startswith("Bearer tok-") for c in linear.calls)


def test_token_refreshed_before_expiry(linear):
    now = [1000.0]
    linear.token_ttl = 3600
    t = make(linear, clock=lambda: now[0])
    t.list_issues()
    now[0] += 3600 - 30  # inside the 60 s refresh margin
    t.list_issues()
    assert linear.token_requests == 2


def test_expired_token_refreshed_after_auth_error_and_request_retried_once(linear, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="pi_factory")
    from pi_factory import logs

    logs.log.propagate = True
    try:
        t = make(linear)
        linear.add_issue("THI-1", state=READY_FOR_DESIGN)
        t.list_issues()
        linear.valid_tokens.clear()  # server-side expiry
        issues = t.list_issues()  # 401 -> new token -> retry succeeds
        assert len(issues) == 1 and linear.token_requests == 2
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "linear_token_refreshed" in text and "tok-2" not in text
    finally:
        logs.log.propagate = False


def test_persistent_auth_failure_does_not_loop(linear):
    t = make(linear)
    linear.client_secret = "rotated"  # token endpoint now rejects us
    with pytest.raises(TrackerError):
        t.list_issues()
    assert linear.token_requests <= 2


def test_api_key_fallback_uses_key_directly():
    fake = FakeLinear(auth="api_key").start()
    try:
        fake.add_issue("THI-1", state=READY_FOR_DESIGN)
        t = make(fake, auth="api_key")
        assert t.check_setup(["pi-relay"]) == []
        assert [i.identifier for i in t.list_issues()] == ["THI-1"]
        t.set_state(fake.issue("THI-1")["id"], READY_FOR_BUILD)
        assert fake.token_requests == 0
        assert {c["auth"] for c in fake.calls} == {"lin_api_secretkey"}
    finally:
        fake.stop()
        forget_all_secrets()


def test_errors_never_contain_secrets(linear):
    linear.fail_next = [500, 500]
    t = make(linear)
    with pytest.raises(TrackerError) as exc:
        t.list_issues()
    assert exc.value.transient
    for secret in ("csecret", "lin_api_secretkey", "tok-1"):
        assert secret not in str(exc.value)


def test_network_failure_is_transient(linear):
    t = LinearTracker(
        LinearConfig(team="THI", owner="o"),
        Secrets(SECRETS),
        api_url="http://127.0.0.1:1/graphql",
        token_url="http://127.0.0.1:1/oauth/token",
        timeout=2,
    )
    with pytest.raises(TrackerError) as exc:
        t.list_issues()
    assert exc.value.transient


def test_graphql_errors_are_not_transient(linear):
    linear.fail_next = []
    t = make(linear)
    with pytest.raises(TrackerError) as exc:
        t._graphql("query Nope { nope }", {})  # the fake has no such operation
    assert not exc.value.transient
