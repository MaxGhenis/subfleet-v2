"""Mutation check for D-STATUS-SRC1: status and alerts name the source of every lane closure.

Each mutant takes one decision of `docs/reports/2026-10-24-status-hold-source.md`
back out of the code, and is paired with the tests whose assertions must catch
it. The unmutated tree runs every named test first and must pass; then each
mutant runs its own tests and must fail at least one of them with a `FAILED`
line (an import or collection error does not count as killing it). Run from the
repository root, with the mutated files clean, or from a `git archive` export of
the tree (review r1 of #263 ran it so, never in a worktree):

    .venv/bin/python tools/status_hold_source_mutations.py [ID ...]

Given IDs (`R1`, `M15`), only those mutants run, and the baseline covers only
their tests (review r2 of #263 ran four so, under the UK Budget hold).

A JUnit file of a run of the same unmutated tree, at `.mutants-baseline.xml`
in the tree's root, stands for the baseline of each named test it shows passed
(every case of it, parametrized ones included); only the rest are run first.

It rewrites the files in place, one mutant at a time, and restores them whatever
happens. M1 to M7 are the build's; M8 to M14 are review r1's (M14 in two
halves, the daemon's refusal and the CLI's); R1 and M15 to M17 are review
r2's. It exits 1 if the baseline fails, or a mutant survives or no longer
applies. It is a development check, not part of the suite.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

STATUS, ALERTS, DAEMON = Path("subfleet/status_json.py"), Path("subfleet/alerts.py"), Path("subfleet/daemon.py")
CONTRACTS, CLI = Path("subfleet/contracts.py"), Path("subfleet/cli.py")
TESTS = "tests/unit/test_status_hold_source.py"
GUARD = Path(TESTS)
SIX_HELD = f"{TESTS}::test_all_six_codex_lanes_held_by_an_operator_read_held_not_limited"
I1 = f"{TESTS}::test_i1_dispatchable_equals_the_frozen_release_217_function"
I2 = f"{TESTS}::test_i2_limited_iff_an_account_limit_and_held_iff_only_a_hold"
I3 = f"{TESTS}::test_i3_fleet_empty_counts_sum_and_limited_words_need_a_limited_lane"
I4 = f"{TESTS}::test_i4_a_note_round_trips_or_is_refused_with_the_store_unchanged"
I5 = f"{TESTS}::test_i5_no_credential_reaches_the_store_status_alerts_or_a_notice_through_a_hold"
DAEMON_REFUSAL = f"{TESTS}::test_r1_p2_the_daemon_refuses_a_credential_note_and_names_it_nowhere"
HOLD_NOTE_SPEC = f"{TESTS}::test_hold_note_accepts_exactly_the_spec_and_round_trips_through_source_event"
STORED_CREDENTIAL = f"{TESTS}::test_r1_p2_a_stored_credential_note_is_withheld_by_every_reader"
EVERY_READER = f"{TESTS}::test_r2_p3_2_every_reader_withholds_a_stored_note"
STRICT_SOURCE = f"{TESTS}::test_r2_p3_3_a_hold_source_lanes_hold_would_not_write_is_withheld"
CLI_REFUSAL = f"{TESTS}::test_r1_p2_lanes_hold_refuses_a_credential_note_without_naming_it"
#: The source guard as it was before review r1 of #263: lines filtered, then matched.
OLD_GUARD = ('''for number, text in enumerate(path.read_text().splitlines(), start=1)
            if "source_event" in text and re.search(r"""source_event\\W{0,3}\\s*(==|!=)|['"]operator['"]|'''
             '''source_event\\s*(=|IN)\\s*\\(?'operator""", text, re.IGNORECASE)]''')

HELD_PART = '        parts.append(f"{len(held)} held by operator"'
LIMITED_PART = '        parts.append(f"{len(limited)} provider-limited"'
OTHER_PART = '            parts.append(f"{len(causes[cause])} {words}")'
READY = '''    return (_admission_verdict(lane) in READY_VERDICTS
            and not any(row.get("scope") == "account" for row in lane.get("closures", ())))
'''

#: name: ([(file, the decision's code, what replaces it)], the tests that must catch it)
MUTANTS = {
    "M1 verdict held -> limited": (
        [(STATUS, '    if operator_held(lane):\n        return "held"\n',
          '    if operator_held(lane):\n        return "limited"\n')],
        [SIX_HELD, I2, f"{TESTS}::test_i1_a_held_lane_is_not_dispatchable_whatever_its_cycle_said"]),
    "M2 held counts as dispatchable": (
        [(STATUS, READY, READY.rstrip("\n").removesuffix(")") + '\n            or lane_verdict(lane) == "held")\n')],
        [I1, f"{TESTS}::test_i1_a_held_lane_is_not_dispatchable_whatever_its_cycle_said"]),
    "M3 the per-cause counts dropped": (
        [(ALERTS, HELD_PART, '        parts.append("held by operator"'),
         (ALERTS, LIMITED_PART, '        parts.append("provider-limited"'),
         (ALERTS, OTHER_PART, '            parts.append(f"{words}")')],
        [SIX_HELD, I3, f"{TESTS}::test_a_mix_of_holds_and_provider_limits_names_each_with_counts"]),
    "M4 holds said to be exhausted": (
        [(ALERTS, HELD_PART, '        parts.append(f"{len(held)} held by operator, exhausted"')],
        [SIX_HELD, I3]),
    "M5 the note stored without validation": (
        [(DAEMON, "                note = hold_note(a.note)\n", "                note = a.note\n")],
        [I4]),
    "M6 a per-lane limit alert for a hold": (
        [(ALERTS, '            if closure.get("reason") in ("auth-dead", "operator-hold"):\n',
          '            if closure.get("reason") == "auth-dead":\n')],
        [SIX_HELD, f"{TESTS}::test_claude_lanes_all_held_raise_no_per_lane_limit_and_stay_warn"]),
    "M7 an all-held fleet left critical": (
        [(ALERTS, '            add(key, "warn" if held_only or provider != "codex" else "critical",\n',
          '            add(key, "critical" if provider == "codex" else "warn",\n')],
        [SIX_HELD, I3]),
    # Review r1 of #263.
    "M8 I3's rendered count: `1 disabled` said `disabled`": (
        [(ALERTS, OTHER_PART, OTHER_PART.replace('{words}")', '{words}".replace("1 disabled", "disabled"))'))],
        [I3, f"{TESTS}::test_r1_p3_4_i3_catches_a_rendered_count_dropped",
         f"{TESTS}::test_a_disabled_lane_reads_disabled_and_the_fleet_is_not_all_held"]),
    "M9 hold_note without the credential check": (
        [(CONTRACTS, "    if holds_credential(note) or holds_credential(json.dumps(note)[1:-1]):\n"
                     "        raise ValueError(HOLD_NOTE_CREDENTIAL_RULE)\n", "")],
        [I4, I5, DAEMON_REFUSAL, CLI_REFUSAL, f"{TESTS}::test_r1_p2_the_credential_rule_is_a_contract_text_of_its_own",
         f"{TESTS}::test_r1_p2_a_credential_note_reaches_no_status_alert_or_notice",
         HOLD_NOTE_SPEC]),
    "M10 the reader backstop dropped": (
        [(CONTRACTS, "    try:\n        note = hold_note(written)\n    except ValueError:\n        return HOLD_NOTE_WITHHELD\n",
          "    note = written\n")],
        [STORED_CREDENTIAL]),
    "M11 dispatchable reads the evidence before the account-closure test": (
        [(STATUS, '    if any(row.get("scope") == "account" for row in lane.get("closures", ())):\n'
                  '        return "limited"\n    return _readings_verdict(lane)\n',
          "    return _readings_verdict(lane)\n")],
        [f"{TESTS}::test_r1_p3_1_a_closed_lane_keeps_the_release_answer_whatever_its_readings",
         f"{TESTS}::test_i1_with_malformed_evidence_dispatchable_answers_or_raises_as_release_did"]),
    "M12 the hold answer's nested closure rows left raw": (
        [(DAEMON, "        view = closure_rows(self._capacity_view(self._desktop_identity()))\n"
                  "        return {\"held\": a.lane_id",
          "        view = self._capacity_view(self._desktop_identity())\n"
          "        view = {**view, \"closures\": closure_rows({\"closures\": view[\"closures\"], \"lanes\": []})[\"closures\"]}\n"
          "        return {\"held\": a.lane_id")],
        [f"{TESTS}::test_r1_p3_2_the_hold_answer_enriches_nested_closure_rows"]),
    "M13 the source guard filters lines before matching": (
        [(GUARD, "for number in _comparisons(path.read_text(), str(path))]", OLD_GUARD)],
        [f"{TESTS}::test_r1_p3_3_the_source_guard_catches_each_comparison"]),
    "M14a the daemon's refusal echoes the note": (
        [(DAEMON, 'raise protocol.ProtocolError(f"lanes hold: {rule}", Exit.INVALID_INPUT, reason="hold-note",',
          'raise protocol.ProtocolError(f"lanes hold: {rule}: {a.note}", Exit.INVALID_INPUT, reason="hold-note",')],
        [DAEMON_REFUSAL]),
    "M14b the CLI's refusal echoes the note": (
        [(CLI, 'return fail(Exit.INVALID_INPUT, f"lanes hold: {refused}")',
          'return fail(Exit.INVALID_INPUT, f"lanes hold: {refused} ({note!r})")')],
        [CLI_REFUSAL]),
    # Review r2 of #263.
    "R1 a note over 160 characters read, not withheld": (
        [(CONTRACTS, "    if not note or len(note) > HOLD_NOTE_MAX or not note.isprintable():\n",
          "    if not note or not note.isprintable():\n")],
        [f"{TESTS}::test_r2_p3_5_a_stored_note_over_160_characters_is_withheld_unread", EVERY_READER, I4,
         HOLD_NOTE_SPEC, f"{TESTS}::test_lanes_hold_refuses_an_invalid_note_before_asking_the_daemon"]),
    "M15 hold_note without the JSON form": (
        [(CONTRACTS, "    if holds_credential(note) or holds_credential(json.dumps(note)[1:-1]):\n",
          "    if holds_credential(note):\n")],
        [f"{TESTS}::test_r2_p3_1_a_url_password_after_a_non_ascii_letter_is_refused",
         f"{TESTS}::test_r2_p3_1_lanes_hold_refuses_it_before_asking_the_daemon", I5, STRICT_SOURCE]),
    "M16 closure_row shows any source opening with the word as stored": (
        [(STATUS, '    if closure_kind(row) == "hold" and "source_event" in row:\n',
          '    if closure_kind(row) == "hold" and not str(row.get("source_event")).startswith("operator"):\n')],
        [STRICT_SOURCE, STORED_CREDENTIAL]),
    "M17 the lanes list answer left unrouted": (
        [(DAEMON, '            return {"lanes": closure_rows(self._capacity_view(self._desktop_identity()))["lanes"],\n',
          '            return {"lanes": self._capacity_view(self._desktop_identity())["lanes"],\n')],
        [EVERY_READER]),
}


#: A JUnit file of the same unmutated tree's run: its passing tests need no baseline run.
BASELINE_JUNIT = Path(".mutants-baseline.xml")


def junit_passed(path: Path) -> set[str]:
    """The `file::test` nodes whose every case passed in the JUnit file at `path`."""
    passed, failed = set(), set()
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        if not classname or not name:
            continue                    # an interrupted run's marker, not a test
        node = classname.replace(".", "/") + ".py::" + name.split("[")[0]
        (failed if any(child.tag in ("failure", "error", "skipped") for child in case) else passed).add(node)
    return passed - failed


def run(nodes: list[str]) -> tuple[int, list[str]]:
    """pytest over `nodes`: its exit status and its `FAILED` summary lines."""
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "-rf",
                           *dict.fromkeys(nodes)], capture_output=True, text=True)
    return done.returncode, [line[len("FAILED "):] for line in done.stdout.splitlines() if line.startswith("FAILED ")]


def main(only: list[str]) -> int:
    unknown = [key for key in only if not any(name.split()[0] == key for name in MUTANTS)]
    if unknown:
        print(f"no such mutant: {', '.join(unknown)}", file=sys.stderr)
        return 2
    mutants = {name: value for name, value in MUTANTS.items() if not only or name.split()[0] in only}
    files = sorted({path for edits, _ in mutants.values() for path, _, _ in edits})
    # A `git archive` export (no checkout) has no uncommitted work to protect.
    checkout = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode == 0
    if checkout and subprocess.run(["git", "diff", "--quiet", "--", *map(str, files)]).returncode:
        print(f"{', '.join(map(str, files))}: uncommitted changes; commit them first", file=sys.stderr)
        return 2
    named = list(dict.fromkeys(node for _, nodes in mutants.values() for node in nodes))
    prior = junit_passed(BASELINE_JUNIT) & set(named) if BASELINE_JUNIT.exists() else set()
    todo = [node for node in named if node not in prior]
    rc, failed = run(todo) if todo else (0, [])
    print(f"baseline  {'passed' if rc == 0 else 'FAILED'}  {len(named)} named tests: "
          f"{len(prior)} passed in {BASELINE_JUNIT if prior else 'no prior run'}, {len(todo)} run now")
    if rc:
        print("\n".join(failed))
        return 1
    originals, survived = {path: path.read_text() for path in files}, []
    try:
        for name, (edits, nodes) in mutants.items():
            texts = dict(originals)
            if any(texts[path].count(rule) != 1 for path, rule, _ in edits):
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            for path, rule, replacement in edits:
                texts[path] = texts[path].replace(rule, replacement)
            for path in {path for path, _, _ in edits}:
                path.write_text(texts[path])
            try:
                rc, failed = run(nodes)
            finally:
                for path in files:
                    path.write_text(originals[path])
            caught = [line for line in failed if line.split(" - ")[0].split("[")[0] in nodes]
            print(f"{'killed  ' if rc and caught else 'SURVIVED'}  {name}")
            for line in caught:
                print(f"            {line[:220]}")
            if not (rc and caught):
                survived.append(name)
    finally:
        for path, text in originals.items():
            path.write_text(text)
    print(f"{len(mutants) - len(survived)} of {len(mutants)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
