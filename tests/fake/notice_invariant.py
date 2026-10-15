"""C-15.1 as a standing check over a fake daemon's store.

Every notice the daemon writes starts `<job id>: <state>; rc=<rc>;
deliverable=<path>; out=<-o path>`, and that state and rc are the job row's,
the ones `wait`, `runs` and `runs show` report (incident: 2026-09-24, cancelled
jobs were announced `ok; rc=0` with an `-o` path nothing had written).
`Harness.close`, the in-process `state_daemon` fixture and `E2E.close` run this
after every test, so each fake-daemon and end-to-end test is also a
consistency check. The parse is
independent of `subfleet.render`, so the check does not share a bug with the
code it checks.

Skipped: a notice with no job (the v1 outbox rows the importer carries), a
notice whose job row is gone, and a first line that is not a v2 header (an
imported v1 notice keeps v1's own text).
"""

from __future__ import annotations

import re
from typing import Any, Callable

HEADER = re.compile(r"^(?P<job>\S+): (?P<state>[a-z-]+); rc=(?P<rc>[^;]*); "
                    r"deliverable=(?P<deliverable>.*?); out=(?P<out>.*)$")


def notice_mismatches(rows: Callable[..., list[dict[str, Any]]]) -> list[str]:
    """One line per notice whose header disagrees with its job row.

    `rows(sql, params)` reads the store (`Harness.rows`, read-only).
    """
    problems = []
    for notice in rows("SELECT notice_id,job_id,text FROM notices WHERE job_id IS NOT NULL "
                       "ORDER BY notice_id"):
        found = rows("SELECT job_id,state,rc,out_path,accepted_attempt_id FROM jobs WHERE job_id=?",
                     (notice["job_id"],))
        first = (notice["text"] or "").split("\n", 1)[0]
        header = HEADER.match(first)
        if not found or not header or header["job"] != notice["job_id"]:
            continue
        job = found[0]
        expected_rc = "-" if job["rc"] is None else str(job["rc"])
        accepted = job["state"] == "succeeded" and job["accepted_attempt_id"]
        wrong = []
        if header["state"] != job["state"]:
            wrong.append(f"state {header['state']!r} but the job is {job['state']!r}")
        if header["rc"] != expected_rc:
            wrong.append(f"rc={header['rc']} but the job's rc is {expected_rc}")
        if accepted and header["out"] != (job["out_path"] or "-"):
            wrong.append(f"out={header['out']} but the job's -o path is {job['out_path']!r}")
        if accepted and not header["deliverable"].endswith("/deliverable.md"):
            wrong.append(f"deliverable={header['deliverable']} for an accepted job")
        if not accepted and (header["deliverable"], header["out"]) != ("-", "-"):
            wrong.append(f"deliverable={header['deliverable']}; out={header['out']} "
                         f"for a job that accepted nothing")
        if wrong:
            problems.append(f"notice {notice['notice_id']} for {notice['job_id']}: "
                            + "; ".join(wrong) + f" (header: {first})")
    return problems
