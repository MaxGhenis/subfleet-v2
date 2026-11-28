"""Mutants of the guard sync r2's hook edit (the two Axiom blocks and the Claude prefilter line), of its fix
r1 (the git config pin, the batched reads and the blocks' parsing, plus review r1's M3), of its fix r2 (each
folder resolved before git runs, the time budget, raw staged names, --no-relative) and of its fix r3 (each cd
splitting only its own words, a cd target's `..` by name beside its physical spelling, the cap on pairs
examined), each to be killed by a named test.

    python tools/gs_mutants.py --scratch <dir containing /sf->

Re-created on 2026-11-14, for the merge of release/217 into the guard sync, from the Mutants table of the r2
build report (docs/reports/2026-11-11-guard-sync.md); the build's own script was not kept. Each mutant is
applied alone to the hook in a fresh `git archive` of HEAD under the scratch directory (never to this
checkout), and SELECTION runs there: DriftTests, the two pins and the undo/rebuild checks, and both Axiom
rules' `blocked` decision cases. A kill is a failed test (a junit <failure>): an error, a skip or a pass is
not. Each mutant names the test that must kill it; every other failure is listed beside it. The unmutated
control must pass every selected test first. Prints one JSON row per run; exit 0 only when the control passes
and every mutant is killed by its named test. Exports are removed afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[1]
HOOK = "private/guard/never-rules-hook.sh"
BLOCKS = "tests/fixtures/guard/claude-blocks"
DOC = "docs/private-guard-overlay.md"
SYNC = "tests/unit/test_guard_sync.py"
OVERLAY = "tests/unit/test_guard_overlay.py"
BASE = "tests/unit/test_guard_subfleet_root.py"
FILES = [SYNC, OVERLAY, BASE]
SELECTION = ("DriftTests or test_private_overlay_bytes or test_gs_undoing or test_merge_undoing"
             " or test_only_the_roots or (axiom_brief_regime and blocked) or (hand_authored_rulespec and blocked)"
             " or test_fix_r1 or fsmonitor or staged_file_count or later_rules or test_fix_r2 or spelled"
             " or time_budget or not_there or too_many_places or relative_diff or test_fix_r3 or grandchild"
             " or long_cd_chain or link_and_dotdot or physical_spelling or folder_and_command_pairs")
#: The first prefilter literal's line, which the Claude hook's prefilter line (claude-blocks/prefilter.txt) heads.
PREFILTER_HEAD = "printf '%s' \"$command\" | grep -qE 'pending_deployments|"
#: The first literal's git term since the fix r1 (2026-11-15); before it, `git([[:space:]]+-C…)?[[:space:]]+commit`.
COMMIT_TERM = "|git[[:space:]].*(commit|\\\\$)"
#: The fix r1's git config pin (tests/fixtures/guard/claude-blocks/git-config-pin.txt).
PIN = "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false\n"
SQUASH_HEAD = "# --- corpus-squash: signed-manifest repos merge-commit, never squash ---\n"
#: The join each block has had: fix r3's (2026-11-26), which splits only the new word, as its mutants 8, 13 and
#: 15 find it; fix r2's (2026-11-17), which split the whole path again; and fix r1's, which split nothing.
HAND_JOIN = """    function join(base, p,   a, n, i, r) {
      p = expand(p)
      if (substr(p, 1, 1) == "/") base = "/"
      n = split(p, a, "/"); r = ""
      for (i = 1; i <= n; i++) if (a[i] != "" && a[i] != ".") r = r "/" a[i]
      return r == "" ? base : (base == "/" ? r : base r)
    }
"""
HAND_JOIN_R2 = '    function join(base, p) { p = expand(p); return norm(substr(p, 1, 1) == "/" ? p : base "/" p) }\n'
HAND_JOIN_R1 = '    function join(base, p) { p = expand(p); return substr(p, 1, 1) == "/" ? p : base "/" p }\n'
HAND_RESOLVE = ("gdir=$(CDPATH= cd -P -- \"$hr_d\" 2>/dev/null || exit 1; { pwd -P || printf '%s\\n' \"$hr_d\"; } "
                "2>/dev/null; echo x) || continue")
BRIEF_JOIN = """    function join(base, p, file,   a, n, i, r) {
      p = expand(p)
      if (substr(p, 1, 1) == "/") base = "/"
      n = split(p, a, "/"); r = ""
      for (i = 1; i <= n; i++) if (a[i] != "" && a[i] != ".") r = r "/" a[i]
      if (r != "") base = (base == "/" ? "" : base) r
      if (file && base != "/" && ("/" p) ~ /\\/\\.?$/) base = base "/"
      return base
    }
"""
BRIEF_JOIN_R2 = ('    function join(base, p, file) { p = expand(p); return norm(substr(p, 1, 1) == "/" ? p : base "/" p, '
                 'file) }\n')
BRIEF_JOIN_R1 = '    function join(base, p, file) { p = expand(p); return substr(p, 1, 1) == "/" ? p : base "/" p }\n'
#: The fix r3's cd step (both blocks): the logical folder, and the physical spelling kept as a folder of its own.
CD_LOGICAL = "else { cur = cdl(cur, v); pcur = join(pcur, v) }"
CD_PHYSICAL = "          if (!(pcur in known)) { known[pcur] = 1; dirs[++nd] = pcur }\n"
PAIR_CAPS = ('          if ((wk += 1 + nc + np) > 4096) { print "TOOMANY"; exit }\n',
             '          if ((wk += 1 + nc) > 4096) { print "TOOMANY"; exit }\n')
BRIEF_RESOLVE = ("pf_abs=$(CDPATH= cd -P -- \"${ab_dir:-/}\" 2>/dev/null || exit 1; { pwd -P || printf '%s\\n' "
                 "\"${ab_dir:-/}\"; } 2>/dev/null; echo x) || continue")
BUDGET = '[ "$hr_n" -le 2 ] || [ $((SECONDS - hr_t0)) -lt 15 ] || block "$hr_late"'
RAW_NAMES = ("diff --cached --name-only -z --no-relative --diff-filter=ACMR 2>/dev/null \\\n"
             "      | LC_ALL=C tr '\\n\\0' '\\001\\n' \\\n")
NEWLINE_DENIED = "if (f[i] ~ /\\001/ || ("

DRIFT = f"{SYNC}::DriftTests::"
#: name -> (what it does, the named test that must kill it)
MUTATIONS = {
    "1-drop-hand-authored-rulespec": ("the [hand-authored-rulespec] block removed whole",
                                      DRIFT + "test_every_pinned_block_is_in_the_overlay_byte_for_byte"),
    "2-one-byte-in-axiom-brief-regime": ("[axiom-brief-regime]'s message: `This brief` -> `this brief`",
                                         DRIFT + "test_every_pinned_block_is_in_the_overlay_byte_for_byte"),
    "3-commit-term-out-of-the-prefilter": ("the fix r1's `git[[:space:]].*(commit|\\\\$)` term (COMMIT_TERM) left "
                                           "out of the first prefilter literal",
                                           DRIFT + "test_the_first_prefilter_literal_is_the_claude_hooks_line"),
    "4-swap-the-two-blocks": ("[hand-authored-rulespec] before [axiom-brief-regime]",
                              DRIFT + "test_the_overlay_keeps_its_nine_rule_blocks_in_order"),
    # the fix r1 (2026-11-15): the review r1's M3, then the fix's own
    "5-m3-corpus-squash-reads-the-hand-blocks-folder": (
        "`cwd=${gdir:-$cwd}` at the top of corpus-squash (review r1's M3)",
        f"{SYNC}::test_later_rules_decide_alike_after_an_axiom_command[corpus-squash]"),
    "6-no-git-config-pin": ("the core.fsmonitor pin line removed",
                            f"{SYNC}::test_no_git_call_runs_the_repositorys_fsmonitor[git -C names the checkout]"),
    "7-the-supplied-hand-block": ("[hand-authored-rulespec] back to the hub's 2026-11-11 cut (per-file reads)",
                                  f"{SYNC}::test_the_staged_file_count_does_not_multiply_git_processes"),
    # the fix r2 (2026-11-17): each of its changes reverted alone
    "8-hand-keys-by-path-text": ("[hand-authored-rulespec] keys each folder by its path text again: no `.` collapse "
                                 "in join, no cd -P/pwd -P (a missing folder still skipped)",
                                 f"{SYNC}::test_one_checkout_spelled_many_ways_is_checked_once"),
    "9-no-time-budget": ("the three budget checks before the hand block's git calls removed",
                         f"{SYNC}::test_the_hand_block_denies_when_its_time_budget_is_spent"),
    "10-quoted-staged-names": ("the hand block's diff without -z and the tr (git's quoted names again)",
                               f"{SYNC}::test_same_decisions_axiom_hand_authored_rulespec"
                               "[a non-ASCII rule name w/o manifest blocked]"),
    "11-a-newline-name-looked-up": ("a staged name holding \\001 (a newline) looked up, not denied",
                                    f"{SYNC}::test_same_decisions_axiom_hand_authored_rulespec"
                                    "[a newline in the name blocked, a \\\\001 manifest staged]"),
    "12-relative-diff": ("--no-relative dropped from the hand block's diff",
                         f"{SYNC}::test_a_relative_diff_config_hides_no_staged_file"),
    "13-brief-keys-by-path-text": ("[axiom-brief-regime] keys each brief by its path text again: no `.` collapse in "
                                   "join, no cd -P/pwd -P",
                                   f"{SYNC}::test_one_brief_spelled_17_ways_is_read_once"),
    "14-git-runs-in-a-missing-folder": ("a folder cd -P cannot enter goes to git as written, instead of being skipped",
                                        f"{SYNC}::test_a_folder_that_is_not_there_starts_no_git_process"),
    # the fix r3 (2026-11-26): each of its changes reverted alone
    "15-join-splits-the-folder-again": ("both blocks' join back to the fix r2's, which splits the stored folder again "
                                        "at every cd (the physical spelling's walk is quadratic again)",
                                        f"{SYNC}::test_a_long_cd_chain_stays_within_its_cpu_bound"
                                        "[hand-cd_a-4000-overlay]"),
    "16-no-logical-dotdot": ("a cd target's `..` kept for cd -P again, in both blocks (cur = join, not cdl)",
                             f"{SYNC}::test_a_cd_through_a_link_and_dotdot_is_judged_where_the_shell_lands"),
    "17-no-physical-spelling": ("a cd target's physical spelling no longer a folder to check, in both blocks",
                                f"{SYNC}::test_the_physical_spelling_of_a_cd_target_is_still_judged"),
    "18-no-pair-cap": ("the cap on (folder, command) pairs removed from both blocks",
                       f"{SYNC}::test_too_many_folder_and_command_pairs_are_refused"),
    "19-dotdot-spelling-split-again": ("the physical spelling of a target holding `..` normalized by splitting the "
                                       "whole path again (norm(pcur \"/\" v)), in both blocks",
                                       f"{SYNC}::test_a_long_cd_chain_stays_within_its_cpu_bound"
                                       "[hand-cd_a..-4000-overlay]"),
}

#: What the selection needs of HEAD.
EXPORTED = ["subfleet", "tests", "tools", "private", "bin", "pyproject.toml", DOC]


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *EXPORTED],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def mutate(tree: Path, name: str) -> None:
    path = tree / HOOK
    text = path.read_text(encoding="utf-8")
    brief = (tree / BLOCKS / "axiom-brief-regime.sh").read_text(encoding="utf-8")
    hand = (tree / BLOCKS / "hand-authored-rulespec.sh").read_text(encoding="utf-8")
    if name == "1-drop-hand-authored-rulespec":
        assert text.count(hand) == 1
        text = text.replace(hand, "")
    elif name == "2-one-byte-in-axiom-brief-regime":
        old = 'block "[axiom-brief-regime] This brief'
        assert text.count(old) == 1
        text = text.replace(old, 'block "[axiom-brief-regime] this brief')
    elif name == "3-commit-term-out-of-the-prefilter":
        lines = text.split("\n")
        at = [i for i, line in enumerate(lines) if line.startswith(PREFILTER_HEAD)]
        assert len(at) == 1 and lines[at[0]].count(COMMIT_TERM) == 1
        lines[at[0]] = lines[at[0]].replace(COMMIT_TERM, "")
        text = "\n".join(lines)
    elif name == "4-swap-the-two-blocks":
        assert text.count(brief + hand) == 1
        text = text.replace(brief + hand, hand + brief)
    elif name == "5-m3-corpus-squash-reads-the-hand-blocks-folder":
        assert text.count(SQUASH_HEAD) == 1
        text = text.replace(SQUASH_HEAD, SQUASH_HEAD + "cwd=${gdir:-$cwd}\n")
    elif name == "6-no-git-config-pin":
        assert text.count(PIN) == 1
        text = text.replace(PIN, "")
    elif name == "7-the-supplied-hand-block":
        assert text.count(hand) == 1
        text = text.replace(hand, (tree / BLOCKS / "hand-authored-rulespec.before.sh").read_text(encoding="utf-8"))
    elif name == "8-hand-keys-by-path-text":
        text = once(once(text, HAND_JOIN, HAND_JOIN_R1), HAND_RESOLVE, "[ -d \"$hr_d\" ] || continue; gdir=$hr_d$'\\n'x")
    elif name == "9-no-time-budget":
        assert text.count(BUDGET) == 3
        text = text.replace(BUDGET, ":")
    elif name == "10-quoted-staged-names":
        text = once(text, RAW_NAMES, "diff --cached --name-only --no-relative --diff-filter=ACMR 2>/dev/null \\\n")
    elif name == "11-a-newline-name-looked-up":
        text = once(text, NEWLINE_DENIED, "if ((")
    elif name == "12-relative-diff":
        text = once(text, "--name-only -z --no-relative --diff-filter", "--name-only -z --diff-filter")
    elif name == "13-brief-keys-by-path-text":
        text = once(once(text, BRIEF_JOIN, BRIEF_JOIN_R1), BRIEF_RESOLVE, "pf_abs=$ab_dir$'\\n'x")
    elif name == "14-git-runs-in-a-missing-folder":
        text = once(text, HAND_RESOLVE, HAND_RESOLVE[:-len("|| continue")] + "|| gdir=$hr_d$'\\n'x")
    elif name == "15-join-splits-the-folder-again":
        text = once(once(text, HAND_JOIN, HAND_JOIN_R2), BRIEF_JOIN, BRIEF_JOIN_R2)
    elif name == "16-no-logical-dotdot":
        assert text.count(CD_LOGICAL) == 2
        text = text.replace(CD_LOGICAL, "else { cur = join(cur, v); pcur = join(pcur, v) }")
    elif name == "17-no-physical-spelling":
        assert text.count(CD_PHYSICAL) == 2
        text = text.replace(CD_PHYSICAL, "")
    elif name == "18-no-pair-cap":
        text = once(once(text, PAIR_CAPS[0], ""), PAIR_CAPS[1], "")
    elif name == "19-dotdot-spelling-split-again":
        assert text.count(CD_LOGICAL) == 2
        text = text.replace(CD_LOGICAL, 'else { cur = cdl(cur, v); pcur = v ~ /\\.\\./ ? norm(substr(v, 1, 1) == "/" ? v '
                                        ': pcur "/" v) : join(pcur, v) }')
    else:
        raise KeyError(name)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, scratch: Path, named: list[str] | None = None) -> dict[str, str]:
    """{file::test id: passed|failed|error|skipped} from one pytest run in ``tree``: of SELECTION, or of the
    ``named`` node ids alone."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GUARD_DIFF_CACHE")}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    f"--basetemp={scratch / ('pytest-gsm-' + tree.name)}",
                    *(named if named else ["-k", SELECTION, *FILES])],
                   cwd=tree, env=env, capture_output=True)
    outcomes: dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        parts = case.get("classname").split(".")
        module = next(i for i, part in enumerate(parts) if part.startswith("test_"))
        path = "/".join(parts[:module + 1]) + ".py"
        name = "::".join([path, *parts[module + 1:], case.get("name")])
        outcomes[name] = outcome
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=list(MUTATIONS))
    parser.add_argument("--no-control", action="store_true", help="skip the control (it ran in an earlier call)")
    parser.add_argument("--named-only", action="store_true",
                        help="run only the named tests (the control: every mutant's), not the whole SELECTION")
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        if not args.no_control:
            control = scratch / "control"
            export(control)
            named = {MUTATIONS[name][1] for name in MUTATIONS}
            outcome = pytest(control, scratch, sorted(named) if args.named_only else None)
            control_ok = bool(outcome) and all(value == "passed" for value in outcome.values()) \
                and named <= set(outcome)
            ok = control_ok
            print(json.dumps({"mutant": "control", "passed": control_ok, "tests": len(outcome),
                              "not_passed": sorted(k for k, v in outcome.items() if v != "passed")}), flush=True)
        for name in args.only:
            tree = scratch / name
            export(tree)
            original = (tree / HOOK).read_bytes()
            mutate(tree, name)
            assert (tree / HOOK).read_bytes() != original
            test = MUTATIONS[name][1]
            outcome = pytest(tree, scratch, [test] if args.named_only else None)
            killed = outcome.get(test) == "failed"
            ok = ok and killed
            failed = sorted(k for k, v in outcome.items() if v == "failed")
            print(json.dumps({"mutant": name, "what": MUTATIONS[name][0], "named_test": test, "killed": killed,
                              "failed": len(failed), "passed": sum(v == "passed" for v in outcome.values()),
                              "errors": sorted(k for k, v in outcome.items() if v == "error"),
                              "killed_by": failed}), flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
