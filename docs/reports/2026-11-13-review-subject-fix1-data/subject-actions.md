# Subject-normalization subagent actions

All repository edits were inside the assigned workspace. Scratch outputs were under `/private/tmp/sf-a2-fix1`. No live state or existing host process was inspected. Test processes were the supplied isolated launcher’s children. No publication command was executed.

## Finding 6: normalization

Cause: `normalize_subject` recognized only HTTP(S) `github.com/.../pull/N` with an optional `/files` suffix. Seven other spellings retained separate text identities.

Changed files:

- `subfleet/review_subject.py`: recognize shorthand `owner/repo#N`, schemeless GitHub URLs, `www.github.com`, `/changes`, `/commits`, `/checks`, and `/files/<hex-sha>`; continue returning a lowercase owner/repository canonical HTTPS PR URL.
- `tests/unit/test_review_subject_normalization.py`: seven parameterized cases, each using mixed owner/repository case and asserting the same canonical URL.

Commands and results:

```sh
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/sf-a2-fix1/uv-cache uv run --no-sync --with 'pytest-xdist>=3.6' python /private/tmp/sf-a2-fix1/run.py -q -n 0 tests/unit/test_review_subject_normalization.py --basetemp=/private/tmp/sf-a2-fix1/pytest-subject-red
```

Before the production change: exit 1, **7 failed** in 31.24 s (launcher 35.70 s). Every new parameter failed its canonical-URL assertion. `uv` reported installing seven packages for its isolated `--with` environment.

The production edit was applied with `apply_patch`, successful.

```sh
TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/sf-a2-fix1/uv-cache uv run --no-sync --with 'pytest-xdist>=3.6' python /private/tmp/sf-a2-fix1/run.py -q -n 0 tests/unit/test_review_subject_normalization.py --basetemp=/private/tmp/sf-a2-fix1/pytest-subject-green
```

After the change: exit 0, **7 passed** in 10.66 s (launcher 17.29 s).

```sh
git add subfleet/review_subject.py tests/unit/test_review_subject_normalization.py && git commit -m "fix(review): canonicalize common GitHub PR spellings"
```

Exit 128: `git add` could not create the repository worktree’s `index.lock` (`Operation not permitted`). The `&&` prevented the commit command from being reached. No commit was created, no index/history was rewritten, and the named files remained for collection.

Read-only source checks included the following commands:

```sh
sed -n '1,220p' /Users/maxghenis/reviews/subfleet-hub/review-guidance-2026-11-13/review-a2-r1.md && sed -n '1,240p' subfleet/review_subject.py && rg -n 'review_subject|canonical' tests/unit/test_review_subject.py tests/unit -g '*review*'
rg --files -g AGENTS.md -g '*review*subject*' -g pyproject.toml -g conftest.py && rg -n 'normalize_subject' tests && sed -n '1,220p' tests/unit/test_review_subject_protocol.py
sed -n '1,260p' tests/fake/test_review_subject_validation.py
sed -n '1,230p' tests/fake/test_review_subject_submission.py
sed -n '1,180p' tests/conftest.py
sed -n '1,180p' tests/unit/conftest.py
ls -l .venv/bin/python .venv/bin/pytest
rg --files docs | rg -v 'reports/' | head -40
sed -n '1,120p' pyproject.toml
git grep -l review_subject -- tests ':!tests/fixtures/*'
git diff -- subfleet/review_subject.py tests/unit/test_review_subject_normalization.py
```

The first combined read exited 2 because the guessed `tests/unit/test_review_subject.py` did not exist. The second exited 1 because the direct `normalize_subject` search had no matches, so its final guessed read did not execute. Existing validation/submission tests were then found and read successfully. One combined `git status --short` and broad documentation search was interrupted (exit 130) after producing the worktree status; it changed nothing. Other listed reads succeeded.

## Importer inventory and safety refinement

No tests ran in this phase. Product/tests/docs were not edited. The active pytest safety plugin was read but never modified by this subagent while workers were running.

Read-only discovery:

```sh
git grep -l -E '(from subfleet([.]sessions|[.]conversations)? import|import subfleet([.]sessions|[.]conversations)?[.])(store|cli|daemon|review_subject|protocol|client|service)|from subfleet\.(store|cli|daemon|review_subject|protocol|sessions[.]client|conversations[.]service) import' -- tests
```
Fix round 4 of PR #314 rewrote the bracket class before the opening parenthesis as the equivalent `\.` to prevent `tests/frontend/test_visual_report_hygiene.py` from reading it as a Markdown link, and the rewritten pattern matches the same lines as the one that ran.

Exit 0; preliminary discovery returned 292 paths. This expression missed some import layouts, so it was replaced by broader grep discovery and an import-statement scanner.

The reproducible final inventory implementation is `importer_inventory_fast.py` in this scratch folder. It invokes:

```sh
git grep -l -E 'subfleet.*(store|cli|daemon|review_subject|protocol|sessions|conversations)|from subfleet import' -- tests
python3 /private/tmp/sf-a2-fix1/importer_inventory_fast.py > /private/tmp/sf-a2-fix1/importer-inventory-summary.txt
```

Exit 0. At the recorded snapshot: **677 grep candidates**, **392 direct importers** comprising **380 test modules and 12 helpers/conftests**. Explicit helper-import closure, including pytest basename imports, contained **544 test modules**, **164 beyond direct imports**. Implicit conftest loading was deliberately not expanded into the entire suite. The scanner handles aliases, parenthesized multiline imports and embedded child-Python imports conservatively.

Outputs:

- `importer-inventory.json`, `importer-inventory-summary.txt`.
- `importers-direct.txt`, `importers-transitive.txt`.
- `importer-safe-selection.json`, `importers-safe-direct.txt`, `importers-skipped.txt`, `importers-first-stage.txt` from `python3 /private/tmp/sf-a2-fix1/finalize-importers.py` (exit 0).

The first selection was 341 safe direct files, 31 whole-file skips, six explicit first-stage files and two final-required direct importers. After delayed git reads identified newer test files, the scratch selection snapshot became 337 safe, 31 skips, ten first-stage and two final-required. The coordinator subsequently changed stage membership and restored safely runnable files; these old snapshots must not be presented as the final executed counts. All four final-required files were excluded by name, although only `test_state_contract.py` and `test_handler_refusals.py` were direct importers.

Read-only hazard scans saved to scratch:

```sh
git grep -n -E '(gh.{0,30}(POST|PATCH|DELETE|merge|rerun)|git.{0,20}push|real_lsof|real_cwd|launchctl|agent-secret|chief-of-staff|[/~][.]subfleet|[/~][.]claude|[/~][.]codex)' -- tests > /private/tmp/sf-a2-fix1/importer-hazard-hits.txt
rg -n -g '*.py' '(\[.*["\x27]gh["\x27].*(POST|PATCH|DELETE|merge|rerun)|["\x27]git push|real_lsof|real_cwd|subprocess.*["\x27]{1}(ps|pgrep|lsof)|["\x27]{1}(ps|pgrep|lsof)["\x27])' tests > /private/tmp/sf-a2-fix1/specific-hazard-hits.txt
rg -n -g '*.py' "[\"']{1}(push|merge|rerun|POST|PATCH|DELETE)[\"']" tests > /private/tmp/sf-a2-fix1/command-verb-hits.txt
rg -n 'real process|real kernel|host.*(process|table)|census.*real|real.*census|whole.host' tests/fake/*.py tests/unit/*.py > /private/tmp/sf-a2-fix1/host-census-candidates.txt
rg -n '(import_module|__import__).*subfleet\.(store|cli|daemon|review_subject|protocol|sessions\.client|conversations\.service)' tests
```
Fix round 4 of PR #314 added `{1}` after each bracket class before an opening parenthesis to prevent `tests/frontend/test_visual_report_hygiene.py` from reading it as a Markdown link, and each rewritten pattern matches the same lines as the one that ran.

All exited 0. The specific scan had 36 hits. Dynamic-import scan found one already-included protocol importer (`test_capacity_wait_backoff.py`). Hazard strings were inspected as source data only.

Four AST-based inventory attempts were interrupted with exit 130 because retained ASTs/source-segment extraction were too costly under load; statement-only and releasing-tree variants were also abandoned. They produced no repository changes or final inventory. The final regex scanner completed. Two scratch finalizer variants waiting for `git diff --name-only` were interrupted (exit 130); an independent delayed read later completed (exit 0), identifying the actual changed tests. All corresponding tool sessions ended.

Source inspection used `sed`/`rg` on the discovered files and their helpers, especially `tests/conftest.py`, `tests/fake/conftest.py`, `tests/fake/run_daemon.py`, gate admission/merge/service/end-to-end tests, all direct host-push importers, publication guards/rules/hooks, process-census tests, all seven direct frontend files, `tests/frontend/{conftest,swift,daemon_harness}.py`, `tests/e2e/{conftest,test_conversations}.py`, `subfleet/conversations/peers.py`, and `subfleet/review_publish.py`. These were read-only. A few `rg`-chained reads exited 1 for no matches; follow-up `sed` reads supplied the relevant content. No listed write-shaped command was executed by those inspections.

## Refined safety conclusions

The detailed recoverable audit is `skipped23-audit.json`: **23 reviewed files; three safe whole-file restores, one conditional real-provider file, 19 justified whole-file exclusions**. `importers-restore-three.txt` lists the restores; `importers-safe-mixed-nodes.txt` lists 16 safe test-function selectors in otherwise mixed unsafe files (static review only).

Restore:

- `tests/unit/test_host_push_cli.py`: only fake socket daemon submissions, no Git process or `worlds` fixture.
- `tests/unit/test_host_push_quarantine_restart.py`: only temporary Daemon constructors and fixture intake cleanup with synthetic process identity readers; no repository push.
- `tests/fake/test_review_launch_permissions.py`: `protected()` constructs a launch; hook self-test sends command text as JSON data to the hook executable; no gh command is invoked. Host-publish cases refuse before intake.

Other host-push files use `worlds()`, whose fixture setup itself executes local `git push`, so full-file execution conflicts with the explicit prohibition even when the tested operation would refuse later. `test_gate_merge.py` and `test_gate_service.py` execute `gh pr merge` through fake runners, also explicitly prohibited.

`tests/process/test_provider_shell_environment.py` does not contain a listed forbidden command: it supplies synthetic HOME/config, localhost mock APIs and dummy auth to real CLIs. Its exclusion follows the coordinator’s real-provider limit, not an explicit user ban naming that file. The executable is resolved through the inherited test PATH, so its actual wrapper/config access was not established by static review; a controlled executable path is needed before recommending it unconditionally.

Real process-census files and `e2e/test_conversations.py` remain excluded: the latter keeps `peers.process_chain`, which enumerates the host process table and reads the real peer’s ancestor environments, despite synthetic providers and HOME.

`tests/fake/test_gate_end_to_end.py` is safe to include: all gates are plan gates, with no merge/push/gh path and empty actions assertions. `tests/unit/test_gate_admission.py` is also safe: a temporary Store and stubbed launch/inspection boundaries, with no publication action. Earlier category-based exclusions were corrected.

## Frontend assessment and proposed scratch setup

All seven direct frontend importers are safe fixture tests: core client, live steer, outbox, protocol, steer, suggestions, and disk ruling. They use temporary stores/sockets, override `_person`, and feed scripted provider frames; steer runners use a journal relay without a guardian socket/provider process. Swift subprocesses are probes built from repository sources. A static `test -x /usr/bin/xcrun` returned exit 0; compiler/SDK success was not tested here.

`frontend-scratch-patch.py.txt` contains the original suggestion. After reading the current `sf_fix1_safety.py`, `frontend-conditional-plugin.py.txt` contains the exact minimal conditional helper plus its one-line call site for the existing `pytest_configure` hook. It activates only when frontend paths are requested/selected, redirects only the compiled-probe cache through temporary `swift.ROOT` replacement, preserves the real `SOURCES` and caller-provided probe paths, and adds `-module-cache-path` under each worker’s scratch RUN. It restores globals immediately after compilation and avoids duplicate installation. The active safety plugin was left unchanged.

```sh
python3 - <<'PY'
from pathlib import Path
p=Path('/private/tmp/sf-a2-fix1/frontend-conditional-plugin.py.txt')
compile(p.read_text(),str(p),'exec')
print('Suggested plugin code parses; active plugin unchanged.')
PY
```

Exit 0: suggestion parses. No frontend tests or compiler were executed by this subagent. Existing safety-plugin worker scratch cleanup can remove the per-worker module cache; the shared compiled-probe cache remains inside the job scratch for final guarded cleanup.

No folders were created outside this subagent’s assigned workspace or `/private/tmp/sf-a2-fix1`. The two normalizer test runs used the coordinator’s scratch launcher. Cleanup of shared scratch belongs to the coordinator after its other workers finish.

## Prepared extra-importer driver (not executed)

Read `sf_fix1_safety.py`, `run4.py`, `run-importer-batches.py`, and the coordinator’s exact eleven-path `importer-extras.txt`. A guessed `importer-driver.py` read failed because that file does not exist; no action depended on it. Further read-only inspection confirmed the three safe restores described above and the remaining exclusions in `skipped23-audit.json`.

Created `/private/tmp/sf-a2-fix1/run-importer-extras.py` only. It validates that exact eleven-file whitelist, requires manual `--after-batches-stopped` invocation, installs the conditional Swift cache wrapper only at that later invocation, selects only those files through the existing safety plugin, and uses the existing `uv`/`run4.py` runner with `-n 4 --dist worksteal --timeout=900`, the `not real_lsof and not real_cwd` marker filter, and directory arguments `tests/unit tests/fake tests/frontend`. Its fresh basetemp is the coordinator’s permitted neutral base plus `pytest-importer-extras-01`. It shares the existing deadline and signals/reaps only its own child on interruption/deadline. It records results to the existing ledger/batch-results files and a separate extras log.

```sh
python3 - <<'PY'
from pathlib import Path
p=Path('/private/tmp/sf-a2-fix1/run-importer-extras.py')
compile(p.read_text(),str(p),'exec')
print('Prepared extras driver parses; not executed; active plugin unchanged.')
PY
```

Exit 0. This was a syntax check, not a test run or compiler invocation. No current worker/plugin/selection was changed by preparing the driver.

## Prepared safe-node driver for mixed host-push files (not executed)

Read-only follow-up verified all sixteen selectors and their downstream helpers. Metadata’s `checkouts` fixture runs local `git init`, commit, config, worktree and pack-refs operations, with the shared Git helper disabling hooks/fsmonitor; it never calls `worlds` or push. The generated store/completion interleavings use `Rows`/`BusyRows` and `ManualExecutor`, replacing `host_push.prepare_workspace` with a function that only creates a fixture directory and sentinel. Storage-retry cases use a completed Future and synthetic SQLite rows. The six build-budget cases use a queue whose `submit` stores Futures without executing workers, and replace `_prepare_push_workspace` with a lambda. Production preparation/reaper/readiness methods were read to check the paths. None of these selected paths invokes git push, gh, live process census or existing state.

Created only scratch files:

- `run-safe-mixed-importers.py`: validates the exact sixteen-line `importers-safe-mixed-nodes.txt`, writes only the five selected module paths to active-selection when later manually run, and passes all sixteen explicit `module::function` arguments to pytest. It never passes whole-file or directory selectors. Uses uv/run4, `-n 4 --dist worksteal --timeout=900`, the existing marker filter, the shared deadline, and fresh permitted `pytest-importer-mixed-01` basetemp.
- `sf_fix1_mixed.py`: an additional scratch pytest plugin that aborts before fixture setup if collected base node IDs differ from the exact sixteen selectors or if their fixture closure requests `worlds` or `queued_admission_template`.

The driver requires manual `--after-batches-stopped` invocation and only supervises/signals its own child. It contains no real-provider selection and changes no current plugin/selection during preparation.

A Python syntax/AST-only command compiled both scratch files without executing them, checked exact list equality, verified sixteen unique selectors across five unit modules, and confirmed each named function exists. Exit 0: `Prepared mixed driver/plugin parse; exact16 selectors exist in5 unit modules; no execution.` No tests, model binaries or processes were started by this preparation.

## Prepared evidence copier (not executed)

The coordinator requested a small recoverable evidence bundle before scratch cleanup. Listed only top-level scratch filenames/sizes and inspected the ledger renderer, table link format, guard source/log schemas, guard proof script, safety audit, installer/remover, and cleanup helper. No databases, caches, archives, provider output, or full test/progress logs were read for copying. One exploratory parse of `cleanup-dryrun.json` failed with JSON extra-data (the file is mixed output); that file is explicitly excluded from the copier.

Prepared `/private/tmp/sf-a2-fix1/copy-evidence.py`. It has an explicit small-file allowlist, validates JSON/JSONL, copies static guard reasons/code locations only, preserves existing edited/unowned destination files, refuses symlinks (including broken links), and never deletes or executes a copied helper. `ledger.jsonl` maps to `test-commands.jsonl`; `ledger-table.md` maps to `commands.md` with Markdown data-directory links made local. Schema/CLI/root/subject action ledgers, selection snapshots, inventory/refinements, guard and test/cleanup helper sources are included. No proof-result JSON currently exists; exact known optional proof-result filenames are recognized if the coordinator saves one before copying. Source and destination SHA-256 values are recorded in `copy-audit.json`; `sha256-manifest.json` covers copied evidence and can later be refreshed after cleanup appends its final journal.

Two syntax-only `compile(...)` checks exited 0, including a review correction to refuse broken destination symlinks. The copier itself was not imported or executed, and no report data directory was created by this subagent.

Exact copy command, ready for the coordinator after final ledger rendering and owned test completion:

```sh
.venv/bin/python /private/tmp/sf-a2-fix1/copy-evidence.py
```

After scratch cleanup, a Python outside the removed workspace `.venv` can refresh only the final evidence hashes:

```sh
python3 docs/reports/2026-11-13-review-subject-fix1-data/copy-evidence.py --manifest-only
```

## Evidence copier optional extension

At the coordinator’s request, extended only the copier’s optional allowlist for the exact foreground command scripts `extras-direct.sh`, `mixed-direct.sh`, `final-direct.sh`; proof files `guard-proof-results-v1.json` and `guard-proof-results-v2.json`; and `guard-v1.py`, `prepare-guard-exclusions.py`, `importer-batch-11-observed-failures.json`. Existing same-name entries were preserved without duplication. Syntax/AST checks succeeded and metadata-only presence checks confirmed all 45 required sources still exist. No copier, test, command script, or guard script was executed; no report files were copied.


Report validation draft preparation (no tests/product/report/guard mutations):
- Read `docs/reports/2026-11-13-review-subject.md` Fix round 1 section, raw ledger rows 1–44, rendered aggregate/table/summary, safety audit, schema-ledger and root/CLI action ledgers, extra/final failure logs, skip audit and guard-skip summaries. One attempted scratch read `schema-actions.md` returned exit 1 because the actual file is `schema-ledger.md`; the corrected read succeeded.
- Parsed JSON/JSONL using standard-library file reads and computed source selection intersections: primary334 files; focused14; extras11; mixed5; final4; primary/focused overlap is only validation. No test launch or host process state was read.
- Created `/private/tmp/sf-a2-fix1/report-validation-draft.md` as a provisional integration draft. It corrects focused counts to451/14, records all44 raw stage rows without duplicate aggregation, retains6+1 environment failures and pending final-refusal rerun, lists safety/commit/scratch/folder-accounting deviations, and requires actual cleanup/head results from root before final integration.

- Refreshed draft from raw rows45–48: CLI330p exit0/11.72s; first callback11p6f exit1/0.58s; corrected callback17p exit0/0.50s; final required4 files447p exit0/19.45s. Pytest-only elapsed values are distinguished from launcher timing. No tests were launched for this read/update.
