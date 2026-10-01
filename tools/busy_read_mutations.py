"""Mutation check for the busy-lane usage read (C-18.3).

Each mutation removes one rule of `docs/decisions/2026-09-30-busy-lane-usage-read.md`
from the code, and the busy-read tests must fail under it. Run from the repository
root, with the mutated files clean:

    uv run python tools/busy_read_mutations.py

It rewrites `subfleet/timers.py` or `subfleet/store.py` in place, one mutation at a
time, and restores the file whatever happens. It exits 1 if a mutation survives or no
longer applies. It is a development check, not part of the suite: it runs the tests
once per mutation.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

TIMERS, STORE = Path("subfleet/timers.py"), Path("subfleet/store.py")
TESTS = ("tests/unit/test_timers_busy_read.py", "tests/fake/test_timers_busy_admission.py",
         "tests/unit/test_timers_probe.py")

HELD = ("""        return bool(self.store.one("SELECT 1 FROM closures WHERE lane_id=? AND reason IN ('auth-dead','operator-hold') """
        """AND released_at IS NULL AND until_at>?", (lane_id, iso(self.now()))))\n""")
UNREADABLE = "        if not current or not current.enabled or current.owner != 'v2' or current.desktop:\n            return True\n"
ATTEMPTS = """self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN ('reserved','starting','running','finalizing')", (lane.lane_id,))"""
LEASES = """self.store.one('SELECT 1 FROM leases WHERE lease_key LIKE ?', (f'lane:{lane.lane_id}:%',))"""
RECHECK = "        if self._never_read(lane.lane_id):\n            return None\n        return not self.store.one("
FENCE = ("""        return not self.store.one("SELECT 1 FROM events WHERE kind='closure.recorded' AND event_id>? AND lane_id=? LIMIT 1",
                                  (opened, lane.lane_id))\n""")
JUDGED = """                try:
                    releases = self._publishable(lane, probe, opened)
                    if releases is not None:
                        self._persist(lane, probe, releases=releases)
                except BaseException:
                    self._set_verdict(lane.lane_id, before)
                    raise
"""
RESTORE = """            with self._verdicts:
                if wrote is not missing and wrote is not before and self.metadata.get(lane.lane_id) is wrote:
                    self._set_verdict(lane.lane_id, before)
"""

#: name: (file, the rule's code, what replaces it)
MUTATIONS = {
    "a busy lane is never read": (TIMERS, "                return BUSY\n", "                return None\n"),
    "a held or auth-dead lane is read": (TIMERS, HELD, "        return False\n"),
    "a disabled lane is read and published": (TIMERS, UNREADABLE, UNREADABLE.replace("not current.enabled or ", "")),
    "a transferred lane is read and published": (TIMERS, UNREADABLE, UNREADABLE.replace("current.owner != 'v2' or ", "")),
    "the desktop's lane is read and published": (TIMERS, UNREADABLE, UNREADABLE.replace(" or current.desktop", "")),
    "a lane with only a conversation turn's attempt is idle": (
        TIMERS, ATTEMPTS, ATTEMPTS.replace("lane_id=? AND", "lane_id=? AND job_id NOT IN (SELECT job_id FROM jobs WHERE kind='turn') AND")),
    "a lane with only a conversation turn's lease is idle": (
        TIMERS, LEASES, LEASES.replace("LIKE ?'", "LIKE ? AND lease_key NOT LIKE ?'").replace(
            "(f'lane:{lane.lane_id}:%',)", "(f'lane:{lane.lane_id}:%', f'lane:{lane.lane_id}:slot:turn-%')")),
    "a busy Claude lane is read": (
        TIMERS, "            return self._busy_read(lane) if lane.provider == 'codex' else None\n",
        "            return self._busy_read(lane)\n"),
    "the read takes a lease": (
        TIMERS, "            opened = self._mark()\n",
        "            opened = self._mark()\n"
        "            self.store.acquire_lease(f'lane:{lane.lane_id}:slot:read', 'probe:timer:busy')\n"),
    "an error before the read fails the cycle": (
        TIMERS, "        opened = 0\n        try:\n            opened = self._mark()\n",
        "        opened = self._mark()\n        try:\n"),
    "an answer that is not a mapping fails the cycle": (
        TIMERS, "            probe = {**self._read_probe(adapter, lane, resolve_credential(lane.credential))}\n",
        "            probe = self._read_probe(adapter, lane, resolve_credential(lane.credential))\n"),
    "every status is published": (
        TIMERS, "        if not mismatch and probe.get('status') not in ('ok', 'limited'):\n            return None\n", ""),
    "a mismatch is withheld": (
        TIMERS, "        mismatch = bool(account) and account != lane.account_key\n", "        mismatch = False\n"),
    "a lane no longer read is published": (TIMERS, RECHECK, "        return not self.store.one("),
    "no later limit fences a release": (TIMERS, FENCE, "        return True\n"),
    "another lane's limit fences this one's release": (TIMERS, FENCE, FENCE.replace(" AND lane_id=?", "").replace(
        "(opened, lane.lane_id)", "(opened,)")),
    "any event of the lane fences a release": (TIMERS, FENCE, FENCE.replace("kind='closure.recorded' AND ", "")),
    "a limit reported again leaves no event": (
        STORE, """                else:
                    conn.execute("UPDATE closures SET until_at=until_at WHERE closure_id=?", (existing["closure_id"],))
""", ""),
    "settlement ignores the fence": (
        TIMERS, "        if status != 'identity-mismatch' and releases:\n", "        if status != 'identity-mismatch':\n"),
    "a closure's release ignores the fence": (
        TIMERS, "            elif status == 'ok' and probe.get('limit_reached') is False and releases:",
        "            elif status == 'ok' and probe.get('limit_reached') is False:"),
    "the publication is not one transaction": (
        TIMERS, """            with self.store.transaction('timer.busy-read', lane_id=lane.lane_id):
                before = self.metadata.get(lane.lane_id)       # under the lock an attempt's verdict needs
""" + JUDGED + "                wrote = self.metadata.get(lane.lane_id)\n",
        "            if True:\n                before = self.metadata.get(lane.lane_id)\n" + JUDGED
        + "                wrote = self.metadata.get(lane.lane_id)\n"),
    "the old verdict is read before the lock": (
        TIMERS, """        before = wrote = missing = object()
        try:
            with self.store.transaction('timer.busy-read', lane_id=lane.lane_id):
                before = self.metadata.get(lane.lane_id)       # under the lock an attempt's verdict needs
""", """        wrote = missing = object()
        before = self.metadata.get(lane.lane_id)
        try:
            with self.store.transaction('timer.busy-read', lane_id=lane.lane_id):
"""),
    "a failed publication keeps the new verdict": (
        TIMERS, "                    self._set_verdict(lane.lane_id, before)\n                    raise\n", "                    raise\n"),
    "a failed publication drops the old verdict": (
        TIMERS, "                    self._set_verdict(lane.lane_id, before)\n                    raise\n",
        "                    self._set_verdict(lane.lane_id, None)\n                    raise\n"),
    "a failed commit keeps the new verdict": (TIMERS, RESTORE, ""),
    "a failed commit puts the old verdict over an attempt's": (
        TIMERS, RESTORE, RESTORE.replace(" and self.metadata.get(lane.lane_id) is wrote", "")),
    "the check and the put-back are two steps": (TIMERS, RESTORE, RESTORE.replace("with self._verdicts:", "if True:")),
    "the old verdict is put back after the lock is released": (
        TIMERS, JUDGED + "                wrote = self.metadata.get(lane.lane_id)\n        except BaseException:\n",
        """                releases = self._publishable(lane, probe, opened)
                if releases is not None:
                    self._persist(lane, probe, releases=releases)
                wrote = self.metadata.get(lane.lane_id)
        except BaseException:
            if wrote is missing and before is not missing:
                self._set_verdict(lane.lane_id, before)
"""),
    "a fenced read is not named on the cycle's event": (
        TIMERS, "                elif not published:\n                    fenced.append(lane.lane_id)\n", ""),
    "only idle reads count toward offline": (
        TIMERS, "        codex = [p for lane, p, _ in results if lane.provider == 'codex']",
        "        codex = [p for lane, p, opened in results if lane.provider == 'codex' and opened is None]"),
}


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "--", str(TIMERS), str(STORE)]).returncode:
        print(f"{TIMERS} or {STORE} has uncommitted changes; commit or stash them first", file=sys.stderr)
        return 2
    originals, survived = {path: path.read_text() for path in (TIMERS, STORE)}, []
    try:
        for name, (path, rule, replacement) in MUTATIONS.items():
            if originals[path].count(rule) != 1:
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            path.write_text(originals[path].replace(rule, replacement))
            try:
                run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                                     capture_output=True, text=True)
            finally:
                path.write_text(originals[path])
            failed = next((line[7:] for line in run.stdout.splitlines() if line.startswith("FAILED ")), "")
            print(f"{'killed  ' if run.returncode else 'SURVIVED'}  {name}" + (f"  ({failed.split(' - ')[0]})" if failed else ""))
            if not run.returncode:
                survived.append(name)
    finally:
        for path, text in originals.items():
            path.write_text(text)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
