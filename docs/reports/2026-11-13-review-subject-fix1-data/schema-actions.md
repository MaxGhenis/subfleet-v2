Findings 1 and 2 (schema subagent)

Cause and changes:
- Finding 1: both branches used version 11 for different columns, so version-based skipping omitted required columns. Slot 11 now has #307's exact five notice columns and caller_wake_sessions table, and store_schema.sql has its exact notices_caller_wake index. No caller-wake runtime or historical-notice stale mutation was ported. Review fields use slot 12. Migration of either historical version-11 shape rechecks both slots by actual column presence; only newly traversed versions are recorded/audited. Fixture helpers now undo table-creating migrations and indexes whose predicates reference removed columns.
- Finding 2: _migrate committed version rows before executescript, whose implicit transaction boundary prevented rollback after schema failure. One BEGIN IMMEDIATE now covers migration DDL, schema statements, version rows and audit events. sqlite3.complete_statement and execute avoid executescript's implicit commit. Version/audit rows are recorded after the schema succeeds.

New tests:
- test_store_upgrade_slots.py::test_real_historical_store_upgrades_to_12_and_reopens_unchanged: three archived historical Store outputs (release/217 a4aded8859fca77678f59e10261ad701160aefb8; #307 02ae7d1eda99bbfc7a44bc85b98218cc3a960219; old #314 5b06c1826bc0641f65b1293197a505b8c8a4843a), each upgraded and opened twice; checks every original job/notice value, all review/wake columns, both indexes and table, and unchanged second-open rows.
- test_store_upgrade_slots.py::test_slot_11_migration_and_schema_equal_307: exact migration tuple and normalized sqlite_master DDL equality with original #307; when the remote branch exists locally, compares its current migration AST and schema too. Frozen fixtures keep shallow CI independent of historical git objects.
- test_store_upgrade_slots.py::test_failed_schema_script_rolls_back_upgrade_and_next_open_retries: three archived Store shapes; append a successful CREATE followed by failing SQL once, verify all rows and sqlite_master unchanged, reopen successfully to version 12, reopen unchanged again.

Fixture extraction command (exit 0 after correction): `.venv/bin/python /private/tmp/sf-a2-fix1/make-schema-fixtures.py`; it ran `git archive <each full commit above> subfleet` to tar files under `/private/tmp/sf-a2-fix1/schema-archives`, extracted each, invoked that tree's original Store in an isolated synthetic HOME to create one synthetic job/notice, and dumped SQL to four committed fixtures. The initial unrestricted `git archive <commit>` produced a 210 MB archive with unrelated assets; I interrupted its extraction (exit 130), guarded-deleted schema-archives with an assertion that sf-a2-fix1 was in its path, then reran with package-only paths. Original Store code was not changed.

Every test command used this prefix:
`TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" UV_CACHE_DIR=/private/tmp/sf-a2-fix1/uv-cache uv run --no-sync --with 'pytest-xdist>=3.6' python /private/tmp/sf-a2-fix1/run.py -q -n 0`

| Arguments after prefix | Result |
| --- | --- |
| `tests/unit/test_store_upgrade_slots.py --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-slots-red` | Exit 1: 4 failed, 10.70s; launched before archive extraction completed, so failures were missing fixture files and do not count as valid red evidence. |
| `tests/unit/test_store_review_subject_migration.py::test_review_subject_metadata_is_the_next_nullable_migration --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-slot-pin-red` | Exit 1: 1 failed, 6.74s, because review migration was 11 rather than 12. |
| `tests/unit/test_store_upgrade_slots.py --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-slots-red-ready` | Exit 1: all 4 new cases failed, 15.26s: release and old #314 stayed at 11; #307 failed missing review_subject; migration tuple differed from #307. Valid pre-fix evidence. |
| `tests/unit/test_store_upgrade_slots.py tests/unit/test_store_review_subject_migration.py tests/unit/test_store_migration_2.py tests/unit/test_store_qos_migration.py --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-slots-green` | Exit 1: 46 passed, 3 failed, 155.65s. The three failures were in the new preservation assertion comparing get_job's normalized provider_qos='utility' with raw historical NULL; corrected to compare raw SQL rows. |
| `tests/unit/test_store_upgrade_slots.py --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-slots-green-raw` | Exit 0: 4 passed, 12.90s. |
| `tests/unit/test_store_upgrade_slots.py -k failed_schema --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-atomic-red` | Exit 1: 3 failed, 4 deselected, 13.69s; each SQL failure left migrations/version rows committed. Valid pre-fix evidence for every new atomic test. |
| `tests/unit/test_store_upgrade_slots.py tests/unit/test_store_review_subject_migration.py tests/unit/test_store_migration_2.py tests/unit/test_store_qos_migration.py --basetemp=/private/tmp/sf-a2-fix1/pytest-schema-atomic-green` | Exit 0: 52 passed, 73.73s. All final schema changes, including optional #307 branch parity, included. |

Other commands/results:
- First command `date -u`: Sun Nov 15 23:16:20 UTC 2026 (subagent start; root's first command determines overall deadline).
- Required review files, report, PR body (`gh pr view 314 --repo MaxGhenis/subfleet-v2`), #307 store/schema and #330 store read successfully; targeted sed/rg reads succeeded except one unmatched zsh glob in a discovery rg command (exit 1, no files read).
- `git rev-parse origin/release/217 02ae7d1ed 5b06c1826` returned the three full hashes above.
- `getconf DARWIN_USER_TEMP_DIR`: `/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/`.
- Creating required `$TMPDIR/sf-a2-fix1/schema-archives` failed `Operation not permitted` (exit 1); the follow-on script launch under that absent path failed exit 2. Used root's approved scratch fallback `/private/tmp/sf-a2-fix1` afterward. No directory was created by the denied command.
- Named-file `git add -- subfleet/store.py subfleet/store_schema.sql tests/unit/test_store_migration_2.py tests/unit/test_store_review_subject_migration.py tests/unit/test_store_upgrade_slots.py tests/fixtures/store/review-upgrade/release-217-v10.sql tests/fixtures/store/review-upgrade/review-subject-old-v11.sql tests/fixtures/store/review-upgrade/wake-caller-307-v11.sql tests/fixtures/store/review-upgrade/wake-caller-307-ddl.json` after finding 1: exit 128, denied index.lock.
- Named-file `git add -- subfleet/store.py tests/unit/test_store_upgrade_slots.py` after finding 2: exit 128, denied index.lock.
- `git commit --only -m 'fix(store): make schema upgrades atomic and retryable' -- subfleet/store.py`: exit 128, denied index.lock. No commit made and no history rewritten.
- `git diff --check -- subfleet/store.py subfleet/store_schema.sql tests/unit/test_store_migration_2.py tests/unit/test_store_review_subject_migration.py tests/unit/test_store_upgrade_slots.py`: exit 0 (twice).
- Fixture INSERT rows were checked with rg: only synthetic values and fixed timestamps.

Files changed: subfleet/store.py; subfleet/store_schema.sql; tests/unit/test_store_migration_2.py; tests/unit/test_store_review_subject_migration.py; new tests/unit/test_store_upgrade_slots.py; four new fixtures under tests/fixtures/store/review-upgrade/.

No finding 1/2 is left unfixed. Broader importer tests and final invariant/refusal tests are owned by root and not claimed here. All subagent commands have completed; nothing remains running. All created temporary folders were under the shared scratch fallback. Root owns its final cleanup.

Finding 1 follow-up (requested after initial importer checks): an already-bricked #330 store can have recorded version 12 with either shared slot's columns missing. `_migrate` now reconciles slots 11/12 whenever a writable initialized Store has an existing supported version, including current version 12. `migrated_from`, version rows and schema audit events are still set only for a genuine version advance. Fresh, read-only and initialize=False opens retain their prior behavior. This also lets the hub's A3 merge add its slot-12 field to a store already opened by fixed A2 without renumbering or changing A3 here.

Added two cases of `test_recorded_version_12_repairs_missing_slot_columns_without_new_versions`: use the old A2 and #307 archived fixtures, manually record 12, repair missing wake/review fields and indexes, preserve every original row including version/audit rows, then reopen unchanged. Every new case failed before the follow-up fix.

Both commands use the same serial prefix above, with neutral permitted attempt basetemps:
- `tests/unit/test_store_upgrade_slots.py -k recorded_version_12 --basetemp=/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1/pytest-schema-followup-red`: exit 1, 2 failed/7 deselected, 6.09s. The old A2 shape failed `no such column: wake_state`; the #307 shape failed `no such column: review_subject`.
- `tests/unit/test_store_upgrade_slots.py tests/unit/test_store_review_subject_migration.py tests/unit/test_store_migration_2.py tests/unit/test_store_qos_migration.py --basetemp=/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1/pytest-schema-followup-green`: exit 0, 54 passed, 35.58s.
- `git diff --check -- subfleet/store.py tests/unit/test_store_upgrade_slots.py`: exit 0.
- `git add -- subfleet/store.py tests/unit/test_store_upgrade_slots.py` and `git commit --only -m 'fix(store): reconcile shared slots at the recorded schema version' -- subfleet/store.py`: each exit 128, denied index.lock. No commit made.

Importer follow-up: `test_prune_217.py` had two old schema-fixture assumptions. Its independent TABLES set omitted the new caller_wake_sessions table; its column-corruption setup inspected only index key columns and could not drop notices.wake_state while a partial index predicate referenced it. Added the table to the expected set and inspect sqlite_master.sql for whole column identifiers before removing dependent indexes. No production prune code changed: it validates schema read-only and opens Store with initialize=False, so current-version repair cannot mask missing columns. Root's interrupted importer batch 10 progress captured five failures, including these two named existing tests before the fixture correction: `tests/unit/test_prune_217.py::test_current_schema_has_no_decision_dependents_and_hashes_every_table` and `tests/unit/test_prune_217.py::test_prune_requires_every_migrated_column[notices.wake_state]`. Full failure tracebacks were unavailable because that run was interrupted. The specific causes above are code-inspection findings; root owns the guarded full-file rerun and its result.
- `git add -- tests/unit/test_prune_217.py`: exit 128, denied index.lock.
- `git commit --only -m 'test(store): cover caller wake table and partial index dependencies' -- tests/unit/test_prune_217.py`: exit 128, denied index.lock.
- `git diff --check -- tests/unit/test_prune_217.py`: exit 0.
- No test or cleanup command was run while preparing this fixture correction and the scratch final-cleanup.py script. The cleanup script was parsed with ast.parse only, not executed.
