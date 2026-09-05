"""C-10.3, C-10.6, C-10.7: which lane is the desktop app's, and which is nobody's.

The desktop app's account is the one lane a job must not be dispatched to without
`allow_desktop`, so getting "which lane is that?" wrong costs the operator their
own interactive session. On 2026-09-05 v1 answered it from `~/.claude.json` and
was wrong. Here the answer comes from the profile endpoint asked with the desktop
app's own keychain credential, and the cached file is only ever a fallback.
"""

from __future__ import annotations

import json

import pytest

from subfleet.capacity import (
    DESKTOP_IDENTITY_EVENT, build_view, cached_desktop_identity, desktop_identity,
    identity_blocked, lane_labels, last_desktop_identity,
)
from subfleet.adapters.claude import PROFILE_NO_SCOPE, PROFILE_UNAVAILABLE, ProfileResult

NOW = "2026-09-05T13:22:14Z"

# The two identities of the incident: what the desktop credential really held,
# and what the cached file said it held.
AXIOM = "1c216ab2-a95f-4554-a2be-36dbc6731133:fc628aae-6967-4171-9bd0-ba0b04cc388a"
AXIOM_EMAIL = "max@axiom.org"
RULESATLAS = "aa53aefb-0000-4000-8000-0000000ac1a5:943e990b-0000-4000-8000-00000000f5a1"
RULESATLAS_EMAIL = "max@rulesatlas.org"


def lane(lane_id="claude-1", **values):
    row = {"lane_id": lane_id, "provider": lane_id.split("-")[0],
           "account_key": f"claude:{AXIOM}", "owner": "v2", "desktop": False,
           "identity": AXIOM, "label": AXIOM_EMAIL, "identity_status": "verified"}
    row.update(values)
    return row


def verified(identity=AXIOM, email=AXIOM_EMAIL, **kwargs):
    account_uuid, _, org_uuid = identity.partition(":")
    return desktop_identity(
        ProfileResult("ok", email=email, account_uuid=account_uuid, org_uuid=org_uuid),
        **kwargs)


# --- a verified desktop identity ---------------------------------------------


def test_the_desktop_lane_is_the_one_whose_identity_matches_the_credential():
    """C-10.3 a lane is `desktop` when its identity equals the identity of the
    desktop app's own credential — not when its email looks familiar."""
    lanes = [lane("claude-1"), lane("claude-2", identity=RULESATLAS,
                                    account_key=f"claude:{RULESATLAS}",
                                    label=RULESATLAS_EMAIL)]
    view = build_view(lanes, now=NOW, desktop=verified())
    assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
        "claude-1": True, "claude-2": False}


def test_a_cached_email_cannot_make_a_lane_desktop_once_the_identity_is_known():
    """C-10.3 the incident, inverted: `~/.claude.json` says RulesAtlas while the
    credential really holds Axiom. The Axiom lane is the desktop's, and the
    RulesAtlas lane — the one v1 protected — is free to take work."""
    lanes = [lane("claude-1"),
             lane("claude-2", identity=RULESATLAS, account_key=f"claude:{RULESATLAS}",
                  label=RULESATLAS_EMAIL, desktop=True)]
    view = build_view(lanes, now=NOW, desktop=verified(cached_label=RULESATLAS_EMAIL))
    assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
        "claude-1": True, "claude-2": False}


def test_a_lane_with_no_identity_falls_back_to_the_verified_desktop_label():
    """C-10.3 a lane that recorded no identity cannot be compared by identity, so
    it is compared by label — protection is never dropped for lack of a binding."""
    lanes = [lane("claude-1", identity=None, label=AXIOM_EMAIL,
                  account_key=f"claude:{AXIOM_EMAIL}"),
             lane("claude-2", identity=None, label="someone@example.test",
                  account_key="claude:someone@example.test")]
    view = build_view(lanes, now=NOW, desktop=verified())
    assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
        "claude-1": True, "claude-2": False}


def test_a_codex_lane_is_never_touched_by_the_claude_desktop_identity():
    """C-10.3 `desktop` is Claude's; Codex has `app_shadowed`, which is not this."""
    view = build_view([lane("codex-1", provider="codex", identity=None, label=None)],
                      now=NOW, desktop=verified())
    assert view["lanes"][0]["desktop"] is False
    assert "desktop_identity" not in view["lanes"][0]


# --- an unverified desktop identity ------------------------------------------


@pytest.mark.parametrize("profile", [
    None,
    ProfileResult(PROFILE_UNAVAILABLE, detail="OSError"),
    ProfileResult(PROFILE_NO_SCOPE, detail="http-403"),
])
def test_while_unverified_the_cached_email_and_the_last_verified_label_both_hold(profile):
    """C-10.3 "while the desktop identity cannot be verified, every lane whose
    label matches either the cached email or the last verified desktop identity
    is treated as desktop"."""
    lanes = [lane("claude-1", identity=None, label=RULESATLAS_EMAIL),
             lane("claude-2", identity=None, label="last@example.test"),
             lane("claude-3", identity=None, label="unrelated@example.test")]
    desktop = desktop_identity(profile, cached_label=RULESATLAS_EMAIL,
                               last_label="last@example.test")
    assert not desktop.verified
    view = build_view(lanes, now=NOW, desktop=desktop)
    assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
        "claude-1": True, "claude-2": True, "claude-3": False}


def test_the_label_fallback_ignores_case():
    """C-10.3 an email is not case sensitive, and a login switch is not either."""
    desktop = desktop_identity(None, cached_label="MAX@Axiom.org")
    assert desktop.owns(lane(identity=None, label="max@axiom.org"))


def test_an_unverified_identity_with_no_fallback_preserves_recorded_flags():
    """C-10.3 an unknown desktop identity never silently removes protection."""
    lanes = [lane("claude-1", desktop=True), lane("claude-2", desktop=False)]
    view = build_view(lanes, now=NOW, desktop=desktop_identity(None))
    assert {row["lane_id"]: row["desktop"] for row in view["lanes"]} == {
        "claude-1": True, "claude-2": False}
    assert view["lanes"][0]["desktop_identity"] == "unverified"


def test_a_lane_enrolled_before_identities_is_matched_by_the_email_in_its_key():
    """C-10.3 a v1-shaped lane is keyed `claude:<email>`; that email is the only
    label it has, and the fallback must still recognise it."""
    row = lane("claude-1", identity=None, label=None,
               account_key=f"claude:{RULESATLAS_EMAIL}")
    assert lane_labels(row) == {RULESATLAS_EMAIL}
    assert desktop_identity(None, cached_label=RULESATLAS_EMAIL).owns(row)


def test_a_verified_key_of_two_uuids_contributes_no_email():
    """C-1.4 a verified lane's key is a pair of uuids, which is nobody's email."""
    assert lane_labels(lane("claude-1", label=None)) == set()


# --- the last verified identity, and the cached hint -------------------------


def test_the_newest_recorded_desktop_identity_wins():
    """C-10.3 the store keeps the last verified desktop identity so an outage has
    something to compare a label against."""
    events = [
        {"event_id": 4, "kind": DESKTOP_IDENTITY_EVENT,
         "data_json": json.dumps({"identity": AXIOM, "label": AXIOM_EMAIL})},
        {"event_id": 9, "kind": DESKTOP_IDENTITY_EVENT,
         "data_json": json.dumps({"identity": RULESATLAS, "label": RULESATLAS_EMAIL})},
        {"event_id": 11, "kind": "probe.state", "data_json": "{}"},
    ]
    assert last_desktop_identity(events)["label"] == RULESATLAS_EMAIL
    assert last_desktop_identity([]) is None
    assert last_desktop_identity([{"event_id": 1, "kind": DESKTOP_IDENTITY_EVENT,
                                   "data_json": "not json"}]) is None


def test_the_cached_file_is_read_whole_for_the_doctor_and_never_trusted(tmp_path):
    """C-10.3 `doctor` compares the cached login with the profile, so it needs the
    identity the file claims, not just its email."""
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {
        "emailAddress": RULESATLAS_EMAIL,
        "accountUuid": RULESATLAS.split(":")[0],
        "organizationUuid": RULESATLAS.split(":")[1],
        "organizationName": "RulesAtlas"}}))
    assert cached_desktop_identity(path) == {
        "email": RULESATLAS_EMAIL, "account_uuid": RULESATLAS.split(":")[0],
        "org_uuid": RULESATLAS.split(":")[1], "organization": "RulesAtlas"}


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"oauthAccount": null}', "[]"])
def test_an_unreadable_cached_file_is_simply_absent(tmp_path, content):
    """C-10.3 a hint that cannot be read is not an error and not an identity."""
    path = tmp_path / ".claude.json"
    if content is not None:
        path.write_text(content)
    assert cached_desktop_identity(path) == {}


# --- a mismatched lane is not a candidate (C-10.6) ---------------------------


def test_a_mismatched_lane_is_not_a_candidate():
    """C-10.6 a lane whose credential proved to hold another account is not a
    candidate until an operator re-enrols it — whatever else is true of it."""
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
    from subfleet.scheduler import evaluate

    policy = load_policy(DEFAULT_POLICY_PATH)
    lanes = [lane("claude-1", identity_status="mismatch"), lane("claude-2")]
    view = build_view(lanes, now=NOW)
    assert identity_blocked(view["lanes"][0])
    assert not identity_blocked(view["lanes"][1])
    decision = evaluate(policy, view, {"job_id": "j", "pinned_model": "haiku",
                                       "sandbox": "read-only", "exclusions": "[]"})
    assert decision.chosen_lane == "claude-2"
    rejected = {row["lane_id"]: row for row in decision.evaluations[0]["rejections"]}
    assert "identity-mismatch" in rejected["claude-1"]["reasons"]


def test_every_other_identity_status_still_routes():
    """C-10.6 only a proven mismatch blocks: an unverified lane keeps its place in
    the roster with no reading, which C-11.3 already knows how to order."""
    for status in ("verified", "enrolled", "unverified", None):
        view = build_view([lane("claude-1", identity_status=status)], now=NOW)
        assert not identity_blocked(view["lanes"][0])
