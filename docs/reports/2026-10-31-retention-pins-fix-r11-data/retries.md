## Archive path-trap rerun

Exactly six initial archive call failures reproduced the documented `/private/tmp` path classification. No product or test assertion was changed. All six pass with `TMPDIR` in the new worktree folder `sf-fix272r11-fixtures`, removed at cleanup.

- `tests/unit/test_retention_archive.py::test_blob_only_in_a_local_branch_is_archived_not_omitted`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[alternates]`
- `tests/unit/test_retention_archive.py::test_borrowed_or_shallow_object_store_is_scratch[shallow]`
- `tests/unit/test_retention_archive.py::test_local_path_remote_is_not_a_remote`
- `tests/unit/test_retention_archive.py::test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit`
- `tests/unit/test_retention_archive.py::test_survey_is_read_only_and_predicts_the_pass`

Rerun pytest start: 2026-10-31T19:27:22.127465+00:00; load 1/5/15: [46.31591796875, 56.158203125, 60.9580078125]; duration: 82.80 seconds. No guard interrupted any property. Both previously unfinished properties now pass: early fences in 443.96 seconds and regenerable output in 165.04 seconds.
