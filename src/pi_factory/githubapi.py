"""A small GitHub REST client over ``urllib`` shared by the tracker and code host adapters.

It sends the machine-account token as ``Authorization: Bearer`` with a pinned API version,
follows pagination, and turns failures into :class:`GitHubAPIError` with a ``kind``:

* ``transient``: network errors and 5xx answers;
* ``rate_limit``: primary and secondary (abuse) rate limits, with the reset time when known;
* ``access``: 401 and 403 (bad credentials, no permission);
* ``not_found``: 404;
* ``other``: any other unexpected status.

``transient`` and ``rate_limit`` are retryable; callers never retry them in a tight loop.
Error text goes through the secret redaction, and no token is ever put into a message.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import logs
from .secrets import RedactedError, Secrets

API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 50


class GitHubAPIError(RedactedError):
    def __init__(
        self,
        message: str,
        kind: str = "other",
        status: int = 0,
        reset_at: float | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.reset_at = reset_at

    @property
    def retryable(self) -> bool:
        return self.kind in ("transient", "rate_limit")


def _format_reset(reset_at: float | None) -> str:
    if reset_at is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(reset_at))


def _reset_time(headers: Any) -> float | None:
    """When the limit lifts: ``Retry-After`` (seconds) or ``X-RateLimit-Reset`` (epoch)."""
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return time.time() + float(retry_after)
        except ValueError:
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return float(reset)
        except ValueError:
            pass
    return None


def _is_rate_limit(status: int, headers: Any, message: str) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    if headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After"):
        return True
    lowered = message.lower()
    return "rate limit" in lowered or "abuse" in lowered


class GitHubClient:
    def __init__(self, secrets: Secrets, api_url: str, timeout: float = 30.0):
        self.secrets = secrets
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Send one request. Returns ``(status, parsed JSON)`` for answers below 400 and for
        400-level answers that are not access, not-found or rate-limit errors (so callers can
        react to, for example, a 422)."""
        req = urllib.request.Request(
            self.api_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secrets.github_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "pi-factory",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            return self._http_error(exc, method, path)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GitHubAPIError(f"GitHub request failed: {exc}", "transient") from exc

    def _http_error(self, exc: urllib.error.HTTPError, method: str, path: str) -> tuple[int, Any]:
        text = exc.read().decode("utf-8", "replace")
        try:
            payload: Any = json.loads(text)
        except ValueError:
            payload = {"message": text[:300]}
        message = str((payload or {}).get("message", "")) if isinstance(payload, dict) else ""
        where = f"{method} {path.split('?')[0]}"
        if _is_rate_limit(exc.code, exc.headers, message):
            reset_at = _reset_time(exc.headers)
            logs.event(
                "github_rate_limited",
                status=exc.code,
                resets_at=_format_reset(reset_at),
                request=where,
            )
            raise GitHubAPIError(
                f"GitHub rate limit reached (HTTP {exc.code}) on {where}; "
                f"resets at {_format_reset(reset_at)}",
                "rate_limit",
                exc.code,
                reset_at,
            ) from exc
        if exc.code >= 500:
            raise GitHubAPIError(
                f"GitHub HTTP {exc.code} on {where}", "transient", exc.code
            ) from exc
        if exc.code in (401, 403):
            raise GitHubAPIError(
                f"GitHub HTTP {exc.code} on {where}: {message[:200]}", "access", exc.code
            ) from exc
        if exc.code == 404:
            raise GitHubAPIError(
                f"GitHub HTTP 404 on {where}: {message[:200]}", "not_found", 404
            ) from exc
        return exc.code, payload

    def ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Like :meth:`request`, but any status of 300 or more is an error."""
        status, payload = self.request(method, path, body)
        if status >= 300:
            message = payload.get("message") if isinstance(payload, dict) else payload
            raise GitHubAPIError(
                f"GitHub HTTP {status} on {method} {path.split('?')[0]}: {str(message)[:300]}",
                "other",
                status,
            )
        return payload

    def paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        """GET every page of a list endpoint (``per_page=100``) and concatenate the items."""
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            query = urllib.parse.urlencode({**(params or {}), "per_page": PER_PAGE, "page": page})
            chunk = self.ok("GET", f"{path}?{query}") or []
            items.extend(chunk)
            if len(chunk) < PER_PAGE:
                break
        return items
