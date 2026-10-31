| Mutant | Named killer | Result | Same assertion at r11 / r10 / r9 / r8 |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | killed | yes/yes/yes/yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | killed | yes/yes/yes/yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | killed | yes/yes/yes/yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | killed | yes/yes/yes/yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | killed | yes/yes/yes/yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | killed | yes/yes/yes/yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | killed | yes/yes/yes/yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | killed | yes/yes/yes/yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | killed | yes/yes/yes/yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | killed | yes/yes/yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | killed | yes/yes/yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | killed | yes/yes/yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | killed | yes/yes/yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | killed | yes/yes |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | killed | yes/yes |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | killed | yes/yes |
| r11-stat-gate-dropped | tests/fake/test_retention_dependency_review_r11.py::test_return_filter_keeps_readable_store_across_case_rename[alternates] | killed | yes |
| r11-any-oserror-drops | tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENOENT] | killed | NO |
| r12-enametoolong-drops | tests/fake/test_retention_dependency_review_r12.py::test_filter_keeps_readable_normalization_length[alternates]; tests/unit/test_retention_location_readable.py::test_filter_stat_table[found-ENAMETOOLONG] | killed | new |
