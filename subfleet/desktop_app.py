"""Which Claude account the desktop app is on (C-10.3, C-10.9).

Two signals, both the app's own:

* **The hook's report.** A Claude Code session the desktop app starts carries
  `CLAUDE_CODE_ENTRYPOINT=claude-desktop` and three names for the account it
  started on: `CLAUDE_CODE_USER_EMAIL`, `CLAUDE_CODE_ACCOUNT_UUID` and
  `CLAUDE_CODE_ORGANIZATION_UUID` (checked from inside a desktop session on
  2026-11-08). `subfleet hook SessionStart` and `UserPromptSubmit` run in those
  sessions and send the three values, with the session and the time, as
  `desktop.report`; the daemon records them as `desktop.account` events. The
  three always describe one account, so each event is a true link between an
  org and an email, whenever its session started.
* **The app's usage file**, `~/Library/Application Support/Claude/
  plan-usage-history.json` (`{"version": 2, "samples": [{"t": <ms>, "org":
  <org uuid>, "u": {"sd": <weekly percent>}}]}`). The app writes a sample when it
  moves to another account, so its newest sample names the org it is on now,
  before any session restarts.

A Claude lane records its account in its key, in one of two forms
(`adapters/claude.py`, C-1.4): a lane enrolled with profile scope records its
identity, `claude:<account uuid>:<org uuid>`; one enrolled without (no profile
scope, or a profile that did not answer) records the email its keychain item
or its home's `.claude.json` gives, `claude:<email>`. An event's email joins
the app's org to the email-keyed lanes, its account uuid and org to the
uuid-keyed ones, and the usage file's org to the uuid-keyed ones too. Nothing
here is inferred.

This module holds the shapes and the pure rules; `capacity.mark_desktop` lays
them over lane rows and the daemon reads the store and the file. It imports no
other Subfleet module but the file reader and `home_paths`, so the hook can
validate a report without loading the scheduler.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: C-10.9: the event a hook's report becomes (`Daemon._desktop_report`).
DESKTOP_ACCOUNT_EVENT = "desktop.account"
#: C-10.9: the protocol verb the hook sends. Not in `daemon.LOOKUP_OPS`: it writes.
REPORT_OP = "desktop.report"
#: C-10.9: a report naming the same account as the newest event is written again
#: only once that event is older than this, so a busy session writes one an hour.
REFRESH_S = 3600
#: C-10.9: how many of the newest `desktop.account` rows the newest events are
#: read from, on the events kind index. Each write adds two rows (the event and
#: its audit row). An org's link never falls out of it: the newest event of each
#: (org, email) pair is kept beside it (`newest_links`).
EVENT_ROWS = 4000
#: C-10.9: the hook waits at most this long for the daemon's reply. After a
#: timeout the client takes one look at the lock's holder, bounded by
#: `client.HOLDER_PROBE_BUDGET_S` (5 s), before the hook goes on.
REPORT_TIMEOUT_S = 1.0
#: C-10.9: the entry point the desktop app stamps on the sessions it starts.
DESKTOP_ENTRYPOINT = "claude-desktop"
ENTRYPOINT_NAME = "CLAUDE_CODE_ENTRYPOINT"
#: C-10.9: the only names the report reads, besides the launch markers and the
#: entry point. Never any other: the report carries exactly these three values.
ACCOUNT_NAMES = ("CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID")
#: C-10.9: the app's usage file is read through the regular-file reader, at most this much.
APP_USAGE_LIMIT = 4 * 1024 * 1024
#: C-10.9: the only usage-file version read; any other is no answer.
APP_USAGE_VERSION = 2
#: C-10.9: a usage sample stamped later than the clock by more than this is
#: left out, as one without a usable `t` (one clock, one machine).
SAMPLE_AHEAD_S = 300
#: C-10.9: the two alerts' patience, in seconds.
ALERT_AFTER_S = 600
EMAIL_MAX = 254

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z", re.ASCII)
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)


# --- validation (the hook's and the daemon's alike) --------------------------

def canonical_uuid(value: Any) -> str | None:
    """A uuid in its canonical 8-4-4-4-12 form, lower case; None for anything else."""
    if not isinstance(value, str) or not _UUID.match(value):
        return None
    return value.lower()


#: C-10.9: the Unicode categories an email never holds: control (Cc), format
#: (Cf) and a lone surrogate (Cs, what `os.environ` gives for a non-UTF-8 byte).
EMAIL_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


def valid_email(value: Any) -> str | None:
    """An email with exactly one `@`, a part on each side, at most 254
    characters, that NFKC leaves unchanged, and with no whitespace, control
    (Cc), format (Cf) or surrogate (Cs) character; None for anything else."""
    if not isinstance(value, str) or not value or len(value) > EMAIL_MAX:
        return None
    if any(ch.isspace() or unicodedata.category(ch) in EMAIL_REFUSED_CATEGORIES for ch in value):
        return None
    if unicodedata.normalize("NFKC", value) != value or value.count("@") != 1:
        return None
    local, _, domain = value.partition("@")
    return value if local and domain else None


def valid_session(value: Any) -> str | None:
    """A Claude Code session id as the hook payload carries it (a token of at most 128 characters)."""
    return value if isinstance(value, str) and _SESSION.match(value) else None


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        # A stamp a few hours inside the calendar's ends overflows it here.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def account_report(email: Any, account_uuid: Any, org_uuid: Any, session_id: Any,
                   observed_at: Any, *, now: datetime | None = None) -> dict[str, str] | None:
    """C-10.9: the report, validated and normalised, or None when any value is
    missing or malformed. With `now`, an `observed_at` later than it is taken as
    `now` (one clock, one machine: a later stamp can only be a wrong one)."""
    values = (valid_email(email), canonical_uuid(account_uuid), canonical_uuid(org_uuid),
              valid_session(session_id), _instant(observed_at))
    if any(value is None for value in values):
        return None
    email, account, org, session, at = values
    if now is not None and at > now:
        at = now
    return {"email": email, "account_uuid": account, "org_uuid": org, "session_id": session,
            "observed_at": iso(at)}


def report_from_environment(env: Mapping[str, Any], session_id: Any, *,
                            now: datetime | None = None) -> dict[str, str] | None:
    """C-10.9: the hook's report from a desktop session's environment, or None.

    Reads the entry point and `ACCOUNT_NAMES` and nothing else; the caller has
    already ruled out a Subfleet launch (C-26.13)."""
    if env.get(ENTRYPOINT_NAME) != DESKTOP_ENTRYPOINT:
        return None
    email, account, org = (env.get(name) for name in ACCOUNT_NAMES)
    return account_report(email, account, org, session_id, iso(now or datetime.now(timezone.utc)))


# --- the app's usage file ----------------------------------------------------

@dataclass(frozen=True)
class AppSample:
    """C-10.9: the newest sample of the app's usage file."""

    org: str                       # the org the app is on (canonical uuid)
    at: str                        # the sample's time, ISO 8601 UTC
    weekly: float | None           # the app's weekly percent (`u.sd`), or None
    since: str                     # the first sample of the newest run naming `org`

    @property
    def instant(self) -> datetime:
        return _instant(self.at)          # type: ignore[return-value]


def _sample_time(value: Any) -> datetime | None:
    # `type(...) is int`: never a bool, a float or an int subclass.
    if type(value) is not int:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _percent(value: Any) -> float | None:
    """A weekly percent: a finite number from 0 to 100 as a float, else None.
    Never a bool, NaN, an infinity, or an integer too large for a float (C-10.9)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def parse_app_usage(data: bytes, *, now: datetime | None = None) -> AppSample | None:
    """C-10.9: the newest sample of a version-2 usage file, or None.

    Raises ValueError for a file that is not JSON, not an object, or another
    version (`version` must be the integer 2). A sample without an integer `t`
    or a canonical `org`, or, with `now`, stamped more than `SAMPLE_AHEAD_S`
    after it, is left out; with none left there is no answer. A `u.sd` that is
    no finite number from 0 to 100 is weekly None. Newest is the greatest `t`;
    equal times break by org, so the answer never depends on the order samples
    are listed in."""
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    version = value.get("version")
    if type(version) is not int or version != APP_USAGE_VERSION:
        raise ValueError("unsupported version")
    samples = value.get("samples")
    if not isinstance(samples, list):
        raise ValueError("no sample list")
    found: list[tuple[int, str, float | None]] = []
    for item in samples:
        if not isinstance(item, dict):
            continue
        org, when = canonical_uuid(item.get("org")), _sample_time(item.get("t"))
        if org is None or when is None:
            continue
        if now is not None and (when - now).total_seconds() > SAMPLE_AHEAD_S:
            continue
        usage = item.get("u") if isinstance(item.get("u"), dict) else {}
        found.append((item["t"], org, _percent(usage.get("sd"))))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], row[1], -1.0 if row[2] is None else row[2]))
    newest_t, org, weekly = found[-1]
    start = newest_t
    for t, other, _ in reversed(found):
        if other != org:
            break
        start = t
    return AppSample(org, iso(_sample_time(newest_t)), weekly, iso(_sample_time(start)))   # type: ignore[arg-type]


#: The last usage file looked at: (path, what `stat` said of it or "missing", the answer).
_app_seen: tuple[str, Any, AppSample | None] | None = None
_app_lock = threading.Lock()


def forget_app_usage() -> None:
    """Drop the remembered usage file; the next read parses it again."""
    global _app_seen
    with _app_lock:
        _app_seen = None


def app_usage_path() -> Path:
    from .home_paths import desktop_usage_path
    return desktop_usage_path()


def read_app_usage(path: str | Path | None = None, *,
                   log: Callable[[str], Any] | None = None) -> AppSample | None:
    """C-10.9: the app's newest usage sample, or None.

    The file is looked at on every call and read only when `stat` says it has
    changed, through the regular-file reader with a 4 MiB cap (never a FIFO,
    never waiting in open). A missing file, another version, a parse error or
    anything else that fails in the parse is no answer, recorded for that
    state of the file and logged once per change of it: nothing raises out of
    here, so no file can stop admission (review r1 of #318, P1 1)."""
    global _app_seen
    from .sessions.transcripts import read_regular
    target = Path(path) if path is not None else app_usage_path()
    try:
        found = os.stat(target)
        state: Any = (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns)
    except FileNotFoundError:
        state = "missing"
    except OSError as exc:
        state = ("unreadable", type(exc).__name__)
    with _app_lock:
        kept = _app_seen
    if kept is not None and kept[:2] == (str(target), state):
        return kept[2]
    result: AppSample | None = None
    why = None
    if state == "missing":
        why = "missing"
    elif isinstance(state, tuple) and state and state[0] == "unreadable":
        why = f"unreadable ({state[1]})"
    else:
        try:
            result = parse_app_usage(read_regular(target, APP_USAGE_LIMIT), now=datetime.now(timezone.utc))
            if result is None:
                why = "no usable sample"
        except Exception as exc:                        # noqa: BLE001 - a file is never worth an admission pass
            result, why = None, f"{type(exc).__name__}"
    with _app_lock:
        _app_seen = (str(target), state, result)
    if why is not None and log is not None:
        try:
            log(f"desktop usage file {target}: {why}; no answer from it (C-10.9)")
        except Exception:                               # noqa: BLE001 - logging never decides
            pass
    return result


# --- the hook's reports, as recorded -----------------------------------------

@dataclass(frozen=True)
class AccountEvent:
    """One `desktop.account` event: a true link between an org and an email."""

    event_id: int
    org: str
    email: str
    account_uuid: str
    session_id: str
    observed_at: str

    @property
    def order(self) -> tuple[datetime, int]:
        return (_instant(self.observed_at), self.event_id)        # type: ignore[return-value]


def account_events(rows: Iterable[Mapping[str, Any]]) -> list[AccountEvent]:
    """C-10.9: the rows that carry a valid report (each write's audit row carries none)."""
    events = []
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
            event_id = int(row["event_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        report = account_report(data.get("email"), data.get("account_uuid"), data.get("org_uuid"),
                                data.get("session_id"), data.get("observed_at"))
        if report is None:
            continue
        events.append(AccountEvent(event_id, report["org_uuid"], report["email"], report["account_uuid"],
                                   report["session_id"], report["observed_at"]))
    return events


def newest(events: Iterable[AccountEvent]) -> AccountEvent | None:
    """The newest event: the latest `observed_at`, equal times broken by event id."""
    return max(events, key=lambda event: event.order, default=None)


def newest_links(events: Iterable[AccountEvent],
                 kept: Mapping[tuple[str, str], AccountEvent] | None = None) -> dict[tuple[str, str], AccountEvent]:
    """C-10.9: the newest event of each (org, email lower-cased) pair, over
    `kept` and `events`.

    `resolve` gives the same org, email, account and "linked elsewhere" set
    over these as over every event they came from: the newest event of all,
    and the newest naming any org, is the newest of its pair, and every pair is
    here. So an org's link never falls out of a row window (review r1 of
    #318, P2 2). `disagree_since` and `should_record` read the newest events."""
    links = dict(kept or {})
    for event in events:
        pair = (event.org, event.email.lower())
        if pair not in links or event.order > links[pair].order:
            links[pair] = event
    return links


def with_links(window: Iterable[AccountEvent], links: Mapping[tuple[str, str], AccountEvent]) -> list[AccountEvent]:
    """C-10.9: what a decision reads: the newest events (a row window) and the
    newest event of each (org, email) pair, each event once, by event id."""
    found = {event.event_id: event for event in window}
    found.update((event.event_id, event) for event in links.values())
    return [found[event_id] for event_id in sorted(found)]


def should_record(report: Mapping[str, str], events: Iterable[AccountEvent], *, now: datetime) -> bool:
    """C-10.9: write the report only when its email (case aside), account or org
    differs from the newest event's, or that event is over `REFRESH_S` old.
    Against the newest event only, so two sessions on different accounts that
    alternate prompts write one event per prompt."""
    last = newest(events)
    if last is None:
        return True
    same = (last.email.lower() == report["email"].lower()
            and last.account_uuid == report["account_uuid"] and last.org == report["org_uuid"])
    return not same or (now - _instant(last.observed_at)).total_seconds() > REFRESH_S   # type: ignore[operator]


# --- C-10.3's rule -----------------------------------------------------------

def lane_account(lane: Mapping[str, Any]) -> tuple[str, ...] | None:
    """C-10.3: the account a Claude lane's key records, by its kind.

    `("email", <email, lower-cased>)` for `claude:<email>`, and `("uuid",
    <account uuid>, <org uuid>)`, lower-cased, for `claude:<account uuid>:<org
    uuid>`, the key of a lane enrolled with profile scope (C-1.4). None for a
    lane of another provider (only a Claude lane is ever the desktop app's) or
    a key of neither form. `owns` and the "linked elsewhere" test both read it.
    Case aside is `lower()`: `casefold` joins distinct addresses."""
    if lane.get("provider", "claude") != "claude":
        return None
    prefix, _, tail = str(lane.get("account_key") or "").partition(":")
    tail = tail.strip()
    if prefix.lower() != "claude" or not tail:
        return None
    if "@" in tail:
        return ("email", tail.lower())
    account, _, org = tail.partition(":")
    account, org = canonical_uuid(account), canonical_uuid(org)
    return ("uuid", account, org) if account and org else None


@dataclass(frozen=True)
class AppAccount:
    """C-10.3: the desktop app's account, as its own signals give it.

    `org` is the newest usage sample's org when the file answers, else the org
    of the newest `desktop.account` event. `email` and `account` are the email
    and account uuid of the newest event naming `org`, at any age (the daemon
    keeps the newest event of each (org, email) pair, `newest_links`), or None
    when none does. `elsewhere` holds every email, lower-cased, an event links
    to another org and none to `org`."""

    org: str
    source: str                                   # "app-file" | "hook"
    email: str | None
    elsewhere: frozenset[str] = frozenset()
    sample: AppSample | None = None
    event: AccountEvent | None = None             # the newest event of all
    linked_at: str | None = None                  # the newest event naming `org`
    linked: int = 0                               # how many orgs the events link
    disagree_since: str | None = None             # `disagree_since(sample, events)`
    account: str | None = None                    # the account uuid of the newest event naming `org`

    def owns(self, lane: Mapping[str, Any]) -> bool:
        """C-10.3: is this Claude lane the app's account, by what its key records?

        An email-keyed lane (`claude:<email>`) when its email is `email`, case
        aside. A uuid-keyed lane (`claude:<account uuid>:<org uuid>`) when its org
        is `org` and, when an event names `org` (so `account` is known), its
        account is `account`: the usage file names an org and no account."""
        found = lane_account(lane)
        if found is None:
            return False
        if found[0] == "email":
            return self.email is not None and found[1] == self.email.lower()
        return found[2] == self.org and (self.account is None or found[1] == self.account)

    def possible(self, lane: Mapping[str, Any]) -> bool:
        """With no email for `org`, every email-keyed Claude lane not linked
        elsewhere. Never a uuid-keyed lane: its key names its org, so it is the
        app's (`owns`) or linked elsewhere."""
        found = lane_account(lane)
        return self.email is None and found is not None and found[0] == "email" and found[1] not in self.elsewhere

    def summary(self, lanes: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """What `status`, `status.json` and the alerts show of it: the org's
        8-character prefix, never the whole uuid."""
        owned = sorted(str(lane["lane_id"]) for lane in lanes
                       if lane.get("provider") == "claude" and self.owns(lane))
        return {"org_prefix": self.org[:8], "source": self.source,
                "linked": self.email is not None, "lanes": owned,
                "app_file": ({"org_prefix": self.sample.org[:8], "at": self.sample.at, "since": self.sample.since,
                              "weekly_percent": self.sample.weekly} if self.sample else None),
                "hook": ({"org_prefix": self.event.org[:8], "at": self.event.observed_at,
                          "event_id": self.event.event_id, "disagree_since": self.disagree_since}
                         if self.event else None),
                "linked_at": self.linked_at, "linked_orgs": self.linked}


def resolve(sample: AppSample | None, events: Iterable[AccountEvent]) -> AppAccount | None:
    """C-10.3, C-10.9: the app's account from its two signals, or None when neither
    answers (then C-10.3's profile and hint rule stands exactly as before).

    Pure and order-free: events are taken by (`observed_at`, event id), never by
    the order given."""
    events = list(events)
    last = newest(events)
    if sample is not None:
        org, source = sample.org, "app-file"
    elif last is not None:
        org, source = last.org, "hook"
    else:
        return None
    mine = [event for event in events if event.org == org]
    link = newest(mine)
    own = {event.email.lower() for event in mine}
    other = {event.email.lower() for event in events if event.org != org}
    return AppAccount(org, source, link.email if link else None, frozenset(other - own), sample, last,
                      link.observed_at if link else None, len({event.org for event in events}),
                      disagree_since(sample, events), account=link.account_uuid if link else None)


def alert_conditions(summary: Mapping[str, Any] | None, *, in_use: Any, now: datetime) -> list[dict[str, Any]]:
    """C-10.9: the two warnings, from `AppAccount.summary` as `status.json` carries it.

    * The app's org has stayed unlinked for `ALERT_AFTER_S` while the desktop
      login is in use (`in_use` not False: unknown is use, as C-10.3 reads it),
      and no lane's key names it (a uuid-keyed lane in that org is `desktop`).
    * The newest event, newer than the file's newest sample, has named another
      org than the file for `ALERT_AFTER_S`."""
    if not summary:
        return []
    found = []
    sample, hook = summary.get("app_file"), summary.get("hook")
    since = _instant((sample or {}).get("since")) if summary.get("source") == "app-file" else None
    if (not summary.get("linked") and not summary.get("lanes") and in_use is not False and since is not None
            and (now - since).total_seconds() >= ALERT_AFTER_S):
        found.append({
            "key": f"desktop-app-unlinked:{summary['org_prefix']}", "severity": "warn",
            "subject": "claude: the desktop app's account is not linked to a lane",
            "body": (f"The Claude desktop app has been on org {summary['org_prefix']} since {sample['since']}, "
                     "and no desktop session has reported its account yet, so every Claude lane not known to be "
                     "elsewhere sorts last (desktop_possible) and none is kept off. Open or restart a session in "
                     "the desktop app; if none reports, run: subfleet daemon install --hooks"),
            "home": "desktop-app"})
    onset = _instant((hook or {}).get("disagree_since"))
    if sample and onset is not None and (now - onset).total_seconds() >= ALERT_AFTER_S:
        found.append({
            "key": f"desktop-app-disagree:{hook['org_prefix']}", "severity": "warn",
            "subject": "claude: a desktop session reports another account than the app's usage file",
            "body": (f"Since {hook['disagree_since']} desktop sessions have reported org {hook['org_prefix']}, "
                     f"newer than the usage file's last sample ({sample['at']}, org {sample['org_prefix']}). "
                     "Routing follows the file. If the app moved accounts, the file has stopped recording it; "
                     "check Settings, Usage in the app."),
            "home": "desktop-app"})
    return found


def disagree_since(sample: AppSample | None, events: Iterable[AccountEvent]) -> str | None:
    """C-10.9: when the newest events began to name another org than the file,
    counting only events newer than the file's newest sample; None when the
    newest event agrees with the file or is not newer than it."""
    if sample is None:
        return None
    file_at = sample.instant
    later = sorted((event for event in events if event.order[0] > file_at), key=lambda event: event.order)
    if not later or later[-1].org == sample.org:
        return None
    onset = later[-1]
    for event in reversed(later):
        if event.org != onset.org:
            break
        onset = event
    return onset.observed_at
