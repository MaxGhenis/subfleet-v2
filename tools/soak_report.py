#!/usr/bin/env python3
"""Daily soak record (docs/decisions/2026-09-05-cutover-prerequisites.md, "Soak evidence").

Reads the v2 store read-only and writes docs/soak/<date>.md: attempts by terminal state,
every quarantined or lost attempt with its evidence, duplicate acceptances, actions left
unknown or stuck, the timer and lifecycle events that fired, and canary progress. Exit 1
when the day is not clean, so a launchd agent's log shows the stop.

    uv run python tools/soak_report.py [--state-root ~/.subfleet] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def connect(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def report(root: Path, date: str, canary_like: str) -> tuple[str, bool]:
    db = connect(root)
    start, end = f"{date}T00:00:00Z", (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat() + "T00:00:00Z"
    by_state_all = rows(db, "SELECT state, count(*) n FROM attempts GROUP BY state ORDER BY state")
    by_state_day = rows(db, "SELECT state, count(*) n FROM attempts WHERE finished_at>=? AND finished_at<? GROUP BY state ORDER BY state", (start, end))
    bad = rows(db, "SELECT attempt_id, state, outcome_detail, quarantine_reason, finished_at FROM attempts WHERE state IN ('quarantined','lost') AND finished_at>=? AND finished_at<? ORDER BY finished_at", (start, end))
    dup = rows(db, "SELECT job_id, count(*) n FROM attempts WHERE state='succeeded' GROUP BY job_id HAVING n>1")
    unknown_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state='unknown'")
    stuck_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state IN ('pending','executing') AND updated_at<?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    events = rows(db, "SELECT kind, count(*) n FROM events WHERE ts>=? AND ts<? GROUP BY kind ORDER BY kind", (start, end))
    timer_kinds = [e for e in events if e["kind"].split(".")[0] in ("probe", "closure", "keepalive", "reset", "timer", "lane", "identity", "service", "notice")]
    canary = rows(db, "SELECT state, count(*) n FROM jobs WHERE job_id LIKE ? GROUP BY state ORDER BY state", (canary_like,))
    canary_total = sum(c["n"] for c in canary)
    clean = not bad and not dup and not unknown_actions and not stuck_actions

    out = [f"# Soak record {date}", "", f"State root `{root}`; generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}.", ""]
    out += ["## Verdict", "", ("CLEAN: no quarantined or lost attempt, no duplicate acceptance, no unknown or stuck action." if clean else "STOP: the soak clock pauses until every item below is explained."), ""]
    out += ["## Attempts", "", "| state | today | all time |", "|---|---|---|"]
    day = {r["state"]: r["n"] for r in by_state_day}
    for r in by_state_all:
        out.append(f"| {r['state']} | {day.get(r['state'], 0)} | {r['n']} |")
    out += ["", "## Quarantined or lost today", ""]
    out += [f"- `{b['attempt_id']}` {b['state']} at {b['finished_at']}: {b['quarantine_reason'] or b['outcome_detail']}" for b in bad] or ["- none"]
    out += ["", "## Duplicate acceptances (must be empty)", ""] + ([f"- `{d['job_id']}`: {d['n']} succeeded attempts" for d in dup] or ["- none"])
    out += ["", "## Actions unknown or stuck over an hour (must be empty)", ""]
    out += [f"- `{a['action_id']}` {a['kind']} {a['subject']} {a['state']} since {a['updated_at']}" for a in unknown_actions + stuck_actions] or ["- none"]
    out += ["", "## Events today", "", "| kind | count |", "|---|---|"] + [f"| {e['kind']} | {e['n']} |" for e in events]
    out += ["", f"Timer and lane events today: {sum(e['n'] for e in timer_kinds)} across {len(timer_kinds)} kinds." , ""]
    out += ["## Canary jobs", "", f"{canary_total} jobs matching `{canary_like}`." ] + [f"- {c['state']}: {c['n']}" for c in canary]
    return "\n".join(out) + "\n", clean


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    ap.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    ap.add_argument("--out-dir", default=str(REPO / "docs" / "soak"))
    ap.add_argument("--canary-like", default="%-canary-%", help="SQL LIKE pattern for canary job ids")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = ap.parse_args()
    text, clean = report(Path(args.state_root).expanduser(), args.date, args.canary_like)
    if args.stdout:
        sys.stdout.write(text)
    else:
        target = Path(args.out_dir) / f"{args.date}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(target)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
