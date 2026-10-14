"""Formatter regression and mutations of the rendered held-lock runner.

Port of review r10's parser_probes.py. No ps or Subfleet subprocess runs;
the real flock and PID check execute, with only ps transport/time replaced.
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

from etime_formatters import darwin, procps
from lock_checks import NOW, check as property_check, corpus, elapsed


def runner_source(command):
    tokens = shlex.split(command.read_text())
    return tokens[tokens.index("-c") + 1]


def check(command):
    runner = compile(runner_source(command), "<rendered-lock-runner>", "exec")
    lock = command.parent / "formatter.lock"
    with lock.open("w+") as holder, patch.dict(os.environ, SUBFLEET_FANOUT_LOCK_WAIT_S="0"), \
            patch("sys.argv", ["lock-runner", str(lock)]):
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def inspect(value, since, now=float(NOW)):
            records = {role: {"pid": os.getpid(), "since": since}
                for role in ("launcher", "helper", "cli")}
            lock.write_text(json.dumps(records))

            def ps(args, **kwargs):
                assert args == ["ps", "-o", "etime=", "-p", str(os.getpid())], args
                assert kwargs == {"capture_output": True, "text": True, "timeout": 2}, kwargs
                return subprocess.CompletedProcess(args, 0, value, "")

            output = io.StringIO()
            with patch("time.time", return_value=now), patch("subprocess.run", side_effect=ps), \
                    redirect_stdout(output):
                try:
                    exec(runner, {})
                except SystemExit as result:
                    assert result.code == 75, (repr(value), result.code)
                else:
                    raise AssertionError("held lock did not exit")
            line = output.getvalue()
            assert line.startswith("LOCKED ") and len(line.splitlines()) == 1, (repr(value), line)
            statuses = [line.split(f"{role}=")[1].split()[0] for role in records]
            assert len(set(statuses)) == 1, line
            return statuses[0]

        ages = {0, 1, 59, 60, 3599, 3600, 3601, 86399, 86400, 86401,
            100 * 86400 - 1, 100 * 86400, 100 * 86400 + 1, 365 * 86400,
            10_000 * 86400, NOW - 1}
        rng = random.Random(125101)
        ages.update(rng.randrange(NOW) for _ in range(2048))
        checked = 0
        for formatter in (darwin, procps):
            for age in sorted(ages):
                value = "   " + formatter(age) + "\n"
                assert elapsed(value) == age, (formatter.__name__, age, repr(value), "oracle")
                status = inspect(value, max(0.125, NOW - age))
                assert status == "alive", (formatter.__name__, age, repr(value), status)
                checked += 1
        assert checked == 4128
        print(f"PASS source formatters: {checked} cases agree with runner and oracle; Darwin 60:00 alive")

        invalid = ["00:60", "60:01", "61:00", "99:00", "00:99", "1-24:00:00", "1-00:60:00",
            "1-00:00:60", "1-00:00", "00:0", "0:00", "00::00", "00:00\n00:00",
            "００:００", "٠٠:٠٠", "00:00\x00", "9" * 310 + ":00",
            "9" * 310 + ":00:00", "9" * 310 + "-00:00:00", "9" * 5000 + "-00:00:00"]
        for value in invalid:
            assert elapsed(value) is None, (repr(value), "oracle")
            assert inspect(value, NOW) == "unknown", repr(value)
        print(f"PASS malformed elapsed time: {len(invalid)} cases unknown, including 60:01, 61:00 and 99:00")

        for now in (float(NOW), NOW + 0.25):
            for delta, expected in ((-1, "alive"), (0, "alive"), (1, "unknown")):
                age = NOW + delta
                value = procps(age)
                assert elapsed(value) == (age if delta <= 0 else None)
                assert inspect(value, max(0.125, now - age), now) == expected, (age, now, expected)
        print("PASS epoch bound: 6 integral/fractional float cases")

        for value in corpus():
            age = elapsed(value)
            since = max(1, NOW - age) if age is not None else NOW
            statuses = [inspect(value, since, clock) for clock in (NOW, float(NOW), NOW + 0.25)]
            assert len(set(statuses)) == 1, (repr(value), statuses)
        print("PASS float clock: 4096 strings agree with integer, integral float and fractional float clocks")


def mutations(command):
    source = runner_source(command)
    mutants = {
        "reject Darwin boundary": source.replace("([0-9]{2}):([0-5][0-9])", "([0-5][0-9]):([0-5][0-9])", 1),
        "remove epoch bound": source.replace("if age > now:", "if False:", 1),
        "relax component ranges": source.replace("([0-9]{2}):([0-5][0-9])", "([0-9]+):([0-9]{2})", 1),
        "force alive": source.replace('return "reused" if now - age > since + 2 else "alive"', 'return "alive"', 1),
    }
    mutant_command = command.parent / "mutant.sh"
    for name, mutant in mutants.items():
        assert mutant != source, (name, "mutation did not apply")
        mutant_command.write_text(shlex.join(["python3", "-c", mutant, "unused"]))
        checks = (check, property_check) if name == "reject Darwin boundary" else (property_check,)
        for regression in checks:
            try:
                with redirect_stdout(io.StringIO()):
                    regression(mutant_command)
            except AssertionError as error:
                print(f"PASS mutation rejected by {regression.__module__}.{regression.__name__}: {name}: {str(error)[:180]}")
            else:
                raise AssertionError(f"mutant passed {regression.__name__}: {name}")
    mutant_command.unlink()


if __name__ == "__main__":
    command = Path(sys.argv[1])
    check(command)
    if "--mutations" in sys.argv[2:]:
        mutations(command)
