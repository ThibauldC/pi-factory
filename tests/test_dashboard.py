import re
import socket
import urllib.error
import urllib.request

import pytest

from pi_factory.dashboard import Dashboard
from pi_factory.scheduler import Scheduler

from .test_scheduler import DESIGN_OK, wait_for


@pytest.fixture
def dash_env(make_env):
    started = []

    def make(**kw):
        env = make_env(**kw)
        sched = Scheduler(env.deps)
        dash = Dashboard(sched, env.runs, env.cfg, port=0).start()
        started.append(dash)
        return env, sched, dash

    yield make
    for d in started:
        d.stop()


def get(dash, path="/"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{dash.port}{path}", timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def request(dash, method, path="/", data=b""):
    req = urllib.request.Request(
        f"http://127.0.0.1:{dash.port}{path}", data=data or None, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_overview_loads_refreshes_every_10_seconds_without_scripts(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK], repos=("pi-relay", "other"))
    status, body, headers = get(dash)
    assert status == 200 and "text/html" in headers["Content-Type"]
    assert "<meta http-equiv='refresh' content='10'>" in body
    assert "<script" not in body.lower()
    assert "pi-relay" in body and "other" in body and "idle" in body
    assert "no poll errors" in body and "nothing queued" in body


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/", "/issue/pi-relay-1", "/anything"])
def test_any_non_get_request_is_405_and_changes_nothing(dash_env, method, path):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    calls = len(env.github.calls)
    assert request(dash, method, path, data=b"x=1") == 405
    assert len(env.github.calls) == calls and env.runs.list_issues() == []
    assert env.github.state(1) == "ready-for-design"


def test_head_and_options_are_not_allowed_either(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    assert request(dash, "HEAD") == 405 and request(dash, "OPTIONS") == 405


def test_active_build_shown_under_its_repo_with_phase_step_round_elapsed(dash_env):
    env, sched, dash = dash_env(
        pi_turns=[{"edits": {"a.txt": "1"}}],
        checks=["echo CHECK-LINE-ONE; sleep 20"],
        repos=("pi-relay", "other"),
    )
    env.seed_issue(1, state="ready-for-build", title="Add retry to fetch")
    sched.poll_once()
    assert wait_for(lambda: sched.tasks and sched.tasks["pi-relay-1"].step == "checks")
    assert wait_for(
        lambda: "CHECK-LINE-ONE" in "\n".join(sched.tasks["pi-relay-1"].check_ring.snapshot())
    )
    _, body, _ = get(dash)
    row = next(
        r for r in re.findall(r"<tr>.*?</tr>", body, re.S) if "pi-relay" in r and "pi-relay#1" in r
    )
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td>(.*?)</td>", row, re.S)]
    assert cells[0] == "pi-relay" and "pi-relay#1" in cells[1]
    assert cells[2] == "build" and cells[3] == "checks" and cells[4] == "1"
    assert re.fullmatch(r"\d+m \d\ds", cells[5])
    # live tail of the running check command on the issue page
    status, issue_body, _ = get(dash, "/issue/pi-relay-1")
    assert status == 200 and "CHECK-LINE-ONE" in issue_body and "Check / setup output" in issue_body
    assert "pi-relay" in issue_body and "checks" in issue_body
    sched.tasks["pi-relay-1"].cancel()
    assert sched.wait_idle(30)


def test_issue_page_tails_are_capped_and_show_newest_lines(dash_env):
    env, sched, dash = dash_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1)
    sched.poll_once()
    assert wait_for(lambda: sched.tasks and env.pi.starts())
    task = sched.tasks["pi-relay-1"]
    for n in range(500):
        task.check_ring.append(f"line {n}")
    _, body, _ = get(dash, "/issue/pi-relay-1")
    assert "line 499" in body and "line 299" not in body and "line 300" in body
    task.cancel()
    sched.wait_idle(30)


def test_unknown_issue_and_path_are_404(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    assert get(dash, "/issue/NOPE-1")[0] == 404
    assert get(dash, "/issue/../../etc/passwd")[0] == 404
    assert get(dash, "/elsewhere")[0] == 404


def test_poll_error_shows_type_and_time_but_not_the_token_or_secret(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    env.github.token = "rotated-elsewhere"  # GitHub now rejects the factory's token
    assert sched.poll_once() is False
    sched.poll_errors.append((1_700_000_000.0, "Authorization: Bearer ghp_bottoken"))
    status, body, _ = get(dash)
    assert "HTTP 401" in body and "2023-11-14" in body
    for secret in ("ghp_bottoken", "rotated-elsewhere"):
        assert secret not in body
    assert "failed" in body and "consecutive failures: 1" in body


def test_recent_runs_show_outcome_links_and_token_totals(dash_env):
    from .test_build_phase import GOOD_DESIGN

    env, sched, dash = dash_env(
        pi_turns=[
            {"text": GOOD_DESIGN, "usage": {"input": 1000, "output": 100}},
            {"edits": {"a.txt": "1"}, "usage": {"input": 2000, "output": 200}},
        ]
    )
    env.seed_issue(1)
    sched.poll_once()
    assert sched.wait_idle(30)
    env.github.move(1, "ready-for-build")
    sched.poll_once()
    assert sched.wait_idle(60)
    _, body, _ = get(dash)
    assert "in_review" in body and "3,000 in / 300 out" in body
    assert "https://github.com/owner/pi-relay/pull/2" in body
    assert "https://github.com/owner/pi-relay/issues/1" in body and "pi-relay#1" in body
    # after the merge closes the issue and cleanup, the run is listed from the archive
    env.github.merge_pr(env.github.open_prs()[0]["number"])
    sched.poll_once()
    assert env.runs.list_issues() == []
    _, body, _ = get(dash)
    assert "in_review" in body and "pi-relay#1" in body and "issue closed: completed" in body


def test_queue_is_listed_in_start_order(dash_env):
    env, sched, dash = dash_env(pi_turns=[{"behavior": "hang"}], max_concurrent=1)
    env.seed_issue(1, labels=("priority:low",))
    env.seed_issue(2, labels=("priority:urgent",))
    sched.poll_once()
    assert wait_for(lambda: env.pi.starts())
    sched.poll_once()
    _, body, _ = get(dash)
    queue = body.split("<h2>Queue</h2>")[1].split("<h2>")[0]
    assert "pi-relay#1" in queue and "priority:low" in queue  # #2 (urgent) is already running
    assert "pi-relay#2" not in queue
    for t in list(sched.tasks.values()):
        t.cancel()
    sched.wait_idle(30)


def test_all_values_are_html_escaped(dash_env):
    env, sched, dash = dash_env(pi_turns=[{"behavior": "hang"}])
    env.seed_issue(1, title="<script>alert(1)</script> & <b>x</b>")
    sched.poll_once()
    assert wait_for(lambda: env.pi.starts())
    task = sched.tasks["pi-relay-1"]
    task.agent_ring.append("<img src=x onerror=alert(1)>")
    for path in ("/", "/issue/pi-relay-1"):
        _, body, _ = get(dash, path)
        assert "<script>alert" not in body and "<img src=x" not in body
        assert "&lt;" in body
    task.cancel()
    sched.wait_idle(30)


def test_non_http_links_are_not_rendered_as_anchors():
    from pi_factory.dashboard import link

    assert link("javascript:alert(1)", "x") == "x"
    assert link("https://example.com/a?b=1&c=2", "t").startswith(
        '<a href="https://example.com/a?b=1&amp;c=2">'
    )


def test_listens_on_loopback_only_and_refuses_other_addresses(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    assert dash.server.server_address[0] == "127.0.0.1"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packets are sent; this just picks the outbound IP
        own_ip = probe.getsockname()[0]
    except OSError:
        pytest.skip("no non-loopback address on this host")
    finally:
        probe.close()
    if own_ip.startswith("127."):
        pytest.skip("no non-loopback address on this host")
    with pytest.raises(OSError):
        socket.create_connection((own_ip, dash.port), timeout=2)


def test_dashboard_never_prints_environment_or_secret_file_contents(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    for path in ("/", "/issue/pi-relay-1"):
        _, body, _ = get(dash, path)
        assert "PI_FACTORY_" not in body and "environ" not in body.lower()


def test_issues_with_conflicting_state_labels_are_listed_as_ignored(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1, state="design-review", title="Two labels")
    env.github.add_label(1, "factory:ready-for-build")
    sched.poll_once()
    _, body, _ = get(dash)
    section = body.split("Ignored issues")[1].split("<h2>Poll errors")[0]
    assert "pi-relay#1" in section and "Two labels" in section
    assert "factory:design-review" in section and "factory:ready-for-build" in section


def test_issue_page_uses_the_run_key_and_shows_the_issue_reference(dash_env):
    env, sched, dash = dash_env(pi_turns=[DESIGN_OK])
    env.seed_issue(1)
    sched.poll_once()
    assert sched.wait_idle(30)
    status, body, _ = get(dash, "/issue/pi-relay-1")
    assert status == 200 and "<h1>pi-relay#1</h1>" in body
