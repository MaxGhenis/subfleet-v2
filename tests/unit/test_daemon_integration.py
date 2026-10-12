"""Integration of lane-owned guard and credential seams without real providers."""
import gc
import fcntl
import json
import os
import sys
from types import ModuleType, SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemon as module
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon


def lane(home):
    return Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)


def test_c14_2_daemon_consumes_guard_preflight_and_passes_override(tmp_path, monkeypatch):
    """C-14.2, C-14.3 daemon preflight supplies the exact reviewed adapter override."""
    guard = ModuleType("subfleet.guard")
    preflight = ModuleType("subfleet.guard.preflight")
    calls = []
    def check(binary, **kwargs):
        calls.append((binary, kwargs))
        return SimpleNamespace(ok=True, override="hooks=reviewed", message="ok", fix=None)
    preflight.preflight = check
    monkeypatch.setitem(sys.modules, "subfleet.guard", guard)
    monkeypatch.setitem(sys.modules, "subfleet.guard.preflight", preflight)
    adapter = SimpleNamespace(codex_bin="codex")
    assert Daemon._guard_override(adapter, lane(tmp_path), str(tmp_path)) == "hooks=reviewed"
    assert calls == [("codex", {"home": str(tmp_path), "workdir": str(tmp_path)})]
    preflight.preflight = lambda *args, **kwargs: SimpleNamespace(ok=False, override=None,
        message="trust hash mismatch", fix="restore TRUST")
    with pytest.raises(AdapterError, match="trust hash mismatch") as error:
        Daemon._guard_override(adapter, lane(tmp_path), str(tmp_path))
    assert error.value.code == 7 and error.value.fix == "restore TRUST"


def test_c6_5_api_key_home_is_refused_without_exposing_its_value(tmp_path):
    """C-6.5, C-10.5 API-key homes are refused with code 7 and a value-free error."""
    secret = "test-value-never-store-this"
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": secret}))
    with pytest.raises(AdapterError) as error:
        Daemon._validate_home(lane(tmp_path))
    assert error.value.code == 7
    assert secret not in str(error.value)
    assert "subscription" in error.value.fix


_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(
        st.sampled_from(["OPENAI_API_KEY", "auth_mode", "tokens", "last_refresh"]) | st.text(max_size=6),
        inner | st.sampled_from(["api_key", "apikey", "chatgpt", ""]), max_size=3),
    max_leaves=10)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(content=st.one_of(st.binary(max_size=48), _JSON_VALUES.map(lambda value: json.dumps(value).encode())))
def test_property_a_codex_home_is_accepted_or_refused_and_nothing_else(tmp_path, content):
    """C-10.2, C-6.12 for all inputs: whatever a Codex home's `auth.json` holds, `_validate_home`
    returns (a JSON object with no API key) or raises AdapterError, code 7 with a fix, which every
    caller handles (tests/fake/test_admission_probe_errors.py). Before, an unparsable file raised
    JSONDecodeError and one that is not an object AttributeError, out of the admission pass. The
    API-key rule is the old one, and no refusal carries the file's contents, even as its context."""
    (tmp_path / "auth.json").write_bytes(content)
    try:
        auth = json.loads(content)
    except (ValueError, RecursionError):
        auth = None
    refused = (not isinstance(auth, dict) or bool(auth.get("OPENAI_API_KEY"))
               or auth.get("auth_mode") in ("api_key", "apikey"))
    try:
        Daemon._validate_home(lane(tmp_path))
    except AdapterError as error:
        assert refused
        assert error.code == 7 and error.fix
        assert error.__cause__ is None and error.__context__ is None
    else:
        assert not refused


@pytest.mark.parametrize("home", ["missing", "directory"])
def test_c10_2_a_codex_home_with_no_auth_json_file_is_not_refused(tmp_path, home):
    """C-10.2: no regular file there is still not a refusal, as before (`is_file()`'s answer)."""
    if home == "directory":
        (tmp_path / "auth.json").mkdir()
    Daemon._validate_home(lane(tmp_path))


def test_c5_8_failed_identity_initialization_releases_flock(tmp_path, monkeypatch):
    """C-5.3, C-5.8 failed identity inspection cannot strand the singleton lock."""
    def unavailable():
        raise module.procs.InspectionError("test inspection failure")
    monkeypatch.setattr(module.procs, "boot_id", unavailable)
    with pytest.raises(module.procs.InspectionError):
        Daemon(tmp_path)
    gc.collect()
    fd = os.open(tmp_path / "daemon.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


# --- C-10.3: the desktop identity the daemon derives each cycle ---------------

def claude_lane(lane_id="claude-1", identity="acct:org", label="max@example.test"):
    return Lane(lane_id, "claude", f"claude:{identity}",
                Credential("claude", f"claude-quota-{label}", "keychain-token"),
                None, LaneOwner.V2, False, True, identity, label)


def test_c10_3_no_desktop_login_means_no_credential_is_read(tmp_path, monkeypatch):
    """C-10.3, C-10.5 with no desktop login recorded for this HOME there is no
    desktop identity to verify, and the daemon reaches for no keychain item."""
    asked = []
    daemon = Daemon(tmp_path, desktop_prober=lambda: asked.append(True))
    try:
        daemon.store.put_lane(claude_lane())
        monkeypatch.setattr(module.capacity, "read_desktop_account", lambda path=None: None)
        desktop = daemon._desktop_identity()
        assert asked == []
        assert not desktop.verified
        # The recorded flag is preserved untouched, which for this lane is 0.
        assert not daemon._capacity_view(desktop)["lanes"][0]["desktop"]
    finally:
        daemon.close()


def test_c10_3_a_verified_desktop_identity_is_recorded_and_flags_its_lane(tmp_path, monkeypatch):
    """C-10.3 the profile of the desktop credential decides which lane is the
    desktop's, and the identity it confirmed is kept so a later outage has
    something to compare a label against."""
    from subfleet.adapters.claude import ProfileResult

    daemon = Daemon(tmp_path, desktop_prober=lambda: ProfileResult(
        "ok", email="desktop@example.test", account_uuid="acct", org_uuid="org"))
    try:
        daemon.store.put_lane(claude_lane("claude-1"))
        daemon.store.put_lane(claude_lane("claude-2", identity="other:org",
                                          label="other@example.test"))
        monkeypatch.setattr(module.capacity, "read_desktop_account",
                            lambda path=None: "stale-cached@example.test")
        desktop = daemon._desktop_identity()
        assert desktop.verified and desktop.identity == "acct:org"
        view = daemon._capacity_view(desktop)
        assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
            "claude-1": True, "claude-2": False}
        def recorded():
            return [json.loads(row["data_json"]) for row in daemon.store.list_events()
                    if row["kind"] == module.capacity.DESKTOP_IDENTITY_EVENT
                    and json.loads(row["data_json"]).get("identity")]

        assert [row["identity"] for row in recorded()] == ["acct:org"]
        # C-10.6: one request per reading window, and the identity is recorded
        # again only when it changes.
        daemon._desktop_identity()
        assert [row["identity"] for row in recorded()] == ["acct:org"]
    finally:
        daemon.close()


def test_c10_6_a_mismatch_is_recorded_on_the_lane_and_never_cleared(tmp_path):
    """C-10.6 the daemon remembers what the adapter found, and "not a candidate
    until an operator re-enrols it" survives a later, healthier-looking answer."""
    from subfleet.contracts import Outcome, OutcomeClass

    daemon = Daemon(tmp_path)
    try:
        daemon.store.put_lane(claude_lane())
        mismatch = Outcome(OutcomeClass.OK, "ok", {"identity": {
            "status": "identity-mismatch",
            "identity": {"email": "someone@else.test",
                         "account_uuid": "other", "org_uuid": "org"}}})
        daemon._record_identity("claude-1", mismatch)
        assert daemon.store.one("SELECT identity_status FROM lanes")["identity_status"] == "mismatch"
        assert not daemon._identity_binds(mismatch)

        verified = Outcome(OutcomeClass.OK, "ok", {"identity": {
            "status": "verified",
            "identity": {"email": "max@example.test",
                         "account_uuid": "acct", "org_uuid": "org"}}})
        daemon._record_identity("claude-1", verified)
        assert daemon.store.one("SELECT identity_status FROM lanes")["identity_status"] == "mismatch"
        assert daemon._identity_binds(verified)
    finally:
        daemon.close()


def test_c1_4_a_label_only_lane_learns_the_identity_its_credential_reports(tmp_path):
    """C-1.4, C-10.6 a lane enrolled on a label alone binds to the identity the
    profile endpoint gave for its own credential, once, and is judged on uuids
    from then on."""
    from subfleet.contracts import Outcome, OutcomeClass

    daemon = Daemon(tmp_path)
    try:
        daemon.store.put_lane(
            Lane("claude-1", "claude", "claude:max@example.test",
                 Credential("claude", "claude-quota-max@example.test", "keychain-token"),
                 None, LaneOwner.V2, False, True, None, "max@example.test"),
            identity_status="enrolled")
        daemon._record_identity("claude-1", Outcome(OutcomeClass.OK, "ok", {"identity": {
            "status": "verified",
            "identity": {"email": "max@example.test",
                         "account_uuid": "acct", "org_uuid": "org"}}}))
        lane_row = daemon.store.get_lane("claude-1")
        assert lane_row.identity == "acct:org"
        assert lane_row.label == "max@example.test"
        assert daemon.store.one("SELECT identity_status FROM lanes")["identity_status"] == "verified"
    finally:
        daemon.close()
