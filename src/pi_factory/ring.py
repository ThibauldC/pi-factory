"""A small thread-safe ring buffer of output lines for the dashboard's live tails."""

from __future__ import annotations

import threading
from collections import deque

MAX_LINE = 500


class RingBuffer:
    def __init__(self, maxlen: int = 200):
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._partial = ""
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        """Add one complete line."""
        with self._lock:
            self._lines.append(line[:MAX_LINE])

    def write(self, text: str) -> None:
        """Add streamed text; complete lines are stored, the remainder is held back."""
        with self._lock:
            self._partial += text
            *done, self._partial = self._partial.split("\n")
            for line in done:
                self._lines.append(line[:MAX_LINE])
            self._partial = self._partial[-MAX_LINE:]

    def flush(self) -> None:
        with self._lock:
            if self._partial:
                self._lines.append(self._partial[:MAX_LINE])
                self._partial = ""

    def snapshot(self) -> list[str]:
        with self._lock:
            lines = list(self._lines)
            if self._partial:
                lines.append(self._partial)
            return lines
