# #141/#139 fence integration

Base: `74d49042d86a8ae7710644db238aeeaec35eb9dc`, the merge of approved
#141 with release/217's #139. Work stayed in the assigned checkout; no network
remotes, sub-agents, history rewriting or push. Shared Git metadata was read-only,
so commits are on `fix/case-alias-race` in this workspace's `.git-local`.
Implementation commits: `8490c8e27`, `c2541a90c`, `7fc376a36` (code head).

## Changes

- `subfleet/daemon.py:4662`: `_turn_fences` gives the early look and reserving
  transaction one comparison, including both Git and actual run folders,
  ancestor fences, folded absent names, deduplication and the named hold folder.
  Identical paths reuse a query only when their absence flags also agree.
- `subfleet/daemon.py:4705`: the early look spells both folders with
  `folders.present` before reading the store. Its state check and transactional
  re-read still decide the hold.
- `subfleet/daemon.py:5133`: reservation retains each folder's spelling and
  independent absence flag, and uses the shared comparison at line 5464.
- `tests/unit/test_retention_shared_folders.py:633`: a differential Hypothesis
  property compares the real early look with the real reservation, plus an
  independent Unicode/path oracle. Workspace snapshots are stubbed; filesystem
  spelling, store fence queries and admission are real. It asserts that spelling
  runs outside the store writer lock.
- `tests/unit/test_retention_shared_folders.py:778` and `:811`: the two fence race
  fakes accept and forward `folded`; their race, placement, workspace preparation,
  hold and capacity assertions remain intact.
- `docs/acceptance-contract.md:242`: C-8.4's early-look sentence specifies the
  same spelling and folded comparison as reservation; other sentences remain.

The property uses 150 generated examples and six explicit examples per run.
Each runs before quarantine and after moving the trees: 312 filesystem scenarios
and 624 admission observations. Inputs include present/absent names, case and
NFC/NFD aliases, nested paths, shared/separate Git and run folders, read-only and
writable turns, and arbitrary subsets of ten fence locations (including
non-ancestor decoys).

## Validation

The full requested suites passed: **496 passed (71 fake, 425 unit), zero errors,
failures or skips**, in 2234.01 seconds (37m14s), across 26 files. The unit glob includes
`test_retention_case_alias.py` and `test_retention_shared_folders.py` once each.
The focused regressions passed all seven selected tests (61 deselected), including
the reported three failures and the property, twice: 64.29 seconds and 27.10
seconds. The new property also passed in the full run (2.220 seconds).

Serial mutation results are pending.

Every pytest invocation uses `/usr/bin/lockf -k
/private/tmp/claude-501/subfleet-suites.lock`, with a dedicated
`$(getconf DARWIN_USER_TEMP_DIR)sf-141m-<pid>/` temporary root removed afterwards.
Mutation runs change temporary source copies, never the checkout's source.

Nothing was skipped for denied `ps` or `lsof`: there were no test skips. A direct
`ps` diagnostic was denied by the sandbox; the selected tests did not need it.
`test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree` passed with real `lsof`.
