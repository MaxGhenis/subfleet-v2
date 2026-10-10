"""Mutation check for the quoted subscription refusal (C-9.3, C-10.2).

Each mutation removes one rule of the refusal handling in `subfleet/adapters/claude.py`
(quote Claude Code's own refusal verbatim, never a bystander line; carry the error
kinds; name both causes without choosing or promising; refuse at enrolment with or
without `system/init`; keep the match inside an excerpt), and
`tests/unit/test_claude_subscription_refusal.py` must fail under it. Those marked
"review r1" are the first lane review's findings and its two surviving mutations.
Run from the repository root, with the adapter clean:

    uv run python tools/refusal_mutations.py

It rewrites the adapter in place, one mutation at a time, and restores it whatever
happens. It exits 1 if a mutation survives or no longer applies. It is a development
check, not part of the suite: it runs the tests once per mutation.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ADAPTER = Path("subfleet/adapters/claude.py")
TESTS = ("tests/unit/test_claude_subscription_refusal.py",)

ENROLL = ('        refusal = _subscription_refusal(corpus, summary)\n'
          '        if refusal is not None:\n'
          '            raise AdapterError(f"claude: {refusal.statement()}", code=5,\n'
          '                               fix=ORG_BLOCK_CAUSES)\n')
WINDOW = '    high = max(low + LINE_EXCERPT_MAX, found_end)\n'
MARKED = '    return ("…" if low else "") + line[low:high] + ("…" if high < len(line) else "")\n'
STAMPED = "        if message.error == ORG_BLOCK_ERROR_KIND:\n"
PROVIDER_FIRST = "    for text in errors:\n        found = ORG_BLOCK_RE.search(text)\n"
PROVIDER_LAST = '    for text in errors:\n        found = re.search(r"\\S", text)\n'
CAUSES_END = '    "run `subfleet lanes enroll` again."\n'

#: name: (the rule's code, what replaces it)
MUTATIONS = {
    "enrolment says the organisation disabled access": (
        ENROLL, ENROLL.replace('f"claude: {refusal.statement()}"',
                               '"claude: the organisation has disabled Claude Code subscription '
                               'access for this account"')
                      .replace("fix=ORG_BLOCK_CAUSES", "fix=\"ask the account's admin to enable "
                               "Claude Code access\"")),
    "enrolment refuses only after system/init": (
        ENROLL, ENROLL.replace("_subscription_refusal(corpus, summary)",
                               "_subscription_refusal(corpus, summary) if summary.has_init else None")),
    "the auth-dead detail drops the causes and the kinds": (
        'f"auth-dead: {refusal.statement()}. {ORG_BLOCK_CAUSES}"', 'f"auth-dead: {refusal.line}"'),
    "the cause line promises that enrolment succeeds (review r1, 3)": (
        CAUSES_END, CAUSES_END.replace("run `subfleet lanes enroll` again.",
                                       "`subfleet lanes enroll` succeeds.")),
    "the cause line adds an admin-only direction (review r1, mutation C)": (
        CAUSES_END, CAUSES_END.replace("again.", "again. Ask the org admin to turn Claude Code back on.")),
    "error kinds are not carried": (
        "        if self.error_kinds:\n            evidence.append",
        "        if False:\n            evidence.append"),
    "error kinds repeat": (
        "    kinds = tuple(dict.fromkeys(summary.error_kinds))", "    kinds = tuple(summary.error_kinds)"),
    "the error kind alone is not a refusal": (
        "    if not stamped and in_corpus is None:\n        return None",
        "    if in_corpus is None:\n        return None"),
    "the stamped frame is not looked at": (STAMPED, "        if False:\n"),
    "any assistant frame is taken for the refusal (review r1, mutation A)": (STAMPED, "        if True:\n"),
    "ordinary text outranks the provider's error text (review r1, 1)": (
        PROVIDER_FIRST, PROVIDER_FIRST.replace("in errors", "in ()")),
    "a wordless stamped frame ignores the provider's error text (review r1, 2)": (
        PROVIDER_LAST, PROVIDER_LAST.replace("in errors", "in ()")),
    "the evidence drops where the words came from": (
        '                         "quoted_from": refusal.quoted_from},', '                         },'),
    "a long line is cut at 300 whatever it held": (
        "    if len(line) <= LINE_EXCERPT_MAX:\n        return line\n", "    return line[:300]\n"),
    "the window can end inside a long match": (WINDOW, WINDOW.replace("found_end)", "found_end - 1)")),
    "trimmed indentation shifts the window": (
        "    offset = start + len(raw) - len(raw.lstrip())", "    offset = start"),
    "a cut is not marked": (MARKED, "    return line[low:high]\n"),
}


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "--", str(ADAPTER)]).returncode:
        print(f"{ADAPTER} has uncommitted changes; commit or stash them first", file=sys.stderr)
        return 2
    original, survived = ADAPTER.read_text(), []
    try:
        for name, (rule, replacement) in MUTATIONS.items():
            if original.count(rule) != 1:
                print(f"NO LONGER APPLIES  {name}")
                survived.append(name)
                continue
            ADAPTER.write_text(original.replace(rule, replacement))
            try:
                run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                                     capture_output=True, text=True)
            finally:
                ADAPTER.write_text(original)
            failed = next((line[7:] for line in run.stdout.splitlines() if line.startswith("FAILED ")), "")
            print(f"{'killed  ' if run.returncode else 'SURVIVED'}  {name}" + (f"  ({failed.split(' - ')[0]})" if failed else ""))
            if not run.returncode:
                survived.append(name)
    finally:
        ADAPTER.write_text(original)
    print(f"{len(MUTATIONS) - len(survived)} of {len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
