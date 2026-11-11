# Guard sync: the overlay's keychain sentence and the attached-runner denial (2026-11-11)

Brief: `~/reviews/subfleet-hub/guard-sync-2026-11-11/brief-build-r1-filled.md` (build r1). An in-session Opus agent ran it in place of lane job 20261111-110910-gs-build-r1. It worked in the worktree `~/subfleet-v2-lanes/guard-sync`, on branch `fix/guard-sync-217`, from `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d` (`origin/release/217`).

Start: 2026-11-11T18:19:54Z (`date -u`, first command). Status: **partial**. Changes 1 and 3 are done, tested and mutation-checked. Change 2 stopped as the brief directs: the repository holds no copy of the two Axiom blocks, so the hub has to supply their text. Only part of test step 2 ran (see Commands).

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
