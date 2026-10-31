| Mutant | Named killer | Result | Same r7 assertion |
|---|---|---|---|
| r2-post-read-identity-check-dropped | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Survived | No — survived |
| r2-terminal-legacy-incomplete-dropped | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Killed | Yes |
| r3-post-traversal-alias-check-dropped | tests/fake/test_retention_dependency_review_r3.py::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Killed | Yes |
| r3-census-moved-after-recovery | tests/fake/test_retention_dependency_review_r3.py::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Killed | Yes |
| r3-write-metadata-check-dropped | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[same-size-running] | Killed | Yes |
| r3-final-path-check-dropped | docs/reports/2026-10-31-retention-pins-fix-r7-data/probes.py::test_changes[symlink-info-running] | Survived | No — survived |
| r3-terminal-census-live-only | tests/fake/test_retention_dependency_review_r2.py::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Killed | Yes |
| r4-initial-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r4.py::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Survived | No — survived |
| r4-initial-objects-recording-dropped | tests/fake/test_retention_dependency_review_r4.py::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Survived | No — survived |
| r5-commondir-whitespace-stripping-restored | tests/fake/test_retention_dependency_review_r5.py::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Killed | Yes |
| r5-gitfile-whitespace-stripping-restored | tests/fake/test_retention_dependency_review_r5.py::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Killed | Yes |
| r5-gitfile-colon-without-space-accepted | tests/unit/test_retention_pointer_whitespace.py::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Killed | Yes |
| r6-empty-common-initial-objects-not-recorded | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Survived | No — survived |
| r6-pointer-read-failure-evidence-dropped | tests/fake/test_retention_dependency_review_r6.py::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Killed | Yes |
| r6-parent-common-fallback-restored | tests/fake/test_retention_dependency_review_r6.py::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Survived | No — survived |
| r6-dangling-pointer-treated-as-missing | tests/fake/test_retention_pointer_failures.py::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Killed | Yes |
| r6-oversized-pointer-treated-as-missing | tests/fake/test_retention_pointer_failures.py::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Killed | Yes |
| r7-unused-parent-pointer-probing-restored | tests/fake/test_retention_dependency_review_r7.py::test_unused_parent_pointer_must_not_globally_pin_trees | Killed | Yes |
| r7-own-objects-revalidation-dropped | tests/fake/test_retention_dependency_review_r7.py::test_named_own_objects_swap_keeps_actual_storage | Survived | No — survived |
| r7-parent-objects-recording-dropped | tests/fake/test_retention_parent_hint.py::test_inferred_parent_objects_keeps_its_resolved_storage | Killed | Yes |
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | Killed | New |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | Killed | New |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | Killed | New |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | Killed | New |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | Killed | New |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | Killed | New |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | Killed | New |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | Killed | New |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | Killed | New |
