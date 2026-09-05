"""v1 invocation compatibility: the parser layer in front of `subfleet/cli.py`.

When the `subfleet` symlink is repointed from v1 to v2, every command any agent
types today must keep working. This module is the front door that makes that
true. It sits in front of `cli.main`, never inside it: `cli.py` owns the v2 verb
table and the C-17.3 exit codes, and nothing here changes either.

Four dispositions, and the reasoning for each:

* **map** — the spelling is v1's and is PERMANENT (plan.md amendment 1: "every
  v1 verb spelling is permanent"). `runs`, `runs show`, `runs reap`, `jobs`,
  `show`, `status`, `capacity`, `wait`, `kill`, `notify`, `resume-codex`, `run`,
  and a bare `subfleet` are all in this class. A permanent spelling gets NO
  stderr note, ever — a note on a spelling the contract promises to keep is
  noise that trains agents to change working commands.

* **note** — the spelling is accepted but deprecated, so exactly one line goes
  to stderr naming the replacement (C-17.2). `enroll`, `hooks install`,
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
  broke them would break every v1 run already in flight. `gate` is delegated for
  the reason the lane brief gives — it lands in milestone 7 — and its 0-to-5
  exit codes pass through untouched (v1 README:717-720).

* **refuse** — `subfleet codex`, `subfleet claude`, and `subfleet mirror` are
  the direct provider verbs the agent contract tells sessions never to call
  (`~/.claude/CLAUDE.md` "Model routing"; v1's PreToolUse guard denies them
  inside a session). v2 refuses them at the CLI with exit 7, whose C-17.3
  meaning is "refused (message names the rule and the fix)".

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

#: Permanent v1 spellings that reach a v2 verb unchanged or through the aliases
#: `cli.rewrite_aliases` already applies (C-17.1). Listed here so `self_check`
#: can prove each one still reaches a handler, and so a future edit to
#: `cli.ALIASES` cannot quietly drop one.
PERMANENT: dict[tuple[str, ...], list[str]] = {
    ("status",): ["status"],
    ("capacity",): ["status"],
    ("runs",): ["runs"],
    ("runs", "show"): ["runs", "show"],
    ("runs", "reap"): ["runs", "reap"],
    ("jobs",): ["runs"],
    ("show",): ["runs", "show"],
    ("wait",): ["wait"],
    ("kill",): ["kill"],
    ("resume",): ["resume"],
    ("resume-codex",): ["resume"],
    ("notify",): ["ping"],
    ("ping",): ["ping"],
    ("run",): ["run"],
    ("lanes",): ["lanes"],
    ("why",): ["why"],
    ("daemon",): ["daemon"],
    ("doctor",): ["doctor"],
    ("hook",): ["hook"],
}

#: Deprecated v1 spellings: accepted, rewritten, and noted exactly once.
RENAMED: dict[tuple[str, ...], tuple[list[str], str]] = {
    ("enroll",): (["lanes", "enroll"],
                  "`enroll <credential>` is now `lanes enroll <credential>`"),
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

#: Direct provider verbs. The agent contract says never to call these from a
#: session and v1's PreToolUse guard denies them; v2 refuses them outright.
REFUSED: dict[str, str] = {
    "codex": "`subfleet codex` launches a provider directly, outside the "
             "ledger, the lane accounting, and the cancellation tree",
    "claude": "`subfleet claude` launches a provider directly, outside the "
              "ledger, the lane accounting, and the cancellation tree",
    "mirror": "`subfleet mirror` is v1's desktop-session mirror; it is not a "
              "dispatch path and v2 does not own it",
}

#: v1 verbs v2 has not built yet, delegated to the v1 binary with one note.
#: The value is the sentence the note carries after "not a v2 verb yet".
DELEGATED: dict[str, str] = {
    "gate": "gates land in milestone 7; v1 runs this one and its 0-to-5 exit "
            "codes come back unchanged",
    "sessions": "the sessions kit is a later milestone",
    "pick": "lane picking belongs to whichever side owns the lane "
            "(`subfleet lanes list` shows the owner)",
    "login": "lane credentials stay with v1 until `lanes transfer --to v2`",
    "reset": "lane resets stay with v1 until `lanes transfer --to v2`",
    "errors": "the error ledger is v1's",
    "watch": "the watchdog is v1's",
    "keepalive": "keepalive is v1's",
    "brief": "the morning brief is v1's",
    "handoff": "handoff is a later milestone",
    "tickle": "tickle is a later milestone",
    "muster": "muster is a later milestone",
    "revive": "revive is a later milestone",
}

#: v1's hidden verbs. Delegated like the rest but SILENTLY: every one of these
#: is called by a v1 runner through `$SUBFLEET_RUN_SUBFLEET`,
#: `$DELEGATE_SUBFLEET`, or `$SUBFLEET_CODEX_PICK`, and a note on stderr would
#: land in a runner's captured `err.log` on every single record it writes.
HIDDEN = ("_session-hook", "_tickle", "_canonical-model", "_api-lane-check",
          "_record-lane-run", "_record-run", "_record-codex-cooldown")

#: `subfleet run` flags v1's `delegate.py:_parser()` accepts and C-17.2 has no
#: equivalent for. Each changes where output lands or what the lane may do, so
#: dropping one silently would change the meaning of a working command. They are
#: refused by name with the v2 thing to use instead.
RUN_ONLY_V1: dict[str, str] = {
    "-b": "v1 passed `-b BRANCH` to the runner; v2 gives every job its own "
          "worktree (C-13) — use `-C DIR`, or `--in-place` to write where the "
          "caller stands",
    "--reuse-out": "v1's `--reuse-out` dispatched onto an `-o` path a live run "
                   "was still writing; v2 exports per job (C-8) and has no "
                   "such override",
    "--independent-review": "a hidden v1 flag that passed `-I -D <root>` to the "
                            "runner; v2 has no independent-review mode yet",
    "--review-root": "a hidden v1 flag that named `--independent-review`'s "
                     "root; v2 has no independent-review mode yet",
}

#: Status flags v1 accepted that v2's `status` does not.
STATUS_ONLY_V1: dict[str, str] = {
    "--cached": "v1's `status --cached` read the last watchdog snapshot; v2's "
                "`status` reads the daemon, and reads the store read-only when "
                "no daemon is listening (C-17.5), so it is never a network call",
}


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
    found = shutil.which("subfleet-gate") or shutil.which("subfleet")
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
    """The dotted path a table target names, ignoring its flags."""
    return ".".join(item for item in argv if not item.startswith("-"))


def _verb_of(argv: Sequence[str]) -> str:
    """The dotted v2 verb path an argv resolves to, or "" if the parser refuses.

    Asked of the real parser rather than inferred, so the table cannot drift
    away from `cli.build_parser` without `self_check` noticing.
    """
    from . import cli
    parser = cli.build_parser()
    try:
        args = parser.parse_args(cli.rewrite_aliases(list(argv)))
    except SystemExit:
        return ""
    parts = [getattr(args, "command", None) or "status"]
    for attr in ("runs_command", "lanes_command", "daemon_command"):
        value = getattr(args, attr, None)
        if value:
            parts.append(value)
    return ".".join(parts)


def _refusal(verb: str, why: str) -> Mapping:
    return Mapping(disposition="refuse", exit_code=int(Exit.REFUSED), rule=verb,
                   notes=[f"{PROG}: {why}", f"  fix: {FRONT_DOOR}"])


def _run_flag_refusal(flags: list[str]) -> Mapping:
    notes = [f"{PROG} run: {flag} is a v1 flag with no v2 equivalent — "
             f"{RUN_ONLY_V1[flag]}" for flag in flags]
    return Mapping(disposition="refuse", exit_code=int(Exit.INVALID_INPUT),
                   rule="run:v1-only-flag", notes=notes)


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
        # (`cli.py:1588`); v2's `rewrite_aliases` does the same for a flag.
        return finish(Mapping("map", ["status", *argv], rule="flag-first"))

    if head in REFUSED:
        return finish(_refusal(head, REFUSED[head]))
    if head in HIDDEN:
        return finish(Mapping("delegate", list(argv), rule=f"hidden:{head}"))
    if head in DELEGATED:
        return finish(Mapping(
            "delegate", list(argv), rule=f"delegate:{head}",
            notes=[f"{PROG}: `{head}` is not a v2 verb yet — {DELEGATED[head]}; "
                   f"running v1's"]))

    two = tuple(argv[:2])
    one = (head,)
    if two in RENAMED:
        to, why = RENAMED[two]
        return finish(Mapping("note", [*to, *argv[2:]], rule=f"renamed:{' '.join(two)}",
                              notes=[f"{PROG}: {why} (accepted through milestone 8)"]))
    if one in RENAMED and two not in PERMANENT:
        to, why = RENAMED[one]
        return finish(Mapping("note", [*to, *argv[1:]], rule=f"renamed:{head}",
                              notes=[f"{PROG}: {why} (accepted through milestone 8)"]))

    if head == "run":
        offenders = [flag for flag in RUN_ONLY_V1
                     if flag in argv[1:] or any(item.startswith(flag + "=")
                                                for item in argv[1:])]
        if offenders:
            return finish(_run_flag_refusal(sorted(offenders)))
        if "--status" in argv[1:]:
            rest = [item for item in argv[1:] if item != "--status"]
            return finish(Mapping(
                "note", ["status", *[item for item in rest if item == "--json"]],
                rule="run:--status",
                notes=[f"{PROG} run --status was v1's lane table; that is "
                       f"`subfleet status` (accepted through milestone 8)"]))
        return finish(Mapping("map", list(argv), rule="run"))

    if head in ("status", "capacity"):
        offenders = [flag for flag in STATUS_ONLY_V1 if flag in argv[1:]]
        if offenders:
            rest = [item for item in argv[1:] if item not in STATUS_ONLY_V1]
            return finish(Mapping(
                "note", [*PERMANENT[one], *rest], rule=f"{head}:--cached",
                notes=[f"{PROG} {head}: {flag} is dropped — "
                       f"{STATUS_ONLY_V1[flag]}" for flag in offenders]))

    if two in PERMANENT:
        return finish(Mapping("map", [*PERMANENT[two], *argv[2:]],
                              rule=f"permanent:{' '.join(two)}"))
    if one in PERMANENT:
        return finish(Mapping("map", [*PERMANENT[one], *argv[1:]],
                              rule=f"permanent:{head}"))

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
        print(f"{PROG}: `{argv[0] if argv else ''}` needs the v1 install and "
              f"none was found", file=stderr)
        print(f"  fix: set {V1_BIN_ENV} to the v1 `subfleet`, or wait for the "
              f"milestone that brings this verb to v2", file=stderr)
        return int(Exit.OPERATIONAL)
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
        + len(HIDDEN) + len(RUN_ONLY_V1) + len(STATUS_ONLY_V1),
        "verbs": len(PERMANENT) + len(RENAMED) + len(REFUSED) + len(DELEGATED)
        + len(HIDDEN),
        "env": len(NOTED_ENV) + len(CARPOOL_EXTRA),
        "unreachable": unreachable,
    }


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(dispatch())
