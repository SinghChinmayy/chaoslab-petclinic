"""User-facing signals, read live from the load generator (Locust)."""

import json
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.request import urlopen

from .settings import LOCUST_URL


@dataclass
class Snapshot:
    at: float                              # time.monotonic()
    counts: dict[str, tuple[int, int]]     # user action -> cumulative (requests, failures)
    p95_ms: float | None                   # Locust's rolling p95 over roughly 10 s
    users: int

    def totals(self) -> tuple[int, int]:
        return (sum(r for r, _ in self.counts.values()),
                sum(f for _, f in self.counts.values()))


@dataclass
class Sample:
    elapsed: float
    phase: str
    rps: float
    error_rate: float | None
    p95_ms: float | None
    stalled: bool                          # users active, yet nothing completed in the window
    action_errors: dict[str, float | None] = field(default_factory=dict)
    semantic_correctness: str | None = None


def error_rates(a: Snapshot, b: Snapshot) -> tuple[float | None, dict[str, float | None]]:
    """Share of failed requests between two snapshots, overall and per user action."""
    per_action = {}
    for name, (req, fail) in b.counts.items():
        req0, fail0 = a.counts.get(name, (0, 0))
        per_action[name] = (fail - fail0) / (req - req0) if req > req0 else None
    (req1, fail1), (req0, fail0) = b.totals(), a.totals()
    overall = (fail1 - fail0) / (req1 - req0) if req1 > req0 else None
    return overall, per_action


class LocustProbe:
    def __init__(self, url: str = LOCUST_URL, window_s: float = 5.0):
        self.url = url
        self.window_s = window_s
        self.history: deque[Snapshot] = deque()

    def snapshot(self) -> Snapshot:
        with urlopen(f"{self.url}/stats/requests", timeout=3) as resp:
            data = json.load(resp)
        counts = {s["name"]: (s["num_requests"], s["num_failures"])
                  for s in data["stats"] if s["name"] != "Aggregated"}
        p95 = (data.get("current_response_time_percentiles") or {}).get("response_time_percentile_0.95")
        return Snapshot(time.monotonic(), counts, p95, data.get("user_count", 0))

    def sample(self, elapsed: float, phase: str) -> Sample:
        """Take a snapshot and summarise the last `window_s` seconds."""
        now = self.snapshot()
        self.history.append(now)
        while len(self.history) > 2 and now.at - self.history[1].at >= self.window_s:
            self.history.popleft()
        ref = self.history[0]
        span = now.at - ref.at
        completed = now.totals()[0] - ref.totals()[0]
        overall, per_action = error_rates(ref, now)
        return Sample(
            elapsed=round(elapsed, 1),
            phase=phase,
            rps=round(completed / span, 2) if span > 0 else 0.0,
            error_rate=overall,
            p95_ms=now.p95_ms,
            stalled=span >= self.window_s and completed == 0 and now.users > 0,
            action_errors=per_action,
        )
