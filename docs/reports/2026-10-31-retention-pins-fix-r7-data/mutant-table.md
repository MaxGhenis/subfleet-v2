| Mutant | Named killer | Killed | Same assertion as r6 |
|---|---|---|---|
| r2-post-read-identity-check-dropped | r2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Yes | Yes |
| r2-terminal-legacy-incomplete-dropped | r2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Yes | Yes |
| r3-post-traversal-alias-check-dropped | r3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Yes | Yes |
| r3-census-moved-after-recovery | r3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Yes | Yes |
| r3-write-metadata-check-dropped | P::test_changes[same-size-running] | Yes | Yes |
| r3-final-path-check-dropped | P::test_changes[symlink-info-running] | Yes | Yes |
| r3-terminal-census-live-only | r2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Yes | Yes |
| r4-initial-objects-revalidation-dropped | r4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Yes | Yes |
| r4-initial-objects-recording-dropped | r4::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Yes | Yes |
| r5-commondir-whitespace-stripping-restored | r5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Yes | Yes |
| r5-gitfile-whitespace-stripping-restored | r5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Yes | Yes |
| r5-gitfile-colon-without-space-accepted | W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Yes | Yes |
| r6-empty-common-initial-objects-not-recorded | r6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Yes | Yes |
| r6-pointer-read-failure-evidence-dropped | r6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Yes | Yes |
| r6-parent-common-fallback-restored | r6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Yes | Yes |
| r6-dangling-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Yes | Yes |
| r6-oversized-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Yes | Yes |
| r7-unused-parent-pointer-probing-restored | r7::test_unused_parent_pointer_must_not_globally_pin_trees | Yes | New |
| r7-own-objects-revalidation-dropped | r7::test_named_own_objects_swap_keeps_actual_storage | Yes | New |
| r7-parent-objects-recording-dropped | PH::test_inferred_parent_objects_keeps_its_resolved_storage | Yes | New |
