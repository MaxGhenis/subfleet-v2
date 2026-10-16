# PR #131 fix round seven: final verification

Prerequisite: `a309de52588236f97c365ad023576fab15a15785`. All changes and commits remain in the assigned workspace, on workspace-local `.git-local` branch `feat/quarantine-self-resolve`. Shared Git metadata is outside the writable roots. No push or history rewrite.

## Fixes

| Commit | Location | Mechanism removed | Evidence |
|---|---|---|---|
| `f2d15d66f` | `subfleet/daemon.py:4036`, `:6299` | Both kill consumers defaulted a missing shape to the attempt's own group, promoting retained-group evidence into signal ownership; each now requires an explicit matching group shape | S2: 50 retained-authority worlds, seed 13107; eight authority probes |
| `31dc1dcc8` | `subfleet/procs.py:633` | Two leader-identity/reuse shortcuts discharged a sampled group even though a failed bracket could associate it with another incarnation; retained groups now hold until verified empty or a proven reboot | S1: 50 reused-before-group worlds, seed 13107; 128 process/round-six tests |
| `9681c7247` | `docs/acceptance-contract.md:203` | C-5.7 states the authority and release rules in one sentence each | Documentation review, `git diff --check` |

The old saved-lineage unit case now expects a populated reused-leader group to hold, matching the conservative rule. This intentionally permits a foreign reused group to keep quarantine held until the shared census verifies it empty. The existing invisible-writer residual and model safety domain are unchanged.

## Process-world results

| Run | Seed | Passing worlds | Failures | Pytest result |
|---|---:|---:|---:|---|
| Fixed | 13107 | 2,000 | 0 | 2 passed in 1794.78 s |
| Fresh | 1564217222 | 500 | 0 | 2 passed in 534.60 s |

No additional production defect was found in either required model run. All four initial scenarios and both kill consumers remain enabled, with 25 steps and shrinking; 342 and 104 invalid generated cases are excluded from the passing-world counts. Fixed and fresh results are committed as `2f43594ef` and `e6cd4f77e`; their full statistics are in `model-fixed-2000.txt` and `model-fresh-500.txt`.

The kernel remains the independent truth for writer lifetime, identity, parent, group and session. Automatic and operator resolvers replay equal cloned worlds. Safety includes S1 (no release with a census-covered writer alive), S2 (signal authority), P1 (resolver/census parity) and L1 (bounded release after every conservative source empties). Zombies cannot write. The separate expected-counterexample test demonstrates the documented invisible-writer residual; it does not claim unconditional S1.

Historical proof is inherited unchanged from the prerequisite: **7/7 known historical bugs found**, including the round-five sampled-group loss and both round-six P1s at both revisions, with authority tested in both consumers; see `history-summary.txt` and the minimized historical logs.

## Tests and mutations

**205 targeted checks passed**, including **all 74 round-4/5/6 probes**; zero failures, errors or skips. Counts are unique nodes in the final targeted slice, excluding repeated focused runs and mutation controls. The two model test nodes were each run twice for the required budgets (four pytest passes, 2,500 passing generated worlds), separately from these 205 nodes. The focused S1/S2 runs each passed another 50 worlds before their respective fix commits.

| Targeted file | Passed |
|---|---:|
| `tests/unit/test_procs.py` | 96 |
| `tests/unit/test_census_interleavings.py` | 8 |
| `tests/unit/test_quarantine_mutations.py` | 6 |
| `tests/fake/test_review_pr131_probes.py` | 21 |
| `tests/fake/test_review_pr131_round4.py` | 10 |
| `tests/fake/test_review_pr131_round5.py` | 24 |
| `tests/fake/test_review_pr131_round6.py` | 16 |
| `tests/fake/test_review_pr131_round6b.py` | 16 |
| `tests/fake/test_review_pr131_round6b_authority.py` | 8 |
| **Total** | **205** |

**33/33 mutations killed**, each after a passing control (66 sequential pytest slices). Survivors: zero; failed controls: zero; inconclusive: zero. Independent SHA-256 comparison and an empty production diff confirm exact restoration of both production files; see `restoration.txt`. The four new mutations cover both signal consumers and the two discharge shortcuts independently. Mutation preparation is committed as `103f6f62b` and `f9347bd2b`.

The mutation tool adds four cases for the two P1 findings (both authority consumers and each discharge shortcut independently), for 33 total. Each requires a passing unmodified control, pytest exit 1 with an actual assertion failure for a kill, and exact production-byte restoration; timeouts or collection errors do not count as kills. Production module caches are removed before each invocation, while stable library caches are retained. `--assert=plain` avoids repeatedly rewriting assertions; ordinary Python assertions remain enabled.

| Mutation | Production file | Control / mutant |
|---|---|---|
| attempt kill infers ownership from missing group shape | `subfleet/daemon.py` | pass / killed |
| probe kill infers ownership from missing group shape | `subfleet/daemon.py` | pass / killed |
| leader reuse discharges an unconfirmed sampled group | `subfleet/procs.py` | pass / killed |
| dead identity discharges an unconfirmed sampled group | `subfleet/procs.py` | pass / killed |
| shared cwd mistakes another attempt for a writer | `subfleet/procs.py` | pass / killed |
| shared root mistakes another attempt for a writer | `subfleet/procs.py` | pass / killed |
| failed confirmation drops the conservative group | `subfleet/procs.py` | pass / killed |
| later observations reuse the old identity | `subfleet/procs.py` | pass / killed |
| later observations inherit the old zombie verdict | `subfleet/procs.py` | pass / killed |
| later observations inherit the old group | `subfleet/procs.py` | pass / killed |
| missing start identity treated as absence | `subfleet/procs.py` | pass / killed |
| paced inspection forgets missing starts | `subfleet/daemon.py` | pass / killed |
| late observations lack durable roots | `subfleet/procs.py` | pass / killed |
| cwd kernel aliases ignored | `subfleet/procs.py` | pass / killed |
| reused guardian root proves provider publication | `subfleet/procs.py` | pass / killed |
| failed identity observations forgotten | `subfleet/procs.py` | pass / killed |
| saved lineage identities ignored | `subfleet/procs.py` | pass / killed |
| saved lineage groups ignored | `subfleet/procs.py` | pass / killed |
| cwd writer excluded from live census | `subfleet/procs.py` | pass / killed |
| partial markers discarded | `subfleet/procs.py` | pass / killed |
| legacy owned provider cannot discharge | `subfleet/daemon.py` | pass / killed |
| lineage overflow silently releases | `subfleet/procs.py` | pass / killed |
| corrupt diagnostic boots accepted as ownership proof | `subfleet/daemon.py` | pass / killed |
| same-boot reboot gate restored | `subfleet/procs.py` | pass / killed |
| marker scan ignored | `subfleet/procs.py` | pass / killed |
| missing child publication accepted | `subfleet/procs.py` | pass / killed |
| retained group descendant roots omitted | `subfleet/procs.py` | pass / killed |
| pre-reboot group roots retained | `subfleet/procs.py` | pass / killed |
| unverifiable census accepted | `subfleet/procs.py` | pass / killed |
| PID reuse ignored | `subfleet/procs.py` | pass / killed |
| leases retained after release | `subfleet/daemon.py` | pass / killed |
| durable pace disabled | `subfleet/daemon.py` | pass / killed |
| salvage receipt ignored | `subfleet/daemon.py` | pass / killed |

## Execution and delivery

Checks use CPython 3.14.7, pytest 9.1.1 and Hypothesis 6.168.1, a fresh Darwin user temporary directory outside HOME with no component named `tmp`, and targeted foreground slices with no `-n`. An initial baseline/model overlap was detected; the first model was interrupted and awaited, and the 50-example authority model was rerun serially. The final verification runs are serial. No model bounds, scenarios, kernel transitions or invariants were changed. All test slices and mutation children were awaited; the task-owned temporary directory is removed at delivery. No live Subfleet state, caller checkout, provider or guardian was used, and no sub-agents were spawned.

A startup profile found assertion rewriting/import work dominating a short slice; installed and stable repository bytecode caches were prepared locally. The saved patch's production authority change and mutation-cache improvement were applied after inspection.

Delivery: `docs/reports/2026-10-09-pr131-fix7c.bundle`, prerequisite `a309de52588236f97c365ad023576fab15a15785`, head ref `refs/heads/feat/quarantine-self-resolve`. The bundle is regenerated and verified after the report commit; it names the final report head on that ref. The final response names the exact bundle head. Every new commit ends with the requested Claude Opus 5.5 co-author trailer.

Reproduction (assign a fresh Darwin user temporary directory to `TMPDIR` first):

```sh
SF_WORLD_EXAMPLES=2000 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest --assert=plain -q tests/fake/test_quarantine_process_world.py --hypothesis-seed=13107 --hypothesis-show-statistics
SF_WORLD_EXAMPLES=500 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest --assert=plain -q tests/fake/test_quarantine_process_world.py --hypothesis-seed=1564217222 --hypothesis-show-statistics
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tools/quarantine_mutations.py
```

Verification artifacts: [fixed model](model-fixed-2000.txt), [fresh model](model-fresh-500.txt), [targeted run](targeted.txt), [per-file counts](targeted-counts.tsv), [mutation controls and kills](mutations.txt), [source restoration](restoration.txt). The final targeted run is committed as `76be3a2a6`; `git diff --check` passed.
