# Salvage ref race (D-SALV1)

Base: `release/217`, `a26d08336fc4`. Validation ran on 2026-10-22.

Confirmed cause: detached worktrees share their repository's ref namespace.
`salvage()` combines branch, reservation second and attempt sequence, so two jobs
can choose the same name. `_hold()` checked for a ref before a create-only
`update-ref`; losing that check/create race raised instead of applying its
existing replay/sibling rule.

The fix tries only the original name and its existing `-<tree12>` sibling.
After Git's C-locale `reference already exists` failure, it rereads that name
once: the same tree and parent reuse the winner; a different snapshot at the
base proceeds to the sibling. A second collision reuses a matching sibling.
Conflicting siblings, disappeared refs and other Git failures still raise.
Every write remains create-only. C-13.1 and its change log state this rule.

Naming is unchanged. Exact-name readers include retention archive and survey
code, which resolve recorded artifact paths; `source_of_gone_tree()` also checks
commit digests when inferring a repository. They accept sibling refs without
reconstructing the base name. `pin_baseline()` separately looks up its existing
`<job>-a<seq>-baseline` name; that convention is unchanged.

Tests used GIL CPython 3.14.7, the required suite wrapper, private scratch, and
`HYPOTHESIS_PROFILE=ci`, with at most four pytest workers. The new property has
eight generated schedules plus an explicit six-writer mixed-tree schedule.
Real detached worktrees and barriers force absent-ref reads before either CAS;
ordered winners distinguish ref collisions from transient lock-file contention.

| Check | Result |
| --- | --- |
| Release race regressions | 3 failed as expected |
| New race/property/failure tests | 9 passed |
| Drop-retry mutation | Named regression failed as expected; source restored |
| Requested salvage/finalization/retention selection | 299 passed, 829.35 s |
| State contract and module names | 38 passed, 12.14 s |

The mutation was killed by
`tests/unit/test_salvage_ref_race.py::test_concurrent_salvage_keeps_different_trees`.

**Whole-suite validation is incomplete.** The first 144-file group waited about
18 minutes for a slot, then was interrupted after about 18 minutes 49 seconds
to remain below the 20-minute hold bound. Complete printed progress rows reached
86%: at least 5,271 passes, 9 unexpected failures, 46 skips and 2 expected
failures. There is no final pytest summary; failed selectors were not emitted
before interruption, so those failures remain untriaged and unattributed.

For reproduction, sort all `test_*.py` and `*_test.py` paths recursively under
`tests/unit` and `tests/fake` together (432 files). The attempted group is
`files[0::3]`; groups `files[1::3]` and `files[2::3]` (288 files) were not started.
No new holds started after the conservative 05:46 UTC cutoff inferred from the
workspace timestamp. Some files in those groups were covered by the focused
runs above.

Commits: `54ef772fb` (tests first), `c1ef190ca` (bounded, ordered schedules),
`08d72b2eb` (fix and contract). Shared-index writes were denied by the sandbox,
so commits use workspace-local `.salv1-git` metadata on
`fix/salvage-ref-race-217`; inspect with `git --git-dir=.salv1-git log`.
Nothing was pushed.

All tracked foreground commands returned. The required `pgrep` audit could not
access the process list (`sysmond service not found`), and `/bin/ps` was denied,
so an independent descendant-process audit was unavailable. Job scratch was
removed after repairing permissions on its remaining test directories; no
workspace `.hypothesis` directory was present.
