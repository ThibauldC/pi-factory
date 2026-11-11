import pytest

from pi_factory.config import ConfigError, load_config, parse_config

MINIMAL = {
    "linear": {"team": "THI", "owner": "me@example.com"},
    "repos": {
        "pi-relay": {
            "url": "https://github.com/o/pi-relay.git",
            "checks": ["pnpm test"],
        }
    },
}


def repo_with(**kw):
    repo = dict(MINIMAL["repos"]["pi-relay"])
    repo.update(kw)
    return {"linear": MINIMAL["linear"], "repos": {"pi-relay": repo}}


def test_default_path(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".config" / "pi-factory"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "factory.toml").write_text(
        '[linear]\nteam="T"\nowner="o"\n[repos.r]\nurl="u"\nchecks=["x"]\n'
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_config().linear.team == "T"


def test_minimal_config_defaults():
    cfg = parse_config(MINIMAL)
    assert cfg.poll_interval_seconds == 120
    assert cfg.max_concurrent == 2
    assert cfg.build.repair_rounds == 2
    assert cfg.design.timeout_seconds == 15 * 60
    assert cfg.build.timeout_seconds == 45 * 60
    assert cfg.retention_days == 30
    assert cfg.dashboard.host == "127.0.0.1" and cfg.dashboard.port == 8787
    assert cfg.pi_path.name == "pi" and ".pi/agent/bin" in str(cfg.pi_path)
    assert cfg.linear.auth == "oauth"
    assert cfg.design.tools == ("read", "grep", "find", "ls")
    assert cfg.repos["pi-relay"].base_branch == "main"
    assert cfg.repos["pi-relay"].setup is None


def test_repository_entry():
    cfg = parse_config(
        repo_with(
            base_branch="main",
            setup="pnpm install --frozen-lockfile",
            checks=["pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build"],
        )
    )
    repo = cfg.repos["pi-relay"]
    assert repo.checks == ("pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build")
    assert repo.setup == "pnpm install --frozen-lockfile"


def test_typo_in_key_names_full_path():
    data = repo_with()
    data["repos"]["pi-relay"]["chekcs"] = ["x"]
    with pytest.raises(ConfigError, match=r"repos\.pi-relay\.chekcs"):
        parse_config(data)


def test_typo_replacing_checks_reports_unknown_key():
    data = repo_with()
    del data["repos"]["pi-relay"]["checks"]
    data["repos"]["pi-relay"]["chekcs"] = ["x"]
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    assert "repos.pi-relay" in str(exc.value)


def test_empty_checks_rejected():
    with pytest.raises(ConfigError, match="at least one check"):
        parse_config(repo_with(checks=[]))


def test_no_repos_rejected():
    with pytest.raises(ConfigError, match="at least one repository"):
        parse_config({"linear": MINIMAL["linear"], "repos": {}})
    with pytest.raises(ConfigError, match="at least one repository"):
        parse_config({"linear": MINIMAL["linear"]})


def test_missing_required_and_wrong_types():
    with pytest.raises(ConfigError, match=r"linear\.owner"):
        parse_config({"linear": {"team": "T"}, "repos": MINIMAL["repos"]})
    with pytest.raises(ConfigError, match=r"repos\.pi-relay\.url"):
        parse_config(repo_with(url=3))
    data = dict(MINIMAL)
    data["factory"] = {"max_concurrent": "two"}
    with pytest.raises(ConfigError, match=r"factory\.max_concurrent"):
        parse_config(data)
    data["factory"] = {"max_concurrent": True}
    with pytest.raises(ConfigError, match=r"factory\.max_concurrent"):
        parse_config(data)


def test_unknown_top_level_and_section_keys():
    with pytest.raises(ConfigError, match=r"unknown key bogus"):
        parse_config({**MINIMAL, "bogus": 1})
    with pytest.raises(ConfigError, match=r"unknown key factory\.nope"):
        parse_config({**MINIMAL, "factory": {"nope": 1}})
    with pytest.raises(ConfigError, match=r"unknown key repos\.pi-relay\.design\.modle"):
        parse_config(repo_with(design={"modle": "x"}))


def test_model_resolution_order():
    data = repo_with(design={"model": "B"})
    data["design"] = {"model": "A", "thinking": "high"}
    data["repos"]["other"] = {"url": "u", "checks": ["c"]}
    cfg = parse_config(data)
    assert cfg.phase_settings("pi-relay", "design").model == "B"
    assert cfg.phase_settings("pi-relay", "design").thinking == "high"
    assert cfg.phase_settings("other", "design").model == "A"
    nothing = cfg.phase_settings("other", "build")
    assert nothing.model is None and nothing.thinking is None


def test_dashboard_must_be_loopback():
    with pytest.raises(ConfigError, match="loopback"):
        parse_config({**MINIMAL, "factory": {"dashboard_listen": "0.0.0.0:8787"}})
    cfg = parse_config({**MINIMAL, "factory": {"dashboard_listen": "127.0.0.1:9000"}})
    assert cfg.dashboard.port == 9000


def test_invalid_toml_and_missing_file(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[linear\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(bad)
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "nope.toml")


def test_api_key_auth_and_validation():
    cfg = parse_config({**MINIMAL, "linear": {**MINIMAL["linear"], "auth": "api_key"}})
    assert cfg.linear.auth == "api_key"
    with pytest.raises(ConfigError, match=r"linear\.auth"):
        parse_config({**MINIMAL, "linear": {**MINIMAL["linear"], "auth": "pw"}})
