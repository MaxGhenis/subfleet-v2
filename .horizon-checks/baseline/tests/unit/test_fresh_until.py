"""C-6.3: until `capacity.fresh_until`, every reading fresh at a decision's clock
is still fresh, so a lane that decision saw measured is still measured.

Property over generated readings (hypothesis is not a dependency here: a seeded
loop): for every instant before `fresh_until`, each reading fresh at `now` is
fresh; just after it, at least one of them is not; with none fresh, None.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from subfleet.capacity import fresh_provider, fresh_until

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
TTL = 120


def iso(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def reading(rng: random.Random) -> dict:
    observed = NOW - timedelta(seconds=rng.choice((0, 1, 30, 119, 120, 121, 300, -5)))
    resets = rng.choice((None, None, NOW + timedelta(seconds=rng.choice((1, 10, 60, 3600))),
                         NOW - timedelta(seconds=1)))
    return {"lane_id": f"codex-{rng.randrange(3)}", "scope": "account", "window": "seven_day",
            "utilization": rng.choice((.1, .5, None, float("nan"), -1)),
            "label": rng.choice(("provider", "provider", "unknown", "stale-provider")),
            "observed_at": iso(observed), "resets_at": iso(resets) if resets else None}


def test_fresh_until_is_when_the_first_fresh_reading_may_go_stale():
    for case in range(500):
        rng = random.Random(case)
        rows = [reading(rng) for _ in range(rng.randrange(0, 8))]
        fresh = [row for row in rows if fresh_provider(row, now=NOW, reading_ttl_s=TTL)]
        until = fresh_until(rows, now=iso(NOW), reading_ttl_s=TTL)
        if not fresh:
            assert until is None, (case, rows)
            continue
        assert until is not None and until >= NOW, (case, rows, until)
        for step in (0, .25, .5, .75, .999):
            instant = NOW + (until - NOW) * step
            assert all(fresh_provider(row, now=instant, reading_ttl_s=TTL) for row in fresh), (case, step)
        later = until + timedelta(seconds=1)
        assert not all(fresh_provider(row, now=later, reading_ttl_s=TTL) for row in fresh), (case, rows)
