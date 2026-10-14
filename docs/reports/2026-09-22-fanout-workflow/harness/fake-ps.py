#!/usr/bin/env python3
"""Controlled process inspection; real kill(pid, 0) still checks existence."""
import os
import sys
import time

mode = os.environ.get("FAKE_PS", "")
if "FAKE_PS_ETIME" in os.environ:
    print(os.environ["FAKE_PS_ETIME"])
    sys.exit(0)
if mode == "hang":
    os.execvp("sleep", ["sleep", "30"])
if mode == "nonzero":
    print("00:00")  # Even plausible stdout cannot prove a failed inspection.
    sys.exit(1)
if mode == "empty":
    sys.exit(1)
if mode == "malformed":
    print("not an elapsed time")
    sys.exit(0)
age = max(0, int(time.time() - float(os.environ["FAKE_PROCESS_STARTED"])))
print("%d-%02d:%02d:%02d" % (age // 86400, age // 3600 % 24, age // 60 % 60, age % 60))
