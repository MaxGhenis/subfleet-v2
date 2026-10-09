"""C-10.3, C-10.6, C-10.7, C-23.45: what `doctor` says about who holds what.

Two questions an operator cannot answer by looking: does the cached desktop login
agree with the credential beside it, and is any account being counted twice? The
first is the 2026-09-05 incident stated in words before anything believes it; the
second is C-10.7's rule that identity, not email, joins two credentials to one
account.
"""

from __future__ import annotations

import json

import pytest

from subfleet import cli
from subfleet.adapters.claude import (
    PROFILE_NO_SCOPE, PROFILE_UNAVAILABLE, ProfileResult,
)
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import Store

AXIOM = ("1c216ab2-a95f-4554-a2be-36dbc6731133", "fc628aae-6967-4171-9bd0-ba0b04cc388a")
AXIOM_EMAIL = "max@axiom.org"
RULESATLAS = ("aa53aefb-0000-4000-8000-0000000ac1a5", "943e990b-0000-4000-8000-00000000f5a1")
RULESATLAS_EMAIL = "max@rulesatlas.org"


def pair(uuids):
    return ":".join(uuids)


def seed(root, *lanes):
    with Store(root / "state.sqlite3") as store:
        for lane_id, identity, label, values in lanes:
            store.put_lane(
                Lane(lane_id, "claude", f"claude:{identity or label}",
                     Credential("claude", f"claude-quota-{label or lane_id}", "keychain-token"),
                     None, LaneOwner.V2, False, values.get("enabled", True),
                     identity, label),
                identity_status=values.get("identity_status"))


def roster_check(root):
    return {check["check"]: check for check in cli.doctor_checks(root)}[
        "one identity per enabled lane"]


def live_row(root, **kwargs):
    return cli.live_checks(root, **kwargs)[0]


def profile(email=AXIOM_EMAIL, uuids=AXIOM):
    return ProfileResult("ok", email=email, account_uuid=uuids[0], org_uuid=uuids[1])


def cached_file(tmp_path, email=RULESATLAS_EMAIL, uuids=RULESATLAS):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {
        "emailAddress": email, "accountUuid": uuids[0], "organizationUuid": uuids[1]}}))
    return path


# --- C-10.7: one account, one enabled lane ------------------------------------


def test_two_enabled_lanes_with_one_identity_are_a_defect(root):
    """C-10.7, C-23.45 at most one lane is canonical for an account key: two lanes
    holding one identity double-count that account's quota."""
    seed(root,
         ("claude-1", pair(AXIOM), AXIOM_EMAIL, {}),
         ("claude-2", pair(AXIOM), AXIOM_EMAIL, {}))
    check = roster_check(root)
    assert check["status"] == "fail"
    assert "claude-1 (max@axiom.org) and claude-2 (max@axiom.org) hold one account" in check["detail"]
    assert "only claude-1 takes its work" in check["detail"]
    assert pair(AXIOM) in check["detail"]
    assert "lanes transfer" in check["detail"]      # the fix line


def test_a_desktop_credential_and_an_inference_token_are_distinct_lanes(root):
    """C-10.7 "distinct lanes even when their labels match": one email, two
    identities, no defect — it is identity that joins credentials to an account."""
    seed(root,
         ("claude-1", pair(AXIOM), AXIOM_EMAIL, {}),
         ("claude-2", pair(RULESATLAS), AXIOM_EMAIL, {}))
    assert roster_check(root)["status"] == "pass"


def test_a_disabled_duplicate_is_not_a_defect(root):
    """C-23.45 the later binding is marked non-canonical; once it is disabled the
    account has exactly one enabled lane again."""
    seed(root,
         ("claude-1", pair(AXIOM), AXIOM_EMAIL, {}),
         ("claude-2", pair(AXIOM), AXIOM_EMAIL, {"enabled": False}))
    assert roster_check(root)["status"] == "pass"


def test_a_mismatched_lane_is_named_with_the_command_that_releases_it(root):
    """C-10.6 a mismatched lane is not a candidate until re-enrolled, so `doctor`
    says which lane and which command."""
    seed(root, ("claude-1", pair(AXIOM), AXIOM_EMAIL, {"identity_status": "mismatch"}))
    check = roster_check(root)
    assert check["status"] == "unknown"
    assert "claude-1" in check["detail"] and "lanes enroll" in check["detail"]


def test_a_claude_lane_that_recorded_nothing_is_named(root):
    """C-10.6 a lane with no identity and no label produces no capacity at all;
    an operator should hear that from `doctor` rather than from an empty status."""
    seed(root, ("claude-1", None, None, {}))
    check = roster_check(root)
    assert check["status"] == "unknown"
    assert "no identity recorded" in check["detail"] and "claude-1" in check["detail"]


def test_no_store_yet_is_not_a_complaint(root):
    """C-17.5 `doctor` runs before the daemon ever has; nothing to read is fine."""
    assert roster_check(root)["status"] == "pass"


# --- C-10.3: the cached login against the credential --------------------------


def test_agreement_says_so_plainly(root, tmp_path):
    """C-10.3 the cached hint and the credential naming one identity is the
    ordinary case, and it is stated rather than left silent."""
    row = live_row(root, claude_json=cached_file(tmp_path, AXIOM_EMAIL, AXIOM),
                   profile=lambda: profile())
    assert row["status"] == "pass"
    assert AXIOM_EMAIL in row["detail"]


def test_disagreement_names_both_identities_and_a_fix(root, tmp_path):
    """C-10.3, C-10.6 the 2026-09-05 incident in words: the file says RulesAtlas,
    the credential says Axiom. Both are named, the credential is believed, and the
    operator is told what to do about it."""
    row = live_row(root, claude_json=cached_file(tmp_path), profile=lambda: profile())
    assert row["status"] == "fail"
    assert RULESATLAS_EMAIL in row["detail"] and pair(RULESATLAS) in row["detail"]
    assert AXIOM_EMAIL in row["detail"] and pair(AXIOM) in row["detail"]
    assert "trust the credential" in row["detail"]
    assert f"lanes enroll claude-quota-{AXIOM_EMAIL}" in row["detail"]


@pytest.mark.parametrize("answer", [
    ProfileResult(PROFILE_UNAVAILABLE, detail="OSError"),
    ProfileResult(PROFILE_NO_SCOPE, detail="http-403"),
])
def test_an_unanswered_profile_is_unverified_and_says_what_happens_next(root, tmp_path, answer):
    """C-10.3 while the desktop identity cannot be verified, lanes fall back to
    matching its label — a warning, not a failure, and not silence."""
    row = live_row(root, claude_json=cached_file(tmp_path), profile=lambda: answer)
    assert row["status"] == "unknown"
    assert answer.status in row["detail"]
    assert RULESATLAS_EMAIL in row["detail"]


def test_an_unreadable_desktop_credential_is_reported_not_raised(root, tmp_path):
    """C-17.3 a doctor never traces back: a keychain it cannot read is a warning."""
    def refuse():
        raise RuntimeError("the keychain is locked")

    row = live_row(root, claude_json=cached_file(tmp_path), profile=refuse)
    assert row["status"] == "unknown"
    assert "could not be read" in row["detail"]


def test_no_cached_login_still_names_whose_credential_it_is(root, tmp_path):
    """C-10.3 with nothing cached there is no disagreement to report, but the
    identity the credential actually holds is still worth saying."""
    row = live_row(root, claude_json=tmp_path / "absent.json", profile=lambda: profile())
    assert row["status"] == "unknown"
    assert AXIOM_EMAIL in row["detail"]


def test_doctor_live_runs_both_families_of_check(root, capsys, monkeypatch):
    """C-17.1 `doctor --live` is the offline checks plus the ones that need a
    credential; the offline `doctor` never reaches for one."""
    seed(root, ("claude-1", pair(AXIOM), AXIOM_EMAIL, {}))
    monkeypatch.setattr(cli, "_version", lambda binary: ("pass", f"stub {binary}"), raising=False)
    assert cli.main(["doctor"]) in (0, 1)  # host checks (PATH shadows, hook entries) may fail on this machine
    offline = capsys.readouterr().out
    assert "one identity per enabled lane" in offline
    assert "cached ~/.claude.json" not in offline

    assert cli.main(["doctor", "--live"]) in (0, 1)  # host checks may fail; the rows below are what this test proves
    live = capsys.readouterr().out
    assert "one identity per enabled lane" in live
    assert "cached ~/.claude.json agrees with the desktop credential" in live
