"""C-14.2, D-SH1 round 8: a review's publish hook that Codex does not trust gets
its own verdict kind, reason and advice.

The never-rules guard verified, so the TRUST pins are not in question, and
`subfleet doctor` never checks the publish hook (it runs the preflight without
one), so "rerun subfleet doctor" repaired nothing (CI at `f62fdec4a`). The
review may be read-only, so the text says "the review", not "the writable job".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from subfleet import refusals
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.guard import preflight


def lane(tmp_path):
    return Lane("codex-1", "codex", "codex:fake", Credential("codex", str(tmp_path), "home"),
                str(tmp_path), LaneOwner.V2, False)


@pytest.mark.parametrize("kind,reason", [
    (preflight.PUBLISH, "guard-preflight-publish"),
    (preflight.TRUST, "guard-preflight"),
    (preflight.VERSION, "guard-preflight"),
    (preflight.TIMEOUT, "guard-preflight-timeout"),
    (preflight.PROBE, "guard-preflight-probe"),
])
def test_the_daemon_gives_each_verdict_kind_its_reason(kind, reason, tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "preflight", lambda *args, **kwargs: preflight.PreflightResult(
        False, 7, "Guard preflight refused: fixture", "fixture fix", kind=kind))
    with pytest.raises(AdapterError) as raised:
        Daemon._guard_override(SimpleNamespace(codex_bin="codex"), lane(tmp_path), str(tmp_path),
                               publish_command="/fixture/publish-hook")
    assert (raised.value.reason, raised.value.code) == (reason, 7)
    assert raised.value.details == {"guard_kind": kind}


def test_a_publish_refusal_names_the_review_s_hook_and_keeps_the_preflight_s_fix():
    """Differential: the wire's fix is `guard.preflight`'s, word for word, as for a
    timeout or an unusable probe."""
    message, fix = refusals.render("guard-preflight-publish")
    assert fix == preflight._PUBLISH_FIX
    assert "review" in message and "publish hook" in message and "guard verified" in message
    assert "writable job" not in message and "TRUST" not in message + fix
    assert "guard-preflight.json" in fix and "does not check" in fix and "-m opus" in fix
    # The verdict kind named in details changes nothing: the reason's form has no value.
    assert refusals.render("guard-preflight-publish", {"guard_kind": "trust"}) == (message, fix)

