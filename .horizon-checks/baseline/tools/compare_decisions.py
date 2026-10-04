#!/usr/bin/env python3
"""Diff v1's routing decisions against v2's, nightly, for the shadow week.

`docs/migration.md` shadow-week step 4: "Every v1 dispatch from one chosen
session is mirrored to `sf2 run --dry-run --json`; a `compare` script (never
shipped) diffs the two decisions nightly against `D/decisions.jsonl` and writes
`docs/shadow-diffs/<date>.md` with every difference explained."

This is that script. It is not part of the `subfleet` package and nothing in the
package imports it: it lives in `tools/` so it never ships (pyproject packages
`subfleet` only).

    tools/compare_decisions.py --since 2026-09-01
    SUBFLEET_HOME=~/.subfleet tools/compare_decisions.py --date 2026-09-06

The inputs are read from the record's `overrides` object, which is where v1 puts
what the *caller* asked for - `model` (the caller's `-m`), `home` and `lane` (the
caller's `-H`/`-a`), `task`, `tier` and `class` - plus the top-level `task` and
`tier` on records new enough to carry them. Nothing is read from the recorded
`cmd`: that argv is built *after* v1 has routed, so its `-m` and `-H` are v1's
answer. Feeding them back as the replay's input would ask v2 to confirm v1's
decision and it would agree by construction. `requested_model` is v1's own
pre-capacity choice, an output too, and is not used either.

A shape with a task and a tier and no pinned lane replays through
`subfleet why --task <task> --tier <tier> --json` (C-11.5: `why` prints the
decision record and never dispatches). Everything else - a caller's lane pin,
which `why` has no flag for, and a `-m` pin with no task, which the CLI refuses
without `--task` - replays through the routing engine directly against a
read-only store (C-3.4). v1 records the caller's exclusions (`-x`) nowhere, so a
decision that had them cannot be replayed faithfully; the report says so rather
than replaying a shape it knows is incomplete.

`decisions.jsonl` is 16,710 lines and 274 MB, one JSON object per line, most of
it the capacity snapshot v1 embedded in each record. The file is streamed, never
loaded, and identical input shapes are evaluated once: a shadow week has tens of
distinct shapes, not tens of thousands.

Every difference gets its own row in the report with an empty explanation slot,
because the point of the exercise is that a human writes the one line saying why
v1 and v2 disagreed - the script never guesses.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from subfleet.policy import DEFAULT_POLICY_PATH, load_policy, pick, policy_hash  # noqa: E402
from subfleet.store import Store  # noqa: E402

DECISIONS = Path("~/.local/state/delegate/decisions.jsonl").expanduser()
OUTPUT_DIR = REPOSITORY / "docs" / "shadow-diffs"

#: v1 routing classes that v2 spells the same way (`cli.LEGACY_TASK_CLASSES`).
#: v1's other classes (`judgment`, `mechanical`, `fable`) have no v2 task, and a
#: class carries no tier, so a class alone is only ever half a shape.
CLASS_TO_TASK = {"build": "build", "review": "review", "sweep": "sweep"}
TIERS = ("trivial", "easy", "standard", "hard")

#: Provider model ids that appear in v1's journal and that the v2 policy names
#: only by a short alias, so a replay compares like with like. Read off
#: `decisions.jsonl` on 2026-09-05; the policy's own `retired` map covers the
#: rest (`sol` -> `astra`, `claude-fable-5` -> `fable`).
V1_MODEL_SPELLINGS = {"gpt-5.6-sol": "sol"}


@dataclass(frozen=True)
class Shape:
    """The inputs of one v1 decision, as far as v1 recorded them."""

    task: str | None
    tier: str | None
    pinned_model: str | None
    pinned_lane: str | None
    exclusions: tuple[str, ...]
    allow_desktop: bool

    @property
    def replayable(self) -> bool:
        """Whether v1 recorded enough of the caller's request to route it again."""
        return bool(self.task and self.tier) or bool(self.pinned_model) or bool(self.pinned_lane)

    @property
    def expressible(self) -> bool:
        """Whether `subfleet why` has a flag for every input in this shape."""
        return bool(self.task and self.tier) and self.pinned_lane is None

    def label(self) -> str:
        parts = []
        if self.task:
            parts.append(f"--task {self.task}")
        if self.tier:
            parts.append(f"--tier {self.tier}")
        if self.pinned_model:
            parts.append(f"-m {self.pinned_model}")
        if self.pinned_lane:
            parts.append(f"-a/-H {self.pinned_lane}")
        for account in self.exclusions:
            parts.append(f"-x {account}")
        if self.allow_desktop:
            parts.append("--allow-desktop")
        return " ".join(parts) or "(no expressible input)"


@dataclass
class Observation:
    """One v1 decision and what v2 would have decided from the same inputs."""

    shape: Shape
    v1_model: str | None
    v1_lane: str | None
    v2_model: str | None
    v2_lane: str | None
    v2_reason: str
    engine: str
    count: int = 1
    examples: list[str] = field(default_factory=list)

    v1_lane_id: str | None = None       # v1's lane name resolved to a v2 lane id

    @property
    def differs(self) -> bool:
        """A difference is a different model, or a different lane where both named one.

        v1 names a lane by Codex home or Claude email and v2 by lane id (C-1.3),
        so the comparison is on the resolved id; a record where v1 logged no lane
        compares models only.
        """
        if self.v1_model != self.v2_model:
            return True
        if self.v1_lane is None or self.v2_lane is None:
            return False
        return self.v1_lane_id != self.v2_lane


# --- reading v1's journal -----------------------------------------------------

def records(path: Path, since: str | None, until: str | None,
            limit: int | None) -> Iterator[dict[str, Any]]:
    """Stream `decisions.jsonl`; it is 274 MB and never fits in memory at once."""
    read = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            stamp = str(record.get("ts") or "")
            if since and stamp < since:
                continue
            if until and stamp[:len(until)] > until:
                # A bare `--until 2026-09-06` means the whole of that day: every
                # timestamp on it sorts after the date, so compare on its prefix.
                continue
            yield record
            read += 1
            if limit is not None and read >= limit:
                return


def shape_of(record: dict[str, Any], model_names: dict[str, str]) -> Shape:
    """What the caller asked v1 for, from `overrides` and the record's own fields.

    Never from `cmd`: v1 writes that argv after routing, so its `-m` and `-H` are
    v1's answer, not the question.
    """
    overrides = record.get("overrides") if isinstance(record.get("overrides"), dict) else {}
    task = record.get("task") or overrides.get("task") or CLASS_TO_TASK.get(record.get("class"))
    tier = record.get("tier") or overrides.get("tier")
    pinned_model = overrides.get("model")
    if isinstance(pinned_model, str) and pinned_model:
        pinned_model = model_names.get(pinned_model.lower(), pinned_model)
    else:
        pinned_model = None
    pinned_lane = overrides.get("home") or overrides.get("lane")
    return Shape(task if isinstance(task, str) else None,
                 tier if tier in TIERS else None, pinned_model,
                 pinned_lane if isinstance(pinned_lane, str) and pinned_lane else None,
                 (), bool(overrides.get("allow_desktop")))


def v1_choice(record: dict[str, Any], names: dict[str, str]) -> tuple[str | None, str | None]:
    """v1's own answer, in v2's vocabulary: a short model name and a lane name."""
    model = record.get("model")
    if isinstance(model, str):
        model = names.get(model.lower(), model)
    lane = record.get("lane/home")
    return model, (lane if isinstance(lane, str) and lane else None)


# --- replaying through v2 -----------------------------------------------------

class Engine:
    """`subfleet why` first, the routing engine directly when a flag is missing."""

    def __init__(self, state_root: Path, binary: str, offline: bool = False):
        self.state_root = state_root
        self.binary = binary
        self.offline = offline
        self._cache: dict[Shape, tuple[str | None, str | None, str, str]] = {}
        self.policy = self._load_policy()
        self.policy_digest = policy_hash(self._policy_path())

    def _policy_path(self) -> Path:
        candidate = self.state_root / "policy.json"
        return candidate if candidate.is_file() else DEFAULT_POLICY_PATH

    def _load_policy(self) -> dict[str, Any]:
        return load_policy(self._policy_path())

    def model_names(self) -> dict[str, str]:
        """Short names, provider ids, retired aliases and v1 spellings, to a short name."""
        index: dict[str, str] = {}
        for short, model in self.policy["models"].items():
            index[short.lower()] = short
            index[str(model["id"]).lower()] = short
        for alias, short in (self.policy.get("retired") or {}).items():
            index[alias.lower()] = short
        for spelling, alias in V1_MODEL_SPELLINGS.items():
            index[spelling] = index.get(alias, alias)
        return index

    def lane_id(self, name: str | None) -> str | None:
        """v1's lane name (a Codex home or a Claude email) as a v2 lane id."""
        if not name:
            return None
        database = self.state_root / "state.sqlite3"
        if not database.is_file():
            return None
        with Store(database, read_only=True) as store:
            return self._resolve_lane(store, name)

    def evaluate(self, shape: Shape) -> tuple[str | None, str | None, str, str]:
        if shape not in self._cache:
            self._cache[shape] = self._evaluate(shape)
        return self._cache[shape]

    def _evaluate(self, shape: Shape) -> tuple[str | None, str | None, str, str]:
        if shape.expressible and not self.offline:
            decision = self._why(shape)
            if decision is not None:
                return (decision.get("chosen_model"), decision.get("chosen_lane"),
                        decision.get("reason", ""), "why")
        return self._direct(shape)

    def _why(self, shape: Shape) -> dict[str, Any] | None:
        """C-11.5: `subfleet why --task T --tier X --json` prints the decision."""
        argv = [self.binary, "why", "--task", str(shape.task), "--tier", str(shape.tier), "--json"]
        if shape.pinned_model:
            argv += ["-m", shape.pinned_model]
        for account in shape.exclusions:
            argv += ["-x", account]
        if shape.allow_desktop:
            argv.append("--allow-desktop")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "SUBFLEET_HOME": str(self.state_root)})
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return (json.loads(result.stdout.splitlines()[-1]) or {}).get("decision")
        except ValueError:
            return None

    def _direct(self, shape: Shape) -> tuple[str | None, str | None, str, str]:
        """The routing engine on a read-only store, for inputs `why` cannot carry."""
        database = self.state_root / "state.sqlite3"
        if not database.is_file():
            return None, None, "no v2 store to evaluate against", "none"
        with Store(database, read_only=True) as store:
            lanes = store.list_lanes()
            readings = store.list_readings()
            closures = store.list_closures()
            in_flight = {row["lane_id"]: row["n"] for row in store.query(
                "SELECT lane_id, count(*) AS n FROM attempts WHERE state IN "
                "('reserved','starting','running','finalizing') GROUP BY lane_id")}
            lane_id = self._resolve_lane(store, shape.pinned_lane) if shape.pinned_lane else None
            if shape.pinned_lane and lane_id is None:
                return None, None, f"v1 pinned {shape.pinned_lane}, which is not a v2 lane", "direct"
        try:
            decision = pick(self.policy, lanes, pinned_model=shape.pinned_model,
                            pinned_lane=lane_id, task=shape.task, tier=shape.tier,
                            exclusions=shape.exclusions, allow_desktop=shape.allow_desktop,
                            closures=closures, readings=readings, in_flight=in_flight,
                            policy_digest=self.policy_digest)
        except ValueError as error:
            return None, None, str(error), "direct"
        return decision.chosen_model, decision.chosen_lane, decision.reason, "direct"

    @staticmethod
    def _resolve_lane(store: Store, name: str) -> str | None:
        for sql, value in (("SELECT lane_id FROM lanes WHERE lane_id=?", name),
                           ("SELECT lane_id FROM lanes WHERE home=?", name),
                           ("SELECT lane_id FROM lanes WHERE credential_ref=?", name),
                           ("SELECT lane_id FROM lanes WHERE account_key=?", f"claude:{name}"),
                           ("SELECT lane_id FROM lanes WHERE account_key=?", f"codex:{name}")):
            row = store.one(sql, (value,))
            if row:
                return row["lane_id"]
        return None


# --- the report ---------------------------------------------------------------

def render(observations: list[Observation], *, when: str, source: Path, state_root: Path,
           scanned: int, skipped: Counter) -> str:
    differing = [item for item in observations if item.differs]
    agreeing = [item for item in observations if not item.differs]
    replayed = sum(item.count for item in observations)
    lines = [
        f"# Shadow-week routing diff, {when}",
        "",
        f"- v1 journal: `{source}`",
        f"- v2 store: `{state_root}`",
        f"- records scanned: {scanned}; replayed: {replayed}; distinct input shapes: "
        f"{len(observations)}",
        f"- shapes where v2 chose differently: {len(differing)}; identical: {len(agreeing)}",
        f"- records the journal could not answer for: {sum(skipped.values())}",
        "",
        "Every differing shape gets one line of explanation, written by a human. A row",
        "with an empty explanation has not been reviewed yet.",
        "",
        "## Differences",
        "",
    ]
    if not replayed:
        lines.append("Nothing was replayed, so this file says nothing about agreement. "
                     "Check the window and the 'Records not replayed' section below.")
    elif not differing:
        lines.append("None. v2 chose what v1 chose for every shape replayed.")
    else:
        lines += ["| n | inputs | v1 model | v1 lane | v2 model | v2 lane | v2 reason | engine | explanation |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for item in sorted(differing, key=lambda row: -row.count):
            lines.append(
                f"| {item.count} | `{item.shape.label()}` | {item.v1_model or '-'} | "
                f"`{item.v1_lane or '-'}`{f' ({item.v1_lane_id})' if item.v1_lane_id else ''} | "
                f"{item.v2_model or '-'} | `{item.v2_lane or '-'}` | "
                f"{item.v2_reason or '-'} | {item.engine} |  |")
        lines += ["", "### Examples", ""]
        for item in sorted(differing, key=lambda row: -row.count):
            for example in item.examples[:3]:
                lines.append(f"- `{item.shape.label()}` first seen {example}")
    lines += ["", "## Agreement", ""]
    if agreeing:
        lines += ["| n | inputs | model | lane | engine |", "|---|---|---|---|---|"]
        for item in sorted(agreeing, key=lambda row: -row.count):
            lines.append(f"| {item.count} | `{item.shape.label()}` | {item.v1_model or '-'} | "
                         f"`{item.v1_lane or '-'}` | {item.engine} |")
    else:
        lines.append("None.")
    if skipped:
        lines += ["", "## Records not replayed", ""]
        for reason, count in skipped.most_common():
            lines.append(f"- {reason}: {count}")
    lines.append("")
    return "\n".join(lines)


def _resolve(engine: "Engine", cache: dict[str | None, str | None],
             name: str | None) -> str | None:
    if name not in cache:
        cache[name] = engine.lane_id(name)
    return cache[name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_decisions.py",
        description="diff v1's routing journal against v2's decisions (migration.md, shadow week)")
    parser.add_argument("--decisions", default=str(DECISIONS), help="v1's decisions.jsonl")
    parser.add_argument("--state-root", default=os.environ.get("SUBFLEET_HOME") or "~/.subfleet")
    parser.add_argument("--binary", default="subfleet", help="the v2 CLI to ask `why`")
    parser.add_argument("--since", help="ISO date or timestamp; records at or after it")
    parser.add_argument("--until", help="ISO date or timestamp; records at or before it")
    parser.add_argument("--limit", type=int, help="stop after this many records")
    parser.add_argument("--date", default=None, help="report file name (default: today, UTC)")
    parser.add_argument("--out-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--offline", action="store_true",
                        help="never shell out to `subfleet why`; use the routing engine")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = parser.parse_args(argv)

    source = Path(args.decisions).expanduser()
    if not source.is_file():
        print(f"compare_decisions: no journal at {source}", file=sys.stderr)
        return 1
    state_root = Path(args.state_root).expanduser()
    engine = Engine(state_root, args.binary, offline=args.offline)
    names = engine.model_names()

    observations: dict[tuple, Observation] = {}
    lanes: dict[str | None, str | None] = {None: None}
    skipped: Counter = Counter()
    scanned = 0
    for record in records(source, args.since, args.until, args.limit):
        scanned += 1
        shape = shape_of(record, names)
        if not shape.replayable:
            skipped["the caller's request is not in the record: no task and tier, "
                    "no -m, no -a/-H"] += 1
            continue
        model, lane, reason, used = engine.evaluate(shape)
        v1_model, v1_lane = v1_choice(record, names)
        key = (shape, v1_model, v1_lane)
        if key in observations:
            observations[key].count += 1
        else:
            observations[key] = Observation(shape, v1_model, v1_lane, model, lane, reason, used,
                                            v1_lane_id=lanes.get(v1_lane, _resolve(engine, lanes, v1_lane)))
        observations[key].examples.append(str(record.get("ts") or "?"))

    when = args.date or datetime.now(timezone.utc).date().isoformat()
    report = render(list(observations.values()), when=when, source=source,
                    state_root=state_root, scanned=scanned, skipped=skipped)
    if args.stdout:
        print(report)
        return 0
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{when}.md"
    path.write_text(report, encoding="utf-8")
    print(f"compare_decisions: {scanned} records, {len(observations)} shapes -> {path}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
