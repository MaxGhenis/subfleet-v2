# Gate certificate round trip (D-GATE-CERT1), then #178's format re-ask on release/217

Build of 2026-11-13, from release/217 at 266c3ab8b. The commits are on a detached HEAD in the job's worktree: `fix/gate-cert-roundtrip-217` is checked out in the caller's worktree (`subfleet-v2-lanes/gate-cert-roundtrip`), so git would not switch to it here, and moving that ref would have changed the caller's checkout. Subfleet's salvage ref carries the commits; the hub points the branch at the head named at the end.

| Commit | What |
|---|---|
| 4b14e71a9 | D-GATE-CERT1: refuse a number past float range at parse time |
| 681c72033 | D-GATE-CERT1: write a re-wrapped verdict's sentinels escaped |
| f16d56094 | D-GATE-CERT1: invariant tests, frozen base parser, differential |
| 505bab790 | D-GATE-CERT1: C-23.9 amendment, change-log line, gates doc |
| 0bb31ecff | Port of #178's re-ask onto the fixed gate |
| 490c12a39 | Port: the gate test harness stands in for `timers` |
| e7ad0109d | Port: diagnosis fast path, so the linearity bound holds |

## The two defects

Both come from `certificate()`. It rebuilds the certificate by serializing the stored verdict with `json.dumps`, wrapping it in sentinels and running `parse_verdict` on it again. Anything `parse_verdict` accepted but `json.dumps` writes differently fails there.

1. **A number past float range.** `json.loads` turns `1e1000` into infinity. The `parse_constant` hook sees only the literal constants (`Infinity`, `-Infinity`, `NaN`), so `parse_verdict` accepted it. `json.dumps` writes it back as `Infinity`, which the re-parse refuses.
2. **An escaped sentinel.** A peer that quotes the sentinel in a string escapes a hyphen (`---SUBFLEET-VERDICT-BEGIN---`), so its output holds one pair of sentinels and parses. The decoded value then holds the literal sentinel. `json.dumps` escapes only non-ASCII characters, so it writes the sentinel literally, and the re-parse finds two.

**Ordering on release/217.** `service.py` orders it the same way as #178's branch. `_consume` journals `status="agreed"` in the round-consumed transaction (lines 409–416 at 266c3ab8b) and only then calls `_complete`. `_complete` calls `certificate()` (line 306). A refusal there returns `{"code": 4, "status": "error"}` from `dispatch` and leaves the journaled state `agreed`. Every later `gate.poll` runs `_complete` again and repeats the refusal; `gate continue` at the same revision does too (line 476).

### Reproduction

`.scratch/repro.py` (kept in the worktree, not committed) extends the hub's `gate-repro.py`. Base is a `git archive 266c3ab8b subfleet` export; head is this worktree.

| Input | Base 266c3ab8b | Head |
|---|---|---|
| control | accepted; certificate OK | accepted; certificate OK |
| `"x": 1e1000` | accepted; certificate FAILED: `invalid JSON constant Infinity` | refused, exit 4: `peer verdict holds a number outside the float range at $.x` |
| `-1e400` in a finding | accepted; certificate FAILED: `invalid JSON constant -Infinity` | refused, exit 4: `... at $.findings[0].weight` |
| escaped begin sentinel in `summary` | accepted; certificate FAILED: `missing or duplicates the verdict sentinel` | accepted; certificate OK, `peer_verdict` equal |
| escaped end sentinel in `summary` | same failure | accepted; certificate OK |
| escaped sentinel in a member name | same failure | accepted; certificate OK |
| three sentinels sharing hyphens, all escaped | same failure | accepted; certificate OK |
| nesting 600 deep | accepted; certificate raised `RecursionError` | refused, exit 4: `nested more than 32 levels deep` |

The last row is a third accepted-but-uncertifiable input on base. `copy.deepcopy` in `certificate()` recurses, so the error is a `RecursionError`, not a `GateError`. The D-GATE-CERT1 commits alone leave it open. The port closes it, because #178's fixed 32-level bound refuses it at parse time.

## The fix (D-GATE-CERT1)

**Numbers.** `parse_verdict` preflights `json.dumps(value, allow_nan=False)` right after the revision binding. It refuses with exit 4 and names the JSON path of the first non-finite number, for example `$.findings[0].weight` or `$["a key"]`. The path is ASCII, at most 200 characters, and built from parent links, so a deep or wide verdict costs no quadratic string building.

I picked the preflight over a `parse_float` hook for three reasons:

- It tests exactly what the certificate needs: that the value can be written back as strict JSON. That covers infinity however it was produced.
- It runs after the binding check, so a wrongly bound verdict is still reported as a binding failure. Under #178 that is a plain `GateError`, never re-asked. A hook would fire inside `json.loads`, before the binding, and hide a binding failure behind a format error.
- #178 already has a serialization preflight (its lone-surrogate check). In the port the two are one `json.dumps(value, ensure_ascii=False, allow_nan=False)` call, which is review r5's suggested fix.

A hook would have seen the literal (`1e1000`), but neither approach knows the location without a walk. The walk runs only after the preflight fails.

**Sentinels.** `wrap_verdict()` (in `verdict.py`) wraps a value in sentinels and writes the first hyphen of either sentinel inside the payload as `-`.

- It uses a lookahead regex, so every sentinel start is found, overlapping ones included. A one-pass `str.replace` resumes after each match and misses a begin sentinel that starts inside another; `test_a_one_pass_replace_would_miss_a_sentinel_that_starts_inside_another` shows this.
- No new sentinel can form. A sentinel's letters can sit only inside a JSON string, where `-` decodes to the same hyphen, and the escape adds no hyphen and none of the sentinels' letters.
- JSON with no sentinel in it keeps its exact bytes.
- Parsing is unchanged: the stored verdict keeps the peer's decoded text.

**Every place that wraps a verdict in sentinels:**

| Place | Treatment |
|---|---|
| `certificate()` (`subfleet/gate/certificate.py`) | `parse_verdict(wrap_verdict(verdict), expected)` |
| `replay()` | Calls `certificate()`; covered by `test_replay_rebuilds_a_certificate_whose_verdict_quotes_a_sentinel` |
| The peer prompt's verdict template (`round.peer_prompt`) | `wrap_verdict(schema, indent=2, sort_keys=True)`. A plan path or repository holding a sentinel left two blocks in the template, and a peer that copied it produced a refused verdict. |
| The peer prompt's context JSON (prior verdict, brief, main response) | `escape_sentinels(...)`. It is not wrapped, but a prior verdict that quoted a sentinel would otherwise put a literal one back into the next round's prompt. |

No other code wraps a verdict. A search of `subfleet/`, `tools/`, `bin/` and `app/` for the sentinel constants finds only these and the parser.

Left as is, because they are data rather than verdicts: #178's re-ask prompt quotes the rejected output as a JSON string, and the fake peer fixture (`tests/fake/gate_peer.py`) is test code.

**Base refusals kept.** Neither change makes the parser accept anything it refused before. The differential test below checks that against the frozen base parser.

**Already-journaled gates.** A gate that is already `agreed` on an escaped-sentinel verdict issues its certificate on its next poll, since `certificate()` now escapes. A gate already `agreed` on a non-finite verdict stays stuck: the verdict cannot be written as strict JSON. Turning a certificate refusal into `blocked` would be a separate change to `_complete`, which this build does not make.

## The port of #178

`git diff $(git merge-base origin/main 904b47400) 904b47400`, applied with `git apply --3way` onto the D-GATE-CERT1 head.

**Applied as written (merged cleanly):** `subfleet/gate/service.py`, `subfleet/gate/cli.py`, `docs/gates.md`, `tests/fake/test_gate_end_to_end.py`, `tests/unit/test_gate_verdict.py`, `tests/unit/test_gate_reask.py`, `tests/unit/test_gate_reask_properties.py`. The 3-way merge kept release/217's `read_regular` for the deliverable and its busy-tolerant `_poll` in the CLI. #178's real-daemon test already passes `--main-model fable` and expects `gpt-6-astra`, both valid on release/217.

**Mapped:**

- **`verdict.py` (conflict).** Kept both sides. The two serialization preflights became one call (above). A number past float range is a `VerdictFormatError`, still checked after the binding. Added a docstring line.
- **`round.py` (conflict).** #178 moved the template after the context and added `OUTPUT_RULE`. The template keeps D-GATE-CERT1's `wrap_verdict` and the context keeps `escape_sentinels`.
- **`tests/fake/gate_peer.py` (conflict).** Kept release/217's `turn.completed` stream line beside #178's prose marker.
- **Contract (conflict).**
  - Took #178's C-23.9 and C-23.10, and re-applied D-GATE-CERT1 to C-23.9: a number past float range joins the list of form failures, plus the certification sentence and an amendment note.
  - Kept release/217's own wording of C-23.43 and C-23.44, and added #178's C-23.43 sentence. C-23.53 merged cleanly.
  - #178's change-log bullet, dated 2026-09-23 in version 2's list, became a version-3 line dated 2026-11-13 with no PR number. Release/217 never had it in September.
- **`tests/unit/test_gate_admission.py` (code only release/217 has).** Release/217's admission checks a pinned job against C-11.8's pin view, which calls `self.timers.merge_lane`; `main` has no such check. The re-ask is pinned to the first dispatch's lane, so every test that reached a re-ask's admission, most of the first run's 51 failures, failed with `'Daemon' object has no attribute 'timers'`. The `core` harness already reads lanes without timers, and now stands in a `timers` that merges no probe findings, as several release/217 tests do.
- **`_read` in `verdict.py` (linearity).** See the last row of the review r5 table below.

**A number past float range under the re-ask.** It is a form failure the re-ask covers.

- In a template member such as `schema_version`, the diagnosis reads the object and nothing locks it, so a clean re-ask approves and certifies (`test_a_number_past_float_range_is_reasked_and_a_clean_retry_certifies`).
- In an extra member, #178's outcome lock ("carried members outside the verdict template") keeps a re-asked approval from counting. A repeat of the number blocks with exit 4 (`test_a_number_past_float_range_never_lets_the_gate_agree_on_it`).
- Either way the gate never journals `agreed` on a verdict it cannot certify.

**Does an escaped sentinel need a re-ask once item 1 lands? No.** It was never a parse failure: base and head both accept it. Item 1 makes it certify, so the round agrees on its first dispatch with no `format_reask` (`test_a_round_whose_peer_quotes_a_sentinel_agrees_and_issues_its_certificate` checks one job).

A sentinel written literally inside a string is a different input. It makes a duplicate sentinel, which is a form failure, and #178's diagnosis cannot read the blocks it splits into, so it refuses the re-ask and the round blocks after one job (`test_a_sentinel_quoted_literally_is_not_reasked`). That is #178's fail-closed design, kept as is.

**Review r5, point by point:**

| r5 item | On the port |
|---|---|
| Finding 1 (Medium): `1e1000` passes, certificate refuses | Fixed by D-GATE-CERT1. The merged preflight is r5's smallest fix, plus the path in the message. |
| Finding 2 (Medium): escaped sentinel passes, certificate refuses | Fixed by D-GATE-CERT1 with r5's escaping. A lookahead also covers overlapping sentinels, and the prompt template and context get the same treatment. |
| Checked 1: depth counting for dicts, lists and `_Members`; 32 passes, 33 fails | Kept: `_too_deep` unchanged. `test_c23_9_nesting_is_bounded_by_a_fixed_depth_not_by_the_interpreter` and the nesting property pass. |
| Checked 2: accepted values can still break later steps (findings 1–2) | Closed. Invariant 1's property now covers it, with nesting drawn past the bound. |
| Checked 3: no revision-binding bypass | Kept. The new check runs after the binding, so a non-finite number never hides one (`test_revision_binding_outranks_a_number_past_float_range`); one inside `artifact_revision` is a binding failure. #178's binding tests pass. |
| Checked 4: no exception or nontermination in diagnosis | Kept. `test_c23_9_the_diagnosis_is_total` passes. Infinity in a candidate verdict is written as `Infinity` by `json.dumps`, which does not raise, and `clip` bounds it. The fast path below changes no result. |
| Checked 5: the approval-shape oracle is sound | Kept. `test_c23_9_a_reask_is_free_to_approve_only_after_an_approval_shaped_object` passes. |
| Checked 6: both nesting statements match the code | Kept unchanged. "A number past float range" was added to the form-failure lists in C-23.9 and `docs/gates.md`. |
| Linearity: 11.0 s for 40,000 begin sentinels | Measured at 6.7 s in a 4-worker run under load, over the 5 s bound. Nearly all of it was 80,000 failing `json.loads` calls on empty blocks, each building a decoder. `_read` now skips a candidate whose first JSON-whitespace-stripped character is neither `{` nor `"`, and reuses one decoder; `test_the_diagnosis_reads_exactly_what_it_read_before_its_fast_path` compares it with #178's version. Now 0.07 s in pytest at load ~26; standalone best of three at load ~68 was 0.14 s (was 2.0–3.8 s), 0.48 s with a `{` after each sentinel, 0.68 s with an `x`. |

## Invariants and their tests

1. **Accepted implies certifiable.** `tests/unit/test_gate_cert_properties.py::test_accepted_implies_certifiable` (Hypothesis, 500 examples). The generated texts cover nested values up to 40 levels (past the 32-level bound), finite, large and non-finite numbers including raw literals, strings holding either sentinel or its escapes in four writing styles, non-ASCII text and lone surrogates, extra members and broken envelopes. Whenever `parse_verdict` accepts a text:
   - the verdict equals the payload as `json.loads` decodes it;
   - `wrap_verdict` parses back to it;
   - for an approval, `certificate()` succeeds and `peer_verdict` equals it.

   `test_the_strategy_reaches_every_class` uses `find()` to show the strategy reaches each class: an accepted approval quoting a sentinel, accepted non-ASCII, accepted nesting 20 or more deep, a base-accepted number the head refuses, and a base refusal.
2. **No new acceptances.** `test_head_refuses_every_text_the_base_refuses` runs the same texts through the head and the base `parse_verdict`. Any exception counts as a refusal, since base lets `RecursionError` escape. If the head accepts, the base accepted and both return the same verdict.
   - The base is bundled verbatim as `tests/fixtures/gates/verdict-266c3ab8b.py.txt`.
   - `tests/reference_verdict_266c3ab8b.py` checks its SHA-256 (`a466a5d7…0121`) and git blob id (`7c88bd13…effa`, equal to `git rev-parse 266c3ab8b:subfleet/gate/verdict.py`) before loading it, so no test needs git history.
   - `test_the_base_parser_is_266c3ab8b_verbatim_and_had_both_defects` pins both hashes and the base's acceptance of `1e1000`.
3. **One re-ask per round.** #178's own tests, kept: `test_gate_reask.py::test_two_format_failures_block_the_round_as_before` (no third job), `test_each_round_has_its_own_reask`, `test_format_reask_does_not_consume_a_round`, and `test_gate_end_to_end.py::test_c23_9_cli_blocks_after_two_format_failures`. `test_gate_cert_roundtrip.py::test_a_number_past_float_range_never_lets_the_gate_agree_on_it` adds a number-past-float-range case: two jobs, then blocked.
4. **Determinism.**
   - `test_gate_cert_roundtrip.py::test_the_same_peer_output_gives_the_same_round_result_and_certificate_bytes` runs two independent daemons (two `core` builds) under one clock and one id sequence. The same peer output gives equal round results (job id aside), verdicts, statuses, errors and `certificate.json` bytes.
   - `test_accepted_implies_certifiable` also checks that re-parsing a text gives the same verdict and the same certificate bytes.

## Mutants

Each was applied alone by `.scratch/mutate.py` to files copied from the committed head (`.scratch/pristine/`). After each run the file was restored from that copy and checked byte for byte with `cmp`; a dry run also confirmed `git diff --quiet HEAD -- subfleet`.

| Mutant | Killed by (failed / run) |
|---|---|
| 1. Drop the non-finite check (`allow_nan=False` removed) | 9 / 30: `test_number_past_float_range_is_refused_at_parse_time_naming_where` (6 cases), `test_gate_verdict.py::test_c23_9_envelope_and_shape_failures_are_typed_for_the_rounds_one_reask[number-past-float-range]`, `test_a_number_past_float_range_never_lets_the_gate_agree_on_it`, `test_accepted_implies_certifiable`. The `schema-past-float-range` case survives, because the schema check refuses that value anyway. |
| 2. Drop the sentinel escaping in `certificate()` | 10 / 10: `test_a_quoted_sentinel_is_accepted_as_written_and_certifies` (6 cases), `test_replay_rebuilds_a_certificate_whose_verdict_quotes_a_sentinel`, `test_a_round_whose_peer_quotes_a_sentinel_agrees_and_issues_its_certificate`, `test_the_same_peer_output_gives_the_same_round_result_and_certificate_bytes`, `test_accepted_implies_certifiable` |
| 3. Escape at parse time instead (stored strings rewritten; certificate unescaped) | 10 / 10: `test_a_quoted_sentinel_is_accepted_as_written_and_certifies` (6), `test_a_sentinel_quoted_in_a_member_name_certifies`, `test_a_round_whose_peer_quotes_a_sentinel_agrees_and_issues_its_certificate`, `test_accepted_implies_certifiable`, `test_head_refuses_every_text_the_base_refuses` |
| 4. Allow a second format re-ask in one round (`_plan_reask`'s spent check disabled) | 3 / 3: `test_two_format_failures_block_the_round_as_before`, `test_c23_9_cli_blocks_after_two_format_failures`, `test_a_number_past_float_range_never_lets_the_gate_agree_on_it` |

## Commands and results

Environment: `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `PYTHONPATH` unset. Python 3.15.2, Hypothesis 6.168.1, pytest 9.1.1, pytest-xdist 3.8.0. Every run used `--basetemp` in a new `mktemp -d` folder and at most `-n 4` (`.scratch/pt.sh`). The machine was heavily loaded: load average 177 at the start, 26–70 later, and disk 99% full.

| Command | Result |
|---|---|
| `git update-index -q --refresh` | ok; took 1 min 51 s under load |
| `uv sync --group dev` | ok |
| `uv run --no-sync --with 'pytest-xdist>=3.6' python -c "import xdist"` | `ModuleNotFoundError`: uv 0.11.7's overlay did not expose the cached environment. Installed `pytest-xdist` into the worktree's `.venv` with `uv pip install --python .venv/bin/python 'pytest-xdist>=3.6'` and ran `.venv/bin/python -m pytest` |
| `python -I .scratch/repro.py <base export>` / `<worktree>` | the reproduction table above |
| D-GATE-CERT1 tests before the port (`test_gate_cert_roundtrip.py`, `test_gate_replay.py`, `test_gate_service.py`, `test_gate_verdict.py`) | 135 passed |
| `test_gate_cert_properties.py` before the port | 8 passed |
| All gate files, first run after the port | 51 failed, 392 passed. Causes: the missing `timers` stand-in (re-ask tests); three subprocess timeouts (3 s, 15 s, 20 s) in real-daemon and fixture-child tests at load ~70–177; one Hypothesis slow-generation health check; the linearity bound |
| `test_gate_reask.py` after the `timers` stand-in | 90 passed |
| `test_gate_reask_properties.py` | 3 passed, 1 failed (Hypothesis health check `too_slow`: 3 inputs in 2.1 s under load); passed in the later runs below |
| `test_gate_verdict.py`, `test_gate_reask_properties.py`, new equivalence property, after the fast path | 138 passed, 1 failed (the same health check, one 4.6 s draw) |
| Mutants 1–4 | killed (table above) |
| Gate files not rerun since the port (`admission`, `merge`, `replay`, `revision`, `service`, `cert_properties`) | 156 passed |
| Step 3 (`test_state_contract.py`, `test_test_module_names.py`, `test_invariants_index.py`, `test_handler_refusals.py`, `test_protocol_registry.py`), plus the three timed-out real-daemon tests and `test_gate_reask_properties.py` | 434 passed (load ~40) |
| **All gate files in one run** (every `tests/**/test_gate*.py`: `admission`, `merge`, `replay`, `revision`, `service`, `verdict`, `cert_roundtrip`, `cert_properties`, `reask`, `reask_properties`, `fake/test_gate_end_to_end`) | **444 passed** in 71 s |
| `test_c23_9_rejected_output_diagnosis_is_linear` | 12 passed; begin-sentinels 0.07 s |

The new refusal comes from `parse_verdict`, not from a handler, and `test_handler_refusals.py` passes. The new keys in records and events are none: `format_reask` and its members are #178's, and the port's tests compare them exactly.

## What did not run

- The whole suite: CI runs it.
- The three tests that timed out under load (`test_plan_peer_process_finalizes_through_real_daemon`, `test_gate_fixture_child_publishes_only_its_explicit_synthetic_attestation`, #178's real-daemon re-ask test) were not rerun on base 266c3ab8b. All three pass at the head at load ~40. Importing the fixture child alone took 1.3–3.6 s here, against the test's 3 s timeout.
- No live gate, peer, daemon or store, and no `subfleet` command against live state. Every verdict in the tests is synthetic.
- No independent review of this build. The hub's `/sr` or a lane review should read it before it merges.

## Head

The code head is e7ad0109d. The commit adding this report sits on top of it; the final answer names that commit.
