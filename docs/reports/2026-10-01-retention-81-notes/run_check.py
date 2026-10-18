"""Run a foreground check, preserving output and reporting its time budget."""
import subprocess
import sys
import time
from pathlib import Path

log = Path(sys.argv[1])
timeout = int(sys.argv[2])
command = sys.argv[3:]
started = time.monotonic()
with log.open("w") as output:
    output.write("Command: " + " ".join(command) + "\n")
    output.flush()
    child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
    print(f"Foreground child PID {child.pid}: {' '.join(command)}", flush=True)
    try:
        result = child.wait(timeout=timeout) if timeout else child.wait()
    except subprocess.TimeoutExpired:
        # This is the exact child created above, never a command-line match.
        try:
            inspected = subprocess.run(["ps", "-p", str(child.pid), "-o", "args="],
                                       capture_output=True, text=True)
        except OSError as exc:
            inspected = subprocess.CompletedProcess([], 1, "", str(exc))
        output.write(f"\nCheck exceeded {timeout}s. Exact-child inspection: {inspected.stdout}{inspected.stderr}\n")
        # uv may own pytest descendants. Killing only uv can strand them;
        # remain attached until the entire command exits, or the operator
        # sends a terminal interrupt to this owned foreground group.
        print("Time budget exceeded; continuing the foreground wait.", flush=True)
        result = child.wait()
    except KeyboardInterrupt:
        # A terminal interrupt reaches the foreground group, including the
        # owned command. Wait for its teardown before this supervisor exits.
        print("Terminal interrupt received; waiting for foreground teardown.", flush=True)
        result = child.wait()
    output.write(f"\nExit: {result}; elapsed: {time.monotonic() - started:.2f}s\n")
print(log.read_text(), flush=True)
raise SystemExit(result)
