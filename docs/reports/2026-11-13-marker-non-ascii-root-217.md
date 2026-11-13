# Marker fix for a non-ASCII state root, rebased onto release/217 (PR #177; C-5.5, C-25.6)

The continuation session started at 2026-11-13T18:29:46Z and stopped at 21:18Z for the brief's 3-hour limit. An earlier session ran from 2026-11-12T15:46Z until a usage limit cut it off. The build is **incomplete**: the contract amendments, the mutant run, a complete fail-before run and test steps 2 to 4 were not done (see "Not done").

## Headline

- No live census bug. b51f08f96 (release/217) already matches every holdable root byte for byte, so this branch changes no census code: `subfleet/procs.py` is identical to b51f08f96's.
- C-25.6 is the remaining fix. `peers.py` now holds the vis(3) notation, keeps the line's trailing space, looks for the root as `ps` prints it, and turns a root no environment can hold into a refusal with exit 7.

## Commits

| Commit | Session | What |
|---|---|---|
| 9cf9d6ec1 | earlier | Rebase of c76db534a. Conflicts resolved toward release/217: the contract, `procs.containment`, and the census tests in `test_procs.py`. Dropped: the PR's `ps -axEww` regex hunk and its change-list line. Kept: the peers change, `PS_BYTES`/`ps_text` (then still in procs), the fixture, the decision record and the rendering tests. The judge tests were adapted to `AuditToken`. |
| 897c0f4c9 | earlier | Rebase of 3f724e616 (round 1's fixes). |
| (dropped) | earlier | 368cf3cf4 and 3f0fd0279 held only `ps`-row census tests and resolved to nothing. |
| 53b8497f7 | this | Moves `_ps_byte`/`PS_BYTES`/`ps_text` into `peers.py` beside `_ps`, fixes their comments, and moves their unit tests to `test_conversation_peers_attachments.py`. `test_procs.py` and `procs.py` are back to release/217. The message lists the census claims in 9cf9d6ec1 and 897c0f4c9 that D-CEN1 overtook. It also fixes a docstring bug: `\012` was an octal escape. |
| d2b22b84d | this | Round 2 finding 6: `judge` catches `ps_text`'s `ValueError` and returns `Verdict(False, peers.UNHOLDABLE_ROOT)`, so `_person` answers exit 7. Tests are in `test_person_boundary.py`. |
| 9899443fa | this | Census tests for every root on the raw reader: Hypothesis unit tests in `test_census_procargs.py`, and the new `tests/process/test_census_root_bytes_process.py`. The census tests and `marker_read_of` leave `test_ps_rendering.py`. |
| a27c83673 | this | Round 2 findings 1, 5 and 7 in the tests: framework Python, NFD, a variable after the root, long values, and the invariant 5 property through `process_chain`. |
| 6591bba28 | this | Decision record: a dated 2026-11-13 section. It cuts the signal sentence, fixes finding 4 and corrects the environment note. |

The earlier session's commits are unreviewed, and their messages still describe the old census reader. 53b8497f7's message corrects them. I found no code error in them beyond the docstring escape.

## Step 0

1. **Census half on b51f08f96: present.**
   - `procs.py:571-579` (`marker_strings`, `os.fsencode`, NUL refusal), `:554-568`/`:582-587` (whole-string match) and the `ValueError` → "marker enumeration unavailable" path (`:1185-1191` at this head) were read and confirmed.
   - `subprocess` encodes a child's environment with `os.fsencode` (CPython 3.14.7 `subprocess.py:1904-1907`), and so does `procs.spawn_disclaimed` (`procs.py:1700-1703`).
   - Real-children results:
     - The earlier session, on b51f08f96: 30/30 found (9 roots, including NFD and a 1,211-byte root, each with the root first, middle and last, plus a non-ASCII attempt id). Other-root and one-byte-longer censuses never matched. The NUL and surrogate cases, for root and attempt, were 4/4 unverifiable.
     - This session (procs.py identical to base): `test_census_root_bytes_process.py` passed. That is 9 roots × 3 positions found, every near-miss root excluded, the non-ASCII id found, and a 25-example property passed.
   - No live bug.
2. **C-25.6 half on b51f08f96: absent.** `peers.py:163` strips both ends, and `:192` matches the root as written.
3. **`ValueError` in `judge`: exit 2.** `service.respond` (`service.py:332-336`) maps `ValueError` to exit 2, "invalid arguments". The earlier session confirmed this with a probe on b51f08f96. I re-read the handler but did not re-run the probe.
4. **#88: still clashes.** It is open at 05cae6e2b. It rewrites the line to `read(["-Ewwp", ...]).strip()` and replaces `_ps` with `PsReader`/`_PS_ENV`.
5. **The PR body's environment note: stale.** `fix/containment-marker-gap` was never merged: f715cb14 is not in release/217, and the branch survives only as `origin/wip/backup-2026-10/...`. C-5.5's D-KILL2 text now documents the gap: a platform, hardened-runtime binary shows no environment strings. The note is corrected in the decision record and in the PR body below.
6. **Tests that depended on the old reader.** The `ps`-row census tests in `test_procs.py` dropped. `marker_read_of` was removed, and its census tests were rewritten on `procargs` bytes and real `containment`.

## Invariants and tests

1. **Every root, raw census.**
   - `test_every_root_is_matched_exactly_when_both_whole_strings_are_there` (Hypothesis, parse).
   - `test_every_root_census_counts_its_marked_process_and_not_another_roots` (Hypothesis, FakeKernel).
   - `test_census_finds_the_marker_under_arbitrary_root_bytes` (Hypothesis, real children).
   - `test_census_finds_a_marked_process_under_any_root_wherever_its_variable_falls`.
2. **Unholdable values:** `test_a_root_or_id_no_environment_can_hold_leaves_the_census_unverifiable` (a NUL, a lone surrogate, a low surrogate below U+DC80, in the root or the attempt id).
3. **Notation:**
   - `test_ps_prints_every_byte_as_ps_text_says` and `test_ps_prints_any_environment_as_ps_text_says`.
   - Under Homebrew's framework 3.14.7: 3 passed, the root-last case skipped.
   - Under uv's non-framework 3.14.7: 4 passed.
4. **Notation shape:** `test_ps_text_invariants` (Hypothesis, up to 1,500 bytes) and `test_ps_text_keeps_nfc_and_nfd_as_their_own_bytes` (Hypothesis).
5. **C-25.6 veto:**
   - `test_the_root_veto_holds_for_every_root_wherever_it_falls` (Hypothesis, through `process_chain` with `_ps` faked).
   - `test_a_root_marker_with_another_variable_after_it_is_found`.
   - Real-process cases: `test_the_person_check_finds_a_root_marker_with_a_variable_after_it` and `..._that_ends_the_line`.
6. **Unholdable root at the person check:** `test_a_root_no_environment_can_hold_is_refused_with_exit_7_not_exit_2` and `test_a_root_no_environment_can_hold_is_a_refusal_even_for_the_app`.

## Mutants

Not run (time). These are the expected killers, unverified:

| # | Mutant | Expected killer |
|---|---|---|
| 1 | root as written | the `ps_prints_it` and veto-property tests |
| 2 | strip both ends | `test_the_chain_keeps_the_space_a_root_ends_in` and the veto property |
| 3 | `endswith` | the middle-variable test and the veto property |
| 4 | `ValueError` escapes `judge` | the exit-7 tests |
| 5 | NFC-normalise | the NFD example and the NFC/NFD property |
| 6 | 0xA0 in an `M-` form | the examples and the fixture test |
| 7 | 0x80-0x9F as `M-^X` | the examples and the fixture test |
| 8 | no NUL refusal | the NUL refusal test |
| 9 | `ascii`/`replace` root | the every-root marker test and the census properties |
| 10 | stripped root | the "ends in a space" cases |
| 11 | root NUL refusal dropped | the unholdable census test (the attempt-id rows still refuse) |

## Both reviews

- **Round 1:** findings 1-6, 8 and 9 were answered by 3f724e616/3f0fd0279 and carried over, with the notation now in peers. Finding 8, the attempt id, is now a raw-reader test. Finding 7 is answered by finding 3 below.
- **Round 2:**
  1. Answered in a27c83673.
  2. Cut in 6591bba28. Round 2's replacement wording is false on release/217 too, because D-KILL4 makes marked members signal targets.
  3. The fresh fail-before run did not complete (below).
  4. Corrected in 6591bba28.
  5. Answered in a27c83673 and 9899443fa.
  6. Answered in d2b22b84d.
  7. Answered in the tests, the record and the body below.

## Commands and results

| Command | Result |
|---|---|
| `uv sync --group dev --extra api` | Failed: there is no `api` extra. Ran `uv sync --group dev --python /opt/homebrew/bin/python3.14` (framework) into `.venv`, plus a non-framework venv in scratch from uv's 3.14.7. |
| `-n 4` | Unavailable: no pytest-xdist in the dev group. Runs were single-process, at most 4 concurrent. Load averages were 98-154. |
| `test_conversation_peers_attachments.py` (after 53b8497f7) | 57 passed (521 s) |
| `test_person_boundary.py -k no_environment_can_hold` | 4 passed |
| census unit and process files (new tests) | 32 passed, 1 failed. The failing case was mine: CPython joins a `􏰀` pair into one astral character. It was replaced by `\udc7f`, and the 14 rerun passed. |
| `test_ps_rendering.py`, framework / non-framework | 3 passed + 1 skipped / 4 passed |
| Peers new tests | 20 passed |
| Final head, one file each | `test_census_procargs.py` 70 passed; `test_conversation_peers_attachments.py` 62 passed; `test_procs.py` 48 passed |
| Fail-before, on a `git archive` of b51f08f96 with this branch's test files | **Killed at 560 s, incomplete**: `.......FFFF.` |

## Not done

- **Contract:** C-5.5's sentence, C-25.6's notation and the change-list line were not added, and `test_invariants_index.py` was not run.
- **Tests:** steps 2-4 were not run, and neither was the full `test_person_boundary.py` or `test_census_root_bytes_process.py` at the final head. Both passed on their new tests earlier.
- **Fresh fail-before counts:** not taken.
- **Mutant run:** not done.

Scratch: `$TMPDIR/sf-mnar-b1` (venv, base export, outputs). No folder outside it was created.

## Follow-up

Switch C-25.6 to the raw `KERN_PROCARGS2` reader. That retires both `ps_text` and the runtime canary. It needs its own review after #88 lands.

## PR body for the hub

> **What was wrong.** On a state root that is not printable ASCII, or that ends in a space, C-25.6's person check could not find `SUBFLEET_ROOT` in a caller's `ps -E` line. `ps` prints the line in vis(3) notation (`é` prints as `M-CM-)`), and `process_chain` stripped both ends of the line. The census half of the original report is fixed on release/217 by D-CEN1 (8d9fe7e42), which compares raw `KERN_PROCARGS2` strings with `os.fsencode` bytes. This branch adds tests that pin that for every root and changes no census code.
>
> **The fix.** `peers.ps_text`/`PS_BYTES` (moved from procs) render the root as `ps` prints it. `process_chain` strips only the newline. `judge` refuses a root no environment can hold with exit 7, where it used to answer exit 2. The match is a substring, which leans toward refusing.
>
> **Evidence.** Unit and real-process tests for every root, under both a framework and a non-framework Python. The notation's process tests run wherever CI runs; CI has not yet measured the notation on this head. Mutants and fresh fail-before counts are outstanding.
>
> **Environment note.** The kernel shows no environment for platform, hardened-runtime binaries (C-5.5, D-KILL2). `fix/containment-marker-gap` was never merged.
>
> **For the merger.** #88 rewrites the same `process_chain` line. Whichever lands second keeps #88's `read([...])` call with this branch's `.rstrip("\n")`, and moves the notation's comment to `_PS_ENV`. Tests that fake `peers._ps` must then fake `read=`.
>
> 🤖 Generated with [Claude Code](https://claude.com/claude-code)
