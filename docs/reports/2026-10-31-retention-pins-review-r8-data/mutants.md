| Mutant | Named killer | Result |
|---|---|---|
| r2-post-read-identity-check-dropped | `R2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace]` | Killed; same r6 assertion |
| r2-terminal-legacy-incomplete-dropped | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database]` | Killed; same r6 assertion |
| r3-post-traversal-alias-check-dropped | `R3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live]` | Killed; same r6 assertion |
| r3-census-moved-after-recovery | `R3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy]` | Killed; same r6 assertion |
| r3-write-metadata-check-dropped | `P::test_changes[same-size-running]` | Killed; same r6 assertion |
| r3-final-path-check-dropped | `P::test_changes[symlink-info-running]` | Killed; same r6 assertion |
| r3-terminal-census-live-only | `R2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting]` | Killed; same r6 assertion |
| r4-initial-objects-revalidation-dropped | `R4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live]` | Killed; same r6 assertion |
| r4-initial-objects-recording-dropped | `R4::test_stable_objects_alias_keeps_the_tree_git_reads[live]` | Killed; same r6 assertion |
| r5-commondir-whitespace-stripping-restored | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing]` | Killed; same r6 assertion |
| r5-gitfile-whitespace-stripping-restored | `R5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading]` | Killed; same r6 assertion |
| r5-gitfile-colon-without-space-accepted | `W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space]` | Killed; same r6 assertion |
| r6-empty-common-initial-objects-not-recorded | `R6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf]` | Killed; same r6 assertion |
| r6-pointer-read-failure-evidence-dropped | `R6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile]` | Killed; same r6 assertion |
| r6-parent-common-fallback-restored | `R6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf]` | Killed; same r6 assertion |
| r6-dangling-pointer-treated-as-missing | `PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling]` | Killed; same r6 assertion |
| r6-oversized-pointer-treated-as-missing | `PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized]` | Killed; same r6 assertion |
| r7-unused-parent-pointer-probing-restored | `R7::test_unused_parent_pointer_must_not_globally_pin_trees` | Killed |
| r7-own-objects-revalidation-dropped | `R7::test_named_own_objects_swap_keeps_actual_storage` | Killed |
| r7-parent-objects-recording-dropped | `PH::test_inferred_parent_objects_keeps_its_resolved_storage` | Killed |
| r8-final-pointer-content-check-dropped | `R8P::test_final_pointer_failure_cannot_hide_changed_storage[rewrite]` | Killed |
| r8-alias-location-recording-dropped | `R8::test_running_job_keeps_tree_containing_required_git_pointer[admin-path]` | Killed on positive trial baseline |
| r8-pointer-file-location-recording-dropped | `R8::test_running_job_keeps_tree_containing_required_git_pointer[gitfile]` | Killed on positive trial baseline |
