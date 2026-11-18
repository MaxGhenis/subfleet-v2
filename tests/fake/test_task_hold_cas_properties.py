"""C-11.10 CAS: every reply and lease agrees with a separate one-row model."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import HealthCheck, example, given, note, settings, strategies as st

from subfleet import protocol, task_holds
from subfleet.contracts import Exit
from tests.disk_floor_model import stamp
from tests.task_hold_support import fresh_daemon


PREFIXES = {"battery": "battery-backoff:", "automation": "booking-bot:"}
FREE_NOTES = st.one_of(st.sampled_from([
    "operator booking", "x battery-backoff: middle", "Battery-backoff: uppercase", "manual booking-bot: middle",
]), st.text(alphabet="abcdeABCDE :0123456789", min_size=1, max_size=30).filter(lambda value: bool(value.strip(" "))))
STEP = st.tuples(
    st.sampled_from([0, 1, 10, 60, 121]),
    st.sampled_from(["battery", "automation", "operator"]),
    st.sampled_from(["hold", "release"]),
    st.booleans(),
    st.sampled_from([1, 10, 60, 600]),
    FREE_NOTES,
)


@dataclass
class ModelHold:
    until: int
    note: str
    since: int

    def reply(self):
        return {"task": "build", "until": stamp(self.until), "note": self.note, "since": stamp(self.since)}


@settings(max_examples=60, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@example(steps=[
    (0, "battery", "hold", True, 10, "first"),
    (1, "automation", "hold", True, 60, "foreign"),
    (0, "automation", "release", True, 60, "foreign"),
    (9, "automation", "hold", True, 60, "expired"),
    (0, "battery", "release", True, 60, "foreign"),
    (1, "operator", "hold", False, 1, "x battery-backoff: middle"),
    (1, "battery", "release", True, 60, "expired foreign"),
    (0, "battery", "hold", True, 10, "new"),
    (1, "battery", "release", True, 60, "own"),
    (0, "automation", "release", True, 60, "absent"),
])
@given(steps=st.lists(STEP, min_size=1, max_size=45))
def test_sequences_match_one_row_model_for_two_automations_and_operator(steps):
    model = None
    now = 0
    with fresh_daemon() as (service, harness, clock, signals):
        for index, (delta, actor, action, conditional, duration, free_note) in enumerate(steps):
            now += delta
            clock[0] = now
            prefix = PREFIXES.get(actor) if conditional else None
            text = (PREFIXES[actor] + " " + free_note if actor in PREFIXES else free_note).strip(" ")
            args = {"action": action, "task": "build"}
            if action == "hold":
                args.update(until=stamp(now + duration), note=" " + text + " ")
            if prefix is not None:
                args["if_note_prefix"] = prefix
            force = model is not None and model.until > now
            blocked = (prefix is not None and model is not None and not model.note.startswith(prefix)
                       and (action == "release" or force))
            before = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
                      for table in ("leases", "jobs", "events")}
            note({"index": index, "clock": now, "args": args, "model": model})
            if blocked:
                try:
                    service.dispatch("tasks", args)
                except protocol.ProtocolError as exc:
                    assert exc.reason == "task-hold-condition"
                    assert exc.code == Exit.REFUSED
                    assert exc.details == {"task_hold": model.reply()}
                else:
                    raise AssertionError("CAS-model: accepted a foreign hold")
                after = {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid") for table in before}
                assert after == before, "CAS-model: refusal changed state or recorded an event"
            else:
                if action == "hold":
                    replaced = model.reply() if force else None
                    model = ModelHold(now + duration, text, model.since if force else now)
                    expected = {"held": "build", "task_hold": model.reply(), "replaced": replaced,
                                "task_holds": [model.reply()]}
                    changed = True
                else:
                    changed = model is not None
                    expected = {"released": "build", "was_held": force, "woken": 0, "task_holds": []}
                    model = None
                assert service.dispatch("tasks", args) == expected, "CAS-model: reply differs"
                events = service.store.query("SELECT * FROM events ORDER BY rowid")
                assert len(events) == len(before["events"]) + int(changed), "CAS-model: wrong event count"
                if changed:
                    assert events[-1]["kind"] == {"hold": "task.held", "release": "task.released"}[action]
                else:
                    assert {table: service.store.query(f"SELECT * FROM {table} ORDER BY rowid")
                            for table in before} == before, "CAS-model: absent release wrote state"
            expected_rows = ([] if model is None else [{
                "lease_key": "task-hold:build", "holder": task_holds.HOLDER + model.note,
                "acquired_at": stamp(model.since), "expires_at": stamp(model.until),
            }])
            assert service.store.query(task_holds.ROWS_SQL, task_holds.ROWS_PARAMS) == expected_rows, "CAS-model: lease differs"
            assert service.dispatch("tasks", {"action": "list"}) == {
                "task_holds": [model.reply()] if model is not None and model.until > now else []
            }, "CAS-model: listing differs"
