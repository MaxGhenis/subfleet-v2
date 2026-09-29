"""C-5.7a: an operator resolves a quarantined probe as C-5.7 resolves an attempt.

A quarantined probe keeps its one lease, a lane slot, and before this clause only
a verified-empty census ended that: `kill --confirm-dead` and `--force-release`
looked only at quarantined attempts, and a timer's or a re-enrolment's probe has
no job at all. On 2026-09-27 four probes on the release line were quarantined on
censuses `ps` could not complete, and each held its lane for the life of the
daemon. Now `kill <job>` resolves a job's quarantined probes, `lanes
release-probe <lane|holder>` resolves any probe, `--confirm-dead` releases only
on a verified-empty census, and `--force-release` releases only with the
operator's override recorded in `events`.

These tests drive the daemon in-process through PR #50's fixtures: a fake
monotonic clock, the census and the guardian identity check replaced by
counters, and every signal forbidden (nothing recorded is alive, and neither
resolution may signal).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from subfleet import cli, daemon as daemon_module, procs, protocol, render
from subfleet.adapters import registry
from subfleet.contracts import Exit
from subfleet.daemon import PROBE_RESOLUTION_KINDS, Daemon, after, utcnow
from subfleet.offline import Offline
from tests.fake.conftest import Harness
from tests.fake.test_probe_quarantine_pacing import (HOLDER, OTHER, SURVIVOR, UNVERIFIABLE, World, census,
                                                     started_probe, submitted)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401 - fixture
from tests.fake_adapter import FakeAdapter

EMPTY = procs.Containment()
NOT_VERIFIABLE = census(errors=UNVERIFIABLE)


# --- fixtures ------------------------------------------------------------------

def second_lane(service, lane_id: str = "codex-2") -> str:
    lane = service.store.get_lane("codex-1")
    service.store.put_lane(dataclasses.replace(lane, lane_id=lane_id))
    return lane_id


def started_turn(service, kind: str, lanes: tuple[str, ...] = ("codex-1",)) -> str:
    """A timer's (`keepalive`, `probe`) or a re-enrolment's (`enroll`) probe whose
    guardian started and exited, lease(s) held, holder no longer in a turn: what
    `_recover_probes` owns once the turn has returned."""
    holder = ("probe:timer:enroll:" if kind == "enroll" else "probe:timer:") + str(uuid4())
    directory = service.root / "lanes" / lanes[0] / "probes" / holder.rsplit(":", 1)[-1]
    directory.mkdir(parents=True)
    record = {"holder": holder, "job_id": None, "lane_id": lanes[0], "timer_kind": kind,
              "model_id": "enrollment" if kind == "enroll" else "gpt-6-terra",
              "directory": str(directory), "state": "starting", "created_at": utcnow(),
              "deadline_at": after(60), "guardian_pid": 900101, "pgid": 900101,
              "boot_id": "fixture-boot", "proc_start": "fixture-start", "owned_identities": {}}
    for lane in lanes:
        assert service.store.acquire_lease(f"lane:{lane}:slot:0", holder)
    service._save_probe(record)
    return holder


def quarantined(service, world: World, holder: str) -> None:
    """The look that quarantines it: the census finds a survivor after the kill protocol."""
    service.term_grace_s = 0
    world.table = census(SURVIVOR)
    service._recover_probes()
    assert service._probe_record(holder)["state"] == "quarantined"
    assert service.store.list_leases(holder)


def admission_probe(service, harness, monkeypatch) -> tuple[str, World]:
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, HOLDER)
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    return job_id, world


def kinds(service, kind: str) -> list[dict]:
    """The events of `kind` that name a holder (an insert's empty audit twin does not)."""
    rows = []
    for row in service.store.query("SELECT * FROM events WHERE kind=? ORDER BY event_id", (kind,)):
        data = json.loads(row["data_json"])
        if data.get("holder"):
            rows.append({**row, "data": data})
    return rows


def kill(service, job_id: str, **flags) -> dict:
    return service.dispatch("kill", {"job_id": job_id, **flags})


def release_probe(service, target: str, **flags) -> dict:
    return service.dispatch("lanes", {"action": "release-probe", "lane_id": target, **flags})["release_probe"]


def records(service, holder: str) -> list[dict]:
    return [json.loads(row["data_json"]) for row in service.store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id")
        if json.loads(row["data_json"]).get("holder") == holder]


# --- --confirm-dead -----------------------------------------------------------------

def test_c5_7a_kill_confirm_dead_releases_a_probe_on_a_verified_empty_census(routing_state, monkeypatch):
    """The request is recorded, not acted on, by the handler (C-16.4: no census
    there); the next pass takes the census whatever the recheck clock says, and a
    verified-empty one finishes the probe as `_finish_probe` does, with an
    `unknown` outcome, in one `probe.confirmed_dead` transaction."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    directory = Path(service._probe_record(HOLDER)["directory"])
    readings = len(service.store.list_readings())
    world.table = EMPTY                                   # the operator checked: gone
    censuses = len(world.censuses)
    answer = kill(service, job_id, confirm_dead=True, operator_note="pids 900003 gone (checked)")
    assert answer["status"] == "resolution requested"
    assert [probe["holder"] for probe in answer["probes"]] == [HOLDER]
    assert HOLDER in answer["detail"] and "--confirm-dead" in answer["detail"]
    assert len(world.censuses) == censuses, "a request handler takes no census (C-16.4)"
    assert service.store.list_leases(HOLDER), "nothing is released until the census says so"
    assert world.now < service._probe_rechecks[HOLDER][1], "the clock is not due"
    service._recover_probes()
    assert len(world.censuses) == censuses + 1, "one census, now, not when the clock is due"
    assert not service.store.list_leases(HOLDER)
    record = service._probe_record(HOLDER)
    assert record["state"] == "completed" and record["outcome"]["cls"] == "unknown"
    [confirmed] = kinds(service, "probe.confirmed_dead")
    assert confirmed["job_id"] == job_id and confirmed["lane_id"] == "codex-1"
    assert confirmed["data"]["operator_note"] == "pids 900003 gone (checked)"
    assert confirmed["data"]["class"] == "unknown" and confirmed["data"]["via"] == "kill"
    assert confirmed["data"]["containment"]["live_pids"] == [] and not confirmed["data"]["containment"]["unverifiable"]
    assert not kinds(service, "probe.force_released") and not kinds(service, "probe.still_live")
    job = service.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity")
    assert len(service.store.list_readings()) == readings, "an unknown outcome admits nothing and closes nothing"
    assert not directory.exists()
    assert HOLDER not in service._probe_rechecks and HOLDER not in service._probe_resolutions
    looks = len(world.looks)
    service._recover_probes()
    assert len(world.looks) == looks, "a released probe is never looked at again"


@pytest.mark.parametrize("table", [census(SURVIVOR), census(OTHER, SURVIVOR), NOT_VERIFIABLE],
                         ids=["the-survivor", "another-survivor", "unverifiable"])
def test_c5_7a_confirm_dead_that_finds_it_live_keeps_the_lease_and_records_the_look(routing_state, monkeypatch, table):
    """Anything but verified empty, an unverifiable census included, leaves it
    quarantined; the look is recorded as `probe.still_live` for the operator,
    and backs the recheck clock off like any look."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    world.table = table
    looks_before = service._probe_rechecks[HOLDER][0]
    kill(service, job_id, confirm_dead=True, operator_note="looked")
    service._recover_probes()
    assert service.store.list_leases(HOLDER)
    assert service._probe_record(HOLDER)["state"] == "quarantined"
    [look] = kinds(service, "probe.still_live")
    assert look["job_id"] == job_id and look["data"]["operator_note"] == "looked"
    assert look["data"]["containment"] == table.to_dict()
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"
    assert service._probe_rechecks[HOLDER][0] == looks_before + 1
    assert not kinds(service, "probe.confirmed_dead")
    assert HOLDER not in service._probe_resolutions, "one request, one look"


# --- --force-release ----------------------------------------------------------------

def test_c5_7a_force_release_releases_without_a_census_or_a_signal(routing_state, monkeypatch):
    """The override is for a census that cannot complete: it takes none, sends no
    signal (World forbids both signal calls), and records the override with the
    last look's evidence. The record says `released`, never `completed`, and the
    directory stays, since a survivor may still be running in it."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    directory = Path(service._probe_record(HOLDER)["directory"])
    world.table = NOT_VERIFIABLE                     # what `ps` would say now, if asked
    censuses, looks = len(world.censuses), len(world.looks)
    answer = kill(service, job_id, force_release=True, operator_note="ps is failing; lane needed")
    assert answer["status"] == "resolution requested" and "--force-release" in answer["detail"]
    service._recover_probes()
    assert (len(world.censuses), len(world.looks)) == (censuses, looks), "no census, no look"
    assert not service.store.list_leases(HOLDER)
    record = service._probe_record(HOLDER)
    assert record["state"] == "released" and record["override"] is True
    [override] = kinds(service, "probe.force_released")
    assert override["job_id"] == job_id and override["data"]["override"] is True
    assert override["data"]["operator_note"] == "ps is failing; lane needed"
    assert override["data"]["containment"]["live_pids"] == [SURVIVOR], "the last recorded look's evidence"
    assert not kinds(service, "probe.confirmed_dead")
    job = service.store.get_job(job_id)
    assert (job["state"], job["wait_reason"]) == ("waiting", "capacity")
    assert directory.is_dir()
    service._recover_probes()
    assert len(world.looks) == looks


def test_c5_7a_the_latest_request_wins(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, confirm_dead=True)
    kill(service, job_id, force_release=True, operator_note="changed my mind")
    service._recover_probes()
    assert [row["data"]["operator_note"] for row in kinds(service, "probe.force_released")] == ["changed my mind"]
    assert not kinds(service, "probe.still_live")


# --- naming the probe ----------------------------------------------------------------

def test_c5_7a_a_plain_kill_leaves_the_probe_and_a_resolution_still_reaches_it(routing_state, monkeypatch):
    """Cancelling the job does not release its probe's slot (only C-5.7a's two
    ends do); the finished job can still be named to resolve it."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    assert kill(service, job_id)["status"] == "cancel requested"
    service._recover_probes()
    assert service.store.get_job(job_id)["state"] == "cancelled"
    assert service.store.list_leases(HOLDER)
    world.table = EMPTY
    assert kill(service, job_id, confirm_dead=True)["status"] == "resolution requested"
    service._recover_probes()
    assert not service.store.list_leases(HOLDER)
    assert service.store.get_job(job_id)["state"] == "cancelled", "a finished job stays finished"


def test_c5_7a_a_job_with_nothing_quarantined_answers_as_before(routing_state, monkeypatch):
    service, harness = routing_state
    job_id = submitted(service, harness)
    assert kill(service, job_id, confirm_dead=True) == {"job_id": job_id, "probes": [], "status": "not quarantined"}
    started_probe(service, job_id)                        # starting, not quarantined
    assert kill(service, job_id, force_release=True)["status"] == "not quarantined"
    assert not service._probe_resolutions


@pytest.mark.parametrize("kind", ["keepalive", "probe", "enroll"])
def test_c5_7a_lanes_release_probe_names_a_turn_by_its_lane(routing_state, monkeypatch, kind):
    """A timer's or a re-enrolment's probe has no job: its lane names it."""
    service, _ = routing_state
    holder = started_turn(service, kind)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    directory = Path(service._probe_record(holder)["directory"])
    world.table = EMPTY
    answer = release_probe(service, "codex-1", operator_note="checked")
    assert answer["status"] == "resolution requested" and answer["holder"] == holder
    assert answer["mode"] == "confirm-dead" and answer["kind"] == kind and answer["job_id"] is None
    service._recover_probes()
    assert not service.store.list_leases(holder)
    assert service._probe_record(holder)["state"] == "completed"
    [confirmed] = kinds(service, "probe.confirmed_dead")
    assert confirmed["job_id"] is None and confirmed["lane_id"] == "codex-1"
    assert confirmed["data"]["holder"] == holder and confirmed["data"]["via"] == "lanes release-probe"
    assert not directory.exists()


def test_c5_7a_lanes_release_probe_names_a_probe_by_its_holder(routing_state, monkeypatch):
    service, _ = routing_state
    holder = started_turn(service, "keepalive")
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    answer = release_probe(service, holder, force_release=True, operator_note="override")
    assert answer["status"] == "resolution requested" and answer["mode"] == "force-release"
    service._recover_probes()
    assert not service.store.list_leases(holder)
    assert [row["data"]["holder"] for row in kinds(service, "probe.force_released")] == [holder]
    # Asked again (C-16.3: an answer lost the first time), it finds nothing to resolve.
    assert release_probe(service, holder, force_release=True)["status"] == "no probe"
    assert release_probe(service, "codex-1")["status"] == "no probe"


def test_c5_7a_a_re_enrolment_probe_is_looked_at_once_and_releases_every_binding(routing_state, monkeypatch):
    """A re-enrolment's holder fences every binding of its account. A pass looks
    at a holder once, not once per lease: the first look that released it used
    to be followed by another census through its second lease, and on a force
    release that second look would have run the kill protocol on its survivors."""
    service, _ = routing_state
    other = second_lane(service)
    holder = started_turn(service, "enroll", ("codex-1", other))
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    assert len(world.looks) == 1, "one look for two leases"
    assert release_probe(service, other, force_release=True)["lane_ids"] == ["codex-1", other]
    looks = len(world.looks)
    service._recover_probes()
    assert len(world.looks) == looks, "a force release takes no census, through either lease"
    assert not service.store.list_leases(holder)
    assert records(service, holder)[-1]["state"] == "released"
    assert len(kinds(service, "probe.force_released")) == 1


def test_c5_7a_a_verified_empty_look_at_a_re_enrolment_probe_is_one_look(routing_state, monkeypatch):
    service, _ = routing_state
    holder = started_turn(service, "enroll", ("codex-1", second_lane(service)))
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    world.table, world.now = EMPTY, 1
    service._recover_probes()
    assert len(world.looks) == 2 and not service.store.list_leases(holder)
    assert [record["state"] for record in records(service, holder)][-2:] == ["contained", "completed"]


def test_c5_7a_a_request_asked_during_a_look_waits_for_the_next_pass(routing_state, monkeypatch):
    """A pass looks at each holder once (C-5.7a), and that includes a request
    asked while its look runs: a re-enrolment's holder has two leases, and a pass
    that walked leases rather than holders would take the new request through the
    second one and census the probe twice in one pass."""
    service, _ = routing_state
    holder = started_turn(service, "enroll", ("codex-1", second_lane(service)))
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    world.now += 61                                       # the holder's clock is due
    look = service._probe_census
    asked = []

    def census_then_ask(record):
        found = look(record)
        if not asked:
            asked.append(release_probe(service, holder, confirm_dead=True))
        return found
    monkeypatch.setattr(service, "_probe_census", census_then_ask)
    censuses = len(world.censuses)
    service._recover_probes()
    assert asked[0]["status"] == "resolution requested"
    assert len(world.censuses) == censuses + 1, "one look per holder per pass"
    assert service._probe_resolutions[holder]["requests"][0]["mode"] == "confirm-dead"
    service._recover_probes()
    assert len(world.censuses) == censuses + 2 and holder not in service._probe_resolutions
    assert len(kinds(service, "probe.still_live")) == 1 and service.store.list_leases(holder)


def test_c5_7a_confirm_dead_never_signals_a_live_guardian(routing_state, monkeypatch):
    """I2 with the guardian still alive. A quarantined record is looked at with a
    census and nothing else, whoever asks: the pass's own look and an operator's
    `--confirm-dead` must not run the kill protocol even when the recorded leader
    is still the recorded process and its survivors are owned (signals raise)."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    record = service._probe_record(HOLDER)
    service._save_probe({**record, "owned_identities": {
        str(pid): dataclasses.asdict(procs.ProcessIdentity(pid, "fixture-boot", "fixture-start"))
        for pid in world.table.live_pids}})
    monkeypatch.setattr(procs, "same_process", lambda *args: True)   # the guardian is alive
    world.now += 61
    service._recover_probes()                                         # the pass's own look
    kill(service, job_id, confirm_dead=True)
    service._recover_probes()                                         # the operator's
    assert [event["data"]["holder"] for event in kinds(service, "probe.still_live")] == [HOLDER]
    assert service._probe_record(HOLDER)["state"] == "quarantined" and service.store.list_leases(HOLDER)


def test_c5_7a_a_timer_turn_gives_its_lease_back_before_it_lets_the_holder_go(routing_state, monkeypatch):
    """`Timers._release` releases while the holder is still the turn's, so a pass
    never looks at a lease on its way out (C-5.7a)."""
    service, _ = routing_state
    holder = "probe:timer:" + str(uuid4())
    assert service.store.acquire_lease("lane:codex-1:slot:0", holder)
    service.timers.active_holders.add(holder)
    seen = []
    release = service.store.release_leases

    def watched(name, **kwargs):
        seen.append(name in service.timers.active_holders)
        return release(name, **kwargs)
    monkeypatch.setattr(service.store, "release_leases", watched)
    service.timers._release(holder)
    assert seen == [True]
    assert holder not in service.timers.active_holders and not service.store.list_leases(holder)


def test_c5_7a_release_probe_answers(routing_state, monkeypatch):
    service, harness = routing_state
    with pytest.raises(protocol.ProtocolError) as unknown:
        release_probe(service, "codex-9")
    assert unknown.value.code == Exit.INVALID_INPUT
    with pytest.raises(protocol.ProtocolError) as never:
        release_probe(service, "probe:never-recorded")
    assert never.value.code == Exit.INVALID_INPUT
    with pytest.raises(protocol.ProtocolError):
        release_probe(service, " ")
    assert release_probe(service, "codex-1") == {"lane_id": "codex-1", "holder": None, "status": "no probe"}
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    answer = release_probe(service, "codex-1")
    assert (answer["status"], answer["state"], answer["holder"]) == ("not quarantined", "starting", HOLDER)
    assert not service._probe_resolutions


# --- serialization with the thread that looks -----------------------------------------

def test_c5_7a_a_request_waits_while_a_turn_owns_its_probe(routing_state, monkeypatch):
    """A holder still in a timer turn or an enrolment (`timers.active_holders`) is
    that turn's; the request is acted on once the turn has let it go."""
    service, _ = routing_state
    holder = started_turn(service, "probe")
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    world.table = EMPTY
    release_probe(service, "codex-1")
    service.timers.active_holders.add(holder)
    looks = len(world.looks)
    service._recover_probes()
    assert len(world.looks) == looks and holder in service._probe_resolutions
    service.timers.active_holders.discard(holder)
    service._recover_probes()
    assert not service.store.list_leases(holder)


def test_c5_7a_a_request_for_a_probe_released_another_way_is_dropped(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True)
    service.store.release_leases(HOLDER)
    service._recover_probes()
    assert not service._probe_resolutions and not kinds(service, "probe.force_released")


def test_c5_7a_a_request_for_a_probe_contained_since_is_dropped(routing_state, monkeypatch):
    """The record is read again when the request is acted on: a probe that is no
    longer quarantined has nothing for an operator to resolve."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True)
    service._save_probe({**service._probe_record(HOLDER), "state": "contained"})
    service._recover_probes()
    assert service.store.list_leases(HOLDER) and not kinds(service, "probe.force_released")


def test_c5_7a_a_restart_forgets_a_request_and_asking_again_is_safe(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True)
    service.close()
    fresh = Daemon(service.root)
    try:
        again = World(fresh, monkeypatch, census(SURVIVOR))
        fresh._recover_probes()
        assert fresh.store.list_leases(HOLDER), "the request did not survive the restart"
        assert kill(fresh, job_id, force_release=True)["status"] == "resolution requested"
        fresh._recover_probes()
        assert not fresh.store.list_leases(HOLDER)
        assert len(kinds(fresh, "probe.force_released")) == 1
        assert again.looks == [0.0], "the restart's own first look, and none for the override"
    finally:
        fresh.close()


# --- what the operator sees ---------------------------------------------------------

def test_c5_7a_show_why_status_and_lanes_name_the_probe_and_how_to_resolve_it(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    commands = [f"subfleet kill {job_id} --confirm-dead", f"subfleet kill {job_id} --force-release"]
    shown = service.dispatch("show", {"job_id": job_id})
    [probe] = shown["probes"]
    assert (probe["holder"], probe["lane_ids"], probe["state"], probe["kind"]) == (HOLDER, ["codex-1"], "quarantined", "admission")
    assert probe["live_pids"] == [SURVIVOR] and probe["resolve"] == commands
    assert probe["recorded_at"] and probe["looks"] == 1 and probe["next_look_in_s"] == 1.0
    assert shown["probe_resolutions"] == []
    why = service.dispatch("why", {"job_id": job_id})
    assert why["job"]["probes"] == [probe]
    assert f"probe {HOLDER} (job {job_id}) holds codex-1: quarantined" in why["text"]
    assert commands[0] in why["text"] and commands[1] in why["text"]
    status = service.dispatch("daemon.status", {})
    assert status["probes"] == [probe]
    text = cli.format_status(status)
    assert "probes" in text.splitlines() and commands[0] in text
    assert service.dispatch("lanes", {})["probes"] == [probe]
    # A pending request, then the operator's look that found it live.
    kill(service, job_id, confirm_dead=True, operator_note="n")
    requested = service.dispatch("show", {"job_id": job_id})["probes"][0]["requested"]
    assert requested["mode"] == "confirm-dead" and requested["via"] == "kill"
    service._recover_probes()
    probe = service.dispatch("show", {"job_id": job_id})["probes"][0]
    assert probe["requested"] is None and probe["operator_look"]["containment"]["live_pids"] == [SURVIVOR]
    assert "operator's last --confirm-dead" in "\n".join(render.probe_lines(probe))
    resolutions = service.dispatch("show", {"job_id": job_id})["probe_resolutions"]
    assert [(row["event"], row["holder"], row["operator_note"]) for row in resolutions] == [
        ("probe.still_live", HOLDER, "n")]
    # Released, it is gone from every view, and `show` keeps what was done.
    kill(service, job_id, force_release=True, operator_note="o")
    service._recover_probes()
    assert service.dispatch("show", {"job_id": job_id})["probes"] == []
    assert [row["event"] for row in service.dispatch("show", {"job_id": job_id})["probe_resolutions"]] == [
        "probe.still_live", "probe.force_released"]
    assert service.dispatch("daemon.status", {})["probes"] == []
    assert set(PROBE_RESOLUTION_KINDS) >= {row["event"] for row in resolutions}


def test_c5_7a_a_turns_probe_is_resolved_through_its_lane(routing_state, monkeypatch):
    service, _ = routing_state
    holder = started_turn(service, "keepalive")
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    [probe] = service.dispatch("daemon.status", {})["probes"]
    assert probe["resolve"] == ["subfleet lanes release-probe codex-1 --confirm-dead",
                                "subfleet lanes release-probe codex-1 --force-release"]
    assert f"probe {holder} (keepalive turn) holds codex-1: quarantined" in cli.format_status({"probes": [probe]})


def test_c5_7a_a_job_retention_has_pruned_still_names_its_probe(routing_state, monkeypatch):
    """The probes are found by their records, not through the job row."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    with service.store.transaction("test.pruned") as tx:
        tx.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    [probe] = service._probe_rows()
    assert probe["job_id"] == job_id and probe["resolve"][1] == f"subfleet kill {job_id} --force-release"
    assert kill(service, job_id, force_release=True)["status"] == "resolution requested"
    service._recover_probes()
    assert not service.store.list_leases(HOLDER)
    with pytest.raises(protocol.ProtocolError):
        kill(service, job_id, force_release=True)           # nothing left that names it


def test_c5_7a_offline_status_and_show_list_the_same_probes(routing_state, monkeypatch):
    """C-17.5: with the daemon down, `status` and `runs show` read the same rows
    from the store (all but the clock and a pending request, which only a
    running daemon has)."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    started_turn(service, "keepalive", (second_lane(service),))       # one row with a job, one without
    kill(service, job_id, confirm_dead=True, operator_note="looked")
    service._recover_probes()                           # a `probe.still_live`: the operator's last look
    online = service._probe_rows()
    shared = ("holder", "lane_id", "lane_ids", "job_id", "kind", "state", "created_at", "recorded_at",
              "live_pids", "unverifiable", "errors", "containment", "operator_look", "resolve")
    offline = Offline(service.root)
    assert [{key: row[key] for key in shared} for row in offline.status()["probes"]] == \
        [{key: row[key] for key in shared} for row in online]
    assert len(online) == 2 and online[0]["operator_look"]["operator_note"] == "looked"
    shown = offline.show_job(job_id)
    assert [row["holder"] for row in shown["probes"]] == [HOLDER]
    assert shown["probe_resolutions"] == service.dispatch("show", {"job_id": job_id})["probe_resolutions"]
    assert [event["event"] for event in shown["probe_resolutions"]] == ["probe.still_live"]


# --- the design review's findings (2026-09-27) -----------------------------------------

LONG_AGO = "2000-01-01T00:00:00Z"


def test_c5_7a_a_resolution_reaches_only_probes_that_existed_when_it_was_issued(routing_state, monkeypatch):
    """A force release sent again after its answer was lost (C-16.3), or a
    command repeated, must not reach a probe the job started since: the job of a
    released probe is admitted again and, on the same failing census, its next
    probe is quarantined too. `issued_at` is minted once per command."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    answer = kill(service, job_id, force_release=True, issued_at=LONG_AGO)
    assert answer["status"] == "not quarantined" and answer["probes"] == []
    assert [row["holder"] for row in answer["newer_probes"]] == [HOLDER]
    assert f"subfleet lanes release-probe {HOLDER}" in answer["detail"]
    assert release_probe(service, "codex-1", force_release=True, issued_at=LONG_AGO)["status"] == "newer probe"
    assert not service._probe_resolutions
    service._recover_probes()
    assert service.store.list_leases(HOLDER)
    # Named by its holder, it is the probe the operator means, whenever asked.
    assert release_probe(service, HOLDER, force_release=True, issued_at=LONG_AGO)["status"] == "resolution requested"
    # And a request issued after it was created reaches it by job.
    assert kill(service, job_id, confirm_dead=True, issued_at=after(5))["probes"][0]["holder"] == HOLDER
    with pytest.raises(protocol.ProtocolError) as refused:
        kill(service, job_id, force_release=True, issued_at="yesterday")
    assert refused.value.code == Exit.INVALID_INPUT


def test_c5_7a_a_later_confirm_dead_does_not_replace_a_pending_force_release(routing_state, monkeypatch):
    """An override an operator was told had been accepted is never quietly
    downgraded, and both requests, with their notes, are kept in the record."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True, operator_note="ps is failing")
    answer = release_probe(service, "codex-1", operator_note="just checking")
    assert answer["mode"] == "force-release" and "does not replace it" in answer["absorbed"]
    assert service.dispatch("show", {"job_id": job_id})["probes"][0]["requested"]["requests"] == 2
    censuses = len(world.censuses)
    service._recover_probes()
    assert len(world.censuses) == censuses and not service.store.list_leases(HOLDER)
    [override] = kinds(service, "probe.force_released")
    assert [(item["mode"], item["operator_note"], item["via"]) for item in override["data"]["requests"]] == [
        ("force-release", "ps is failing", "kill"), ("confirm-dead", "just checking", "lanes release-probe")]


def test_c5_7a_an_escalation_asked_during_the_census_is_acted_on_next(routing_state, monkeypatch):
    """The request is taken before it is acted on: a --force-release asked while
    a --confirm-dead's census is running (seconds, when `ps` is slow) is the next
    pass's, not lost when the first finishes."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    census_now = service._probe_census

    def slow_census(record):
        release_probe(service, "codex-1", force_release=True, operator_note="escalated")
        return census_now(record)
    kill(service, job_id, confirm_dead=True)
    monkeypatch.setattr(service, "_probe_census", slow_census)
    service._recover_probes()
    assert len(kinds(service, "probe.still_live")) == 1 and service.store.list_leases(HOLDER)
    assert service._probe_resolutions[HOLDER]["force_release"] is True
    monkeypatch.setattr(service, "_probe_census", census_now)
    service._recover_probes()
    assert not service.store.list_leases(HOLDER) and len(kinds(service, "probe.force_released")) == 1


def test_c5_7a_a_pass_that_raises_keeps_the_request(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True, operator_note="first")

    def broken(record, resolution):
        release_probe(service, "codex-1", operator_note="asked meanwhile")
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(service, "_force_release_probe", broken)
    with pytest.raises(RuntimeError):
        service._recover_probes()
    pending = service._probe_resolutions[HOLDER]
    assert pending["force_release"] is True and len(pending["requests"]) == 2
    monkeypatch.undo()


def test_c5_7a_a_pass_that_raises_with_nothing_asked_meanwhile_keeps_its_request(routing_state, monkeypatch):
    """The common case of a pass that raises while acting on a request: nothing was
    asked while it ran. The request it took goes back as it was, the pass raises its
    own error for C-5.10 to retry (not a TypeError from putting the request back),
    and the next pass acts on it. (PR #57 put it back by merging it with the None of
    "nothing asked since", which raised and dropped it.)"""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True, operator_note="only")
    asked = dict(service._probe_resolutions[HOLDER])

    def broken(record, resolution):
        raise RuntimeError("store unavailable")
    service._force_release_probe = broken              # the instance's, removed below
    try:
        with pytest.raises(RuntimeError, match="store unavailable"):
            service._recover_probes()
    finally:
        del service._force_release_probe
    assert service._probe_resolutions[HOLDER] == asked
    assert service.store.list_leases(HOLDER) and not kinds(service, "probe.force_released")
    service._recover_probes()
    [forced] = kinds(service, "probe.force_released")
    assert forced["data"]["operator_note"] == "only" and len(forced["data"]["requests"]) == 1
    assert not service.store.list_leases(HOLDER) and HOLDER not in service._probe_resolutions


def test_c5_7a_a_pass_whose_lease_check_raises_keeps_the_request(routing_state, monkeypatch):
    """The pass takes the request before it reads the lease again, and a store
    error there has not acted on it either: the request stays for the next pass."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True, operator_note="first")
    taken = dict(service._probe_resolutions[HOLDER])
    one = service.store.one

    def locked(sql, params=()):
        if sql == LEASE_CHECK:
            raise sqlite3.OperationalError("database is locked")
        return one(sql, params)
    monkeypatch.setattr(service.store, "one", locked)
    with pytest.raises(sqlite3.OperationalError):
        service._recover_probes()
    assert service._probe_resolutions[HOLDER] == taken and service.store.list_leases(HOLDER)
    monkeypatch.setattr(service.store, "one", one)
    service._recover_probes()
    assert not service.store.list_leases(HOLDER) and len(kinds(service, "probe.force_released")) == 1


#: The lease `_recover_probes` reads again after it takes a request.
LEASE_CHECK = "SELECT 1 FROM leases WHERE holder=?"


#: One operator request as `_request_probe_resolution` builds it.
REQUEST = st.builds(
    lambda force, note, at, via: {"force_release": force, "operator_note": note, "requested_at": at, "via": via,
                                  "requests": [{"mode": "force-release" if force else "confirm-dead",
                                                "operator_note": note, "at": at, "via": via}]},
    st.booleans(), st.one_of(st.none(), st.sampled_from(["a", "b", ""])),
    st.sampled_from(["2026-09-29T00:00:0%dZ" % n for n in range(5)]), st.sampled_from(["kill", "lanes release-probe"]))


@settings(max_examples=300, deadline=None)
@given(requests=st.lists(st.one_of(st.none(), REQUEST), max_size=6), split=st.integers(0, 6))
def test_c5_7a_merged_requests_keep_every_request_and_any_override(requests, split):
    """`merge_probe_requests`, folded over any sequence of requests with missing
    ones (None: nothing asked, or nothing taken) anywhere in it:

    - a `--force-release` asked at any point is kept (never downgraded);
    - every request is kept, in the order asked;
    - the note is the last one given, and the time and route the last request's;
    - nothing merged is nothing (None), and grouping does not matter
      (associative), so a request put back by a pass that raised merges with
      whatever was asked meanwhile the same as if it had never been taken.
    """
    merge = daemon_module.merge_probe_requests
    merged = None
    for request in requests:
        merged = merge(merged, request)
    present = [request for request in requests if request]
    if not present:
        assert merged is None
        return
    assert merged["force_release"] is any(request["force_release"] for request in present)
    assert merged["requests"] == [entry for request in present for entry in request["requests"]]
    notes = [request["operator_note"] for request in present if request["operator_note"] is not None]
    assert merged["operator_note"] == (notes[-1] if notes else None)
    assert (merged["requested_at"], merged["via"]) == (present[-1]["requested_at"], present[-1]["via"])
    left = right = None
    for request in requests[:split]:
        left = merge(left, request)
    for request in requests[split:]:
        right = merge(right, request)
    assert merge(left, right) == merged


def test_c5_7a_why_lists_fleet_probes_only_for_the_pool_they_count_toward(routing_state, monkeypatch):
    """On this line a probe's lease counts toward the detached pool's
    `max_active_attempts` (scheduler `reserved_probes`); a turn's fleet cap is
    `max_active_turns`, over turns alone (C-26.9), so `why` for a turn held
    `fleet-full` lists no probes as filling it."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    turn = submitted(service, harness)
    with service.store.transaction("test.turn", job_id=turn) as tx:
        tx.execute("UPDATE jobs SET kind='turn',name=? WHERE job_id=?", ("turn-conversation-1", turn))
    service._holds = {turn: {"reason": "fleet-full", "max_active_attempts": 4}}
    why = service.dispatch("why", {"job_id": turn})
    assert why["job"]["fleet_probes"] == []
    assert "each counts toward max_active_attempts" not in why["text"]


def test_c5_7a_probe_rows_are_one_committed_state(routing_state, monkeypatch):
    """C-3.7: `_probe_rows` reads a row's lease, record and operator look in one
    snapshot. A release another thread commits between those reads is not half
    seen: the row is the probe as it was when the rows were read (quarantined,
    holding its lane), and the next read sees it gone."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    real = service._probe_record
    releaser = []

    def release_elsewhere():
        record = real(HOLDER)
        with service.store.transaction("test.release", job_id=job_id) as tx:
            service._save_probe({**record, "state": "released", "override": True})
            tx.execute("DELETE FROM leases WHERE holder=?", (HOLDER,))

    def release_then_record(holder):
        # After the leases were read, before the record is: the release commits
        # (or, on a store without read connections, waits for the snapshot's lock).
        if not releaser:
            thread = threading.Thread(target=release_elsewhere)
            releaser.append(thread)
            thread.start()
            thread.join(0.5)
        return real(holder)

    monkeypatch.setattr(service, "_probe_record", release_then_record)
    [row] = service._probe_rows()
    releaser[0].join(10)
    assert not releaser[0].is_alive()
    assert (row["holder"], row["state"], row["lane_ids"]) == (HOLDER, "quarantined", ["codex-1"])
    assert row["resolve"] == render.probe_resolutions("codex-1", job_id)
    assert service._probe_rows() == []


def test_c5_7a_a_confirm_dead_whose_finish_raises_stays_quarantined_and_keeps_the_request(routing_state, monkeypatch):
    """A verified-empty census on a `--confirm-dead` finishes the probe in one
    `probe.confirmed_dead` transaction. If that transaction fails, nothing was
    written ahead of it: the record still says quarantined (not `contained`), the
    lease is held and the request kept, and the next pass finishes it as the
    operator's (`probe.confirmed_dead`, with the note), never as an ordinary
    look's `probe.completed` with an outcome read from the old receipt."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, confirm_dead=True, operator_note="checked")
    world.table = EMPTY

    def broken(record, outcome, **kwargs):
        raise RuntimeError("store unavailable")
    service._finish_probe = broken                     # the instance's, removed below
    try:
        with pytest.raises(RuntimeError, match="store unavailable"):
            service._recover_probes()
    finally:
        del service._finish_probe
    assert service._probe_record(HOLDER)["state"] == "quarantined"
    assert service.store.list_leases(HOLDER)
    assert service._probe_resolutions[HOLDER]["operator_note"] == "checked"
    service._recover_probes()
    [confirmed] = kinds(service, "probe.confirmed_dead")
    assert confirmed["data"]["operator_note"] == "checked" and not service.store.list_leases(HOLDER)
    assert not service.store.query("SELECT 1 FROM events WHERE kind='probe.completed'")
    assert [record["state"] for record in records(service, HOLDER)][-1] == "completed"


def test_c5_7a_the_receipt_is_kept_with_a_confirm_dead(routing_state, monkeypatch):
    """The finish records an `unknown` outcome (C-5.7a), and the directory goes,
    so what the turn's receipt said is kept in the event."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    world.table = EMPTY
    kill(service, job_id, confirm_dead=True)
    service._recover_probes()
    [confirmed] = kinds(service, "probe.confirmed_dead")
    assert confirmed["data"]["receipt"] == {"rc": 0, "signal": None, "wall_s": .1, "child_pid": 900002,
                                            "spawn_error": None}


def test_c5_7a_a_force_released_job_keeps_its_probe_clock(routing_state, monkeypatch):
    """C-6.10: an inconclusive probe's job waits its own 60 s before it is looked
    at again, and survivors may still be running on the lane just given back."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    kill(service, job_id, force_release=True)
    service._recover_probes()
    job = service.store.get_job(job_id)
    assert job["wait_reason"] == "capacity"
    assert after(50) <= job["next_check_at"] <= after(70)


def test_c5_7a_a_pass_never_looks_at_a_lease_released_since_it_read_the_leases(routing_state, monkeypatch):
    """A turn's lease can go between the pass's read of the leases and its look;
    the pass reads again before it acts, so it neither looks nor acts on a request."""
    service, _ = routing_state
    holder = started_turn(service, "keepalive")
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantined(service, world, holder)
    stale = service.store.query("SELECT * FROM leases WHERE holder LIKE 'probe:%'")
    release_probe(service, "codex-1", force_release=True)
    service.store.release_leases(holder)
    query = service.store.query
    monkeypatch.setattr(service.store, "query", lambda sql, params=(): stale
                        if sql == "SELECT * FROM leases WHERE holder LIKE 'probe:%'" else query(sql, params))
    looks = len(world.looks)
    service._recover_probes()
    assert len(world.looks) == looks and not kinds(service, "probe.force_released")


@pytest.mark.parametrize("state,released", [("quarantined", False), ("starting", False), ("containing", False),
                                            ("contained", True), ("completed", True), ("reserved", True)])
def test_c5_7a_a_turns_release_asks_the_record(routing_state, monkeypatch, state, released):
    """A turn that raised after its probe was quarantined never reported it; the
    record, not the turn's flag, decides whether its lease goes. The holder is let
    go after the lease, so `_recover_probes` takes over a lease that stays."""
    service, _ = routing_state
    holder = started_turn(service, "keepalive")
    service._save_probe({**service._probe_record(holder), "state": state})
    service.timers.active_holders.add(holder)
    order = []
    release = service.store.release_leases
    monkeypatch.setattr(service.store, "release_leases",
                        lambda h, **kw: (order.append(("release", holder in service.timers.active_holders)),
                                         release(h, **kw))[1])
    service.timers._release(holder, quarantined=False)
    assert bool(service.store.list_leases(holder)) is not released
    assert holder not in service.timers.active_holders
    assert order == ([("release", True)] if released else [])


def test_c5_7a_a_turn_with_no_record_gives_its_lease_back(routing_state):
    service, _ = routing_state
    holder = "probe:timer:" + str(uuid4())
    assert service.store.acquire_lease("lane:codex-1:slot:0", holder)
    service.timers.active_holders.add(holder)
    service.timers._release(holder)
    assert not service.store.list_leases(holder)


def test_c5_7a_why_names_the_probes_that_fill_the_fleet(routing_state, monkeypatch):
    """Each probe lease counts toward max_active_attempts (scheduler
    `reserved_probes`): four quarantined probes filled the fleet on 2026-09-27,
    and `why` for every other job said only that the fleet was full."""
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    other = submitted(service, harness)
    service._holds = {other: {"reason": "fleet-full", "max_active_attempts": 4}}
    why = service.dispatch("why", {"job_id": other})
    assert [row["holder"] for row in why["job"]["fleet_probes"]] == [HOLDER]
    assert "Probes hold 1 lane slot(s), and each counts toward max_active_attempts:" in why["text"]
    assert f"subfleet kill {job_id} --force-release" in why["text"]
    service._holds = {other: {"reason": "behind-older-job", "behind": job_id, "tier": "hard"}}
    assert service.dispatch("why", {"job_id": other})["job"]["fleet_probes"] == []


def test_c5_7a_lanes_release_says_it_released_no_hold_and_names_the_probe(routing_state, monkeypatch):
    service, harness = routing_state
    job_id, world = admission_probe(service, harness, monkeypatch)
    answer = service.dispatch("lanes", {"action": "release", "lane_id": "codex-1"})
    assert answer["released"] == "codex-1" and answer["holds_released"] == 0
    assert answer["probe"] == {"holder": HOLDER, "state": "quarantined"}
    assert service.store.list_leases(HOLDER), "`release` ends holds; it never touches a probe"
    service.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": after(3600)})
    assert service.dispatch("lanes", {"action": "release", "lane_id": "codex-1"})["holds_released"] == 1


# --- the property ----------------------------------------------------------------------

#: What can happen to a quarantined probe, weighted so that most examples reach
#: a release one way or the other and many are still quarantined when they end.
VALUES = {"pass": st.sampled_from([.05, .5, 1, 3, 30, 61]),
          "table": st.sampled_from(["survivor", "other", "empty", "empty", "empty", "unverifiable"]),
          "confirm": st.sampled_from(["kill", "lane", "holder"]),
          "force": st.sampled_from(["kill", "lane", "holder"]),
          "cancel": st.none(), "restart": st.none(), "turn": st.sampled_from([.05, 1, 30]),
          "stale": st.sampled_from(["kill", "lane"]), "turn-ends": st.none(),
          "fault": st.tuples(st.sampled_from(["resolve", "lease", "finish"]), st.sampled_from([.05, 1, 61])),
          "leader": st.booleans()}
OPS = st.lists(st.sampled_from(["pass"] * 5 + ["table"] * 3 + ["confirm"] * 3 + ["force"] * 2
                               + ["cancel", "restart", "turn", "stale", "turn-ends", "fault", "leader"])
               .flatmap(lambda op: st.tuples(st.just(op), VALUES[op])), min_size=12, max_size=50)

TABLES = {"survivor": census(SURVIVOR), "other": census(OTHER), "empty": EMPTY, "unverifiable": NOT_VERIFIABLE}


#: Sequences the property always runs, whatever it draws: a `--confirm-dead`
#: whose census comes back empty and whose finishing transaction fails, then a
#: pass that finishes it; for an admission probe (by job) and a re-enrolment's
#: (by holder, with the guardian alive).
ALWAYS = [("admission", [("confirm", "kill"), ("table", "empty"), ("fault", ("finish", 1)), ("pass", 1)]),
          ("enroll", [("leader", True), ("confirm", "holder"), ("table", "empty"), ("fault", ("finish", 61)),
                      ("pass", 1), ("pass", 61)])]


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@example(kind=ALWAYS[0][0], ops=ALWAYS[0][1])
@example(kind=ALWAYS[1][0], ops=ALWAYS[1][1])
@given(kind=st.sampled_from(["admission", "keepalive", "enroll"]), ops=OPS)
def test_c5_7a_no_path_releases_a_quarantined_probe_but_a_verified_empty_census_or_a_recorded_override(
        tmp_path_factory, kind, ops):
    """For any sequence of passes, process-table changes, operator requests (by
    job, lane or holder), cancels, restarts (the in-memory clock and requests
    forgotten) and passes while a turn still owns the holder, and for each kind
    of probe:

    - I1: the lease goes only in a step whose last census came back verified
      empty, or in a step that recorded a `probe.force_released` naming the
      holder, which only a `--force-release` request produces;
    - I2: nothing is ever signalled (World forbids it), whether or not the
      recorded guardian is still alive and its survivors are recorded as owned;
    - I3: once released it stays released, no later record says quarantined,
      and its job, if still waiting, is not held `uncertain`;
    - I4: a request handler takes no census;
    - a force release issued before the probe existed reaches nothing, and a
      turn's release that reports nothing quarantined never frees it;
    - a pass that raises while acting on a request raises its own error and
      keeps the request as it was (C-5.10 retries the pass);
    - while the lease is held, the record says quarantined.
    """
    root = tmp_path_factory.mktemp("override") / "state"
    root.mkdir()
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = Daemon(root)
        try:
            job_id = None
            if kind == "admission":
                job_id = submitted(service, harness)
                started_probe(service, job_id)
                holder = HOLDER
            else:
                lanes = ("codex-1", second_lane(service)) if kind == "enroll" else ("codex-1",)
                holder = started_turn(service, kind, lanes)
            world = World(service, monkeypatch, census(SURVIVOR))
            quarantined(service, world, holder)
            # `leader` ops make the recorded guardian alive, with the survivors
            # recorded as owned: the kill protocol would have a target to signal.
            leader = {"alive": False}
            monkeypatch.setattr(procs, "same_process", lambda *args: leader["alive"])
            record = service._probe_record(holder)
            service._save_probe({**record, "owned_identities": {
                str(pid): dataclasses.asdict(procs.ProcessIdentity(pid, "fixture-boot", "fixture-start"))
                for pid in (SURVIVOR, OTHER)}})
            forced, released = 0, False
            for op, value in ops:
                held = bool(service.store.list_leases(holder))
                censuses = len(world.censuses)
                overrides = len(kinds(service, "probe.force_released"))
                if op == "pass":
                    world.now += value
                    service._recover_probes()
                elif op == "table":
                    world.table = TABLES[value]
                elif op in ("confirm", "force"):
                    flags = {"force_release": True} if op == "force" else {"confirm_dead": True}
                    if value == "kill" and job_id:
                        kill(service, job_id, **flags)
                    else:
                        release_probe(service, holder if value == "holder" else "codex-1", **flags)
                    assert len(world.censuses) == censuses, "I4: a request handler takes no census"
                    if op == "force" and held:
                        forced += 1
                elif op == "stale":
                    # A force release issued before this probe existed (sent again
                    # after a lost answer, C-16.3): it must reach nothing.
                    if value == "kill" and job_id:
                        kill(service, job_id, force_release=True, issued_at=LONG_AGO)
                    else:
                        release_probe(service, "codex-1", force_release=True, issued_at=LONG_AGO)
                    assert len(world.censuses) == censuses, "I4: a request handler takes no census"
                elif op == "turn-ends":
                    # A turn that raised after quarantining returns no outcome: its
                    # release reports nothing quarantined, and the record decides.
                    service.timers.active_holders.add(holder)
                    service.timers._release(holder, quarantined=False)
                elif op == "cancel" and job_id:
                    kill(service, job_id)
                elif op == "restart":
                    service._probe_rechecks.clear()
                    service._probe_resolutions.clear()
                elif op == "turn":
                    # A pass while a turn still owns the holder (it has quarantined
                    # the probe and not yet returned): the pass must leave it alone.
                    service.timers.active_holders.add(holder)
                    looks = len(world.looks)
                    world.now += value
                    service._recover_probes()
                    service.timers.active_holders.discard(holder)
                    assert len(world.looks) == looks, "a turn's probe is the turn's"
                elif op == "leader":
                    leader["alive"] = value
                elif op == "fault":
                    # The next pass raises after it has taken a pending request: a
                    # store error reading the lease again, while resolving, or in
                    # the transaction that finishes a verified-empty
                    # `--confirm-dead`. It acts on nothing and keeps the request,
                    # and (checked below) the record still says quarantined.
                    where, step = value
                    pending = service._probe_resolutions.get(holder)
                    one = service.store.one

                    def broken(*args, **kwargs):
                        raise RuntimeError("store unavailable")

                    def locked(sql, params=()):
                        if sql == LEASE_CHECK:
                            raise RuntimeError("store unavailable")
                        return one(sql, params)
                    if where == "resolve":
                        service._resolve_probe = broken
                    elif where == "finish":
                        service._finish_probe = broken
                    else:
                        service.store.one = locked
                    world.now += step
                    try:
                        if pending is not None and held and where != "finish":
                            with pytest.raises(RuntimeError, match="store unavailable"):
                                service._recover_probes()
                            assert service._probe_resolutions.get(holder) == pending, "the request is kept"
                        elif pending is not None and held:
                            # Only a --confirm-dead whose census comes back empty
                            # reaches the finish; any other resolution completes.
                            try:
                                service._recover_probes()
                            except RuntimeError:
                                assert service._probe_resolutions.get(holder) == pending, "the request is kept"
                        elif where == "resolve":
                            service._recover_probes()
                    finally:
                        service.__dict__.pop("_resolve_probe", None)
                        service.__dict__.pop("_finish_probe", None)
                        service.store.__dict__.pop("one", None)
                now_held = bool(service.store.list_leases(holder))
                record = service._probe_record(holder)
                new_overrides = len(kinds(service, "probe.force_released")) - overrides
                if released:
                    assert not now_held, "I3: a released lease stays released"
                    assert record["state"] in ("completed", "released"), "I3: nothing resurrects it"
                    assert new_overrides == 0
                    continue
                if held and not now_held:
                    verified = len(world.censuses) > censuses and world.seen[-1].verified_empty
                    assert verified or new_overrides == 1, "I1: released without either end"
                    assert not (verified and new_overrides), "one end or the other, not both"
                    assert record["state"] == ("released" if new_overrides else "completed")
                    if job_id:
                        job = service.store.get_job(job_id)
                        assert not (job["state"] == "waiting" and job["wait_reason"] == "uncertain"), "I3: not stranded"
                    released = True
                    event("released by an override" if new_overrides else "released on a verified-empty census")
                    continue
                assert new_overrides == 0, "I1: an override is recorded only with its release"
                assert now_held and record["state"] == "quarantined"
            assert len(kinds(service, "probe.force_released")) <= forced, "I1: only a --force-release request overrides"
            if not released:
                event("still quarantined")
        finally:
            service.close()
