"""Whose credential a lane holds, judged across the fleet (C-1.4, C-10.6 to C-10.9).

Incident D-ID1 (2026-10-09): three Claude lanes, labelled max@maxghenis.com,
max@hivesight.ai and max.ghenis@gmail.com, all held tokens of one account. Their
readings matched to the minute, Subfleet counted that account three times, and the
other two accounts were unreachable. Nothing noticed, because a lane's setup token
(scope `user:inference`) cannot ask the profile endpoint who it is (403), so every
lane recorded no identity at all.

What a setup token can say about itself, observed that day
(`docs/decisions/2026-10-09-lane-identity.md`): `GET /v1/models` answers with an
`anthropic-organization-id` header naming the organization the token belongs to,
at no model cost. On a Pro or Max plan one account has one personal organization,
so that uuid names the account; seats of one Team organization share it. An email
is never in it. Only a full login's profile names email, account and organization
together.

So an identity here is one of two strings:

* `<account_uuid>:<org_uuid>`: what a profile answered (C-1.4), account-level;
* `org:<org_uuid>`: what a setup token's own response header answered,
  organization-level.

Everything here is pure (no store, no network, no clock) except `record`, which
keeps one finding on a lane row through the store it is handed. The daemon, the
timers and the doctor call it with rows they read.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contracts import IDENTITY_STATUS_BY_EVIDENCE, IdentityStatus

#: The prefix of an organization-level identity (`org:<org_uuid>`).
ORG_PREFIX = "org:"

#: An organization or account uuid as an endpoint gives it. A colon, above all,
#: is refused: it would split an identity in two.
_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")

#: Organization types whose organization has exactly one member (the profile's
#: `organization.organization_type`). A Team or Enterprise organization has many,
#: and an unknown type is not assumed to be personal: matching organizations then
#: proves and contradicts nothing.
PERSONAL_ORG_TYPES = frozenset({"claude_max", "claude_pro", "claude_free"})


# --- identity strings ---------------------------------------------------------


def _part(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _PART_RE.fullmatch(text) else None


def org_identity(org_uuid: Any) -> str | None:
    """`org:<org_uuid>`, or None when the value is not an organization id."""
    org = _part(org_uuid)
    return f"{ORG_PREFIX}{org}" if org else None


def account_identity(account_uuid: Any, org_uuid: Any) -> str | None:
    """`<account_uuid>:<org_uuid>` (C-1.4), or None unless both parts are ids."""
    account, org = _part(account_uuid), _part(org_uuid)
    return f"{account}:{org}" if account and org else None


def split_identity(identity: Any) -> tuple[str | None, str | None]:
    """`(account_uuid, org_uuid)`. The account is None for an organization-level
    identity; both are None for anything that is not an identity."""
    if not isinstance(identity, str):
        return None, None
    text = identity.strip()
    if text.startswith(ORG_PREFIX):
        return None, _part(text[len(ORG_PREFIX):])
    account, sep, org = text.partition(":")
    if not sep:
        return None, None
    account, org = _part(account), _part(org)
    return (account, org) if account and org else (None, None)


def identity_org(identity: Any) -> str | None:
    return split_identity(identity)[1]


def is_identity(identity: Any) -> bool:
    return split_identity(identity)[1] is not None


def account_level(identity: Any) -> bool:
    """Does this identity name an account, not only an organization?"""
    return split_identity(identity)[0] is not None


def same_account(a: Any, b: Any) -> bool:
    """C-10.7: are these one account, as far as both can tell?

    Two account-level identities are one account when they are equal. When either
    names only an organization, the organizations decide: on a personal plan the
    organization is the account. Not transitive across levels: two seats of one
    Team organization are different accounts, and an organization-level identity
    of that team matches both. Uuids compare without regard to case.
    """
    account_a, org_a = split_identity(a)
    account_b, org_b = split_identity(b)
    if not org_a or not org_b or org_a.casefold() != org_b.casefold():
        return False
    if account_a and account_b:
        return account_a.casefold() == account_b.casefold()
    return True


def short(identity: Any) -> str:
    """A display form: the organization's first 8 characters, `org:` kept."""
    account, org = split_identity(identity)
    if not org:
        return "-"
    return f"{ORG_PREFIX}{org[:8]}" if account is None else f"{account[:8]}:{org[:8]}"


# --- what profiles have said (C-10.6) ------------------------------------------


@dataclass(frozen=True)
class AccountFact:
    """One profile answer: this email is this account, in this kind of organization.

    Only the profile endpoint, asked with a full login, names an email and an
    account together, so only its answers are facts. A label an operator typed,
    a keychain item's name, a login folder's name and `~/.claude.json` are not.
    """

    email: str
    identity: str                   # account-level: "<account_uuid>:<org_uuid>"
    org_type: str | None
    source: str                     # "lane:<id>", "login:<folder>", "desktop"
    observed_at: str | None = None

    @property
    def personal(self) -> bool:
        return self.org_type in PERSONAL_ORG_TYPES


def _fact(email: Any, identity: Any, org_type: Any, source: str, at: Any) -> AccountFact | None:
    if not isinstance(email, str) or "@" not in email or not account_level(identity):
        return None
    return AccountFact(email.strip(), str(identity).strip(),
                       org_type.strip() if isinstance(org_type, str) and org_type.strip() else None,
                       source, at if isinstance(at, str) else None)


def account_facts(*, lanes: Iterable[Mapping[str, Any]] = (),
                  logins: Iterable[Mapping[str, Any]] = (),
                  desktop: Iterable[Mapping[str, Any]] = ()) -> tuple[AccountFact, ...]:
    """Every profile answer the fleet has kept, newest last within each source.

    * `lanes`: a Claude lane whose own profile bound it: `verified`, with an
      account-level identity and its label (enrolment records the profile's email
      as the label, C-1.4).
    * `logins`: `claude-cards.json` accounts (C-9.10), each read with a full
      login: `identity`, `email` and `plan.organization_type`.
    * `desktop`: the `desktop.identity` events' data (C-10.3): `identity`,
      `label`, `organization_type`.
    """
    facts: list[AccountFact] = []
    for lane in lanes:
        if lane.get("provider", "claude") != "claude" or lane.get("identity_status") != "verified":
            continue
        fact = _fact(lane.get("label"), lane.get("identity"), None,
                     f"lane:{lane.get('lane_id')}", lane.get("updated_at"))
        if fact:
            facts.append(fact)
    for login in logins:
        plan = login.get("plan") if isinstance(login.get("plan"), Mapping) else {}
        fact = _fact(login.get("email"), login.get("identity"), plan.get("organization_type"),
                     f"login:{login.get('login')}", login.get("read_at"))
        if fact:
            facts.append(fact)
    for row in desktop:
        fact = _fact(row.get("label"), row.get("identity"), row.get("organization_type"),
                     "desktop", row.get("observed_at"))
        if fact:
            facts.append(fact)
    return tuple(facts)


@dataclass(frozen=True)
class LabelVerdict:
    """What the facts say about a lane's label, given the identity its credential has.

    `proven`: a fact names this label's email for this account. `contradicted`: a
    fact names another email for this account. `unproven`: neither, or facts
    that disagree with each other (an email that moved accounts, say): a verdict
    a person must look at is never made from those.
    """

    verdict: str                     # "proven" | "contradicted" | "unproven"
    fact: AccountFact | None = None

    @property
    def proven(self) -> bool:
        return self.verdict == "proven"

    @property
    def contradicted(self) -> bool:
        return self.verdict == "contradicted"


UNPROVEN = LabelVerdict("unproven")


def _decides(fact: AccountFact, identity: str) -> bool:
    """Does this fact speak for the account behind `identity`? Account-level, when
    both name the account; organization-level only for a personal organization."""
    return same_account(fact.identity, identity) and (account_level(identity) or fact.personal)


def label_verdict(label: Any, identity: Any, facts: Iterable[AccountFact]) -> LabelVerdict:
    """C-10.6: is `label` the email of the account behind `identity`?"""
    email = label.strip().casefold() if isinstance(label, str) else ""
    if not email or not is_identity(identity):
        return UNPROVEN
    speaking = [fact for fact in facts if _decides(fact, str(identity))]
    proof = [fact for fact in speaking if fact.email.casefold() == email]
    contra = [fact for fact in speaking if fact.email.casefold() != email]
    newest = lambda rows: max(rows, key=lambda fact: (fact.observed_at or "", fact.source))  # noqa: E731
    if proof and not contra:
        return LabelVerdict("proven", newest(proof))
    if contra and not proof:
        return LabelVerdict("contradicted", newest(contra))
    return UNPROVEN


def judge(status: IdentityStatus | None, *, label: Any, identity: Any,
          facts: Iterable[AccountFact]) -> tuple[IdentityStatus | None, LabelVerdict]:
    """C-10.6: the adapter's finding, with the fleet's profile facts applied.

    The adapter compares a credential with the lane's own record and, when the
    credential can read its profile, with the label; it cannot know what other
    logins' profiles said. This adds that, for a lane its own credential could
    not prove (`enrolled`): a fact naming the label proves it (`verified`), a
    fact naming another email contradicts it (`mismatch`). `verified` from the
    credential's own profile is not overruled by older facts about others, and
    `mismatch` and `unverified` stand as the adapter found them.
    """
    if status is not IdentityStatus.ENROLLED or not is_identity(identity):
        return status, UNPROVEN
    verdict = label_verdict(label, identity, facts)
    if verdict.proven:
        return IdentityStatus.VERIFIED, verdict
    if verdict.contradicted:
        return IdentityStatus.MISMATCH, verdict
    return status, verdict


BINDING_STATUSES = (IdentityStatus.VERIFIED, IdentityStatus.ENROLLED)

#: The event a change of a lane's recorded identity or status leaves (C-10.6).
IDENTITY_EVENT = "lane.identity"


def observed_identity(finding: Mapping[str, Any] | None) -> str | None:
    """The identity an adapter's finding (`IdentityCheck.evidence()`) observed:
    `observed` when it says, else the account and organization it lists."""
    if not finding:
        return None
    observed = finding.get("observed")
    if is_identity(observed):
        return str(observed).strip()
    named = finding.get("identity") if isinstance(finding.get("identity"), Mapping) else {}
    return account_identity(named.get("account_uuid"), named.get("org_uuid"))


def record(store: Any, lane_id: str, finding: Mapping[str, Any] | None,
           facts: Iterable[AccountFact] = ()) -> bool:
    """C-10.6: keep one identity finding on the lane row; may its readings count?

    The adapter decides what the credential said; this remembers it, so the
    scheduler can refuse a lane whose credential holds another account and an
    operator can see why. A lane that recorded no identity learns the one its
    credential answered with, once, when that answer binds it (C-1.4): an
    account from its profile, or, for a setup token, its organization (D-ID1).
    Profile facts about other logins then judge the label (`judge`).

    A lane already `mismatch` stays so, whatever any later answer says; only
    re-enrolment releases it (C-10.6), and its readings never count. A change of
    status or identity leaves a `lane.identity` event. Returns whether readings
    from the same run may be stored as the lane's capacity.
    """
    status = IDENTITY_STATUS_BY_EVIDENCE.get(str((finding or {}).get("status") or ""))
    if status is None:
        return True
    row = store.one("SELECT identity,label,identity_status FROM lanes WHERE lane_id=?", (lane_id,))
    if row is None:
        return status in BINDING_STATUSES
    if row["identity_status"] == IdentityStatus.MISMATCH.value:
        return False
    identity, label = row["identity"], row["label"]
    observed = observed_identity(finding)
    values: dict[str, Any] = {}
    if not identity and observed and status in BINDING_STATUSES:
        values["identity"] = identity = observed
        email = ((finding or {}).get("identity") or {}).get("email")
        if isinstance(email, str) and email.strip() and not label:
            values["label"] = label = email.strip()
    final, verdict = judge(status, label=label, identity=identity, facts=facts)
    if final is not None and row["identity_status"] != final.value:
        values["identity_status"] = final.value
    if values:
        store.update_lane(lane_id, **values)
        store.add_event(IDENTITY_EVENT, lane_id=lane_id, data={
            "from": row["identity_status"], "to": final.value if final else None,
            "identity": identity, "observed": observed, "source": (finding or {}).get("source"),
            "label": label, "verdict": verdict.verdict,
            "fact": ({"email": verdict.fact.email, "identity": verdict.fact.identity,
                      "source": verdict.fact.source} if verdict.fact else None)})
    return final in BINDING_STATUSES


# --- one account, one candidate (C-10.8) --------------------------------------


def _eligible(lane: Mapping[str, Any]) -> bool:
    return (lane.get("provider") == "claude" and bool(lane.get("enabled", True))
            and lane.get("owner", "v2") == "v2" and lane.get("identity_status") != "mismatch"
            and is_identity(lane.get("identity")))


def _lane_order(lane_id: Any) -> tuple:
    """C-1.3: `<provider>-<n>` in the order its numbers were given out (claude-4
    before claude-19); anything else after, by name."""
    prefix, _, number = str(lane_id).rpartition("-")
    return (0, prefix, int(number), "") if prefix and number.isdigit() else (1, "", 0, str(lane_id))


def _rank(lane: Mapping[str, Any]) -> tuple:
    """The lane that speaks for a shared account: a proven label first, then the
    earliest binding (C-23.45's binding order: enrolment time, then lane number)."""
    return (lane.get("identity_status") != "verified", str(lane.get("created_at") or ""),
            _lane_order(lane.get("lane_id")))


def shadowing(lanes: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """C-10.8: for each Claude lane whose credential is one account with another
    lane's, the lanes it shares with and the one that speaks for them.

    Only enabled, v2-owned lanes with a recorded identity that is not mismatched
    take part. In rank order (`_rank`), a lane that shares an account with an
    earlier lane that is still a candidate is shadowed by the first of those, and
    is not a candidate. So no two candidates are one account, and every eligible
    lane is a candidate or shares its account with an earlier candidate. Shadowing
    only by candidates matters where `same_account` is not transitive: an
    organization-level lane of a Team organization matches two seats, and must
    not leave the second seat with no candidate. The answer does not depend on
    the order of `lanes`.
    """
    eligible = sorted((lane for lane in lanes if _eligible(lane)), key=_rank)
    answer: dict[str, dict[str, Any]] = {}
    candidates: list[Mapping[str, Any]] = []
    for lane in eligible:
        shared = [other for other in eligible
                  if other is not lane and same_account(lane["identity"], other["identity"])]
        ahead = next((other for other in candidates
                      if same_account(lane["identity"], other["identity"])), None)
        if ahead is None:
            candidates.append(lane)
        if shared:
            answer[str(lane["lane_id"])] = {
                "shared_with": sorted(str(other["lane_id"]) for other in shared),
                "shadowed_by": str(ahead["lane_id"]) if ahead is not None else None,
            }
    return answer


def mark_shared(lanes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put `identity_shared_with` and `identity_shadowed_by` on each lane row of a
    view (and take stale ones off); returns the rows."""
    rows = list(lanes)
    found = shadowing(rows)
    for lane in rows:
        entry = found.get(str(lane.get("lane_id")))
        if entry:
            lane["identity_shared_with"] = entry["shared_with"]
            lane["identity_shadowed_by"] = entry["shadowed_by"]
        else:
            lane.pop("identity_shared_with", None)
            lane.pop("identity_shadowed_by", None)
    return rows


def identity_shadowed(lane: Mapping[str, Any]) -> bool:
    """C-10.8: is another lane the candidate for this lane's account?"""
    return bool(lane.get("identity_shadowed_by"))


def shared_groups(lanes: Iterable[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Each speaking lane with the lanes it shadows, speaking lane first."""
    rows = {str(lane.get("lane_id")): lane for lane in lanes}
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for lane_id, entry in sorted(shadowing(rows.values()).items()):
        if entry["shadowed_by"]:
            groups[entry["shadowed_by"]].append(rows[lane_id])
    return [[rows[head], *sorted(members, key=_rank)] for head, members in sorted(groups.items())]


# --- readings that match too well (C-10.9) ------------------------------------


@dataclass(frozen=True)
class TwinSettings:
    """C-10.9's thresholds; `alerts.twin_*` in the policy overrides each."""

    within_s: float = 600.0              # two readings observed this close in time
    reset_tolerance_s: float = 60.0      # report one reset to this tolerance
    utilization_tolerance: float = 0.005  # and one utilization to this tolerance
    min_values: int = 3                  # distinct values one window must match on alone

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any] | None) -> "TwinSettings":
        alerts = (policy or {}).get("alerts") or {}
        values = {}
        for field, key in (("within_s", "twin_within_s"), ("reset_tolerance_s", "twin_reset_tolerance_s"),
                           ("utilization_tolerance", "twin_utilization_tolerance"),
                           ("min_values", "twin_min_values")):
            value = alerts.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
                values[field] = max(1, int(value)) if field == "min_values" else float(value)
        return cls(**values)


def _when(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class _Sample:
    lane_id: str
    scope: str
    window: str
    utilization: float
    reset: float
    observed: float


def _samples(readings: Iterable[Mapping[str, Any]], lanes: Mapping[str, str]) -> list[_Sample]:
    seen: set = set()
    found: list[_Sample] = []
    for row in readings:
        lane_id = str(row.get("lane_id"))
        if lane_id not in lanes or row.get("label") not in ("provider", "stale-provider"):
            continue
        utilization = row.get("utilization")
        if (not isinstance(utilization, (int, float)) or isinstance(utilization, bool)
                or not math.isfinite(utilization) or not 0 <= utilization <= 1):
            continue
        reset, observed = _when(row.get("resets_at")), _when(row.get("observed_at"))
        if reset is None or observed is None or not row.get("window") or not row.get("scope"):
            continue
        key = (lane_id, row.get("reading_id"), row.get("scope"), row.get("window"),
               float(utilization), reset, observed)
        if key in seen:
            continue
        seen.add(key)
        found.append(_Sample(lane_id, str(row["scope"]), str(row["window"]), float(utilization), reset, observed))
    return found


def _matches(samples: list[_Sample], providers: Mapping[str, str],
             settings: TwinSettings) -> dict[tuple[str, str], set[tuple[str, str, float]]]:
    """For each pair of lanes, the (scope, window, utilization) values on which a
    reading of one and a reading of the other agree: observed within
    `within_s`, resets within `reset_tolerance_s`, utilization within
    `utilization_tolerance`. Readings are bucketed by provider, scope, window and
    reset, so lanes whose resets differ are never compared."""
    width = max(settings.reset_tolerance_s, 1.0)
    buckets: dict[tuple, list[_Sample]] = defaultdict(list)
    for sample in samples:
        buckets[(providers[sample.lane_id], sample.scope, sample.window,
                 math.floor(sample.reset / width))].append(sample)
    pairs: dict[tuple[str, str], set[tuple[str, str, float]]] = defaultdict(set)
    for (provider, scope, window, slot), members in buckets.items():
        # A reset within tolerance lies in this bucket or the next one up; each
        # pair of neighbouring buckets is compared once, from the lower.
        candidates = sorted(members + buckets.get((provider, scope, window, slot + 1), []),
                            key=lambda sample: sample.observed)
        own = {id(sample) for sample in members}
        for i, first in enumerate(candidates):
            for second in candidates[i + 1:]:
                if second.observed - first.observed > settings.within_s:
                    break
                if first.lane_id == second.lane_id:
                    continue
                if id(first) not in own and id(second) not in own:
                    continue        # both in the upper bucket: compared from there
                if abs(first.reset - second.reset) > settings.reset_tolerance_s:
                    continue
                if abs(first.utilization - second.utilization) > settings.utilization_tolerance:
                    continue
                key = tuple(sorted((first.lane_id, second.lane_id)))
                # Whole percents, the providers' own resolution: two agreements
                # within tolerance of each other are one value, not two.
                value = round((first.utilization + second.utilization) / 2, 2)
                pairs[key].add((scope, window, value))
    return pairs


def _twin_verdict(values: set[tuple[str, str, float]], settings: TwinSettings) -> bool:
    """Do these agreements say one account rather than coincidence?

    Weekly resets are whole hours and utilization moves in whole percents, so one
    agreement on one window is easily a coincidence. Either two windows agree
    (the account's five-hour and weekly windows both), or one window agrees on
    `min_values` distinct values (two accounts moving in step). Either way at least
    one agreed value lies strictly between 0 and 1: two exhausted or two untouched
    accounts agree without being one.
    """
    if not any(0 < value < 1 for _, _, value in values):
        return False
    windows = {(scope, window) for scope, window, _ in values}
    if len(windows) >= 2:
        return True
    return len(values) >= settings.min_values


def reading_twins(lanes: Iterable[Mapping[str, Any]], readings: Iterable[Mapping[str, Any]],
                  settings: TwinSettings | None = None) -> list[dict[str, Any]]:
    """C-10.9: pairs of enabled lanes whose provider readings match too well to be
    two accounts, with the values they matched on.

    A pair whose Claude identities are both known is left out: one account is
    C-10.8's finding, two accounts cannot match. Only lanes of one provider are
    compared. Symmetric, never a lane with itself, and independent of the order
    of `lanes` and `readings`.
    """
    settings = settings or TwinSettings()
    rows = {str(lane["lane_id"]): lane for lane in lanes
            if lane.get("enabled", True) and lane.get("owner", "v2") == "v2" and lane.get("provider")}
    providers = {lane_id: str(lane["provider"]) for lane_id, lane in rows.items()}
    found = []
    for (first, second), values in sorted(_matches(_samples(readings, providers), providers, settings).items()):
        a, b = rows[first], rows[second]
        if (a.get("provider") == "claude" and is_identity(a.get("identity"))
                and is_identity(b.get("identity"))):
            continue
        if not _twin_verdict(values, settings):
            continue
        found.append({"lanes": [first, second], "provider": providers[first],
                      "values": sorted([{"scope": scope, "window": window, "utilization": value}
                                        for scope, window, value in values],
                                       key=lambda row: (row["scope"], row["window"], row["utilization"]))})
    return found
