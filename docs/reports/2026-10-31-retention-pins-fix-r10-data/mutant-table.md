| Mutant | Named killer | Result | Same r9 assertion |
|---|---|---|---|
| r8-alias-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[admin-path] | Killed | Yes |
| r8-pointer-file-location-recording-dropped | tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer[gitfile] | Killed | Yes |
| r8-intermediate-link-walk-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[intermediate-HEAD] | Killed | Yes |
| r8-lexical-dotdot | tests/unit/test_retention_locations.py::test_dotdot_applies_after_the_link_before_it | Killed | Yes |
| r8-link-location-followed | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[gitfile-target-HEAD] | Killed | Yes |
| r8-top-level-links-dropped | tests/unit/test_retention_locations.py::test_required_locations_survive_two_relative_links[git-entry-HEAD] | Killed | Yes |
| r8-parent-hint-scanned | tests/unit/test_retention_locations.py::test_inferred_parent_is_not_scanned | Killed | Yes |
| r8-walk-revalidation-dropped | tests/unit/test_retention_locations.py::test_changed_link_chain_with_the_same_final_target_is_incomplete | Killed | Yes |
| r8-routine-skip-dropped | tests/unit/test_retention_locations.py::test_routine_records_are_exactly_unchanged[worktree] | Killed | Yes |
| r9-text-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[gitfile] | Killed | Yes |
| r9-alternates-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[alternates] | Killed | Yes |
| r9-scan-canonicalization-dropped | tests/fake/test_retention_dependency_review_r9.py::test_firmlink_metadata_location_keeps_the_tree_real_git_reads[objects-pack] | Killed | Yes |
| r9-start-spelling-dropped | tests/unit/test_retention_location_spelling.py::test_routine_alias_keeps_the_exact_record[data-volume-worktree] | Killed | Yes |
| r10-found-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[alternates-self-suffix] | Killed | New |
| r10-entries-filter-dropped | tests/unit/test_retention_location_cycles.py::test_cycle_routes_keep_spelling_and_evidence[scan-self-suffix] | Killed | New |
| r10-check-before-spelling | tests/unit/test_retention_location_cycles.py::test_scan_retargeted_into_cycle_before_spelling_is_incomplete[resolved-target] | Killed | New |
