"""Move one account between v1 and v2 ownership (plan amendment 8).

`docs/migration.md` principle 1: "During the shadow period every account has
`owner: v1` or `owner: v2` in `lanes.json`; v2 never dispatches, probes for
dispatch, redeems, or keeps alive on a v1-owned account, and v1's roster loses an
account the moment v2 takes it." Shadow week step 6: "a transfer records an
`events` row and edits both rosters in one step."

So one transfer does three things:

1. flips `owner` on the `lanes` row (C-10.4),
2. writes the `events` row in the same transaction (C-3.2),
3. edits both rosters: v2's `lanes.json`, so a rebuilt store re-seeds with the
   right owner, and the v1 roster file the manifest names for that provider.

The v1 edit is the only write to a v1 file in this repository. It is refused
unless the caller passes `--i-understand-v1-edit`, it copies the file to
`<name>.bak-<utc>` beside itself first, and `--dry-run` prints the unified diff
of both rosters and writes nothing.

Order matters more than atomicity here, because the one thing migration.md
principle 5 forbids is two schedulers or two redeemers alive on one account:

* `--to v2` drops the account from v1's roster **first**, then flips the store.
  A failure in between leaves nobody dispatching on it, which is safe.
* `--to v1` flips the store **first**, then puts the account back in v1's
  roster. A failure in between again leaves nobody dispatching on it.

What each v1 roster edit actually achieves, read from v1's own source on
2026-09-05:

* Claude (`claude-accounts.json`): v1 calls an account a lane only when it
  appears in `enrolled` (`claude.py:125-142`, `claude.py:284`), and keepalive
  iterates exactly that map (`keepalive.py:41,301,313`). Moving the entry out of
  `enrolled` into `transferred_to_v2` is therefore the real lever, and `accounts`
  is left alone because it is v1's identity list, not its lane list.
* Codex (`codex-accounts.json`): v1 does **not** read a roster of homes from this
  file. `paths.codex_homes()` globs `~/.codex-1` … `~/.codex-9` and honours only
  the `SUBFLEET_CODEX_HOMES` environment override. The file records the transfer
  so the roster tells the truth about who owns what, but the record is not a
  lever, so a Codex `--to v2` is **refused** while any of v1's launch agents can
  still reach the home: `v1_codex_scope` reads their plists, and the refusal
  names the agents and the exact `SUBFLEET_CODEX_HOMES` value they need. That is
  what keeps migration.md principle 5 - never two schedulers or two redeemers on
  one account - an invariant rather than an instruction.

The write to a v1 file is outside `$SUBFLEET_HOME`, which C-2.1 does not permit.
C-2.1 binds milestones 1 to 3 by the contract's own first line; the transfer is
milestone 4, and `migration.md` (plan amendment 8) is what binds it. The
integrator should decide whether C-2.1 grows an explicit exception when the
contract is extended past milestone 3.
"""

from __future__ import annotations

import difflib
import json
import os
import plistlib
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Exit
from .store import Store, utc_now

V1_ROSTER_DIR = Path("~/chief-of-staff/subfleet").expanduser()
ROSTER_FILE = {"claude": "claude-accounts.json", "codex": "codex-accounts.json"}
TRANSFERRED_KEY = "transferred_to_v2"
V2_ROSTER = "lanes.json"

#: Where v1's scheduled work is defined. `paths.codex_homes()` reads
#: `SUBFLEET_CODEX_HOMES` from the environment, and for a launch agent the
#: environment is its plist's `EnvironmentVariables`.
LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
CODEX_HOMES_ENV = "SUBFLEET_CODEX_HOMES"
#: `paths.codex_homes()`: `[HOME / f".codex-{i}" for i in range(1, 10)]`.
CODEX_HOME_RANGE = range(1, 10)


class TransferError(Exception):
    """A transfer that must not proceed; carries the CLI's exit code (C-17.3)."""

    def __init__(self, message: str, code: Exit = Exit.INVALID_INPUT, fix: str | None = None):
        super().__init__(message)
        self.code = code
        self.fix = fix


@dataclass
class RosterEdit:
    """One roster file's before and after, and where its backup goes."""

    path: Path
    before: str
    after: str
    owner: str                     # "v1" | "v2": whose file this is
    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile=f"a/{self.path}", tofile=f"b/{self.path}"))


@dataclass
class TransferPlan:
    lane_id: str
    provider: str
    account_key: str
    from_owner: str
    to_owner: str
    edits: list[RosterEdit] = field(default_factory=list)
    follow_up: list[str] = field(default_factory=list)
    blocker: str | None = None      # why v2 must not take this account yet

    @property
    def changed(self) -> bool:
        return self.from_owner != self.to_owner or any(edit.changed for edit in self.edits)

    def diff(self) -> str:
        return "".join(edit.diff() for edit in self.edits if edit.changed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id, "provider": self.provider,
            "account_key": self.account_key, "from": self.from_owner, "to": self.to_owner,
            "changed": self.changed, "diff": self.diff(),
            "edits": [{"path": str(edit.path), "owner": edit.owner,
                       "changed": edit.changed,
                       "backup": str(edit.backup) if edit.backup else None}
                      for edit in self.edits],
            "follow_up": self.follow_up, "blocker": self.blocker,
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _indent_of(text: str) -> int:
    """How many spaces this file already indents by, so a diff shows one change.

    The whole point of `--dry-run` is that an operator can read the one line that
    moves; re-serialising v1's roster in this repository's own style would show
    every line as changed and hide it.
    """
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def _dumps(value: Any, *, indent: int = 2, ascii_only: bool = True) -> str:
    return json.dumps(value, indent=indent, sort_keys=False, ensure_ascii=ascii_only) + "\n"


def _restyle(original: str, value: Any) -> str:
    """Re-serialise `value` the way `original` was written."""
    return _dumps(value, indent=_indent_of(original),
                  ascii_only="\\u" in original or original.isascii())


def _publish(path: Path, text: str) -> None:
    """C-8.1's publication shape: temporary file, fsync, rename, fsync directory.

    An existing file keeps its mode: a v1 roster is a 0644 git-tracked config and
    a transfer has no business tightening it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb", opener=lambda name, flags: os.open(name, flags, mode)) as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _backup_path(path: Path) -> Path:
    """A free `<name>.bak-<utc>` beside the file.

    Whole seconds are v1's own convention (`cooldowns.json.bak-2026-09-04`), and
    accounts transfer in batches, so two transfers land in one second routinely;
    a copy that overwrote the first backup would destroy the pre-batch roster.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    serial = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{serial}")
        serial += 1
    return candidate


# --- what v1 can still reach --------------------------------------------------

def v1_codex_scope(home: Path, roster_dir: Path,
                   agents_dir: Path | None = None) -> dict[str, list[str]]:
    """The Codex homes each of v1's launch agents can still dispatch on.

    Read from v1's own source on 2026-09-05: `paths.codex_homes()` returns
    `[~/.codex-1 … ~/.codex-9]` that exist, unless `SUBFLEET_CODEX_HOMES`
    (colon-separated) overrides discovery entirely. A launch agent's environment
    is its plist's `EnvironmentVariables`, so that plist is where the exclusion
    has to be written for it to bite. Returns `{agent label: [home, …]}` for every
    agent that runs v1.
    """
    agents = Path(agents_dir) if agents_dir is not None else LAUNCH_AGENTS
    discovered = [str(home / f".codex-{index}") for index in CODEX_HOME_RANGE
                  if (home / f".codex-{index}").is_dir()]
    scope: dict[str, list[str]] = {}
    if not agents.is_dir():
        return scope
    for path in sorted(agents.glob("*.plist")):
        try:
            plist = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        argv = " ".join(str(item) for item in (plist.get("ProgramArguments") or []))
        if str(roster_dir) not in argv:
            continue
        override = (plist.get("EnvironmentVariables") or {}).get(CODEX_HOMES_ENV)
        scope[str(plist.get("Label") or path.stem)] = (
            [item for item in str(override).split(":") if item] if override is not None
            else list(discovered))
    return scope


# --- the v2 roster ------------------------------------------------------------

def _v2_roster_edit(state_root: Path, lane: dict[str, Any], to_owner: str) -> RosterEdit:
    """`<state root>/lanes.json` is the daemon's seed file (`daemon._seed_lanes`).

    A store rebuilt from it must come back with the ownership the transfer set,
    so the lane's row is added when the file does not name it yet.
    """
    path = state_root / V2_ROSTER
    before = _read_text(path)
    try:
        roster = json.loads(before) if before.strip() else []
    except ValueError:
        raise TransferError(f"{path} is not valid JSON; refusing to edit it",
                            Exit.OPERATIONAL, "repair lanes.json or remove it") from None
    rows = roster.get("lanes", []) if isinstance(roster, dict) else roster
    if not isinstance(rows, list):
        raise TransferError(f"{path} does not hold a lane list", Exit.OPERATIONAL)
    rows = [dict(row) for row in rows if isinstance(row, dict)]
    for row in rows:
        if row.get("lane_id") == lane["lane_id"]:
            row["owner"] = to_owner
            break
    else:
        rows.append({
            "lane_id": lane["lane_id"], "provider": lane["provider"],
            "account_key": lane["account_key"], "credential_ref": lane["credential_ref"],
            "credential_kind": lane["credential_kind"],
            "credential_epoch": lane["credential_epoch"], "home": lane["home"],
            "owner": to_owner, "desktop": bool(lane["desktop"]),
            "enabled": bool(lane["enabled"]),
        })
    after = (_restyle(before, {"lanes": rows} if isinstance(roster, dict) else rows)
             if before.strip() else _dumps({"lanes": rows} if isinstance(roster, dict) else rows))
    return RosterEdit(path, before, after, "v2")


# --- the v1 rosters -----------------------------------------------------------

def _claude_roster_edit(roster_dir: Path, lane: dict[str, Any], to_owner: str) -> RosterEdit:
    """Move the account between `enrolled` and `transferred_to_v2`.

    v1 treats an account as a lane only while it is in `enrolled`
    (`claude.py:125`, `keepalive.py:41`), so this is the edit that makes v1 stop.
    """
    path = roster_dir / ROSTER_FILE["claude"]
    before = _read_text(path)
    if not before.strip():
        raise TransferError(f"{path} is missing or empty; refusing to write a v1 roster "
                            "this process did not read", Exit.OPERATIONAL)
    try:
        roster = json.loads(before)
    except ValueError:
        raise TransferError(f"{path} is not valid JSON; refusing to edit it",
                            Exit.OPERATIONAL) from None
    if not isinstance(roster, dict):
        raise TransferError(f"{path} does not hold a roster object", Exit.OPERATIONAL)
    email = lane["account_key"].split(":", 1)[-1]
    enrolled = dict(roster.get("enrolled") or {})
    parked = dict(roster.get(TRANSFERRED_KEY) or {})
    if to_owner == "v2":
        reference = enrolled.pop(email, None) or parked.get(email) or lane["credential_ref"]
        parked[email] = reference
    else:
        reference = parked.pop(email, None) or enrolled.get(email) or lane["credential_ref"]
        enrolled[email] = reference
    roster["enrolled"] = enrolled
    if parked:
        roster[TRANSFERRED_KEY] = parked
    else:
        roster.pop(TRANSFERRED_KEY, None)
    return RosterEdit(path, before, _restyle(before, roster), "v1")


def _codex_roster_edit(roster_dir: Path, lane: dict[str, Any], to_owner: str,
                       home: Path) -> tuple[RosterEdit, list[str]]:
    """Record the transfer, and name the follow-up that actually enforces it.

    `paths.codex_homes()` globs `~/.codex-1` … `~/.codex-9` and reads no roster
    from this file, so the record here is the roster telling the truth and the
    returned follow-up is what makes v1 stop.
    """
    path = roster_dir / ROSTER_FILE["codex"]
    before = _read_text(path)
    if not before.strip():
        raise TransferError(f"{path} is missing or empty; refusing to write a v1 roster "
                            "this process did not read", Exit.OPERATIONAL)
    try:
        roster = json.loads(before)
    except ValueError:
        raise TransferError(f"{path} is not valid JSON; refusing to edit it",
                            Exit.OPERATIONAL) from None
    if not isinstance(roster, dict):
        raise TransferError(f"{path} does not hold a roster object", Exit.OPERATIONAL)
    lane_home = lane.get("home") or lane.get("credential_ref")
    parked = [dict(row) for row in (roster.get(TRANSFERRED_KEY) or []) if isinstance(row, dict)]
    parked = [row for row in parked if row.get("home") != lane_home]
    if to_owner == "v2":
        parked.append({"home": lane_home, "account_id": lane["account_key"].split(":", 1)[-1],
                       "lane_id": lane["lane_id"], "at": utc_now()})
    if parked:
        roster[TRANSFERRED_KEY] = parked
    else:
        roster.pop(TRANSFERRED_KEY, None)
    parked_homes = {row.get("home") for row in parked}
    remaining = [str(candidate)
                 for candidate in (home / f".codex-{index}" for index in CODEX_HOME_RANGE)
                 if candidate.is_dir() and str(candidate) not in parked_homes]
    follow_up = [
        "v1 discovers Codex homes by globbing ~/.codex-1..9 (paths.codex_homes); "
        "this file is a record, not a lever.",
        f"{CODEX_HOMES_ENV}={':'.join(remaining)} is the value v1's launch agents "
        f"need for the exclusion of {lane_home} to bite.",
    ]
    return RosterEdit(path, before, _restyle(before, roster), "v1"), follow_up, remaining


# --- planning and applying ----------------------------------------------------

def plan_transfer(store: Store, state_root: Path, lane_id: str, to_owner: str, *,
                  roster_dir: Path | None = None, home: Path | None = None,
                  agents_dir: Path | None = None) -> TransferPlan:
    """Build the whole edit without touching anything (C-19.1: `--dry-run` never acts)."""
    if to_owner not in ("v1", "v2"):
        raise TransferError("lanes transfer: --to must be v1 or v2")
    row = store.one("SELECT * FROM lanes WHERE lane_id=?", (lane_id,))
    if row is None:
        raise TransferError(f"unknown lane {lane_id}", Exit.INVALID_INPUT,
                            "subfleet lanes list")
    roster_dir = Path(roster_dir) if roster_dir else V1_ROSTER_DIR
    home = Path(home) if home else Path.home()
    plan = TransferPlan(lane_id, row["provider"], row["account_key"], row["owner"], to_owner)
    plan.edits.append(_v2_roster_edit(Path(state_root), row, to_owner))
    if row["provider"] == "claude":
        plan.edits.append(_claude_roster_edit(roster_dir, row, to_owner))
    else:
        edit, follow_up, remaining = _codex_roster_edit(roster_dir, row, to_owner, home)
        plan.edits.append(edit)
        plan.follow_up.extend(follow_up)
        if to_owner == "v2":
            # migration.md principle 5: never two schedulers on one account. The
            # codex-accounts.json record above is not a lever v1 reads, so v2 may
            # take the account only once v1's own launch agents cannot reach it.
            lane_home = str(row.get("home") or row["credential_ref"])
            reaching = sorted(label for label, homes in
                              v1_codex_scope(home, roster_dir, agents_dir).items()
                              if lane_home in homes)
            if reaching:
                plan.blocker = (
                    f"v1 still dispatches on {lane_home}: launch agent(s) "
                    + ", ".join(reaching)
                    + f" have no {CODEX_HOMES_ENV} excluding it. Set "
                    + f"{CODEX_HOMES_ENV}={':'.join(remaining)} in their "
                    + "EnvironmentVariables, reload them, and run this again")
    return plan


def apply_transfer(store: Store, plan: TransferPlan, *, confirm_v1_edit: bool) -> dict[str, Any]:
    """Flip ownership, record the event, and publish both rosters in the safe order."""
    if plan.blocker:
        raise TransferError(plan.blocker, Exit.REFUSED,
                            "subfleet lanes transfer <lane> --to v2 --dry-run shows the plan")
    v1_edits = [edit for edit in plan.edits if edit.owner == "v1" and edit.changed]
    if v1_edits and not confirm_v1_edit:
        raise TransferError(
            "this transfer edits " + ", ".join(str(edit.path) for edit in v1_edits)
            + ", a v1 file", Exit.REFUSED,
            "re-run with --i-understand-v1-edit (a backup is written beside it)")
    v2_edits = [edit for edit in plan.edits if edit.owner == "v2" and edit.changed]

    def publish(edits: list[RosterEdit]) -> None:
        for edit in edits:
            if edit.owner == "v1" and edit.path.exists():
                edit.backup = _backup_path(edit.path)
                shutil.copy2(edit.path, edit.backup)
            _publish(edit.path, edit.after)

    def flip() -> None:
        """C-10.4: ownership changes here and records an event.

        A transfer that changed only a roster - v2 already owned the lane but v1's
        file had not been told - records one too: it is a fact about who owns the
        account, and an unrecorded edit to a v1 file is invisible. A transfer that
        changed nothing records nothing.
        """
        rosters = [str(edit.path) for edit in plan.edits if edit.changed]
        data = {"from": plan.from_owner, "to": plan.to_owner,
                "account_key": plan.account_key, "rosters": rosters,
                "follow_up": plan.follow_up}
        if plan.from_owner != plan.to_owner:
            with store.transaction("lane.transferred", lane_id=plan.lane_id, data=data):
                store.update_lane(plan.lane_id, owner=plan.to_owner)
        elif rosters:
            # One row, not the two `Store.add_event` writes (it logs its own
            # transaction as well), and no state change to wrap it in.
            store.conn.execute(
                "INSERT INTO events(ts,kind,lane_id,data_json) VALUES (?,?,?,?)",
                (utc_now(), "lane.transferred", plan.lane_id,
                 json.dumps(data, sort_keys=True, separators=(",", ":"))))

    if plan.to_owner == "v2":
        publish(v1_edits)       # v1 stops first: never two schedulers (principle 5)
        flip()
        publish(v2_edits)
    else:
        flip()                  # v2 stops first
        publish(v2_edits)
        publish(v1_edits)
    return plan.as_dict()


def transfer(store: Store, state_root: Path, lane_id: str | None, to_owner: str | None, *,
             dry_run: bool = False, confirm_v1_edit: bool = False,
             roster_dir: Path | None = None, home: Path | None = None,
             agents_dir: Path | None = None) -> dict[str, Any]:
    """The daemon op behind `subfleet lanes transfer` (C-16.2 `lanes`)."""
    if not lane_id:
        raise TransferError("lanes transfer: name a lane")
    plan = plan_transfer(store, state_root, lane_id, to_owner or "", roster_dir=roster_dir,
                         home=home, agents_dir=agents_dir)
    if dry_run:
        return {**plan.as_dict(), "dry_run": True, "applied": False}
    result = apply_transfer(store, plan, confirm_v1_edit=confirm_v1_edit)
    return {**result, "dry_run": False, "applied": plan.changed}
