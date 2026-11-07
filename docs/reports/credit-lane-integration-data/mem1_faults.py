"""D-CREDIT-MEM1 forced faults (r3, attempt a2), kept as the integration report's data.

Each test runs one reader from the 13:10Z command's files as `c8bd8aae4` had it (or as
HEAD has it), against a writer that never stops, under the memory bound
(`-p tests.memory_bound`, SUBFLEET_TEST_MEMORY_MB set by the caller). Every test appends
one JSON line to $MEM1_LOG: what it ran, how long, and the footprint before and after.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from tests import memory_bound

LOG = os.environ["MEM1_LOG"]

ZEROS = "import os\nb = bytes(1 << 20)\nwhile True:\n    os.write(2, b)\n"
# A daemon that logs one traceback in a loop: the "repeats" shape.
LINES = ("import os\n"
         "line = (b'Traceback (most recent call last):\\n  File \"subfleet/daemon.py\", line 10080, in _launch\\n'\n"
         "        b'    self._open_gate(write_fd, key)\\nBrokenPipeError: [Errno 32] Broken pipe\\n') * 256\n"
         "while True:\n    os.write(2, line)\n")


def mb():
    found = memory_bound.footprint(os.getpid())
    return None if found is None else round(found[0] / (1 << 20))


def record(name, start, before, **extra):
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(json.dumps({"test": name, "seconds": round(time.monotonic() - start, 2),
                              "footprint_mb_before": before, "footprint_mb_after": mb(), **extra}) + "\n")


@pytest.mark.parametrize("child", [ZEROS, LINES], ids=["zeros", "lines"])
def test_c8_stdio_gate_reader(child):
    """c8bd8aae4's tests/fake/test_launch_gates_above_stdio.py reader, with its options:
    subprocess.run(capture_output=True, text=True, timeout=180), on a child that writes
    to stderr without end."""
    start, before, outcome = time.monotonic(), mb(), "returned"
    try:
        subprocess.run([sys.executable, "-c", child], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=180)
    except BaseException as exc:
        outcome = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        record("c8_stdio_gate_reader", start, before, outcome=outcome)


def test_after_a_trip_the_next_test_starts_here():
    """What the previous test left held when this one starts."""
    record("next_test_start", time.monotonic(), mb())


def flood(fd, stop, counter):
    chunk = bytes(1 << 16)
    try:
        while not stop.is_set():
            counter[0] += os.write(fd, chunk)
    except OSError:
        pass
    finally:
        os.close(fd)


def test_c8_admission_gate_reader_with_a_second_writer():
    """c8bd8aae4's tests/unit/test_credit_lane_admission.py reader:
    b"".join(iter(lambda: os.read(read, 65536), b"")), when a second holder of the write
    end writes without end (the test's own write end is closed, as there)."""
    start, before, outcome = time.monotonic(), mb(), "returned"
    read, write = os.pipe()
    stop, counter = threading.Event(), [0]
    # The release byte first, as _open_gate writes it, then the second writer: written
    # the other way round, the flood fills the pipe and this thread's own write blocks.
    os.write(write, b"1")
    threading.Thread(target=flood, args=(os.dup(write), stop, counter), daemon=True).start()
    try:
        os.close(write)
        gate = b"".join(iter(lambda: os.read(read, 65536), b""))
        outcome = f"read {len(gate)} bytes"
    except BaseException as exc:
        outcome = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        stop.set()
        os.close(read)
        record("c8_admission_gate_reader", start, before, outcome=outcome, written=counter[0])


def test_head_admission_gate_reader_with_a_second_writer():
    """HEAD's read_to_end (82bfd6a9e) on the same flood."""
    from tests.unit.test_credit_lane_admission import read_to_end
    start, before, outcome = time.monotonic(), mb(), "returned"
    read, write = os.pipe()
    stop, counter = threading.Event(), [0]
    # The release byte first, as _open_gate writes it, then the second writer: written
    # the other way round, the flood fills the pipe and this thread's own write blocks.
    os.write(write, b"1")
    threading.Thread(target=flood, args=(os.dup(write), stop, counter), daemon=True).start()
    try:
        os.close(write)
        gate = read_to_end(read, 1 + 4 + 4096)
        outcome = f"read {len(gate)} bytes"
    except BaseException as exc:
        outcome = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise
    finally:
        stop.set()
        os.close(read)
        record("head_admission_gate_reader", start, before, outcome=outcome, written=counter[0])


@pytest.mark.parametrize("prefix", [b"", b"1" + (0xFFFFFFFF).to_bytes(4, "big"),
                                    b"1" + (16).to_bytes(4, "big") + b"k" * 16],
                         ids=["zeros", "huge-length", "frame-then-zeros"])
def test_guardian_read_gate_on_a_flooded_gate(prefix):
    """The guardian's read_gate, in process as tests/unit/test_guardian.py runs it, on a
    gate whose writer never stops after `prefix`."""
    from subfleet import guardian
    start, before = time.monotonic(), mb()
    read, write = os.pipe()
    os.write(write, prefix)
    stop, counter = threading.Event(), [0]
    threading.Thread(target=flood, args=(write, stop, counter), daemon=True).start()
    try:
        released, key = guardian.read_gate(read, key_expected=True)
        outcome = f"released={released} key_bytes={None if key is None else len(key)}"
    finally:
        stop.set()
        record("guardian_read_gate", start, before, prefix=prefix.hex()[:20], outcome=outcome, written=counter[0])
