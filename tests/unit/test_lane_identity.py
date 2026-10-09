"""C-1.4, C-10.6 to C-10.9: whose credential a lane holds, judged across the fleet.

Incident D-ID1 (2026-10-09): claude-4 (max@maxghenis.com), claude-7
(max@hivesight.ai) and claude-19 (max.ghenis@gmail.com) held tokens of one
account, so Subfleet counted it three times and could reach neither other account.
Every test here is about one of the properties that would have caught it: an
identity a setup token can report (its organization), one candidate per account,
a label judged only on profile answers, and readings that match too well.

The examples use the incident's shape with synthetic uuids. The properties run
Hypothesis over arbitrary fleets.
"""

from __future__ import annotations

import itertools
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from subfleet import lane_identity as li
from subfleet.contracts import Credential, IdentityStatus, Lane, LaneOwner
from subfleet.store import Store

ORG_SHARED = "5ba7ed00-1111-4000-8000-00000000d1d1"      # the one account three lanes held
ORG_GMAIL = "9a1a9a1a-2222-4000-8000-000000000002"       # max.ghenis@gmail.com's own
ACCOUNT_MAXGHENIS = "aaaa1111-0000-4000-8000-000000000003"
ACCOUNT_GMAIL = "bbbb2222-0000-4000-8000-000000000004"
TEAM = "7eam0000-0000-4000-8000-000000000005"

PROPERTY = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --- identity strings (C-1.4) --------------------------------------------------


def test_c1_4_the_two_identity_forms_round_trip():
    """C-1.4 an identity is `<account>:<org>` from a profile or `org:<org>` from a
    setup token's own response header; each splits back into what made it."""
    assert li.split_identity(li.account_identity("a-1", "o-1")) == ("a-1", "o-1")
    assert li.split_identity(li.org_identity("o-1")) == (None, "o-1")
    assert li.account_level("a-1:o-1") and not li.account_level("org:o-1")


@pytest.mark.parametrize("value", [None, "", "  ", "org:", "org:a:b", ":o", "a:", "a:b:c", "a b:c",
                                   "org:" + "x" * 129, 7, "org:-leading-dash", "naïve:o"])
def test_c1_4_anything_else_is_not_an_identity(value):
    """C-1.4 a colon inside a part, a missing part or an oversized one is no identity,
    and nothing that is not one is ever the same account as anything."""
    assert not li.is_identity(value)
    assert not li.same_account(value, value)


def test_c10_7_same_account_by_organization_when_one_side_names_only_that():
    """C-10.7 on a personal plan the organization is the account: an
    organization-level identity matches the account in it, and only that one."""
    assert li.same_account(f"org:{ORG_SHARED}", f"{ACCOUNT_MAXGHENIS}:{ORG_SHARED}")
    assert li.same_account(f"org:{ORG_SHARED}", f"org:{ORG_SHARED.upper()}")
    assert not li.same_account(f"org:{ORG_SHARED}", f"org:{ORG_GMAIL}")
    # Two seats of one Team organization are two accounts.
    assert not li.same_account(f"seat-1:{TEAM}", f"seat-2:{TEAM}")
    assert li.same_account(f"org:{TEAM}", f"seat-1:{TEAM}") and li.same_account(f"org:{TEAM}", f"seat-2:{TEAM}")


part = st.text("abcdef0123456789-", min_size=1, max_size=12).filter(lambda s: s[0] != "-")
identity = st.one_of(st.builds(li.org_identity, part), st.builds(li.account_identity, part, part))


@PROPERTY
@given(identity, identity)
def test_c10_7_same_account_is_reflexive_symmetric_and_never_across_organizations(a, b):
    """C-10.7 reflexive and symmetric; two identities in different organizations are
    never one account; two account-level identities are one exactly when equal."""
    assert li.same_account(a, a)
    assert li.same_account(a, b) == li.same_account(b, a)
    if li.identity_org(a).casefold() != li.identity_org(b).casefold():
        assert not li.same_account(a, b)
    if li.account_level(a) and li.account_level(b):
        assert li.same_account(a, b) == (a.casefold() == b.casefold())


# --- facts and labels (C-10.6) ---------------------------------------------------


def fact(email, identity, org_type="claude_max", source="desktop", at="2026-10-09T12:00:00Z"):
    return li.AccountFact(email, identity, org_type, source, at)


GMAIL_FACT = fact("max.ghenis@gmail.com", f"{ACCOUNT_GMAIL}:{ORG_GMAIL}")
MAXGHENIS_FACT = fact("max@maxghenis.com", f"{ACCOUNT_MAXGHENIS}:{ORG_SHARED}", source="login:max@maxghenis.com")


def test_c10_6_d_id1_a_fact_about_another_account_proves_nothing_about_this_one():
    """C-10.6 the desktop said max.ghenis@gmail.com is organization 9a1a9a1a. The
    lane labelled with that email holds 5ba7ed00: the fact does not name 5ba7ed00,
    so it neither proves nor contradicts the label on its own."""
    verdict = li.label_verdict("max.ghenis@gmail.com", f"org:{ORG_SHARED}", [GMAIL_FACT])
    assert verdict.verdict == "unproven"


def test_c10_6_d_id1_once_a_login_names_the_shared_account_every_other_label_is_contradicted():
    """C-10.6 when a full login's profile says 5ba7ed00 is max@maxghenis.com's
    personal organization, claude-4's label is proven and claude-7's and
    claude-19's are contradicted, by name."""
    facts = [GMAIL_FACT, MAXGHENIS_FACT]
    assert li.label_verdict("max@maxghenis.com", f"org:{ORG_SHARED}", facts).proven
    for label in ("max@hivesight.ai", "max.ghenis@gmail.com"):
        verdict = li.label_verdict(label, f"org:{ORG_SHARED}", facts)
        assert verdict.contradicted and verdict.fact == MAXGHENIS_FACT


def test_c10_6_a_team_organization_proves_and_contradicts_nothing_at_organization_level():
    """C-10.6 a Team organization has many members: matching it says nothing about
    which seat a setup token is; an account-level identity still decides."""
    facts = [fact("a@team.example", f"seat-1:{TEAM}", "claude_team")]
    assert li.label_verdict("a@team.example", f"org:{TEAM}", facts).verdict == "unproven"
    assert li.label_verdict("b@team.example", f"org:{TEAM}", facts).verdict == "unproven"
    assert li.label_verdict("a@team.example", f"seat-1:{TEAM}", facts).proven
    assert li.label_verdict("b@team.example", f"seat-1:{TEAM}", facts).contradicted


def test_c10_6_an_unknown_organization_type_is_not_assumed_personal():
    facts = [fact("a@x.example", f"acct:{ORG_SHARED}", None)]
    assert li.label_verdict("b@x.example", f"org:{ORG_SHARED}", facts).verdict == "unproven"


def test_c10_6_facts_that_disagree_decide_nothing():
    """C-10.6 an email that moved accounts leaves two answers about one account;
    a verdict a person must act on is never made from those."""
    facts = [MAXGHENIS_FACT, fact("max@hivesight.ai", f"{ACCOUNT_MAXGHENIS}:{ORG_SHARED}")]
    assert li.label_verdict("max@hivesight.ai", f"org:{ORG_SHARED}", facts).verdict == "unproven"


emails = st.sampled_from(["a@x.example", "b@x.example", "c@x.example", "A@X.example"])
org_types = st.sampled_from(["claude_max", "claude_pro", "claude_free", "claude_team", None])
small_part = st.sampled_from(["p1", "p2", "p3"])
account_identity = st.builds(li.account_identity, small_part, small_part)
any_identity = st.one_of(account_identity, st.builds(li.org_identity, small_part))
facts_strategy = st.lists(st.builds(fact, emails, account_identity, org_types,
                                    st.sampled_from(["desktop", "login:a", "lane:claude-1"])), max_size=6)


@PROPERTY
@given(emails, any_identity, facts_strategy, st.randoms(use_true_random=False))
def test_c10_6_a_verdict_is_only_ever_what_some_profile_fact_said(label, ident, facts, rnd):
    """C-10.6 `proven` needs a fact naming this email for this account, and
    `contradicted` a fact naming another email for it; at organization level only
    a personal organization's fact counts. Never both, and the order the facts
    were read in changes nothing."""
    verdict = li.label_verdict(label, ident, facts)

    def speaks(f):
        return li.same_account(f.identity, ident) and (li.account_level(ident) or f.personal)

    if verdict.proven:
        assert any(speaks(f) and f.email.casefold() == label.casefold() for f in facts)
        assert not any(speaks(f) and f.email.casefold() != label.casefold() for f in facts)
    if verdict.contradicted:
        assert any(speaks(f) and f.email.casefold() != label.casefold() for f in facts)
        assert not any(speaks(f) and f.email.casefold() == label.casefold() for f in facts)
    shuffled = list(facts)
    rnd.shuffle(shuffled)
    assert li.label_verdict(label, ident, shuffled).verdict == verdict.verdict


statuses = st.sampled_from([None, *IdentityStatus])


@PROPERTY
@given(statuses, emails, any_identity, facts_strategy)
def test_c10_6_judge_moves_only_an_enrolled_lane_and_only_on_a_verdict(status, label, ident, facts):
    """C-10.6 the adapter's `verified`, `mismatch` and `unverified` stand; an
    `enrolled` lane becomes `verified` exactly when its label is proven and
    `mismatch` exactly when it is contradicted."""
    final, verdict = li.judge(status, label=label, identity=ident, facts=facts)
    if status is not IdentityStatus.ENROLLED:
        assert final is status
        return
    expected = li.label_verdict(label, ident, facts)
    assert verdict == expected
    assert final is {"proven": IdentityStatus.VERIFIED, "contradicted": IdentityStatus.MISMATCH,
                     "unproven": IdentityStatus.ENROLLED}[expected.verdict]


def test_c10_6_account_facts_are_profile_answers_only():
    """C-10.6 a lane's own verified profile, a login's profile and the desktop's
    are facts; a lane whose label was never proven, an organization-level
    identity and an answer without an email are not."""
    facts = li.account_facts(
        lanes=[{"lane_id": "claude-1", "provider": "claude", "identity": "a:o", "label": "a@x.example",
                "identity_status": "verified"},
               {"lane_id": "claude-2", "provider": "claude", "identity": "b:o2", "label": "b@x.example",
                "identity_status": "enrolled"},
               {"lane_id": "claude-3", "provider": "claude", "identity": "org:o3", "label": "c@x.example",
                "identity_status": "verified"}],
        logins=[{"login": "d@x.example", "identity": "d:o4", "email": "d@x.example",
                 "plan": {"organization_type": "claude_max"}},
                {"login": "e@x.example", "identity": None, "email": "e@x.example"}],
        desktop=[{"identity": "f:o5", "label": "f@x.example", "organization_type": "claude_pro"},
                 {"identity": "g:o6", "label": None}])
    assert [(f.email, f.identity, f.org_type, f.source) for f in facts] == [
        ("a@x.example", "a:o", None, "lane:claude-1"),
        ("d@x.example", "d:o4", "claude_max", "login:d@x.example"),
        ("f@x.example", "f:o5", "claude_pro", "desktop")]


# --- recording a finding (C-10.6) ------------------------------------------------


def finding(status, observed=None, *, email=None, source="org-header"):
    return {"status": status, "observed": observed, "source": source,
            "identity": {"email": email, "account_uuid": None, "org_uuid": li.identity_org(observed)}}


def make_store(directory: Path, *lanes: tuple[str, str | None, str | None]) -> Store:
    store = Store(directory / "state.sqlite3")
    for lane_id, label, ident in lanes:
        store.put_lane(Lane(lane_id, "claude", f"claude:{label}",
                            Credential("claude", f"claude-quota-{label}", "keychain-token"),
                            None, LaneOwner.V2, False, True, ident, label),
                       identity_status="enrolled")
    return store


def row(store, lane_id):
    return store.one("SELECT identity,label,identity_status FROM lanes WHERE lane_id=?", (lane_id,))


def test_c10_6_d_id1_a_setup_token_lane_learns_its_organization_once(tmp_path):
    """C-1.4, C-10.6 a lane that recorded nothing learns the organization its own
    token answered with, keeps the operator's label, and leaves an event."""
    with make_store(tmp_path, ("claude-7", "max@hivesight.ai", None)) as store:
        assert li.record(store, "claude-7", finding("identity-enrolled", f"org:{ORG_SHARED}"))
        assert dict(row(store, "claude-7")) == {"identity": f"org:{ORG_SHARED}", "label": "max@hivesight.ai",
                                                "identity_status": "enrolled"}
        events = store.query("SELECT data_json FROM events WHERE kind='lane.identity' AND lane_id='claude-7' "
                             "AND data_json!='{}'")
        assert len(events) == 1 and ORG_SHARED in events[0]["data_json"]
        # The same answer again changes nothing and leaves nothing.
        assert li.record(store, "claude-7", finding("identity-enrolled", f"org:{ORG_SHARED}"))
        assert len(store.query("SELECT 1 FROM events WHERE kind='lane.identity' AND data_json!='{}'")) == 1


def test_c10_6_a_contradicted_label_is_mismatch_and_its_readings_never_count(tmp_path):
    """C-10.6 the profile facts turn a lane its own token could not prove into
    `mismatch` when they name another email for its account; the readings that
    came with that finding are not capacity, and no later answer releases it."""
    with make_store(tmp_path, ("claude-7", "max@hivesight.ai", None)) as store:
        assert not li.record(store, "claude-7", finding("identity-enrolled", f"org:{ORG_SHARED}"),
                             [MAXGHENIS_FACT])
        assert row(store, "claude-7")["identity_status"] == "mismatch"
        assert not li.record(store, "claude-7", finding("verified", f"org:{ORG_SHARED}", source="profile"))
        assert row(store, "claude-7")["identity_status"] == "mismatch"


def test_c10_6_a_proven_label_is_verified(tmp_path):
    with make_store(tmp_path, ("claude-4", "max@maxghenis.com", None)) as store:
        assert li.record(store, "claude-4", finding("identity-enrolled", f"org:{ORG_SHARED}"), [MAXGHENIS_FACT])
        assert row(store, "claude-4")["identity_status"] == "verified"


evidence_status = st.sampled_from(["verified", "identity-enrolled", "identity-mismatch", "identity-unverified",
                                   "nonsense", None])
observations = st.one_of(st.none(), st.sampled_from([f"org:{ORG_SHARED}", f"org:{ORG_GMAIL}",
                                                     f"{ACCOUNT_MAXGHENIS}:{ORG_SHARED}"]))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(evidence_status, observations), min_size=1, max_size=8),
       st.lists(st.sampled_from([GMAIL_FACT, MAXGHENIS_FACT]), max_size=2))
def test_c10_6_recording_learns_once_and_mismatch_is_final(findings, facts):
    """C-1.4, C-10.6 over any run of findings: the recorded identity changes at
    most once, from nothing; once `mismatch`, always `mismatch`; and `record`
    answers True exactly when the row it leaves binds (`verified`/`enrolled`)."""
    with tempfile.TemporaryDirectory() as directory, \
            make_store(Path(directory), ("claude-7", "max@hivesight.ai", None)) as store:
        identities, statuses = [None], []
        for status, observed in findings:
            binds = li.record(store, "claude-7", finding(status, observed), facts)
            now = row(store, "claude-7")
            if now["identity"] != identities[-1]:
                identities.append(now["identity"])
            statuses.append(now["identity_status"])
            if status not in ("verified", "identity-enrolled", "identity-mismatch", "identity-unverified"):
                assert binds is True                       # no finding: nothing to refuse
            else:
                assert binds == (now["identity_status"] in ("verified", "enrolled"))
        assert len(identities) <= 2
        if "mismatch" in statuses:
            first = statuses.index("mismatch")
            assert set(statuses[first:]) == {"mismatch"}


# --- one account, one candidate (C-10.8) -------------------------------------------


def lane(lane_id, ident, *, status="enrolled", created="2026-10-01T00:00:00Z", enabled=1, owner="v2",
         provider="claude", label=None):
    return {"lane_id": lane_id, "provider": provider, "identity": ident, "identity_status": status,
            "created_at": created, "enabled": enabled, "owner": owner, "label": label or f"{lane_id}@x.example"}


D_ID1 = [lane("claude-4", f"org:{ORG_SHARED}", created="2026-09-06T00:00:00Z", label="max@maxghenis.com"),
         lane("claude-7", f"org:{ORG_SHARED}", created="2026-09-06T01:00:00Z", label="max@hivesight.ai"),
         lane("claude-19", f"org:{ORG_SHARED}", created="2026-10-01T00:00:00Z", label="max.ghenis@gmail.com"),
         lane("claude-9", "org:0e9e0e9e", created="2026-09-06T02:00:00Z")]


def test_c10_8_d_id1_three_lanes_on_one_account_leave_one_candidate():
    """C-10.8 the incident: three lanes answer as one organization. The earliest
    binding takes the account's work; the others are shadowed by it; a lane on
    an account of its own is untouched."""
    found = li.shadowing(D_ID1)
    assert found == {
        "claude-4": {"shared_with": ["claude-19", "claude-7"], "shadowed_by": None},
        "claude-7": {"shared_with": ["claude-19", "claude-4"], "shadowed_by": "claude-4"},
        "claude-19": {"shared_with": ["claude-4", "claude-7"], "shadowed_by": "claude-4"},
    }
    groups = li.shared_groups(D_ID1)
    assert [[row["lane_id"] for row in group] for group in groups] == [["claude-4", "claude-7", "claude-19"]]


def test_c10_8_a_proven_label_speaks_for_the_account_before_an_earlier_binding():
    rows = [dict(row) for row in D_ID1]
    rows[2]["identity_status"] = "verified"
    assert li.shadowing(rows)["claude-19"]["shadowed_by"] is None
    assert li.shadowing(rows)["claude-4"]["shadowed_by"] == "claude-19"


def test_c10_8_a_team_organization_lane_does_not_strand_a_second_seat():
    """C-10.8 `same_account` is not transitive across levels: an organization-level
    lane of a Team matches both seats, which are two accounts. The second seat
    is shadowed only by a candidate, so it keeps its own work."""
    rows = [lane("claude-1", f"seat-1:{TEAM}", created="1"), lane("claude-2", f"org:{TEAM}", created="2"),
            lane("claude-3", f"seat-2:{TEAM}", created="3")]
    found = li.shadowing(rows)
    assert found["claude-2"]["shadowed_by"] == "claude-1"
    assert found["claude-3"]["shadowed_by"] is None


fleet_lane = st.fixed_dictionaries({
    "identity": st.one_of(st.none(), st.sampled_from(["org:o1", "org:o2", "a1:o1", "a2:o1", "a3:o3", "org:o3",
                                                      "bad identity"])),
    "identity_status": st.sampled_from(["verified", "enrolled", "mismatch", "unverified", None]),
    "created_at": st.sampled_from(["2026-09-01", "2026-09-02", "2026-09-03"]),
    "enabled": st.sampled_from([0, 1, True]),
    "owner": st.sampled_from(["v2", "v2", "v1"]),
    "provider": st.sampled_from(["claude", "claude", "codex"]),
})


@PROPERTY
@given(st.lists(fleet_lane, max_size=9), st.randoms(use_true_random=False))
def test_c10_8_no_two_candidates_are_one_account_and_no_account_is_left_without_one(fleet, rnd):
    """C-10.8 over any fleet: (1) no two candidates are one account; (2) every
    eligible lane is a candidate or is shadowed by an earlier-ranked candidate
    that is its account; (3) `shared_with` is symmetric; (4) a lane that cannot
    take part never appears; (5) the order of the rows changes nothing."""
    rows = [{"lane_id": f"claude-{index}", **entry} for index, entry in enumerate(fleet)]
    found = li.shadowing(rows)
    eligible = {row["lane_id"]: row for row in rows if li._eligible(row)}
    candidates = [lane_id for lane_id in eligible if not (found.get(lane_id) or {}).get("shadowed_by")]
    for a, b in itertools.combinations(candidates, 2):
        assert not li.same_account(eligible[a]["identity"], eligible[b]["identity"])
    for lane_id, entry in found.items():
        assert lane_id in eligible
        head = entry["shadowed_by"]
        if head is not None:
            assert head in candidates
            assert li.same_account(eligible[lane_id]["identity"], eligible[head]["identity"])
            assert li._rank(eligible[head]) < li._rank(eligible[lane_id])
        for other in entry["shared_with"]:
            assert lane_id in found[other]["shared_with"]
    for lane_id in eligible:
        if lane_id not in found:
            assert not any(li.same_account(eligible[lane_id]["identity"], eligible[other]["identity"])
                           for other in eligible if other != lane_id)
    shuffled = list(rows)
    rnd.shuffle(shuffled)
    assert li.shadowing(shuffled) == found


# --- readings that match too well (C-10.9) -------------------------------------


def reading(lane_id, window, utilization, resets_at, observed_at, *, scope="account", label="provider", rid=None):
    return {"lane_id": lane_id, "scope": scope, "window": window, "utilization": utilization,
            "resets_at": resets_at, "observed_at": observed_at, "label": label, "reading_id": rid}


def unbound(*names, provider="claude"):
    return [lane(name, None, provider=provider) for name in names]


def d_id1_readings(*names, at="2026-10-05T18:0{}:00Z"):
    """The 10/5 readings: weekly 99% resetting 10/10 14:00Z, five-hour 40%, all
    observed within two minutes."""
    rows = []
    for index, name in enumerate(names):
        rows.append(reading(name, "seven_day", 0.99, "2026-10-10T14:00:00Z", at.format(index)))
        rows.append(reading(name, "five_hour", 0.40, "2026-10-05T21:00:00Z", at.format(index)))
    return rows


def test_c10_9_d_id1_three_lanes_reporting_one_accounts_windows_are_twins():
    """C-10.9 the incident's readings: the weekly and the five-hour windows agree
    for every pair, within two minutes. Each pair is named once, with what matched."""
    twins = li.reading_twins(unbound("claude-4", "claude-7", "claude-19"),
                             d_id1_readings("claude-4", "claude-7", "claude-19"))
    assert [twin["lanes"] for twin in twins] == [["claude-19", "claude-4"], ["claude-19", "claude-7"],
                                                 ["claude-4", "claude-7"]]
    assert twins[0]["values"] == [{"scope": "account", "window": "five_hour", "utilization": 0.4},
                                  {"scope": "account", "window": "seven_day", "utilization": 0.99}]


def test_c10_9_lanes_whose_identities_are_known_are_c10_8s_business_not_a_guess():
    rows = [lane("claude-4", f"org:{ORG_SHARED}"), lane("claude-7", f"org:{ORG_SHARED}"), lane("claude-9", None)]
    twins = li.reading_twins(rows, d_id1_readings("claude-4", "claude-7", "claude-9"))
    assert [twin["lanes"] for twin in twins] == [["claude-4", "claude-9"], ["claude-7", "claude-9"]]


@pytest.mark.parametrize("change", [
    {"utilization": 1.0},                         # two exhausted accounts
    {"utilization": 0.0},                         # two untouched accounts
])
def test_c10_9_agreement_only_at_zero_or_one_is_no_evidence(change):
    rows = []
    for name in ("a", "b"):
        rows.append(reading(name, "seven_day", change["utilization"], "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z"))
        rows.append(reading(name, "five_hour", change["utilization"], "2026-10-05T21:00:00Z", "2026-10-05T18:00:00Z"))
    assert li.reading_twins(unbound("a", "b"), rows) == []


def test_c10_9_one_window_must_move_in_step_on_several_values():
    """C-10.9 weekly resets are whole hours and utilization whole percents, so one
    weekly agreement is a coincidence waiting to happen; three distinct values in
    step are not."""
    base = [reading(name, "seven_day", 0.43, "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z") for name in "ab"]
    assert li.reading_twins(unbound("a", "b"), base) == []
    moving = base + [reading(name, "seven_day", value, "2026-10-10T14:00:00Z", at)
                     for name in "ab" for value, at in ((0.44, "2026-10-05T19:00:00Z"),
                                                        (0.45, "2026-10-05T20:00:00Z"))]
    assert [twin["lanes"] for twin in li.reading_twins(unbound("a", "b"), moving)] == [["a", "b"]]


@pytest.mark.parametrize("second", [
    reading("b", "seven_day", 0.99, "2026-10-10T15:00:00Z", "2026-10-05T18:00:00Z"),   # another reset
    reading("b", "seven_day", 0.99, "2026-10-10T14:00:00Z", "2026-10-05T18:11:00Z"),   # 11 minutes later
    reading("b", "seven_day", 0.97, "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z"),   # another value
    reading("b", "seven_day", 0.99, "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z", label="unknown"),
])
def test_c10_9_a_reading_that_differs_in_any_respect_does_not_match(second):
    rows = [reading("a", "seven_day", 0.99, "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z"),
            reading("a", "five_hour", 0.40, "2026-10-05T21:00:00Z", "2026-10-05T18:00:00Z"),
            reading("b", "five_hour", 0.40, "2026-10-05T21:00:00Z", "2026-10-05T18:00:00Z"), second]
    assert li.reading_twins(unbound("a", "b"), rows) == []


def test_c10_9_lanes_of_two_providers_are_never_compared():
    rows = d_id1_readings("claude-1", "codex-1")
    lanes = unbound("claude-1") + unbound("codex-1", provider="codex")
    assert li.reading_twins(lanes, rows) == []


def test_c10_9_the_thresholds_come_from_the_policy():
    settings_ = li.TwinSettings.from_policy({"alerts": {"twin_within_s": 30, "twin_min_values": 0.5,
                                                         "twin_reset_tolerance_s": True, "twin_utilization_tolerance": -1}})
    assert settings_ == li.TwinSettings(within_s=30.0, min_values=1)


def reference_values(lanes, readings, s):
    """C-10.9 by brute force: for each pair of lanes, every value a reading of one
    and a reading of the other agree on, comparing every reading with every other."""
    rows = {lane["lane_id"]: lane for lane in lanes if lane.get("enabled", True) and lane.get("owner", "v2") == "v2"}
    usable = [r for r in readings if r["lane_id"] in rows and r["label"] in ("provider", "stale-provider")
              and isinstance(r["utilization"], (int, float)) and 0 <= r["utilization"] <= 1
              and li._when(r["resets_at"]) is not None and li._when(r["observed_at"]) is not None]
    values = {}
    for x, y in itertools.combinations(usable, 2):
        if x["lane_id"] == y["lane_id"] or rows[x["lane_id"]]["provider"] != rows[y["lane_id"]]["provider"]:
            continue
        if (x["scope"], x["window"]) != (y["scope"], y["window"]):
            continue
        if (abs(li._when(x["observed_at"]) - li._when(y["observed_at"])) > s.within_s
                or abs(li._when(x["resets_at"]) - li._when(y["resets_at"])) > s.reset_tolerance_s
                or abs(x["utilization"] - y["utilization"]) > s.utilization_tolerance):
            continue
        pair = tuple(sorted((x["lane_id"], y["lane_id"])))
        values.setdefault(pair, set()).add((x["scope"], x["window"],
                                            round((x["utilization"] + y["utilization"]) / 2, 2)))
    return values


def reference_twins(lanes, readings, s):
    rows = {lane["lane_id"]: lane for lane in lanes if lane.get("enabled", True) and lane.get("owner", "v2") == "v2"}
    found = []
    for pair, agreed in sorted(reference_values(lanes, readings, s).items()):
        a, b = rows[pair[0]], rows[pair[1]]
        if a["provider"] == "claude" and li.is_identity(a.get("identity")) and li.is_identity(b.get("identity")):
            continue
        if li._twin_verdict(agreed, s):
            found.append(list(pair))
    return found


twin_lane = st.fixed_dictionaries({"identity": st.sampled_from([None, None, "org:o1", "org:o2"]),
                                   "provider": st.sampled_from(["claude", "claude", "codex"]),
                                   "enabled": st.sampled_from([1, 1, 0]), "owner": st.sampled_from(["v2", "v2", "v1"])})
twin_reading = st.tuples(st.integers(0, 4), st.sampled_from(["seven_day", "five_hour"]),
                         st.sampled_from([0.0, 0.4, 0.41, 0.43, 0.99, 1.0, 0.433]),
                         st.sampled_from(["2026-10-10T14:00:00Z", "2026-10-10T14:00:59Z", "2026-10-10T15:00:00Z",
                                          "2026-10-10T13:59:30+00:00", "not a time"]),
                         st.integers(0, 40), st.sampled_from(["provider", "stale-provider", "unknown"]))


def build(fleet, raw):
    lanes = [{"lane_id": f"l{index}", **entry} for index, entry in enumerate(fleet)]
    readings = [reading(f"l{index % max(len(lanes), 1)}", window, value, reset,
                        f"2026-10-05T18:{minute:02d}:00Z", label=label, rid=n)
                for n, (index, window, value, reset, minute, label) in enumerate(raw)]
    return lanes, readings


@PROPERTY
@given(st.lists(twin_lane, min_size=1, max_size=5), st.lists(twin_reading, max_size=40),
       st.randoms(use_true_random=False))
def test_c10_9_the_bucketed_detector_agrees_with_brute_force_and_is_order_free(fleet, raw, rnd):
    """C-10.9 differential: the bucketed sweep finds exactly the pairs a
    comparison of every reading with every other finds. Symmetric, never a lane
    with itself, one provider per pair, and the order of lanes and readings
    changes nothing."""
    lanes, readings = build(fleet, raw)
    s = li.TwinSettings()
    providers = {lane["lane_id"]: lane["provider"] for lane in lanes
                 if lane.get("enabled", True) and lane.get("owner", "v2") == "v2"}
    agreed = li._matches(li._samples(readings, providers), providers, s)
    assert {pair: values for pair, values in agreed.items()} == reference_values(lanes, readings, s)
    found = li.reading_twins(lanes, readings, s)
    assert [twin["lanes"] for twin in found] == reference_twins(lanes, readings, s)
    providers = {lane["lane_id"]: lane["provider"] for lane in lanes}
    for twin in found:
        a, b = twin["lanes"]
        assert a < b and providers[a] == providers[b] == twin["provider"]
    rnd.shuffle(lanes)
    rnd.shuffle(readings)
    assert li.reading_twins(lanes, readings, s) == found


@PROPERTY
@given(st.lists(twin_lane, min_size=1, max_size=4), st.lists(twin_reading, max_size=25),
       st.lists(twin_reading, max_size=10))
def test_c10_9_more_readings_never_unmake_a_twin(fleet, raw, more):
    """C-10.9 monotone: a pair found on some readings is still found when more
    readings are added; agreement once seen is not erased by later evidence."""
    lanes, readings = build(fleet, raw)
    _, extra = build(fleet, more)
    extra = [{**row, "reading_id": 1000 + index} for index, row in enumerate(extra)]
    before = {tuple(twin["lanes"]) for twin in li.reading_twins(lanes, readings)}
    after = {tuple(twin["lanes"]) for twin in li.reading_twins(lanes, readings + extra)}
    assert before <= after


def test_c10_9_resets_either_side_of_a_minute_still_agree():
    """C-10.9 two resets 30 s apart are one reset at the 60 s tolerance, though
    they fall in neighbouring buckets of the sweep."""
    rows = [reading("a", "seven_day", 0.99, "2026-10-10T13:59:30Z", "2026-10-05T18:00:00Z"),
            reading("b", "seven_day", 0.99, "2026-10-10T14:00:00Z", "2026-10-05T18:00:00Z"),
            reading("a", "five_hour", 0.40, "2026-10-05T21:00:00Z", "2026-10-05T18:00:00Z"),
            reading("b", "five_hour", 0.40, "2026-10-05T21:00:00Z", "2026-10-05T18:00:00Z")]
    assert [twin["lanes"] for twin in li.reading_twins(unbound("a", "b"), rows)] == [["a", "b"]]


def test_c10_9_one_reading_given_twice_is_one_reading():
    rows = d_id1_readings("a", "b")
    rows = [{**row, "reading_id": index} for index, row in enumerate(rows)]
    assert li.reading_twins(unbound("a", "b"), rows + rows) == li.reading_twins(unbound("a", "b"), rows)
