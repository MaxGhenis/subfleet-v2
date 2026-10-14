"""Bounded property check of the complete rendered runner under a real flock.

Seed 12510; 4,096 strings of at most 8,192 characters. Only ps transport and
wall time are replaced: file locking, PID existence, holder JSON, LOCKED and
SystemExit execute the generated source. No child or evidence file is made.
"""
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import random
import shlex
import subprocess
import sys
from unittest.mock import patch

NOW = 2_000_000_000
SEED = 12510
COUNT = 4096


def elapsed(value):
    """Independent split/range oracle, with no production regex or parser."""
    pieces = value.strip().split("-")
    if len(pieces) not in (1, 2):
        return None
    clock = pieces[-1].split(":")
    if len(clock) not in (2, 3) or (len(pieces) == 2 and len(clock) != 3):
        return None
    fields = ([pieces[0]] if len(pieces) == 2 else []) + clock
    if any(not field or not field.isascii() or not field.isdecimal() for field in fields):
        return None
    if any(len(field) != 2 for field in clock[-2:]):
        return None
    try:
        days = int(pieces[0]) if len(pieces) == 2 else 0
        hours = int(clock[0]) if len(clock) == 3 else 0
        minutes, seconds = map(int, clock[-2:])
    except ValueError:
        return None
    if minutes > 59 or seconds > 59 or (len(pieces) == 2 and hours > 23):
        return None
    age = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    return age if age <= NOW else None


def corpus():
    # Numeric boundaries, missing fields, separators, Unicode and conversion
    # guard/float-overflow sizes, including huge valid-shaped hours and days.
    values = ["00:99", "9" * 310 + ":00", "", " ", "\x00", "\ud800",
        "00:00\n00:00", "1-00:00", "0:00", "00:0", "-00:00", "+00:00",
        "٠٠:٠٠", "００:００", "00:00.0", "NaN", "Infinity", "00::00",
        "9" * 310 + ":00:00", "9" * 310 + "-00:00:00",
        "9" * 5000 + ":00:00", "9" * 5000 + "-00:00:00",
        "0" * 5000 + ":00:00", "0" * 8192,
        "555555:33:19", "555555:33:20", "555555:33:21"]
    for days in (None, "0", "1", "23148"):
        for hours in ("00", "23", "24", "99"):
            for minutes in ("00", "59", "60", "99"):
                for seconds in ("00", "59", "60", "99"):
                    values.append((days + "-" if days is not None else "") +
                        ":".join((hours, minutes, seconds)))
    for minutes in ("00", "01", "59", "60", "99", "999"):
        for seconds in ("00", "01", "59", "60", "99"):
            values.append(minutes + ":" + seconds)
    rng = random.Random(SEED)
    alphabet = "0123456789:-+ .\t\r\n\x00abc９١\ud800"
    while len(values) < COUNT:
        kind = len(values) % 4
        if kind == 0:
            age = rng.randrange(NOW)
            form = rng.randrange(3)
            if form == 0:
                age %= 3600
                value = "%02d:%02d" % (age // 60, age % 60)
            elif form == 1:
                value = "%02d:%02d:%02d" % (age // 3600, age // 60 % 60, age % 60)
            else:
                value = "%d-%02d:%02d:%02d" % (age // 86400, age // 3600 % 24, age // 60 % 60, age % 60)
            values.append(rng.choice(("", " ", "\t")) + value + rng.choice(("", "\n", "\r\n")))
        elif kind == 1:
            values.append("%d-%02d:%02d:%02d" % (rng.randrange(100000), rng.randrange(100), rng.randrange(100), rng.randrange(100)))
        elif kind == 2:
            values.append("".join(rng.choices(alphabet, k=rng.randrange(129))))
        else:
            # Include enormous numeric fields without expensive random sizes.
            values.append(rng.choice("09") * rng.choice((1, 2, 3, 20, 310, 5000)) + rng.choice((":00", ":00:00", "-00:00:00")))
    assert len(values) == COUNT and max(map(len, values)) <= 8192
    return values


def check(command):
    tokens = shlex.split(command.read_text())
    runner = compile(tokens[tokens.index("-c") + 1], "<rendered-lock-runner>", "exec")
    lock = command.parent / "property.lock"
    checked = alive = reused = unknown = 0
    with lock.open("w+") as holder, patch.dict(os.environ, SUBFLEET_FANOUT_LOCK_WAIT_S="0"), \
            patch("sys.argv", ["lock-runner", str(lock)]), patch("time.time", return_value=NOW):
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for value in corpus():
            age = elapsed(value)
            since = max(1, NOW - age) if age is not None else NOW
            records = {role: {"pid": os.getpid(), "since": max(1, since - offset)}
                for role, offset in (("launcher", 0), ("helper", 3), ("cli", 0))}
            lock.write_text(json.dumps(records))
            def inspect(args, **kwargs):
                assert args == ["ps", "-o", "etime=", "-p", str(os.getpid())], args
                assert kwargs == {"capture_output": True, "text": True, "timeout": 2}, kwargs
                return subprocess.CompletedProcess(args, 0, value, "")
            output = io.StringIO()
            with patch("subprocess.run", side_effect=inspect), redirect_stdout(output):
                try:
                    exec(runner, {})
                except SystemExit as result:
                    assert result.code == 75, (repr(value), result.code)
                else:
                    raise AssertionError((repr(value), "held lock did not exit"))
            line = output.getvalue()
            assert line.startswith("LOCKED ") and len(line.splitlines()) == 1, (repr(value), line)
            for role, entry in records.items():
                expected = "unknown" if age is None else (
                    "reused" if NOW - age > entry["since"] + 2 else "alive")
                assert f"{role}_pid={os.getpid()} {role}={expected}" in line, (repr(value), expected, line)
                if expected == "alive":
                    assert age is not None and abs(NOW - age - entry["since"]) <= 2
                    alive += 1
                elif expected == "reused":
                    reused += 1
                else:
                    unknown += 1
            checked += 1
    assert checked == COUNT and alive and reused and unknown
    print(f"PASS elapsed-time property: seed {SEED}, {checked} strings, {checked * 3} holder classifications "
        f"({alive} alive, {reused} reused, {unknown} unknown); LOCKED/exit 75 throughout")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
