"""Mutation check for the quoted subscription refusal (C-9.3, C-10.2, C-25.7).

Each mutation removes one rule of the refusal handling (quote Claude Code's own
refusal verbatim, never a bystander line or a model's words; carry the error kinds;
name both causes without choosing or promising; refuse at enrolment with or without
`system/init`; keep the match inside an excerpt; read only the CLI's words; let a
verified success supersede; show the client only the template), and the refusal
tests must fail under it. Those marked "review r1" are the first lane review's
findings and its two surviving mutations; those marked "rebase" are the rebase
brief's (2026-11-11). Run from the repository root, with the mutated files clean:

    uv run python tools/refusal_mutations.py [--only N,N,...] [--basetemp-root DIR]

It rewrites one file at a time in place, one mutation at a time, and restores it
from a pristine copy whatever happens. Each mutation names the test that should
kill it; that test runs first, and only if it passes do the whole test files run.
`--only` takes 1-based mutation numbers (`--list` prints them), so slices can run
apart; `--basetemp-root` gives each pytest run a new `--basetemp` under DIR. It
exits 1 if a mutation survives or no longer applies. It is a development check, not
part of the suite: it runs the tests once or twice per mutation.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ADAPTER = Path("subfleet/adapters/claude.py")
REFUSALS = Path("subfleet/refusals.py")
PR = "tests/unit/test_claude_subscription_refusal.py"
BOUNDS = "tests/unit/test_claude_refusal_boundaries.py"
TESTS = (PR, BOUNDS)

ENROLL = ('        refusal = _subscription_refusal(corpus, summary)\n'
          '        if refusal is not None:\n')
ENROLL_RAISE = ('            raise AdapterError(f"claude: {refusal.statement()}", code=5, fix=ORG_BLOCK_CAUSES,\n'
                '                               reason="claude-subscription-refused", details=refusal.details())\n')
WINDOW = '    high = max(low + LINE_EXCERPT_MAX, found_end)\n'
MARKED = '    return ("…" if low else "") + line[low:high] + ("…" if high < len(line) else "")\n'
STAMPED = "        if message.error == ORG_BLOCK_ERROR_KIND:\n"
PROVIDER_FIRST = "    for text in errors:\n        found = ORG_BLOCK_RE.search(text)\n"
PROVIDER_LAST = '    for text in errors:\n        found = re.search(r"\\S", text)\n'
CAUSES_END = '    "run `subfleet lanes enroll` again."\n'
CLI_CORPUS = '        corpus = "\\n".join([stderr, *summary.cli_texts()])\n'
GATE = "        refusal = None if successful else _subscription_refusal(cli_corpus, summary)\n"
CLI_FRAMES = "                 if m.error and m.text and cli_placeholder(m.raw))\n"
FIX = ('CLAUDE_REFUSAL_CAUSES = (\n'
       '    "Either the account\'s subscription lapsed or was cancelled, or an org admin "\n'
       '    "disabled Claude Code; once the account is subscribed with Claude Code allowed, "\n'
       '    "run `subfleet lanes enroll` again."\n'
       ')\n')
TEMPLATE = "    \"claude-subscription-refused\": ('claude: Claude Code refused subscription access for this account: '\n"
CHECK = "    if type(value) is not str:\n        return False\n    choices = _choices()\n"
FIRST = "    for message in summary.assistants:\n" + STAMPED


def pr(name: str) -> str:
    return f"{PR}::{name}"


def bounds(name: str) -> str:
    return f"{BOUNDS}::{name}"


#: name: (file, the rule's code, what replaces it, the test that should kill it)
MUTATIONS = {
    "enrolment says the organisation disabled access": (
        ADAPTER, ENROLL_RAISE,
        ENROLL_RAISE.replace('f"claude: {refusal.statement()}"',
                             '"claude: the organisation has disabled Claude Code subscription '
                             'access for this account"')
                    .replace("fix=ORG_BLOCK_CAUSES", "fix=\"ask the account's admin to enable "
                             "Claude Code access\""),
        pr("test_enroll_quotes_the_recorded_refusal_and_its_error_kind")),
    "enrolment refuses only after system/init (rebase 9)": (
        ADAPTER, ENROLL, ENROLL.replace("_subscription_refusal(corpus, summary)",
                                        "_subscription_refusal(corpus, summary) if summary.has_init else None"),
        pr("test_enroll_quotes_the_refusal_without_system_init")),
    "the auth-dead detail drops the causes and the kinds": (
        ADAPTER, 'f"auth-dead: {refusal.statement()}. {ORG_BLOCK_CAUSES}"', 'f"auth-dead: {refusal.line}"',
        pr("test_classify_quotes_the_recorded_refusal_with_its_error_kind")),
    "the cause line promises that enrolment succeeds (review r1, 3)": (
        ADAPTER, CAUSES_END, CAUSES_END.replace("run `subfleet lanes enroll` again.",
                                                "`subfleet lanes enroll` succeeds."),
        pr("test_the_shipped_cause_line_is_the_neutral_one")),
    "the cause line adds an admin-only direction (review r1, mutation C)": (
        ADAPTER, CAUSES_END, CAUSES_END.replace("again.", "again. Ask the org admin to turn Claude Code back on."),
        pr("test_the_shipped_cause_line_is_the_neutral_one")),
    "error kinds are not carried": (
        ADAPTER, "        if self.error_kinds:\n            evidence.append",
        "        if False:\n            evidence.append",
        pr("test_enroll_quotes_the_recorded_refusal_and_its_error_kind")),
    "error kinds repeat": (
        ADAPTER, "    kinds = tuple(dict.fromkeys(summary.error_kinds))", "    kinds = tuple(summary.error_kinds)",
        pr("test_enroll_lists_every_distinct_error_kind_once")),
    "the error kind alone is not a refusal": (
        ADAPTER, "    if not stamped and in_corpus is None:\n        return None",
        "    if in_corpus is None:\n        return None",
        pr("test_enroll_quotes_a_reworded_refusal_carried_only_by_its_error_kind")),
    "the stamped frame is not looked at": (
        ADAPTER, STAMPED, "        if False:\n",
        pr("test_enroll_quotes_a_reworded_refusal_carried_only_by_its_error_kind")),
    "any assistant frame is taken for the refusal (review r1, mutation A)": (
        ADAPTER, STAMPED, "        if True:\n",
        pr("test_a_progress_frame_is_not_quoted_for_a_kind_only_refusal")),
    "ordinary text outranks the provider's error text (review r1, 1)": (
        ADAPTER, PROVIDER_FIRST, PROVIDER_FIRST.replace("in errors", "in ()"),
        pr("test_the_provider_error_text_outranks_ordinary_text")),
    "a wordless stamped frame ignores the provider's error text (review r1, 2)": (
        ADAPTER, PROVIDER_LAST, PROVIDER_LAST.replace("in errors", "in ()"),
        pr("test_a_wordless_stamped_frame_quotes_the_provider_error_text")),
    "the evidence drops where the words came from": (
        ADAPTER, '                         "quoted_from": refusal.quoted_from},', '                         },',
        pr("test_classify_quotes_the_recorded_refusal_with_its_error_kind")),
    "a long line is cut at 300 whatever it held (rebase 8)": (
        ADAPTER, "    if len(line) <= LINE_EXCERPT_MAX:\n        return line\n", "    return line[:300]\n",
        pr("test_an_excerpt_always_holds_its_match")),
    "the window can end inside a long match": (
        ADAPTER, WINDOW, WINDOW.replace("found_end)", "found_end - 1)"), pr("test_an_excerpt_always_holds_its_match")),
    "trimmed indentation shifts the window": (
        ADAPTER, "    offset = start + len(raw) - len(raw.lstrip())", "    offset = start",
        pr("test_an_excerpt_always_holds_its_match")),
    "a cut is not marked": (ADAPTER, MARKED, "    return line[low:high]\n", pr("test_an_excerpt_always_holds_its_match")),
    # The rebase brief's mutants (8 and 9 are above).
    "enrolment reads summary.texts() again (rebase 1)": (
        ADAPTER, CLI_CORPUS, '        corpus = f"{stderr}\\n{chr(10).join(summary.texts())}"\n',
        bounds("test_a_model_quoting_the_refusal_does_not_refuse_enrolment")),
    "a verified success no longer supersedes the refusal (rebase 2)": (
        ADAPTER, GATE, "        refusal = _subscription_refusal(cli_corpus, summary)\n",
        bounds("test_a_verified_success_is_never_refused")),
    "the provider's error text admits every frame with an error (rebase 3)": (
        ADAPTER, CLI_FRAMES, "                 if m.error and m.text)\n",
        bounds("test_a_model_frame_with_an_error_kind_is_not_the_provider_s_error_text")),
    "the template's fix is the old one (rebase 4)": (
        REFUSALS, FIX, 'CLAUDE_REFUSAL_CAUSES = "ask the account\'s admin to enable Claude Code access"\n',
        bounds("test_the_template_s_fix_is_the_adapter_s_cause_line")),
    "the template's text is the old one (rebase 4)": (
        REFUSALS, TEMPLATE, TEMPLATE.replace("Claude Code refused subscription access for this account",
                                             "the organisation has disabled Claude Code subscription access "
                                             "for this account"),
        bounds("test_the_recorded_refusal_reaches_the_wire_whole_and_daemon_log_has_every_word")),
    "enrolment raises without its reason (rebase 5)": (
        ADAPTER, ENROLL_RAISE, ENROLL_RAISE.replace('reason="claude-subscription-refused", ', ""),
        bounds("test_the_recorded_refusal_reaches_the_wire_whole_and_daemon_log_has_every_word")),
    "the template quotes any line, unchecked (rebase 6)": (
        REFUSALS, CHECK, '    if name == "claude_refusal" and type(value) is str:\n        return True\n' + CHECK,
        bounds("test_the_wire_reads_one_of_the_template_s_forms")),
    "a bystander line is quoted before the stamped frame (rebase 7)": (
        ADAPTER, FIRST, '    if in_corpus is not None:\n        return quote(corpus, in_corpus, "output")\n' + FIRST,
        pr("test_unrelated_matching_text_is_never_quoted_as_the_refusal")),
}


def pytest(tests, basetemp: Path | None) -> subprocess.CompletedProcess:
    extra = [f"--basetemp={basetemp}"] if basetemp is not None else []
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *extra, *tests],
                          capture_output=True, text=True)


def first_failure(run: subprocess.CompletedProcess) -> str:
    found = next((line[7:] for line in run.stdout.splitlines() if line.startswith("FAILED ")), "")
    return found.split(" - ")[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", help="comma-separated 1-based mutation numbers")
    parser.add_argument("--basetemp-root", type=Path, help="a new --basetemp for each pytest run under DIR")
    parser.add_argument("--list", action="store_true", help="print the mutations, numbered, and exit")
    args = parser.parse_args(argv)
    numbered = list(enumerate(MUTATIONS.items(), 1))
    if args.list:
        for number, (name, _) in numbered:
            print(f"{number:2}  {name}")
        return 0
    if args.only:
        chosen = {int(part) for part in args.only.split(",") if part.strip()}
        numbered = [(number, item) for number, item in numbered if number in chosen]
    files = sorted({path for _, (_, (path, *_)) in numbered}, key=str)
    if subprocess.run(["git", "diff", "--quiet", "--", *map(str, files)]).returncode:
        print(f"{', '.join(map(str, files))}: uncommitted changes; commit them first", file=sys.stderr)
        return 2
    if args.basetemp_root is not None:
        args.basetemp_root.mkdir(parents=True, exist_ok=True)
    pristine = {path: path.read_text(encoding="utf-8") for path in files}
    survived, runs = [], 0

    def basetemp() -> Path | None:
        nonlocal runs
        runs += 1
        return args.basetemp_root / f"run-{runs}" if args.basetemp_root else None

    try:
        for number, (name, (path, rule, replacement, killer)) in numbered:
            original = pristine[path]
            if original.count(rule) != 1:
                print(f"NO LONGER APPLIES  {number:2}  {name}", flush=True)
                survived.append(name)
                continue
            path.write_text(original.replace(rule, replacement), encoding="utf-8")
            try:
                run = pytest((killer,), basetemp())
                if not run.returncode:          # its own killer missed it: try everything
                    run = pytest(TESTS, basetemp())
            finally:
                path.write_text(original, encoding="utf-8")
            failed = first_failure(run)
            if run.returncode not in (0, 1) or (run.returncode and not failed):
                # pytest's 2 to 5 (interrupted, internal error, usage, nothing collected) kill nothing.
                tail = " | ".join(run.stdout.strip().splitlines()[-3:])
                print(f"ERROR     {number:2}  {name}  (pytest exit {run.returncode}: {tail})", flush=True)
                survived.append(name)
                continue
            verdict = "killed  " if run.returncode else "SURVIVED"
            print(f"{verdict}  {number:2}  {name}" + (f"  ({failed})" if failed else ""), flush=True)
            if not run.returncode:
                survived.append(name)
    finally:
        for path, text in pristine.items():
            path.write_text(text, encoding="utf-8")
    print(f"{len(numbered) - len(survived)} of {len(numbered)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
