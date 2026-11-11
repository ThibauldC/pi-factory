"""Typed factory configuration with strict TOML validation.

Every setting has the default given in the specs. Unknown keys, missing required keys,
wrong types and an empty repository list are all errors, and each error names the
offending key by its full dotted path (for example ``repos.pi-relay.chekcs``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "~/.config/pi-factory/factory.toml"
DEFAULT_DESIGN_TOOLS = ("read", "grep", "find", "ls")

_MISSING: Any = object()


class ConfigError(Exception):
    """The configuration file is invalid. The message names the offending key."""


@dataclass(frozen=True)
class LinearConfig:
    team: str
    owner: str
    auth: str = "oauth"  # "oauth" (client credentials) or "api_key"


@dataclass(frozen=True)
class GitHubConfig:
    bot_name: str = "pi-factory-bot"
    bot_email: str = "pi-factory-bot@users.noreply.github.com"


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(frozen=True)
class PhaseSettings:
    """Per-phase settings; ``None`` means "use pi's own default"."""

    model: str | None = None
    thinking: str | None = None


@dataclass(frozen=True)
class DesignConfig:
    timeout_seconds: float = 15 * 60
    tools: tuple[str, ...] = DEFAULT_DESIGN_TOOLS
    settings: PhaseSettings = field(default_factory=PhaseSettings)


@dataclass(frozen=True)
class BuildConfig:
    timeout_seconds: float = 45 * 60
    command_timeout_seconds: float = 20 * 60
    repair_rounds: int = 2
    settings: PhaseSettings = field(default_factory=PhaseSettings)


@dataclass(frozen=True)
class RepoConfig:
    key: str
    url: str
    checks: tuple[str, ...]
    base_branch: str = "main"
    setup: str | None = None
    env_passthrough: tuple[str, ...] = ()
    design: PhaseSettings = field(default_factory=PhaseSettings)
    build: PhaseSettings = field(default_factory=PhaseSettings)


@dataclass(frozen=True)
class FactoryConfig:
    linear: LinearConfig
    repos: dict[str, RepoConfig]
    github: GitHubConfig = field(default_factory=GitHubConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    design: DesignConfig = field(default_factory=DesignConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    poll_interval_seconds: float = 120.0
    max_concurrent: int = 2
    pi_path: Path = field(default_factory=lambda: Path("~/.pi/agent/bin/pi").expanduser())
    state_root: Path = field(default_factory=lambda: Path("~/factory/state").expanduser())
    worktree_root: Path = field(default_factory=lambda: Path("~/factory/worktrees").expanduser())
    repos_root: Path = field(default_factory=lambda: Path("~/factory/repos").expanduser())
    retention_days: float = 30.0
    min_free_disk_bytes: int = 2 * 1024**3

    @property
    def runs_dir(self) -> Path:
        return self.state_root / "runs"

    @property
    def archive_dir(self) -> Path:
        return self.state_root / "archive"

    def phase_settings(self, repo_key: str, phase: str) -> PhaseSettings:
        """Resolve model and thinking: repo+phase override, then global phase, then pi default."""
        if phase not in ("design", "build"):
            raise ValueError(f"unknown phase {phase!r}")
        repo = getattr(self.repos[repo_key], phase)
        glob = getattr(self, phase).settings
        return PhaseSettings(
            model=repo.model or glob.model,
            thinking=repo.thinking or glob.thinking,
        )


class _Table:
    """Reads keys from a TOML table and reports whatever is left over as unknown."""

    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise ConfigError(f"{path or 'config'} must be a table")
        self.data = data
        self.path = path
        self.seen: set[str] = set()

    def _full(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def get(self, key: str, kind: str, default: Any = _MISSING) -> Any:
        self.seen.add(key)
        if key not in self.data:
            if default is _MISSING:
                raise ConfigError(f"missing required key {self._full(key)}")
            return default
        return _check_type(self._full(key), self.data[key], kind)

    def table(self, key: str, required: bool = False) -> _Table:
        self.seen.add(key)
        if key not in self.data:
            if required:
                raise ConfigError(f"missing required table {self._full(key)}")
            return _Table({}, self._full(key))
        return _Table(self.data[key], self._full(key))

    def done(self) -> None:
        unknown = sorted(set(self.data) - self.seen)
        if unknown:
            raise ConfigError(f"unknown key {self._full(unknown[0])}")


def _check_type(path: str, value: Any, kind: str) -> Any:
    ok: bool
    if kind == "str":
        ok = isinstance(value, str) and value != ""
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif kind == "strlist":
        ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
    else:  # pragma: no cover - programming error
        raise AssertionError(kind)
    if not ok:
        article = {"str": "a non-empty string", "int": "an integer", "number": "a number"}.get(
            kind, "a list of non-empty strings"
        )
        raise ConfigError(f"{path} must be {article}")
    return value


def _positive(path: str, value: float) -> float:
    if value <= 0:
        raise ConfigError(f"{path} must be greater than zero")
    return value


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def _phase_settings(t: _Table) -> PhaseSettings:
    settings = PhaseSettings(
        model=t.get("model", "str", None), thinking=t.get("thinking", "str", None)
    )
    return settings


def _parse_listen(value: str, path: str) -> tuple[str, int]:
    host, sep, port_s = value.rpartition(":")
    if not sep or not host or not port_s.isdigit() or not 0 < int(port_s) < 65536:
        raise ConfigError(f"{path} must look like 127.0.0.1:8787")
    host = host.strip("[]")
    if not (host == "localhost" or host == "::1" or host.startswith("127.")):
        raise ConfigError(f"{path} must be a loopback address (got {host}); use tailscale serve")
    return host, int(port_s)


def _parse_repo(key: str, data: Any) -> RepoConfig:
    t = _Table(data, f"repos.{key}")
    url = t.get("url", "str")
    base_branch = t.get("base_branch", "str", "main")
    setup = t.get("setup", "str", None)
    checks = t.get("checks", "strlist")
    env_passthrough = t.get("env_passthrough", "strlist", [])
    dt, bt = t.table("design"), t.table("build")
    design, build = _phase_settings(dt), _phase_settings(bt)
    dt.done()
    bt.done()
    t.done()
    if not checks:
        raise ConfigError(
            f"repository '{key}' needs at least one check command (repos.{key}.checks is empty)"
        )
    return RepoConfig(
        key=key,
        url=url,
        checks=tuple(checks),
        base_branch=base_branch,
        setup=setup,
        env_passthrough=tuple(env_passthrough),
        design=design,
        build=build,
    )


def parse_config(data: dict[str, Any]) -> FactoryConfig:
    """Validate a parsed TOML document and build a :class:`FactoryConfig`."""
    root = _Table(data, "")

    lt = root.table("linear", required=True)
    auth = lt.get("auth", "str", "oauth")
    if auth not in ("oauth", "api_key"):
        raise ConfigError('linear.auth must be "oauth" or "api_key"')
    linear = LinearConfig(team=lt.get("team", "str"), owner=lt.get("owner", "str"), auth=auth)
    lt.done()

    gt = root.table("github")
    defaults = GitHubConfig()
    github = GitHubConfig(
        bot_name=gt.get("bot_name", "str", defaults.bot_name),
        bot_email=gt.get("bot_email", "str", defaults.bot_email),
    )
    gt.done()

    ft = root.table("factory")
    base = FactoryConfig(linear=linear, repos={})
    poll = _positive(
        "factory.poll_interval_seconds",
        ft.get("poll_interval_seconds", "number", base.poll_interval_seconds),
    )
    max_concurrent = ft.get("max_concurrent", "int", base.max_concurrent)
    if max_concurrent < 1:
        raise ConfigError("factory.max_concurrent must be at least 1")
    pi_path = _path(ft.get("pi_path", "str", str(base.pi_path)))
    state_root = _path(ft.get("state_root", "str", str(base.state_root)))
    worktree_root = _path(ft.get("worktree_root", "str", str(base.worktree_root)))
    repos_root = _path(ft.get("repos_root", "str", str(base.repos_root)))
    retention = _positive(
        "factory.retention_days", ft.get("retention_days", "number", base.retention_days)
    )
    listen = ft.get("dashboard_listen", "str", f"{base.dashboard.host}:{base.dashboard.port}")
    host, port = _parse_listen(listen, "factory.dashboard_listen")
    ft.done()

    dt = root.table("design")
    design = DesignConfig(
        timeout_seconds=_positive("design.timeout_minutes", dt.get("timeout_minutes", "number", 15))
        * 60,
        tools=tuple(dt.get("tools", "strlist", list(DEFAULT_DESIGN_TOOLS))),
        settings=_phase_settings(dt),
    )
    dt.done()

    bt = root.table("build")
    repair_rounds = bt.get("repair_rounds", "int", base.build.repair_rounds)
    if repair_rounds < 0:
        raise ConfigError("build.repair_rounds must not be negative")
    build = BuildConfig(
        timeout_seconds=_positive("build.timeout_minutes", bt.get("timeout_minutes", "number", 45))
        * 60,
        command_timeout_seconds=_positive(
            "build.command_timeout_minutes", bt.get("command_timeout_minutes", "number", 20)
        )
        * 60,
        repair_rounds=repair_rounds,
        settings=_phase_settings(bt),
    )
    bt.done()

    rt = root.table("repos")
    repos = {key: _parse_repo(key, value) for key, value in rt.data.items()}
    root.done()
    if not repos:
        raise ConfigError(
            "the configuration must define at least one repository under [repos.<key>]"
        )

    return FactoryConfig(
        linear=linear,
        repos=repos,
        github=github,
        dashboard=DashboardConfig(host=host, port=port),
        design=design,
        build=build,
        poll_interval_seconds=float(poll),
        max_concurrent=max_concurrent,
        pi_path=pi_path,
        state_root=state_root,
        worktree_root=worktree_root,
        repos_root=repos_root,
        retention_days=float(retention),
    )


def load_config(path: str | os.PathLike[str] | None = None) -> FactoryConfig:
    """Read and validate the TOML file at ``path`` (default: DEFAULT_CONFIG_PATH)."""
    p = Path(os.path.expanduser(str(path) if path else DEFAULT_CONFIG_PATH))
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
    return parse_config(data)
