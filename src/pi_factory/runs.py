"""Run directories: the atomic run record, saved outputs, archive and retention.

Layout under ``<state_root>``::

    runs/<KEY>/run.json              the run record (KEY is the issue key, e.g. pi-relay-123)
    runs/<KEY>/sessions/           pi session files
    runs/<KEY>/commands/           output of every setup and check command
    runs/<KEY>/posted/             copies of every comment and PR body the factory posted
    archive/<KEY>--<stamp>/        cleaned-up runs, pruned after the retention period
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STEPS = ("setup", "pi_turn", "checks", "commit", "push", "pr")
ARCHIVED_MARKER = ".archived_at"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    issue: str  # the issue key, e.g. "pi-relay-123" (also the run directory name)
    repo: str  # the repository key
    issue_number: int = 0
    branch: str = ""
    title: str = ""
    issue_url: str = ""
    phase: str = ""  # "design" or "build"
    round: int = 0  # the current phase's round
    rounds: dict[str, int] = field(default_factory=dict)  # {"design": n, "build": n}
    step: str = ""
    session_dir: str = ""
    session_file: str = ""
    session_ids: dict[str, str] = field(default_factory=dict)
    design_comment_id: str | None = None
    design_comment_at: str = ""
    design_text: str = ""
    factory_comment_ids: list[str] = field(default_factory=list)
    recoveries: dict[str, int] = field(default_factory=dict)
    usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    pr_number: int | None = None
    pr_url: str = ""
    last_build_comment_at: str | None = None
    repairs_used: int = 0
    checks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    outcome: str = ""  # "", "designed", "in_review", "blocked"
    outcome_detail: str = ""
    swap: dict[str, str] | None = None  # {"from": label, "to": label} while a swap is running
    close_reason: str = ""  # "completed" or "not_planned" once the issue is closed
    started_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    @property
    def ref(self) -> str:
        """The issue reference, e.g. ``pi-relay#123``."""
        return f"{self.repo}#{self.issue_number}" if self.issue_number else self.issue

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` so a crash leaves either the old or the new file, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class RunStore:
    def __init__(self, runs_dir: Path, archive_dir: Path):
        self.runs_dir = Path(runs_dir)
        self.archive_dir = Path(archive_dir)
        self._lock = threading.Lock()

    # -- paths ---------------------------------------------------------------------------
    def run_dir(self, issue: str) -> Path:
        return self.runs_dir / issue

    def record_path(self, issue: str) -> Path:
        return self.run_dir(issue) / "run.json"

    def sessions_dir(self, issue: str) -> Path:
        return self.run_dir(issue) / "sessions"

    # -- record --------------------------------------------------------------------------
    def create(self, issue: str, repo: str, **kw: Any) -> RunRecord:
        rec = RunRecord(issue=issue, repo=repo, **kw)
        self.sessions_dir(issue).mkdir(parents=True, exist_ok=True)
        self.save(rec)
        return rec

    def exists(self, issue: str) -> bool:
        return self.record_path(issue).is_file()

    def load(self, issue: str) -> RunRecord | None:
        try:
            raw = self.record_path(issue).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return RunRecord.from_dict(json.loads(raw))

    def save(self, rec: RunRecord) -> None:
        rec.updated_at = utcnow()
        with self._lock:
            atomic_write_text(self.record_path(rec.issue), json.dumps(rec.to_dict(), indent=2))

    def set_step(self, rec: RunRecord, step: str) -> None:
        """Record ``step`` durably *before* the work for that step starts."""
        if step not in STEPS:
            raise ValueError(f"unknown step {step!r}")
        rec.step = step
        self.save(rec)

    def list_issues(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.name for p in self.runs_dir.iterdir() if (p / "run.json").is_file())

    # -- saved texts -----------------------------------------------------------------------
    def write_output(self, issue: str, name: str, text: str) -> Path:
        """Store the output of a setup or check command."""
        path = self.run_dir(issue) / "commands" / name
        atomic_write_text(path, text)
        return path

    def write_posted(self, issue: str, name: str, text: str) -> Path:
        """Store a copy of a comment or PR body the factory posted."""
        path = self.run_dir(issue) / "posted" / name
        atomic_write_text(path, text)
        return path

    # -- archive and retention ------------------------------------------------------------
    def archive(self, issue: str, now: float | None = None) -> Path | None:
        src = self.run_dir(issue)
        if not src.is_dir():
            return None
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        dest = self.archive_dir / f"{issue}--{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = self.archive_dir / f"{issue}--{stamp}-{n}"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        (dest / ARCHIVED_MARKER).write_text(str(int(now if now is not None else time.time())))
        return dest

    def prune(self, retention_days: float, now: float | None = None) -> list[Path]:
        """Delete archived runs older than the retention period. Never touches ``runs/``."""
        now = time.time() if now is None else now
        removed: list[Path] = []
        if not self.archive_dir.is_dir():
            return removed
        for entry in sorted(self.archive_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                archived_at = float((entry / ARCHIVED_MARKER).read_text())
            except (OSError, ValueError):
                archived_at = entry.stat().st_mtime
            if now - archived_at > retention_days * 86400:
                shutil.rmtree(entry, ignore_errors=True)
                removed.append(entry)
        return removed

    def recent_archived(self, limit: int = 20) -> list[RunRecord]:
        """Most recently archived run records (for the dashboard)."""
        out: list[tuple[float, RunRecord]] = []
        if self.archive_dir.is_dir():
            for entry in self.archive_dir.iterdir():
                with contextlib.suppress(OSError, ValueError, TypeError):
                    stamp = float((entry / ARCHIVED_MARKER).read_text())
                    out.append(
                        (stamp, RunRecord.from_dict(json.loads((entry / "run.json").read_text())))
                    )
        out.sort(key=lambda t: t[0], reverse=True)
        return [r for _, r in out[:limit]]
