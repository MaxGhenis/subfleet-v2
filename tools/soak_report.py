#!/usr/bin/env python3
"""Daily soak record (docs/decisions/2026-09-05-cutover-prerequisites.md, "Soak evidence").

Reads the v2 store read-only and writes docs/soak/<date>.md: attempts by terminal state,
every quarantined or lost attempt with its evidence, duplicate acceptances, actions left
unknown or stuck, the timer and lifecycle events that fired, and canary progress. Exit 1
when the observation is incomplete or not clean. A clean daily observation alone does
not certify the seven-day release gate. By default report yesterday, a complete UTC day.

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
    db = sqlite3.connect((root / 'state.sqlite3').resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    return db


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


#: Rows the importer wrote from v1's ledger carry this in `attempts.evidence_json`
#: (`importer.import_runs`); they are history, not soak evidence.
IMPORTED = "CASE WHEN json_valid(evidence_json) THEN coalesce(json_extract(evidence_json, '$.imported'), 0) ELSE 0 END"


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
    incomplete = []
    try:
        instant = dt.datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
        if instant is None or instant.utcoffset() != dt.timedelta(0):
            raise ValueError("missing UTC start")
        since = instant.strftime("%Y-%m-%dT%H:%M:%SZ")
        if since >= end:
            incomplete.append("reported day is before the soak window")
    except (ValueError, AttributeError):
        incomplete.append("no valid UTC soak start recorded")
        since = "0000-00-00T00:00:00Z"
    scope = f"reserved_at>=? AND reserved_at<? AND ({IMPORTED}) != 1"
    scope_params = (since, end)
    by_state_all = rows(db, f"SELECT state, count(*) n FROM attempts WHERE {scope} GROUP BY state ORDER BY state", scope_params)
    by_state_day = rows(db, f"SELECT state, count(*) n FROM attempts WHERE {scope} AND finished_at>=? AND finished_at<? GROUP BY state ORDER BY state", (*scope_params, start, end))
    # A loss remains a blocker after midnight, including a quarantine whose finished_at
    # is absent. An operator must resolve it explicitly, not wait for the daily filter.
    bad_all = rows(db, f"SELECT attempt_id, state, outcome_detail, quarantine_reason, finished_at FROM attempts WHERE {scope} AND state IN ('quarantined','lost') AND (finished_at IS NULL OR finished_at<?) ORDER BY finished_at", (*scope_params, end))
    bad = [b for b in bad_all if b["finished_at"] and b["finished_at"] >= start]
    dup = rows(db, f"SELECT job_id, count(*) n FROM attempts WHERE {scope} AND state='succeeded' GROUP BY job_id HAVING n>1", scope_params)
    historical = rows(db, f"SELECT state, count(*) n FROM attempts WHERE ({IMPORTED}) = 1 GROUP BY state ORDER BY state")
    unknown_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state='unknown'")
    stuck_actions = rows(db, "SELECT action_id, kind, subject, state, updated_at FROM actions WHERE state IN ('pending','executing') AND updated_at<?", ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    # The importer's events are `import.cursor`, `import.run`, and `<table>.imported`, all stamped at import time.
    events = rows(db, "SELECT kind, count(*) n FROM events WHERE ts>=? AND ts<? AND ts>=? AND kind NOT LIKE 'import.%' AND kind NOT LIKE '%.imported' GROUP BY kind ORDER BY kind", (start, end, since))
    timer_kinds = [e for e in events if e["kind"].split(".")[0] in ("probe", "closure", "keepalive", "reset", "timer", "lane", "identity", "service", "notice")]
    timer_runs = rows(db, "SELECT data_json FROM events WHERE kind='timer.run' AND ts>=? AND ts<? AND ts>=?", (start, end, since))
    if not any(json.loads(e["data_json"]).get("timer") == "probe"
               and not json.loads(e["data_json"]).get("last_error_type") for e in timer_runs):
        incomplete.append("no successful scheduled probe cycle recorded for this day")
    ownership = rows(db, "SELECT lane_id, identity_status FROM lanes WHERE owner='v2' AND identity_status='mismatch'")
    canary = rows(db, "SELECT state, count(*) n FROM jobs WHERE job_id LIKE ? GROUP BY state ORDER BY state", (canary_like,))
    canary_total = sum(c["n"] for c in canary)
    clean = not incomplete and not bad_all and not dup and not unknown_actions and not stuck_actions and not ownership
    db.close()

    out = [f"# Soak record {date}", "", f"State root `{root}`; generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}.",
           f"Soak window starts {since}; attempts imported from v1's ledger are listed under history and never count." if since != "0000-00-00T00:00:00Z"
           else "WARNING: no soak start recorded (`<state root>/soak.json` or the canary baseline); every non-imported attempt counts.", ""]
    out += ["## Verdict", "", ("CLEAN observation: no unresolved loss, quarantine, duplicate acceptance, identity mismatch, or unknown/stuck action." if clean else "STOP: the observation is incomplete or a soak blocker remains."),
            "This daily report does not certify seven elapsed clean days, uninterrupted ownership, or the shadow decision review.", ""]
    out += ["## Missing evidence", ""] + ([f"- {item}" for item in incomplete] or ["- none"])
    out += ["", "## Attempts", "", "| state | today | since soak start |", "|---|---|---|"]
    day = {r["state"]: r["n"] for r in by_state_day}
    for r in by_state_all:
        out.append(f"| {r['state']} | {day.get(r['state'], 0)} | {r['n']} |")
    out += ["", "## Quarantined or lost today", ""]
    out += [f"- `{b['attempt_id']}` {b['state']} at {b['finished_at']}: {b['quarantine_reason'] or b['outcome_detail']}" for b in bad] or ["- none"]
    out += ["", "## Unresolved quarantined or lost since soak start", ""]
    out += [f"- `{b['attempt_id']}` {b['state']} at {b['finished_at']}: {b['quarantine_reason'] or b['outcome_detail']}" for b in bad_all] or ["- none"]
    out += ["", "## Ownership identity mismatches", ""] + ([f"- `{lane['lane_id']}`: {lane['identity_status']}" for lane in ownership] or ["- none"])
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
    ap.add_argument("--date", default=(dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat(),
                    help="UTC day to report (default: yesterday, so the whole day is covered)")
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
