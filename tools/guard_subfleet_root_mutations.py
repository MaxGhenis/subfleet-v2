"""Mutants of D-LANE-SCOPE1's guard edit, each to be killed by a named test.

    python tools/guard_subfleet_root_mutations.py --scratch <dir containing /sf->

Each mutant is applied to a fresh `git archive` of HEAD under the scratch
directory (never to this checkout), then its named test in
tests/unit/test_guard_subfleet_root.py runs there. A kill is a failed test
assertion: an error, a skip or a pass is not. The unmutated control must pass
every named test first. Prints one JSON row per mutant; exit 0 only when the
control passes and every mutant is killed. Exports are removed afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/unit/test_guard_subfleet_root.py"
HOOK = "private/guard/never-rules-hook.sh"
TRUST = "private/guard/TRUST"
DOC = "docs/private-guard-overlay.md"
#: The original reviewed pair's commit (release/217 at abe117e67). Its TRUST still pins codex-cli 0.153.3,
#: behind the installed TRUST (0.159.0) that this revision's TRUST is built on.
BASE = "abe117e676b042b0cc8b4208dc25da52820221e2"
NEW_ROOTS = "|\\.subfleet(/[^/[:space:]'\\\"]+)?"
#: r2 (the hub's review r1, P2-3): $SUBFLEET_ROOT / $SUBFLEET_HOME as spellings of ~/.subfleet, and the
#: ${…} normalization on both `judged` lines.
ENV_ROOTS = "|\\\\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\\\"]+)?"
ENV_SED = " s/\\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g'"
OLD_HOOK_SHA256 = "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db"
NEW_HOOK_SHA256 = "c42f05ef3b4513ec4972192e5bceeaf298df7fc523db532c6057fb7819fd865f"
ROOT_CORE = 'root_core="/|/\\\\*|/Users/?|(${broad_re})/?"'

# name -> (file, old text, new text, the named test that must kill it)
MUTATIONS = {
    # r1's root edits: the path-word pass now judges every resolvable word against snapshot-5's roots, so
    # these mutants show only on a word the text rules judge alone (one still holding $ after resolving).
    # An edit outside the r3 blocks makes the ``hooks`` fixture error (its r2 rebuild no longer hashes to
    # R2_HOOK_SHA256), and an error is no kill, so each is named by a test that reads HOOK directly.
    "new-roots-removed": (HOOK, NEW_ROOTS, "", "test_each_direct_child_is_a_root"),
    "child-form-dropped": (HOOK, NEW_ROOTS, "|\\.subfleet", "test_each_direct_child_is_a_root"),
    "prefix-match": (HOOK, NEW_ROOTS, "|\\.subfleet(/[^[:space:]'\\\"]+)?",
                     "test_near_misses_patterns_prose_and_bounded_searches_stay_allowed"),
    # The roots moved to their own root_core alternative, without the shared `/?`.
    "trailing-slash-missed": (HOOK, None, None, "test_trailing_slash_spellings_are_denied"),
    "trust-hash-stale": (TRUST, None, None, "test_trust_pins_the_new_hook_bytes"),
    # The hub review's finding: a TRUST built on the repository's stale copy rolls the lane pin back.
    "trust-built-on-repo-copy": (TRUST, None, None, "test_trust_keeps_the_installed_lane_pin"),
    # r2: the env spellings dropped (their broad_re alternative and their ${…} normalization).
    "env-spellings-dropped": (HOOK, None, None, "test_every_env_spelling_is_a_root_one_level_down_and_no_deeper"),
    # r3 (decisions 1-3), each as EDITS rewrites r3b's code.
    "file-exception-removed": (HOOK, None, None, "test_r3_a_file_is_never_a_broad_root"),
    "not-dir-dropped": (HOOK, None, None, "test_r3_a_directory_a_link_to_one_and_a_name_stay_roots"),
    "exists-dropped": (HOOK, None, None, "test_r3_a_directory_a_link_to_one_and_a_name_stay_roots"),
    "env-value-used": (HOOK, None, None, "test_r3_env_values_are_never_read"),
    "glob-as-single-file": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "dot-rule-dropped": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "nomatch-judged-by-text": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "case-insensitive": (HOOK, None, None, "test_r3_selective_globs_are_judged_by_what_they_match"),
    "slash-star-left-as-text": (HOOK, None, None, "test_r3_a_bare_star_after_a_root_is_that_root"),
    "state-not-restored": (HOOK, None, None, "test_r3_glob_expansion_restores_every_setting"),
    # r3b (decisions 1-4): the brief's eleven.
    "stall-mount-listed": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    "volumes-not-listed": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "plain-name-unchecked": (HOOK, None, None, "test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed"),
    "volumes-name-stated": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    "usr-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "case-sensitive-roots": (HOOK, None, None, "test_r3b_root_table"),
    "alias-folding-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "holds-root-fixed-list": (HOOK, None, None, "test_r3b_listable_folders_are_computed"),
    "non-last-keeps-all": (HOOK, None, None, "test_r3b_scoped_paths_no_match_and_mount_contents_stay_allowed"),
    "written-link-dropped": (HOOK, None, None, "test_r3b_the_new_roots_aliases_case_links_and_globs_block"),
    "file-test-on-stall": (HOOK, None, None, "test_r3b_nothing_inside_a_mount_is_read"),
    # r3c (the hub's decisions on review r2): the brief's seven.
    "r3c-wild-only-dropped": (HOOK, None, None, "test_r3c_globs_that_match_every_entry_are_their_folder"),
    "r3c-subchild-dropped": (HOOK, None, None, "test_r3c_globs_that_match_every_entry_are_their_folder"),
    "r3c-volumes-mapping-dropped": (HOOK, None, None, "test_r3c_volumes_entries_are_judged_as_the_rest_of_the_path"),
    "r3c-braces-dropped": (HOOK, None, None, "test_r3c_braces_and_alternation_are_judged_by_their_alternatives"),
    "r3c-physical-dots-dropped": (HOOK, None, None, "test_r3c_dotdot_after_a_link_is_physical"),
    "r3c-basename-dropped": (HOOK, None, None, "test_r3c_searchers_are_found_by_basename"),
    "r3c-unescape-dropped": (HOOK, None, None, "test_r3c_the_other_escapes_are_closed"),
    # review r3's five (its h9), retargeted to the r4 code
    "r3c-wrappers-env-only": (HOOK, None, None, "test_r4_searchers_after_keywords_wrappers_and_quotes_are_found"),
    "r3c-unbalanced-brace-allows": (HOOK, None, None, "test_r4_brace_groups_that_span_a_slash_or_climb_fail_closed"),
    "r3c-physical-stall-guard-dropped": (HOOK, None, None, "test_r3c_nothing_inside_a_mount_is_read"),
    "r3c-in-mount-glob-lists": (HOOK, None, None, "test_r3c_nothing_inside_a_mount_is_read"),
    "r3c-eval-unwrap-dropped": (HOOK, None, None, "test_r4_every_shell_and_eval_script_is_judged"),
    # r4 (the hub's decisions on review r3), one per decision
    "r4-fold-dropped": (HOOK, None, None, "test_r4_dotdot_after_a_glob_is_folded"),
    "r4-tree-loop-skipped": (HOOK, None, None, "test_r4_a_tree_glob_after_a_wildcard_component_is_its_folder"),
    "r4-scripts-dropped": (HOOK, None, None, "test_r4_every_shell_and_eval_script_is_judged"),
    "r4-keywords-dropped": (HOOK, None, None, "test_r4_searchers_after_keywords_wrappers_and_quotes_are_found"),
    "r4-starred-slash-allowed": (HOOK, None, None, "test_r4_brace_groups_that_span_a_slash_or_climb_fail_closed"),
    "r4-cap-dropped": (HOOK, None, None, "test_r4_hostile_words_fail_closed_fast"),
    "r4-device-is-stdin": (HOOK, None, None, "test_r4_a_device_or_a_closed_stdin_is_no_pipe"),
    "r4-meta-walks": (HOOK, None, None, "test_r4_print_and_exit_forms_walk_nothing"),
    "r4-tilde-user-dropped": (HOOK, None, None, "test_r4_the_unlisted_forms_are_judged"),
    # r5 (the hub's decisions on review r4), one per decision
    "r5-base-quadratic": (HOOK, None, None, "test_r5_a_long_word_without_a_slash_costs_what_the_installed_hook_does"),
    "r5-heredoc-unread": (HOOK, None, None, "test_r5_heredoc_bodies_are_data"),
    "r5-too-long-allowed": (HOOK, None, None, "test_r5_a_command_too_long_to_prep_fails_closed"),
    "r5-pipe-stages-dropped": (HOOK, None, None, "test_r5_the_unjudged_forms_are_judged"),
    "r5-bound-hides-producer": (HOOK, None, None, "test_r5_a_depth_bound_covers_only_its_own_walk"),
    "r5-backtick-space-dropped": (HOOK, None, None, "test_r5_a_closing_backtick_ends_a_root"),
    "r5-dq-substitution-unread": (HOOK, None, None, "test_r5_a_closing_backtick_ends_a_root"),
    "r5-alias-unfolded": (HOOK, None, None, "test_r3c_nothing_inside_a_mount_is_read"),
    "r5-brace-root-unread": (HOOK, None, None, "test_r5_a_root_spelling_before_a_brace_is_a_path_word"),
    "r5-voldots-glob-dropped": (HOOK, None, None, "test_r5_the_wider_user_and_volume_family"),
    # r5 decision 4: the overlay doc's body twice again (a second copy of a section)
    "r5-doc-duplicated": (DOC, "\n## Verification\n",
                          "\n## First installation over the existing private release\n\n## Verification\n",
                          "test_r5_the_overlay_doc_holds_one_copy_of_its_body"),
    # review r4's four (rv4-*), each killed by the test its review commit added (cb63c20a1)
    "rv4-otherfd-is-stdin": (HOOK, None, None, "test_review_r4_pins_r4s_unpinned_readings"),
    "rv4-grep-meta-walks": (HOOK, None, None, "test_review_r4_pins_r4s_unpinned_readings"),
    "rv4-backtick-space-dropped": (HOOK, None, None, "test_review_r4_pins_r4s_unpinned_readings"),
    "rv4-function-keyword-dropped": (HOOK, None, None, "test_review_r4_pins_r4s_unpinned_readings"),
    "r5-dotdot-parent-dropped": (HOOK, None, None, "test_r5_a_component_bash_can_match_against_dotdot_names_its_parent"),
    # review r5's two probe mutants, each killed by the test its review commit added (9487a0f59)
    "rv5-execdir-dropped": (HOOK, None, None, "test_review_r5_pins_r5s_unpinned_readings"),
    "rv5-wrapper-equals-dropped": (HOOK, None, None, "test_review_r5_pins_r5s_unpinned_readings"),
    # r6 (the hub's decisions on review r5), one per decision 1-9 (two for 9: substitutions and loops), and the
    # step budget decisions 4 and 5 share
    "r6-body-unread": (HOOK, None, None, "test_r6_an_unquoted_heredoc_bodys_substitutions_are_judged"),
    "r6-xargs-shell-unread": (HOOK, None, None, "test_r6_xargs_into_a_shell_whose_script_names_a_searcher"),
    "r6-filter-heredoc-dropped": (HOOK, None, None, "test_r6_any_producers_heredoc_piped_into_a_shell_is_its_script"),
    "r6-arith-unread": (HOOK, None, None, "test_r6_an_arithmetic_shift_starts_no_heredoc"),
    "r6-unterminated-disables-rest": (HOOK, None, None, "test_r6_an_arithmetic_shift_starts_no_heredoc"),
    "r6-dq-parend-plain": (HOOK, None, None, "test_r6_a_substitution_in_double_quotes_reads_its_heredoc"),
    "r6-equals-backtick-dropped": (HOOK, None, None, "test_r6_zsh_equals_cmd_after_a_backtick_is_read"),
    # decision 7 keeps r5's closing backtick in root_end (a fenced line, so the fence fails with this one too)
    "r6-span-backtick-dropped": (HOOK, None, None,
                                 "test_r6_a_markdown_code_span_in_a_quoted_body_is_an_intended_over_block"),
    "r6-cap-dropped": (HOOK, None, None, "test_r6_hostile_words_fail_closed_fast"),
    "r6-psub-unread": (HOOK, None, None, "test_r6_process_substitutions_and_loops_are_judged"),
    "r6-loop-words-dropped": (HOOK, None, None, "test_r6_process_substitutions_and_loops_are_judged"),
    "r6-budget-dropped": (HOOK, None, None, "test_r6_dense_substitutions_fail_closed_fast"),
    # r7 (the hub's decisions on review r6), one per decision 1-5 (two for 5: the reading and where it lasts), and
    # review r6's probe mutant (its edit rebuilt on r7's lines: the filter and cat pass-on of a <(...) dropped)
    "r7-arith-plain": (HOOK, None, None, "test_r7_an_arithmetic_shift_crosses_lines"),
    "r7-scan-uncounted": (HOOK, None, None, "test_r7_heredoc_bodies_count_against_the_step_budget"),
    "r7-passon-dropped": (HOOK, None, None, "test_r7_a_filter_passes_on_its_process_substitution"),
    "rv6-psub-passon-dropped": (HOOK, None, None, "test_r7_a_filter_passes_on_its_process_substitution"),
    "r7-known-form-unlisted": (DOC, None, None, "test_r7_known_differences_name_the_open_forms"),
    "r7-loopvar-unscoped": (HOOK, None, None, "test_r7_a_loop_variable_reassigned_after_its_loop_is_read_as_assigned"),
    "r7-loopvar-anywhere": (HOOK, None, None, "test_r7_a_loop_variable_reassigned_after_its_loop_is_read_as_assigned"),
    # review r7's probe mutant: the loop variable's reset no longer stops inside a backtick (killed at c83be9c89)
    "rv7-bto-dropped": (HOOK, None, None, "test_review_r7_pins_r7s_unpinned_readings"),
    # r8 (the hub's decisions on review r7): one per bullet of decision 1, the round's deviation in it (a value that is
    # not a plain word joins the loop's words), zsh's repeat, and one per decision 2-4; decisions 5 and 6 are doc pins
    "r8-continuation-dropped": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-time-keyword-dropped": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-function-prescan-dropped": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-plain-word-dropped": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-value-joins-dropped": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-repeat-uncounted": (HOOK, None, None, "test_r8_a_loop_variable_keeps_its_root_where_the_shells_keep_it"),
    "r8-cap-quotes-dropped": (HOOK, None, None, "test_r8_quotes_and_backticks_count_against_the_cap"),
    "r8-dollar-arith-dropped": (HOOK, None, None,
                                "test_r8_a_dollar_double_paren_span_ending_in_two_closes_is_arithmetic"),
    "r8-passon-roots-dropped": (HOOK, None, None, "test_r8_a_piped_while_read_takes_the_roots_a_filter_passes_on"),
    "r8-known-form-unlisted": (DOC, None, None, "test_r8_known_differences_name_the_stricter_and_open_forms"),
    # review r8's probe mutants: each passes every r4-r8 list and S1 (a continuation only at the top of the command,
    # time's keywords without {, and the cap one past 4,096 for ' and backtick)
    "rv8-continuation-top-only": (HOOK, None, None, "test_review_r8_pins_r8s_unpinned_readings"),
    "rv8-time-brace-dropped": (HOOK, None, None, "test_review_r8_pins_r8s_unpinned_readings"),
    "rv8-cap-edge-moved": (HOOK, None, None, "test_review_r8_pins_r8s_unpinned_readings"),
    # r9 (the hub's decisions on review r8): one per fix, the join where the reset is off and the count of "
    "r9-join-dropped": (HOOK, None, None, "test_r9_a_later_value_joins_the_loop_words_where_the_reset_does_not_apply"),
    "r9-cap-dquote-dropped": (HOOK, None, None, "test_r9_double_quotes_count_against_the_cap"),
    # r10 (the hub's decisions on review r9): one per fix, the depth gate restored and the function body's join dropped,
    # and review r9's probe mutant (the deny message drops ", which review r9's committed test reads)
    "r10-depth-gate-restored": (HOOK, None, None, "test_r10_a_value_joins_inside_a_substitution_or_a_nested_script"),
    "r10-function-body-join-dropped": (HOOK, None, None, "test_r10_a_function_body_written_before_its_loop_joins"),
    "rv9-message-dquote-dropped": (HOOK, None, None, "test_review_r9_pins_r9s_unpinned_readings"),
}
#: r3's and r3b's mutants: (old text, new text) pairs, each old text exactly once in the hook.
LIST_GUARD = '      elif pw_holds "$p"; then\n'
RESTORE = """  if [ -n "$gi_set" ]; then GLOBIGNORE=$gi; fi
  if [ -n "$n" ]; then shopt -s nullglob; else shopt -u nullglob; fi
  if [ -n "$d" ]; then shopt -s dotglob; else shopt -u dotglob; fi
  if [ -n "$c" ]; then shopt -s nocaseglob; else shopt -u nocaseglob; fi
  if [ -n "$e" ]; then shopt -s extglob; else shopt -u extglob; fi
  if [ -n "$g" ]; then shopt -s failglob; else shopt -u failglob; fi
  if [ -n "$f" ]; then set -f; fi
"""
HOLDS = """pw_holds() {
  local n=""
  if pw_stall "$1" && ! pw_volumes "$1"; then return 1; fi
  for n in TheAxiomFoundation RulesFoundation PolicyEngine Library Caches .cache .local .subfleet \\
           Users Volumes System private tmp var folders opt homebrew Applications usr Data @sf-any@; do
    pw_normalize "$1/$n"
    pw_isbroad "$pw_n" && return 0
  done
  return 1
}
"""
#: r3c: pw_alt's trailing rule (1(b): a component made only of wildcards, or .* or **, is its folder).
WILD_LINE = "        if [ \"$c\" = '.*' ] || [ \"$c\" = '**' ] || pw_wild \"$c\"; then b=${b%/*}; else break; fi ;;\n"
EDITS = {
    "file-exception-removed": [('    pw_isfile "$1" && return 1\n    return 0\n', "    return 0\n")],
    "not-dir-dropped": [('pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }',
                         'pw_nondir() { [ -e "$pw_fs$1" ]; }')],
    "exists-dropped": [('pw_nondir() { [ -e "$pw_fs$1" ] && [ ! -d "$pw_fs$1" ]; }',
                        'pw_nondir() { [ ! -d "$pw_fs$1" ]; }')],
    "env-value-used": [("""    '$SUBFLEET_ROOT'|'$SUBFLEET_HOME') pw_p="$HOME/.subfleet" ;;
    '$SUBFLEET_ROOT/'*|'$SUBFLEET_HOME/'*) pw_p="$HOME/.subfleet/${1#*/}" ;;
""", """    '$SUBFLEET_ROOT') pw_p=${SUBFLEET_ROOT:-$HOME/.subfleet} ;;
    '$SUBFLEET_HOME') pw_p=${SUBFLEET_HOME:-$HOME/.subfleet} ;;
    '$SUBFLEET_ROOT/'*) pw_p="${SUBFLEET_ROOT:-$HOME/.subfleet}/${1#*/}" ;;
    '$SUBFLEET_HOME/'*) pw_p="${SUBFLEET_HOME:-$HOME/.subfleet}/${1#*/}" ;;
""")],
    "glob-as-single-file": [("    '/*') return 0 ;;\n    *[*?[]*) ;;\n", "    '/*') return 0 ;;\n")],
    "dot-rule-dropped": [('    case "$x" in .*) [ -n "$dot" ] || continue ;; esac\n', "")],
    "nomatch-judged-by-text": [('  if pw_globbroad "$pw_p" "$f"; then pw_av=G; else pw_av=NG; fi',
                                '  if pw_globbroad "$pw_p" "$f"; then pw_av=G; fi')],
    "case-insensitive": [("  shopt -u nocasematch extglob\n", "  shopt -s nocasematch; shopt -u extglob\n")],
    # r3c: the trailing run is now read in pw_alt's loop (r3's bare * among its forms)
    "slash-star-left-as-text": [(WILD_LINE, "          break ;;\n")],
    "state-not-restored": [(RESTORE, "")],
    "stall-mount-listed": [(LIST_GUARD, '      elif pw_holds "$p" || pw_stall "$p"; then\n')],
    "volumes-not-listed": [(LIST_GUARD, '      elif ! pw_stall "$p" && pw_holds "$p"; then\n')],
    "plain-name-unchecked": [('        if pw_lexists "$pw_n"; then np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=u; fi\n',
                              '        np[${#np[@]}]=$pw_n; nl[${#nl[@]}]=u\n')],
    "volumes-name-stated": [('      elif [ -z "$glob" ] && ! pw_stall "$p"; then\n', '      elif [ -z "$glob" ]; then\n')],
    "usr-dropped": [('/Library /Applications /usr"', '/Library /Applications"')],
    "case-sensitive-roots": [('  if [ -n "${pw_hfast-}" ]; then case "$p" in /[Vv]*) ;; /*/*/*/*/*/*) return 1 ;; esac; fi\n  shopt -q nocasematch && nc=1\n  shopt -s nocasematch\n',
                              '  if [ -n "${pw_hfast-}" ]; then case "$p" in /[Vv]*) ;; /*/*/*/*/*/*) return 1 ;; esac; fi\n  shopt -q nocasematch && nc=1\n  shopt -u nocasematch\n')],
    "alias-folding-dropped": [("""  case "$pw_n" in
    /system/volumes/data) pw_n=/ ;;
    /system/volumes/data/*) pw_n=${pw_n:20} ;;
  esac
  case "$pw_n" in
    /private/tmp|/private/tmp/*|/private/var|/private/var/*|/private/etc|/private/etc/*) pw_n=${pw_n:8} ;;
  esac
""", "")],
    # r3's fixed list (pw_listed): /, /Users, /private, the home, its Library and .subfleet, /Users/<any>
    # and its Library and .subfleet.
    "holds-root-fixed-list": [(HOLDS, """pw_holds() {
  case "$1" in /|/Users|/private|"$pw_home"|"$pw_home/Library"|"$pw_home/.subfleet") return 0 ;; esac
  case "$1" in /Users/*/*/*) return 1 ;; /Users/*/Library|/Users/*/.subfleet) return 0 ;; /Users/*/*) return 1 ;; /Users/*) return 0 ;; esac
  return 1
}
""")],
    "non-last-keeps-all": [('        if { [ "$k" -lt "$up" ] || pw_isbroad "${np[$m]}"; } && ! pw_isfile "${np[$m]}"; then', '        if :; then')],
    "written-link-dropped": [("""  pw_physical "$1"
  [ "$pw_r" != "$1" ] && pw_isbroad "$pw_r" && ! pw_isfile "$pw_r"
}
""", "  return 1\n}\n")],
    "file-test-on-stall": [('pw_isfile() { pw_stall "$1" && return 1; pw_nondir "$1"; }', 'pw_isfile() { pw_nondir "$1"; }')],
    # r3c, decision by decision
    "r3c-wild-only-dropped": [(WILD_LINE, WILD_LINE.replace('|| pw_wild "$c"', "|| [ \"$c\" = '*' ]"))],
    "r3c-subchild-dropped": [('      if [ -n "$glob" ] && pw_subchild "$p" && ! pw_isfile "$p"; then return 0; fi\n', "")],
    "r3c-volumes-mapping-dropped": [('  while [[ $p == /volumes/*/* ]]; do rest=${p:9}; pw_normalize "/${rest#*/}"; p=$pw_n; done\n', "")],
    "r3c-braces-dropped": [('  pw_gs=(); pw_ge=(); pw_gk=(); pw_gn=(); pw_gx=()\n  while [ "$k" -lt "$n" ]; do\n',
                            '  pw_gs=(); pw_ge=(); pw_gk=(); pw_gn=(); pw_gx=()\n  return 0\n  while [ "$k" -lt "$n" ]; do\n')],
    "r3c-physical-dots-dropped": [('  local rest="$1/" c="" path=""\n  pw_pd=$1\n', '  local rest="$1/" c="" path=""\n  pw_pd=$1\n  return 0\n')],
    "r3c-basename-dropped": [('    if (t <= nt) { b = base(val[t]); if (isname(b) && tk[t] != canon(b)) { tk[t] = canon(b); val[t] = tk[t] } }\n', "")],
    "r3c-wrappers-env-only": [('    while (t <= nt && (base(val[t]) in WRAP)) {\n', '    while (t <= nt && base(val[t]) == "env") {\n')],
    "r3c-unbalanced-brace-allows": [('    k=$((k + 1))\n  done\n  k=0; d=0\n', '    k=$((k + 1))\n  done\n  [ "$top" -eq 0 ] || exit 0\n  k=0; d=0\n')],
    # retargeted in r5: pw_physical's stall test, and the per-step one decision 7 added beside it
    "r3c-physical-stall-guard-dropped": [('  local path="" rest="" name="" hops=0 d=${1%/*} top=""\n  pw_r=$1\n  pw_mount "$1" && return 0\n',
                                          '  local path="" rest="" name="" hops=0 d=${1%/*} top=""\n  pw_r=$1\n'),
                                         ('      if pw_mount "$path/$name"; then path="$path/$name"; continue; fi\n', '')],
    "r5-alias-unfolded": [("""      /[Ss][Yy][Ss][Tt][Ee][Mm]/[Vv][Oo][Ll][Uu][Mm][Ee][Ss]/[Dd][Aa][Tt][Aa]) p=/ ;;
      /[Ss][Yy][Ss][Tt][Ee][Mm]/[Vv][Oo][Ll][Uu][Mm][Ee][Ss]/[Dd][Aa][Tt][Aa]/*) p=${p:20} ;;
""", "")],
    "r3c-in-mount-glob-lists": [('      if pw_stall "$p" && ! pw_volumes "$p"; then\n', '      if false; then\n')],
    "r3c-eval-unwrap-dropped": [('        if (scr != "") script(scr, ctx, depth)\n', '')],
    "r4-fold-dropped": [('  local -a st\n  pw_fd=$1\n', '  local -a st\n  pw_fd=$1\n  return 0\n')],
    "r4-tree-loop-skipped": [("""    case "/$b" in */'**'/*) b=${b%%/'**'/*} ;; esac
  fi
""", """    case "/$b" in */'**'/*) b=${b%%/'**'/*}; x=walk ;; esac
  fi
"""), ("""  while :; do
    case "$b" in
      /|'') break ;;
""", """  while [ "$x" != walk ]; do
    case "$b" in
      /|'') break ;;
""")],
    "r4-scripts-dropped": [('    if (!noext) {\n', '    if (0) {\n')],
    # retargeted in r7: decision 5 counts the compound commands the keywords open and close (ld, st["ka"])
    "r4-keywords-dropped": [("""      if (!qd[t] && val[t] ~ /^(if|then|else|elif|while|until|do|done|fi|esac|coproc|!|\\{|\\})$/) {
        if (mode == "prep" && !depth) { if (val[t] ~ /^(if|while|until|\\{)$/) ld++; else if (val[t] ~ /^(fi|done|esac|\\})$/ && ld > 0) ld-- }
        st["ka"] = 1; t++; continue
      }
""", "")],
    "r4-starred-slash-allowed": [('        [ -z "${pw_gx[$k]}" ] || pw_fc=1\n', '')],
    "r4-cap-dropped": [("""  [ "${#1}" -le 4096 ] || return 0
  [ "${#1}" -le 1024 ] || pw_bytes "$1" || return 0
""", """  return 1
""")],
    "r4-device-is-stdin": [('          else if (op == "<&") stdin = (tg == "-") ? 0 : 1\n          else stdin = (tg ~ /^\\/dev\\//) ? 0 : 1\n',
                            '          else stdin = 1\n')],
    "r4-meta-walks": [('    if (t <= nt && ismeta(val[t], val, rd, t, nt)) return sp[0] ":" sp[nt]\n', '')],
    # retargeted in r5: decision 10 widened r4's three spellings to one reading
    "r4-tilde-user-dropped": [("""  case "$w" in '~$'*|'~`'*) pw_tilde "$w"; w=$pw_w; q=q ;; esac
""", "")],
    "r5-backtick-space-dropped": [("""      if (sep == "`") { if (bto) r = r " "; bto = !bto }
""", "")],
    "r5-dq-substitution-unread": [("""          if (mode == "prep" && !noext) spans(substr(seg, i + 1, j - i - 1), ctx, depth)
""", "")],
    # retargeted in r6: decision 9 keeps the roots a bounded producer writes too
    # retargeted in r7 (decision 3 keeps lastps beside lastprod)
    "r5-bound-hides-producer": [("""    if (hs && hb) { out = bound(tk, val, ex, isfr, sp, nt); lastprod = prod; lastps = st["lp"]; lastroots = proots(val[t], fw, prod); if (collect) coll = coll "\\n" prod; return out }
""", """    if (hs && hb) return sp[0] ":" sp[nt]
""")],
    "r5-pipe-stages-dropped": [("""      nextin = (sep == "|" || sep == "|&") ? pi (pi == "" ? "" : "\\n") lastprod : ""
""", """      nextin = (sep == "|" || sep == "|&") ? lastprod : ""
""")],
    "r5-too-long-allowed": [("""if pw_named "$command"; then
""", """if [ "${#command}" -le 65536 ] && pw_named "$command"; then
""")],
    "rv4-otherfd-is-stdin": [("""          if (op !~ /^0?(<|<<|<<-|<<<|<&|<>)$/) continue
          sub(/^0/, "", op)
""", """          if (op !~ /^[0-9]*(<|<<|<<-|<<<|<&|<>)$/) continue
          sub(/^[0-9]*/, "", op)
""")],
    "rv4-grep-meta-walks": [("""        if (c == "grep" && v ~ /^--(help|version)$/) return 1
""", ""), ("""          if (c == "grep" && substr(v, j, 1) == "V") return 1
""", "")],
    "rv4-backtick-space-dropped": [("""    if (bt && t == 1 && sp[0] == "" && isname(base(val[1]))) sp[0] = " "
""", "")],
    # retargeted in r7 (decision 5 marks a keyword before the command word)
    "rv4-function-keyword-dropped": [("""      if (!qd[t] && val[t] == "function" && t < nt) { st["ka"] = 1; t += 2; continue }
""", "")],
    "r5-base-quadratic": [("""  function base(v, n, a) { n = split(v, a, "/"); return a[n] }
""", """  function base(v,   x) { x = v; sub(/.*\\//, "", x); return x }
""")],
    # retargeted in r6: decision 4 replaced the !hf test with the arithmetic one; in r7, decision 1 added the $[ test
    "r5-heredoc-unread": [("""      if (c == "<" && substr(t, i + 1, 1) == "<" && (j = hdword(t, i)) && i > abe && !(na && arith(t, i, AO, AD, na, AV))) {
""", """      if (0) {
""")],
    "rv5-execdir-dropped": [("""        if (rd[k] || val[k] !~ /^-(exec|execdir|ok|okdir)$/) continue
""", """        if (rd[k] || val[k] !~ /^-(exec|ok|okdir)$/) continue
""")],
    "rv5-wrapper-equals-dropped": [("""      if (t <= nt && !qd[t] && val[t] ~ /^=[A-Za-z_.\\/]/) val[t] = substr(val[t], 2)
""", "")],
    "r6-body-unread": [("""          if (mode == "prep" && !noext && HDW !~ /[\\\\\\047"]/) { x = HDW; spans(HDB, ctx, depth); HDW = x }
""", "")],
    "r6-xargs-shell-unread": [("""      if (mode == "prep" && st["a"] && st["s"] && st["c"] && st["g"] && pwn(val[st["g"]])) xsh = 1
""", "")],
    "r6-filter-heredoc-dropped": [("""        if (hstr hd != "") prod = prod (prod == "" ? "" : "\\n") hstr (hstr != "" && hd != "" ? "\\n" : "") hd
""", "")],
    # retargeted in r7 (decision 1 added the $[ test before it)
    "r6-arith-unread": [("""(j = hdword(t, i)) && i > abe && !(na && arith(t, i, AO, AD, na, AV))) {
""", """(j = hdword(t, i)) && i > abe) {
""")],
    # a body that never ends turns heredoc reading off for every later line, as r5's hf did
    "r6-unterminated-disables-rest": [("""        if (!x && hn) HZ[HDS, HDD] = 1
""", """        if (!x) hz = 1
""")],
    "r6-dq-parend-plain": [("""      if (c == "\\"") { j = dqhend(t, i); if (!j) j = n; seg = seg substr(t, i, j - i + 1); i = j + 1; continue }
""", """      if (c == "\\"") { j = dqend(t, i); if (!j) j = n; seg = seg substr(t, i, j - i + 1); i = j + 1; continue }
""")],
    "r6-equals-backtick-dropped": [("""    || printf '%s' "$1" | grep -qE "(^|[[:space:];&|(\\`])=(find|rg|[efru]?grep)([[:space:]]|\\$)"
""", """    || printf '%s' "$1" | grep -qE "(^|[[:space:];&|(])=(find|rg|[efru]?grep)([[:space:]]|\\$)"
""")],
    "r6-span-backtick-dropped": [("""  root_end="['\\"]?([[:space:]|;&)\\`]|\\$)"
""", """  root_end="['\\"]?([[:space:]|;&)]|\\$)"
""")],
    "r6-cap-dropped": [("""  [ "$pw_many" = 0 ] || block""", """  [ "$pw_many" = 0 ] || true""")],
    "r6-psub-unread": [("""      if (c == "<" && substr(t, i + 1, 1) == "(" && mode == "prep" && !noext && (j = parend(t, i + 1))) {
""", """      if (0) {
""")],
    "r6-loop-words-dropped": [("""          if (w in LVAR) { tk[k] = LVAR[w]; val[k] = value(tk[k]); qd[k] = (tk[k] ~ /["\\047\\\\]/) }
""", "")],
    # retargeted in r7: the main loop of parend (decision 2 counts body lines with the same line, deeper)
    "r6-budget-dropped": [("""    while (i <= n) {
      if (++pst > 20000) { cplx = 1; return 0 }
""", """    while (i <= n) {
""")],
    "r7-arith-plain": [("""      if (c == "(" && substr(t, i + 1, 1) == "(" && (j = arend(t, i))) { if (!d) return j; i = j + 1; continue }
""", "")],
    "r7-scan-uncounted": [("""            if (++pst > 20000) { cplx = 1; return 0 }
            if (!(i in LE))""", """            if (!(i in LE))"""),
                          ("""{ if (++pst > 20000) { cplx = 1; return 0 }; line = LN[m];""", """{ line = LN[m];""")],
    # retargeted in r8: decision 4 spread the pass-on over lines; the edit still drops the whole else-if
    "r7-passon-dropped": [("""      else if (!oe && ow) {
        if (sep == "|" || sep == "|&") {
          nextin = nextin (nextin == "" ? "" : "\\n") oc
          x = oc; gsub(/[[:space:]]+/, " ", x); sub(/^ /, "", x); sub(/ $/, "", x)
          if (x != "") nextroots = nextroots (nextroots == "" ? "" : " ") x
        }
        oc = ""; ow = 0
      }
""", "")],
    "rv6-psub-passon-dropped": [("""        st["lp"] = (st["ps"] != "")
""", ""), ("""        if (st["ps"] != "") { prod = prod (prod == "" ? "" : "\\n") st["ps"]; st["lp"] = 1 }
""", "")],
    "r7-known-form-unlisted": [("""  - zsh's `=(…)`, `source =(echo 'rg --files ~/.subfleet')` and `zsh =(…)`: zsh writes""",
                                """  - zsh's process substitution to a file: zsh writes""")],
    # retargeted in r8, r9 and r10: each rewrote the assignment line (r10 gave it an else, the function body's value);
    # the edit still drops the update, its else with it
    "r7-loopvar-unscoped": [("""        if ((w in LVAR) && op != "") LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : LVAR[w] " " op
        else if (FN && op != "" && (st["ka"] || ld || !st["vt"])) FA[w] = FA[w] " " op
""", "")],
    "r7-loopvar-anywhere": [("""      vtop = (!depth && !pd && !pn && !bto && (pv == "" || pv == ";" || pv == "\\n" || pv == "&") && (sep == ";" || sep == "\\n" || sep == "&&" || sep == "||"))
""", """      vtop = !depth
""")],
    "rv7-bto-dropped": [("""      vtop = (!depth && !pd && !pn && !bto && (pv == "" || pv == ";" || pv == "\\n" || pv == "&") && (sep == ";" || sep == "\\n" || sep == "&&" || sep == "||"))
""", """      vtop = (!depth && !pd && !pn && (pv == "" || pv == ";" || pv == "\\n" || pv == "&") && (sep == ";" || sep == "\\n" || sep == "&&" || sep == "||"))
"""), ("""    vtop = (!depth && !pd && !pn && !bto && (pv == "" || pv == ";" || pv == "\\n" || pv == "&"))
""", """    vtop = (!depth && !pd && !pn && (pv == "" || pv == ";" || pv == "\\n" || pv == "&"))
""")],
    "r8-continuation-dropped": [("""      if (sep == "\\n" && (pv == "&&" || pv == "||" || pv == "|" || pv == "|&") && seg !~ /[^[:space:]]/) {
""", """      if (0) {
""")],
    "r8-time-keyword-dropped": [("""      if (!qd[t] && val[t] == "time") {
""", """      if (0) {
""")],
    # retargeted in r9: the reset's condition moved to vrp (FN dropped from it, so a plain value replaces again)
    "r8-function-prescan-dropped": [("""vrp = (!st["ka"] && !ld && st["vt"] && !FN)""", """vrp = (!st["ka"] && !ld && st["vt"])""")],
    # retargeted in r9 (decision 1 rewrote the line): a quote, a backslash or a blank counts as plain again
    "r8-plain-word-dropped": [("""LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : LVAR[w] " " op""",
                               """LVAR[w] = (vrp && op !~ /[$`]/) ? op : LVAR[w] " " op""")],
    # retargeted in r9: where the reset applies, a value that is not a plain word leaves the loop's words alone again
    "r8-value-joins-dropped": [("""LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : LVAR[w] " " op""",
                                """LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : vrp ? LVAR[w] : LVAR[w] " " op""")],
    "r8-repeat-uncounted": [("""val[t] ~ /^(for|select|case|repeat|foreach)$/) ld++""", """val[t] ~ /^(for|select|case)$/) ld++""")],
    # retargeted in r9 (decision 2 added f, the count of "): ' and backtick dropped, " kept
    "r8-cap-quotes-dropped": [("""c > 4096 || d > 4096 || e > 4096 || f > 4096) ? 1 : 0""", """c > 4096 || f > 4096) ? 1 : 0""")],
    "r8-dollar-arith-dropped": [("""return (j && (PAI == j - 1 || (substr(t, i - 1, 1) == "$" && substr(t, j - 1, 1) == ")"))) ? j : 0""",
                                 """return (j && PAI == j - 1) ? j : 0""")],
    "r8-passon-roots-dropped": [("""          if (x != "") nextroots = nextroots (nextroots == "" ? "" : " ") x
""", "")],
    "r8-known-form-unlisted": [("""    <(echo ~/.subfleet) | bash` and `paste -d' ' <(echo 'rg --files') <(echo ~/.subfleet) | bash`: the""",
                                """    <(echo ~/.subfleet) | bash` and `paste <(…) <(…) | bash`: the""")],
    "rv8-continuation-top-only": [("""      if (sep == "\\n" && (pv == "&&" || pv == "||" || pv == "|" || pv == "|&") && seg !~ /[^[:space:]]/) {
""", """      if (sep == "\\n" && !depth && (pv == "&&" || pv == "||" || pv == "|" || pv == "|&") && seg !~ /[^[:space:]]/) {
""")],
    "rv8-time-brace-dropped": [("""repeat|foreach|coproc|!|\\{)$/) { st["ka"] = 1; t = k; continue }""",
                                """repeat|foreach|coproc|!)$/) { st["ka"] = 1; t = k; continue }""")],
    # retargeted in r9 (decision 2 added f): the edges of ' and backtick moved, that of " kept
    "rv8-cap-edge-moved": [("""c > 4096 || d > 4096 || e > 4096 || f > 4096) ? 1 : 0""",
                            """c > 4096 || d > 4097 || e > 4097 || f > 4096) ? 1 : 0""")],
    # r9 decision 1: where the reset is off, the value is dropped again (r8's reading: the loop's words are kept alone)
    "r9-join-dropped": [("""LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : LVAR[w] " " op""",
                         """LVAR[w] = (vrp && op !~ /[$`\\047"\\\\[:space:]]/) ? op : !vrp ? LVAR[w] : LVAR[w] " " op""")],
    # r9 decision 2: " is counted but no longer against the cap
    "r9-cap-dquote-dropped": [("""e > 4096 || f > 4096) ? 1 : 0""", """e > 4096) ? 1 : 0""")],
    # r10 decision 1: the depth gate restored, so inside a substitution or a nested script the value is dropped again
    "r10-depth-gate-restored": [("""    if (mode == "prep" && t > nt) {
""", """    if (mode == "prep" && !depth && t > nt) {
""")],
    # r10 decision 2: the value a function body assigns before its loop no longer joins the loop's words
    "r10-function-body-join-dropped": [("""LVAR[val[t + 1]] = w ((FN && (val[t + 1] in FA)) ? FA[val[t + 1]] : "")""",
                                        """LVAR[val[t + 1]] = w""")],
    # review r9's probe mutant: the cap's deny message drops "
    "rv9-message-dquote-dropped": [(r"""of one of \$, #, \\, ', \" or \` and names""",
                                    r"""of one of \$, #, \\, ' or \` and names""")],
    "r5-dotdot-parent-dropped": [("""  case "$1" in .*[*?[]*) ;; *) return 1 ;; esac
  pw_pattern "$1" || return 0
""", "  return 1\n")],
    "r5-voldots-glob-dropped": [("""      /*) if pw_voldots "$pw_p"; then sv=$pw_av; pw_glob "$pw_vd" "$ctx" "$f"; [ "$pw_av" = G ] || pw_av=$sv; fi ;;
""", "")],
    "r5-brace-root-unread": [("""(\\/|$|\\{)/ || sf ~ /^~[$`]/""", """(\\/|$)/ || sf ~ /^~[$`]/""")],
    "r3c-unescape-dropped": [('        if (c == "\\\\") { if (i < n) out = out (mode ? lit(substr(run, i + 1, 1)) : substr(run, i + 1, 1)); i += 2; continue }\n',
                              '        if (c == "\\\\") { out = out c; i++; continue }\n')],
}
FENCE = "test_only_the_roots_the_header_and_the_message_changed"
REVIEW_R3 = {"r3c-wrappers-env-only", "r3c-unbalanced-brace-allows", "r3c-physical-stall-guard-dropped",
             "r3c-in-mount-glob-lists", "r3c-eval-unwrap-dropped"}


#: What the named tests need of HEAD (the rest of the tree, the app and the docs, is a sixth of
#: the export's bytes and none of its use).
EXPORTED = ["subfleet", "tests", "tools", "private", "bin", "pyproject.toml", DOC]


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *EXPORTED],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def mutate(tree: Path, name: str) -> None:
    file, old, new, _ = MUTATIONS[name]
    path = tree / file
    if name == "trust-hash-stale":                 # the new TRUST, its hook_sha256 left at the old bytes'
        text = path.read_text(encoding="utf-8")
        assert text.count(NEW_HOOK_SHA256) == 1
        path.write_text(text.replace(NEW_HOOK_SHA256, OLD_HOOK_SHA256), encoding="utf-8")
        return
    if name == "trust-built-on-repo-copy":         # the repository's copy at BASE, new hook hash, new sentence
        base = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE}:{TRUST}"],
                                         check=True, capture_output=True, text=True).stdout)
        new = json.loads(path.read_text(encoding="utf-8"))
        base["hook_sha256"] = new["hook_sha256"]
        base["provenance"] += new["provenance"][new["provenance"].index(" 2026-10-24, D-LANE-SCOPE1:"):]
        path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if name in EDITS:
        for old_text, new_text in EDITS[name]:
            assert text.count(old_text) == 1, (name, old_text[:60])
            text = text.replace(old_text, new_text)
    elif name == "env-spellings-dropped":
        assert text.count(ENV_ROOTS) == 1 and text.count(ENV_SED) == 2, (text.count(ENV_ROOTS), text.count(ENV_SED))
        text = text.replace(ENV_ROOTS, "").replace(ENV_SED, "'").replace("/$HOME/g;'", "/$HOME/g'")
    elif name == "trailing-slash-missed":
        assert text.count(NEW_ROOTS) == 1 and text.count(ROOT_CORE) == 1
        text = text.replace(NEW_ROOTS, "").replace(
            ROOT_CORE, ROOT_CORE[:-1] + "|(/Users/[^/[:space:]'\\\"]+|~|\\\\\\$HOME)/\\.subfleet"
                                        "(/[^/[:space:]'\\\"]+)?\"")
    else:
        assert text.count(old) == 1, (name, old)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, tests: list[str], scratch: Path) -> dict[str, str]:
    """{test name: passed|failed|error|skipped} from one pytest run in ``tree``."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    *[f"{TEST}::{test}" for test in tests]], cwd=tree, env=env, capture_output=True)
    import xml.etree.ElementTree as ElementTree
    outcomes = {}
    rank = ["passed", "skipped", "error", "failed"]
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        # A parametrized test's cases fold into its name: failed if any failed.
        name = case.get("name").split("[", 1)[0]
        outcomes[name] = max(outcomes.get(name, "passed"), outcome, key=rank.index)
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    # r4's and review r3's mutants first, then r3c's, r3b's and r3's: a run its hold cuts short still
    # reports the newest decisions.
    edited = [name for name in MUTATIONS if name in EDITS]
    newest = ([name for name in MUTATIONS if name.startswith(("r10-", "rv9-"))]
              + [name for name in MUTATIONS if name.startswith(("r9-", "rv8-"))]
              + [name for name in MUTATIONS if name.startswith(("r8-", "rv7-"))]
              + [name for name in MUTATIONS if name.startswith(("r7-", "rv6-"))]
              + [name for name in MUTATIONS if name.startswith(("r6-", "rv5-"))]
              + [name for name in MUTATIONS if name.startswith(("r5-", "rv4-"))]
              + [name for name in edited if name.startswith("r4-")] + [name for name in edited if name in REVIEW_R3])
    order = (newest + [name for name in reversed(edited) if name not in newest]
             + [name for name in MUTATIONS if name not in EDITS and name not in newest])
    parser.add_argument("--only", nargs="*", default=order)
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        named = sorted({MUTATIONS[name][3] for name in args.only} | {FENCE})
        control = scratch / "control"
        export(control)
        outcome = pytest(control, named, scratch)
        print(json.dumps({"mutant": "control", "outcomes": outcome}), flush=True)
        ok = all(outcome.get(test) == "passed" for test in named)
        for name in args.only:
            tree = scratch / name
            export(tree)
            mutate(tree, name)
            test = MUTATIONS[name][3]
            outcome = pytest(tree, [test, FENCE], scratch)
            killed = outcome.get(test) == "failed"
            ok = ok and killed
            print(json.dumps({"mutant": name, "named_test": test, "killed": killed, "outcomes": outcome}),
                  flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
