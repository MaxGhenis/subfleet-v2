# Guard sync: the overlay's keychain sentence and the attached-runner denial (2026-11-11)

Brief: `~/reviews/subfleet-hub/guard-sync-2026-11-11/brief-build-r1-filled.md` (build r1). An in-session Opus agent ran it in place of lane job 20261111-110910-gs-build-r1. It worked in the worktree `~/subfleet-v2-lanes/guard-sync`, on branch `fix/guard-sync-217`, from `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d` (`origin/release/217`).

Start: 2026-11-11T18:19:54Z (`date -u`, first command). Status: **partial**. Changes 1 and 3 are done, tested and mutation-checked. Change 2 stopped as the brief directs: the repository holds no copy of the two Axiom blocks, so the hub has to supply their text. Only part of test step 2 ran (see Commands). **r2 (2026-11-14): change 2 is done**: the two Axiom blocks are in the overlay, and the pins, tests and mutants are done. Some r2 test runs stopped at their wall caps; see "r2: the two Axiom blocks".

## The changes

### 1. Keychain-dump sentence, overlay side (done, commit a5c4b8f9d)

- `private/guard/never-rules-hook.sh:334`: the `[keychain-dump]` block's `block "…"` line now ends with the brief's sentence. The edit touches nothing else in the hook (`git diff` shows one line). The rule's logic stays the same.
- The hook's SHA-256 moved from `e869dd4c…dfed` (r12d) to `34682488eea6a6ef8049407e1c2ecc24fbef3e9eb399236f072f4f7e9e0f3983`.
- `private/guard/TRUST`:
  - `hook_sha256` now carries the new hash.
  - One dated provenance sentence comes after the existing provenance. It ends, as the pin test requires, "installed only after the hub's review and a live hooks/list preflight."
  - `hooks_trust_hash` and `override` stay the same, because they hash the hook's identity, not its bytes (`test_identity_hash_and_override_do_not_depend_on_the_hook_bytes`).
  - The TRUST SHA-256 is now `680f864e8eea198099b89570914af8348203d53663d5c7b2f80ad193f86b12d9`.
- Pins updated to the new pair:
  - `tests/unit/test_guard_overlay.py:173-174`, plus its docstring: "five dated provenance additions" and a guard-sync sentence.
  - `tests/unit/test_guard_subfleet_root.py`:
    - `R12D_HOOK_SHA256` keeps the old hash and `NEW_HOOK_SHA256` takes the new one.
    - `GS_KEYCHAIN_OLD`, `GS_KEYCHAIN_NEW` and `GS_TEXT` form a new undo step, prepended to `R3_TEXT`. With it, `rebuild_r2` and `rebuild_old` still hash to `R2_HOOK_SHA256` and `OLD_HOOK_SHA256`.
    - S3 (`test_s3_other_rules_give_identical_output`) labels the keychain-dump sentence as an intended difference. The module docstring's S3 paragraph says so.
  - `tools/guard_subfleet_root_mutations.py:38` (`NEW_HOOK_SHA256`).
  - `docs/private-guard-overlay.md`: the policy revision's hash table, the provenance count, and one guard-sync paragraph. That paragraph sits above the table, so the hash stays inside the "## Policy revision" section, as `test_r5_the_overlay_doc_holds_one_copy_of_its_body` requires.

### 2. The two Axiom blocks (stopped: the repository holds no copy)

`[axiom-brief-regime]` and `[hand-authored-rulespec]` appear nowhere in the repository.

- `git grep` of the worktree found no match.
- `git grep` of four guard branch tips found no match either: `fix/private-guard-overlay`, `fix/guard-subfleet-root-217-r4v`, `subfleet-archive/20261028-001405-guard-fix-r6/4c4f0639…` and `fix/guard-preflight-diagnostics`.
- A `git log --all -S'axiom-brief-regime'` over 7,640 commits ran for more than 400 s. The tool moved it to the background, and I stopped it to keep the no-background rule, so that search gave no answer.

What the repository pins:
- The v1 drift test the overlay header names, `tests/test_guard_never_rules.py::DriftTests` (per-rule byte identity, the prefilter literal, the hf-dest literals), is not in this repository. It lived in v1 (`~/chief-of-staff/subfleet/tests/`).
- Here, the only pinned copy of the Claude hook's blocks is the overlay itself. `test_guard_overlay.py` and TRUST pin it by SHA-256, and `test_guard_subfleet_root.py` rebuilds the original reviewed hook `a60d1c51…`, the 2026-09-05 byte-for-byte v1 copy.
- The new `DriftTests` (below) checks any block it finds in `tests/fixtures/guard/claude-blocks/<rule>.sh` against the overlay.

What the hub needs to supply:
1. The two blocks from `~/.claude/hooks/guard-never-rules.sh`, each cut from its column-0 `# --- <rule>:` line up to the next column-0 `# --- ` or `# >>>` line.
2. The Claude hook's current prefilter line. The overlay's first prefilter literal must stay byte-identical to it, and a block whose trigger word misses the prefilter never runs.
3. The Claude harness's cases for both rules (`~/.claude/tests/guard-never-rules-test.sh`).

With those, a follow-up build would:
- drop each block in as `tests/fixtures/guard/claude-blocks/<rule>.sh`, which `DriftTests` picks up with no code change;
- paste each block into the overlay in the Claude hook's order;
- update the first prefilter literal and its drift note;
- add each rule to `RULE_BLOCKS` and to the header's rule list;
- add the decision cases;
- move the hashes in TRUST, the pins, the mutation tool and the overlay doc, and add an undo step to `test_guard_subfleet_root.py`.

### 3. The attached-runner denial (done, commit d36838f8f)

`subfleet/hooks.py:758-765` adds `attached_denial(offender)`. It builds the brief's text with `'|'.join(TASK_CHOICES)` and `'|'.join(TIER_CHOICES)`, imported from `subfleet.cli` inside the function. `pre_tool_use` (`:785`) calls it.

- The hook runs through `subfleet.compat:dispatch` → `cli.main`, so `cli` is already loaded when the import runs. The import is lazy because `cli` imports `hooks` lazily too, and a lazy import keeps the module graph acyclic.
- The `{offender}` substitution, the JSON shape and the return code 0 stay the same.
- For `codex exec`, the denial now reads: `codex exec runs attached to this session and can die when it restarts. Use subfleet run --task lookup|research|sweep|review|build|authored-prose|strategy|adjudication --tier trivial|easy|standard|hard -C <dir> -p prompt.md -o out.md, then arm subfleet wait <job-id> as a background task. For an intentional short attached call, prefix that command with SUBFLEET_ATTACHED_OK=1.`

### Tests (commit 9f32c6ef0)

- `tests/unit/test_guard_sync.py`, new: the three invariants below.
- `tests/fixtures/guard/claude-blocks/keychain-dump.before.sh` (SHA-256 `85ba8c86856dde8d6d2e081cf20b2bf59c28f15fcd726902660dc964006ce394`) and `keychain-dump.sh` (`b7e8c83695a309ace6de9acdb34e63b4e23d74480034751749e08512ea80e818`): the pinned Claude block before and after the install edit.
- `tests/unit/test_guard_subfleet_root.py::test_gs_the_pinned_claude_keychain_block_is_the_reviewed_hooks`: the "before" fixture equals the keychain-dump block of the original reviewed hook (`hooks["old"]`, a60d1c51…) and of the r2 rebuild. The overlay's block equals that block with only the sentence replaced.

## Where the pinned Claude text came from

The brief bars reading `~/.claude`, so the Claude block comes from the repository's pinned copy. That copy is the overlay's `[keychain-dump]` block at `b51f08f96` (hook `e869dd4c…`, pinned in `test_guard_overlay.py` and TRUST), cut by the header's sync contract. The new subfleet-root test checks that it equals the same block in the rebuilt original reviewed hook `a60d1c51…`. The overlay header's contract says that hook copied the Claude hook's seven Bash blocks byte for byte on 2026-09-05.

One independent observation backs the old sentence. On 2026-11-11, between about 18:44Z and 18:52Z, the live Claude hook denied two of this session's own Bash commands, because the text of each held the rule's literal (the commands only fed strings to tests). The denial message's last sentence was exactly `GS_KEYCHAIN_OLD`. That checks the sentence, not the whole block.

## The Claude-side edit the hub makes at install

In `~/.claude/hooks/guard-never-rules.sh`, inside the `# --- keychain-dump:` block's `block "…"` line, replace exactly:

- old: `Use 'agent-secret' / 'security find-generic-password -s <item>' for specific secrets.`
- new: `Read one secret with 'agent-secret get <service>'; 'agent-secret search <query>' finds its name. A bare 'security find-generic-password' on the agent-secrets keychain raises a password dialog while that keychain stays locked.`

Then:
1. Run `~/.claude/tests/guard-never-rules-test.sh`. Update any harness case that pins the old sentence.
2. Cut the block out of the Claude hook and check it against `tests/fixtures/guard/claude-blocks/keychain-dump.sh` byte for byte. Its SHA-256 should be `b7e8c83695a309ace6de9acdb34e63b4e23d74480034751749e08512ea80e818`.
3. Stage the new overlay pair into a fresh state root and swap it in, as `docs/private-guard-overlay.md` describes, after a live `hooks/list` preflight.

The pair is hook `34682488…3983` and TRUST `680f864e…12d9`. The rollback is the r12d pair: hook `e869dd4c…dfed`, TRUST `36267abb…afc0`.

`subfleet/hooks.py` reaches users through the next Subfleet release install; it needs no separate step.

## Contract

`docs/acceptance-contract.md`: no clause covers the guard's rule list or the hook's denial text, so I amended nothing.
- C-14.1 covers the private pair's bytes and pins as a class. D-LANE-SCOPE1's eight revisions changed the same bytes without amending it.
- C-23.48 covers what the session hook counts as a launch and the `SUBFLEET_ATTACHED_OK=1` override, not the denial's wording.

`tests/unit/test_invariants_index.py` passes (25).

## Invariants and their tests

All of these are in `tests/unit/test_guard_sync.py` unless named otherwise.

1. **Byte identity.** Each pinned Claude block sits in the overlay exactly once, byte for byte, cut by the sync contract.
   - `DriftTests` (a `unittest.TestCase`, so pytest collects it under that name):
     - `test_every_pinned_block_is_in_the_overlay_byte_for_byte`;
     - `test_the_overlay_keeps_its_seven_rule_blocks_in_order`;
     - `test_the_pinned_blocks_differ_by_the_one_sentence` (both fixture SHA-256s, one changed line);
     - `test_the_old_sentence_is_nowhere_in_the_overlay`.
   - In `test_guard_subfleet_root.py`: `test_gs_the_pinned_claude_keychain_block_is_the_reviewed_hooks`.
2. **Same decisions.** The overlay hook (fed a Bash payload, under an empty `CODEX_GUARD_TEST_FSROOT`) and the pinned Claude block (run alone by a harness that reads the command on stdin and defines `block` as the deny) reach the same decision with the same reason. Both keep the old block's decisions, with only the sentence swapped.
   - `test_same_decisions_overlay_and_pinned_claude_block` runs 10 cases:
     - the subfleet-root harness's own two: `OTHER_RULES`' deny and its `find-generic-password -s x` near miss;
     - five more denials: `login.keychain-db`, the `~/Library/Keychains/login.keychain-db` path, `/usr/bin/security … -a -d`, and prose naming the rule with ` -d`, a known over-match both sides share;
     - four more allows: no `-d`, an `agent-secrets.keychain-db` path, `-dx`, and `-d;`.
   - `test_same_decisions_on_generated_keychain_commands`: Hypothesis, 40 commands built from the rule's words.
3. **The denial names every task and tier.**
   - `test_denial_text_is_the_hubs_exactly`: the brief's text byte for byte, for all nine offender forms.
   - `test_denial_names_every_task_and_tier_once`: each `TASK_CHOICES` and `TIER_CHOICES` member exactly once after the offender, the `--task` and `--tier` alternations in the CLI's order, the backgrounded wait, exit 0.
   - `test_denial_follows_the_cli_choices`: a monkeypatched ninth task and fifth tier appear.
   - `test_denial_names_every_task_and_tier_for_generated_offenders`: Hypothesis, 200 launches with env and wrapper prefixes, a path before the binary and extra arguments.
   - `test_an_override_or_a_detach_still_passes`.

## Mutants

I applied each mutant alone, ran the tests, restored the file from a pristine copy, and confirmed `git diff --quiet` on the file after each restore. Script: `~/reviews/subfleet-hub/opus-burn-2026-11-11/runs/briefs-gs/gs-build-evidence/gs_mutants.py`.

| Mutant | Killed by |
| --- | --- |
| 1. Old keychain sentence left in the overlay | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte`, `DriftTests::test_the_old_sentence_is_nowhere_in_the_overlay`, the 5 deny cases of `test_same_decisions_overlay_and_pinned_claude_block`, `test_guard_overlay.py::test_private_overlay_bytes_remain_the_original_reviewed_pair` (8 kills) |
| 2. Keychain block dropped | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte`, `::test_the_old_sentence_is_nowhere_in_the_overlay`, `::test_the_overlay_keeps_its_seven_rule_blocks_in_order`, the 5 deny cases, the overlay pin (9 kills) |
| 3. One byte changed inside the block (`head -1` → `head -2`; no case's decision moves) | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte`, `test_guard_overlay.py::test_private_overlay_bytes_remain_the_original_reviewed_pair` |
| 4. Three tasks hard-coded in the denial again | `test_denial_text_is_the_hubs_exactly`, `test_denial_names_every_task_and_tier_once`, `test_denial_follows_the_cli_choices`, `test_denial_names_every_task_and_tier_for_generated_offenders` |

Only change 1's keychain block counts as a ported block, since change 2 never started. So mutants 2 and 3 act on that block.

## Commands and results

Environment:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, scratch `$TMPDIR/sf-gs-b1`.
- A fresh `--basetemp` under it for each run, and `-p no:cacheprovider`.
- `.venv` uses CPython 3.15.2.
- Host load averages ran from 170 to 180 throughout.

| Command | Result |
| --- | --- |
| `git branch --show-current; git rev-parse HEAD; git status --porcelain` | `fix/guard-sync-217`, `b51f08f961e1…`, clean |
| `df -k /System/Volumes/Data` | 89,945,684 KiB available (about 92.1 GB) |
| `uv sync --group dev --extra api` | error: extra `api` is not defined |
| `uv sync --group dev` | ok |
| `git log --all --oneline -S'axiom-brief-regime'` | more than 400 s; moved to the background by the tool, stopped (no result) |
| `git grep -l -e axiom-brief-regime -e hand-authored-rulespec <4 guard refs>` | no match |
| `pytest tests/unit/test_guard_sync.py`, first run | 11 failed, 9 passed: the C-20.6 keychain exec guard refused the harness's `bash -c` launch, then its `/usr/bin`-first PATH. Fixed to a script file on stdin under the session's PATH |
| `pytest tests/unit/test_guard_sync.py` | **20 passed** (96.6 s) |
| `pytest tests/unit/test_guard_subfleet_root.py -k 'test_gs_ or trust or test_only_the_roots or test_s3_other_rules or overlay_doc'` | **7 passed**, 154 deselected (205.9 s) |
| `python gs_mutants.py` | 4 of 4 mutants killed, tree clean after each |
| Step 3: `tests/fake/test_state_contract.py` | 37 passed |
| Step 3: `tests/unit/test_test_module_names.py` | 1 passed |
| Step 3: `tests/unit/test_invariants_index.py` | 25 passed |
| Step 3: `tests/unit/test_handler_refusals.py` | 362 passed |
| Step 2: `tests/unit/test_guard_overlay.py` | 22 passed |
| Step 2: `tests/unit/test_hooks_native_guard.py` | 18 passed |
| Step 2: `tests/unit/test_hooks.py` | 81 passed |
| Step 2: `tests/unit/test_guard_trust.py` | 105 passed |
| Step 2: `tests/unit/test_cli_batch.py` | 18 passed |
| Step 2: `tests/unit/test_route_check.py` | 24 passed |
| Step 2: `tests/unit/test_policy_support.py` | 14 passed |
| Step 2: `tests/unit/test_keychain_exec_guard_properties.py` | 3 passed |
| Step 2: `tests/unit/test_doctor.py` | 47 passed |
| Step 2: `tests/unit/test_sessions_handoff.py` | 145 passed |
| Step 2: `tests/unit/test_conversation_handoff_briefs.py` | 28 passed |
| Step 2: `tests/unit/test_compat.py` | stopped by my 400 s wall cap at 98%: 1,584 results, all passes, no F or E. It matches the step-2 grep only through the words "attached short values" |
| Step 2: `tests/unit/test_guard_subfleet_root.py`, whole file | stopped by my 550 s wall cap after 33 results: 31 passed, 2 skipped, 0 failed. The 8 tests this build touches passed in the `-k` run above |
| Step 4: `git grep` for the old denial text and the old keychain sentence in `tests`, `tools`, `subfleet` and `docs` | no test pinned either text. Only the new tests and the `GS_*` constants name them |

Each row's runner: `gs_run.py` (one pytest per file, junit XML, a wall cap). Rows: `gs-build-evidence/results.tsv`.

Not run from step 2's 69 files, which CI runs: the other 55 files, among them `tests/e2e`, `tests/fake`, `tests/frontend`, `tests/live`, the `test_live_store_guard*` and `test_launch_env_round*` series, `test_codex_shim.py`, `test_claude_cards_wiring.py`, `test_legacy_hold.py` and `test_soak_tools.py`. Most of them match the grep only through the words "keychain" or "attached" in unrelated code.

## Deviations

1. **Change 2 not done.** The brief directs this when the repository holds no copy of the blocks. The hub supplies their text; the list is above.
2. **`--extra api`** does not exist in `pyproject.toml`. I used `uv sync --group dev`.
3. **No pytest-xdist.** The dev group lacks it, so `-n` is unrecognized and every run was serial. The host limit (at most `-n 2`) holds regardless.
4. **Step 2 is partial:**
   - 14 of 69 files ran: 12 complete and 2 stopped by my wall caps.
   - Each tool call ends at 600 s, and a longer command gets moved to the background, which this run must not start.
   - A serial whole-file run of `test_guard_subfleet_root.py` takes far more than 550 s at load 175.
   - Two commands did get moved to the background: a history search, and a first chunk run whose `perl alarm` pytest-timeout's SIGALRM handler swallowed. I stopped both with TaskStop and confirmed with `pgrep` that no process was left. Later runs used `--timeout-method=thread`.
5. **Pinned Claude text.** The Claude text came from the repository's pinned overlay copy and the rebuilt reviewed original, since I could not read `~/.claude`. `DriftTests` lives in a new file (`tests/unit/test_guard_sync.py`), not the v1 file the overlay header names, which is not in this repository.
6. **TRUST provenance.** TRUST gained one provenance sentence, as each earlier dated revision did. The brief did not ask for it, but `test_trust_pins_the_new_hook_bytes` reads the provenance's ending.

## Folders outside the job's scratch

- `$TMPDIR/sf-gs-b1` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-gs-b1`) held the basetemps, mutant pristine copies, junit XML and logs. I copied `results.tsv` and the two scripts to `~/reviews/subfleet-hub/opus-burn-2026-11-11/runs/briefs-gs/gs-build-evidence/`, then deleted the folder with a guarded `shutil.rmtree` that asserts `sf-gs-b1` is in the path.
- `$TMPDIR/uv-cache` is the shared uv cache the brief names. I kept it.
- `.venv` in the worktree is git-ignored. I kept it for the hub's re-runs.
- The scripts' originals sit in this session's scratchpad.

Head built: see the commit that adds this report (`git log -1` on `fix/guard-sync-217`). The code head under test is `9f32c6ef003a10e1703d8e1c53c41a869a32a5e6`.

## r2: the two Axiom blocks

Brief: `~/reviews/subfleet-hub/guard-sync-2026-11-11/brief-build-r2-filled.md`, finished under `brief-continue-20261113.md`. Two sessions did the work:
- **2026-11-12**, an in-session Opus build. It started at 15:46:15Z and a usage limit cut it off. It made commits `c0fa5d1e5`, `de5f8749a` and `f9c83e05a`, and left an uncommitted test file (`wip-20261112/tracked.patch`).
- **2026-11-14**, lane job 20261113-092038-gs-build-r2c, in a detached checkout at `f9c83e05a`. It started at 2026-11-14T20:45:06Z (`date -u`, first command).

Status: **done; some test runs incomplete.**
- All five changes are in, and all four mutants are killed.
- Step 2's four files passed in full.
- Step 1's file passed in full (51) on the WIP bytes. Those bytes differ from the committed file only in its docstring.
- At head, a 560 s wall cap stopped the re-run of step 1's file, step 3's `-k` run and step 4, at load 140-250. Each had no failure when stopped (see Commands). CI runs them.

### Supply check

At 20:45Z each supplied file matched:

| File | SHA-256 |
| --- | --- |
| `axiom-brief-regime.sh` | `77002282d3c865f10e1f640595ebcbd9a244a024d97e8df97dba9496941bd571` (brief's value) |
| `hand-authored-rulespec.sh` | `f870acdf650677c3fea2856e1b1044668bad043152b9f7273099353c3595558c` (brief's value) |
| `claude-prefilter-line.txt` | `c46d8eec748c649daaae06b0982bb5fc0cf6011918a35144d0ea1de06596781f` (recorded) |
| `claude-harness-excerpt.sh` | `54614037662ce2b2f94827edd4a51f55cadbcce6bf4f0955f33526f47316aa51` (recorded) |

`cmp` shows the three fixtures byte-identical to the supply: the two blocks and `prefilter.txt`.

### The WIP patch

- `git apply` succeeded. `tests/unit/test_guard_sync.py` then matched `SHA256SUMS` (`5cdf815b…f67179`).
- I read it, and it passed in full as-is (51 tests).
- I changed two things: the docstring's invariants were out of order (I1, I2, I4, I3), and its r2 bullet was dated 2026-11-12. Both edits went into the commit that adds it (`65b1ee2e5`).

### The changes

1. **Fixtures** (`c0fa5d1e5`, 2026-11-12).
   - Added `tests/fixtures/guard/claude-blocks/axiom-brief-regime.sh` (21 lines) and `hand-authored-rulespec.sh` (29 lines), byte for byte.
   - `DriftTests` picked them up with no code change, according to the earlier session's notes: before the overlay edit it failed `axiom-brief-regime 0 != 1`. I did not re-run that state.
2. **The overlay** (`de5f8749a`, 2026-11-12). Both blocks were pasted byte for byte into `private/guard/never-rules-hook.sh`:
   - `[axiom-brief-regime]` at `:357-377` and `[hand-authored-rulespec]` at `:378-406`;
   - after trust-root (`:350`) and before corpus-squash (`:407`), which is the Claude hook's order.
   - The blocks run unmodified. They use `$command`, `$cwd`, `$HOME`, `block`, `git`, `grep`, `sed`, `awk` and `head`, and the overlay defines each the same way (next section).
   - Under the overlay's `set -u` every variable the blocks read is assigned first.
   - `done < <(…)` needs bash, and the overlay runs as `/bin/bash`.
3. **The prefilter** (`f9c83e05a`, 2026-11-12).
   - The first literal (`:326`) is again the Claude hook's line 138, up to its `|| exit 0`. It is pinned as `claude-blocks/prefilter.txt`.
   - The drift note at `:312-325` says what the re-copy adds.
   - The header also changed: `:3` eleven rules, `:8` nine Bash rule blocks, `:16-20` the drift note, `:70-73` the two rules. So did `:136` and `:2879` ("nine").
4. **Rule lists** (`65b1ee2e5`).
   - `RULE_BLOCKS` in `tests/unit/test_guard_sync.py` now holds nine rule blocks in the Claude hook's order.
   - r1's seven-block order test is now `test_the_overlay_keeps_its_nine_rule_blocks_in_order`.
5. **Pins** (`17e70955e`).
   - `private/guard/TRUST`: the new `hook_sha256`, plus one provenance sentence dated 2026-11-14 that ends "installed only after the hub's review and a live hooks/list preflight." `hooks_trust_hash` and `override` did not move.
   - `tests/unit/test_guard_overlay.py`: the pair, and its docstring ("six dated provenance additions").
   - `tests/unit/test_guard_subfleet_root.py`:
     - `GS_HOOK_SHA256` keeps r1's hook and `NEW_HOOK_SHA256` takes the new one.
     - `GS2_TEXT` is the undo step, run ahead of `GS_TEXT`. It removes the two blocks (read from the fixtures) and restores 8 r1 texts: the prefilter line and its comment, the header's counts, its rule list and its drift note. The pairs were generated from `git diff 4a6482250` and checked to give r1's bytes exactly.
     - With the step, `rebuild_r2` and `rebuild_old` still hash to `R2_HOOK_SHA256` and `OLD_HOOK_SHA256`.
     - The new `test_gs_undoing_the_r2_step_gives_the_guard_sync_hook` checks that the step alone gives r1's hook.
   - `tools/guard_subfleet_root_mutations.py:38`.
   - `docs/private-guard-overlay.md`: one r2 paragraph, the table, and "six … the guard sync's two".

The re-copied prefilter adds more than the two Axiom terms. It also adds the words of Claude-only rules (`pending_deployments`, `deployment_protection_rule`, `approveDeployments`, `pkill`, `killall`, `pgrep`) and a wider searcher term (`gfind|bfs|fd|fdfind|…|ugrep`, bounded by `[^[:alnum:]_.-]`). The old term admitted a subset of what the new one admits.
- No overlay block reads those words, so such commands now reach the rule blocks and the unscoped-search region, which judges its own searcher list.
- The brief's byte identity requires this.
- I did not measure the extra hook cost on the commands it now admits. Every `git commit` and `pgrep` now runs the full Bash branch.

### How `$command`, `$cwd`, `$HOME` and `git` behave in a lane

- **`$command` and `$cwd`.** The overlay reads `.tool_input.command` and `.cwd` from the payload with jq (`:131-134`), the fields the Claude harness's `run_bash` sets. The tests' Claude-side harness reads them the same way. `$cwd` is the Codex turn's cwd. Each block falls back to it when the command has no `-C`.
- **`$HOME`.** The overlay keeps the inherited `HOME` (`:112`, `: "${HOME:=/var/empty}"`).
  - A Codex lane's launch environment is the allowlisted ambient environment. `HOME` is in `OBSERVED_NAMES` (`subfleet/launch_env.py:19-25`).
  - The Codex adapter then adds `CODEX_HOME`, `SUBFLEET_ATTEMPT` and `SUBFLEET_JOB` but never sets `HOME` (`subfleet/adapters/codex.py:609-612`).
  - So `~` in `-C ~/…`, `-p ~/…` or `git -C ~/…` expands to the daemon's home, as it does in a Claude session.
  - If `HOME` were unset, `~` would become `/var/empty`. `[axiom-brief-regime]` would still match the word `TheAxiomFoundation` in a `-C` path, but would miss a `-p ~/…` brief. That is a fail-open the Claude hook shares only if its own `HOME` is unset.
- **`git -C "$gdir"` in a lane.**
  - The overlay header (`:94-95`) says the hook runs on the host, outside the sandbox.
  - Four existing blocks already run `git -C "$cwd"`: corpus-squash, corpus-push, stash-shared and local-main (`:410-465`).
  - TRUST's provenance records two live lane turns, under codex 0.158.0 and 0.159.0, that `[stash-shared]` denied in a two-worktree scratch repo. That rule denies only when `git -C "$cwd" worktree list` (`:449`) prints more than one line, so a lane's hook has run `git -C` on its cwd.
  - The two new blocks only read. They run `remote get-url`, `cat-file -e` and `diff --cached --name-only`, and `grep` the brief file. Nothing in either block writes, so a read-only sandbox would not stop them either.
  - `git` resolves to `/usr/bin/git`, because the hook puts `/usr/bin:/bin` first (`:110`).
  - Not checked: a live lane run of these two blocks. The brief bars running the installed guard or `subfleet` against live state.

### Invariants and their tests

All of these are in `tests/unit/test_guard_sync.py`.
1. **Byte identity.**
   - `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte`: keychain-dump, axiom-brief-regime and hand-authored-rulespec, each cut by the sync contract.
   - `::test_each_pinned_block_occurs_once_in_the_overlay`: a raw byte count of 1 for each, plus the two Axiom fixtures' SHA-256.
   - `::test_the_first_prefilter_literal_is_the_claude_hooks_line`.
2. **Same decisions.** Both sides read the same Bash payload, the overlay hook (under an empty `CODEX_GUARD_TEST_FSROOT`) and the pinned block alone under a harness whose `block` prints the reason. Each case has its own temporary HOME and git fixtures, rebuilt from the harness excerpt's lines 15-42.
   - `test_same_decisions_axiom_brief_regime`, 13 cases:
     - the harness's 4;
     - a lower-case `encoding` only brief (denied);
     - a brief path relative to `-C` (allowed: both sides read a relative `-p` against the payload cwd, never `-C`);
     - a `subfleet run` with no `-p` (allowed);
     - the regime in another case (denied);
     - a relative `-p` found in the cwd (denied);
     - no `-C` with the cwd inside `TheAxiomFoundation` (denied);
     - a `-p ~/…` brief (denied);
     - a missing brief (allowed);
     - `cd <dir> && subfleet run` (allowed: the payload cwd decides).
   - `test_same_decisions_axiom_hand_authored_rulespec`, 15 cases:
     - the harness's 5;
     - a `git -C ~/…` form (denied);
     - a `.yml` file (denied);
     - a `regulations/` path (denied);
     - a manifest in HEAD (allowed);
     - only the files without a manifest named, in git's order;
     - a non-rulespec origin (allowed);
     - `-C` to a missing folder (allowed);
     - paths outside `<cc>/<kind>/` (allowed);
     - `git add … && git commit` (allowed: the index is read before the command runs);
     - `cd /tmp; git -C <repo> commit` (denied).
   - `test_same_decisions_on_generated_staged_files` (Hypothesis, 40 derandomized examples):
     - staged lists of up to 6 paths: `<cc>/(statutes|regulations|policies|legislation|programs|notes)/…`, with `.yaml`, `.yml`, `.test.yaml`, `.json`, `.md` or `.YAML` endings;
     - `composed/` and `.axiom/` paths, each with or without its manifest staged, plus one manifest in HEAD;
     - each in the cwd, `-C <abs>` and `-C ~/…` forms.
     - Both hooks must name the files an independent Python oracle names, in its order.
3. **Order.** `DriftTests::test_the_overlay_keeps_its_nine_rule_blocks_in_order` checks the nine heads in the Claude hook's order, and that both Axiom blocks sit back to back directly before `# --- corpus-squash:`. The file labels this I4, because r1's I3 is the denial invariant.

The pins:
- `test_guard_overlay.py::test_private_overlay_bytes_remain_the_original_reviewed_pair`;
- in `test_guard_subfleet_root.py`: `test_gs_undoing_the_r2_step_gives_the_guard_sync_hook`, `test_only_the_roots_the_header_and_the_message_changed`, the `hooks` fixture's rebuild hashes, and `test_trust_*`.

### Mutants

Each mutant was applied alone to the hook in a `git archive` copy of `17e70955e`, never the worktree, and the killer tests were run. The hook was then restored from a pristine copy and checked byte-identical before the next mutant.
- Copies: `sf-gs-b2/mut-a` and `mut-b`.
- Selection, in `test_guard_sync.py`, `test_guard_overlay.py` and `test_guard_subfleet_root.py`: `-k "DriftTests or test_private_overlay_bytes or test_gs_undoing or test_only_the_roots"`, plus the rule's `blocked` decision cases for mutants 1-3.
- Script: `gs_mutants.py`.

| Mutant | Result | Killed by |
| --- | --- | --- |
| 1. Drop the hand-authored-rulespec block | 12 failed, 3 passed | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte`, `::test_the_overlay_keeps_its_nine_rule_blocks_in_order`, 6 `hand_authored … blocked` decision cases, the overlay pin, `test_gs_undoing_…`, `test_only_the_roots_…` |
| 2. One byte in axiom-brief-regime (`This brief` → `this brief`) | 12 failed, 3 passed | `DriftTests::test_every_pinned_block_…`, `::test_the_overlay_keeps_its_nine_rule_blocks_in_order` (the adjacency check), 6 `axiom_brief_regime … blocked` cases, the overlay pin, `test_gs_undoing_…`, `test_only_the_roots_…` |
| 3. The `git([[:space:]]+-C…)?[[:space:]]+commit` term left out of the first prefilter literal | 10 failed, 5 passed | `DriftTests::test_the_first_prefilter_literal_is_the_claude_hooks_line`, 6 `hand_authored … blocked` cases (the overlay allows what the Claude block denies), the overlay pin, `test_gs_undoing_…`, `test_only_the_roots_…` |
| 4. Swap the two blocks | 4 failed, 5 passed | `DriftTests::test_the_overlay_keeps_its_nine_rule_blocks_in_order`, the overlay pin, `test_gs_undoing_…`, `test_only_the_roots_…` (no decision moves) |

The same selection on the unmutated copy ("mutant-control") passed 11 of 21 before a cap stopped it. Every killer passed at head in another run: quick-pins, step 2, and the full WIP run of the sync file.

### Commands and results

Environment:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `PYTHONPATH` unset, scratch `$TMPDIR/sf-gs-b2`.
- Each pytest run had a fresh `--basetemp`, `-p no:cacheprovider` and `--timeout-method=thread`.
- The runner (`gs_run.py`) kills the process group at a wall cap.
- `.venv` uses CPython 3.15.2. pytest-xdist is not in the dev group, so every run was serial.
- Load ran from 35 to 254.

| Command | Result |
| --- | --- |
| `shasum -a 256 supply/*`, `wip-20261112/tracked.patch` | all match (above); patch `2da7bab8…` |
| `git apply --check` then `git apply tracked.patch`; `shasum` | applied; `5cdf815b…` matches `SHA256SUMS` |
| `cmp` of the supply against the three fixtures | identical |
| `uv sync --group dev` | ok (51 s) |
| `pytest tests/unit/test_guard_sync.py`, WIP bytes, 21:11-21:21Z | **51 passed**, rc 0, 9 min 41 s at load ~148. It ran past the tool's 580 s limit, so the tool moved it to the background, where it finished; nothing outlived it |
| `gen_undo.py`: the r1 hook from the head hook through `GS2_TEXT` | exact (`34682488…`) |
| a direct import check of `rebuild_old`, `rebuild_r2` and the strict R3 chain | all three hash to `OLD_HOOK_SHA256` / `R2_HOOK_SHA256` |
| Step 2: `test_guard_overlay.py test_guard_trust.py test_hooks_native_guard.py test_keychain_exec_guard_properties.py`, one serial run | **148 passed**, rc 0, 175 s |
| Mutants 1-4 (`gs_mutants.py`) | 4 of 4 killed; each restore identical |
| Step 1 at head: `test_guard_sync.py` | capped at 560 s (load 141): 32 passed, 0 failed. Collection order puts these first: DriftTests 6, keychain 11, all 13 brief cases, 2 hand cases |
| Step 3: `test_guard_subfleet_root.py -k "test_gs_ or trust or test_only_the_roots or test_s3_other_rules or overlay_doc"` | capped at 560 s (load 254): 6 passed, 0 failed. These were `test_gs_the_pinned_…` (the `hooks` fixture built, its rebuild hashes held), `test_gs_undoing_…`, the three `test_trust_*` and `test_only_the_roots_…`. `test_s3_other_rules_give_identical_output` was running at the cap |
| Step 4: `test_state_contract.py test_test_module_names.py test_invariants_index.py test_handler_refusals.py` | capped at 560 s (load 232): 233 passed, 0 failed. In file order that is state_contract 37, module_names 1, invariants_index 25, then 170 of handler_refusals' 362 |
| mutant-control (unmutated copy) | capped at 400 s: 11 passed, 0 failed |
| quick-pins: `test_guard_sync.py` and `test_guard_subfleet_root.py -k "DriftTests or test_gs_undoing or test_only_the_roots or test_trust_pins or test_trust_changed or test_trust_keeps or test_r5_the_overlay_doc or test_denial or test_an_override"` | **17 passed**, rc 0, 94 s |

The step 1, 3 and 4 runs and mutant-control ran at once (four processes) while the host's load rose to about 250; that is why all four hit their caps. Not run to completion at head:
- test_guard_sync.py's last 19 tests (13 hand-authored cases, the staged-file Hypothesis test and the 5 denial tests). They passed in the WIP run, and the 5 denial tests passed again in quick-pins.
- `test_s3_other_rules_give_identical_output`.
- 192 of `test_handler_refusals.py`'s 362.

CI runs them.

### Hash pairs

- **New:** hook `6c1beb64d961b0f0225853c7ec3e2434d90026897e3f2dc9627aab5143634d04`, TRUST `2955f02bc22b88e57ee72fc92e7bef61f07d349f2d53d5d156f908e240f2316c`.
- **Rollback (r1):** hook `34682488eea6a6ef8049407e1c2ecc24fbef3e9eb399236f072f4f7e9e0f3983`, TRUST `680f864e8eea198099b89570914af8348203d53663d5c7b2f80ad193f86b12d9`.
- At install, the hub stages the new pair into a fresh state root and swaps it in after a live `hooks/list` preflight, as `docs/private-guard-overlay.md` describes. The Claude hook needs no edit for r2.

### Contract

No clause in `docs/acceptance-contract.md` lists the overlay's rules, so I amended nothing. C-14.1 covers the private pair's bytes as a class, and C-26.11 covers which turns run the hook.

`docs/invariants.md` row 49 ("Arm the nine NEVER rules") is v1's inventory, citing `bin/subfleet-guard` lines, so I left it. `test_invariants_index.py` passed (25) inside step 4's capped run.

### Review of the earlier session's commits

I checked `c0fa5d1e5`, `de5f8749a` and `f9c83e05a` against the supply and the r1 hook:
- the fixtures and the prefilter fixture are `cmp`-identical to the supply;
- the overlay holds each block once, byte for byte, in the Claude order;
- the prefilter literal equals the Claude line;
- the header's counts are right: nine Bash blocks, plus unscoped-search and hf-dest, make eleven rules.

I found nothing wrong and made no fix commit. Its WIP test file needed the two docstring edits above.

### Deviations

1. **Test runs incomplete.** Steps 1 (at head), 3 and 4 hit their wall caps; what each covered is above. Starting four runs at once during a load spike cost these runs. The brief's 2-hour stop left no time to re-run them.
2. **One run outlived its tool call.** The WIP sync run went past the tool's 580 s limit and was moved to the background. It finished by itself (rc 0) ten minutes after it started, before any other run began. Later runs used a 560 s in-process wall cap.
3. **Concurrency.** Step 2 ran serially in one process, but up to four pytest processes (different steps, and mutants in archive copies) ran at once. That stays within the brief's four-worker cap.
4. **The re-copied prefilter admits more commands.** It carries more than the two Axiom terms (above). The brief's byte identity requires this; the extra cost was not measured.
5. **The provenance date** is 2026-11-14, the build, and it names the brief of 2026-11-11.
6. **No live lane check of the blocks**, which the brief bars; the lane evidence above is from the repository.

### Folders outside the job's scratch

`$TMPDIR/sf-gs-b2` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-gs-b2`) held the basetemps, the two archive copies, the pristine hooks, the junit XML and the logs.
- I copied `results.tsv`, `logs/` and the four scripts to the job's attempt folder, `gs-build-r2-evidence/`.
- Then I deleted `sf-gs-b2` with a guarded `shutil.rmtree` that asserts `sf-gs-b2` is in the path.
- I kept `$TMPDIR/uv-cache` (shared) and the worktree's git-ignored `.venv`.

Head built: the commit that adds this section, on top of code head `17e70955e6efc811987a92af371788e31d9f217f`.

---

## Merge r1: release/217 into #303

Brief: `~/reviews/subfleet-hub/guard-sync-2026-11-11/brief-merge-r1.md`. Lane job 20261114-174616-gs-merge-r1, detached at `1dd95f551`, started 2026-11-14T23:27:53Z. `origin/release/217` was still at `82d3dd819117ce308abb9279f2c518327ef0e602` when fetched, and that commit was merged.

Commits: `349af1bcf` (the merge), `cb118c684` (merge fix), `60d40d028` and `a6991c343` (`tools/gs_mutants.py`), then this report.

### Conflicts

| File | Resolution |
| --- | --- |
| `private/guard/TRUST` | `hook_sha256` = sha256 of the merged hook (below). The other four fields equal release/217's. `provenance` is release/217's with this branch's two dated guard-sync sentences appended. |
| `tools/guard_subfleet_root_mutations.py` | `NEW_HOOK_SHA256` = the merged hook's hash (its `trust-hash-stale` mutant must find it once in TRUST). |
| `tests/unit/test_guard_overlay.py` | Pins the merged pair. Its docstring names D-SEARCH-REL1, then the guard sync and r2, then the merge. |
| `tests/unit/test_guard_subfleet_root.py` | Keeps every real hook as a constant: `R12D` e869dd4c, `SR` af5e7239 (release/217), `GS` 34682488, `GS2` 6c1beb64 (#303). `NEW_HOOK_SHA256` = merged. The undo chain keeps both lines: GS2_TEXT, GS_TEXT, SR_TEXT, then R6_TEXT (the two lines edit disjoint text). |
| `docs/private-guard-overlay.md` | Release's parser section, the guard sync as its own section (it now names the eleven rules, nine of them Bash blocks), and a merged-pair section with the one table. |

`never-rules-hook.sh` and `docs/acceptance-contract.md` merged without a textual conflict.

**The merged hook is the base plus both sides' hunks, and nothing else.** I checked this hunk for hunk: the merged hook minus ours equals release minus base, and the merged hook minus release equals ours minus base. `bash -n` passes.

**Semantic merge fix (`cb118c684`).** `test_guard_search_rel.py`'s `r12d_hook` fixture removes the parser and pinned `R12D_HOOK_SHA256`. On the merged hook that removal gives #303's hook, `6c1beb64`, the hook before the parser on this line.
- R3 compares against that hook over a corpus that includes `security dump-keychain -d`. Against the real r12d it would differ by the guard sync's sentence, which is not a parser difference.
- The pin moved to `GS2_HOOK_SHA256`. The names stay, because test ids and the 2026-11-05 report cite them.
- `test_gs_undoing_the_r2_step_gives_the_guard_sync_hook` now removes D-SEARCH-REL1 first, using the parser tests' own `rebuild_r12d`, and gets GS2. It then undoes the r2 step and gets GS.
- The new `test_merge_undoing_the_guard_sync_gives_the_d_search_rel1_hook` undoes GS2 and GS and gets release/217's hook, af5e7239.

### How the parser and the two blocks interact

They share no state. The Axiom blocks run before the region:
- They read `$command`, `$cwd` and `$HOME`, assign only their own names, and never write `$command` or `$cwd`.
- Release changed only header comments above them.

The parser block reads `$command`, `$cwd`, `$HOME`, `$SHELL`, `us_*` and `pw_*`.
- A script scan found that every shell-level read of `f` or `m` in the region is a function `local`. The other hits are awk variables inside `pw_lib_awk` and `pw_prep_awk`.
- `corpus-push` assigns `seg` and `origin_url` before it reads them.

The parser runs only when `pw_named` sets `us_ask`.
- `pw_named`'s four terms are the prefilter's terms 3 to 6, byte for byte, and neither line changed them. So whatever reaches the parser passes the prefilter under either first literal.
- The wider first literal only lets more non-search commands (`git commit`, `pgrep`, `subfleet run`, `fd`) reach the Bash branch. There they pay the region's walk and the heredoc's parse, but they do not start Python.
- A command an Axiom block denies never reaches the parser, as with every earlier block.

### TRUST

- `hook_sha256` = `ed31bcaca0800e611cda54642abd8245a8316e3fc11638be30cf1effca3d65f3`. It is `hashlib.sha256(HOOK.read_bytes()).hexdigest()`, as `test_trust_pins_the_new_hook_bytes` computes it, and `shasum -a 256` agrees.
- TRUST's own sha256 = `990dcbb8c1eefad6d4db8f710a83f8c41759c0df59d5e4307df74accaca0d31c`.
- `codex_version` stays `codex-cli 0.159.0`.
- `provenance` differs from release/217's value only by the guard sync's two appended sentences. They record the keychain sentence and the Axiom blocks, which are bytes the merged hook carries. Release added no provenance sentence for D-SEARCH-REL1, and I did not add one.

### Contract

The merged `docs/acceptance-contract.md` equals release/217's byte for byte, because this branch never touched it.
- **Clause ids.** 260 `- **C-x.y**` clauses and 262 bold heads. None is duplicated.
- **Change log.** No entry from the base, release/217 or #303 is missing, and release's 5 new entries are present.
- **Already in the base.** One entry appears twice, "C-20.6, C-23.28 (2026-10-15, D-ST1, PR #198 round 8)". Blank lines between entries are normal there (69 of them).
- `merge_contract.py` was not needed.

### Mutants

All 139 mutants of the three tools apply exactly once on the merged hook and change it. This was a static pass over git-archive copies, with no pytest.

| Mutant | Killed by |
| --- | --- |
| gs 1 (drop hand-authored-rulespec) | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte` |
| gs 2 (`This brief` → `this brief`) | `DriftTests::test_every_pinned_block_is_in_the_overlay_byte_for_byte` |
| gs 3 (commit term out of the first prefilter literal) | `DriftTests::test_the_first_prefilter_literal_is_the_claude_hooks_line` |
| gs 4 (swap the two blocks) | `DriftTests::test_the_overlay_keeps_its_nine_rule_blocks_in_order` |
| sr a, b | `test_fallback_a_nonzero_exit_is_no_answer` |
| sr c, d | `test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed` |
| sr e | `test_fallback_an_interpreter_that_cannot_run_is_none` |
| sr f | `test_r5_the_heredoc_holds_the_hub_parser_byte_for_byte` |
| sr g | `test_fallback_no_answer_within_5_s_is_no_answer` |
| sfr trust-hash-stale | `test_trust_pins_the_new_hook_bytes` |
| sfr trust-built-on-repo-copy | `test_trust_keeps_the_installed_lane_pin` |
| sfr r5-doc-duplicated | `test_r5_the_overlay_doc_holds_one_copy_of_its_body` |
| sfr r7-known-form-unlisted | `test_r7_known_differences_name_the_open_forms` |
| sfr r8-known-form-unlisted | `test_r8_known_differences_name_the_stricter_and_open_forms` |
| sfr r12d-xargs-pipe-charge-dropped | `test_r12d_text_under_xargs_costs_steps_of_the_shared_budget` |

Controls passed:
- **gs:** the three named DriftTests.
- **sfr:** the FENCE test and both TRUST tests. Every other named test above also passed on HEAD.
- **sr:** each named test passed in the file runs.

`tools/gs_mutants.py` re-creates the four gs mutants from the r2 table. I ran it `--named-only`, so each mutant ran only its named test, not the r2 table's full kill lists.

The other 122 sfr mutants were not run; there was no time at this load. Each edits only the unscoped-search region, which is byte-identical to release/217's. A region mutant can be killed only by a command that passed release's prefilter. That command passes the merged prefilter too, because the old first literal admits a subset of what the new one admits and terms 2 to 6 are unchanged. No Axiom block fires on a search command. So each such mutant meets the same code on the same commands as on release/217.

### Test runs

All runs used `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, `PYTHONPATH` unset, `-p no:cacheprovider` and `--basetemp $TMPDIR/pytest-gsm-<label>`.
- There were 10 rounds of at most 4 concurrent serial jobs, each capped at 540 s or less by macOS `timeout`. Capped files resumed by deselecting node ids that had already passed.
- Load was 145 to 305 for rounds 1 to 7 and 66 to 141 for rounds 8 to 10. Each job's start and end `uptime` is in the attempt folder's `results.jsonl`.

| File | Result |
| --- | --- |
| `test_guard_sync.py` | **51/51 passed**, over 7 runs. The Hypothesis staged-file test finished alone in 234 s at load 75-79. |
| `test_guard_overlay.py`, `test_invariants_index.py`, `test_test_module_names.py` | **48 passed** in one run at load 247-267 |
| `test_handler_refusals.py` | **372/372 passed**, over 2 runs |
| `test_guard_search_rel.py` | 79 passed and 1 skipped (drift: `SUBFLEET_CLAUDE_UNSCOPED_SEARCH` unset). **Not finished:** R3 and the two R2 corpus tests (R3 was running at the last cap), plus the cost test, which is skip-gated. |
| `test_guard_subfleet_root.py` | 61 passed and 2 skipped (S4, gated the same way). That covers S1 ×2, S2, S3 ×2, S5, every pin, rebuild and TRUST test, both merge tests and the doc tests. **100 of 163 not run.** |
| whole unit suite | not run |

One transient failure: `test_r1_each_interpreter_the_hook_may_take_runs_the_parser[/usr/bin/python3]` failed once at load ~300.
- The parser's loader arms a 5 s alarm, and Apple's Python can miss it at that load.
- A direct differential (`diag_rows.py`: the merged and release/217 hooks on the 17 rows, commands as stdin data) gave both hooks the expected verdict on every row at load 110-150. Single hook runs took 5-60 s.
- The test passed on re-run at load ~110.

### Deviations

- Not run or not finished: as listed above. CI runs the rest.
- I added the semantic fix commit, a test in `test_guard_subfleet_root.py`, and the `--no-control`/`--named-only` flags to the new tool.
- The scripts outside the repository (the runner, the static mutant check, the sfr no-control driver and the row differential) are in the job's attempt folder.

### Cleanup

I removed 11 folders with a guarded `shutil.rmtree`, which accepts only `pytest-gsm-*` names directly under `$TMPDIR`: `pytest-gsm-cache`, `-diag`, `-hr`, `-mut`, `-scratch`, `-sfr`, `-sfrS`, `-small`, `-sr`, `-sync` and `-syncden`. I kept `$TMPDIR/uv-cache` (shared) and the worktree's ignored `.venv`.

---

## Fix round 1

Brief: the hub's fix round 1 for review r1 (`~/reviews/subfleet-hub/guard-sync-2026-11-11/review-r1.md`). Lane job 20261115-100032-gs-fix-r1 ran it in a detached checkout at `abfb48fca`. It started at 2026-11-15T15:35:34Z (`date -u`, first command).

**Status: partial.** Every P1 and P2 finding and both doc P3s are changed, and the new tests pass at the code head. Step 3's four files and `test_guard_search_rel.py` never ran, and the per-test "fails before" evidence is incomplete (see Tests). The report came about 45 minutes after the brief's 3-hour stop (see Deviations).

Commits: `a278c5e1b` (tests), `af95672e1` (the fix), `049071b06` (pins, undo step, mutants, doc), `e908a6892` (a skip for missing folders and the harness timeout), then this report.

### Where the Claude side lives

The repository holds no copy of the Claude hook. Its side is the fixtures under `tests/fixtures/guard/claude-blocks/` and the test harness that runs a fixture block alone. "Both hooks" therefore means:
- the overlay, changed here;
- the Claude hook, whose text this round fixes as fixtures, for the hub to install. I never read or touched `~/.claude`.

The hub's 2026-11-11 cut stays as `axiom-brief-regime.before.sh`, `hand-authored-rulespec.before.sh` and `prefilter.before.txt`. `pinned()` skips `.before` files, as it did for keychain-dump.

### The findings

**P1: git ran a repository's `core.fsmonitor`.**
- **Cause.** `git diff --cached` and `git cat-file -e :<path>` read the index. Reading the index makes git run the configured fsmonitor command, here on the host.
- **Other git calls.** A scratch repo had an fsmonitor, a hooks path, an external diff, a pager and a textconv driver, each writing a marker. Every git call either hook makes ran there with no pin:

  | Call | Configured command run |
  | --- | --- |
  | `diff --cached --name-only` | fsmonitor |
  | `cat-file -e :x` | fsmonitor |
  | `cat-file --batch-check` | fsmonitor |
  | `cat-file -e HEAD:x` | none |
  | `remote get-url` | none |
  | `branch --show-current` | none |
  | `worktree list` | none |
  | `show-ref` | none |

  With the pin, none ran. No call reads an index besides those three, and git never gets a terminal, so the pager cannot run. Only `core.fsmonitor` needs pinning.
- **Change.**
  - Overlay `:126-135`: the brief's line, `export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false`, right after `input=$(cat …)`, with a comment. It sits outside every block.
  - `git-config-pin.txt` (`f653d463…`) is the same line for the Claude hook.
  - The harness now puts it after its own input read. `payload_harness(pin=False)` models today's Claude hook.
- **Limit (checked, not changed).** With both `GIT_CONFIG_PARAMETERS` and the pin set, git read `GIT_CONFIG_PARAMETERS` last, so a fsmonitor set there still ran. The launcher sets that variable, and a lane's commands cannot change the hook's environment.
- **Tests.**
  - `test_no_git_call_runs_the_repositorys_fsmonitor`, 4 cases: the review's two payloads, nothing staged, and the git-using rules after a commit. Two controls write the marker: git's own index read, and the unpinned Claude block. With the pin, both sides leave it absent and the decision stands.
  - `DriftTests::test_the_git_config_pin_precedes_every_git_call_outside_the_blocks`.
  - **Before:** mutant 6 (pin removed) was killed by `[git -C names the checkout]`.
  - **After:** all 4 passed.

**P2: about 0.1 s per staged file.**
- **Cause.** The block made up to four process calls per staged rule file, and two of them were `cat-file`.
- **Change.** The block now takes the staged list once, filters it in one `awk`, and asks one `git cat-file --batch-check` for both the `:<manifest>` and `HEAD:<manifest>` of every file. One `awk` joins the answers in git's order and keeps the old message text. There are three git processes per checkout (`remote get-url`, `diff`, `cat-file`), whatever the index holds.
- **Bound.** A command with more than 16 folders to check is refused with its own reason, and so is one with more than 16 briefs (see the parser finding). A candidate folder that does not exist is skipped before git runs (`e908a6892`); git would have failed there anyway.
  - **Corrected in fix round 2.** The 16 counted path strings, not folders: `cd .; cd ./.; …` made 16 keys for one checkout, and with 3,000 staged files the review's command ran 128-150 s, past the timeout, which allows. Even 16 distinct checkouts would have outlasted it. The bound did not keep the work under the timeout. See "Fix round 2".
- **Tests.**
  - `test_the_staged_file_count_does_not_multiply_git_processes` counts `start` events in `GIT_TRACE2_EVENT` with 2 and 60 staged files. The counts must be equal on each side and at most 4.
  - `test_more_than_16_places_to_check_is_refused`.
  - **After:** both passed.
  - **Before:** not shown by name. Mutant 7 (the supplied block back) was not run.
- **Not measured:** the review's 700-file run against the 60 s timeout.

**P2: parser gaps.**
- **Cause.** Each block found its one `git -C <dir> commit` or `subfleet run` with a regex: the first `-C`, `-p` followed by a space, the last `subfleet run`, and the payload cwd. A relative `-C` was passed as written, so it resolved against the hook process's cwd.
- **Change (both blocks).** Each block now walks the command's words in one `awk`:
  - It joins line continuations and drops quotes and backslashes, which also reads `bash -c "…"` bodies.
  - It splits at `;&|()`, backticks, `{}`, `<>` and newlines.
  - It tracks every `cd`, `pushd` and `popd` target, a relative one resolved against the folder before it.
  - Each `git … commit` or `subfleet run` is judged from every folder seen so far, the payload cwd included.
- **The git commit block** also reads:
  - git's global options before `commit` (`-C`, applied in order; `-c`; `--no-pager`; `-p`; `--git-dir`; and the rest);
  - a `GIT_DIR=` before git;
  - a path to git (`/usr/bin/git`);
  - every commit in the command.
- **The brief block** also reads:
  - attached `-C<dir>`, `-p<file>` and `-p=<file>`;
  - every `-C` and `-p`, where argparse keeps only the last;
  - every `subfleet run` in the command.

  A relative `-C` or `-p` resolves against the folder, as subfleet resolves it.
- **Prefilter.** The first prefilter literal's git term was `git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit`. It is now `git[[:space:]].*(commit|\\$)`: git and commit on one line, or a line that continues. The old term matched a subset of what the new one matches.
- **Tests: a case per form.** All are new rows in `test_same_decisions_axiom_*`, where both sides must agree.
  - The five forms: `git -c … commit`, `--no-pager`, `-C <rs> -c a=b`, `cd <rs> && git commit` and `-p<brief>`.
  - A relative `-C`, judged with the hook process in another folder, and a relative `cd`.
  - `-p` with a path to git, `--git-dir`, `GIT_DIR=`, two commits, `bash -c`, a line continuation, and `git log --grep commit` (allowed).
  - For the brief block: `-p=`, attached `-C`, `-C .`, two runs, two `-p`s, a continuation and `bash -c`.
  - The Hypothesis staged-file test gained the forms `-c`, `cd` and relative `-C`.
  - **After:** all passed.
  - **Before:** the pre-fix run (below) had 8 failures in its first 23 results, with no names printed.
- **Decision changes beyond the five forms.**
  - `cd <briefs> && subfleet run … -p no-regime.md` was a pinned allow (a gap both sides shared). It is now denied.
  - A subshell's `cd`, as in `(cd <rs> && true); git commit`, still counts. This over-approximates, toward denial.
  - A `-p` in a later command is no longer read as the run's brief, which narrows one old over-match.

**P2: stale rollback pair** (`docs/private-guard-overlay.md:633`). The sentence is now marked as the pre-merge rollback and says why that hook is no rollback since. The fix r1 section names the merged pair as this round's rollback.

**P2: no test that the blocks leave later rules' inputs alone (M3).**
- **Test.** `test_later_rules_decide_alike_after_an_axiom_command` runs five later rules: corpus-squash, corpus-push, stash-shared, local-main and unscoped-search. Each runs in its own repo, alone and after each of six allowed Axiom commands, and must give the same reason.
- **M3.** The tool re-creates M3 as mutant 5 and applies it to the head hook. Because `gdir` still holds the last folder after the loop, M3 is not equivalent there. It was killed by `[corpus-squash]`.
- **After:** all 5 passed. Before the fix, M3 was not applied to the review's head.

**P3: the cost sentence** (`:661`). It now says such a command pays for the region's walk, with the review's measurements.

**P3: TRUST provenance for D-SEARCH-REL1. Left for the hub to file.** This round appended only its own sentence.

### Hash pairs

- **New:** hook `324d2a846213bbc621fee618a65d49b4c5015253f973d341d78db214855bccb6`, TRUST `cc26c42108f87085d516fa3e525c17a47720461631065f4886201013e2176293`.
  - `049071b06` pinned an intermediate pair, `51c31b4b…`/`53554448…`. `e908a6892` replaced it.
- **Rollback:** the merged pair, hook `ed31bcac…` with TRUST `990dcbb8…`.
- **Undo step.** `GS3_TEXT` in `test_guard_subfleet_root.py`, each edit found once, gives `ed31bcac` (`test_fix_r1_undoing_its_step_gives_the_merged_hook`). `GS2_BLOCKS` now reads the `.before` fixtures.
- **Search-rel rebuild.** The r12d rebuild in `test_guard_search_rel.py` undoes `GS3_TEXT` first, so R3 also checks that the fix r1 changes no corpus pair's output. It did not run.

### The Claude hook edits the hub makes at install

In `~/.claude/hooks/guard-never-rules.sh`:
1. Add `git-config-pin.txt`'s line (`f653d463…`) right after the input read.
2. Replace the prefilter line (`c46d8eec…`) with `prefilter.txt` (`2acb063e…`).
3. Replace the two blocks (`77002282…`, `f870acdf…`) with `axiom-brief-regime.sh` (`45775b4d…`) and `hand-authored-rulespec.sh` (`0b6b418c…`).

The harness's own nine cases are the first rows of `BRIEF_CASES` and `HAND_CASES`, and their decisions did not change.

### Contract

No clause in `docs/acceptance-contract.md` names the overlay's rules, its git calls or its cost. C-14.1 covers the pair's bytes as a class, as in earlier rounds, so I amended nothing and added no change-log line.

### Commands and results

Environment:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, scratch `$TMPDIR/sf-gs-fix1`, `PYTHONPATH` unset.
- Each pytest run had a fresh `--basetemp …/pytest-*` and `-p no:cacheprovider`. Runs with `-n` went through `uv run --no-sync --with 'pytest-xdist>=3.6'`, at no more than 4 workers in total.
- Load ran from 30 to 225.

| Command | Result |
| --- | --- |
| `uv sync --group dev` | ok, 29 s |
| `git_audit.sh` unpinned, then pinned | the table above |
| `GIT_CONFIG_PARAMETERS` plus the pin | fsmonitor ran (the limit above) |
| Pre-fix, at `a278c5e1b` (hook unchanged): the new and changed Axiom tests, `-n 4` | my 580 s cap at load ~200 stopped it after 23 results: 15 passed, 8 failed, names not printed |
| `smoke.py`, 14 forms, at `af95672e1` | each as intended; 1.2-35.9 s per hook run at load ~160 |
| `test_guard_sync.py`, whole file, `-n 4`, at `af95672e1` | 590 s cap: 73 passed, 1 failed (`[staged rule YAML w/o manifest blocked]`, which passed alone in 88 s) |
| the 13 that had not reported, at `af95672e1` | 11 passed, 2 failed: both were the Claude harness's 60 s subprocess timeout, and Hypothesis called one flaky |
| at `e908a6892`, `-k "DriftTests or hand_authored or fsmonitor or staged or more_than_16 or later_rules"` | 590 s cap: 47 of 49 passed, 0 failed |
| the other 2 of those 49 | 2 passed, 329 s |
| `test_guard_overlay.py`, plus `test_guard_subfleet_root.py -k "test_gs_ or test_merge_undoing or test_fix_r1 or test_trust or test_only_the_roots or overlay_doc"`, at `e908a6892` | **31 passed**, 77 s |
| `tools/gs_mutants.py --only 5-… 6-… --named-only --no-control` | both killed (M3; the pin removed) |

**Not run:**
- step 3's four files;
- `test_guard_search_rel.py`;
- the rest of `test_guard_subfleet_root.py`;
- the other files that import the changed test modules;
- `test_guard_sync.py`'s keychain (11), brief (23) and I3 (5) tests at `e908a6892`. They passed at `af95672e1`, and `e908a6892` changed only the hand block and the harness timeout;
- mutants 1-4 and 7, and the tool's control;
- the 700-file timing.

CI runs the suite.

### Deviations

1. **Past the 3-hour stop.** The mutant call started at 17:38:59Z. The tool moved it to its background mode at its 600 s limit, and it returned at 19:15:57Z: my `timeout 590` did not stop it, and the host probably stalled. The scratch removal also went to the background. Nothing else outlived a call.
2. **The skip and the timeout came after the first runs** (`e908a6892`), and so did the scope beyond the five forms (above).
3. **New denial texts:** the two "more than 16" refusals.
4. **The session's own Claude guard refused one Bash command**, because my test data held `git stash`. It did not run, and I made the edit with the Edit tool.

### Folders outside the job's scratch

- `$TMPDIR/sf-gs-fix1` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-gs-fix1`) held the probes, the logs and the basetemps.
  - Its logs and scripts are copied to the attempt folder's `gs-fix-r1-evidence/`.
  - A guarded `shutil.rmtree` that asserts `sf-gs-fix1` is in the path removed it. It finished at about 19:22Z, after this section's first commit (`bc9c74d72`), and the folder is gone.
- I kept `$TMPDIR/uv-cache` (shared) and the worktree's ignored `.venv`.

Head built: the commit that adds this section, on top of code head `e908a6892`.

## Fix round 2

Brief: the hub's fix round 2 for review r2 (`~/reviews/subfleet-hub/guard-sync-2026-11-11/review-r2.md`). Lane job 20261117-114947-gs-fix-r2 ran it in a detached checkout at `b58d8f977`. It started at 2026-11-17T16:52:04Z (`date -u`, first command).

**Status: partial.** P1 and P2 are fixed in both hooks. Every new test failed before the fix and passed after it. The pins, the undo step and the mutants are written. The suite runs were cut short: load ran from 148 to 394, and every pytest call but three hit my 585 s cap. The mutants, `test_guard_search_rel.py`, most of `test_guard_subfleet_root.py` and step 3's four files did not run (see "Not run").

Commits:
- `52d515f9a`: the tests, before the fix.
- `8d58890f1`: the fix, with a 20 s budget.
- `f7673b439`: the budget cut to 15 s.
- `03a5bb582`: pins, the undo step, mutants and the doc. It pinned an intermediate pair, `56015c96…`/`c1161574…`.
- `fc5d6bc4b`: the first two folders exempt from the budget, re-pinned. This is the code head.
- Then this report.

### Where the Claude side lives

As in fix round 1: the overlay is changed here, and the Claude hook's text is the fixtures under `tests/fixtures/guard/claude-blocks/`, for the hub to install. I never read or touched `~/.claude`. The `.before` cuts are unchanged. Fix round 1's two blocks are kept byte for byte as `claude-blocks/fix-r1/<rule>.sh` (`45775b4d…`, `0b6b418c…`) for the undo chain. `pinned()` globs `claude-blocks/*.sh` only, so it does not read them. `DriftTests::test_the_fix_r1_blocks_are_kept_byte_for_byte_and_are_not_in_the_overlay` pins them.

### The findings

**P1: the 16-place bound counted path strings, not folders.**
- **Cause.** Both blocks keyed a candidate by its joined text. `join` kept `.`, `..`, repeated `/` and links as written, so every spelling was a new key. In the hand block each key cost three git processes and a pass over the whole index.
- **Change, both blocks, both hooks.**
  - The awk drops `.` and empty components from every path. The new `norm` keeps `..`, because only the file system can say where `..` after a link leads. A brief's path keeps a trailing `/`, because only a folder passes it.
  - The shell resolves each candidate folder with `CDPATH= cd -P -- <dir>` and `pwd -P` before git runs.
    - A trailing `echo x` keeps a newline that ends a physical path.
    - If `pwd -P` fails, the path as written is kept.
    - A folder `cd -P` cannot enter is skipped. `git -C` would fail there too.
  - Keys are compared whole, in bash arrays: (folder, `--git-dir`) for the hand block, the resolved file for the brief block. An earlier draft kept a newline-delimited string of keys. I dropped it because a physical path can hold a newline or `\037`, and that could make a real checkout look already seen.
  - Either block refuses more than 64 paths to resolve. This bounds the resolving forks, and it denies, never allows. It did not bound the parse: review r3 found that the `cd` walk split the whole path at every `cd`, so its CPU grew with the square of the chain, before any count was reached (corrected in fix round 3, which bounds it). The brief block still refuses more than 16 distinct briefs, now counted after resolving.
- **The budget (hand block): 15 s.**
  - It never counts the first two folders: the first two distinct (resolved folder, `--git-dir`) keys that exist, in the order the awk meets them. The awk takes each `git … commit` in command order and, for each, every folder met before it (the payload cwd, then each `cd` or `pushd` target in order), with that commit's `-C` words applied. A folder that is not there is skipped and not counted. These are not always the payload cwd and the folder a command names (corrected in fix round 3; see below).
  - Before each git call for a third or later folder, the block denies with its own reason once 15 s have passed since it began. It never allows a folder it did not check.
  - The clock is `$SECONDS` minus its value at the block's start. bash 3.2 imports `SECONDS` from the environment, so only the difference is used.
- **How I chose the number** (hook timings in scratch checkouts with 3,000 staged rule files and their manifests):
  - At a load of 250-285, one git call took 10-30 s. `cat-file --batch-check` over 6,000 manifests took 30.7 s wall for 0.46 s of CPU.
  - At that load the hook took 4.0-5.6 s for `echo hello` and 11.2 s for a lone stash. A searcher took 31.6 s at a load of 168.
  - A budget of 20 s, plus one call, plus the rest of the hook, could pass 60 s. 15 s leaves 45 s for the call in flight and the later rules.
- **Why the first two folders are exempt.**
  - With the 15 s budget counted from the block's start and checked before every call, the harness row "companion test alone allowed" was refused, alone, at a load of about 320.
  - That row is one checkout with one staged file. The parse and the forks alone took 15 s.
  - 13 of the first 20 hand rows failed the same way in a `-n 4` run at a load of 320-354.
  - Exempting two keeps load alone from refusing the common commit: from the payload cwd, or from one `cd` or `-C` target. It does not cover every commit from one checkout, as this line first said. A folder met before the checkout, or a subfolder of it, is a key of its own. So `cd a && cd b && git commit` has three folders (the cwd, `a` and `b`), and under load the budget can refuse it (review r3's case 1; corrected in fix round 3).
- **What the bound guarantees.**
  - Spellings that resolve to one folder cost what one `cd` costs. This holds per folder, not per checkout: a subfolder of a checkout is a folder of its own, with its own git processes (corrected in fix round 3).
  - Folders that are not there cost no git process.
  - Past the first two folders, the block starts no git call once 15 s have passed since it began. So the block's git work ends within the larger of 15 s and the first two folders' cost, plus one git call.
- **What it leaves open.**
  - One or two checkouts whose index alone outlasts the timeout. At a load of 250, one 3,000-file checkout plus the stash rule took the whole hook 44.5 s with 4 git processes.
  - One git call that runs long after the last check.
  - A pathological command that holds 64 lexically distinct `..` spellings pays 64 resolving forks, then is refused. Each block can pay up to 64 `cd -P` forks, each a fork with no exec (not measured).
  - Added in fix round 3, from review r3: the parse. Its CPU grew with the square of a relative `cd` chain, with no bound before the awk ended (fix round 3 bounds it).
  - Added in fix round 3, from review r3: the brief block's reads, up to 16 regular files, each read by up to two `grep`s, with no time bound (not measured).
- **Brief block.** It gets the same resolved keys, and its 16-brief refusal now counts distinct resolved briefs.
- **Tests.**
  - (a) `test_one_checkout_spelled_many_ways_is_checked_once`.
    - Setup: the payload cwd is a rulespec checkout with a second worktree and 40 staged rule files, each with its manifest.
    - Three commands, each followed by `git commit -m x` and the stash: one `cd .`; the review's 15 spellings; and `cd il/..`, `cd .//il/../il/..`, a trailing `/`, a link, `pushd …//rulespec/./il/..`, `cd ~/rulespec` and `cd ../rulespec/il/statutes/../..`.
    - Measure: `start` events in `GIT_TRACE2_EVENT`, which must be equal on each side, with the Claude block at 3.
    - **Before** (`52d515f9a` with `b58d8f977`'s hook): Claude 6, 48 and 21; overlay 7 for one `cd`.
    - **After:** passed.
  - (b) `test_the_hand_block_denies_when_its_time_budget_is_spent`.
    - The wrapper: git is an exported bash function, `BASH_FUNC_git%%`, which both hooks' bash 3.2 imports and their `PATH` cannot bypass. It logs each call and sleeps 6 s.
    - Four distinct rulespec checkouts. Unbounded, that is 12 calls and 72 s.
    - Both sides must deny with the budget's reason after exactly 6 calls.
    - **Before:** the Claude side allowed after its 9 calls of the first draft (7 s each).
    - **After:** passed at `fc5d6bc4b`'s bytes (budget2, below).
  - `test_too_many_places_to_check_is_refused` replaces `test_more_than_16_places_to_check_is_refused`, whose claim was false.
    - Refused on both sides: 65 `cd il/..` spellings, 65 `-p` spellings and 17 distinct briefs.
    - Allowed: 64 spellings, and 16 briefs.
    - **Before:** failed, on the 16-folder message.
- **Brief-block test.** `test_one_brief_spelled_17_ways_is_read_once`.
  - 17 spellings (`./`, `..`, a link) of one brief, for a brief with the regime block and for one without.
  - A logging `grep` (`BASH_FUNC_grep%%`) must show exactly the two greps of one read of the resolved file.
  - The brief with the regime is allowed; the one without is denied for that.
  - **Before:** refused as more than 16 briefs.
- **The review's question 3.** `test_a_folder_that_is_not_there_starts_no_git_process`.
  - The payload cwd and 17 `cd`, `-C` and `pushd` targets that do not exist must start 0 git processes and be allowed on both sides.
  - **Before:** refused as more than 16 folders.
- **Claims corrected.**
  - `docs/private-guard-overlay.md`: the fix r1 sentence now says what the 16 was and what review r2 found. A new "fix r2" section states the bound and what it leaves open.
  - The test docstring: the test it belonged to is replaced (above).
  - Fix round 1's "Bound" paragraph above carries a correction.
  - TRUST's fix r1 sentence is not edited. The fix r2 sentence corrects it.

**P2: a staged rule file with a non-ASCII name got through.**
- **Cause.**
  - `git diff --cached --name-only` C-quotes a name that holds a byte outside printable ASCII, a double quote, a backslash or a control character.
  - With `core.quotepath=off` it still quotes the last three. I probed `il/statutes/a"b.yaml`: still quoted.
  - The anchor `^[a-z][a-z]…` never matched the leading `"`.
- **Change, both hooks.**
  - The diff runs with `-z --no-relative`, then `tr '\n\0' '\001\n'`. NULs become line ends, and a newline inside a name becomes `\001`.
  - Each name under the anchor is then looked up with its own bytes. `cat-file --batch-check` reads the whole line, tab and space included.
  - A name that holds `\001` is denied whatever is staged: the line-based lookup cannot carry a newline. A manifest spelled with `\001` where the name has a newline must not let the name through.
  - The denial shows a control byte as `?`.
- **Tests: rows in `HAND_CASES`, both sides.**
  - `il/statutes/s-1.yaml` without its manifest, denied: the control, which passed before.
  - `sécti-2.yaml` without its manifest (denied) and with it (allowed); the same through `git -C`.
  - `a"b.yaml` without (denied) and with (allowed); `a\b.yaml` without (denied).
  - A tab in the name, without (denied, shown as `?`) and with (allowed).
  - A newline in the name, denied both with its real manifest staged and with a `\001` manifest staged.
  - **Before:** every "denied" row but the control was allowed on both sides (7 failures).
  - **After:** passed at `8d58890f1`.

**Beyond the brief: a checkout's `diff.relative` hid every staged file.**
- I found this while fixing P2, in the same diff call.
- With `diff.relative=true` in a rulespec checkout's config, `git diff --cached --name-only`, run from a subfolder, prints paths relative to it and drops the rest. So `cd <rs>/il && git commit` passed the anchor nothing.
- A lane's command can set that key.
- `--no-relative` (git 2.28 or later) fixes it. Every git here is newer: this host's `/usr/bin/git` is 2.54, and `macos-15` CI ships 2.39.
- **Test:** `test_a_relative_diff_config_hides_no_staged_file`, a `cd` form and a `-C` form.
  - **Before:** allowed. **After:** passed.
- The hub may drop this. Its mutant is 12.

**Question 3** of the review is covered under P1 above.

### Hash pairs

- **New:** hook `7c96811e0c0ff0a90b4d9a62fee4fc55a69c2018e2d44ef59c78915499b2b213`, TRUST `d284687f16ab6fac7fccf08f457013afff3f761d243ad97246aa8de95ba0dee6`.
  - TRUST changes only `hook_sha256` and appends one dated sentence. That sentence ends as the pin test requires.
  - `03a5bb582` pinned an intermediate pair, `56015c96…`/`c1161574…`. `fc5d6bc4b` replaced it.
- **Rollback:** fix round 1's pair, hook `324d2a84…` with TRUST `cc26c421…`.
- **Undo step.** `GS4_TEXT` in `test_guard_subfleet_root.py` maps the two new blocks back to `claude-blocks/fix-r1/`. It edits nothing outside the blocks. `test_fix_r2_undoing_its_step_gives_the_fix_r1_pair` rebuilds both files byte for byte from it and `FR2_PROVENANCE`.
- **The other undo tests.** `GS3_BLOCKS` now reads `fix-r1/`. `R3_TEXT`, the three earlier undo tests and the search-rel r12d rebuild all undo `GS4_TEXT` first.

### The Claude hook edits the hub makes at install

In `~/.claude/hooks/guard-never-rules.sh`:
1. Add `git-config-pin.txt`'s line (`f653d463…`) right after the input read. Skip this step if the live hook already carries that line from d2958. That line is byte-identical to this fixture, so the hook should have one pin, not two.
2. Replace the prefilter line (`c46d8eec…`) with `prefilter.txt` (`2acb063e…`). This is unchanged from fix round 1.
3. Replace the two blocks:
   - the hub's 2026-11-11 cut, `77002282…` and `f870acdf…`;
   - or fix round 1's text, `45775b4d…` and `0b6b418c…`, if that was applied;
   - with `axiom-brief-regime.sh` (`34c9279dbf1226cde2108ce983b1073142c663038a4fa45cda90665a43df42d5`) and `hand-authored-rulespec.sh` (`407e2dfae5cfc0447c466738ce2c15a2324ac2a213f9436503a9a775fb61a106`).

The blocks need bash (3.2 is enough: `$SECONDS`, indexed arrays, `$'…'`), `tr`, and git 2.28 or later for `--no-relative`.

### Contract

No clause in `docs/acceptance-contract.md` names the overlay's rules, its git calls, its cost or its budget. C-14.1 covers the pair's bytes as a class, as in earlier rounds. So I amended nothing and added no change-log line.

### Commands and results

Environment, as in fix round 1:
- `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)`, `UV_CACHE_DIR=$TMPDIR/uv-cache`, scratch `$TMPDIR/sf-gs-fix2`, `PYTHONPATH` unset.
- Each pytest call had a fresh `--basetemp …/sf-gs-fix2/pytest-<name>`, `-p no:cacheprovider` and `--timeout-method=thread`.
- Each ran under `timeout 585` through `uv run --no-sync --with 'pytest-xdist>=3.6'`, with at most `-n 4`.

| Command (at) | Result | Load |
| --- | --- | --- |
| `uv sync --group dev` | ok, 10.6 s | 255 |
| probes: exported `git`/`grep` functions in bash 3.2, `tr`, `diff.relative`, trace2 `start` on a failed `-C`, control-byte names on APFS, `-c core.quotepath=off` | each as described above | ~200 |
| before-r2 (`52d515f9a`, hook `b58d8f977`): the new tests and rows | **13 failed, 5 passed** (the 5: the ASCII control, three allow rows, and the existing brief row "regime spelled in another case", which `-k spelled` also matched), 196 s | 148-207 |
| after-r2a (`8d58890f1`'s tree): the same, plus DriftTests | 25 passed, 1 failed (the fixture hash pin, not yet moved), 243 s | 155-181 |
| measure.py, base (`af5e7239`, from `82d3dd819`) / r1 (`324d2a84`) / draft r2 (`a4928966`) | `echo`: 5.6 / 2.1 / 4.0 s. Review command, 3,000 files: 10.7 s, 1 git / **over the 240 s cap**, 33 git started / 44.5 s, **4 git** | 251-275 |
| measure.py, draft r2 only | stash alone 11.2 s, 1 git; `find . -name x` 31.6 s, 0 git; one `cd` 45.9 s, 4 git; the 15 spellings 20.3 s, 4 git | 149-170 |
| one checkout's steps by hand | `remote get-url` 0.05 s, diff 0.97 s, `tr`+`awk` 2.9 s, `cat-file` 30.7 s (0.46 s CPU); with `--buffer` 19.3, 10.0 and 6.7 s (noise, so not adopted) | 250-285 |
| pins1 (`03a5bb582`'s tree): overlay file, undo, TRUST, DriftTests | **42 passed**, 143 s | 200-214 |
| sync-axiom (`03a5bb582`): the Axiom tests | cap: 45 results, 6 F, names not printed | 170-197 |
| sync-hand (same, `-v`) | cap: 20 results, 13 F | 320-354 |
| one-hand: "companion test alone allowed" alone | **failed: the 15 s budget's reason**, 181 s; this led to `fc5d6bc4b` | 326 |
| budget2 (`fc5d6bc4b`'s bytes): the budget test, companion, `cd` into the checkout, both `-C` forms | **5 passed**, 277 s | 361-394 |
| pins2 (`fc5d6bc4b`): overlay file, undo/TRUST/doc tests, DriftTests, the new tests and P2 rows | cap: **54 passed, 0 failed**; the rest of the new tests did not start | 167-272 |

### Not run

- `test_same_decisions_on_generated_staged_files`. See the addendum if a later call ran it.
- All the mutants in `tools/gs_mutants.py` (1-14) and its control. The new mutants 8-14 each have a named test. Each named test failed before the fix with every change reverted at once (before-r2), but no mutant was applied alone.
- `test_guard_search_rel.py`.
- `test_guard_subfleet_root.py` beyond the pin, undo and TRUST tests.
- `test_review_publish_env_round8.py`, `test_guard_trust.py`, `test_doctor.py` and the other files that import `tests/fake/guard.py`.
- Step 3's four files.
- At `fc5d6bc4b`'s exact bytes, these never reported: most new tests, the P2 rows, the rest of `HAND_CASES` and `BRIEF_CASES`, and fsmonitor, later-rules and staged-count. They passed at `8d58890f1` (the new ones) or at `b58d8f977` (the old ones). Since then only the budget clause changed, and it cannot fire for two or fewer folders. Every one of those cases has at most two, except the budget test, which ran.

CI runs the suite.

### Deviations

1. **The `--no-relative` fix is beyond the brief** (above). The hub may drop it.
2. **The budget changed twice.** First 20 s, from the block's start, before every call. Then 15 s, from the measurements. Then the first two folders were exempt, after a load-only refusal. The brief asked for a budget "well under the 60 s timeout"; the exemption is my call, and the reasons are above.
3. **New denial texts.**
   - The hand block's "more than 64 folders" refusal.
   - Its budget denial, which ends "Commit from one checkout per command, or retry when the host is less busy."
   - The brief block's refusal now reads "more than 16 briefs to read, or more than 64 paths to them".
4. **A new fixture folder**, `claude-blocks/fix-r1/`, for the undo chain.
5. **`test_more_than_16_places_to_check_is_refused` is replaced** by `test_too_many_places_to_check_is_refused` and `test_a_folder_that_is_not_there_starts_no_git_process`.
6. **The first measurement's base row was invalid.** I gave `git show` the hook hash `af5e7239` as a revision, and it wrote an empty file. The second run took the hook from `82d3dd819`, and it matched `af5e7239`.
7. **Two probe files outside the scratch folder**, `/tmp/sf-probe-<pid>` and `/tmp/sf-gs-fix2-started`. Each was removed in the same command that made it.

### Folders outside the job's scratch

- `$TMPDIR/sf-gs-fix2` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-gs-fix2`) held the probes, `run.sh`, `measure.py`, the logs, the basetemps and the scratch checkouts. It is removed at the end with a guarded `shutil.rmtree` that asserts `sf-gs-fix2` is in the path; the addendum records it.
- I kept `$TMPDIR/uv-cache` (shared) and the worktree's ignored `.venv`.

Code head: `fc5d6bc4b`. Head built: the commit that adds this section.

### Addendum: the runs after the report's first commit (`50ac2a7e4`)

| Command (at) | Result | Load |
| --- | --- | --- |
| `test_same_decisions_on_generated_staged_files` alone, no `-n` | **no result at my 585 s cap**, as in both earlier rounds | 158-205 |
| final-new (`50ac2a7e4`): the new tests and the 11 P2 rows | **16 passed**, 183 s, so every new test has now passed at the final bytes | 100-187 |
| control: `test_a_relative_diff_config_hides_no_staged_file` in an unmutated `git archive` export | 1 passed, 2 s | ~70 |
| `gs_mutants.py --named-only --no-control`, mutants 8, 10, 12, 13, 14 | **all killed** by their named tests | ~70 |
| the same, mutant 11 | first run: its named test id lacked pytest's escaped backslash, so it showed "not killed" with 0 run. Fixed in `399f06a03`, then **killed** | ~130 |
| the same, mutant 9 (no time budget) | **killed** | ~130 |
| the same, mutants 1-7 | 1 and 2 **killed**; my 200 s cap stopped it before 3-7 | 136-159 |

**Still not run:**
- the generated test, to a result;
- mutants 3-7 and the tool's control;
- `test_guard_search_rel.py`;
- the rest of `test_guard_subfleet_root.py`;
- the other files that read the changed files;
- step 3's four files.

**Folders outside the job's scratch:**
- `$TMPDIR/sf-gs-fix2` (`/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-gs-fix2`). Its logs, `run.sh`, `measure.py` and the TRUST sentence are copied to the attempt folder's `gs-fix-r2-evidence/`. A guarded `shutil.rmtree` that asserts `sf-gs-fix2` is in the path removed it at 19:48Z, and the folder is gone.
- The `sf-mutants-*` folder that the capped mutant call left was inside it.
