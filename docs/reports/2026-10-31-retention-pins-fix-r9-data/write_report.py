"""Render the current round report from its receipts and tables."""
import json
from pathlib import Path

D = Path(__file__).resolve().parent
R = D.parents[2]
summary = json.loads((D / "summary.json").read_text())
classes = (D / "class-table.md").read_text()
reversions = (D / "reversion-table.md").read_text()
mutants = (D / "mutant-table.md").read_text()
files = (D / "file-table.md").read_text()
lead = """**Fix round r9 repairs R9-F1: every returned discovery place now has one spelling.** Successful physical pointer files and resolved scan targets take `folders.canonical`; physical symlink entries retain canonical parent plus entry name. Spelling the supplied start before routine-location comparisons keeps routine records unchanged through accepted aliases. Discovery stays outside transactions. Retention remains dormant pending approving review r10 and completed CI."""
evidence = f"""- **Fix r9 evidence:** review r9 is red 4/4 at `236c80883` and green 4/4 after repair. The new real-Git property has 55 complete owner-dependent inputs covering all 33 position/spelling classes, with zero spelling skips. Five routine records equal `236c80883` exactly. All four new mutants and all nine r8 mutants die; the nine r8 assertions are unchanged. Every single-site generated-only reversion is detected. {summary['ordinary_passing_nodes']} distinct ordinary nodes pass across {summary['completed_runs']} completed locked pytest holds; per-file counts, UTC starts and loads are retained. The six documented archive path controls pass with worktree TMPDIR.
- **Reader boundaries:** D-R6-1 through D-R6-4, D-R7-1, D-R8-1 and D-R8-2 remain listed differences. No reader is widened; C-8.3/C-8.4 are unchanged.
- **Next:** approving review r10, then the hub's all-pass CI and mergeability checks before activation or merge."""
(D / "pr-body-lines.md").write_text(lead + "\n\n" + evidence + "\n")
text = f"""# Retention dependency pins: fix round r9

{lead}

Started **2026-10-31 09:56:57 UTC**, input head `236c80883ce3a6432e1621fff5a08e73dbc32bf3`. Holds began at 09:58:02 UTC, inside the first hour. The four-hour hold-start cutoff was 13:56:57 UTC. The requested macOS scratch folder was denied; `/private/tmp/sf-fix272r9` was used. Shared Git metadata rejected `index.lock`, so the assigned branch is committed in worktree-local metadata and delivered in `fix272r9.bundle`, requiring exactly `236c80883`. No shared branch or history was rewritten.

## Decisions as built

1. `_Aliases.text`, `_alternates`, and `_Aliases.scan` canonicalize their successful physical file or resolved target exactly as the three-site trial specifies. Link-entry recording is unchanged.
2. `_Aliases.locations` canonicalizes start before its ancestor comparison. The executed linked-checkout counterexample requires the one-line change; plain controls are unchanged.
3. `assert_location_spelling` captures the actual discovery `_Aliases.links` set. Every returned place equals canonical parent plus basename; every non-link place also equals `folders.canonical(place)`. It runs in all four r9 sibling controls, the new property and all five existing routine controls.
4. Only r9's strict xfail decorator was removed. Its remaining bytes and the entire prior location-input module are unchanged.
5. The new `tests/unit/test_retention_location_spelling.py` uses real Git, three trees and an outside checkout; all eleven positions; accepted Data-volume, other-case and Unicode-normalization spellings; 55 generated inputs, `derandomize=True`, `database=None`, no explicit examples. The owner has a non-ASCII name. The checkout also has one so the supplied-folder spelling exercises decision 2. The oracle reads first, requires complete evidence, excludes unrelated, checks the spelling invariant and exact routine `.git` omission, checks owner by `folders.within(place, canonical_owner)` without respelling a recorded place, removes owner and requires the identical read to fail.
6. Four new named mutants die. All nine r8 mutants die at the same assertions as r8. The older twenty were deliberately not rerun.
7. In isolated scratch source copies, each of the four sites is reverted alone. Every new property copy is byte-identical, has no explicit examples, and detects its reversion. First failures before shrinking appear below.
8. The discovery docstring, desktop discovery description and D-R8-2 line, main report, this report and PR-body replacement lines are updated. The acceptance contract is unchanged.

No decision changed the reader's reach. No new product gap was found. The initial unconstrained 55-input strategy left five cells empty (28/33): that was a strategy finding, not complete coverage. The final finite cross product generates all 55 distinct position/variant inputs and all 33 nonempty cells. Named-admin variants add a second Data-volume and case input per position; normalization uses the unnamed admin. The earlier receipts are retained as superseded and excluded from final accounting.

## Decision 2: execution before editing

Both a plain repository and `git worktree add` checkout were passed directly through the Data-volume spelling to `git_storage` and `record`. Neither test caller canonicalizes the supplied folder first. `record` forwards workdir unchanged. Daemon submission uses `Path.resolve`, which preserves this firmlink spelling; its separate canonical `folder` does not replace the workdir passed to `record` (daemon.py around 2989/3106). Legacy discovery likewise calls `record(row["workdir"])` before separately spelling that row's workdir.

| Input | At 236c80883 | Three sites alone | With start canonicalized |
|---|---|---|---|
| Plain | `.git`, `.git/objects` | Same | Same |
| Linked checkout | admin, common, common/objects | Same plus checkout/.git | Exact base list |

`git_storage` returns those lists; `record` wraps each as `{{"git": list}}`. All six observations report no incomplete evidence. The `.git` file added by the three-site trial is canonical, but start is still Data-volume spelled, so its parent no longer matches the ancestor set. The one-line start fix restores that comparison. Full paths and both APIs are in `start-before.json`.

## Five routine controls against 236c80883

`routine-comparison.json` executes the base code from `git show 236c80883:subfleet/dependencies.py` beside the repair on each identical real-Git fixture. Plain, worktree-add, shared clone, nested git-new-workdir and no-Git all satisfy `base == repaired == expected`. Their existing exact assertions remain, followed by the new spelling helper. The untouched dependency-unit file passes all 19 cases under linked TMPDIR.

## Generated class table

All 55 inputs have successful initial reads, complete evidence, unrelated omitted, canonical owner matched by consumers' string comparison, and failing identical reads after owner removal. Independent inputs: 0. Skips: 0; this volume accepts every requested form. Linked TMPDIR reproduces the same table.

{classes}

## Generated-only single-site reversions

Each copy preserves the property bytes and deterministic settings. Recorded inputs below are the first generated failures before shrinking, not inserted examples. The source manifest and attempt receipts retain every trial.

{reversions}

## Red, green and per-file verification

The red hold exposes four failures at owner retention with passing setup/teardown. Green: r9 file **4**, new tests **11**, location tests **22**, unchanged location property **1**. Each required linked-TMPDIR file passes in its own hold: r9 **4**, new tests **11**, locations **22**, location property **1**, pointer inputs **2**, r8 **2**, dependency units **19**, fake dependencies **30**. The R-files, dependency set, all turn-retention files, both admission lease-oracle tests, archive, nested leases and three guards also run. Of the 714 distinct ordinary nodes, 707 are repository tests and seven are the requested pre-repair/routine execution probes. Exact final per-file counts follow; the hold table records every repeated invocation.

{files}

All {summary['completed_runs']} completed holds used the shared lock, peak pytest concurrency {summary['peak_pytest_concurrency']}; longest {summary['longest_seconds']:.2f} seconds, peak pytest RSS {summary['peak_rss_mib']:.2f} MiB. No 30-minute direct fallback was used. The wrapper's `sysctl` was sandbox-denied; receipts use actual `os.getloadavg()` at admission. Unresolved ordinary failures: {len(summary['unresolved'])}.

Archive was split by test function into seven holds so no pytest approached ten minutes. Under `/private/tmp`, these six controls hit only the documented scratch-path classification trap: `test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`, `test_blob_only_in_a_local_branch_is_archived_not_omitted`, `test_local_path_remote_is_not_a_remote`, `test_borrowed_or_shallow_object_store_is_scratch[alternates]`, `test_borrowed_or_shallow_object_store_is_scratch[shallow]`, `test_survey_is_read_only_and_predicts_the_pass`. All six pass unchanged with TMPDIR in the new worktree folder `sf-fix272r9-fixtures`, removed at cleanup. Final archive union: 108 passing nodes.

## Named mutants

Every kill is a test-call assertion failure with passing setup/teardown. All nine r8 assertion expressions equal their recorded r8 expressions, as checked in `mutant-results.json`.

{mutants}

## PR-body replacement lines

{lead}

{evidence}

## Not run and cleanup

Not run: `tests/unit/test_retention_archive.py::test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` (mandated live-case deselection); the older twenty mutants (review r9 settled them); full suite; Python 3.12; person-only `tests/frontend/test_core_live.py`; remote CI. Review r9's additional notes (38), archive properties (2), salvage properties (2), and sweep examples/property (13) are outside this brief's requested Run block and were not repeated. Those 55 older nodes explain the difference from r9's 745-node set; this round adds 11 spelling tests, four r9 regressions and two admission oracle nodes, plus seven execution probes. The hub owns CI and publication. No real provider, live daemon/store, real Subfleet CLI, remote fetch, merge, push or GitHub call was used. No process was detached.

Cleanup measurements and delivery details are in `delivery.json`; final bundle verification is retained beside the bundle in `fix272r9-bundle-verify.txt`. The locked environment uses Python 3.14.7, Git 2.55.0, pytest 9.1.1 and Hypothesis 6.168.1 (`environment.json`). The largest retained scratch measurement is 423 MiB; worktree fixtures measured 3.70 MiB. The first guarded cleanup left 80 KiB in one read-only archive-fixture directory; restoring owner permissions there and repeating the same guarded cleanup removed it. Scratch and worktree fixtures are absent; all owned sessions ended. Durable report, data, tests and a verified bundle remain in this worktree.

DONE
"""
(R / "docs/reports/2026-10-31-retention-pins-fix-r9.md").write_text(text)
# Preserve older rounds as dated history, replacing only the current lead.
p = R / "docs/reports/2026-10-27-retention-dependency-pins.md"
s = p.read_text()
index = s.index("## Review r8 and fix round r8")
section = f"""## Review r9 and fix round r9

Review r9 at `a3b9cc246` found **R9-F1 (P2)**: successful physical pointer files and scanned targets reached through a Data-volume alias retained the walk's spelling, missing a canonical owner tree. Four healthy Git fixtures recorded complete evidence, retired the required owner and then read 128. The hub committed review evidence unchanged at `236c80883`.

Fix r9 canonicalizes the three auxiliary sites and start before routine-location comparisons, preserving physical link-entry names and discovery outside transactions. All four red cases pass; five routine records equal `236c80883`; all 55 generated spelling inputs cover all 33 classes without skips. The four site mutants and generated-only reversions are detected; all nine r8 mutants die at the same assertions. The requested holds pass, with the six documented archive controls rerun using worktree TMPDIR. D-R8-2 records unused target/input-link extras without broad descendant pins or complete terminal Git pins. No reader was widened; C-8.3/C-8.4 are unchanged.

[Fix round r9](2026-10-31-retention-pins-fix-r9.md) contains the current tables, UTC starts/loads, PR-body replacements and delivery details. Retention remains dormant pending approving review r10 and completed CI. Earlier rounds below are dated history.

"""
p.write_text(s.split("\n", 1)[0] + "\n\n" + lead + "\n\n" + section + s[index:])
