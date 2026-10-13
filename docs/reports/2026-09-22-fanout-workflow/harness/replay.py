"""Seeded independent oracle for submission history and reporting.

Loads the generated helper, replaces file reads with in-memory fake logs,
and never runs real Subfleet. Registry integration is exercised separately.
"""
import contextlib
import io
import random
import sys

namespace = {}
source = open(sys.argv[1], encoding="utf-8").read().split('\nverb = sys.argv[1]')[0]
exec(compile(source, sys.argv[1], "exec"), namespace)
F = namespace["F"]

if len(sys.argv) > 2 and sys.argv[2] == "--concurrent":
    # Exercise the actual generated commands, including the lock boundary.
    # Two shell processes wait at a start barrier in each seeded history.
    import json
    import os
    from pathlib import Path
    import re
    import shlex
    import shutil
    import subprocess
    import time

    original = Path(sys.argv[1]).parent
    command = Path(sys.argv[3]).read_text()
    rng = random.Random(12506)
    scenarios = ["", "unknown-c", "refuse-b,unknown-c", "submit-down-always",
                 "rowless-unknown", "lose-stdout", "manifest-refusal"]
    invocations = submissions = 0
    for history in range(64):
        root = original.parent / ("concurrent-%03d" % history)
        state = root / "state"
        state.mkdir(parents=True)
        for key in ("helper", "all", "manifest"):
            shutil.copyfile(original / F[key], state / F[key])
        current = command.replace(shlex.quote(str(original)), shlex.quote(str(state)))
        env = {**os.environ, "FAKE_STATE": str(root)}
        pair = [subprocess.Popen(["zsh", "-c", 'read -r START; eval "$1"', "concurrent-replay", current],
                cwd=root, env={**env, "SCENARIO": rng.choice(scenarios)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
        rng.shuffle(pair)
        for process in pair:
            process.stdin.write("start" + chr(10))
            process.stdin.close()
            process.stdin = None
            time.sleep(rng.random() / 1000)
        for process in pair:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (history, stdout, stderr)
            assert len(re.findall(r"^(?:SUBMITTED|REFUSED|NOT_SENT|OUTCOME_UNKNOWN) name=", stdout, re.M)) == 3, stdout
            invocations += 1
        # A final validation refusal stops retries and recovers every provable
        # unanswered request. The registry is an independent existence oracle.
        final = subprocess.run(["zsh", "-c", 'eval "$1"', "concurrent-replay", current],
            cwd=root, env={**env, "SCENARIO": "manifest-refusal"}, capture_output=True, text=True, timeout=30)
        assert final.returncode == 0, final.stdout + final.stderr
        registry = json.loads((root / "registry.json").read_text()) if (root / "registry.json").exists() else {}
        registered = {row["job_id"] for row in registry.values()}
        known = set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", final.stdout, re.M))
        assert known == registered, (history, registered, final.stdout)
        assert "UNSETTLED=0" + chr(10) in final.stdout, final.stdout
        count = int((root / "calls.run").read_text())
        logs = list(state.glob(F["submitPrefix"] + "*.jsonl"))
        assert len(logs) == count, (history, count, logs)
        assert 1 <= count <= 3
        # Replaying must not replace evidence or launch another submission.
        evidence = {path.name: path.read_bytes() for path in logs}
        replay = subprocess.run(["zsh", "-c", 'eval "$1"', "concurrent-replay", current],
            cwd=root, env=env, capture_output=True, text=True, timeout=30)
        assert replay.returncode == 0, replay.stdout + replay.stderr
        assert set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", replay.stdout, re.M)) == registered
        assert int((root / "calls.run").read_text()) == count
        assert evidence == {path.name: path.read_bytes() for path in logs}
        submissions += count
        if (history + 1) % 16 == 0:
            print(f"Concurrent replay: {history + 1}/64 histories checked", flush=True)
    print(f"PASS randomized concurrent replay: seed 12506, 64 histories, {invocations} overlapping invocations, {submissions} immutable submission logs", flush=True)
    sys.exit(0)

randomizer = random.Random(12505)
names = ["a", "b", "c"]
histories = transitions = 0

for use_request_id in (False, True):
    namespace["REQUEST_ID"] = "rq-review" if use_request_id else ""
    for _ in range(10000):
        logs, data, refusal, recovered = [], {}, None, []
        expected = {name: {"id": "", "uncertain": False, "outcome": ""} for name in names}
        namespace["submit_logs"] = lambda: logs
        namespace["rows"] = lambda path, kind: recovered if path == F["recovered"] else data.get(path, [])
        namespace["load"] = lambda path: refusal if path == F["refusal"] else data[path]
        namespace["recover_unknown"] = lambda state: None
        namespace["write_jobs"] = lambda ids: None
        namespace["tail"] = lambda path: ""
        data[F["all"]] = data[F["manifest"]] = {"jobs": [{"name": name, "out": "/fake/" + name} for name in names]}
        # This setup is a fake file system: post-submit must see the manifest.
        namespace["os"].path.isfile = lambda path: (path == F["refusal"] and refusal is not None) or path == F["manifest"]
        for invocation in range(randomizer.randint(1, 12)):
            # Only reachable submissions: no request-id relaunch can resubmit,
            # and refused/fully known entries cannot drive another invocation.
            if invocation and (not use_request_id or not any(
                    not entry["id"] and entry["outcome"] in ("", "not-sent", "unknown")
                    for entry in expected.values())):
                break
            path = f"log-{invocation}"
            logs.append(path)
            data[path] = []
            data[path + ".identity.json"] = {}
            whole_refusal = randomizer.randrange(8) == 0
            if whole_refusal:
                refusal = {"log": path, "rc": 2, "error": "nothing was submitted"}
                for entry in expected.values():
                    if not entry["id"] and not entry["uncertain"]:
                        entry["outcome"] = "refused"
            else:
                for name in names:
                    entry = expected[name]
                    answer = randomizer.choice(["created", "existing", "refused", "not-sent", "unknown", "missing"])
                    row = {"name": name, "outcome": answer, "job_id": None, "rc": -1,
                           "request_id": f"rq-review-{names.index(name)+1}" if use_request_id else f"auto-{invocation}-{name}"}
                    if answer in ("created", "existing"):
                        row["job_id"] = f"job-{invocation}-{name}"
                    if answer != "missing":
                        data[path].append(row)
                    if entry["id"]:
                        continue
                    if answer in ("created", "existing"):
                        entry["id"] = row["job_id"]
                        entry["outcome"] = answer
                    elif answer in ("unknown", "missing"):
                        entry["uncertain"] = True
                        entry["outcome"] = "unknown"
                    elif not entry["uncertain"]:
                        entry["outcome"] = answer
            # Relaunch replays every log; caller must receive each unset entry.
            state = namespace["settled"](names)
            for name, entry in expected.items():
                assert state[name]["job_id"] == entry["id"], (logs, name, entry, state[name])
                assert state[name]["outcome"] == entry["outcome"], (logs, name, entry, state[name])
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                namespace["cmd_post_submit"](-1)
            output = capture.getvalue()
            unsettled = sum(not entry["id"] and entry["outcome"] in ("not-sent", "unknown", "") for entry in expected.values())
            assert f"UNSETTLED={unsettled}\n" in output, output
            for name, entry in expected.items():
                marker = "SUBMITTED" if entry["id"] else {"refused": "REFUSED", "not-sent": "NOT_SENT"}.get(entry["outcome"], "OUTCOME_UNKNOWN")
                assert f"{marker} name={name} " in output, output
            retry = use_request_id and unsettled > 0 and refusal is None
            assert ("RETRY_STEP1\n" in output) == retry, output
            transitions += len(names)
            if whole_refusal:
                break  # Whole-manifest refusal prohibits further submissions.
        histories += 1

print(f"PASS randomized replay/reporting: seed 12505, {histories} histories, {transitions} entry transitions, with/without request ids")
