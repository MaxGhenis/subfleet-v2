# PR #192 review round 2 fix

Base: `58177ad3306f`. All work is confined to the assigned workspace, using
synthetic stores. No caller checkout writes, live store/state/process, network
remote, or sub-agent was used.

Commits:

- `8f0c76877` — preserve the nine round-2 reviewer probes byte for byte and add
  storage-class injectivity, special-value, and repeated-read tests.
- `ce39a5295` — tag every SQLite cell in the shared row fingerprint; update
  C-11.5 and `docs/migration.md`.

The former JSON spelling of BLOB `X'00FF'` was identical to TEXT `blob:00ff`.
Every cell is now encoded as `[storage-class, payload]`, retaining column
names in the canonical row object. NULL, INTEGER, REAL, TEXT and BLOB have
distinct tags. REAL payloads preserve all 64 IEEE-754 bits, including signed
zero, infinities and NaN payloads passed directly. SQLite converts bound NaN
to NULL, which has a separate regression. BLOB payloads are hex bytes.

Canonical JSON uses `ensure_ascii=False` followed by UTF-8 with
`surrogatepass`, preserving NULs and lone surrogates and distinguishing a
literal surrogate pair from its corresponding non-BMP character, including
in column names. ASCII JSON escaping alone would alias that last pair.
Planning, whole-store backup verification, retained-decision checks and both
post-delete and post-VACUUM checks all use `_row_digest` through `_digest` or
`_digest_ids`. The table fingerprint still hashes a sorted multiset of row
digests; counts and column names remain part of the proof.

`tests/unit/test_pr192_review_r2.py` retains both executed review inputs under
`test_one_changed_backup_row_at_identical_counts_is_refused`:

- `service_notices.text`: TEXT `blob:00ff` becomes BLOB `X'00FF'`.
- `review_evidence.payload`: BLOB `X'00FF'` becomes TEXT `blob:00ff`.

They require healthy copied integrity, unchanged table counts, a demonstrably
different copied row, a named verification error, zero committed deletions
and batches, and unchanged source tables. The other seven reviewer cases are
also preserved.

`tests/unit/test_prune_decision_fingerprints.py` configures 500 generated row
pairs across all five storage classes and mixed column names, plus nine
explicit injectivity examples. It compares digests against an independent
value/type/name equality oracle, including equal rows with reordered columns,
single-cell edits, column renames and unrelated rows. Eleven explicit cell
pairs cover TEXT/BLOB, INTEGER/REAL, TEXT/INTEGER, signed zero, infinities,
surrogates and NULs. A second property configures 200 generated SQLite table
reads plus one explicit mixed-storage example and compares repeated and
reversed reads. Surrogates that SQLite's default adapter cannot bind are
covered directly by the row property. These are configured counts; executed
counts are recorded below.

Validation: **0 passed, 0 failed, 0 collected/executed**. Three separate
foreground lock attempts each waited up to 60 seconds and exited 75 with
`lockf: /private/tmp/claude-501/subfleet-suites.lock: already locked`.
No attempt acquired a lock hold or started pytest. All commands completed
and each task TMPDIR was cleaned; no runner or build was left behind.

| Selection | Prepared cases (static source count) | Executed | Result |
| --- | ---: | ---: | --- |
| Unchanged `58177ad33`: four copied-backup mutations, including TEXT→BLOB and BLOB→TEXT | 4 | 0 | Unrun: shared lock |
| Fixed suite: required prune suite, both review suites, new fingerprint tests, prune-217, restore, module names | 107 | 0 | Unrun: shared lock |
| Untagged mutation: round-2 probes and new fingerprint tests | 24 | 0 | Unrun: shared lock |

The fixed selection includes 48 cases in `test_prune_decisions.py`, 9 in
`test_pr192_review_r2.py`, 15 in `test_prune_decision_fingerprints.py`,
21 in `test_pr192_review_r1.py`, 9 in `test_prune_217.py`, 4 in
`test_prune_restore.py`, and 1 in `test_test_module_names.py`. These counts
come from function definitions and parametrization, not pytest collection.

**Hypothesis examples executed: 0.** The 500 generated plus 9 explicit row
pairs and 200 generated plus 1 explicit repeated-read table examples remain
unrun. No successful fixed-suite result, demonstrated baseline regression
failure, or executed mutation failure is claimed. The reviewer independently
executed both collision regressions on this baseline, but this job could not
repeat them under the required lock.

Static checks passed: AST parsing of the implementation and both new test
modules; `git diff --check`; the baseline module's byte comparison and SHA-256;
and byte-for-byte comparison of all nine copied probes with the reviewer's
file. Source tracing confirms that planning, copy verification and the
post-delete/post-VACUUM proofs share `_row_digest`. The dependency import
check reports CPython 3.14.7 free-threading, pytest 9.1.1, and Hypothesis
6.168.1. None of these static checks substitutes for execution of the tests.

The unchanged baseline lives in ignored workspace-local
`.git-local/baseline/`, with the original prune module checked byte for byte
against `git show 58177ad3306f:subfleet/prune_decisions.py`; its SHA-256 is
`1fb26da19ee1fa520ff939064e67668df06951d06ce715f915a143c872ca9879`.
The mutation plugin in `.git-local/untagged_mutation.py` extracts exactly the
base `_blob` and `_row_digest` functions with AST, then installs the untagged
row encoder for one pytest process. It leaves committed source untouched.

Every pytest attempt uses a separate foreground hold of
`/private/tmp/claude-501/subfleet-suites.lock`, a fresh
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r192f2-$$/`, and an EXIT trap with
`python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TMPDIR"`.
Dependencies are copied read-only from cached packages into an ignored local
virtualenv. `UV_NO_SYNC=true`, `UV_OFFLINE=true`, and a local `UV_CACHE_DIR`
prevent downloads and editable builds. Automatic pytest plugins are disabled;
the Hypothesis plugin is loaded explicitly with `-p _hypothesis_pytestplugin`.

Hub retry commands, from the assigned workspace after preparing dependencies
and the described baseline/mutation helper. Run one block at a time in the
foreground, with one separate lock hold per command, under 20 minutes:

```sh
export UV_CACHE_DIR="$PWD/.git-local/uv-cache"
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
export UV_NO_SYNC=true UV_OFFLINE=true PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
```

Unchanged baseline (selects four cases, including both collision directions):

```sh
(
    export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)sf-r192f2-$$/"
    mkdir -p "$TMPDIR"
    trap 'python3 -c '\''import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'\'' "$TMPDIR"' EXIT
    cd .git-local/baseline
    /usr/bin/lockf -k -t 60 /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q -p _hypothesis_pytestplugin tests/unit/test_pr192_review_r2.py -k test_one_changed_backup_row_at_identical_counts_is_refused
)
```

Fixed suite (includes statistics for generated examples):

```sh
(
    export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)sf-r192f2-$$/"
    mkdir -p "$TMPDIR"
    trap 'python3 -c '\''import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'\'' "$TMPDIR"' EXIT
    /usr/bin/lockf -k -t 60 /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q -p _hypothesis_pytestplugin --hypothesis-show-statistics tests/unit/test_prune_decisions.py tests/unit/test_pr192_review_r2.py tests/unit/test_prune_decision_fingerprints.py tests/unit/test_pr192_review_r1.py tests/unit/test_prune_217.py tests/unit/test_prune_restore.py tests/unit/test_test_module_names.py
)
```

Untagged mutation (restores exactly the baseline encoder in memory):

```sh
(
    export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)sf-r192f2-$$/"
    mkdir -p "$TMPDIR"
    trap 'python3 -c '\''import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'\'' "$TMPDIR"' EXIT
    export PYTHONPATH="$PWD/.git-local:$PWD"
    /usr/bin/lockf -k -t 60 /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q -p _hypothesis_pytestplugin -p untagged_mutation --hypothesis-show-statistics tests/unit/test_pr192_review_r2.py tests/unit/test_prune_decision_fingerprints.py
)
```

The ignored baseline and mutation helper are workspace artifacts, not staged
deliverables. If only the commits are transferred, reconstruct the baseline
from `58177ad33` and copy the two committed new test modules into it. To
reconstruct the mutation helper, AST-extract the baseline `_blob` and
`_row_digest` functions, execute them with a copy of the current prune
module's globals, and install that `_row_digest` in `pytest_configure`.
The committed implementation is never reverted or modified by this check.

Unrun beyond the selections above: full repository suite, full retention/Git
archive round trips, and any live-store dry run/apply. No live checks were
attempted.

The sandbox prevents writing the shared Git metadata and the worktree `.git`
pointer. Commits therefore use ignored workspace-local `.git-local` metadata,
on local branch `feat/prune-decisions-217` rooted at the assigned base.
Commands use `git --git-dir=.git-local`; the original shared metadata and
detached HEAD are untouched. Only named deliverable files are staged. Every
commit includes the requested co-author trailer.
