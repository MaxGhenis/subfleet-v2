#!/usr/bin/env python3
"""Count, over review r5's grammar, the texts whose reminders' removal shows a secret (#233 review r5, P2-1).

Each text is `tests.unit.test_daemon_log_first_pass.grammar_text(random.Random(seed))`:
keys, separators, every quote kind, escapes, flags, private-key and data-URI pieces,
margins, reminders and orphans, with reminders between a separator and a quote. For each,
the text the tail would cut (`daemon_log._reminders_resolved`) is computed three ways:

- `round 6`: reminders taken out whatever the first pass over what is left replaces, as
  round 6 took them out, checking only the margin-free reading's pairs (the mutation
  tool's `unconditional-take-out`);
- `round 7`: this tree's, which takes them out only where that pass replaces exactly what
  the original's does outside them, and its margin-free reading the same;
- `c2e080e44`: this tree's with #233's merged comparison of the margin-free readings, exact,
  so a margin file ending in a reminder falls back for its trailing line break (review r6 of
  #233, P3-A; the mutation tool's `asymmetric-margin-free-comparison`), the fallback count
  before the follow-up.

With `--margin-texts N`, N more texts are drawn after the grammar's: margin files that end
in a reminder (`tests.unit.test_daemon_log_trailing_reminders.margin_reminder_text`, the
shape review r6 noted no test drew), each counted the same way, its tails at `--bound`
(that file's property runs them after 600 admission lines at the real bound). For those it
also says why each refused text keeps the marker (that file's `refusal_reasons`), how many
data URI refusals review r6's direction alone (the mutation tool's
`trailing-breaks-stripped-alone`) would have leaked, and whether any text `c2e080e44`
resolves is refused here or resolves to another text.

A text leaks when the whole-text scrub of what would be cut (its block, lines kept) shows
a secret the whole-text scrub of the text hides. A text falls back when the resolution is
None (round 5's marker rule). The tail itself is also taken, cut to `--bound` at each of
`--counts`, and checked the same way. Run it under the suite lock:

    .venv/bin/python tools/reminder_join_census.py --texts 60000 --budget-s 1000
    .venv/bin/python tools/reminder_join_census.py --texts 20000 --margin-texts 20000
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from subfleet import daemon_log  # noqa: E402
from tests.unit.test_daemon_log_first_pass import BETWEEN, grammar_text  # noqa: E402
from tests.unit.test_daemon_log_trailing_reminders import margin_reminder_text, refusal_reasons  # noqa: E402

import daemon_log_cut_mutations as mutations  # noqa: E402

SECRET = re.compile(r"SECRET7[a-z0-9]*")


def shown(text: str) -> set[str]:
    return set(SECRET.findall(daemon_log.scrubbed_block(text, group=daemon_log._GROUP_MARK in text, keep_lines=True)))


def resolved_round6(text: str):
    patch = pytest.MonkeyPatch()
    try:
        mutations._unconditional_take_out(patch)
        return daemon_log._reminders_resolved(text)
    finally:
        patch.undo()


def resolved_c2e080e44(text: str):
    patch = pytest.MonkeyPatch()
    try:
        mutations._asymmetric_margin_free_comparison(patch)
        return daemon_log._reminders_resolved(text)
    finally:
        patch.undo()


def resolved_stripped_alone(text: str):
    patch = pytest.MonkeyPatch()
    try:
        mutations._trailing_breaks_stripped_alone(patch)
        return daemon_log._reminders_resolved(text)
    finally:
        patch.undo()


#: The resolutions counted for each text, the control first.
RESOLVERS = (("r6", resolved_round6), ("c2e080e44", resolved_c2e080e44), ("r7", lambda text: daemon_log._reminders_resolved(text)))


def corpora(texts: int, budget_s: float | None = None) -> int:
    """The same counts over round 6's corpora (`test_daemon_log_reminders.CORPORA`, each
    text's own bound, and the edge generator), whose tags sit on their own lines or between
    pieces: how often a realistic tagged log keeps only the marker now."""
    from tests.unit import test_daemon_log_reminders as reminders
    sources = {name: build for name, build in reminders.CORPORA.items()}
    sources["edge generator"] = lambda rng: reminders.edge_text(rng)[:2]
    started, leaks = time.monotonic(), 0
    for name, build in sorted(sources.items()):
        rng = random.Random(6000)
        counted = {"texts": 0, "tagged and cut": 0, "r6 falls back": 0, "c2e080e44 falls back": 0, "r7 falls back": 0,
                   "r6 leaks": 0, "c2e080e44 leaks": 0, "r7 leaks": 0}
        for _ in range(texts):
            if budget_s is not None and time.monotonic() - started > budget_s:
                break
            text, bound = build(rng)
            counted["texts"] += 1
            saved = daemon_log.RECORD_SCRUB_MAX
            daemon_log.RECORD_SCRUB_MAX = bound
            try:
                if not daemon_log.reminder_tags(text) or len(text) <= daemon_log._tail_limit(text, 1):
                    continue
            finally:
                daemon_log.RECORD_SCRUB_MAX = saved
            counted["tagged and cut"] += 1
            whole = shown(text)
            for key, resolver in RESOLVERS:
                resolved = resolver(text)
                if resolved is None:
                    counted[f"{key} falls back"] += 1
                elif shown(resolved) - whole:
                    counted[f"{key} leaks"] += 1
        leaks += counted["r7 leaks"]
        print(f"- {name}: " + ", ".join(f"{key} {value:,}" for key, value in counted.items()), flush=True)
    return int(bool(leaks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--texts", type=int, default=60_000)
    parser.add_argument("--seed", type=int, default=0, help="the first seed")
    parser.add_argument("--bound", type=int, default=120, help="RECORD_SCRUB_MAX for the tail check")
    parser.add_argument("--counts", default="1,8,40")
    parser.add_argument("--budget-s", type=float, default=None, help="stop drawing after this many seconds")
    parser.add_argument("--corpora", type=int, default=0, help="texts of each of round 6's corpora, instead")
    parser.add_argument("--margin-texts", type=int, default=0,
                        help="margin files ending in a reminder to draw after the grammar's texts")
    args = parser.parse_args(argv)
    if args.corpora:
        return corpora(args.corpora, args.budget_s)
    counts = [int(part) for part in args.counts.split(",")]
    started = time.monotonic()
    failed = False
    for label, draw, bound, texts in (
            ("review r5's grammar", grammar_text, args.bound, args.texts),
            ("margin files ending in a reminder", margin_reminder_text, args.bound, args.margin_texts)):
        if texts:
            failed |= census(label, draw, args.seed, texts, bound, counts, started, args.budget_s,
                             explain=draw is margin_reminder_text)
    return int(failed)


def census(label: str, draw, first: int, texts: int, bound: int, counts: list[int], started: float,
           budget_s: float | None, explain: bool = False) -> bool:
    """One corpus's counts, printed; whether this tree leaked on it. With `explain`, each
    refusal's reason and the comparison with `c2e080e44` too."""
    began = time.monotonic()
    totals = {"texts": 0, "tagged": 0, "between": 0}
    for name, _ in RESOLVERS:
        totals[f"{name} leaks"] = totals[f"{name} falls back"] = 0
    totals.update({"r7 tail leaks": 0, "tails cut": 0, "tails": 0})
    examples: dict[str, str] = {}
    for seed in range(first, first + texts):
        if budget_s is not None and time.monotonic() - started > budget_s:
            break
        text = draw(random.Random(seed))
        totals["texts"] += 1
        if not daemon_log.reminder_tags(text):
            continue
        totals["tagged"] += 1
        totals["between"] += bool(BETWEEN.search(text))
        whole = shown(text)
        results = {}
        for name, resolver in RESOLVERS:
            resolved = results[name] = resolver(text)
            if resolved is None:
                totals[f"{name} falls back"] += 1
            elif shown(resolved) - whole:
                totals[f"{name} leaks"] += 1
                examples.setdefault(name, repr(text))
        if explain:
            explained(text, whole, results, totals)
        saved = daemon_log.RECORD_SCRUB_MAX
        daemon_log.RECORD_SCRUB_MAX = bound
        try:
            for count in counts:
                totals["tails"] += 1
                if len(text) > daemon_log._tail_limit(text, count):
                    totals["tails cut"] += 1
                if set(SECRET.findall("\n".join(daemon_log.scrubbed_tail(text, count)))) - whole:
                    totals["r7 tail leaks"] += 1
                    examples.setdefault("r7 tail", repr((text, count)))
        finally:
            daemon_log.RECORD_SCRUB_MAX = saved
    print(f"## {label}: seeds {first} to {first + totals['texts'] - 1}, bound {bound} for the tails, "
          f"counts {counts}, {time.monotonic() - began:.0f} s")
    for key, value in totals.items():
        print(f"- {key}: {value:,}")
    for name, example in examples.items():
        print(f"- first {name} leak: {example[:600]}")
    return bool(totals["r7 leaks"] or totals["r7 tail leaks"])


def explained(text: str, whole: set[str], results: dict, totals: dict) -> None:
    """Why this tree refuses `text`, and how its resolution compares with `c2e080e44`'s."""
    before, after = results["c2e080e44"], results["r7"]
    reasons = refusal_reasons(text)
    assert (after is None) == bool(reasons), text
    for reason in sorted(reasons):
        totals[f"refused: {reason}"] = totals.get(f"refused: {reason}", 0) + 1
    if "data-uri" in reasons:
        alone = resolved_stripped_alone(text)
        if alone is not None and shown(alone) - whole:
            totals["refused: data-uri, which review r6's direction alone leaks"] = \
                totals.get("refused: data-uri, which review r6's direction alone leaks", 0) + 1
    for key, counted in (("refused at c2e080e44, resolved here", before is None and after is not None),
                         ("resolved at c2e080e44, refused here", before is not None and after is None),
                         ("resolved both ways, to different texts", None not in (before, after) and before != after)):
        totals[key] = totals.get(key, 0) + counted


if __name__ == "__main__":
    sys.exit(main())
