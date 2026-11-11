"""The allowlisted environment for pi and for setup and check commands (design D7)."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from .secrets import ENV_PREFIX

_EXACT = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ")
_PREFIXES = ("LC_", "XDG_")


def build_child_env(
    passthrough: Iterable[str] = (),
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment children get. Everything not listed here is dropped.

    ``PI_FACTORY_*`` names (the factory's secrets) can never be passed through.
    """
    src = os.environ if source is None else source
    env: dict[str, str] = {}
    for key, value in src.items():
        if key in _EXACT or key.startswith(_PREFIXES):
            env[key] = value
    home = env.get("HOME") or os.path.expanduser("~")
    data_home = src.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    path_parts = [p for p in src.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(":") if p]
    for extra in (
        os.path.join(home, ".local", "bin"),
        os.path.join(data_home, "pi-node/current/bin"),
    ):
        if extra not in path_parts:
            path_parts.append(extra)
    env["PATH"] = ":".join(path_parts)
    env["CI"] = "1"
    env["TERM"] = "dumb"
    for name in passthrough:
        if name.startswith(ENV_PREFIX):
            continue
        if name in src:
            env[name] = src[name]
    return env
