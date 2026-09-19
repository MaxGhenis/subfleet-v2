"""v1 invocation compatibility: the parser layer in front of `subfleet/cli.py`.

When the `subfleet` symlink is repointed from v1 to v2, every command any agent
types today must keep working. This module is the front door that makes that
true. It sits in front of `cli.main`, never inside it: `cli.py` owns the v2 verb
table and the C-17.3 exit codes, and nothing here changes either.

Four dispositions, and the reasoning for each:

* **map** — the spelling is permanent (plan.md amendment 1: "every v1 verb
  spelling is permanent"). `runs`, `runs show`, `runs reap`, `status`,
  `capacity`, `wait`, `kill`, `notify`, `resume-codex`, `run`, and a bare
  `subfleet` are v1 spellings; `jobs` and a top-level `show` are NOT — neither
  string appears anywhere in the v1 tree, and v1 answers both with a usage
  error, because C-17.1 introduces them as v2 aliases. Either way the class is
  the same: a permanent spelling gets NO stderr note, ever, since a note on a
  spelling the contract promises to keep is noise that trains agents to change
  working commands.

* **note** — the spelling is accepted but deprecated, so exactly one line goes
  to stderr naming the replacement (C-17.2). `hooks install`,
  `hooks status`, and `run --status` are here. Flag-level deprecations
  (`-t CLASS`, `--overflow`, `-m sol`) are NOT re-noted here: `cli.py`'s
  `_apply_deprecations` already prints them, and two notes for one flag reads
  like two problems.

* **delegate** — v2 has no home for the verb yet, but v1 does, and during the
  shadow period v1 is still installed (plan amendment 8). The whole argv goes to
  the v1 binary and its exit code comes back unchanged. This is not a
  convenience: `_record-run`, `_record-lane-run`, `_canonical-model` and the
  other hidden verbs are called BY v1's own runners through
  `$SUBFLEET_RUN_SUBFLEET` and `$DELEGATE_SUBFLEET`, so a symlink flip that
  broke them would break every v1 run already in flight. `gate` is native in v2
  and preserves its separate 0-to-5 exit meanings (v1 README:717-720).

* **refuse** — `subfleet codex` and `subfleet claude` are the direct provider
  verbs the agent contract tells sessions never to call (`~/.claude/CLAUDE.md`
  "Model routing"; v1's PreToolUse guard denies them inside a session). v2
  refuses them at the CLI with exit 7, whose C-17.3 meaning is "refused (message
  names the rule and the fix)". `subfleet mirror` was refused for the same
  reason until milestone 6 built the sessions kit; it is now a PERMANENT
  spelling of `sessions mirror`, because v2 owns the mirror (C-23.28).

Environment variables are read, mapped, and noted, never silently reinterpreted.
The one real mapping is the `CARPOOL_*` → `SUBFLEET_*` aliasing that v1 performs
in its bash launcher (`~/chief-of-staff/subfleet/bin/subfleet:8-12`); v2's entry
point is a console script with no launcher in front of it, so the aliasing has
to happen here or it stops happening at the cutover. Everything else — the
`DELEGATE_*` and `CLAUDE_LANE_*` families — is noted rather than mapped, because
those names point at v1's own state trees and binaries and pointing v2 at them
would be worse than ignoring them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .contracts import Exit

PROG = "subfleet"

#: Where the v1 install lives when nothing overrides it. `SUBFLEET_V1_BIN` is
#: the override, and the tests' seam.
V1_BIN_ENV = "SUBFLEET_V1_BIN"
V1_BIN_DEFAULT = "~/chief-of-staff/subfleet/bin/subfleet"

FRONT_DOOR = (
    "use the front door: `subfleet run --task <task> --tier <tier> "
    "-C <dir> -p prompt.md -o out.md`")


@dataclass
class Mapping:
    """One v1 invocation's disposition. `argv` is what `cli.main` should see."""

    disposition: str                                    # map|note|delegate|refuse
    argv: list[str] = field(default_factory=list)       # v2 argv (map/note)
    verb: str = ""                                      # dotted v2 verb path
    notes: list[str] = field(default_factory=list)      # stderr lines, in order
    env: dict[str, str] = field(default_factory=dict)   # variables to export
    exit_code: int | None = None                        # refuse only
    rule: str = ""                                      # which table row fired

    def as_dict(self) -> dict[str, Any]:
        return {"disposition": self.disposition, "argv": list(self.argv),
                "verb": self.verb, "notes": list(self.notes),
                "env": dict(self.env), "exit_code": self.exit_code,
                "rule": self.rule}


# --- the verb table -----------------------------------------------------------
#
# Each entry maps a v1 first token (optionally with its second token) to what v2
# does with it. `to` is a v2 argv prefix that replaces the matched tokens;
# `note` is the single stderr line for a deprecated spelling; `None` means the
# spelling is permanent and gets no note.

#: Permanent spellings that reach a v2 verb unchanged or through the aliases
#: `cli.rewrite_aliases` already applies (C-17.1). Listed here so `self_check`
#: can prove each one still reaches a handler, and so a future edit to
#: `cli.ALIASES` cannot quietly drop one. `jobs` and `show` are marked: they are
#: C-17.1's own additions, not v1 spellings — v1 has no `jobs` anywhere and
#: `show` only as `runs show` — so they cannot break a v1 command, only add one.
PERMANENT: dict[tuple[str, ...], list[str]] = {
    ("gate",): ["gate"],
    ("status",): ["status"],
    ("capacity",): ["status"],
    ("runs",): ["runs"],
    ("runs", "show"): ["runs", "show"],
    ("runs", "reap"): ["runs", "reap"],
    ("jobs",): ["runs"],                                # C-17.1, not v1
    ("show",): ["runs", "show"],                        # C-17.1, not v1
    ("wait",): ["wait"],
    ("kill",): ["kill"],
    ("resume",): ["resume"],
    ("resume-codex",): ["resume"],
    ("notify",): ["ping"],
    ("ping",): ["ping"],
    ("run",): ["run"],
    ("lanes",): ["lanes"],
    ("enroll",): ["lanes", "enroll"],
    ("why",): ["why"],
    ("daemon",): ["daemon"],
    ("doctor",): ["doctor"],
    ("hook",): ["hook"],
    # The sessions kit (milestone 6). C-17.1 makes `sessions` and `handoff`
    # first-class and permanent, and plan amendment 1 makes every v1 spelling
    # permanent — so `tickle`, `muster`, `revive` and `mirror` are mapped, not
    # noted: an agent that has been told to run `subfleet tickle --all` since
    # August should not start reading a deprecation line every morning.
    #
    # These are argv PREFIXES and the caller's remaining tokens are appended, so
    # every flag v1's three sweeps accepted is registered on `sessions continue`
    # (see `sessions/cli.py:add_continue_flags`), and the ones v2 has no use for
    # are in `V1_ONLY_FLAGS` below rather than silently dropped.
    ("sessions",): ["sessions"],
    ("handoff",): ["handoff"],
    ("tickle",): ["sessions", "continue", "--scope", "interrupted"],
    ("muster",): ["sessions", "continue", "--scope", "idle"],
    ("revive",): ["sessions", "continue", "--scope", "cold"],
    ("mirror",): ["sessions", "mirror"],
}

#: Deprecated v1 spellings: accepted, rewritten, and noted exactly once.
RENAMED: dict[tuple[str, ...], tuple[list[str], str]] = {
    ("hooks", "install"): (["daemon", "install", "--hooks"],
                           "`hooks install` is now `daemon install --hooks`, "
                           "which prints the settings diff before writing"),
    ("hooks", "status"): (["doctor"],
                          "`hooks status` is now a `doctor` row "
                          "(\"hook entries in ~/.claude/settings.json\")"),
    ("hooks",): (["doctor"],
                 "`hooks` with no sub-verb was `hooks status`; that is now a "
                 "`doctor` row"),
}

#: `hooks uninstall` removes v1's OWN entries from ~/.claude/settings.json
#: (v1 `hooks.py:166` walks every event and drops each command containing
#: `subfleet-hook`). That is v1 tidying up after itself, which is exactly right
#: during the shadow period and is not something v2 can do on its behalf — so it
#: is delegated whole rather than rewritten into a v2 verb. Without this row the
#: `("hooks",)` rule above would quietly turn an uninstall into a `doctor` run.
DELEGATED_PAIRS: dict[tuple[str, ...], str] = {
    ("hooks", "uninstall"): "`hooks uninstall` removes v1's own hook entries; "
                            "v1 owns them, so v1 removes them",
}

#: Direct provider verbs. The agent contract says never to call these from a
#: session and v1's PreToolUse guard denies them; v2 refuses them outright.
#: v2 refuses these unconditionally, which is NARROWER than v1's PreToolUse
#: guard: `bin/subfleet-hook:61-67` let `subfleet codex -d` and `subfleet claude
#: -d` through, because the runners' own `-d` re-execs under `setsid` and
#: survives the session. v2 has no `subfleet-codex`/`subfleet-claude` runner to
#: re-exec — those are v1 binaries — so there is nothing for `-d` to make
#: survivable, and the message says so rather than leaving an agent to wonder
#: why the flag stopped helping.
REFUSED: dict[str, str] = {
    "codex": "`subfleet codex` launches a provider directly, outside the "
             "ledger, the lane accounting, and the cancellation tree; v1's "
             "guard let `-d` through because v1's runner re-exec'd under "
             "setsid, and v2 has no such runner",
    "claude": "`subfleet claude` launches a provider directly, outside the "
              "ledger, the lane accounting, and the cancellation tree; v1's "
              "guard let `-d` through because v1's runner re-exec'd under "
              "setsid, and v2 has no such runner",
}

#: v1 verbs v2 has not built yet, delegated to the v1 binary with one note.
#: The value is the sentence the note carries after "not a v2 verb yet".
DELEGATED: dict[str, str] = {
    "pick": "lane picking belongs to whichever side owns the lane "
            "(`subfleet lanes list` shows the owner)",
    "login": "lane credentials stay with v1 until `lanes transfer --to v2`",
    "reset": "lane resets stay with v1 until `lanes transfer --to v2`",
    "errors": "the error ledger is v1's",
    "watch": "the watchdog is v1's",
    "keepalive": "keepalive is v1's",
    "brief": "the morning brief is v1's",
}

#: v1's hidden verbs. Delegated like the rest but SILENTLY: every one of these
#: is called by a v1 runner through `$SUBFLEET_RUN_SUBFLEET`,
#: `$DELEGATE_SUBFLEET`, or `$SUBFLEET_CODEX_PICK`, and a note on stderr would
#: land in a runner's captured `err.log` on every single record it writes.
HIDDEN = ("_session-hook", "_tickle", "_canonical-model", "_api-lane-check",
          "_record-lane-run", "_record-run", "_record-codex-cooldown")

@dataclass(frozen=True)
class V1Flag:
    """A flag v1 accepted and v2's parser has no name for.

    `action` is `drop` when v2 does the same thing or a safer thing without it,
    and `refuse` when continuing without it would do something the caller did
    not ask for. `takes_value` says whether the following token belongs to it,
    so a dropped flag does not leave its argument behind as a stray positional.
    """

    action: str                                         # drop | refuse
    takes_value: bool
    why: str


def _drop(why: str, *, takes_value: bool = False) -> V1Flag:
    return V1Flag("drop", takes_value, why)


def _refuse(why: str, *, takes_value: bool = False) -> V1Flag:
    return V1Flag("refuse", takes_value, why)


#: Keyed by the v1 verb path as typed. Built by diffing v1's own parsers
#: (`cli.py`'s subparsers and `delegate.py:_parser()`) against
#: `cli.build_parser()`, not by reading either help text — `tests/unit/
#: test_compat.py::test_no_v1_flag_is_unaccounted_for` redoes that diff and
#: fails if v1 ever grows a flag this table has not decided about.
V1_ONLY_FLAGS: dict[str, dict[str, V1Flag]] = {
    "enroll": {
        "--mint": _refuse(
            "v1 starts `claude setup-token` to mint a credential; v2 never "
            "performs provider login (C-23.52) — run `claude setup-token` "
            "yourself, store the token with `agent-secret`, then use "
            "`subfleet lanes enroll claude-quota-<email>`"),
        "--paste": _refuse(
            "v1 accepts a browser callback code for `enroll --mint`; v2 "
            "never performs provider login (C-23.52) — run `claude setup-token` "
            "yourself, store the token with `agent-secret`, then use "
            "`subfleet lanes enroll claude-quota-<email>`"),
    },
    "status": {
        "--cached": _drop(
            "v1 read the last watchdog snapshot; v2's `status` reads the daemon, "
            "and reads the store read-only when no daemon is listening (C-17.5), "
            "so it is never a network call and never stale by a whole cycle"),
    },
    "runs reap": {
        "--dry-run": _refuse(
            "v1 listed what reaping would finalise without writing; v2's `runs "
            "reap` has no preview, and running it anyway would perform the "
            "reconciliation you asked to preview — `subfleet runs --running` "
            "lists the same jobs"),
        "--grace": _drop(
            "v1 waited this many seconds (default 60) before finalising an "
            "orphan; v2 decides by process identity rather than by elapsed time "
            "(C-5.3), so there is nothing for a grace period to buy",
            takes_value=True),
    },
    "wait": {
        "--cat": _refuse(
            "v1 printed each finished run's output on stdout after waiting; v2's "
            "`wait` puts nothing but the contract on stdout (C-17.4), so "
            "dropping it would silently empty a `subfleet wait x --cat > file` "
            "— use `subfleet runs show <id> --out`"),
        "--interval": _drop(
            "v1 polled every N seconds (default 2); v2's `wait` is a server-side "
            "long poll (C-15.4), so there is no interval to set",
            takes_value=True),
    },
    "kill": {
        "--grace": _drop(
            "v1 waited this many seconds (default 10) between SIGTERM and "
            "SIGKILL; v2's containment owns that escalation (C-5) and reports "
            "what it did rather than taking the number from the caller",
            takes_value=True),
    },
    "notify": {
        "--force": _drop(
            "v1 delivered even to a lane session, overwriting its captured "
            "deliverable; v2 never addresses a lane session and has no override "
            "for it (C-15.2 layer 4)"),
        "--mode": _drop(
            "v1 declared a permission class on the envelope; v2 resolves the "
            "recipient's own mode from its transcript",
            takes_value=True),
    },
    "run": {
        "-b": _refuse(
            "v1 passed `-b BRANCH` straight to the runner; v2 gives every job "
            "its own worktree (C-13) — use `-C DIR`, or `--in-place` to write "
            "where the caller stands",
            takes_value=True),
        "--reuse-out": _refuse(
            "v1's only way onto an `-o` path a LIVE run was still writing; v2 "
            "exports per job (C-8) and has no such override"),

    },
    "revive": {
        "--no-fallback": _drop(
            "v1 walked a model chain (SUBFLEET_REVIVE_MODELS) and `--no-fallback` "
            "stopped it after the first; v2 pins never fall back (C-11.2), so "
            "`--model M` already means M or nothing, and without it a revive "
            "keeps the session's own recorded tier (C-23.39)"),
    },
    "mirror": {
        "--version": _refuse(
            "v1's mirror was a separate binary with its own version string; v2's "
            "mirror is part of subfleet and running it anyway would perform a "
            "sidebar pass you asked to identify \u2014 `subfleet -V`"),
    },
}

FLAG_TABLE_ALIASES = {"capacity": "status", "jobs": "runs", "show": "runs show",
                      "ping": "notify", "resume-codex": "resume"}

#: Where each flag-table key lands in v2's parser, so the abbreviation matcher
#: can leave v2's own options alone.
FLAG_TABLE_V2_PATH = {"status": "status", "runs reap": "runs.reap",
                      "enroll": "lanes.enroll",
                      "wait": "wait", "kill": "kill", "notify": "ping",
                      "run": "run", "revive": "sessions.continue",
                      "mirror": "sessions.mirror"}


def _flag_table(argv: Sequence[str]) -> tuple[str, dict[str, V1Flag]]:
    """(the key that matched, its flags) for the longest verb path that has one."""
    for width in (2, 1):
        key = " ".join(argv[:width])
        key = FLAG_TABLE_ALIASES.get(key, key)
        if key in V1_ONLY_FLAGS:
            return key, V1_ONLY_FLAGS[key]
    return "", {}


def v2_options(path: str) -> set[str]:
    """Every option string v2 accepts at `path`, plus the root parser's own."""
    from . import cli

    def walk(parser: Any, prefix: str) -> dict[str, set[str]]:
        found = {prefix or "(root)": {option for action in parser._actions  # noqa: SLF001
                                      for option in action.option_strings}}
        for action in parser._actions:                  # noqa: SLF001 - our parser
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and hasattr(action, "add_parser"):
                for name, sub in choices.items():
                    found.update(walk(sub, f"{prefix}.{name}" if prefix else name))
        return found

    table = walk(cli.build_parser(), "")
    return table.get(path, set()) | table.get("(root)", set())


def _abbreviates(flag: str, token: str) -> bool:
    """Is `token` the flag, or one of argparse's abbreviations of it?

    Any prefix of a long option is accepted when it is unambiguous, which for a
    v1-only flag it always is on the v2 side — v2 does not have the flag at all.
    """
    bare = token.split("=", 1)[0]
    return bare.startswith("--") and len(bare) > 2 and flag.startswith(bare)


def _resolve(token: str, flags: dict[str, V1Flag], protected: set[str]) -> str | None:
    """Which v1-only flag `token` names, honouring argparse's prefix matching.

    v1's parsers accept any unambiguous abbreviation, so `--independent-rev` and
    `--reuse` were both real v1 spellings. v2's parser would reject them with a
    usage error that names nothing useful, so they resolve here to the flag they
    abbreviated and get that flag's message instead.

    `protected` is v2's own option set for this verb, and nothing in it is ever
    resolved to a v1 flag. `--independent` is the case that makes this matter:
    in v1 it was an unambiguous abbreviation of the hidden `--independent-review`
    and in v2 it is a real flag of its own (C-7.3). v2 is the version being run,
    so v2's meaning wins, and the message on `--independent-review` says so.
    """
    bare = token.split("=", 1)[0]
    if bare in protected:
        return None
    if bare in flags:
        return bare
    if not bare.startswith("--") or len(bare) <= 2:
        return None
    matches = [name for name in flags
               if name.startswith("--") and name.startswith(bare)]
    return matches[0] if len(matches) == 1 else None


def scan_flags(argv: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(argv without the dropped flags, notes, refusals) for one invocation."""
    key, flags = _flag_table(argv)
    if not flags:
        return list(argv), [], []
    protected = v2_options(FLAG_TABLE_V2_PATH.get(key, key)) - set(flags)
    kept: list[str] = []
    notes: list[str] = []
    refusals: list[str] = []
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            continue
        name = (_resolve(token, flags, protected)
                if index and token.startswith("-") else None)
        if name is None:
            kept.append(token)
            continue
        flag = flags[name]
        spelled = "" if "=" in token else (
            f" {argv[index + 1]}" if flag.takes_value and index + 1 < len(argv) else "")
        if flag.takes_value and "=" not in token:
            skip = index + 1 < len(argv)
        line = f"{PROG} {key}: {name} {flag.why}"
        if flag.action == "refuse":
            refusals.append(line)
        else:
            notes.append(f"{line} — `{token}{spelled}` is dropped")
    return kept, notes, refusals


# --- environment --------------------------------------------------------------

#: Names v1's bash launcher aliases unconditionally, on top of every `CARPOOL_*`
#: it finds in the environment (`bin/subfleet:8-12`). `${_legacy/CARPOOL/SUBFLEET}`
#: replaces the FIRST occurrence, which is why these two land where they do.
CARPOOL_EXTRA = {"CLAUDE_LANE_CARPOOL": "CLAUDE_LANE_SUBFLEET",
                 "DELEGATE_CARPOOL": "DELEGATE_SUBFLEET"}

#: Read and noted, never mapped. Each names a v1 path or binary; pointing v2 at
#: one would make v2 read v1's tree or run v1's code under a v2 verb.
NOTED_ENV: dict[str, str] = {
    "DELEGATE_STATE_DIR": "names v1's second state root "
                          "(`~/.local/state/delegate`); v2's root is "
                          "$SUBFLEET_HOME and the two schemas are unrelated, so "
                          "v2 ignores it",
    "DELEGATE_ACCOUNTS_FILE": "names v1's claude-accounts.json; v2 reads lanes "
                              "from its own store (`subfleet lanes list`)",
    "DELEGATE_SUBFLEET": "names the v1 `subfleet` binary a v1 runner calls back "
                         "into; v2 ignores it (set SUBFLEET_V1_BIN to steer "
                         "what v2 delegates to)",
    "DELEGATE_CODEX_RUN": "names v1's `subfleet-codex` runner; v2's Codex "
                          "adapter launches `codex` itself (C-12)",
    "DELEGATE_CLAUDE_LANE": "names v1's `subfleet-claude` runner; v2's Claude "
                            "adapter launches `claude` itself (C-12)",
    "SUBFLEET_STATE_DIR": "is v1's state root; v2 reads $SUBFLEET_HOME "
                          "(default ~/.subfleet) and the two are different "
                          "trees with different schemas (C-2.1)",
    "CLAUDE_LANE_CLAUDE": "names the `claude` binary for v1's runner; v2's "
                          "adapter resolves `claude` on PATH (`subfleet doctor` "
                          "reports which one wins)",
    "CLAUDE_LANE_SUBFLEET": "names the v1 `subfleet` binary v1's Claude runner "
                            "calls back into; v2 ignores it",
    "CLAUDE_LANE_AGENT_SECRET": "names v1's agent-secret helper; v2 reads "
                                "credentials through its own store (C-10)",
}


def map_env(env: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """(variables to export, notes) — v1's launcher aliasing, plus what is read.

    v1's `bin/subfleet` is a bash launcher that aliases every `CARPOOL_*` name
    to its `SUBFLEET_*` twin before exec'ing Python, and only when the twin is
    unset. v2 is a console script with no launcher, so the same rule runs here.
    """
    env = dict(os.environ if env is None else env)
    updates: dict[str, str] = {}
    notes: list[str] = []
    aliased: list[str] = []
    pairs = [(name, "SUBFLEET_" + name[len("CARPOOL_"):])
             for name in sorted(env) if name.startswith("CARPOOL_")]
    pairs += [(old, new) for old, new in sorted(CARPOOL_EXTRA.items())]
    for old, new in pairs:
        if env.get(old) and not env.get(new):
            updates[new] = env[old]
            aliased.append(f"{old}->{new}")
    if aliased:
        notes.append(f"{PROG}: carpool-era names aliased for this call "
                     f"({', '.join(aliased)}); rename them, the aliasing was "
                     f"meant to last a week and it is long past that")
    for name, why in NOTED_ENV.items():
        if env.get(name):
            notes.append(f"{PROG}: ${name} is set and {why}")
    return updates, notes


# --- the v1 binary ------------------------------------------------------------

def v1_binary(env: dict[str, str] | None = None) -> str | None:
    """The v1 `subfleet` a delegated verb runs, or None when there is none.

    Never returns this process's own entry point: after the symlink flip
    `shutil.which("subfleet")` is v2, and delegating to it would recurse until
    the process table gave out.
    """
    env = os.environ if env is None else env
    candidates = [env.get(V1_BIN_ENV), V1_BIN_DEFAULT]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    found = shutil.which("subfleet")
    if found and not _is_self(found):
        return found
    return None


def _is_self(path: str) -> bool:
    """True when `path` is the program currently running (a delegation loop)."""
    try:
        return Path(path).resolve() == Path(sys.argv[0]).resolve()
    except (OSError, ValueError):
        return False


# --- translation --------------------------------------------------------------

def verb_paths() -> set[str]:
    """Every dotted subcommand path `cli.build_parser()` defines.

    Walked from the parser's own subparser actions rather than probed with
    argv, because probing a target prefix like `runs show` fails on its required
    positional and would report a perfectly reachable verb as missing.
    """
    from . import cli

    def walk(parser: Any, prefix: str) -> set[str]:
        found: set[str] = set()
        for action in parser._actions:                  # noqa: SLF001 - our parser
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict) or not hasattr(action, "add_parser"):
                continue
            for name, sub in choices.items():
                path = f"{prefix}.{name}" if prefix else name
                found.add(path)
                found |= walk(sub, path)
        return found

    return walk(cli.build_parser(), "")


def _target_path(argv: Sequence[str]) -> str:
    """The dotted path a table target names: its leading run of verb tokens.

    Everything from the first flag onward belongs to the flags — a target like
    `sessions continue --scope interrupted` names the verb `sessions.continue`,
    and `interrupted` is `--scope`'s value, not a third sub-verb. Skipping only
    the flags themselves would read it as `sessions.continue.interrupted` and
    report a perfectly reachable verb as missing.
    """
    path = []
    for item in argv:
        if item.startswith("-"):
            break
        path.append(item)
    return ".".join(path)


#: `_verb_of` for an argv that argparse answers by printing help and exiting 0.
#: Distinguished from `""` — a usage error — because the difference is the whole
#: question the case table asks: did a command that worked stop working?
HELP = "(help)"


def _verb_of(argv: Sequence[str]) -> str:
    """The dotted v2 verb path an argv resolves to, or "" if the parser refuses.

    Asked of the real parser rather than inferred, so the table cannot drift
    away from `cli.build_parser` without `self_check` noticing.
    """
    import contextlib
    import io

    from . import cli
    parser = cli.build_parser()
    try:
        # A probe, not a parse: argparse writes usage to stderr and help to
        # stdout on its way out, and neither belongs in a caller's output.
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            args = parser.parse_args(cli.rewrite_aliases(list(argv)))
    except SystemExit as exc:
        return HELP if int(exc.code or 0) == 0 else ""
    parts = [getattr(args, "command", None) or "status"]
    for attr in ("runs_command", "lanes_command", "daemon_command",
                 "sessions_command"):
        value = getattr(args, attr, None)
        if value:
            parts.append(value)
    return ".".join(parts)


def _refusal(verb: str, why: str) -> Mapping:
    return Mapping(disposition="refuse", exit_code=int(Exit.REFUSED), rule=verb,
                   notes=[f"{PROG}: {why}", f"  fix: {FRONT_DOOR}"])


#: The one place a permanent verb's OUTPUT changed rather than its spelling.
#: v1's `cmd_runs` (`cli.py:691-710`) printed the metadata JSON and then
#: `--- out.md ---` and the deliverable, unconditionally; v2 keeps stdout to one
#: thing at a time (C-17.4) and streams the deliverable only under `--out`.
#: Neither v2 form reproduces v1 — `--out` returns before the metadata block —
#: so nothing is rewritten here and the difference is said out loud instead, on
#: exactly the invocation that used to print output and now does not.
RUNS_SHOW_NOTE = (
    "runs show: v1 printed the deliverable after the metadata; v2 keeps stdout "
    "to one thing (C-17.4) — `subfleet runs show <id> --out` streams the "
    "deliverable, `--err` the saved stderr, and this form is the metadata")


def _runs_show_note(two: tuple[str, ...], kept: list[str]) -> list[str]:
    if two != ("runs", "show"):
        return []
    if any(item in ("--out", "--err", "--json") for item in kept[2:]):
        return []
    return [f"{PROG} {RUNS_SHOW_NOTE}"]


def _flag_refusal(rule: str, refusals: list[str]) -> Mapping:
    return Mapping(disposition="refuse", exit_code=int(Exit.INVALID_INPUT),
                   rule=rule, notes=refusals)


def translate(argv: Sequence[str], env: dict[str, str] | None = None) -> Mapping:
    """Map one v1 invocation onto a v2 disposition. Never raises, never writes."""
    argv = [str(item) for item in argv]
    env_updates, env_notes = map_env(env)

    def finish(mapping: Mapping) -> Mapping:
        mapping.env = env_updates
        mapping.notes = [*env_notes, *mapping.notes]
        if mapping.disposition in ("map", "note") and not mapping.verb:
            mapping.verb = _verb_of(mapping.argv)
        return mapping

    if not argv:
        return finish(Mapping("map", ["status"], rule="bare"))
    head = argv[0]

    if head in ("-h", "--help", "-V", "--version"):
        return finish(Mapping("map", list(argv), rule="passthrough"))
    if head.startswith("-"):
        # v1 prepends `status` to any argv whose first token is not a known verb
        # (`cli.py:1588`); v2's `rewrite_aliases` does the same for a flag. The
        # flags are then read against `status`, which is what makes a bare
        # `subfleet --cached` — a real v1 spelling — keep working.
        kept, flag_notes, refusals = scan_flags(["status", *argv])
        if refusals:
            return finish(_flag_refusal("status:v1-only-flag", refusals))
        return finish(Mapping("note" if flag_notes else "map", kept,
                              rule="flag-first", notes=flag_notes))

    if head in REFUSED:
        return finish(_refusal(head, REFUSED[head]))
    if head in HIDDEN:
        return finish(Mapping("delegate", list(argv), rule=f"hidden:{head}"))

    two = tuple(argv[:2])
    one = (head,)
    if two in DELEGATED_PAIRS:
        return finish(Mapping(
            "delegate", list(argv), rule=f"delegate:{' '.join(two)}",
            notes=[f"{PROG}: {DELEGATED_PAIRS[two]}; running v1's"]))
    if head in DELEGATED:
        return finish(Mapping(
            "delegate", list(argv), rule=f"delegate:{head}",
            notes=[f"{PROG}: `{head}` is not a v2 verb yet — {DELEGATED[head]}; "
                   f"running v1's"]))

    # `run --status` was never a dispatch: it printed v1's lane table and then
    # re-exec'd `pick codex` (v1 `delegate.py:378`). It is rewritten before the
    # flag scan so its other flags are read against `status`, not `run`.
    # `--stat` and `--statu` were unambiguous v1 abbreviations of it, and v2's
    # `run` has no `--stat*` flag at all, so they would otherwise reach a usage
    # error naming nothing.
    if head == "run" and any(_abbreviates("--status", item) for item in argv[1:]):
        rest = [item for item in argv[1:] if not _abbreviates("--status", item)]
        return finish(Mapping(
            "note", ["status", *[item for item in rest if item == "--json"]],
            rule="run:--status",
            notes=[f"{PROG} run --status was v1's lane table; that is "
                   f"`subfleet status` (accepted through milestone 8)"]))

    kept, flag_notes, refusals = scan_flags(argv)
    if refusals:
        key, _flags = _flag_table(argv)
        return finish(_flag_refusal(f"{key or head}:v1-only-flag", refusals))

    if two in RENAMED:
        to, why = RENAMED[two]
        return finish(Mapping("note", [*to, *kept[2:]],
                              rule=f"renamed:{' '.join(two)}",
                              notes=[f"{PROG}: {why} (accepted through milestone 8)",
                                     *flag_notes]))
    if one in RENAMED and two not in PERMANENT:
        to, why = RENAMED[one]
        return finish(Mapping("note", [*to, *kept[1:]], rule=f"renamed:{head}",
                              notes=[f"{PROG}: {why} (accepted through milestone 8)",
                                     *flag_notes]))

    if two in PERMANENT:
        notes = [*flag_notes, *_runs_show_note(two, kept)]
        return finish(Mapping("note" if notes else "map",
                              [*PERMANENT[two], *kept[2:]],
                              rule=f"permanent:{' '.join(two)}", notes=notes))
    if one in PERMANENT:
        return finish(Mapping("note" if flag_notes else "map",
                              [*PERMANENT[one], *kept[1:]],
                              rule=f"permanent:{head}", notes=flag_notes))

    # An unknown first token. v1 prepended `status` and let argparse fail with
    # its own message; v2 hands it to the v2 parser, which does the same. Either
    # way the caller sees a usage error, so this is a `map` and not a refusal.
    return finish(Mapping("map", list(argv), rule="unknown"))


# --- the front door -----------------------------------------------------------

def dispatch(argv: Sequence[str] | None = None, *,
             env: dict[str, str] | None = None,
             stderr: Any = None,
             runner=subprocess.run) -> int:
    """Translate, say what changed, then hand off. The process entry point.

    Notes go to stderr and the contract stays on stdout (C-17.4), so a script
    piping `subfleet runs --json` sees exactly what it saw under v1.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    stderr = sys.stderr if stderr is None else stderr
    mapping = translate(argv, env)
    for line in mapping.notes:
        print(line, file=stderr)
    for name, value in mapping.env.items():
        os.environ[name] = value
    if mapping.disposition == "refuse":
        return int(mapping.exit_code or Exit.REFUSED)
    if mapping.disposition == "delegate":
        return delegate(mapping.argv, stderr=stderr, runner=runner)
    from . import cli
    return cli.main(mapping.argv)


def delegate(argv: Sequence[str], *, stderr: Any = None,
             runner=subprocess.run) -> int:
    """Run v1 with this argv and return its exit code unchanged.

    Unchanged is the whole point for `gate`, whose 0-to-5 codes mean things no
    other verb's codes mean (agreement, operational error, invalid input,
    changes requested, blocked review, failed action — v1 README:717-720). A
    caller that reads those codes must not have them remapped onto C-17.3.
    """
    stderr = sys.stderr if stderr is None else stderr
    binary = v1_binary()
    if binary is None:
        # Exit 7, not 1. `gate`'s own codes run 0 to 5 and 1 means "operational
        # error" inside that scheme (v1 README:717-720), so returning 1 here
        # would hand a driving agent a gate verdict that no gate produced. 7 is
        # C-17.3's "refused (message names the rule and the fix)" and is outside
        # every delegated verb's range.
        print(f"{PROG}: `{argv[0] if argv else ''}` is not a v2 verb and the v1 "
              f"install it delegates to was not found", file=stderr)
        print(f"  fix: set {V1_BIN_ENV} to the v1 `subfleet`, or wait for the "
              f"milestone that brings this verb to v2", file=stderr)
        return int(Exit.REFUSED)
    try:
        done = runner([binary, *argv])
    except OSError as exc:
        print(f"{PROG}: cannot run {binary}: {exc}", file=stderr)
        return int(Exit.OPERATIONAL)
    return int(getattr(done, "returncode", 0) or 0)


# --- self check (doctor) ------------------------------------------------------

def self_check() -> dict[str, Any]:
    """Every rule still targets something the v2 parser accepts.

    `doctor` calls this before the cutover: a table row whose target verb was
    renamed or removed is a command that used to work and now prints a usage
    error, which is exactly the failure this lane exists to prevent.
    """
    paths = verb_paths()
    unreachable: list[str] = []
    targets = [(tokens, target) for tokens, target in PERMANENT.items()]
    targets += [(tokens, target) for tokens, (target, _n) in RENAMED.items()]
    for tokens, target in targets:
        if _target_path(target) not in paths:
            unreachable.append(f"{' '.join(tokens)} -> {' '.join(target)}")
    return {
        "rules": len(PERMANENT) + len(RENAMED) + len(REFUSED) + len(DELEGATED)
        + len(DELEGATED_PAIRS) + len(HIDDEN)
        + sum(len(flags) for flags in V1_ONLY_FLAGS.values()),
        "verbs": len(PERMANENT) + len(RENAMED) + len(REFUSED) + len(DELEGATED)
        + len(DELEGATED_PAIRS) + len(HIDDEN),
        "env": len(NOTED_ENV) + len(CARPOOL_EXTRA),
        "unreachable": unreachable,
    }


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(dispatch())
