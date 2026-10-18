"""Replay recorded fake transport with a controlled recovery clock.

The generated helper still reads the actual staged manifest, creation labels,
prompt bytes, digests and log files. Only transport and elapsed time are fake.
No host scheduling delay can consume its recovery budget in these checks.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

helper, mode = sys.argv[1:3]
namespace = {}
source = Path(helper).read_text().split("\nverb = sys.argv[1]")[0]
exec(compile(source, helper, "exec"), namespace)
os.chdir(Path(helper).parent)
registry = json.loads(Path(os.environ["FAKE_STATE"], "registry.json").read_text())
files = namespace["F"]
clock = 0
calls = []
broken = None


def transport(command, **kwargs):
    global clock
    assert command[:2] == ["subfleet", "runs"], command
    assert "CLAUDE_CODE_SESSION_ID" not in kwargs["env"]
    calls.append(command)
    if command[2] == "show":
        if mode == "budget":
            assert kwargs["timeout"] == 15
            clock += 15
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        row = copy.deepcopy(next(row for row in registry.values() if row["job_id"] == command[3]))
        if broken:
            action, field = broken
            if action == "MISSING":
                row.pop(field, None)
            else:
                row[field] = "different"
        batch = row.pop("batch", None)
        result = {"job": row, "batch": batch}
    else:
        assert command[2] == "--request-id", command
        result = registry[command[3]]
        if mode == "between-lookups":
            clock += 60
    return subprocess.CompletedProcess(command, 0, json.dumps(result) + "\n", "")


namespace["subprocess"] = SimpleNamespace(run=transport, TimeoutExpired=subprocess.TimeoutExpired)
namespace["time"] = SimpleNamespace(monotonic=lambda: clock)


def report():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        namespace["cmd_post_submit"](-1)
    return output.getvalue()


def clear_recovery():
    Path(files["recovered"]).unlink(missing_ok=True)


if mode in ("budget", "between-lookups"):
    clear_recovery()
    output = report()
    print(output, end="")
    if mode == "budget":
        assert len(calls) == 4, calls
        assert len(re.findall(r"^LOOKUP_UNKNOWN name=", output, re.M)) == 4, output
        assert len(re.findall(r"^REFUSED name=", output, re.M)) == 6, output
        for name in "ef":
            assert re.search(r"^REFUSED name=" + name + r" .*\(not looked up: recovery budget spent after 4 lookups; relaunch to recover\)$", output, re.M), output
        assert "RECOVERY_LOOKUPS=4\nRECOVERY_SKIPPED=2\n" in output, output
        assert "JOBS=0\nUNSETTLED=0\n" in output, output
        assert "RETRY_STEP1" not in output, output
    else:
        assert len(calls) == 1 and calls[0][2] == "--request-id", calls
        assert re.search(r"^OUTCOME_UNKNOWN name=a .*\(show not looked up: recovery budget spent after 1 lookups; relaunch to recover\)$", output, re.M), output
        for name in "bc":
            assert re.search(r"^OUTCOME_UNKNOWN name=" + name + r" .*\(not looked up: recovery budget spent after 1 lookups; relaunch to recover\)$", output, re.M), output
        assert "RECOVERY_LOOKUPS=1\nRECOVERY_SKIPPED=3\n" in output, output
        assert "JOBS=0\nUNSETTLED=3\n" in output, output
    # A later invocation has a fresh budget and exactly the same evidence.
    mode = "healthy"
    calls.clear()
    clock = 0
    recovered = report()
    expected = len(registry)
    assert f"JOBS={expected}\nUNSETTLED=0\n" in recovered, recovered
    assert Path(files["jobs"]).read_text().count("\n") == expected
    print(f"PASS {expected} jobs recovered with a fresh budget")
else:
    assert mode in ("MISSING", "MISMATCH"), mode
    fields = ("job_id", "request_id", "name", "workdir", "out_path", "task", "tier", "sandbox", "pinned_model", "prompt_path", "workdir_head", "policy_hash", "payload_digest")
    for field in fields:
        clear_recovery()
        calls.clear()
        broken = (mode, field)
        output = report()
        assert "MANIFEST_REFUSED" in output and "JOBS=0\nUNSETTLED=3\n" in output, (field, output)
        assert len(re.findall(r"^LOOKUP_UNKNOWN name=", output, re.M)) == 3, (field, output)
        assert not re.search(r"^SUBMITTED |^REFUSED |RETRY_STEP1", output, re.M), (field, output)
        assert Path(files["jobs"]).read_text() == ""
        assert len(calls) == 6, (field, calls)
        broken = None
        recovered = report()
        assert "JOBS=3\nUNSETTLED=0\n" in recovered, (field, recovered)
        print(f"PASS {mode.lower()} {field}: all three uncertain, all three recover on complete proof")
