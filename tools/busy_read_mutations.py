"""Mutation check for the busy-lane usage read (C-18.3).

Each mutation removes one rule of `docs/decisions/2026-09-30-busy-lane-usage-read.md`
from `subfleet/timers.py`, and the busy-read tests must fail under it. Run from the
repository root, with a clean `subfleet/timers.py`:

    uv run python tools/busy_read_mutations.py

It rewrites `subfleet/timers.py` in place, one mutation at a time, and restores it
whatever happens. It exits 1 if a mutation survives or no longer applies. It is a
development check, not part of the suite: it runs the tests once per mutation.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

SOURCE = Path("subfleet/timers.py")
TESTS = ("tests/unit/test_timers_busy_read.py", "tests/fake/test_timers_busy_admission.py",
         "tests/unit/test_timers_probe.py")

CLOSURES = ("""            if self.store.one("SELECT 1 FROM closures WHERE lane_id=? AND reason IN ('auth-dead','operator-hold') """
            """AND released_at IS NULL AND until_at>?", (lane.lane_id, iso(self.now()))):
                return None
""")
ATTEMPTS = """self.store.one("SELECT 1 FROM attempts WHERE lane_id=? AND state IN ('reserved','starting','running','finalizing')", (lane.lane_id,))"""
LEASES = """self.store.one('SELECT 1 FROM leases WHERE lease_key LIKE ?', (f'lane:{lane.lane_id}:%',))"""
BUSY = f"""            if ({ATTEMPTS}
                    or {LEASES}):
                return BUSY
"""

#: `_publishable`'s question, with the lines after it: `_claim` asks the same one.
RECHECK = """        if not current or not current.enabled or current.owner != 'v2' or current.desktop:
            return None
        return not self._limits(lane.lane_id) - opened
"""

#: name: (the rule's code, what replaces it)
MUTATIONS = {
    "a busy lane is never read": ("                return BUSY\n", "                return None\n"),
    "a held lane is read when busy": (CLOSURES + BUSY, BUSY + CLOSURES),
    "a lane with only a conversation turn's attempt is idle": (
        ATTEMPTS, ATTEMPTS.replace("lane_id=? AND", "lane_id=? AND job_id NOT IN (SELECT job_id FROM jobs WHERE kind='turn') AND")),
    "a lane with only a conversation turn's lease is idle": (
        LEASES, LEASES.replace("LIKE ?'", "LIKE ? AND lease_key NOT LIKE ?'").replace(
            "(f'lane:{lane.lane_id}:%',)", "(f'lane:{lane.lane_id}:%', f'lane:{lane.lane_id}:slot:turn-%')")),
    "a busy Claude lane is read": (
        "            return self._busy_read(lane) if lane.provider == 'codex' else None\n",
        "            return self._busy_read(lane)\n"),
    "the read takes a lease": (
        "            opened = self._limits(lane.lane_id)\n",
        "            opened = self._limits(lane.lane_id)\n"
        "            self.store.acquire_lease(f'lane:{lane.lane_id}:slot:read', 'probe:timer:busy')\n"),
    "an error before the read fails the cycle": (
        "        opened = frozenset()\n        try:\n            opened = self._limits(lane.lane_id)\n",
        "        opened = self._limits(lane.lane_id)\n        try:\n"),
    "every status is published": (
        "        if not mismatch and probe.get('status') not in ('ok', 'limited'):\n            return None\n", ""),
    "a mismatch is withheld": (
        "        mismatch = bool(account) and account != lane.account_key\n", "        mismatch = False\n"),
    "a disabled lane is published": (RECHECK, RECHECK.replace("not current.enabled or ", "")),
    "a transferred lane is published": (RECHECK, RECHECK.replace("current.owner != 'v2' or ", "")),
    "a lane made the desktop's is published": (RECHECK, RECHECK.replace(" or current.desktop", "")),
    "no limit fences a release": (
        "        return not self._limits(lane.lane_id) - opened\n", "        return True\n"),
    "a limit an attempt reports again fences nothing": (
        "        return closures | limited\n", "        return closures\n"),
    "settlement ignores the fence": (
        "        if status != 'identity-mismatch' and releases:\n", "        if status != 'identity-mismatch':\n"),
    "a closure's release ignores the fence": (
        "            elif status == 'ok' and probe.get('limit_reached') is False and releases:",
        "            elif status == 'ok' and probe.get('limit_reached') is False:"),
    "the publication is not one transaction": (
        """            with self.store.transaction('timer.busy-read', lane_id=lane.lane_id):
                before = self.metadata.get(lane.lane_id)       # under the lock an attempt's verdict needs
                releases = self._publishable(lane, probe, opened)
                if releases is not None:
                    self._persist(lane, probe, releases=releases)
""", """            before = self.metadata.get(lane.lane_id)
            releases = self._publishable(lane, probe, opened)
            if releases is not None:
                self._persist(lane, probe, releases=releases)
"""),
    "a failed publication keeps the new verdict": (
        """            if before is None:
                self.metadata.pop(lane.lane_id, None)
            elif before is not missing:
                self.metadata[lane.lane_id] = before
            raise
""", "            raise\n"),
    "a failed publication drops the old verdict": (
        """            if before is None:
                self.metadata.pop(lane.lane_id, None)
            elif before is not missing:
                self.metadata[lane.lane_id] = before
""", "            self.metadata.pop(lane.lane_id, None)\n"),
    "only idle reads count toward offline": (
        "        codex = [p for lane, p, _ in results if lane.provider == 'codex']",
        "        codex = [p for lane, p, opened in results if lane.provider == 'codex' and opened is None]"),
}


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "--", str(SOURCE)]).returncode:
        print(f"{SOURCE} has uncommitted changes; commit or stash them first", file=sys.stderr)
        return 2
    original, survived = SOURCE.read_text(), []
    try:
        for name, (rule, replacement) in MUTATIONS.items():
            if original.count(rule) != 1:
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            SOURCE.write_text(original.replace(rule, replacement))
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                                 capture_output=True, text=True)
            failed = next((line[7:] for line in run.stdout.splitlines() if line.startswith("FAILED ")), "")
            print(f"{'killed  ' if run.returncode else 'SURVIVED'}  {name}" + (f"  ({failed.split(' - ')[0]})" if failed else ""))
            if not run.returncode:
                survived.append(name)
    finally:
        SOURCE.write_text(original)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
