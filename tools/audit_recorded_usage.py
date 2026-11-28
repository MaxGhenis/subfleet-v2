"""Audit the daemon's recorded sensor results; never load or send credentials.

Usage: python tools/audit_recorded_usage.py /path/to/state.sqlite3 --log /path/to/daemon.log
The SQLite connection is read-only and query-only. Output contains counts and
timestamps only, never event payloads, account identities, or raw log lines.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time


def audit(path: Path, log: Path | None = None) -> dict:
    started = time.monotonic()
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as db:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() - started > 55), 10_000)
        # A short read snapshot bounds the audit without copying the live WAL.
        db.execute('BEGIN')
        def counts(since, until):
            return [dict(status=status, count=count, retry_after_3600=retry or 0)
                    for status, count, retry in db.execute("""
                SELECT json_extract(data_json,'$.probe_status'), count(*),
                       sum(json_extract(data_json,'$.retry_after_s')=3600)
                FROM events WHERE kind='timer.verdict' AND lane_id LIKE 'claude-%'
                  AND ts>=? AND ts<=? AND json_valid(data_json)
                  AND json_extract(data_json,'$.probe_status') IS NOT NULL
                  AND json_extract(data_json,'$.probed_at') IS NOT NULL
                GROUP BY 1 ORDER BY 1""", (since, until))]
        cutoff = db.execute('SELECT max(ts) FROM events').fetchone()[0]
        sensor = db.execute("SELECT count(*),count(DISTINCT observed_at),min(observed_at),max(observed_at) FROM readings WHERE source='oauth-usage'").fetchone()
        codex = db.execute("SELECT count(*),sum(window='five_hour') FROM readings WHERE lane_id LIKE 'codex-%'").fetchone()
        result = {'audited_at': datetime.now(timezone.utc).isoformat(), 'cutoff': cutoff,
                  'replay_claude_results': counts('2026-09-26T15:23:38Z', '2026-10-03T15:23:38Z'),
                  'since_cutover_claude_results': counts('2026-10-04T02:08:33Z', cutoff),
                  'oauth_usage': dict(rows=sensor[0], distinct_observations=sensor[1], first=sensor[2], last=sensor[3]),
                  'codex_readings': dict(rows=codex[0], five_hour=codex[1] or 0)}
    if log:
        needles = ('no-scope', 'HTTP 403', 'HTTP 429', 'Retry-After', 'rate-limited')
        hits = Counter()
        with log.open(errors='replace') as stream:
            for line in stream:
                hits.update(needle for needle in needles if needle in line)
        result['log_status_line_counts'] = {needle: hits[needle] for needle in needles}
        result['log_note'] = 'Log counts are corroboration only; structured timer.verdict rows determine sensor outcomes.'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('store', type=Path)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.store, args.log), indent=2))
