| Mutant | Named killer | Result |
|---|---|---|
| r2-post-read-identity-check-dropped | Rr2::test_c8_4_changed_alternates_do_not_retire_the_tree_git_now_reads[replace] | Killed |
| r2-terminal-legacy-incomplete-dropped | Rr2::test_c8_4_legacy_incomplete_census_survives_terminal_export[database] | Killed |
| r3-post-traversal-alias-check-dropped | Rr3::test_c8_4_git_parent_symlink_swap_during_discovery_keeps_allocated_tree[live] | Killed |
| r3-census-moved-after-recovery | Rr3::test_c8_4_recovery_discovers_incomplete_before_quarantine[live-legacy] | Killed |
| r3-write-metadata-check-dropped | probes::test_changes[same-size-running] | Killed |
| r3-final-path-check-dropped | probes::test_changes[symlink-info-running] | Killed |
| r3-terminal-census-live-only | Rr2::test_c8_4_legacy_incomplete_census_survives_terminal_export[already-exporting] | Killed |
| r4-initial-objects-revalidation-dropped | Rr4::test_objects_parent_swap_keeps_every_allocated_tree[same-inode-live] | Killed |
| r4-initial-objects-recording-dropped | Rr4::test_stable_objects_alias_keeps_the_tree_git_reads[live] | Killed |
| r5-commondir-whitespace-stripping-restored | Rr5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-commondir-trailing] | Killed |
| r5-gitfile-whitespace-stripping-restored | Rr5::test_pointer_path_whitespace_keeps_the_storage_git_reads[live-gitfile-leading] | Killed |
| r5-gitfile-colon-without-space-accepted | W::test_pointer_syntax_and_whitespace_match_real_git[gitfile-no-space] | Killed |
| r6-empty-common-initial-objects-not-recorded | Rr6::test_empty_common_target_keeps_the_own_store_git_reads[directory-live-control-lf] | Killed |
| r6-pointer-read-failure-evidence-dropped | Rr6::test_crlf_padded_pointer_keeps_the_storage_real_git_reads[padded-gitfile] | Killed |
| r6-parent-common-fallback-restored | Rr6::test_empty_common_target_keeps_the_own_store_git_reads[named-live-lf] | Killed |
| r6-dangling-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[dangling] | Killed |
| r6-oversized-pointer-treated-as-missing | PF::test_unreadable_common_pointer_keeps_allocated_trees[oversized] | Killed |
