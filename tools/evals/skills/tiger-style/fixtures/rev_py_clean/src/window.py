"""Sliding-window counters for request rates."""

from collections import deque

WINDOW_MS_MAX = 3_600_000
EVENTS_MAX = 10_000
SNAPSHOT_BYTES_MAX = 65_536


class WindowFull(Exception):
    """The window already holds EVENTS_MAX live events; the caller decides what to drop."""


class Window:
    def __init__(self, window_ms: int) -> None:
        if not 0 < window_ms <= WINDOW_MS_MAX:
            raise ValueError(f"window_ms must be in 1..{WINDOW_MS_MAX}, got {window_ms}")
        self.window_ms = window_ms
        self.stamps_ms: deque[int] = deque()
        assert self.window_ms > 0, "the span is positive after the check"
        assert not self.stamps_ms, "a new window starts empty"

    def add(self, now_ms: int) -> int:
        """Record one event at now_ms and return how many events are in the window."""
        if self.stamps_ms and now_ms < self.stamps_ms[-1]:
            raise ValueError(f"now_ms {now_ms} is before the last stamp {self.stamps_ms[-1]}")
        live = self.count(now_ms)
        if live >= EVENTS_MAX:
            raise WindowFull(f"{live} live events, limit is {EVENTS_MAX}")
        self.stamps_ms.append(now_ms)
        assert len(self.stamps_ms) <= EVENTS_MAX, "the deque stays bounded"
        assert self.stamps_ms[-1] == now_ms, "the new stamp is last"
        return live + 1

    def count(self, now_ms: int) -> int:
        """Expire stamps older than the window, then return the live count."""
        assert len(self.stamps_ms) <= EVENTS_MAX, "the deque stays bounded"
        assert self.window_ms > 0, "the span is positive"
        for _ in range(EVENTS_MAX):
            if not self.stamps_ms:
                break
            if self.stamps_ms[0] > now_ms - self.window_ms:
                break
            self.stamps_ms.popleft()
        return len(self.stamps_ms)


def rate_per_s(window: Window, now_ms: int) -> float:
    assert window.window_ms > 0, "the window has a span"
    assert len(window.stamps_ms) <= EVENTS_MAX, "the deque stays bounded"
    return window.count(now_ms) * 1000 / window.window_ms


def snapshot(window: Window, limit_bytes: int = SNAPSHOT_BYTES_MAX) -> bytes:
    """Serialize the newest whole stamps that fit in limit_bytes, oldest first."""
    if not 0 < limit_bytes <= SNAPSHOT_BYTES_MAX:
        raise ValueError(f"limit_bytes must be in 1..{SNAPSHOT_BYTES_MAX}, got {limit_bytes}")
    assert len(window.stamps_ms) <= EVENTS_MAX, "the deque stays bounded"
    kept: list[bytes] = []
    used_bytes = 0
    for stamp in reversed(window.stamps_ms):  # bounded by EVENTS_MAX
        item = str(stamp).encode("ascii")
        cost_bytes = len(item) + (1 if kept else 0)
        if used_bytes + cost_bytes > limit_bytes:
            break
        kept.append(item)
        used_bytes += cost_bytes
    data = b",".join(reversed(kept))
    assert len(data) <= limit_bytes, "the snapshot fits"
    return data
