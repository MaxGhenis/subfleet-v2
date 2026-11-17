"""Probe p6 (daemon half): the release/217 daemon before #309 (82d3dd819), given each
payload the new CLI sends for a sandbox target. Run with PYTHONPATH = the export.
Prints one JSON line per payload: the refusal and whether any row changed."""
import json
import subfleet
from subfleet import protocol
from tests.task_hold_support import fresh_daemon, store_image

assert "old217" in subfleet.__file__, subfleet.__file__
UNTIL = "2099-06-01T00:00:00Z"
PAYLOADS = {
    "hold": {"action": "hold", "task": None, "until": UNTIL, "note": "disk: brake", "sandbox": "workspace-write"},
    "release": {"action": "release", "task": None, "until": None, "note": None, "sandbox": "workspace-write"},
    "hold-if": {"action": "hold-if", "task": None, "until": UNTIL, "note": "disk: brake",
                "sandbox": "workspace-write", "if_note_prefix": "disk:"},
    "release-if": {"action": "release-if", "task": None, "until": None, "note": None,
                   "sandbox": "workspace-write", "if_note_prefix": "disk:"},
}
results = {"subfleet": subfleet.__file__}
with fresh_daemon() as (service, harness, clock, signals):
    # A task hold already in force, so "nothing changed" covers an existing row too.
    service.dispatch("tasks", {"action": "hold", "task": "build", "until": UNTIL, "note": "task brake"})
    for name, payload in PAYLOADS.items():
        before = store_image(service)
        try:
            answer = service.dispatch("tasks", payload)
            outcome = {"answered": answer}
        except protocol.ProtocolError as exc:
            outcome = {"refused": str(exc), "code": int(exc.code), "reason": getattr(exc, "reason", None)}
        outcome["store_unchanged"] = store_image(service) == before
        outcome["sandbox_field_kept"] = "sandbox" in protocol.TasksArgs.__dataclass_fields__
        results[name] = outcome
print(json.dumps(results, indent=1))
