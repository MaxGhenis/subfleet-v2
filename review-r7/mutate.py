"""Foreground mutation checks for the round-seven review; each restores its source."""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "subfleet/conversations"
STEP = "self._moot_blocks, self._replay_final_wakes,"
TICK_REPLAY = ("        try:\n            self.service._replay_final_wakes()\n        except Exception as exc:\n"
               "            self.service.log.warning(\"final wake replay deferred: %s: %s\", type(exc).__name__, exc)\n")
FENCE = ('    if tx.execute("SELECT 1 FROM final_wake_intents i JOIN messages m USING(message_id) "\n'
         '                  "WHERE m.conversation_id=? LIMIT 1", (cid,)).fetchone():\n        return False\n')
MUTATIONS = {
    # Round five's ordering: no replay before evaluation and no intent fence.
    "r5-order": [("service.py", STEP, "self._moot_blocks,"), ("wakes.py", TICK_REPLAY, ""), ("wakes.py", FENCE, "")],
    "fence-only-removed": [("wakes.py", FENCE, "")],
    "replay-before-evaluation-removed": [("service.py", STEP, "self._moot_blocks,"), ("wakes.py", TICK_REPLAY, "")],
    "replay-newest-first": [("wakes.py", "ORDER BY m.conversation_id,m.seq", "ORDER BY m.conversation_id,m.seq DESC")],
    "never-acknowledge": [("wakes.py", 'tx.execute("DELETE FROM final_wake_intents WHERE message_id=?", (mid,))',
                           "pass")],
}


def main():
    name, *tests = sys.argv[1:]
    originals = {}
    try:
        for filename, before, after in MUTATIONS[name]:
            path = SRC / filename
            originals.setdefault(path, path.read_text())
            text = path.read_text()
            assert text.count(before) == 1, (name, filename, "anchor must be unique")
            path.write_text(text.replace(before, after))
        for path in originals:
            for cached in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
                cached.unlink()
        env = {**os.environ, "TMPDIR": str(REPO / ".review-tmp") + "/",
               "GIT_CEILING_DIRECTORIES": str(REPO / ".review-tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        log = REPO / "review-r7/logs" / f"mutation-{name}.log"
        with log.open("w") as output:
            child = subprocess.Popen([str(REPO / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                      "--tb=line", *tests], cwd=REPO, env=env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = child.wait(timeout=400)
            except BaseException:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise
        summary = log.read_text().strip().splitlines()[-1]
        failed = [line.split(" - ")[0] for line in log.read_text().splitlines() if line.startswith("FAILED")]
        killed = rc == 1 and bool(re.search(r"\b[1-9]\d* failed\b", summary)) and "error" not in summary
        print(f"{name}: rc={rc} killed={killed} summary={summary}")
        for line in failed:
            print("  " + line)
    finally:
        for path, text in originals.items():
            path.write_text(text)
            for cached in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
                cached.unlink()


if __name__ == "__main__":
    main()
