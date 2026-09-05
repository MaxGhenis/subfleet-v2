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


#: Rows the importer wrote from v1's ledger carry this in `attempts.evidence_json`
#: (`importer.import_runs`); they are history, not soak evidence.
IMPORTED = "%\"imported\": true%"


def soak_since(root: Path, explicit: str | None) -> str | None:
    """The soak window's start: `--since`, else `<state root>/soak.json`, else the canary baseline."""
    if explicit:
        return explicit
    for path in (root / "soak.json", Path("~/subfleet-v2-canary/baseline.json").expanduser()):
        try:
            value = json.loads(path.read_text()).get("since")
        except (OSError, ValueError):
            continue
        if value:
            return str(value)
    return None


def report(root: Path, date: str, canary_like: str, since: str | None = None) -> tuple[str, bool]:
    db = connect(root)
    start, end = f"{date}T00:00:00Z", (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat() + "T00:00:00Z"
    since = since or "0000-00-00T00:00:00Z"
    scope = "reserved_at>=? AND (evidence_json IS NULL OR evidence_json NOT LIKE ?)"
    by_state_all = rows(db, f"SELECT state, count(*) n FROM attempts WHERE {scope} GROUP BY state ORDER BY state", (since, IMPORTED))
    by_state_day = rows(db, f"SELECT state, count(*) n FROM attempts WHERE {scope} AND finished_at>=? AND finished_at<? GROUP BY state ORDER BY state", (since, IMPORTED, start, end))
    bad = rows(db, f"SELECT attempt_id, state, outcome_detail, quarantine_reason, finished_at FROM attempts WHERE {scope} AND state IN ('quarantined','lost') AND finished_at>=? AND finished_at<? ORDER BY finished_at", (since, IMPORTED, start, end))
    dup = rows(db, f"SELECT job_id, count(*) n FROM attempts WHERE {scope} AND state='succeeded' GROUP BY job_id HAVING n>1", (since, IMPORTED))
    historical = rows(db, "SELECT state, count(*) n FROM attempts WHERE evidence_json LIKE ? GROUP BY state ORDER BY state", (IMPORTED,))
    unknown_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state='unknown'")
    stuck_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state IN ('pending','executing') AND updated_at<?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    events = rows(db, "SELECT kind, count(*) n FROM events WHERE ts>=? AND ts<? AND ts>=? AND kind NOT LIKE 'import.%' GROUP BY kind ORDER BY kind", (start, end, since))
    timer_kinds = [e for e in events if e["kind"].split(".")[0] in ("probe", "closure", "keepalive", "reset", "timer", "lane", "identity", "service", "notice")]
    canary = rows(db, "SELECT state, count(*) n FROM jobs WHERE job_id LIKE ? GROUP BY state ORDER BY state", (canary_like,))
    canary_total = sum(c["n"] for c in canary)
    clean = not bad and not dup and not unknown_actions and not stuck_actions

    out = [f"# Soak record {date}", "", f"State root `{root}`; generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}.",
           f"Soak window starts {since}; attempts imported from v1's ledger are listed under history and never count." if since != "0000-00-00T00:00:00Z"
           else "WARNING: no soak start recorded (`<state root>/soak.json` or the canary baseline); every non-imported attempt counts.", ""]
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
    out += ["", "## History imported from v1 (not soak evidence)", ""] + ([f"- {h['state']}: {h['n']}" for h in historical] or ["- none"])
    return "\n".join(out) + "\n", clean


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME", "~/.subfleet"))
    ap.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    ap.add_argument("--out-dir", default=str(REPO / "docs" / "soak"))
    ap.add_argument("--canary-like", default="%-canary-%", help="SQL LIKE pattern for canary job ids")
    ap.add_argument("--since", help="soak window start (ISO UTC); default from <state root>/soak.json or the canary baseline")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = ap.parse_args()
    root = Path(args.state_root).expanduser()
    text, clean = report(root, args.date, args.canary_like, soak_since(root, args.since))
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
