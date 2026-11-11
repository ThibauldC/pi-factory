import re
from pathlib import Path

from pi_factory.tracker.base import REQUIRED_STATES

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()


def spec_text() -> str:
    for candidate in (
        ROOT / "openspec/changes/add-initial-factory/specs/task-lifecycle/spec.md",
        ROOT / "openspec/specs/task-lifecycle/spec.md",
    ):
        if candidate.exists():
            return candidate.read_text()
    raise AssertionError("task-lifecycle spec not found")


def states_table() -> dict[str, list[str]]:
    rows = {}
    for line in README.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0] not in ("State", "---", "") and not set(cells[0]) <= {"-"}:
            rows[cells[0]] = cells
    return rows


def test_every_link_in_the_readme_resolves():
    links = re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", README)
    assert links
    for target in links:
        if target.startswith(("http://", "https://")):
            continue
        assert (ROOT / target).exists(), f"broken link: {target}"


def test_states_table_lists_every_required_state_plus_done_and_canceled():
    rows = states_table()
    for state in REQUIRED_STATES:
        assert state in rows, f"{state} missing from the README states table"
    assert {"Done", "Canceled"} <= set(rows)


def test_states_table_matches_the_task_lifecycle_spec():
    spec = spec_text()
    rows = states_table()
    # every factory-owned transition in the spec is stated in the README and agrees with it
    transitions = re.findall(r"^- (.+?) → (.+)$", spec, re.M)
    assert len(transitions) == 5
    for src, dst in transitions:
        if src.startswith("any active"):
            assert rows[dst][1] == "factory"
            continue
        assert f"{src} → {dst}" in " ".join(README.split())
        assert rows[dst][1] == "factory"
    # states humans set vs states the factory sets
    for human_state in ("Ready for Design", "Ready for Build"):
        assert rows[human_state][1] == "you"
    # the spec's required state list equals the code's
    required = re.search(r"Ready for Design, Designing, Design Review.*?Blocked", spec, re.S)
    assert required
    for state in REQUIRED_STATES:
        assert state in required.group(0)
    assert "never move" in spec.lower() or "SHALL NOT move an issue to Done" in spec


def test_docs_mention_the_commands_that_exist():
    ops = (ROOT / "docs/operations.md").read_text()
    for needle in (
        "pi-factory check",
        "git pull && uv sync",
        "systemctl",
        "tailscale serve",
        "0600",
    ):
        assert needle in ops
