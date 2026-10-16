"""C-25.7: a handler refusal reads its own closed template, by reason, with checked values.

Review r5 of #181 (P1): the generic argument template had taken over every handler
refusal, and with it the job a reused request id names (C-16.3), the `-s read-only`
fix (C-11.1), the isolation variable or layer (C-23.3) and `unknown job <id>`.

Invariants, for every reason, details, exception message and repair text:
- the text is one of the reason's forms, the most specific whose values all pass
  their checks, plus the by-policy suffix exactly when the details carry it;
- a known reason never reads the operation fallback, and an unknown one always does;
- the exception's own message and repair text never reach it;
- every refusal the daemon's code builds names a known reason, and where its own
  message is a constant, the template says the same thing (the two cannot drift).
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module, protocol, refusals
from subfleet.adapters.base import AdapterError
from subfleet.lanes_transfer import TransferError
from tests.unit.test_protocol_secrecy import wire_reply

SOURCE = Path(refusals.__file__).resolve().parent
REFUSAL_TYPES = ("ProtocolError", "AdapterError", "TransferError")
CLIENT_SIDE = ("client.py",)                # the CLI's own errors never cross the wire from the daemon
SENTINEL = "SECRET7-left"
VALID = {
    "job_id": ("20261013-014534-astra", "20260925-000000-none", "20261013-014534-gate-review-2"),
    "lane_id": ("codex-1", "claude-12", "codex-999999999"),
    "task": ("build", "review", "authored-prose"),
    "model": ("opus", "sol61", "gpt-6.1-sol"),
    "count": (0, 2, 999),
    "limit": (1, 20),
}


def forms(reason: str) -> tuple[tuple[str, str | None], ...]:
    return (refusals.HANDLER_MESSAGES[reason], *refusals.LESS_SPECIFIC.get(reason, ()))


def names(form: tuple[str, str | None]) -> tuple[str, ...]:
    return (*refusals.fields(form[0]), *refusals.fields(form[1]))


def constructions():
    """Every refusal the daemon's code builds: (where, call)."""
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name in CLIENT_SIDE:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] in REFUSAL_TYPES:
                yield f"{path.relative_to(SOURCE.parent)}:{node.lineno}", node


def keyword(call: ast.Call, name: str, position: int | None = None) -> ast.expr | None:
    found = {k.arg: k.value for k in call.keywords}
    if name in found:
        return found[name]
    return call.args[position] if position is not None and len(call.args) > position else None


# --- the vocabulary -------------------------------------------------------------

def test_handler_reasons_are_apart_from_request_reasons():
    assert set(refusals.HANDLER_MESSAGES).isdisjoint(protocol.REFUSAL_MESSAGES)
    assert set(refusals.LESS_SPECIFIC) <= set(refusals.HANDLER_MESSAGES)


@pytest.mark.parametrize("reason", sorted(refusals.HANDLER_MESSAGES))
def test_every_reason_ends_in_a_form_that_names_no_value(reason):
    """So a known reason never reads the operation fallback, whatever its details."""
    assert names(forms(reason)[-1]) == ()
    for form in forms(reason):
        assert set(names(form)) <= refusals.value_names(), form
        for text in form:
            assert text is None or ("\n" not in text and 0 < len(text) <= 300), text


def test_every_refusal_the_daemon_builds_names_a_known_reason():
    """A new refusal without a reason would read `operation: request was refused`."""
    missing, unknown = [], []
    for where, call in constructions():
        if where.startswith("subfleet/protocol.py"):
            continue                            # `request_error`: the request vocabulary itself
        value = keyword(call, "reason")
        if value is None:
            missing.append(where)
        elif isinstance(value, ast.Attribute) and value.attr == "reason":
            continue                            # the reason of the refusal it wraps, passed on
        else:
            found = [node.value for node in ast.walk(value)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)]
            # A handler's argument check may name a request reason (its words then stay off the wire).
            unknown += [(where, reason) for reason in found or [ast.unparse(value)]
                        if reason not in refusals.HANDLER_MESSAGES and reason not in protocol.REFUSAL_MESSAGES]
    assert missing == [] and unknown == []


def test_constant_refusal_text_and_its_template_say_the_same_thing():
    """Differential: where a raise site's message or fix is a constant, the wire says it."""
    checked, differ_at = 0, []
    for where, call in constructions():
        reason = keyword(call, "reason")
        if not (isinstance(reason, ast.Constant) and reason.value in refusals.HANDLER_MESSAGES):
            continue
        details = keyword(call, "details")
        try:
            details = ast.literal_eval(details) if details is not None else None
        except ValueError:
            continue                            # a computed value: the property tests cover it
        message, fix = refusals.render(reason.value, details)
        for raised, shown in ((keyword(call, "message", 0), message), (keyword(call, "fix", 2), fix)):
            if isinstance(raised, ast.Constant) and isinstance(raised.value, str):
                checked += 1
                if raised.value != shown:
                    differ_at.append((where, raised.value, shown))
    assert checked > 100 and differ_at == [], differ_at


# --- rendering, as properties ----------------------------------------------------

def _values(name):
    valid = VALID.get(name) or refusals._choices().get(name, ())
    return st.one_of(st.sampled_from(valid), st.none(), st.booleans(), st.integers(-3, 10 ** 10),
                     st.text(max_size=40), st.text(max_size=20).map(lambda tail: SENTINEL + tail),
                     st.sampled_from(valid).map(lambda value: f"{value}\n{SENTINEL}" if isinstance(value, str) else -1))


details_strategy = st.fixed_dictionaries({}, optional={
    **{name: _values(name) for name in sorted(refusals.value_names())},
    refusals.BY_POLICY: st.sampled_from((True, False, 1, "yes", None)),
    "other": st.text(max_size=10),
})
reasons = st.one_of(st.sampled_from(sorted(refusals.HANDLER_MESSAGES)), st.text(max_size=30),
                    st.sampled_from(sorted(protocol.REFUSAL_MESSAGES)), st.none(), st.integers())


def expected(reason, details) -> tuple[str, str | None]:
    """The rule, stated apart from the module: the first form whose values all check."""
    if not isinstance(reason, str) or reason not in refusals.HANDLER_MESSAGES:
        return refusals.FALLBACK
    for form in forms(reason):
        if all(refusals.checked(name, details.get(name)) for name in names(form)):
            values = {name: details[name] for name in names(form)}
            message = form[0].format(**values)
            fix = form[1].format(**values) if form[1] is not None else None
            break
    if details.get(refusals.BY_POLICY) is True:
        task = details.get("task") if refusals.checked("task", details.get("task")) else "these"
        message, fix = f"{message} ({task} jobs write by policy)", (fix + "; or " if fix else "") + "pass -s read-only"
    return message, fix


@given(reason=reasons, details=details_strategy, prose=st.text(max_size=40),
       kind=st.sampled_from((protocol.ProtocolError, AdapterError, TransferError)))
@example(reason="unknown-job", details={"job_id": "20261013-014534-astra"}, prose="", kind=protocol.ProtocolError)
@example(reason="policy-workdir", details={"repository": "this directory is not a git repository", "task": "build"},
         prose="", kind=AdapterError)
@example(reason="other-writer", details={"job_id": "20261013-014534-astra", refusals.BY_POLICY: True, "task": None},
         prose="", kind=AdapterError)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_refusal_reads_its_most_specific_checked_form_and_never_its_exception(reason, details, prose, kind):
    error = kind(SENTINEL + prose, fix=SENTINEL + prose, reason=reason, details=details)
    shown = refusals.handler_refusal(error)
    assert shown == refusals.handler_refusal(error) == expected(reason, details)
    message, fix = shown
    assert SENTINEL not in message and SENTINEL not in (fix or "")
    assert (shown == refusals.FALLBACK) == (not isinstance(reason, str) or reason not in refusals.HANDLER_MESSAGES)


# Each value's shape, stated apart from the module's own patterns.
SHAPES = {
    "job_id": r"[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,40}",
    "lane_id": r"(codex|claude)-[1-9][0-9]{0,8}",
    "task": r"[a-z][a-z0-9-]{0,39}",
    "model": r"[a-z][a-z0-9._-]{0,79}",
}


@given(name=st.sampled_from(sorted(refusals.value_names())),
       value=st.one_of(st.text(max_size=50), st.integers(-5, 10 ** 10), st.booleans(), st.none(),
                       st.floats(allow_nan=False), st.lists(st.text(max_size=5), max_size=2),
                       st.sampled_from([v for vs in VALID.values() for v in vs]),
                       st.sampled_from([v for vs in refusals._choices().values() for v in vs])))
@example(name="job_id", value="20261013-014534-ghp-" + "a" * 20)
@example(name="lane_id", value="codex-01")
@example(name="count", value=True)
@settings(max_examples=500, deadline=None)
def test_a_checked_value_has_its_shape_and_no_free_text(name, value):
    if not refusals.checked(name, value):
        return
    if name in ("count", "limit"):
        assert type(value) is int and 0 <= value < 10 ** 9
    elif name in SHAPES:
        assert isinstance(value, str) and re.fullmatch(SHAPES[name], value)
    else:
        assert value in refusals._choices()[name]


@given(details=details_strategy)
@settings(max_examples=200, deadline=None)
def test_the_wire_shows_what_render_says(details):
    """Differential: the daemon's responder and `render` agree on every handler reason."""
    for reason in ("unknown-job", "unknown-lane", "policy-workdir", "isolation-layer", "writer-cap"):
        response = wire_reply(op="submit", error=AdapterError(SENTINEL, fix=SENTINEL, reason=reason,
                                                             details=details))
        assert (response["error"]["message"], response["error"]["fix"]) == refusals.render(reason, details)
        assert SENTINEL not in json.dumps(response)


@pytest.mark.parametrize("reason", sorted(refusals.HANDLER_MESSAGES))
def test_every_reason_reaches_the_wire_with_its_code(reason):
    details = {name: (VALID.get(name) or refusals._choices()[name])[0]
               for name in names(refusals.HANDLER_MESSAGES[reason])}
    response = wire_reply(op="submit", error=AdapterError(SENTINEL, code=7, fix=SENTINEL, reason=reason,
                                                         details=details))
    assert response["error"]["code"] == 7
    message, fix = refusals.HANDLER_MESSAGES[reason]
    assert response["error"]["message"] == message.format(**details)
    assert response["error"]["fix"] == (fix.format(**details) if fix is not None else None)


# --- the seams that build refusals ----------------------------------------------

def test_an_argument_refusal_keeps_its_words_for_direct_callers_only():
    """A submit argument check keeps its words on the exception (tests and callers that
    dispatch directly read them), and the wire reads the argument template."""
    error = protocol.ProtocolError(f"pinned_lane: {SENTINEL!r} was given as a claude lane", reason="invalid-args")
    assert "was given as a claude lane" in str(error)
    response = wire_reply(op="submit", error=error)
    assert response["error"]["message"] == protocol.REFUSAL_MESSAGES["invalid-args"]
    assert SENTINEL not in json.dumps(response)


@pytest.mark.parametrize("model", ("absent", "", False, 0, SENTINEL, "ghp_" + "A" * 36))
def test_unknown_model_refusal_names_only_a_checked_model(model):
    error = AdapterError(SENTINEL, code=7, fix=SENTINEL,
                         reason="unknown-pinned-model", details={"model": model})
    response = wire_reply(op="submit", error=error)
    named = " absent" if model == "absent" else ""
    assert response["error"] == {
        "code": 7, "message": f"unknown pinned model{named}",
        "fix": "pin a model from the live policy",
    }


@pytest.mark.parametrize("details", (
    {"model": "opus", "sandbox": "read-only"},
    {"model": SENTINEL, "sandbox": "read-only"},
    {"model": "opus", "sandbox": SENTINEL},
    {"model": "ghp_" + "A" * 36, "sandbox": "read-only"},
    {},
))
def test_shell_refusal_names_values_only_when_both_are_checked(details):
    error = AdapterError(SENTINEL, code=7, fix=SENTINEL,
                         reason="needs-shell", details=details)
    response = wire_reply(op="submit", error=error)
    message = ("needs a shell: opus runs read-only jobs without one"
               if details == {"model": "opus", "sandbox": "read-only"}
               else "needs a shell: the pinned launch runs without one")
    assert response["error"] == {
        "code": 7, "message": message,
        "fix": "pass -s workspace-write, or pin a Codex model",
    }


def test_a_refusal_by_policy_keeps_its_reason_and_names_the_way_out():
    """C-11.1, d261: a writer refusal of a job the caller did not ask to write."""
    inner = AdapterError("session s has a live writable job (20261013-014534-astra) from x",
                         fix="dispatch from that instance, or wait for or kill 20261013-014534-astra",
                         reason="other-writer", details={"job_id": "20261013-014534-astra"})
    wrapped = daemon_module._written_by_policy(inner, "build")
    assert str(wrapped).endswith("(build jobs write by policy)")
    response = wire_reply(op="submit", error=wrapped)
    assert response["error"]["message"] == ("session already has a live writable job (20261013-014534-astra) "
                                            "from another instance (build jobs write by policy)")
    assert response["error"]["fix"].endswith("; or pass -s read-only")


@pytest.mark.parametrize("lane", ("codex-99", "nonsense", SENTINEL))
def test_a_transfer_refusal_names_a_checked_lane_only(lane, tmp_path):
    from subfleet import lanes_transfer

    class Store:
        @staticmethod
        def one(sql, params):
            return None
    with pytest.raises(TransferError) as caught:
        lanes_transfer.transfer(Store(), tmp_path, lane, "v2")
    # The daemon's `lanes` op passes the reason and details on (the CLI test runs that path).
    named = f" {lane}" if lane == "codex-99" else ""
    assert refusals.handler_refusal(caught.value) == (f"lanes transfer: unknown lane{named}", "subfleet lanes list")


def test_a_refusal_s_own_words_go_to_the_daemon_log_scrubbed():
    """C-25.7 details go only to daemon.log, through the whole-text scrubber."""
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    log = logging.getLogger("handler-refusals-log")
    log.setLevel(logging.INFO)
    log.addHandler(Capture())
    token = "ghp_" + "A" * 36
    refusals.log_detail(log, "submit", AdapterError(f"host push refused: remote token {token}",
                                                    fix="omit --push-branch", reason="push-refused"))
    refusals.log_detail(log, "submit", protocol.request_error("invalid-args"))

    class Unrenderable(AdapterError):
        def __str__(self):
            raise RuntimeError("no words")
    refusals.log_detail(log, "submit", Unrenderable("x", reason="push-refused"))
    assert len(lines) == 1 and "host push refused: remote token" in lines[0]
    assert token not in lines[0] and "fix: omit --push-branch" in lines[0]


def test_the_guard_preflight_templates_keep_the_preflights_own_fixes():
    """Differential (C-14.2): a timeout or an unusable probe is unverified, not mismatched;
    its template's fix is `guard.preflight`'s, word for word, and never asks for a restore."""
    from subfleet.guard import preflight as guard
    for reason, fix in (("guard-preflight-timeout", guard._TIMEOUT_FIX), ("guard-preflight-probe", guard._PROBE_FIX)):
        message, shown = refusals.render(reason)
        assert shown == fix and "unverified, not mismatched" in message and "Restore" not in message + shown
