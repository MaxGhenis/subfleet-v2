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
