"""C-8.5 with C-8.3 and C-6.5: a host-push job's export, after its push.

release/217 (#148) decides every export on output identity: of every holder
of any spelling of the output, the oldest wins; a lease not held, a newer
owner, or no accepted attempt sets `export_error`; a replay annotates the
notice once. #158 publishes a push job's bundle on the push thread first,
with the job's leases held. These tests drive both through real submit,
acceptance, a local `file://` push and the production export. Each export
decision is checked for a push that succeeded, one Git refused, and an
ordinary job with no push: all three must export alike.

The second spelling of the output goes through a symlinked parent folder.
`folders.identity` resolves it on any volume, so the legacy-alias census runs
here on case-sensitive filesystems too.
"""
import threading
from uuid import uuid4

import pytest

from subfleet import resource_leases
from test_host_push import accept, commit, git, remote_heads, submit, worlds  # noqa: F401

#: The fixture adapter's deliverable (`test_host_push.worlds`).
DELIVERABLE = b"Finished and tested.\n"
OLDER, NEWER = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
#: What the export decides, for the job and for the legacy job holding the
#: output's other spelling. `{job}` and `{legacy}` are the two job ids.
DECISIONS = {
    # The job's lease is the oldest: it publishes and marks the newer holder.
    "owner": {"error": None, "output": DELIVERABLE, "exports": 1,
              "legacy_error": "superseded by {job}", "legacy_lease": True},
    # A legacy job took the other spelling first: its output stays.
    "superseded": {"error": "superseded by {legacy}", "output": b"OLDER\n", "exports": 0,
                   "legacy_error": None, "legacy_lease": True},
    # Nobody holds the output: nothing is written.
    "unheld": {"error": "export failed: output lease not held", "output": None, "exports": 0,
               "legacy_error": None, "legacy_lease": False},
}


def hold(core, key, holder, at):
    """A lease taken at `at`: admission's canonical key, or an older daemon's spelling."""
    with core.store.transaction("test.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, holder, at))


def notice(core, job):
    return "\n".join(row["text"] for row in core.store.query(
        "SELECT text FROM notices WHERE job_id=? ORDER BY notice_id", (job,)))


def outcome(core, job, legacy):
    """Everything an export or a push writes, for comparison across a replay."""
    row = core._job(job)
    return {
        "job": {key: row[key] for key in ("state", "rc", "accepted_attempt_id", "export_error",
                                          "push_sha", "push_error")},
        "artifacts": [dict(a) for a in core.store.query(
            "SELECT role,path,sha256,bytes FROM artifacts WHERE attempt_id=? ORDER BY role,path",
            (row["accepted_attempt_id"],))],
        "pushes": [dict(p) for p in core.store.query("SELECT * FROM job_pushes")],
        "leases": [dict(lease) for lease in core.store.list_leases()],
        "notices": (notice(core, job), notice(core, legacy)),
        "legacy_error": core._job(legacy)["export_error"],
    }


def pushes(world):
    return [args for _, args in world.calls if args[0] == "push"]


@pytest.mark.parametrize("decision", sorted(DECISIONS))
@pytest.mark.parametrize("push", ["pushed", "refused", "none"])
def test_a_push_settles_with_leases_held_then_the_canonical_export_is_an_ordinary_jobs(worlds, push, decision):
    with worlds() as world:
        core = world.core
        if push == "refused":
            # Git refuses `jobs/finished` beside an existing `jobs` branch.
            git(world.repo, "push", "origin", f"{world.base}:refs/heads/jobs")
        outputs, link = world.prompt.parent / "outputs", world.prompt.parent / "outputs-link"
        outputs.mkdir()
        link.symlink_to(outputs, target_is_directory=True)
        if push == "none":
            job = submit(world, None, sandbox="read-only", out_path=str(outputs / "result.md"))
        else:
            job = submit(world, out_path=str(outputs / "result.md"))
        out = core._job(job)["out_path"]
        claim = resource_leases.OutputClaim.prepare(core.store.query, out)
        alias = f"out:{link / 'result.md'}"
        legacy = core.store.add_job(job_id=f"legacy-{uuid4().hex[:8]}", request_id=str(uuid4()),
                                    payload_digest="legacy", kind="dispatch", state="running",
                                    workdir=str(world.repo), prompt_path=str(world.prompt),
                                    sandbox="read-only", out_path=str(link / "result.md"))
        core.store.add_notice(legacy, "legacy job finished")
        if decision == "owner":
            hold(core, claim.key, job, OLDER)        # what admission takes (C-6.5)
            hold(core, alias, legacy, NEWER)
        elif decision == "superseded":
            hold(core, alias, legacy, OLDER)
            hold(core, claim.key, job, NEWER)
            (outputs / "result.md").write_bytes(b"OLDER\n")
        if decision != "unheld":
            # The census sees both spellings: two keys, one output.
            assert claim.key != alias and alias in resource_leases.OutputClaim.prepare(core.store.query, out).keys
        before = {lease["lease_key"] for lease in core.store.list_leases(job)}
        sha = commit(world.repo) if push != "none" else None

        gate, entered = threading.Event(), threading.Event()
        actual = core._publish_bundle

        def held(row):
            entered.set()
            assert gate.wait(timeout=600)
            return actual(row)

        def in_flight():
            # The push is queued or running: nothing is decided or written, every lease is held.
            assert {lease["lease_key"] for lease in core.store.list_leases(job)} == before | {"push:" + job}, (
                "leases released before the push ended")
            assert core.store.query("SELECT * FROM artifacts WHERE role='export'") == [], (
                "exported before the push ended")
            assert core._job(job)["export_error"] is None and core._job(legacy)["export_error"] is None, (
                "export decided before the push ended")
            assert core._wait_answer([job]) is None
            assert core._pending_exports() == [job]
        core._publish_bundle = held
        try:
            row = accept(world, job, bundle=push != "none", settle=False)   # and acceptance's export pass
            attempt = row["accepted_attempt_id"]
            if push != "none":
                in_flight()
                assert entered.wait(timeout=600)
                in_flight()
                core._export(job)           # the control loop's next pass, while the push runs
                in_flight()
        finally:
            gate.set()                      # a failed assertion never leaves the push thread waiting

        if push != "none":
            core.pushes.submit(lambda: None).result(timeout=900)
            row = core._job(job)
            if push == "pushed":
                assert row["push_sha"] == sha and not row["push_error"]
                assert remote_heads(world)["refs/heads/jobs/finished"] == sha
            else:
                assert row["push_error"] and not row["push_sha"]
                assert "refs/heads/jobs/finished" not in remote_heads(world)
            # Settled: the export has not run yet, and the leases are still held.
            assert core.store.query("SELECT * FROM artifacts WHERE role='export'") == []
            assert core._job(job)["export_error"] is None
            assert core._pending_exports() == [job]
            core._export(job)               # the pass after the push: #148's export
        else:
            assert not entered.is_set() and core.store.query("SELECT * FROM job_pushes") == []

        want = {key: value.format(job=job, legacy=legacy) if isinstance(value, str) else value
                for key, value in DECISIONS[decision].items()}
        row = core._job(job)
        assert row["state"] == "succeeded" and row["rc"] == 0 and row["accepted_attempt_id"] == attempt
        assert row["export_error"] == want["error"], "export decision"
        assert ((outputs / "result.md").read_bytes() if (outputs / "result.md").exists() else None) == want["output"]
        assert len(core.store.query("SELECT * FROM artifacts WHERE attempt_id=? AND role='export'",
                                    (attempt,))) == want["exports"]
        assert core._job(legacy)["export_error"] == want["legacy_error"], "the other holder's decision"
        assert bool(core.store.list_leases(legacy)) == want["legacy_lease"]
        assert core.store.list_leases(job) == [] and core.store.list_leases(attempt) == []
        assert core._pending_exports() == [] and core._wait_answer([job]) is not None
        text = notice(core, job)
        if want["error"]:
            assert text.count(want["error"]) == 1
        if want["legacy_error"]:
            assert notice(core, legacy).count(want["legacy_error"]) == 1
        assert text.count("pushed ") + text.count("push failed: ") == (push != "none")
        # One `git push` for a push job (the remote refuses one); none without.
        assert len(pushes(world)) == (push != "none")

        # A replay, as after a restart: the same decision, nothing written twice.
        settled = outcome(core, job, legacy)
        for _ in range(2):
            core._export(job)
        core.pushes.submit(lambda: None).result(timeout=900)
        replayed = outcome(core, job, legacy)
        for key, value in settled.items():
            assert replayed[key] == value, f"replay changed {key}"
        assert len(pushes(world)) == (push != "none")


def test_a_push_job_without_an_accepted_attempt_never_pushes_and_takes_the_export_failure(worlds):
    """#148's missing-attempt rule holds for a push job: no push is handed to the
    push thread, the export fails with a notice, and every lease is released."""
    with worlds() as world:
        core = world.core
        out = world.prompt.parent / "result.md"
        job = submit(world, out_path=str(out))
        hold(core, resource_leases.OutputClaim.prepare(core.store.query, core._job(job)["out_path"]).key, job, OLDER)
        commit(world.repo)
        core._export = lambda job_id: None          # acceptance's own export pass waits
        row = accept(world, job, settle=False)
        del core._export
        attempt = row["accepted_attempt_id"]
        assert {lease["lease_key"] for lease in core.store.list_leases(job)} >= {"push:" + job}
        core.store.update_job(job, accepted_attempt_id=None)
        for _ in range(2):                          # the second is a replay
            core._export(job)
            core.pushes.submit(lambda: None).result(timeout=900)
            row = core._job(job)
            assert row["export_error"] == "export failed: no accepted attempt"
            assert notice(core, job).count("export failed: no accepted attempt") == 1
            assert not row["push_sha"] and not row["push_error"]
            assert core.store.query("SELECT * FROM job_pushes") == [] and pushes(world) == []
            assert core.store.list_leases(job) == [] and core.store.list_leases(attempt) == []
            assert not out.exists()
