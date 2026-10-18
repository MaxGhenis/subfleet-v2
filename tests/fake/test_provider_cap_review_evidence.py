"""Review round 1 inputs for PR #194, copied with regression expectations.

All state is temporary and fake; run under the shared suite lock.
"""

import json

import pytest

from subfleet import scheduler
from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import Daemon, after
from subfleet.policy import load_policy
from subfleet.procs import Containment
from tests.fake.test_admission_latency import fleet_daemon, submit, submit_turn
from tests.fake.test_admission_liveness import _end, _live
from tests.fake.test_provider_active_cap_fake import live_count, prepare


def test_policy_40_loaded_after_restart_and_shared_by_pools(tmp_path):
    """C-6.4a / C-11.1: execute the chosen value through disk loading and admission."""
    with fleet_daemon(tmp_path / "state") as (old, harness, patch):
        prepare(old, patch, 2)
        data = json.loads((old.root / "policy.json").read_text())
        data["providers"] = {"codex": {"max_active_attempts": 40}}
        data["caps"]["reading_ttl_s"] = 3600
        (old.root / "policy.json").write_text(json.dumps(data) + "\n")
        assert old.policy["providers"]["codex"]["max_active_attempts"] == 2
        assert load_policy(old.root / "policy.json")["providers"]["codex"]["max_active_attempts"] == 40
        root = old.root
        old.close()
        new = Daemon(root)
        try:
            patch.setattr(new, "_workspace", lambda job: (job["workdir"], None, None, []))
            patch.setattr(new, "_desktop_in_use", lambda: False)
            assert new.policy["providers"]["codex"]["max_active_attempts"] == 40
            turns = [submit_turn(new, harness, n) for n in range(1, 6)]
            jobs = [submit(new, harness) for _ in range(36)]
            new._admit()
            assert all(_live(new, job) for job in turns)
            assert sum(_live(new, job) for job in jobs) == 35
            assert live_count(new) == 40
            assert not new.store.list_attempts(jobs[-1])
            assert new._holds[jobs[-1]]["reason"] == "provider-cap"
            _end(new, turns[0])
            new._admit()
            assert _live(new, jobs[-1]) and live_count(new) == 40
            print("POLICY40: disk edit -> new Daemon -> 5 turns + 35 detached; 41st held unleased; completion admits it")
        finally:
            new.close()


def test_quarantined_survivor_retains_provider_slot(tmp_path):
    """D-RT1 diagnostic: quarantine can retain a live provider process."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        prepare(service, patch, 1)
        first = submit(service, harness)
        service._admit()
        attempt = service.store.list_attempts(first)[0]
        census = Containment(marker_pids=frozenset({123456}))
        assert not census.verified_empty
        service._quarantine(attempt, census, "review: provider child still live")
        second = submit(service, harness)
        service._admit()
        assert service.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
        assert not _live(service, second)
        assert not service.store.list_attempts(second)
        assert service._holds[second]["reason"] == "provider-cap"
        assert scheduler.provider_live(service._capacity_view())["codex"] == 1
        print("QUARANTINE: cap=1, uncontained marker pid=123456 -> second held unleased; reported count=1")


@pytest.mark.parametrize("extra_key,value", [("max_in_flight_per_lane", 2), ("max_active_attempts_per_parent", 1)])
def test_existing_optional_caps_allow_selected_uncapped_provider(tmp_path, extra_key, value):
    """C-6.4a diagnostic: unrelated old caps disable deferred provider FIFO."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        prepare(service, patch, 2)
        service.policy["caps"][extra_key] = value
        service.store.add_closure(Closure("codex-1", "account", after(3600),
                                         ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "review"))
        older = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
        mixed = submit(service, harness, pinned_model=None, task="research", tier="standard")
        service._admit()
        assert not _live(service, older)
        assert _live(service, mixed)
        assert service.store.list_attempts(mixed)[0]["lane_id"] == "claude-1"
        print(f"MIXED: providers.codex.max_active_attempts=2, caps.{extra_key}={value}, older Codex lane closed; mixed research reserves Claude without waiting behind Codex")


def test_transient_retry_keeps_provider_when_only_cap_blocks_it(tmp_path):
    """C-4.5: the provider gate is a slot wait, not a reason to abandon a retry."""
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        prepare(service, patch, 1)
        first = submit(service, harness, pinned_model=None, task="research", tier="standard")
        # Force the first attempt onto Codex; reopen Claude before its retry.
        closure = Closure("claude-1", "account", after(3600),
                          ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "review")
        service.store.add_closure(closure)
        service._admit()
        attempt = service.store.list_attempts(first)[0]
        assert attempt["lane_id"].startswith("codex-")
        _end(service, first)
        with service.store.transaction("review.transient") as tx:
            tx.execute("UPDATE attempts SET state='failed',outcome_class='transient' WHERE attempt_id=?", (attempt["attempt_id"],))
            tx.execute("UPDATE closures SET released_at=? WHERE lane_id='claude-1'", (after(0),))
        filler = submit(service, harness)
        # Make the retry pending after another job has filled the provider.
        service._admit()
        assert _live(service, filler)
        with service.store.transaction("review.due") as tx:
            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=NULL WHERE job_id=?", (first,))
        service._admit()
        assert len(service.store.list_attempts(first)) == 1
        assert service._holds[first]["reason"] == "provider-cap"
        print("RETRY: transient Codex first attempt + cap full + Claude now open -> waits on Codex, no provider switch")
