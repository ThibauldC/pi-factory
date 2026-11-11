"""Names derived from a GitHub issue: its reference, run key and branch (design D4, D5).

* reference: ``pi-relay#123``, used in logs, comments and on the dashboard;
* key: ``pi-relay-123``, used for run directories, session IDs and comment markers;
* branch: ``factory/123-add-retry-to-fetch``.
"""

from __future__ import annotations

import re

SLUG_MAX_LENGTH = 40


def slugify(title: str) -> str:
    """Lowercase, reduce to ``a-z0-9-`` and cut to 40 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-")


def branch_name(number: int, title: str) -> str:
    slug = slugify(title)
    return f"factory/{number}-{slug}" if slug else f"factory/{number}"


def issue_key(repo_key: str, number: int) -> str:
    return f"{repo_key}-{number}"


def issue_ref(repo_key: str, number: int) -> str:
    return f"{repo_key}#{number}"
