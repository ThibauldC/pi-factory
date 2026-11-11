"""Secrets: read the owner-only env file, hand values out through accessors, redact them.

Secrets live only in a dedicated ``KEY=VALUE`` file (mode 0600) that the service manager
also loads as the unit's ``EnvironmentFile``. They never appear in the config file, command
lines, logs, run records or the dashboard. Any error text that might carry a secret goes
through :func:`redact`; :class:`RedactedError` does that automatically in ``__str__``.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

DEFAULT_SECRETS_PATH = "~/.config/pi-factory/secrets.env"
ENV_PREFIX = "PI_FACTORY_"

GITHUB_TOKEN = "PI_FACTORY_GITHUB_TOKEN"

_MIN_REDACT_LEN = 6
_lock = threading.Lock()
_known: set[str] = set()


class SecretsError(Exception):
    """The secrets file is unusable. Messages never contain secret values."""


def register_secret(value: str) -> None:
    """Make ``value`` redacted everywhere :func:`redact` is applied."""
    if len(value) >= _MIN_REDACT_LEN:
        with _lock:
            _known.add(value)


def forget_all_secrets() -> None:
    """Clear the redaction registry (tests)."""
    with _lock:
        _known.clear()


def redact(text: str) -> str:
    """Replace every registered secret value in ``text`` with ``[redacted]``."""
    with _lock:
        values = sorted(_known, key=len, reverse=True)
    for value in values:
        if value in text:
            text = text.replace(value, "[redacted]")
    return text


class RedactedError(Exception):
    """Base class for factory errors whose text may have come from a remote service."""

    def __str__(self) -> str:
        return redact(super().__str__())


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (``#`` comments, optional single or double quotes)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


class Secrets:
    """Holds secret values and exposes them only through accessor methods."""

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)
        for value in self._values.values():
            register_secret(value)

    def _get(self, name: str) -> str:
        value = self._values.get(name, "")
        if not value:
            raise SecretsError(f"secret {name} is not set in the secrets file")
        return value

    def github_token(self) -> str:
        return self._get(GITHUB_TOKEN)

    def has(self, name: str) -> bool:
        return bool(self._values.get(name))

    def __repr__(self) -> str:
        return f"Secrets(names={sorted(self._values)})"


def secrets_path(path: str | os.PathLike[str] | None = None) -> Path:
    chosen = path or os.environ.get("PI_FACTORY_SECRETS_FILE") or DEFAULT_SECRETS_PATH
    return Path(os.path.expanduser(str(chosen)))


def load_secrets(path: str | os.PathLike[str] | None = None) -> Secrets:
    """Load the secrets file, refusing anything but owner-only permissions."""
    p = secrets_path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise SecretsError(f"cannot read secrets file {p}: {exc.strerror or exc}") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        mode = oct(stat.S_IMODE(st.st_mode))
        raise SecretsError(
            f"secrets file {p} has mode {mode}; it must be owner-only (chmod 0600 {p})"
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretsError(f"cannot read secrets file {p}: {exc.strerror or exc}") from exc
    return Secrets(parse_env_file(text))
