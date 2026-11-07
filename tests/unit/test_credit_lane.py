"""C-31.2, C-31.4: an API credit lane's credential and its renewal arithmetic.

Credential tests use stand-in runners (D-ST1): no test starts `security`,
`agent-secret` or `claude-credits`, and none reads the real registry
(`SUBFLEET_API_PLAN_REGISTRY` names a file under the test's tmp_path).
The differential tests compare `subfleet.credit_lane` with the verbatim copy of
the `claude-credits` functions in tests/fixtures/claude_credits_ref.py.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import credit_lane
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential
from subfleet.credentials import resolve_credential, resolve_credit_key

FIXTURE = Path(__file__).parents[1] / "fixtures" / "claude_credits_ref.py"
_SPEC = importlib.util.spec_from_file_location("claude_credits_ref", FIXTURE)
REF = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(REF)

EMAIL = "plan@example.org"
REFERENCE = f"claude-api-{EMAIL}"
#: Fake values only: neither is a real key or a real organization.
KEY = "sk-ant-api03-" + "A1b2C3d4" * 10 + "-_Zz"
ORG = "00000000-0000-4000-8000-00000000000a"
OTHER_ORG = "00000000-0000-4000-8000-00000000000b"
HELPER = "/nonexistent/standin/agent-secret"


class Runner:
    """A stand-in for subprocess.run: it records each argv and answers as told."""

    def __init__(self, stdout: str = KEY + "\n", rc: int = 0, raises: BaseException | None = None):
        self.stdout, self.rc, self.raises, self.calls = stdout, rc, raises, []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Write the registry `claude-credits` would keep, under tmp_path, and point at it."""
    path = tmp_path / "claude-credits" / "registry.json"
    path.parent.mkdir()
    monkeypatch.setenv(credit_lane.REGISTRY_VARIABLE, str(path))
    monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", HELPER)

    def write(accounts=None, raw: str | None = None):
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            body = {"version": 1, "accounts": accounts if accounts is not None else {
                EMAIL: {"service": REFERENCE, "kind": "api", "fingerprint": credit_lane.fingerprint(KEY),
                        "renews_day": 15, "credit_usd": 200, "org_id": ORG, "status": "active"}}}
            path.write_text(json.dumps(body), encoding="utf-8")
        return path
    write()
    return write


def credential(ref: str = REFERENCE, kind: str = "keychain-token") -> Credential:
    return Credential(provider="claude-api", ref=ref, kind=kind)


def refusal(runner: Runner, **kwargs) -> AdapterError:
    options = {"identity": ORG, **kwargs}
    cred = options.pop("credential", credential())
    with pytest.raises(AdapterError) as caught:
        resolve_credential(cred, runner=runner, **options)
    error = caught.value
    assert error.reason == "credential-refused"
    assert error.code == 7 and error.fix
    runs = {KEY[i:i + 12] for i in range(len(KEY) - 11)}
    for text in (str(error), error.fix or "", json.dumps(error.details)):
        # Like claude-credits' own test: neither the key nor any 12-character run of it.
        assert not any(run in text for run in runs)
    return error


def test_a_registered_key_resolves_to_exactly_anthropic_api_key(registry):
    runner = Runner()
    assert resolve_credential(credential(), runner=runner, identity=ORG) == {"ANTHROPIC_API_KEY": KEY}
    # Read through the agent-keychain helper, the way claude-quota- references are; never `security`.
    assert runner.calls == [[HELPER, "get", REFERENCE]]


@pytest.mark.parametrize("kind", ["env", "home"])
def test_any_kind_but_a_keychain_token_is_refused_before_a_read(registry, kind):
    runner = Runner()
    refusal(runner, credential=credential(ref="ANTHROPIC_API_KEY" if kind == "env" else "/tmp/x", kind=kind))
    assert runner.calls == []


@pytest.mark.parametrize("ref", ["claude-quota-plan@example.org", "claude-api-", "claude-api-plan", "claude-api-a@b",
                                 "claude-api-plan@example.org\n", " claude-api-plan@example.org", "plan@example.org",
                                 "claude-api-plan@exa mple.org", "claude-api-pl/an@example.org"])
def test_a_reference_that_is_not_claude_api_email_is_refused(registry, ref):
    runner = Runner()
    refusal(runner, credential=credential(ref=ref))
    assert runner.calls == []


@pytest.mark.parametrize("raw", ["", "{", "[]", '{"accounts": []}', "ÿ"])
def test_an_unreadable_registry_refuses(registry, raw):
    registry(raw=raw)
    runner = Runner()
    refusal(runner)
    assert runner.calls == []


def test_a_missing_registry_refuses(registry, tmp_path, monkeypatch):
    monkeypatch.setenv(credit_lane.REGISTRY_VARIABLE, str(tmp_path / "absent.json"))
    refusal(Runner())


@pytest.mark.parametrize("change", [
    {"fingerprint": None}, {"fingerprint": "sha256:xyz"}, {"org_id": None}, {"org_id": "  "}, "drop"])
def test_an_email_the_registry_lacks_a_fingerprint_or_organization_for_refuses(registry, change):
    entry = {"service": REFERENCE, "fingerprint": credit_lane.fingerprint(KEY), "org_id": ORG, "renews_day": 15}
    if change == "drop":
        accounts = {"someone@example.org": entry}
    else:
        entry.update(change)
        accounts = {EMAIL: {k: v for k, v in entry.items() if v is not None}}
    registry(accounts)
    runner = Runner()
    refusal(runner)
    assert runner.calls == []


def test_another_organization_than_the_lanes_refuses_before_a_read(registry):
    runner = Runner()
    error = refusal(runner, identity=OTHER_ORG)
    assert "organization" in str(error)
    assert ORG not in str(error) and OTHER_ORG not in str(error)
    assert runner.calls == []


def test_a_lane_without_an_identity_refuses_outside_enrolment(registry):
    refusal(Runner(), identity=None)
    assert resolve_credit_key(credential(), identity=None, enrolling=True, runner=Runner()) == {
        "ANTHROPIC_API_KEY": KEY}


def test_a_denied_organization_refuses(registry):
    runner = Runner()
    error = refusal(runner, denied_org_ids=(OTHER_ORG, ORG))
    assert "denied_org_ids" in str(error)
    assert runner.calls == []


def test_no_helper_refuses_and_never_falls_back_to_security(registry, monkeypatch):
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET")
    runner = Runner()
    refusal(runner)
    assert runner.calls == []


@pytest.mark.parametrize("runner", [Runner(rc=1), Runner(stdout=""), Runner(stdout="\n"),
                                    Runner(raises=OSError("boom")),
                                    Runner(raises=subprocess.TimeoutExpired("agent-secret", 10))])
def test_a_failed_read_refuses(registry, runner):
    refusal(runner)


@pytest.mark.parametrize("value", [
    "sk-ant-oat01-" + "x" * 80, "sk-ant-ort01-" + "x" * 80, "sk-ant-admin01-" + "x" * 80,
    "sk-ant-api03-short", "not-a-key", "sk-ant-usr01-" + "x" * 39, KEY + " trailing"])
def test_a_value_that_is_not_a_console_key_refuses_without_naming_it(registry, value):
    error = refusal(Runner(stdout=value + "\n"))
    assert value not in str(error) and value not in (error.fix or "")


def test_a_key_whose_fingerprint_is_not_the_registrys_refuses(registry):
    other = "sk-ant-api03-" + "Q" * 90
    error = refusal(Runner(stdout=other + "\n"))
    assert other not in str(error)
    assert "registry" in str(error)


VALID_KEYS = st.builds(lambda kind, ver, body: f"sk-ant-{kind}{ver}-{body}",
                       st.sampled_from(["usr", "svc", "api"]), st.sampled_from(["", "01", "03"]),
                       st.text(alphabet="ABCXYZabcxyz0189_-", min_size=40, max_size=60))
VALUES = st.one_of(VALID_KEYS, st.text(min_size=1, max_size=120)).filter(
    lambda v: v.strip() and "\n" not in v and "\r" not in v)


@settings(max_examples=80, deadline=None)
@given(value=VALUES, matching=st.booleans())
def test_resolution_accepts_a_value_only_when_the_reference_rules_do(value, matching, tmp_path_factory):
    """Invariant 1, C-31.2: a value resolves exactly when `claude_credits.classify`
    accepts it and its fingerprint is the registry's; no refusal names it."""
    path = tmp_path_factory.mktemp("registry") / "registry.json"
    recorded = REF.fingerprint(value) if matching else REF.fingerprint(value + "x")
    path.write_text(json.dumps({"accounts": {EMAIL: {"fingerprint": recorded, "org_id": ORG}}}), encoding="utf-8")
    try:
        REF.classify(value)
        accepted = matching
    except REF.Refused:
        accepted = False
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(credit_lane.REGISTRY_VARIABLE, str(path))
        patch.setenv("CLAUDE_LANE_AGENT_SECRET", HELPER)
        try:
            found = resolve_credential(credential(), runner=Runner(stdout=value), identity=ORG)
        except AdapterError as error:
            assert not accepted
            assert error.reason == "credential-refused"
            # A value of 12 characters or more never appears (shorter ones can by chance).
            assert len(value) < 12 or value not in str(error) and value not in (error.fix or "")
        else:
            assert accepted and found == {"ANTHROPIC_API_KEY": value}


# --- differential tests against the verbatim claude-credits copy -------------------------

KEYISH = st.one_of(
    st.text(max_size=80),
    st.builds(lambda kind, ver, body: f"sk-ant-{kind}{ver}-{body}",
              st.sampled_from(["usr", "svc", "api", "oat", "ort", "admin", "xyz", ""]),
              st.sampled_from(["", "01", "03", "1", "123", "٣٣"]),
              st.text(alphabet="AZaz09_-+./", min_size=0, max_size=60)),
)


@settings(max_examples=300, deadline=None)
@given(value=KEYISH)
def test_key_rules_agree_with_claude_credits_classify(value):
    try:
        expected = REF.classify(value)
    except REF.Refused:
        expected = None
    assert credit_lane.key_kind(value) == expected
    assert (credit_lane.key_refusal(value) is None) == (expected is not None)


@settings(max_examples=200, deadline=None)
@given(value=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=80))
def test_fingerprint_agrees_with_claude_credits(value):
    assert credit_lane.fingerprint(value) == REF.fingerprint(value)


DATES = st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2099, 12, 31))
DAYS = st.integers(min_value=1, max_value=31)


@settings(max_examples=400, deadline=None)
@given(day=DAYS, ref=DATES)
def test_renewal_arithmetic_agrees_with_claude_credits(day, ref):
    assert credit_lane.renewal_on_or_after(day, ref) == REF.renewal_on_or_after(day, ref)
    nxt = credit_lane.next_renewal(day, ref)
    assert (nxt - ref).days == REF.days_left({"renews_day": day}, ref)
    start = credit_lane.last_renewal(day, ref)
    # The cycle `ref` is in: from the last renewal on or before it to the next one after it.
    assert start <= ref < nxt
    assert credit_lane.renewal_on_or_after(day, start) == start
    assert credit_lane.next_renewal(day, start) == nxt
    assert (nxt - start).days <= credit_lane.TRAILING_CYCLE_DAYS


@settings(max_examples=400, deadline=None)
@given(day=st.one_of(st.none(), DAYS), seen=DATES, workspace=st.booleans())
def test_closure_length_agrees_with_claude_credits_exhausted_until(day, seen, workspace):
    """Invariant 5: the next renewal strictly after the day seen, or one day later
    when the renewal is unknown, falls that day, or the limit is a workspace's."""
    entry = {"renews_day": day, **({"exhausted_by": "workspace"} if workspace else {})}
    until = credit_lane.exhausted_until(day, seen, workspace=workspace)
    assert until.isoformat() == REF.exhausted_until(entry, seen)
    assert until > seen
    if day is None or workspace or credit_lane.renewal_on_or_after(day, seen) == seen:
        assert until == seen + dt.timedelta(days=1)
    else:
        assert until == credit_lane.renewal_on_or_after(day, seen) and until.day == min(
            day, __import__("calendar").monthrange(until.year, until.month)[1])


@pytest.mark.parametrize("entry,expected", [
    ({"renews_day": 15}, 15), ({"renews_day": 0}, None), ({"renews_day": 32}, None), ({"renews_day": True}, None),
    ({"renews_day": "15"}, None), ({}, None), (None, None)])
def test_renews_day_reads_only_a_valid_day(entry, expected):
    assert credit_lane.renews_day(entry) == expected


def test_registry_path_never_falls_back_under_pytest(monkeypatch):
    from subfleet.home_paths import PersonalPathRefused
    monkeypatch.delenv(credit_lane.REGISTRY_VARIABLE, raising=False)
    with pytest.raises(PersonalPathRefused):
        credit_lane.registry_path()


ORGS = st.sampled_from([ORG, OTHER_ORG, None, "", "   ", 7])


@settings(max_examples=150, deadline=None)
@given(registered=ORGS, identity=st.sampled_from([ORG, OTHER_ORG, None]),
       denied=st.frozensets(st.sampled_from([ORG, OTHER_ORG])))
def test_a_key_resolves_only_for_the_lanes_own_undenied_organization(registered, identity, denied,
                                                                     tmp_path_factory):
    """Invariant 7, first half, for any registry contents: a credit lane's key
    resolves (so the lane can launch) only while the registry's organization for
    its email is a nonblank id equal to the lane's identity and not denied; every
    other case refuses before the keychain is read."""
    path = tmp_path_factory.mktemp("orgs") / "registry.json"
    entry = {"service": REFERENCE, "fingerprint": credit_lane.fingerprint(KEY)}
    if registered is not None:
        entry["org_id"] = registered
    path.write_text(json.dumps({"version": 1, "accounts": {EMAIL: entry}}), encoding="utf-8")
    runner = Runner()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(credit_lane.REGISTRY_VARIABLE, str(path))
        patch.setenv("CLAUDE_LANE_AGENT_SECRET", HELPER)
        allowed = isinstance(registered, str) and registered.strip() == registered != "" \
            and registered == identity and registered not in denied
        try:
            found = resolve_credential(credential(), runner=runner, identity=identity, denied_org_ids=denied)
        except AdapterError as error:
            assert not allowed and error.reason == "credential-refused"
            assert runner.calls == []
        else:
            assert allowed and found == {"ANTHROPIC_API_KEY": KEY}
