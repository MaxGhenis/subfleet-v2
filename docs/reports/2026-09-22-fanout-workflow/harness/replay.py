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
    selected = os.environ.get("FANOUT_REPLAY_CASE", "")
    assert selected in ("", "perturbations", "head", "policy", "sol", "fable"), selected
    scenarios = ["", "unknown-c", "refuse-b,unknown-c", "submit-down-always",
                 "rowless-unknown", "lose-stdout", "manifest-refusal"]
    invocations = submissions = 0
    for history in range(0 if selected else 64):
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
    if not selected:
        print(f"PASS randomized concurrent replay: seed 12506, 64 histories, {invocations} overlapping invocations, {submissions} immutable submission logs", flush=True)
    # Round 7: coordinate the mutation at the ACTUAL submission boundary,
    # after previews, then overlap a retry/refuser while the first holds the
    # workflow lock. The registry, not the helper's snapshot, is the oracle.
    rng = random.Random(12507)
    checked = 0
    for history in range(24):
        case = ("head" if history % 2 == 0 else "policy") if history < 16 else ("sol" if history % 2 == 0 else "fable")
        if selected not in ("", "perturbations", case):
            continue
        root = original.parent / ("perturbed-%03d" % history)
        state = root / "state"
        state.mkdir(parents=True)
        document = json.loads((original / F["manifest"]).read_text())
        alias = ("sol" if history % 2 == 0 else "fable") if history >= 16 else None
        canonical = "astra" if alias == "sol" else "opus"
        for job in document["jobs"]:
            prompt = root / ("prompt-" + job["name"] + ".md")
            prompt.write_bytes(Path(job["prompt"]).read_bytes())
            job["prompt"] = str(prompt)
            if alias:
                job["model"] = alias
        shutil.copyfile(original / F["helper"], state / F["helper"])
        for key in ("all", "manifest"):
            (state / F[key]).write_text(json.dumps(document))
        current = command.replace(shlex.quote(str(original)), shlex.quote(str(state)))
        env = {**os.environ, "FAKE_STATE": str(root), "SCENARIO": "check-prompts"}
        field = "workdir_head" if history % 2 == 0 else "policy_hash"
        variable = "FAKE_SUBMIT_HEAD" if field == "workdir_head" else "FAKE_SUBMIT_POLICY"
        if not alias:
            old = root / "old-state"
            old.mkdir()
            for key in ("helper", "all", "manifest"):
                shutil.copyfile(state / F[key], old / F[key])
            result = subprocess.run(["zsh", "-c", 'eval "$1"', "concurrent-seed",
                current.replace(shlex.quote(str(state)), shlex.quote(str(old)))],
                cwd=root, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and "JOBS=3\nUNSETTLED=0" in result.stdout, result.stdout + result.stderr
            before = (root / "registry.json").read_bytes()
            env[variable] = "different-at-submit"
        env["FAKE_CAPTURE_PAYLOADS"] = str(root / "actual-payloads.jsonl")
        # The wrapper leaves preview values alone, pauses only a batch call,
        # and hands every submission/lookup to the shipped fake.
        fake = root / "bin" / "subfleet"
        fake.parent.mkdir()
        shipped = shutil.which("subfleet", path=os.environ["PATH"])
        fake.write_text("#!" + sys.executable + "\n" + '''import os,pathlib,sys,time
root=pathlib.Path(os.environ["FAKE_STATE"])
if sys.argv[1] == "run" and "--batch" in sys.argv and os.environ.get("PERTURBED_ROLE") == "lost":
    (root/"lost-ready").touch()
    deadline=time.monotonic()+20
    while not (root/"release-lost").exists():
        if time.monotonic()>deadline: raise RuntimeError("submission barrier timed out")
        time.sleep(.01)
os.execv(''' + repr(shipped) + ''', [''' + repr(shipped) + ''', *sys.argv[1:]])
''')
        fake.chmod(0o755)
        env["PATH"] = str(fake.parent) + os.pathsep + env["PATH"]
        lost = subprocess.Popen(["zsh", "-c", 'eval "$1"', "concurrent-perturbed", current],
            cwd=root, env={**env, "SCENARIO": "lose-stdout,check-prompts", "PERTURBED_ROLE": "lost"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 20
        while not (root / "lost-ready").exists():
            assert time.monotonic() < deadline, history
            time.sleep(.01)
        second = "manifest-refusal" if alias or rng.randrange(2) else "check-prompts"
        if alias:
            Path(document["jobs"][1]["prompt"]).unlink()
        following_env = {**env, "SCENARIO": second}
        if not alias:
            following_env["FAKE_WORKDIR_HEAD" if field == "workdir_head" else "FAKE_POLICY_HASH"] = "different-at-submit"
        following = subprocess.Popen(["zsh", "-c", 'eval "$1"', "concurrent-perturbed", current],
            cwd=root, env=following_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.02)
        (root / "release-lost").touch()
        first_out, first_err = lost.communicate(timeout=30)
        following_out, following_err = following.communicate(timeout=30)
        assert lost.returncode == following.returncode == 0, (history, first_err, following_err)
        assert "JOBS=0\nUNSETTLED=3" in first_out, first_out
        registry = json.loads((root / "registry.json").read_text())
        registered = {row["job_id"] for row in registry.values()}
        actual = [json.loads(line) for line in (root / "actual-payloads.jsonl").read_text().splitlines()]
        assert len(actual) >= 3, actual
        reported = set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", following_out, re.M))
        if alias:
            assert reported == registered and "UNSETTLED=0\n" in following_out, following_out
            assert all(row["pinned_model"] == canonical for row in registry.values()), registry
            assert all(row["payload_digest"] == registry[row["request_id"]]["payload_digest"] for row in actual)
        else:
            assert not reported and "JOBS=0\nUNSETTLED=3" in following_out, following_out
            assert (root / "registry.json").read_bytes() == before
            assert all(row[field] != "different-at-submit" for row in registry.values())
            assert all(row[field] == "different-at-submit" and row["payload_digest"] != registry[row["request_id"]]["payload_digest"] for row in actual)
        count = int((root / "calls.run").read_text())
        logs = list(state.glob(F["submitPrefix"] + "*.jsonl"))
        assert len(logs) == 2 and count == (2 if alias else 3), (history, count, logs)
        evidence = {path.name: path.read_bytes() for path in logs}
        if not alias:
            # Stop further attempts without changing earlier uncertainty.
            following_env["SCENARIO"] = "manifest-refusal"
        final = subprocess.run(["zsh", "-c", 'eval "$1"', "concurrent-perturbed", current],
            cwd=root, env=following_env, capture_output=True, text=True, timeout=30)
        assert final.returncode == 0, final.stdout + final.stderr
        assert set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", final.stdout, re.M)) == reported, final.stdout
        count = int((root / "calls.run").read_text())
        replay_logs = list(state.glob(F["submitPrefix"] + "*.jsonl"))
        immutable = {path.name: path.read_bytes() for path in replay_logs}
        assert all(immutable[name] == content for name, content in evidence.items())
        replay = subprocess.run(["zsh", "-c", 'eval "$1"', "concurrent-perturbed", current],
            cwd=root, env=following_env, capture_output=True, text=True, timeout=30)
        assert replay.returncode == 0 and int((root / "calls.run").read_text()) == count, replay.stdout + replay.stderr
        assert immutable == {path.name: path.read_bytes() for path in state.glob(F["submitPrefix"] + "*.jsonl")}
        assert set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", replay.stdout, re.M)) == reported
        checked += 1
        if (history + 1) % 8 == 0:
            print(f"Concurrent perturbations: {history + 1}/24 histories checked", flush=True)
    print(f"PASS concurrent perturbations: seed 12507, {checked} histories, {checked * 2} overlapping invocations, case={selected or 'all'}, immutable logs and relaunch proof", flush=True)
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
