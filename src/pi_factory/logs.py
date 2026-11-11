"""Structured one-line-per-event logging to stdout (picked up by the systemd journal)."""

from __future__ import annotations

import json
import logging
import sys

from .secrets import redact

LOGGER_NAME = "pi_factory"
log = logging.getLogger(LOGGER_NAME)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def setup_logging(stream=None, level: int = logging.INFO) -> None:
    """Send factory log lines to stdout. journald adds its own timestamps."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(_RedactFilter())
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False


def _fmt(value: object) -> str:
    text = str(value)
    if text == "" or any(c in text for c in " \t\n\"'=") or len(text) > 200:
        return json.dumps(text[:200] + ("..." if len(text) > 200 else ""))
    return text


def event(name: str, issue: str | None = None, *, level: int = logging.INFO, **fields) -> None:
    """Log ``name issue=<id> key=value ...`` on one line."""
    parts = [name]
    if issue:
        parts.append(f"issue={issue}")
    parts.extend(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
    log.log(level, " ".join(parts))
