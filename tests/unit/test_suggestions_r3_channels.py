"""Round-three probes of the promised suggestion diagnostic secrecy."""

from __future__ import annotations

import io
import json
import uuid
from contextlib import redirect_stderr

import pytest

from subfleet import cli
from subfleet.client import DaemonError
from subfleet.conversations import suggestions
from tests.unit.test_suggestion_error_boundaries import reply
from tests.unit.test_suggestion_secret_properties import (
    AGENT, assert_secret_absent, everything_shown, fixture, origin_turn,
)

SECRET = "password='SECRET7-left\n右秘密-TAIL8'"
VALUE = 'failure said "ok" ' + SECRET


@pytest.mark.parametrize("surface", ["wire", "notice", "cli"])
def test_unknown_model_is_scrubbed_before_diagnostic_quoting(surface, monkeypatch):
    with fixture() as service:
        payload = {"request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture",
                   "brief": "Fixture brief", "model": VALUE}
        response = reply(service, "suggestion.create", payload)
        assert response["ok"] is False and response["error"]["code"] == 2
        assert service.store.query("SELECT * FROM suggestions") == []
        error = response["error"]
        if surface == "wire":
            shown = response
        elif surface == "notice":
            # The real fields consumed by UIModel.describe.
            shown = error["message"] + " (" + error["fix"] + ")"
        else:
            monkeypatch.setenv("SUBFLEET_HOME", str(service.root))
            monkeypatch.setattr(cli, "session_id", lambda: None)

            def call(*args, **kwargs):
                raise DaemonError(error["code"], error["message"], error["fix"])

            monkeypatch.setattr(cli.Client, "call", call)
            args = cli.build_parser().parse_args(["suggest", "--list"])
            output = io.StringIO()
            with redirect_stderr(output):
                assert cli.cmd_suggest(args) == 2
            shown = output.getvalue()
        assert_secret_absent(shown, SECRET)


ERRORS = {
    "key dict": lambda: KeyError({"x": VALUE}),
    "key tuple": lambda: KeyError((5, VALUE)),
    "key padded dict": lambda: KeyError({"x": "界" * 430 + " " + VALUE}),
    "key padded tuple": lambda: KeyError((5, "界" * 450 + " " + VALUE)),
    "os bytes filenames": lambda: OSError(5, "failure", VALUE.encode(), None, VALUE.encode()),
    "key string": lambda: KeyError(VALUE),
    "os string filenames": lambda: OSError(5, "failure", VALUE, None, VALUE),
    "unicode": lambda: UnicodeError("failure", VALUE),
    "unicode decode": lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, VALUE),
    "unicode encode": lambda: UnicodeEncodeError("utf-8", "\ud800", 0, 1, VALUE),
    "unicode translate": lambda: UnicodeTranslateError("\ud800", 0, 1, VALUE),
}


@pytest.mark.parametrize("kind", ERRORS)
def test_exception_argument_shapes_are_safe_in_all_start_channels(kind):
    with fixture() as service:
        cid, job = origin_turn(service)
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture brief",
            "workspace": str(service.folder / "work"), "calling_job": job,
        }, AGENT)["suggestion"]
        sid = row["suggestion_id"]
        error = ERRORS[kind]()

        def fail(**kwargs):
            raise error

        service.store.submit_message = fail
        response = reply(service, args={"suggestion_id": sid})
        assert response["ok"] is False
        service._resume_start(sid, 1, service.clock())
        shown = everything_shown(service, sid, cid, response["error"]["message"])
        shown["wire"] = response
        refusal = response["error"]
        shown["notice"] = refusal["message"] + (" (" + refusal["fix"] + ")" if refusal.get("fix") else "")
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("SUBFLEET_HOME", str(service.root))
            patch.setattr(cli, "session_id", lambda: None)

            def call(*args, **kwargs):
                raise DaemonError(refusal["code"], refusal["message"], refusal.get("fix"))

            patch.setattr(cli.Client, "call", call)
            output = io.StringIO()
            with redirect_stderr(output):
                assert cli.cmd_suggest(cli.build_parser().parse_args(["suggest", "--list"])) == refusal["code"]
            shown["cli"] = output.getvalue()
        assert service.suggestions.get(sid)["state"] == suggestions.STARTING
        assert service.captured.lines and any("finishing the start" in line for line in service.captured.lines)
        leaked = [name for name, value in shown.items() if "SECRET7-left" in json.dumps(value, ensure_ascii=False)]
        assert not leaked, {"kind": kind, "leaked_surfaces": leaked}
        assert_secret_absent(shown, SECRET)
