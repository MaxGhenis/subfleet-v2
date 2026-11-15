# Marker fix for a non-ASCII state root, rebased onto release/217 (PR #177; C-5.5, C-25.6)

Three sessions built this branch:

| Session | Ran | Ended |
|---|---|---|
| First | 2026-11-12T15:46Z | A usage limit cut it off. |
| Second | 2026-11-13T18:29:46Z to 21:18Z | The 3-hour limit. Report at 355e295c1. |
| Third | 2026-11-14T23:28:30Z to about 02:15Z (2026-11-15) | The work was finished. |

Between the second and third sessions, the hub merged release/217 at 85769b826 into 355e295c1 (39e229c0e, a clean merge). This session finished what the second left: the contract, test steps 2 to 4, fresh fail-before counts, the mutant run, and round 2's finding 3. Some of test step 2 did not run; see "Not done".

## Headline

- **No live census bug.** b51f08f96 and 85769b826 (release/217) already match every root a process can carry, byte for byte. This branch changes no census code: `subfleet/procs.py` is identical to 85769b826's (and to b51f08f96's).
- **C-25.6 is the fix.** `peers.py` now holds the vis(3) notation and keeps the line's trailing space. It looks for the root as `ps` prints it, and it refuses a root that no environment can hold with exit 7, where it used to answer exit 2. The contract now says this.
- **Evidence.** On b51f08f96, the 29 new census cases pass and the 26 new C-25.6 cases fail. All 11 mutants are killed.

## Commits

| Commit | Session | What |
|---|---|---|
| 9cf9d6ec1 | first | Rebase of c76db534a. Conflicts went toward release/217: the contract, `procs.containment` and the census tests in `test_procs.py`. It dropped the PR's `ps -axEww` regex hunk and its change-list line. It kept the peers change, `PS_BYTES`/`ps_text` (still in procs then), the fixture, the decision record and the rendering tests. The judge tests were adapted to `AuditToken`. |
| 897c0f4c9 | first | Rebase of 3f724e616 (round 1's fixes). |
| (dropped) | first | 368cf3cf4 and 3f0fd0279 held only census tests built on `ps` rows, and resolved to nothing. |
| 53b8497f7 | second | Moves `_ps_byte`, `PS_BYTES` and `ps_text` into `peers.py` beside `_ps`, fixes their comments, and moves their unit tests to `test_conversation_peers_attachments.py`. `procs.py` and `test_procs.py` go back to release/217's. The message lists which census claims in 9cf9d6ec1 and 897c0f4c9 D-CEN1 overtook. It also fixes a docstring where `\012` was an octal escape. |
| d2b22b84d | second | Round 2 finding 6: `judge` catches `ps_text`'s `ValueError` and returns `Verdict(False, peers.UNHOLDABLE_ROOT)`, so `_person` answers exit 7. |
| 9899443fa | second | Census tests for every root on the raw reader: Hypothesis unit tests in `test_census_procargs.py`, plus a new `tests/process/test_census_root_bytes_process.py`. |
| a27c83673 | second | Round 2 findings 1, 5 and 7 in the tests: a framework Python, an NFD root, a variable after the root, values over 256 bytes, and the invariant-5 property through `process_chain`. |
| 6591bba28 | second | Decision record: a section dated 2026-11-13. It cuts the false sentence about signals, fixes finding 4 and corrects the environment note. |
| 355e295c1 | second | The second session's report, which was incomplete. |
| 39e229c0e | hub | Merges release/217 (85769b826). |
| 72d9f8deb | third | The contract: C-5.5's sentence, C-25.6's notation and one change-list line (details below). |
| 24123d6f9 | third | Decision record: the fresh fail-before counts (round 2 finding 3). |
| (this report) | third | This report, rewritten to cover all three sessions. |

Nobody has reviewed the first session's commits, and their messages still describe the old census reader; 53b8497f7's message corrects them. This session read the second session's code diff (`peers.py`, the tests) and found no error in it.

## Step 0

The second session recorded these findings. Where this session relied on one, it rechecked it against 39e229c0e.

1. **The census half is present on b51f08f96.**
   - Rechecked this session: `marker_strings` (`procs.py:571-579`) builds both strings with `os.fsencode` and refuses a NUL. `procargs_strings` and `procargs_marked` (`:554-568`, `:582-587`) compare whole strings. A `ValueError` from `marker_strings` becomes "marker enumeration unavailable" (`:1145-1152`).
   - `subprocess` encodes a child's environment with `os.fsencode` (Homebrew CPython 3.14.7 `subprocess.py:1904-1907`), so the daemon's markers and `marker_strings` are the same bytes.
   - `git diff --quiet b51f08f96 85769b826 -- subfleet/procs.py subfleet/conversations/peers.py subfleet/conversations/service.py` passed: the merge changed none of the three.
   - Real children:
     - The first session, on b51f08f96: 30 of 30 marked children found (9 roots, each with the root first, middle and last, plus a non-ASCII attempt id). Other roots never matched, and 4 of 4 roots or ids holding a NUL or a surrogate left the census unverifiable.
     - This session, on b51f08f96 itself: `test_census_root_bytes_process.py` 11 passed. That is 9 roots × 3 positions, with near-miss roots excluded, a non-ASCII attempt id, and the real-process property.
   - No live bug, so no fix commit.
2. **The C-25.6 half is absent on b51f08f96.** `peers.py:163` strips both ends of the line, and `:192` matches the root as written.
3. **What a `ValueError` in `judge` becomes.** `service.respond` answers it as "invalid arguments", exit 2. The first session confirmed this with a probe on b51f08f96. There is a nuance, from this session's fail-before run: b51f08f96 has no `ps_text`, so `judge` raises nothing there for an unholdable root. It looks for the root as written, finds nothing, and takes the app's caller for a person. The request then reaches the approval lookup (`unknown-approval`, exit 2). Round 2's exit 2 came from the PR head, where `ps_text` raised inside `judge`.
4. **#88 still clashes.** It is open at 05cae6e2b. It rewrites the same line to `read(["-Ewwp", ...]).strip()` and replaces `_ps` with `PsReader`/`_PS_ENV`. (The second session found this. This session did not query GitHub again.)
5. **The PR body's environment note was stale.** `fix/containment-marker-gap` (f715cb14) was never merged. C-5.5's D-KILL2 text on release/217 documents the gap: the kernel shows no environment strings for a platform, hardened-runtime binary. The note is corrected in the decision record and in the PR body below.
6. **Tests that relied on the old reader:** the census tests built on `ps` rows dropped, and `marker_read_of` is gone. Their cases were rewritten on `procargs` bytes and the real `containment`.

## The contract (72d9f8deb)

- **C-5.5.** One sentence, after the argv sentence: "The census compares the attempt id and the root as `os.fsencode` gives them, the bytes `subprocess` writes into a launched process's environment, byte for byte, so a root that is not printable ASCII, or that ends in a space, matches like any other (PR #177)." The PR's vis passage was not carried over.
- **C-25.6.** After the clause's first sentence:
  - The veto reads `ps -Ewwp <pid> -o command=` in the C locale (`peers._ps` sets `LC_ALL=C`), in vis(3) notation with `VIS_TAB | VIS_NL | VIS_NOSLASH`, and looks for `SUBFLEET_ROOT=` plus `peers.ps_text(root)`.
  - What `VIS_NOSLASH` does, and the two pairs of roots that print alike.
  - It strips only the newline, and it matches a substring, not a space-bounded token, which leans toward refusing.
  - It sees only environments `ps -E` shows (C-5.5, D-KILL2).
  - An unholdable root is a refusal, exit 7.
- **Change list.** One line at the top of "Changes in version 3", dated 2026-11-14, naming PR #177 and D-CEN1. There is no blank line between it and the next item.
- No clause was renumbered. `tests/unit/test_invariants_index.py`: 25 passed, both after the edit and at the end.

## Invariants and their tests

1. **Every root, raw census** (Hypothesis):
   - `test_every_root_is_matched_exactly_when_both_whole_strings_are_there` (parse);
   - `test_every_root_census_counts_its_marked_process_and_not_another_roots` (`FakeKernel`);
   - `test_census_finds_the_marker_under_arbitrary_root_bytes` (real children);
   - `test_census_finds_a_marked_process_under_any_root_wherever_its_variable_falls` (9 roots × 3 positions, with near-miss roots).
2. **Unholdable values:** `test_a_root_or_id_no_environment_can_hold_leaves_the_census_unverifiable`. It covers a NUL, a lone surrogate, and a low surrogate below U+DC80, in the root or the attempt id.
3. **Notation:** `test_ps_prints_every_byte_as_ps_text_says` and `test_ps_prints_any_environment_as_ps_text_says`. Under Homebrew's framework 3.14.7, 3 passed and the root-last case skipped (its precondition is false there). Under uv's non-framework 3.14.7, 4 passed.
4. **Notation shape** (Hypothesis): `test_ps_text_invariants` (up to 1,500 bytes) and `test_ps_text_keeps_nfc_and_nfd_as_their_own_bytes`.
5. **C-25.6 veto:**
   - `test_the_root_veto_holds_for_every_root_wherever_it_falls` (Hypothesis, through `process_chain` with `_ps` faked);
   - `test_a_root_marker_with_another_variable_after_it_is_found`;
   - real processes: `test_the_person_check_finds_a_root_marker_with_a_variable_after_it` and `..._that_ends_the_line`.
6. **Exit 7 for an unholdable root:** `test_a_root_no_environment_can_hold_is_refused_with_exit_7_not_exit_2` and `test_a_root_no_environment_can_hold_is_a_refusal_even_for_the_app`.

## Fail-before on b51f08f96 (round 2 finding 3)

This ran on a `git archive` of b51f08f96 in scratch, with this branch's five test files and `tests/fixtures/ps_vis_bytes.json` copied in. It used the worktree's `.venv` (the `pythonpath = ["."]` setting imports the archive's `subfleet`; checked: its `peers` has no `ps_text`). Each file ran on its own.

| File (new cases) | On b51f08f96 | Why |
|---|---|---|
| `tests/unit/test_census_procargs.py` (18 new of 70) | 70 passed | D-CEN1 already compares bytes. |
| `tests/process/test_census_root_bytes_process.py` (11, all new) | 11 passed | Same reason. |
| `tests/unit/test_conversation_peers_attachments.py`, the 9 new functions (18 cases) | 18 failed | 16: `peers.ps_text`/`PS_BYTES` missing. `..._found_as_ps_prints_it`: the app's caller was admitted ("this needs the app" path, not the marker refusal). `..._keeps_the_space_a_root_ends_in`: the chain stripped `/tmp/root `. |
| `tests/process/test_ps_rendering.py` (4, all new) | 4 failed | `peers.ps_text` missing. |
| `tests/unit/test_person_boundary.py -k no_environment_can_hold` (4 new) | 4 failed | 2: `peers.UNHOLDABLE_ROOT` missing. 2: the app's caller was taken for a person and reached `unknown-approval` (exit 2). |

So the 29 new census cases pass there, as the brief expected, and the 26 new C-25.6 cases fail. The decision record carries these numbers (24123d6f9).

## Mutants

Each mutant was applied alone to `sf-mnar-b2/mut`, a `git archive` of the head (mutant 11 used a copy, `mut2`). The script asserts that the original text occurs exactly once and prints the diff. After the run it writes the file back from `sf-mnar-b2/pristine` and asserts that the sha256 equals the pristine file's: `peers.py` 3d2aa00e1ac01564…, `procs.py` 0bdc61b5a1a877ab…. All 11 restores matched.

Mutants 1 and 2 ran the whole peers file, the whole person file, and the rendering file under both Pythons. At load 240 to 275, mutants 3 to 11 ran only the files that hold their expected killers (see the deviations).

| # | Mutant | Killed by (genuine failures) |
|---|---|---|
| 1 | peers matches the root as written | `test_a_root_marker_is_found_as_ps_prints_it`, `..._with_another_variable_after_it_is_found`, the veto property; the 4 unholdable-root person tests; real `ps`: `..._with_a_variable_after_it` (both Pythons) and `..._that_ends_the_line` (non-framework) |
| 2 | `process_chain` strips both ends | `test_the_chain_keeps_the_space_a_root_ends_in`, the veto property; real `ps`: `..._that_ends_the_line` (non-framework only; it is skipped under a framework Python) |
| 3 | `endswith` | `..._with_another_variable_after_it_is_found`, the veto property; real `ps`: `..._with_a_variable_after_it` (non-framework). Under the framework Python that test failed on a 5 s `ps` timeout, which was load, not a kill. |
| 4 | `ValueError` escapes `judge` | all 4 unholdable-root person tests (`ValueError` and `UnicodeEncodeError` escape) |
| 5 | NFC-normalise | `test_ps_text_examples[José]`, `test_ps_text_keeps_nfc_and_nfd_as_their_own_bytes`. Two real-process failures were 5 s `ps` timeouts, which was load. |
| 6 | 0xA0 in an `M-` form | `test_ps_text_prints_every_byte_as_ps_printed_it` (fixture), 2 `test_ps_text_examples` rows, `test_ps_text_invariants`; real `ps`: both rendering tests. Two person-check failures were timeouts. |
| 7 | 0x80-0x9F as `M-^X` | the fixture test, the `\udc80…` and NFD example rows; real `ps`: both rendering tests |
| 8 | `ps_text` keeps a NUL | both `test_ps_text_refuses_a_value_with_a_nul` rows; the person tests' NUL rows (app admitted; `unknown-approval`). The veto property's failure was a Hypothesis "input generation is slow" health check, which was load. |
| 9 | root `.encode("ascii", "replace")` | 11 census unit cases: `..._is_the_roots_own_bytes` (5 roots), both census properties, 2 lone-surrogate refusal rows, and `test_an_escaped_byte_is_a_holdable_root` (2). Also 4 real-process roots (`('first', ())`: the marked child was missing). |
| 10 | stripped root | `..._is_the_roots_own_bytes[ends in a space]` and `[over 1,000 bytes]`, both census properties; real process `[ends in a space]` and `[over 1,000 bytes]` |
| 11 | root NUL refusal dropped | `test_a_root_or_id_no_environment_can_hold_leaves_the_census_unverifiable[a NUL in the root]` |

No mutant survived. Two killers are platform-conditional. Under a framework Python, mutant 2 is killed by the unit tests but not by the real `ps` test, which skips there. Mutant 3's real-process kill was measured only under the non-framework Python.

## Both reviews

- **Round 1:** findings 1-6, 8 and 9 were answered by 3f724e616 and 3f0fd0279 and carried over, with the notation now in peers. Finding 8 (the attempt id) is now a raw-reader test. Finding 7 (a precise fail-before list) is answered by the table above.
- **Round 2:**
  1. CI red under a framework Python: answered in a27c83673. The rendering tests read through `peers._ps`, assert a contiguous run bounded by spaces, and check the root-last precondition or skip.
  2. The false signal sentence: cut in 6591bba28. Round 2's replacement doesn't hold on release/217 either, because D-KILL4 makes marked members signal targets.
  3. Stale fail-before numbers: answered by this session's fresh run (above, and in 24123d6f9).
  4. "All but the first predate this change": corrected in 6591bba28.
  5. Test gaps its mutants found: answered in 9899443fa and a27c83673. This session's mutants 5, 3 and 9/10 confirm the NFD, after-the-root and long-root tests kill.
  6. Exit 2 for an unholdable root: answered in d2b22b84d, and killed by mutant 4's tests.
  7. Wording: answered in the tests, the record, C-25.6 (72d9f8deb) and the body below.

## Commands and results (this session)

Setup:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, scratch `$TMPDIR/sf-mnar-b2`.
- Every pytest ran through `sf-mnar-b2/run1.sh`: `env -u PYTHONPATH`, a fresh `--basetemp` and Hypothesis directory per run, `-p no:cacheprovider`, JUnit XML, and an in-process 565 s alarm.
- One file per process, and at most 4 at once.
- Load averages were 95 to 275 throughout. The power source was AC.

| Command | Result |
|---|---|
| `uv sync --group dev --extra api --python /opt/homebrew/bin/python3.14` | Failed: there is no `api` extra. |
| `uv sync --locked --group dev --python /opt/homebrew/bin/python3.14` | `.venv`: framework 3.14.7, GIL. |
| `UV_PROJECT_ENVIRONMENT=$S/venv-nonfw uv sync --locked --group dev --python <uv 3.14.7>` | Non-framework 3.14.7 venv. |
| `edit_contract.py` (exact single-occurrence inserts), then `test_invariants_index.py`, `test_protocol_registry.py`, `test_desktop_ledger.py` | 25 / 4 / 4 passed |
| **Step 1 at head:** `test_census_procargs.py` | 70 passed (55 s) |
| `test_census_root_bytes_process.py` | 11 passed (121 s) |
| `test_person_boundary.py` (whole) | 101 passed (143 s) |
| `test_conversation_peers_attachments.py` | 62 passed (36 s) |
| `test_procs.py` | 48 passed |
| `test_ps_rendering.py` framework / non-framework | 3 passed + 1 skipped / 4 passed |
| **Step 2 at head**, one file at a time (the step-1 files above are also step-2 files) | `test_person_boundary_identity.py` 25 passed; `test_conversation_steer_service.py` 45 passed; `test_conversation_handoff.py` 48 passed; `test_suggestions.py` 30 passed; `test_suggest_cli.py` 7 passed; `test_hidden_environment.py` 7 passed; `test_guard_overlay.py` 22 passed; after the report commit: `test_hooks.py` 81 passed; `test_launch_env_round2.py` 67 passed; `test_census_recycled_pid.py` 25 passed; `test_quar_start1_census.py` 21 passed + 17 xfailed; `test_timers_busy_read.py` 115 passed; `test_review_publish_env_round8.py` 17 passed; `test_census_boot_seeds.py` 36 passed |
| Step 2, cut by the 565 s alarm | `test_suggestion_secret_properties.py`: 4 passed, then interrupted, none failed. `test_suggestion_properties.py`: interrupted before any test finished. |
| Step 2, stopped | `test_guard_subfleet_root.py`: the first run's shell was stopped by the tool's background limit, with no result written. The rerun hit the 565 s alarm at 26 passed and 2 skipped, with none failed. Afterwards no process under `sf-mnar-b2` was left (`ps` check). |
| **Step 3:** `tests/fake/test_state_contract.py` | 37 passed |
| `tests/unit/test_test_module_names.py` | 1 passed |
| `tests/unit/test_invariants_index.py` | 25 passed |
| `tests/unit/test_handler_refusals.py` | 372 passed (469 s) |
| **Step 4:** `grep` for the pinned reasons | "the caller carries Subfleet's attempt markers" is pinned in `test_ps_rendering.py` (2), `test_conversation_peers_attachments.py` (4) and `test_person_boundary.py` (3), and all three pass. The new refusal is compared only through `peers.UNHOLDABLE_ROOT` (5 uses in `test_person_boundary.py`). "marker enumeration unavailable" appears in 6 test files; its census text is unchanged (`procs.py` is identical), so nothing needed updating. |
| **Fail-before:** the five files on the b51f08f96 archive | See the table above. |
| **Mutants:** `mutants.py 1` … `11` | All killed; see the table above. |

Earlier sessions' commands (second session, on 355e295c1's code, which this head shares):
- the peers file 57 passed (521 s), then 62 passed at its final head;
- `test_person_boundary.py -k no_environment_can_hold` 4 passed;
- the census files 32 passed and 1 failed, the failure being a bad test case of the session's own (CPython joins a `􏰀` pair), replaced, then 14 passed;
- the rendering file 3 + 1 skipped / 4;
- a fail-before attempt killed at 560 s (`.......FFFF.`).

## Deviations

- There is no `api` extra, and no pytest-xdist, so there was no `-n 4`. Step 2 ran one file per process, at most 4 at once, as the continuation brief requires, rather than as one `-n 4` run.
- At load 240 to 275, mutants 3 to 11 ran only the files that hold their expected killers. That meant the person file narrowed to its 4 unholdable-root tests, and `test_procs.py` was not run for mutants 9 to 11.
- Three mutant runs hit the 565 s alarm or timeouts caused by load: mutant 2's whole person file (98 passed, then cut), and mutant 9's and mutant 10's real-process census files (cut after 8 and 10 cases). Kills are counted only from assertion failures, never from a timeout or a health check.
- The rule "run pytest one file at a time" was taken to mean one file per pytest process. Up to 4 processes ran at once.

## Not done

- **Test step 2, partly.** The following files did not run at this head, for lack of time at this load:
  - process: `test_census_hidden_environment_process.py`, `test_census_parent_chain_process.py`, `test_census_raw_markers_process.py`, `test_census_responsibility_process.py`, `test_guardian_process.py`, `test_kill4_fix1_process.py`, `test_quar_start1_process.py`;
  - unit: `test_retention_location_review_r13.py`, `test_sessions_headless_properties.py`;
  - fake: `test_census_boot_seeds_daemon.py`, `test_census_escapers_daemon.py`, `test_enrollment_guardian.py`, `test_suggestions_fake.py`;
  - e2e: `test_conversations.py`, `test_suggestions_r1b_peer_e2e.py`;
  - frontend: `test_core_live_steer.py`, `test_core_protocol.py`, `test_core_queue_engine.py`.

  Also unfinished: `test_guard_subfleet_root.py` (stopped once, then cut at 26 passed), `test_suggestion_properties.py` and `test_suggestion_secret_properties.py` (cut). None of these files is changed by this branch. The census ones test `procs.py`, which is identical to release/217's. CI runs them all.
- The PR body is not set and nothing is pushed. The hub does both.

Scratch is `$TMPDIR/sf-mnar-b2`. It keeps the scripts (`run1.sh`, `mutants.py`, `edit_contract.py`, `junit_summary.py`), `out/` (every run's text and JUnit XML, and `mutant-NN.json`), the Hypothesis directories, and the stopped run's basetemp. At the end, a guarded `shutil.rmtree` (asserting `sf-mnar-b2` and the scratch parent) removed the non-framework venv, the b51f08f96 archive, both mutant trees and the pristine copies. No folder outside it was created, apart from the worktree's ignored `.venv`. No process was left running.

## Follow-up

Switch C-25.6 to the raw `KERN_PROCARGS2` reader. That would retire both `ps_text` and the runtime canary the decision record describes. It needs its own review, after #88 lands.

## PR body for the hub

> **What was wrong.** On a state root that is not printable ASCII, or that ends in a space, C-25.6's person check could not find `SUBFLEET_ROOT` in a caller's `ps -E` line. `ps` prints the line in vis(3) notation (`é` prints as `M-CM-)`), `judge` looked for the root as written, and `process_chain` stripped both ends of the line. This PR's original census half is already on release/217: D-CEN1 (8d9fe7e42) compares raw `KERN_PROCARGS2` strings with the markers' `os.fsencode` bytes. This branch adds tests that pin that for every root, and it changes no census code (`subfleet/procs.py` is release/217's).
>
> **The fix (C-25.6).**
> - `peers.ps_text`/`PS_BYTES` (moved from procs) render the root as `ps` prints it.
> - `process_chain` strips only the newline.
> - `judge` refuses a root that no environment can hold with exit 7, where it used to answer exit 2.
> - The match is a substring, which leans toward refusing.
>
> C-5.5 gains one sentence (byte-for-byte `os.fsencode` comparison). C-25.6 gains the notation, and the change list gains one line.
>
> **Evidence.**
> - Unit and real-process tests for every root. The notation was checked against the real `ps` under a framework Python (3 passed, the root-last case skipped) and a non-framework Python (4 passed), locally on macOS 26.
> - On b51f08f96 the 29 new census cases pass, as expected, and the 26 new C-25.6 cases fail.
> - 11 of 11 mutants are killed.
> - CI has not yet measured the notation on this head. Some step-2 regression files were not run locally because of host load; CI runs the whole suite.
>
> **Environment note.** The kernel shows no environment for platform, hardened-runtime binaries (C-5.5, D-KILL2), so neither reader finds a marker on them. `fix/containment-marker-gap` was never merged.
>
> **For the merger.** #88 rewrites the same `process_chain` line. Whichever PR lands second should:
> - keep #88's `read([...])` call with this branch's `.rstrip("\n")`;
> - move the notation's comment to `_PS_ENV`;
> - change tests that fake `peers._ps` to fake `read=`.
>
> 🤖 Generated with [Claude Code](https://claude.com/claude-code)

Head built: the commit that last changed this report, on top of 24123d6f9 and 414b0c0b7 (detached in the job workspace).
