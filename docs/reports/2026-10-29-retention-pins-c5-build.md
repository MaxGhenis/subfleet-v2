# Retention dependency pins: continuation 5 build, 2026-10-29

Base: `ce9e154ebf7e7e4ceab59a89e86885e5d3035b60`. Assigned branch: `fix/retention-dependency-pins-217`. The resumed checkpoint retained the red/green records, locked Python 3.14.7 dev environment and two forward commits in private scratch Git metadata. Their source changes were already present in the assigned workspace and were preserved. Shared metadata is outside the writable workspace; delivery uses a verified bundle based on `ce9e154eb`, without changing the assigned `.git` file. No fetch, merge, push, live store, real provider or production CLI ran. No sub-agents were used.

Checkpoint evidence begins before 02:00 UTC; this resumed turn started 02:17:40 UTC. Conservatively using 01:57 UTC as the job start gives a 05:57 UTC cutoff for new holds and a 07:57 UTC wall. The first red hold started 01:59:54 UTC. Each pytest has its own supplied suite hold, no xdist and no more than two admitted lane pytest processes at once. The resumed driver raises KeyboardInterrupt after 570 seconds; no completed run exceeded ten minutes. All starts and 1/5/15-minute loads below come from inside the admitted Python process, using UTC and `os.getloadavg()`. The wrapper's denied `sysctl` output is not a load measurement.

## Decisions as built

1. **One dependency comparison key.** In `42bdf10798ba43551adb98fdf9cf81a1ed0eed98`, reservation's dependency attribution uses `dependencies.key` for both the checked path and the retention-fence path, just as `dependencies.fences` does. That helper normalizes the path and applies NFD(casefold(NFD(path))). Only dependency attribution changed; `subfleet/folders.py` is byte-for-byte identical to the base. The preceding source-folder attribution and legacy `folders.retiring_folded` / `folders.folded` (NFC plus casefold) remain intact.
2. **Red then green.** `c03ab23cf6a040ef8d365d882c9b4d9816db0c79` adds the audit's exact fake replay to `tests/fake/test_retention_dependencies.py`, using `ΐ` and `Ϊ́`. On the base, early admission names the dependency but the late transaction omits `folder` and `needs`. With the fix, both name the alias and `review-root`, keep the same fence, and remain waiting. The workspace stand-in also confirms preparation happens only in the late case.
3. **Generated aliases.** `test_dependency_hold_names_every_place_the_canonical_caseless_query_blocks` in `tests/unit/test_retention_folded_pin.py` uses an independent Unicode oracle, all three dependency roles, early/late fences, equal/nested/normalized tails, and components covering Greek dialytika/tonos, sharp S, fi ligature, Kelvin sign, combining marks and mixed case. Each case includes the Greek mismatch and an absent path, so kernel spelling cannot hide the error. Whenever the dependency query blocks, the actual fake hold must name that exact path and role and have the same key. Seed 217; 100 normal generated examples plus one explicit mixed example.
4. **Three targeted mutants.** Property and mutation evidence commit: `09eb8ab36`. The attribution mutant restores the audited `folders.fences(..., fold=True)` block, and the two prior mutants are replayed unchanged from continuation 4b. [Exact definitions](2026-10-27-retention-dependency-pins-data/c5-mutant-definitions.json) include their test selectors. An in-memory import loader requires exactly one source replacement, compiles it, and records that the target module actually loaded. Product files are never mutated on disk. No mutant sweep was run.
5. **Review text.** C-6.11 and C-8.4 name the shared dependency key; the legacy sentence is retained. One change-log line records D1. The audit marks D1 fixed with its commit while retaining its original red evidence. The main report's status and current open list are updated.

No hub decision needed a deviation. The first new-property attempt failed before reaching product assertions because importing helper modules inside `@given` triggered Hypothesis's nested-test health check. Imports now happen in the outer pytest function before the normal inner property starts. No health check was weakened and no product counterexample was found. That failed development hold is accounted for separately below.

## Verification

P/F/S/U counts require setup, call and teardown for passes; a failure requires completed teardown. An expected mutant failure is killing evidence. The failed development attempt is superseded by the completed property run, not counted as passing coverage. Machine-readable records are in [c5-holds.json](2026-10-27-retention-dependency-pins-data/c5-holds.json).

All starts are UTC on October 29. All holds were locked. **169 distinct requested nodes passed**, with no final failure, skip or unfinished node. The separate initial green regression repeats two of those nodes (171 passing head executions across successful holds). The base red, corrected property-harness attempt and three expected mutant failures are separate.

| Hold | Start UTC | Load 1 / 5 / 15 | P / F / S / U | Driver wall s |
|---|---|---|---|---:|
| `red` | 01:59:54.489 | 18.8 / 26.2 / 26.7 | 1 / 1 / 0 / 0 | 13.12 |
| `green` | 02:01:30.869 | 15.4 / 23.3 / 25.5 | 2 / 0 / 0 / 0 | 7.55 |
| `fake` | 02:19:51.889 | 39.6 / 36.8 / 32.9 | 30 / 0 / 0 / 0 | 161.51 |
| `units` | 02:23:44.914 | 76.2 / 55.1 / 41.3 | 19 / 0 / 0 / 0 | 121.35 |
| `d1-property` | 02:22:51.996 | 83.0 / 50.8 / 39.0 | 0 / 1 / 0 / 0 | 33.39 |
| `d1-property-green` | 02:25:43.175 | 51.4 / 53.5 / 42.5 | 1 / 0 / 0 / 0 | 170.78 |
| `folded` | 02:28:48.351 | 43.8 / 47.7 / 42.0 | 37 / 0 / 0 / 0 | 122.66 |
| `spelling` | 02:31:59.147 | 14.2 / 31.3 / 36.2 | 16 / 0 / 0 / 0 | 35.95 |
| `mutant-attribution` | 02:31:36.069 | 16.4 / 32.8 / 36.8 | 0 / 1 / 0 / 0 | 14.95 |
| `mutant-collate` | 02:31:09.786 | 20.1 / 34.9 / 37.6 | 0 / 1 / 0 / 0 | 13.51 |
| `mutant-record` | 02:33:33.011 | 34.9 / 33.9 / 36.7 | 0 / 1 / 0 / 0 | 4.69 |
| `property-reference` | 02:33:50.120 | 33.9 / 33.7 / 36.5 | 1 / 0 / 0 / 0 | 104.55 |
| `property-monotone` | 02:35:39.229 | 33.4 / 33.6 / 36.2 | 1 / 0 / 0 / 0 | 244.71 |
| `property-fences` | 02:37:10.251 | 27.0 / 31.2 / 34.9 | 1 / 0 / 0 / 0 | 287.90 |
| `guards` | 02:36:10.529 | 28.2 / 32.2 / 35.5 | 63 / 0 / 0 / 0 | 51.91 |

Per-file counts at the final source (each row references the admitted hold above):

| File | Hold(s) | Passed |
|---|---|---:|
| `tests/fake/test_retention_dependencies.py` | fake | 30 |
| `tests/unit/test_retention_dependency_units.py` | units | 19 |
| `tests/unit/test_retention_folded_pin.py` | d1-property-green, folded | 38 |
| `tests/fake/test_retention_dependency_spelling.py` | spelling | 16 |
| `tests/unit/test_retention_dependency_properties.py` | property-reference, property-monotone | 2 |
| `tests/unit/test_retention_shared_folders.py` | property-fences | 1 |
| `tests/fake/test_state_contract.py` | guards | 37 |
| `tests/unit/test_test_module_names.py` | guards | 1 |
| `tests/unit/test_invariants_index.py` | guards | 25 |

Normal property counts, observed without changing Hypothesis generation, phases, shrinking or maxima:

| Property / hold | Generated passing | Invalid / overrun | Explicit passing |
|---|---:|---:|---:|
| `d1-property-green` | 100 | 46 | 1 |
| `property-reference` | 100 | 0 | 0 |
| `property-monotone` | 100 | 4 | 0 |
| `property-fences` | 400 | 90 | 12 |

The folded-pin file also completed its existing 500-example string and 150-example real-volume properties (the terminal statistics and machine records retain their invalid counts). The two properties in the dependency-properties file and the fence differential property each ran exactly once at their normal settings. Their full names are the selected nodes in the machine record.

| Mutant | Killing test | Start UTC | Load 1 / 5 / 15 | Result |
|---|---|---|---|---|
| `attribution-back-on-folded` | `tests/fake/test_retention_dependencies.py::test_c6_11_unicode_dependency_hold_names_its_place[transaction]` | 02:31:36.069 | 16.4 / 32.8 / 36.8 | killed (0 P / 1 F / 0 U) |
| `collate-nocase-restored` | `tests/unit/test_retention_folded_pin.py::test_a_job_id_typed_with_letters_the_volume_folds_keeps_the_tree[recorded-submitted-all]` | 02:31:09.786 | 20.1 / 34.9 / 37.6 | killed (0 P / 1 F / 0 U) |
| `record-raw-spelling` | `tests/unit/test_retention_dependency_units.py::test_record_spells_each_place_once` | 02:33:33.011 | 34.9 / 33.9 / 36.7 | killed (0 P / 1 F / 0 U) |

The attribution killer failed specifically at the final `folder` / `needs` assertion after its blocking and workspace-count assertions passed. The ASCII SQL killer lost `worktree-in-use`; the raw-spelling killer returned `REVIEW` instead of the canonical `Review`. All three had completed teardown. Each corresponding killing test passed in the unmutated requested files.

The driver's early Hypothesis import produced two assertion-rewrite warnings in instrumented holds; these concern the observer's import order. No product warning or failed head assertion remained.

## Whole-PR description

Record canonical review-root, output and Git-storage dependencies at submission. Keep an allocated worktree while another live job or a pending export needs a place in it, and check retention fences before workspace preparation and again in reservation. Replace ASCII SQL pin matching with normalized canonical caseless keys; late dependency holds now identify the same `folder` and `needs` as the blocking query.

Preserve atomic selection/commit rechecks, output pins until export completion, retention-only dependency fences, exact canonical folder-row ownership, and filesystem discovery outside transactions. Preserve the legacy detached-folder NFC plus casefold lookup and ordinary writer behavior.

Evidence before this round: **32 mutants killed plus 1 intended equivalent**, all three previously capped properties completed at normal settings, the **202-sentence contract/decision audit**, and the one-shot measurements below. This round adds a killed attribution mutant, re-kills two prior mutants, closes audit D1 with red/green fake admission, and adds the 100-example Unicode attribution property. The three prior properties and the requested files/guards are rerun at the fixed head; counts are above.

| Jobs / live | Old SQL ms | New Python pin ms | New / old | Query load (1 / 5 / 15 min) |
|---|---:|---:|---:|---|
| 3,000 / 100 | 164.445 | 10.267 | 0.0624 | 12.7 / 13.8 / 24.0 |
| 6,000 / 600 | 1,565.193 | 32.488 | 0.0208 | 12.4 / 13.4 / 22.7 |

These are continuation 4b's measurements, not new timings. Pin sets matched at both scales; neither new read exceeded twice the old time, so no query tuning was required. The tool was revalidated in 4b. Its [report](2026-10-29-retention-pins-c4b-evidence.md) and the [resolved audit](2026-10-29-retention-pins-c4a-audit.md) remain part of the review evidence.

Not run locally: the full suite, Python 3.12, CI and `tests/frontend/test_core_live.py` (the lane cannot pass its daemon person-only check). The other prior mutants, the broader retention/admission suite and measurement tool were not rerun in this round; their earlier evidence is retained. No requested continuation 5 check is intentionally omitted.

## Delivery

Four ordered forward steps: red regression `c03ab23cf6a040ef8d365d882c9b4d9816db0c79`; dependency-attribution fix `42bdf10798ba43551adb98fdf9cf81a1ed0eed98`; property/mutants `09eb8ab36`; this report/contract text commit. All commit messages end with the requested co-author footer; named files only were staged, and no history was rewritten.

Delivery artifact: workspace-root `rdp217c5.bundle`, created from `fix/retention-dependency-pins-217` excluding base `ce9e154ebf7e7e4ceab59a89e86885e5d3035b60`, then verified. The final response names the text commit's full head. The assigned detached metadata remains at the base; the bundle carries the private branch's commits. The workspace retains all versioned changes.

The scratch environment was reused with `uv run --no-sync`; no additional network was used. TMPDIR and Hypothesis storage stayed in the prescribed scratch folder. All test and lock-wait sessions completed before closeout, scratch was checked below 1.5 GB and removed with the lane rules' guarded `shutil.rmtree` command after bundle verification. The workspace has no bytecode, Hypothesis cache, pytest cache, build artifacts or lane-owned kept failure roots.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
