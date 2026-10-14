"""Execute an unchanged held-lock runner with an in-process ps fake.

Installed temporarily as python3 for diagnostic steps only. No interpreter
or shell starts per successful inspection. The hanging mode delegates to
the shell fake and retains the real production two-second timeout. Flock,
PID existence, recorded identity and monotonic time are never replaced.
"""
import os
import subprocess
import sys
import time

assert sys.argv[1] == "-c"
source = sys.argv[2]
assert "def status(entry):" in source
sys.argv = ["-c", *sys.argv[3:]]
# Keep fake age and its subsequent identity comparison at one wall-clock
# instant even if the host deschedules this process between those operations.
now = float(os.environ.get("FANOUT_FAKE_INSPECTION_NOW", time.time()))
time.time = lambda: now

real_run = subprocess.run


def inspect(args, **kwargs):
    if args[0] != "ps":
        return real_run(args, **kwargs)
    assert len(args) == 5 and args[:4] == ["ps", "-o", "etime=", "-p"] and args[4].isdigit(), args
    assert kwargs == {"capture_output": True, "text": True, "timeout": 2}, kwargs
    if "FAKE_PS_ETIME" in os.environ:
        value, rc = os.environ["FAKE_PS_ETIME"] + "\n", 0
    else:
        mode = os.environ.get("FAKE_PS", "")
        if mode == "hang":
            return real_run(args, **kwargs)
        if mode == "nonzero":
            value, rc = "00:00\n", 1
        elif mode == "empty":
            value, rc = "", 1
        elif mode == "malformed":
            value, rc = "not an elapsed time\n", 0
        else:
            age = max(0, int(time.time() - float(os.environ["FAKE_PROCESS_STARTED"])))
            value = "%d-%02d:%02d:%02d\n" % (age // 86400, age // 3600 % 24, age // 60 % 60, age % 60)
            rc = 0
    return subprocess.CompletedProcess(args, rc, value, "")


subprocess.run = inspect
exec(compile(source, "<rendered-lock-runner>", "exec"), {"__name__": "__main__"})
