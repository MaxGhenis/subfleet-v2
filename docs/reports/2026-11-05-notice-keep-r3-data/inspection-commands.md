# Fix round 3: inspection, setup and initial build evidence

Commands are grouped by purpose below; validation commands retain their
arguments, results and counts. The additional
[paging review](paging-inspection-commands.md) and
[measurement review](measurement-inspection-commands.md) appendices record
the other parallel workers' shell commands, including corrected inspection
errors. The generated regression command list links every test log and XML.

Work stayed in
`/Users/maxghenis/.subfleet/worktrees/20261113-070657-nk-fix-r3`.
Disposable state and caches used the permitted child scratch directory:

```text
/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-070657-nk-fix-r3-a1-mzq_pia6/sf-nk-f3
```

## Initial inspection and setup

The root worker's first command included `date -u`, as required, and recorded
**2026-11-13 12:17:32 UTC**. The three-hour cutoff is therefore
**2026-11-13 15:17:32 UTC**. Its initial
read-only inspection read `review-r3.md`, the neighboring `brief-fix-r2.md`,
and `docs/reports/2026-11-05-notice-keep-days.md`; it inspected the acceptance
contract, the outcome lookup and its callers, SQLite indexes and existing
tests. Searches were confined to the assigned worktree and explicitly named
review files. There was no `AGENTS.md` or pre-existing `.venv` in the checkout.

The required default temporary/cache setup was attempted:

```sh
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR="$(getconf DARWIN_USER_TEMP_DIR)/uv-cache" uv sync --group dev
```

Result: **exit 2**, opening `uv-cache/sdists-v9/.git` was denied with
`Operation not permitted`. Sync was retried with the literal permitted paths:

```sh
TMPDIR=/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-070657-nk-fix-r3-a1-mzq_pia6/sf-nk-f3 UV_CACHE_DIR=/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261113-070657-nk-fix-r3-a1-mzq_pia6/sf-nk-f3/uv-cache uv sync --group dev
```

Result: **exit 0**, **9 packages installed**, **Python 3.15.2**.

The root worker also ran:

```sh
.venv/bin/python -m py_compile subfleet/retention_notices.py subfleet/retention_cli.py tools/notice_keep_round3_measure.py tools/notice_keep_round3_mutants.py tools/notice_keep_round3_verify.py tests/unit/test_retention_outcome_pages.py tests/unit/test_retention_outcome_permissions.py
git -c core.fsmonitor=false diff --check
```

Results: **exit 0** for compilation of all **7 files**, and **exit 0** for the
diff whitespace check.

## Initial verification and measurements

| Command or operation | Observed result |
| --- | --- |
| First focused invocation of `tests/unit/test_retention_outcome_pages.py` | Exit 0; **8 passed in 119.11 s**. The retained [`pages-focused-1.xml`](pages-focused-1.xml) records 8 tests, 0 failures, 0 errors and 0 skips. Its testsuite duration is 78.559 s; 119.11 s is the reported pytest invocation duration. |
| Initial benchmark, **6,000 retirement events**, requested session absent | Complete lookup succeeded in **1.165777583 s**; maximum materialized page statement **0.025455917 s**. |
| Initial benchmark, **100,000 retirement events**, requested session absent | Exit 1 with named `OUTCOMES_LOOKUP_DEADLINE` statement-deadline failure. No successful/truncated lookup was reported. This initial benchmark aborted before retaining its JSON result, so it supplies a failure observation rather than a retained JSON timing receipt. |

The focused test invocation used a fresh scratch basetemp and wrote the XML
receipt above. Its exact full shell command and the initial benchmark command
are not present in this writer's transcript; this table records the root
worker's observed counts, times and failure result without inventing them.

The retained final measurements and mutant receipts were then produced by:

```sh
.venv/bin/python tools/notice_keep_round3_measure.py --samples 3 --output docs/reports/2026-11-05-notice-keep-r3-data/measurements.json
.venv/bin/python tools/notice_keep_round3_mutants.py --output-dir docs/reports/2026-11-05-notice-keep-r3-data/mutants
```

Results: measurement **exit 0**, **12 successful lookups, 0 failed**, covering
three samples each at 6,000, 100,000, 1,000,000 and 3,000,000 retirement
events. Mutation **exit 0**, **4 controls passed, 4 mutants killed,
0 survivors, 0 invalid**. Each mutant was applied alone to its own copy,
restored to the pristine bytes, and verified by SHA-256; worktree source
hashes also stayed unchanged. See [`measurements.json`](measurements.json)
and [`mutants/summary.json`](mutants/summary.json) for detailed receipts.

The final regression script was launched at approximately **12:58 UTC**. It
runs the required files individually, with new basetemps. Its exact commands
and per-file results are generated as JSON by the verification script; this
document does not predeclare the outcome of an active run.

## Named-file staging and commit attempts

The first named-file `git add` used these arguments:

```text
subfleet/retention_notices.py
subfleet/retention_cli.py
tests/unit/test_retention_outcomes.py
tests/unit/test_retention_outcome_pages.py
tests/unit/test_retention_outcome_permissions.py
docs/acceptance-contract.md
```

Result: **exit 128**, Git could not create
`/Users/maxghenis/subfleet-v2/.git/worktrees/20261113-070657-nk-fix-r3/index.lock`
because the sandbox denies writes to that administrative directory.

```sh
git commit -m "Bound archived outcome snapshots and report lookup failures" -- subfleet/retention_notices.py subfleet/retention_cli.py tests/unit/test_retention_outcomes.py tests/unit/test_retention_outcome_pages.py tests/unit/test_retention_outcome_permissions.py docs/acceptance-contract.md
```

Result: **exit 1**. New files were unknown pathspecs because the denied
`git add` had never put them in the index. This attempt produced no commit.

A second named-file `git add` used these arguments:

```text
tools/notice_keep_round3_measure.py
tools/notice_keep_round3_mutants.py
tools/notice_keep_round3_verify.py
docs/reports/2026-11-05-notice-keep-r3-data/inspection-commands.md
```

Result: **exit 128**, the same denied `index.lock`. No commit was produced.
No `git add -A`, stash, history rewrite or push was performed. The unchanged
built head at this point was
`e8d32cca6aef761966b8f7db89bd2fff6c6ce320`; source and evidence changes remain
in the assigned workspace for collection.

## Read-only test-review inspections

The following exact command groups are available from the test-review
worker's transcript. All were run in the assigned workspace. Each command
group completed with exit 0 unless a narrower result is identified below.
`cat`, `sed` and `rg` here only inspect source or specifically authorized
review files; none runs tests or accesses live state.

```sh
date -u
cat /Users/maxghenis/reviews/subfleet-hub/notice-keep-2026-11-05/brief-fix-r2.md
cat /Users/maxghenis/reviews/subfleet-hub/notice-keep-2026-11-05/review-r3.md
```

Result: exit 0; this child worker's clock read was
`Fri Nov 13 12:20:08 UTC 2026`. This is an inspection timestamp, not a
replacement for the root worker's start time.

```sh
rg --files -g AGENTS.md -g '*retention*outcome*' -g conftest.py
sed -n '1,320p' subfleet/retention_notices.py
sed -n '1,300p' tests/unit/test_retention_outcomes.py
rg -n 'read_only|class Store|def close' subfleet/store.py
```

Result: exit 0; inspected the original unrestricted event query, read-only
Store initialization and existing outcome coverage. The file search found no
`AGENTS.md`.

```sh
sed -n '314,410p' subfleet/store.py
sed -n '720,745p' subfleet/store.py
cat tests/unit/conftest.py
cat tests/conftest.py
rg -n 'chmod\(0o555|READONLY_DIRECTORY|read.only directory|read-only folder|chmod\(0o444|os.geteuid|usable sidecar' tests/unit tests/fake
rg -n 'Lookup filesystem promise|Fix round 2|C-8.4' docs/reports/2026-11-05-notice-keep-days.md docs/acceptance-contract.md
```

Result: exit 0 for the group; reviewed SQLite connection/cleanup behavior,
test isolation, permission-test conventions and contract/report text.

```sh
sed -n '1,160p' docs/reports/2026-11-05-notice-keep-days.md
rg -n 'geteuid|chmod\(0o555|chmod\(0o444' tests/unit/test_retention* tests/unit/test_store* tests/unit/test_test_module_names.py
sed -n '1,180p' tests/unit/test_test_module_names.py
```

Result: exit 0 for the group; no existing matching chmod/geteuid case was
printed. The module-name guard requires distinct test basenames.

```sh
git diff --check -- tests/unit/test_retention_outcome_permissions.py
sed -n '1,210p' tests/unit/test_retention_outcome_permissions.py
```

Result: exit 0; the new three-case permission module passed whitespace
inspection. No test was run by this worker.

```sh
rg -n 'archived_outcomes|def cmd_retention|def dispatch|retention_cli|retention outcomes' subfleet/retention_cli.py subfleet/cli.py subfleet/compat.py tests/unit/test_retention_outcomes.py
sed -n '1,215p' subfleet/retention_cli.py
sed -n '1,180p' subfleet/compat.py
sed -n '185,245p' subfleet/retention_notices.py
```

Result: exit 0; the sole production caller of `archived_outcomes` is
`retention_cli.cmd_retention`. Both `cli.main` and the installed
`compat.dispatch` entrypoint reach that handler.

```sh
sed -n '740,845p' subfleet/compat.py
rg -n '^def main|^def fail|^def emit' subfleet/cli.py
sed -n '1,170p' subfleet/retention_notices.py
sed -n '45,170p' tests/unit/test_retention_outcomes.py
rg -n 'compat.dispatch\(' tests/unit/test_compat.py | head -12
sed -n '115,145p' subfleet/cli.py
sed -n '3896,3938p' subfleet/cli.py
sed -n '300,350p' tests/unit/test_retention_outcomes.py
```

Result: exit 0; checked deadline naming, stderr/exit behavior and existing
held-read hooks before updating the tests.

```sh
git diff --check -- tests/unit/test_retention_outcomes.py
git diff --stat -- tests/unit/test_retention_outcomes.py
```

Result: exit 0; **32 insertions, 3 deletions** at that point. The changes
updated two statement-count/query hooks and added six deadline-error cases:
three entrypoints multiplied by text/JSON output. The cases set the real
lookup deadline to zero and require exit 1, named stderr and no stdout.

During this evidence draft, `rg --files` and `rg -n` inspected the retained
round-3 evidence, and `sed` inspected the focused XML, page tests and
`pyproject.toml`. Those command groups exited 0. One exploratory unquoted
glob, `tools/notice_keep_r3*`, had no match and zsh refused that individual
`rg` command before execution; the surrounding group's final `rg` succeeded.
It produced no state change and no benchmark/test result.

All test-review worker tool sessions completed. This worker changed only the
assigned test modules and this Markdown evidence file; it did not execute
tests or make a commit.

## Root read-first and focused-test commands

The first root shell command, exit 0 after its status operation completed,
was exactly:

```sh
date -u
pwd
git status --short --branch
cat /Users/maxghenis/reviews/subfleet-hub/notice-keep-2026-11-05/review-r3.md
cat /Users/maxghenis/reviews/subfleet-hub/notice-keep-2026-11-05/brief-fix-r2.md
cat docs/reports/2026-11-05-notice-keep-days.md
```

It printed `Fri Nov 13 12:17:32 UTC 2026`, the assigned workspace and a clean
detached head. The three `cat` reads were repeated while the original status
operation was pending, exit 0. These read-only commands execute zero tests.

Source inspection used `rg --files -g AGENTS.md -g '*retention*outcome*'`,
`rg -n` for `archived_outcomes`, event indexes, Store initialization/query/
close, CLI entrypoints, C-8.4, deadline names and test names, and `sed`/`cat`
on those files. `rg --files subfleet tests tools | wc -l` returned 1,363.
`cat AGENTS.md` and `ls -ld .venv` initially reported absent files. The
exploratory command
`rg -n 'events_notice_retired|CREATE TABLE IF NOT EXISTS events|CREATE INDEX.*events' subfleet/schema* subfleet/*.sql subfleet/store.py`
was refused by zsh's unmatched `subfleet/schema*` glob (exit 1); the corrected
`rg` read `subfleet/store_schema.sql` successfully. Repeated `git -c
core.fsmonitor=false status --short --untracked-files=normal`, `git diff
--stat`, `git diff --check`, `ls` of owned evidence and `date -u` completed;
all searches stayed within the worktree, explicitly authorized review files
or this job's scratch. No test count is attributed to an inspection command.

Using the literal scratch path documented above as `T`, the first focused
validation command had these exact arguments and environment (exit 0,
8 passed in 119.11 seconds):

```sh
TMPDIR="$T" UV_CACHE_DIR="$T/uv-cache" SUBFLEET_LIVE=0 uv run --no-sync pytest -q -s --tb=short --timeout=90 --basetemp "$T/pages-focused-1" --junitxml docs/reports/2026-11-05-notice-keep-r3-data/pages-focused-1.xml tests/unit/test_retention_outcome_pages.py
```

Repeated Python receipt inspections parsed the retained JSON/XML, printed
completed-file/test/error/skip counts and computed the benchmark medians;
these were read-only, exit 0, and ran no additional tests. Mutation log
inspections confirmed the actual call-phase assertions: `[2503]` instead of
`[1000,1000,503]`, one extra late event, a non-None partial report, and the
missing second-session outcome.

## Final checks and cleanup

Final regression completed: 19 separate invocations, 1,065 passed, 0 failures,
0 errors and 0 skips, exit 0. Exact command arrays and per-file counts are in
`final-r3-commands.json` and `regression-summary.json`; all logs/XML are retained.
The explicit-cache sync, eight-file py_compile and git diff --check each exited
0 (`final-checks.json`); sync resolved 10 packages and checked nine installed
packages, without another install. Ten source/document whitespace checks,
source SHA-256 and contiguous changelog checks passed.

Final named-file staging was denied at index.lock, exit 128; the exact list
and stderr are in `final-stage.json`. No final commit command can run before
successful staging. The original runtime commit attempt is recorded above.

Guarded `shutil.rmtree` first exited 1 on an intentionally readonly archive
fixture. It succeeded, exit 0, after granting owner directory permissions on
one owned directory without following symlinks (`cleanup.json`). Scratch was
fully removed. Thirteen framework-cache folders created within the worktree,
outside scratch, were recorded before cleanup, renamed to paths containing
`sf-nk-f3`, asserted to be owned workspace paths, and removed using guarded
`shutil.rmtree` (`worktree-cache-cleanup.json`). No live state was accessed.
