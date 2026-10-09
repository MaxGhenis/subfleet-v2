"""C-1.4, C-9.1, C-10.6, C-10.7: a Claude lane is bound to the identity its own
credential reports, and a reading it cannot attribute is evidence, never capacity.

On 2026-09-05 v1 read `~/.claude.json`, believed the account name it found there,
and recorded one account's usage against another lane. Every test here asks the
same question that incident answered wrongly: *whose credential is this?* — and
proves that when the answer is not the lane's, no window survives.

The fixtures under `tests/fixtures/claude/identity/` are the two halves of that
incident: `profile-ok.json` is the RulesAtlas identity the lane recorded, and
`profile-mismatch.json` is the identity the credential really held, copied from
`identity-fix-live-result.json`.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import (
    ENROLL_MODEL, OAUTH_PROFILE_URL, PROFILE_INVALID, PROFILE_NO_SCOPE, PROFILE_OK,
    PROFILE_UNAVAILABLE, ClaudeAdapter,
)
from subfleet.contracts import Credential, IdentityStatus, ReadingLabel
from tests.conftest import NOW, exit_info, make_lane, make_launch, org_opener, stage_case
from tests.fake.profile import IDENTITY_DIR, fixture_response, opener


def identity_of(fixture: str) -> tuple[str, str]:
    """`(identity, email)` a profile fixture names, read from the fixture itself."""
    payload = json.loads((IDENTITY_DIR / f"{fixture}.json").read_text(encoding="utf-8"))
    return (f"{payload['account']['uuid']}:{payload['organization']['uuid']}",
            payload["account"]["email"])


LANE_IDENTITY, LANE_EMAIL = identity_of("profile-ok")            # RulesAtlas
OTHER_IDENTITY, OTHER_EMAIL = identity_of("profile-mismatch")    # max@axiom.org
TOKEN = "fixture-lane-token"
ENV = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}


LANE_ORG = LANE_IDENTITY.split(":")[1]
OTHER_ORG = OTHER_IDENTITY.split(":")[1]


def adapter_for(fixture: str | None = "profile-ok", *, seen: list | None = None,
                org: str | None = LANE_ORG, org_status: int = 200,
                org_error: Exception | None = None, **kwargs) -> ClaudeAdapter:
    """An adapter whose profile answers with `fixture` and whose organization
    probe (D-ID1) names `org`: by default the fixture lane's own organization."""
    return ClaudeAdapter(now=lambda: NOW, profile_opener=opener(fixture, seen=seen),
                         org_opener=org_opener(org_status, org, error=org_error), **kwargs)


def bound_lane(**kwargs):
    return make_lane(account=LANE_EMAIL, identity=LANE_IDENTITY, **kwargs)


def enroll_stream(model=ENROLL_MODEL, info=None) -> str:
    rows = [{"type": "system", "subtype": "init", "session_id": "s", "model": model},
            {"type": "assistant", "session_id": "s",
             "message": {"model": model, "content": [{"type": "text", "text": "ok"}]}}]
    if info is not None:
        rows.append({"type": "rate_limit_event", "session_id": "s", "rate_limit_info": info})
    rows.append({"type": "result", "subtype": "success", "is_error": False,
                 "result": "ok", "session_id": "s"})
    return "\n".join(json.dumps(row) for row in rows) + "\n"


ALLOWED = {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.05, "resetsAt": 1788000000},
    "seven_day": {"utilization": 0.25, "resetsAt": 1788600000}}}


class Runner:
    """A `claude` that authenticates and reports two windows, without a process."""

    def __init__(self, stdout: str | None = None, rc: int = 0, token: str = TOKEN):
        self.stdout = enroll_stream(info=ALLOWED) if stdout is None else stdout
        self.rc, self.token, self.calls = rc, token, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0].endswith("security") or argv[1:2] == ["get"]:
            return subprocess.CompletedProcess(argv, 0, self.token, "")
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


# --- the endpoint (C-10.6) ---------------------------------------------------


def test_probe_profile_asks_the_endpoint_with_the_lanes_own_bearer():
    """C-10.6 the identity of a lane is what the profile endpoint says about the
    very credential that produces its usage — so that is the token it carries."""
    seen: list = []
    profile = adapter_for(seen=seen).probe_profile(ENV)
    assert profile.status == PROFILE_OK
    assert profile.identity == LANE_IDENTITY
    assert profile.email == LANE_EMAIL
    assert profile.triple == {"email": LANE_EMAIL,
                              "account_uuid": LANE_IDENTITY.split(":")[0],
                              "org_uuid": LANE_IDENTITY.split(":")[1]}
    url, _suffix = seen[0]
    assert url == OAUTH_PROFILE_URL


def test_probe_profile_carries_the_bearer_and_nothing_else_does():
    """C-10.5 the credential appears in one Authorization header and nowhere in
    the result — not in a status, not in a detail, not in an identity."""
    captured: list = []

    def watchful(request, timeout):
        captured.append((request.full_url, dict(request.headers), timeout))
        return fixture_response("profile-ok")

    profile = ClaudeAdapter(now=lambda: NOW, profile_opener=watchful).probe_profile(ENV)
    url, headers, timeout = captured[0]
    assert url == OAUTH_PROFILE_URL and timeout == 15.0
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(profile.__dict__)


@pytest.mark.parametrize("fixture,status", [
    ("profile-ok", PROFILE_OK),
    ("profile-no-scope", PROFILE_NO_SCOPE),
    ("profile-unavailable", PROFILE_UNAVAILABLE),
])
def test_each_fixture_answer_maps_to_one_status(fixture, status):
    """C-10.6 four answers and no fifth: an account, no scope, no answer, nonsense."""
    assert adapter_for(fixture).probe_profile(ENV).status == status


def test_a_200_without_an_account_is_invalid_not_unavailable():
    """C-10.6 a reply the endpoint completed but that names nobody is `invalid`:
    it is a different failure from never having been answered."""
    def nameless(request, timeout):
        return 200, json.dumps({"account": {"email": "who@example.test"}}).encode()

    profile = ClaudeAdapter(now=lambda: NOW, profile_opener=nameless).probe_profile(ENV)
    assert profile.status == PROFILE_INVALID


def test_a_401_is_unverified_and_never_auth_dead():
    """C-9.3 `auth-dead` needs a 401 from a *usage* endpoint; the profile endpoint
    is not one, so a 401 here leaves the identity unverified and nothing else."""
    def unauthorized(request, timeout):
        raise urllib.error.HTTPError(OAUTH_PROFILE_URL, 401, "Unauthorized", {}, None)

    profile = ClaudeAdapter(now=lambda: NOW, profile_opener=unauthorized).probe_profile(ENV)
    assert (profile.status, profile.detail) == (PROFILE_UNAVAILABLE, "http-401")


def test_a_failed_request_records_its_kind_and_never_its_message():
    """C-10.5 a urllib exception can quote the request headers, and those hold the
    bearer, so only the exception's type is ever kept."""
    def leaky(request, timeout):
        raise OSError(f"failed sending Authorization: Bearer {TOKEN}")

    profile = ClaudeAdapter(now=lambda: NOW, profile_opener=leaky).probe_profile(ENV)
    assert profile.status == PROFILE_UNAVAILABLE
    assert profile.detail == "OSError"
    assert TOKEN not in json.dumps(profile.__dict__)


def test_no_credential_at_all_is_unavailable():
    """C-10.6 a launch rebuilt without its credential cannot ask, so it does not
    claim to know: `unavailable`, which drops the reading rather than storing it."""
    profile = adapter_for().probe_profile({})
    assert (profile.status, profile.detail) == (PROFILE_UNAVAILABLE, "no-token")


def test_a_home_lane_reads_its_bearer_from_the_provider_cli_store(tmp_path):
    """C-23.47 the auth store belongs to the provider CLI: subfleet reads that
    file to ask who the credential is, and never writes it."""
    home = tmp_path / "lane-home"
    home.mkdir()
    credentials = home / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    before = credentials.read_bytes()
    profile = adapter_for().probe_profile({"CLAUDE_CONFIG_DIR": str(home)})
    assert profile.identity == LANE_IDENTITY
    assert credentials.read_bytes() == before


def test_one_request_per_credential_per_reading_window():
    """C-10.6 the profile beside a reading was fetched in the same probe cycle:
    a credential is asked once per `READING_TTL_S`, then reused."""
    seen: list = []
    adapter = adapter_for(seen=seen)
    for _ in range(3):
        adapter.probe_profile(ENV)
    assert len(seen) == 1
    adapter.probe_profile({"CLAUDE_CODE_OAUTH_TOKEN": "a-different-lanes-token"})
    assert len(seen) == 2
    adapter.probe_profile(ENV, refresh=True)
    assert len(seen) == 3


# --- the check (C-10.6) ------------------------------------------------------


def test_the_credentials_own_account_verifies_the_lane():
    """C-10.6 the readings of a run are capacity when the profile identity of the
    credential that produced them equals the lane's recorded identity."""
    check = adapter_for().identity_check(LANE_IDENTITY, LANE_EMAIL, ENV)
    assert check.status is IdentityStatus.VERIFIED
    assert check.binds
    assert check.evidence()["status"] == "verified"


def test_another_account_is_a_mismatch_carrying_the_observed_triple():
    """C-10.6 when the identity differs the finding is kept with the identity it
    actually saw — the operator has to be able to see whose credential it is."""
    check = adapter_for("profile-mismatch").identity_check(LANE_IDENTITY, LANE_EMAIL, ENV)
    assert check.status is IdentityStatus.MISMATCH
    assert not check.binds
    evidence = check.evidence()
    assert evidence["status"] == "identity-mismatch"
    assert evidence["expected"] == LANE_IDENTITY
    assert evidence["identity"] == {"email": OTHER_EMAIL,
                                    "account_uuid": OTHER_IDENTITY.split(":")[0],
                                    "org_uuid": OTHER_IDENTITY.split(":")[1]}


def test_a_setup_token_keeps_the_identity_recorded_at_enrolment():
    """C-10.6 a setup token cannot ask the profile endpoint (403). A lane enrolled
    that way keeps working on the label the operator gave it, and says so."""
    check = adapter_for("profile-no-scope").identity_check(None, LANE_EMAIL, ENV)
    assert check.status is IdentityStatus.ENROLLED
    assert check.binds
    assert check.evidence()["status"] == "identity-enrolled"


@pytest.mark.parametrize("org, error, expected", [
    (LANE_ORG, None, IdentityStatus.UNVERIFIED),     # its organization, but which seat?
    (OTHER_ORG, None, IdentityStatus.MISMATCH),      # another organization: another account
    (None, OSError("down"), IdentityStatus.UNVERIFIED),
])
def test_a_bound_lane_that_stops_answering_is_unverified_not_enrolled(org, error, expected):
    """C-10.6 the setup-token carve-out is for a lane that never recorded an
    identity. A lane that recorded an account and now answers 403 has had its
    credential changed under it — the incident's own shape — and stores nothing.
    Its organization header (D-ID1) can still say it is another account; it
    cannot say it is the same one, because an organization is not an account."""
    check = adapter_for("profile-no-scope", org=org, org_error=error).identity_check(
        LANE_IDENTITY, LANE_EMAIL, ENV)
    assert check.status is expected
    assert not check.binds


def test_an_unanswered_profile_is_unverified():
    """C-10.6 an unavailable profile stores the finding and no windows."""
    check = adapter_for("profile-unavailable").identity_check(LANE_IDENTITY, LANE_EMAIL, ENV)
    assert check.status is IdentityStatus.UNVERIFIED
    assert check.evidence()["status"] == "identity-unverified"
    assert not check.binds


def test_a_lane_that_recorded_nothing_is_unverified_without_being_asked():
    """C-10.6 "recorded only when the profile identity equals the lane's recorded
    identity": with nothing recorded, equality cannot hold, and there is nothing
    to compare an answer against — so none is requested."""
    seen: list = []
    check = adapter_for(seen=seen).identity_check(None, None, ENV)
    assert check.status is IdentityStatus.UNVERIFIED
    assert not check.binds
    assert seen == []
    assert check.evidence()["expected"] is None


def test_a_label_only_lane_is_judged_on_its_label_and_learns_its_identity():
    """C-1.4, C-10.6 a lane enrolled without a verifiable profile has one claim:
    the operator's label. A profile that agrees hands back the identity the lane
    should record; a profile that names someone else is a mismatch."""
    agreeing = adapter_for().identity_check(None, LANE_EMAIL, ENV)
    assert agreeing.status is IdentityStatus.VERIFIED
    assert agreeing.observed == LANE_IDENTITY          # what the caller records

    disagreeing = adapter_for("profile-mismatch").identity_check(None, LANE_EMAIL, ENV)
    assert disagreeing.status is IdentityStatus.MISMATCH
    assert disagreeing.observed_email == OTHER_EMAIL


# --- enrolment (C-10.2, C-1.4) -----------------------------------------------


def test_enrolment_records_the_identity_the_credential_reports():
    """C-1.4, C-10.6 the account key is the identity pair from the lane's own
    credential; the email the operator used to name the keychain item is a label."""
    adapter = adapter_for(runner=Runner())
    info = adapter.enroll(Credential("claude", f"claude-quota-{LANE_EMAIL}", "keychain-token"))
    assert info.account_key == f"claude:{LANE_IDENTITY}"
    assert info.identity == LANE_IDENTITY
    assert info.label == LANE_EMAIL
    assert info.identity_status == "verified"
    assert {r.window for r in info.readings} == {"five_hour", "seven_day"}


def test_enrolment_of_a_setup_token_records_the_operators_label():
    """C-10.6 a setup token has no profile scope, so the lane is enrolled on the
    label the operator gave it and its readings will carry `identity-enrolled`.
    D-ID1: it records the organization its own response header named, so it can
    be told apart from every other lane from its first cycle."""
    adapter = adapter_for("profile-no-scope", runner=Runner())
    info = adapter.enroll(Credential("claude", f"claude-quota-{LANE_EMAIL}", "keychain-token"))
    assert info.account_key == f"claude:{LANE_EMAIL}"
    assert info.identity == f"org:{LANE_ORG}"
    assert info.label == LANE_EMAIL
    assert info.identity_status == "enrolled"
    assert {r.window for r in info.readings} == {"five_hour", "seven_day"}


def test_enrolment_with_only_an_organization_answer_is_enrolled_on_it():
    """C-10.6 an unreachable profile is no longer the end of it: the token's own
    organization header (D-ID1) binds the lane at the organization level."""
    adapter = adapter_for("profile-unavailable", runner=Runner())
    info = adapter.enroll(Credential("claude", f"claude-quota-{LANE_EMAIL}", "keychain-token"))
    assert info.identity_status == "enrolled"
    assert info.identity == f"org:{LANE_ORG}"


@pytest.mark.parametrize("profile", ["profile-unavailable", "profile-no-scope"])
@pytest.mark.parametrize("org_status, org, error", [
    (200, None, None),                 # an answer that names no organization
    (401, LANE_ORG, None),             # an unauthenticated answer names nothing
    (503, LANE_ORG, None),
    (200, "not:an-id", None),          # a header that is not an organization id
    (200, None, OSError("down")),
])
def test_enrolment_that_cannot_tell_whose_token_it_is_is_refused(profile, org_status, org, error):
    """C-10.6, D-ID1 enrolment is where an operator is watching, so a token
    nobody can name is refused there and nothing is recorded, rather than enrolled
    unverified and found out later."""
    adapter = adapter_for(profile, runner=Runner(), org=org, org_status=org_status, org_error=error)
    with pytest.raises(AdapterError) as refused:
        adapter.enroll(Credential("claude", f"claude-quota-{LANE_EMAIL}", "keychain-token"))
    assert refused.value.code == 7
    assert "could not tell whose token" in str(refused.value)
    assert TOKEN not in str(refused.value) + str(refused.value.fix)


def test_enrolment_refuses_a_token_whose_profile_names_another_email():
    """C-10.6, D-ID1 the keychain item names the account the operator meant; a
    token whose own profile names another is refused, not relabelled."""
    adapter = adapter_for("profile-ok", runner=Runner())
    with pytest.raises(AdapterError) as refused:
        adapter.enroll(Credential("claude", "claude-quota-someone@else.example", "keychain-token"))
    assert refused.value.code == 7
    assert LANE_EMAIL in str(refused.value) and "someone@else.example" in str(refused.value)


def test_enrolment_refuses_a_profile_that_names_nobody():
    """C-10.6 a 200 without an account is not a credential this fleet can bind."""
    def nameless(request, timeout):
        return 200, b'{"account": {}, "organization": {}}'

    adapter = ClaudeAdapter(now=lambda: NOW, runner=Runner(), profile_opener=nameless)
    with pytest.raises(AdapterError) as caught:
        adapter.enroll(Credential("claude", f"claude-quota-{LANE_EMAIL}", "keychain-token"))
    assert caught.value.code == 7
    assert caught.value.fix


# --- probing and classification (C-9.1, C-9.8, C-10.6) -----------------------


def test_probe_returns_readings_only_for_the_account_it_is_bound_to():
    """C-9.1, C-10.6 a probe of a mismatched credential yields no readings at all."""
    lane = bound_lane()
    assert adapter_for(runner=Runner()).probe(lane, ENV)
    assert adapter_for("profile-mismatch", runner=Runner()).probe(lane, ENV) == ()


def test_classify_of_a_mismatched_credential_stores_evidence_and_no_windows(tmp_path):
    """C-9.8, C-10.6 the 2026-09-05 incident, reproduced: a credential whose
    profile says max@axiom.org on a lane recorded as RulesAtlas keeps every
    window out of the store and records what it saw instead."""
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id=ENROLL_MODEL,
                         identity=LANE_IDENTITY, label=LANE_EMAIL)
    outcome = adapter_for("profile-mismatch").classify(attempt_dir, launch, exit_info(rc))
    assert outcome.readings == ()
    finding = outcome.evidence["identity"]
    assert finding["status"] == "identity-mismatch"
    assert finding["identity"]["email"] == OTHER_EMAIL
    assert finding["expected"] == LANE_IDENTITY
    # The turn itself still worked; C-10.6 governs capacity, not whether the
    # provider answered (C-9.2 keeps that judgement where it was).
    assert outcome.cls.value == "ok"


def test_classify_of_the_lanes_own_credential_keeps_its_windows(tmp_path):
    """C-9.8 the same run, on the account the lane is bound to, is capacity."""
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id=ENROLL_MODEL,
                         identity=LANE_IDENTITY, label=LANE_EMAIL)
    outcome = adapter_for().classify(attempt_dir, launch, exit_info(rc))
    assert {r.window for r in outcome.readings} == {"five_hour", "seven_day"}
    assert all(r.label is ReadingLabel.PROVIDER for r in outcome.readings)
    assert outcome.evidence["identity"]["status"] == "verified"


def test_a_setup_token_lanes_stream_readings_are_stored_as_enrolled(tmp_path):
    """C-10.6 a setup-token lane keeps working: its `rate_limit_event` windows are
    stored, and the evidence beside them says the identity was never verifiable."""
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id=ENROLL_MODEL,
                         identity=None, label=LANE_EMAIL)
    outcome = adapter_for("profile-no-scope").classify(attempt_dir, launch, exit_info(rc))
    assert {r.window for r in outcome.readings} == {"five_hour", "seven_day"}
    assert outcome.evidence["identity"]["status"] == "identity-enrolled"


def test_an_unverifiable_profile_drops_the_windows_of_a_bound_lane(tmp_path):
    """C-10.6 when the profile is unavailable the reading is evidence, not capacity."""
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id=ENROLL_MODEL,
                         identity=LANE_IDENTITY, label=LANE_EMAIL)
    outcome = adapter_for("profile-unavailable").classify(attempt_dir, launch, exit_info(rc))
    assert outcome.readings == ()
    assert outcome.evidence["identity"]["status"] == "identity-unverified"


def test_a_rejection_on_a_mismatched_credential_records_no_admission(tmp_path):
    """C-9.8, C-10.6 an `admission-observed` reading says "this model was refused
    on *this lane*". If the credential is not the lane's, that is not true either."""
    attempt_dir, rc = stage_case("rejected-credits-fable", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id="claude-fable-5-1",
                         identity=LANE_IDENTITY, label=LANE_EMAIL)
    outcome = adapter_for("profile-mismatch").classify(attempt_dir, launch, exit_info(rc))
    assert outcome.readings == ()
    assert outcome.cls.value == "limited"          # the refusal itself still happened


def test_probe_outcome_carries_the_identity_finding(tmp_path):
    """C-11.4, C-10.6 a probe the daemon acts on carries the same finding, so a
    mismatch discovered by a probe is recorded exactly as one found by an attempt."""
    lane = bound_lane()
    adapter = adapter_for("profile-mismatch", runner=Runner())
    outcome = adapter.probe_outcome(lane, ENV, ENROLL_MODEL)
    assert outcome.readings == ()
    assert outcome.evidence["identity"]["status"] == "identity-mismatch"


def test_the_bearer_never_reaches_an_outcome(tmp_path):
    """C-10.5 nothing an attempt leaves behind may contain the credential."""
    attempt_dir, rc = stage_case("success-allowed", tmp_path / "a1")
    launch = make_launch(attempt_dir, session_id="s", model_id=ENROLL_MODEL,
                         identity=LANE_IDENTITY, label=LANE_EMAIL)
    outcome = adapter_for("profile-mismatch").classify(attempt_dir, launch, exit_info(rc))
    assert TOKEN not in json.dumps(outcome.evidence) + outcome.detail
