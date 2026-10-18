# Quota projection review fix, 2026-10-06

Base: `9f7efacb5a5987c77f58b2c156060daa01819ee7` (PR #145).

## Changes

- `subfleet/store_schema.sql:53`: add the partial expression index
  `readings_weekly_history_parsed` on `julianday(observed_at)` for weekly provider
  and stale-provider readings. Writable Store opens install it on existing
  databases as well as fresh ones; the history query and its filters are unchanged.
- `subfleet/store.py:47`: document that both UNION branches search time bounds.
- `tests/unit/test_render_quota_projection.py:167`: reject every `SCAN` and
  `TEMP B-TREE`, and require both branches to search indexed lower/upper time
  bounds. Cover mixed canonical, offset, fractional, malformed, old, future,
  non-provider and non-weekly readings, including reopening an existing database.
- `tests/unit/test_quota_projection.py:5` and `tests/unit/test_status_json.py:445`:
  preserve all seven projection properties and both status JSON properties, their
  generators, assertions and example counts. Disable Hypothesis deadlines and
  suppress `HealthCheck.too_slow` for the loaded host.

## Before/after measurement

Same on-disk Store, SQLite 3.53.4, 100,000 canonical weekly provider readings
across 25 lanes: 4,000 readings per lane, ten-minute intervals ending at
`2026-10-05T21:12:00Z`. The inclusive 24-hour query returns 3,625 samples.
Measure exact VM steps with `sqlite3.Connection.set_progress_handler(callback, 1)`;
CPU times are medians of seven additional executions without that callback,
including row materialization and sorting by reading ID for comparison.

| Full query | Exact VM steps | Median CPU |
| --- | ---: | ---: |
| Before | 568,943 | 33.359 ms |
| After | 90,737 | 8.438 ms |

VM work falls 84.1%. All 3,625 complete rows are identical before and after.

Before:

```text
COMPOUND QUERY
LEFT-MOST SUBQUERY
SEARCH readings USING INDEX readings_weekly_history (observed_at>? AND observed_at<?)
UNION ALL
SCAN readings USING INDEX readings_weekly_history
```

After:

```text
COMPOUND QUERY
LEFT-MOST SUBQUERY
SEARCH readings USING INDEX readings_weekly_history (observed_at>? AND observed_at<?)
UNION ALL
SEARCH readings USING INDEX readings_weekly_history_parsed (<expr>>? AND <expr><?)
```

## Validation and delivery

The strengthened plan test fails against the original schema specifically on
the fallback scan. The final targeted suite passes: **130 tests**, including
all nine Hypothesis properties, in 431.69 seconds on the loaded host. Command:

```sh
.venv/bin/python -m pytest -q tests/unit/test_quota_projection.py \
  tests/unit/test_render_quota_projection.py tests/unit/test_render.py \
  tests/unit/test_status_json.py tests/unit/test_store_snapshot_plans.py
```

Tests run in one pytest process at a time, without `-n`, using the frozen
development dependencies in the workspace's `.venv`.

**6/6 mutations caught**, each by an assertion failure in its selected test:

| Mutation | Test |
| --- | --- |
| Reverse slope sign | `test_three_days_to_reset_first_to_last_trend` |
| Allow negative rate | `test_negative_rate_is_zero` |
| Remove reset-window filter | `test_other_reset_window_does_not_supply_a_rate` |
| Remove clamp | `test_clamp_when_trend_would_exhaust_quota` |
| Remove parsed-time index | `test_store_snapshot_retains_trend_and_history_query_uses_index` (`SCAN`) |
| Add temporary ORDER BY sort | Same plan test (`TEMP B-TREE`) |

The mutation harness verifies byte-for-byte restoration of all three source
files. A final restored-source run passes **7 tests** in 31.25 seconds: the four
projection examples, the strict plan test, and both fresh/existing-store mixed
timestamp cases. `git diff --check` passes; projection math and
`subfleet/default_policy.json` remain unchanged.

Shared git metadata denies creation of `index.lock`; commits use `.git-local`
on branch `quota-projection-r1`. Delivery bundle:
`docs/reports/2026-10-06-quota-projection-r1.bundle`, prerequisite `9f7efacb`.
