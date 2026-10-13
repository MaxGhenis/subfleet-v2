"""Reviewer's independent round-8 oracle, ported for round 9.

Seed 20261013; 32 histories, including preview faults, prompt deletion,
foreign holders and launcher/helper kills. Every child is supervised and
joined; no detached sessions or evidence files are created.
"""
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parents[3]
SCRIPT = Path(sys.argv[1]).resolve()


class Fixture:
    def __init__(self, label, model=None, scenario="check-prompts"):
        self.root = Path(tempfile.mkdtemp(prefix=label + "-"))
        (self.root / "bin").mkdir()
        (self.root / "empty-home").mkdir()
        fake = self.root / "bin/subfleet"
        shutil.copyfile(HARNESS / "fake-subfleet.sh", fake)
        fake.chmod(0o755)
        git = self.root / "bin/git"
        git.write_text('#!/bin/bash\nprintf "%s\\n" "${FAKE_WORKDIR_HEAD:-head-one}"\n')
        git.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.root / "bin") + ":" + os.environ["PATH"],
            "FAKE_STATE": str(self.root), "SUBFLEET_HOME": str(self.root / "empty-home"),
            "ZDOTDIR": str(self.root / "empty-home"), "SUBFLEET_FANOUT_RETRY_S": "0",
            "SUBFLEET_RUN_DETACH": "0", "FANOUT_FAKE_REPOSITORY": str(REPO),
            "SCENARIO": scenario, "PYTHONDONTWRITEBYTECODE": "1", "FAKE_PROCESS_STARTED": str(time.time())}
        self.env.pop("PYTHONPATH", None)
        self.args = {"label": "review8", "requestId": "rq-r8", "allowTmp": True,
            "stateDir": str(self.root / "state"), "items": []}
        for name in "abc":
            prompt = self.root / ("prompt-" + name + ".md")
            prompt.write_text("Review " + name)
            self.args["items"].append({"task": "review", "tier": "standard", "dir": str(REPO),
                "promptPath": str(prompt), "outPath": str(self.root / ("out-" + name + ".md")), "name": name})
        if model:
            self.args["model"] = model
        shutil.copyfile(HARNESS / "fake-ps.py", self.root / "bin/ps")
        (self.root / "bin/ps").chmod(0o755)
        self.render()

    @property
    def state(self):
        return Path(self.args["stateDir"])

    def render(self):
        subprocess.run(["bun", str(HARNESS / "render.mjs"), str(SCRIPT), str(self.root), json.dumps(self.args)],
            check=True, capture_output=True, text=True, env=self.env, timeout=120)
        self.commands = [(self.root / ("step%d.sh" % i)).read_text() for i in range(3)]

    def launch(self, step, env=None):
        return subprocess.Popen(["zsh", "-c", 'eval "$1"', "review8", self.commands[step]],
            cwd=self.root, env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def step(self, step, env=None):
        child = self.launch(step, env)
        out, err = child.communicate(timeout=300)
        return child.returncode, out, err

    def setup(self):
        rc, out, err = self.step(0)
        assert rc == 0 and "SETUP_OK" in out, (rc, out, err)

    def wrapper(self, code):
        fake = self.root / "bin/subfleet"
        shutil.copyfile(fake, str(fake) + "-registry")
        Path(str(fake) + "-registry").chmod(0o755)
        fake.write_text("#!" + sys.executable + "\nimport os,json,sys,time\nfrom pathlib import Path\nroot=Path(os.environ['FAKE_STATE'])\n" + code +
            "\nos.execv(__file__+'-registry',[__file__+'-registry',*sys.argv[1:]])\n")

    def cleanup(self):
        subprocess.run([sys.executable, "-c", 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)', str(self.root)], check=True)


def until(predicate, seconds=120):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "readiness timeout"
        time.sleep(.01)


rng = random.Random(20261013)
total_locked = total_kills = 0
count = int(os.environ.get("R8_MACHINE_HISTORIES", "32"))
start = int(os.environ.get("R8_MACHINE_START", "0"))
for history in range(start, count):
    model = [None, "opus", "sol", "fable"][history % 4]
    preview = ["", "down", "bad", "missing"][(history // 4) % 4]
    own = history % 3 != 0
    kill = [None, "launcher", "both"][history % 3]
    scenario = rng.choice(["resend-moved,check-prompts", "resend-moved-c,unknown-a,check-prompts", "lose-stdout,check-prompts"])
    c = Fixture("machine", model=model)
    owner = follower = None
    holders = None
    try:
        if not own:
            c.args["stateDir"] = str(c.root / "old-state")
            c.render(); c.setup()
            rc, out, err = c.step(1)
            assert rc == 0 and "JOBS=3\nUNSETTLED=0" in out, (rc, out, err)
            c.args["stateDir"] = str(c.root / "state")
            c.render()
        c.setup()
        c.wrapper('''if sys.argv[1] == "run" and "--batch" in sys.argv and os.environ.get("R8_ROLE") == "owner":
    (root/"owner-ready.new").write_text(json.dumps({"cli":os.getpid(),"helper":os.getppid()}))
    os.replace(root/"owner-ready.new",root/"owner-ready")
    deadline=time.monotonic()+120
    while not (root/"release-owner").exists():
        assert time.monotonic()<deadline, "owner barrier timeout"
        time.sleep(.01)
''')
        c.env.update(FAKE_POLICY_PROOF=preview, FAKE_RESEND_MOVES=rng.choice(["head", "policy"]),
            FAKE_SUBMIT_HEAD="moved-at-submit", FAKE_SUBMIT_POLICY="policy-at-submit")
        owner = c.launch(1, {**c.env, "SCENARIO": scenario, "R8_ROLE": "owner"})
        until(lambda: (c.root / "owner-ready").exists())
        holders = json.loads((c.root / "owner-ready").read_text())
        lock_record = json.loads((c.state / "rq-r8.lock").read_text())
        assert lock_record["helper"]["pid"] == holders["helper"]
        if history % 2:
            Path(c.args["items"][1]["promptPath"]).unlink()
        if kill:
            os.kill(lock_record["launcher"]["pid"], signal.SIGKILL)
            total_kills += 1
            if kill == "both":
                os.kill(holders["helper"], signal.SIGKILL)
                total_kills += 1
        following_env = {**c.env, "SCENARIO": rng.choice(["manifest-refusal", "check-prompts", "resend-moved,check-prompts"]), "R8_ROLE": "follower"}
        follower = c.launch(1, following_env)
        time.sleep(.1)
        assert follower.poll() is None, "follower crossed inherited lock"
        (c.root / "release-owner").touch()
        first_out, first_err = owner.communicate(timeout=300)
        out, err = follower.communicate(timeout=300)
        rc = follower.returncode
        if rc == 75:
            assert out.startswith("LOCKED ") and "JOBS=" not in out, (out, err)
            total_locked += 1
            # The dead parent's CLI may still be completing the fake registry.
            deadline = time.monotonic() + 120
            while True:
                try:
                    os.kill(holders["cli"], 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline
                time.sleep(.02)
            rc, out, err = c.step(1, following_env)
        assert rc == 0, (history, rc, out, err)
        logs = {p.name: p.read_bytes() for p in c.state.glob("rq-r8.submit.*.jsonl")}
        registry = json.loads((c.root / "registry.json").read_text())
        valid_ids = {row["job_id"] for row in registry.values()}
        reported = set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", out, re.M))
        assert reported <= valid_ids, (history, out, registry)
        # Every entry is reported, even when rewritten aliases cannot be proved.
        for name in "abc":
            assert re.search(r"^(SUBMITTED|REFUSED|OUTCOME_UNKNOWN|NOT_SENT) name=" + name + r" ", out, re.M), out
        for log in c.state.glob("rq-r8.recovered.jsonl"):
            for line in log.read_text().splitlines():
                recovered = json.loads(line)
                stored = next(row for row in registry.values() if row["job_id"] == recovered["job_id"])
                assert recovered["submit_batch"] == recovered["identity"]["submit_batch"] == stored["batch"]
                assert recovered["identity"]["pinned_model"] == stored["pinned_model"]
                assert recovered["payload_digest"] == stored["payload_digest"]
                assert own, "adopted a job from an older state"
        c.env.update(following_env, SCENARIO="manifest-refusal")
        c.setup()
        rc2, out2, err2 = c.step(1)
        assert rc2 == 0, (rc2, out2, err2)
        reported2 = set(re.findall(r"^SUBMITTED name=\S+ job_id=(\S+)", out2, re.M))
        assert reported <= reported2 <= valid_ids, (out, out2)
        # Independently require every answer or provable creation to settle.
        # A failed alias preview may prevent proof, but must not hide an
        # unchanged pin or an unpinned job this state actually created.
        answered_ids = {row["job_id"] for log in c.state.glob("rq-r8.submit.*.jsonl")
            for line in log.read_text().splitlines() if line.strip()
            for row in [json.loads(line)] if row.get("job_id")}
        snapshots = [json.loads(path.read_text()) for path in c.state.glob("rq-r8.submit.*.jsonl.identity.json")]
        expected = set(answered_ids)
        for stored in registry.values():
            for snapshot in snapshots:
                identity = snapshot.get(stored["name"], {})
                if identity.get("submit_batch") != stored["batch"]:
                    continue
                fields = ("name", "workdir", "out_path", "task", "tier", "sandbox", "pinned_model")
                if all(identity.get(field) == stored[field] for field in fields) and identity.get("prompt_sha256") == stored["prompt_sha256"]:
                    expected.add(stored["job_id"])
        assert reported2 == expected, (history, expected, reported2, out2)
        assert all((c.state / name).read_bytes() == data for name, data in logs.items())
        assert set((c.state / "rq-r8.jobs.txt").read_text().split()) == reported2
        print("PASS independent machine", history, "model", model, "preview", preview or "ok", "own", own, "kill", kill, flush=True)
    finally:
        (c.root / "release-owner").touch()
        for process in [owner, follower]:
            if process:
                # Draining inherited pipes also joins the surviving fake CLI
                # after a killed launcher. Release its barrier before waiting.
                try:
                    process.communicate(timeout=300)
                except subprocess.TimeoutExpired:
                    process.kill()
                    if holders:
                        for pid in (holders["cli"], holders["helper"], lock_record["launcher"]["pid"]):
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    process.communicate()
        c.cleanup()
print("PASS independent machine:", count-start, "histories;", total_kills, "kills;", total_locked, "LOCKED peers", flush=True)
