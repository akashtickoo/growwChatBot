"""A small sliding-window rate limiter for the Streamlit app (architecture §10).

Streamlit has no per-request middleware, so the app checks two limiters before each question:
one per browser session (`RATE_LIMIT`) and one for the whole process (`GLOBAL_RATE_LIMIT`), which
also protects the Groq quota when many sessions are open. The FastAPI app uses slowapi instead.
"""

import re
import threading
import time
from collections import deque
from collections.abc import Callable

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    """'20/minute' → (20, 60). Also accepts '5 per second', '100/hour', '1000/day'."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:/|per)\s*(second|minute|hour|day)s?\s*", rate.lower())
    if not match:
        raise ValueError(f"bad rate limit {rate!r}; expected e.g. '20/minute'")
    return int(match.group(1)), _PERIODS[match.group(2)]


class SlidingWindowLimiter:
    def __init__(self, rate: str, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window = parse_rate(rate)
        self.clock = clock
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> float:
        """0 if the call may go ahead (and is counted); otherwise seconds until a slot frees."""
        with self._lock:
            now = self.clock()
            while self._hits and now - self._hits[0] >= self.window:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return self.window - (now - self._hits[0])
            self._hits.append(now)
            return 0.0
