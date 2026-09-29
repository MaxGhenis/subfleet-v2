# Merging + New titles into 2.1.9: changes, invariants, verification

Branch line: `candidate/219` (WIP merge `e652fe80`, 26 failing tests) → `5218ecbd` (previous
run) → `038d5b1c`, `ce27f6fc`, `eb4efaf2`, `6954e1bd` (this run). Briefs:
`~/reviews/subfleet-steer-2026-09-28/brief-candidate-titles-{original,cont,cont2}.md`.

## Outcome

- Every run of the brief's affected set that completed is green (see [Final counts](#final-counts)).
  - The 26 WIP-merge failures are fixed, and each passed 3 of 3 at HEAD.
  - Did not complete within a 10-minute foreground window on this overloaded machine:
    `app/build.sh`, and two frontend view-probe files at HEAD. The Swift inputs are
    unchanged, and those files passed at `5218ecbd` this session.
- An adversarial review of `5218ecbd` against the six rules found four more gaps in the
  title path. Two were mine: title-before-queued-command and oversized title line. The
  other two came from an independent Opus 5.5 reviewer: the claim-window stop race and a
  cancel for a request the relay never took. All four are fixed.
  - Each fix has a test that fails without it.
  - Every rule's enforcing code was mutated, one mutant at a time, in place. Each mutant
    was restored with `git checkout --`, with `git diff` empty afterwards.
- Three single-guard mutants survive, each because a second, independent guard holds.
  The test suite has no coverage gap there: removing all the guards together is killed.

## Every change since the WIP merge (`e652fe80..HEAD`)

- **(a)** means an expectation changed legitimately because a title frame now goes out
  (or because the merged steer branch changed the fake CLI).
- **(b)** means a code defect was fixed.
- Line numbers are at HEAD.

### Product code

| # | Where (HEAD) | Label | Change and justification |
|---|---|---|---|
| 1 | `subfleet/conversations/runner.py:727-760` (`_transmit`) | (b) | Back to the pre-merge code. The WIP merge's `_send_frame` read and set `relay.timeout_s` and the private `relay._sock` on every frame. Every relay double without them (legacy-hold `RecordingRelay`/`Relay`, the steer and property doubles) crashed the runner at its first frame, so `init`, `close` and `interrupt` were never sent. That was most of the 26 failures, and it broke rule 6. (`5218ecbd`) |
| 2 | `runner.py:556-557` (`_apply`) | (b) | The title request no longer rides the ordered outbox behind the message frame. There it was written after a stop recorded during a lost-answer handover, and could hold a stop or a steer behind it (rules 1, 2). (`5218ecbd`) |
| 3 | `runner.py:660-662` (`_send_outbox`) | (b) | Turn frames first (`_send_frames`), then `_send_title`. Since `038d5b1c` there is no pre-check of the outbox, so `_title_may_go` is the one place that enforces "nothing of the turn waits". |
| 4 | `runner.py:672-676` (`_send_frames`) | (b) | After an unanswered title write, whose number may be taken, the turn's next frame resynchronizes (status handshake) before it goes (rule 5). The WIP merge's skip of queued title frames is gone, because titles never enter the outbox. (`5218ecbd`) |
| 5 | `runner.py:135-139` | (b) | Title state out of the outbox: `title_asked`, `title_frame` (a claimed, unwritten request or its cancellation; was `title_cancel`), `optional_ack_lost`. |
| 6 | `runner.py:762-770` (`_title_closed`) | (b) | The permanent barriers: not Claude, replay, recorded outcome, ended, withheld, relay failed, frame refused, a stop asked or started, an outcome, an interrupt, a close sent or queued. (`5218ecbd`) |
| 7 | `runner.py:772-789` (`_title_may_go`) | (b) | New in `038d5b1c`. It waits while any command (a stop, a steer, an answer) is queued, not only while a frame is (rule 2). It drops a cancellation unless the relay's log shows the request written. It reads a stop the service committed before `interrupt` arrived. |
| 8 | `runner.py:791-795` (`_drop_title`) | (b) | Drops a pending title frame for good and clears the budget, so no cancellation follows a request never written or a barrier. |
| 9 | `runner.py:797-834` (`_send_title`) | (b) | Claim, then check again before writing (`runner.py:816-820`). The claim is a store transaction and can wait behind a stop's or a steer's. `5218ecbd` checked only before claiming, so a person's Stop committed while the claim waited, or the daemon's stop (wall limit, kill: `interrupt` alone), still let the title go (rule 1). A command queued during the claim now keeps the claimed frame until the drain (rule 2). The write uses only `relay.send` (rule 6). |
| 10 | `runner.py:931-933` (`_timers`) | (b) | The budget's cancellation goes to `_send_title`, which writes it only if still allowed. The WIP merge put it in the outbox whenever there was no outcome and the relay was up, so it could follow a stop. |
| 11 | `subfleet/conversations/store.py:537-547` (`claim_title_generation`) | (b) | The claim refuses once the message has a recorded stop (`NOT EXISTS ... stop_requested_at IS NOT NULL`). A person's stop is a transaction on the same store (`service.py:829`), so the two are linearized: a claim that waited behind the stop sees it (rule 1). |
| 12 | `subfleet/conversations/titles.py:25-31, 51-70, 96` | (b) | The request line is at most `TITLE_LINE_MAX` = 8 KiB, and its description is the longest prefix of the message that fits. Before this it held up to 16,384 escaped characters: 196,752 bytes for emoji and 98,449 for CJK. A macOS pipe held 64 KiB (measured 2026-09-29). The relay applies frames in order under one lock (`relay.py:329`) and writes with a blocking `os.write` (`relay.py:377`, on an `os.pipe()` fd, `guardian.py:159`). So a larger title, sent while the provider was not reading, held the stop's interrupt and SIGINT behind it (rule 2). |

### Tests

| # | Where (HEAD) | Label | Change and justification |
|---|---|---|---|
| 13 | `tests/unit/test_turn_runner.py:454, 489, 519` | (b) restore | The WIP merge had added `session-title` to three expected logs. They return to the base (`078c0902`) because those tests feed no acceptance, and the title now waits for it. (`5218ecbd`) |
| 14 | `tests/unit/test_turn_replay.py:99-100` (`World.deliver`) | (a) | Waits for the title request after acceptance: a first Claude turn now writes it. (`5218ecbd`) |
| 15 | `test_turn_replay.py:151, 175` | (a) | Expect exactly one `session-title` across a restart (a replay never re-sends it; rule 4). |
| 16 | `test_turn_replay.py:191-192, 200` | (a) | The legacy-stop replay's log includes the first runner's title after acceptance. |
| 17 | `test_turn_replay.py:271-272, 277-278, 313-314, 330-331, 377-378, 419-420` | (a) | Expected relay logs include the first runner's title request after `settings`; the replay adds none. |
| 18 | `test_turn_replay.py:294-297` | (a) test setup | The first runner's `_sync_answers` is stubbed. The test stores a person's answer as one "the runner never wrote", but a live runner catches up stored answers after every stdout line. The answer can land between `add_approval` and `_sync_answers` for the same line, so that runner writes it. This is a setup race independent of titles. The rule under test (a replay after a recorded outcome never writes the answer) is still asserted: mutant `R1-replay-after-recorded-outcome` is killed by this test. |
| 19 | `tests/e2e/test_conversations.py:838-841, 898-902` | (a) | The SIGINT and wrong-model cases: the title request follows acceptance, before the interrupt. |
| 20 | `tests/fake/test_interactive_claude_titles.py:44-47` | (a) | The merged steer branch taught the fake CLI to emit `command_lifecycle` rows before replaying the message. The titles fixture now expects them. |
| 21 | `tests/unit/test_conversation_titles.py` | (b) | Replaces the WIP test that asserted `relay.timeout_s` (that test pinned the rule-6 defect). Adds tests for every rule, listed below. |

New tests in `tests/unit/test_conversation_titles.py`:
- From `5218ecbd`, lines 258-561:
  - `InterfaceRelay`, a `__slots__` relay with only `status`/`send`/`close`, applied through a real `RelayServer`.
  - `TitleTurn`.
  - Example tests for each rule.
  - The Hypothesis schedule property.
- From this run:
  - `:494` `test_a_title_waiting_behind_a_command_is_never_written_after_a_barrier`
  - `:574` `test_a_title_claim_that_waits_behind_a_stop_writes_nothing`
  - `:604` `test_a_steer_queued_while_the_title_is_claimed_is_written_first`
  - `:628` `test_a_steer_after_an_unanswered_title_write_keeps_its_handover`
  - `:650` `test_a_title_cancellation_goes_only_for_a_request_the_relay_took`
  - `:674` `test_the_title_line_fits_a_pipe_the_provider_is_not_reading` (a real pipe with no reader)
  - `:695` a Hypothesis property: the line is bounded, ASCII, and describes the longest prefix that fits.
  - `:708` a static check (AST) that every use of `self.relay` in the runner is `status`, `send`, `close` or the assignment.
  - `:474` now also covers a recorded, not yet delivered, stop.
  - The schedule property (`:742`) gains `race-recorded-stop` and `race-asked-stop` (a stop landing inside the claim) and `queue-answer`. Invariant 4 now asserts that no command waits at any title write.
- At `5218ecbd`'s runner and store, 11 of these fail, including the schedule property. All pass at HEAD.

## The six rules: enforcement, tests, mutants

**How the mutants were run.** `.diag/mutate.py` (not committed) applies one mutant in place, runs the named tests in the foreground, restores the file with `git checkout --`, and confirms `git diff` is empty. Every run below ended "git diff empty".

**Rule 1: nothing is written after a recorded stop, cancel or close, or on replay after a recorded terminal outcome.**
- Enforced by:
  - The user message: `runner.py:700-702` (under the handover lock) and `:853`. A person's stop is recorded under the same lock (`service.py:829`), and cancels too (`store.withdraw`).
  - Replay after a recorded outcome: `:691`.
  - Titles: `store.py:546`, `runner.py:766-770`, `:777-788`, `:819`.
  - After a close, the relay itself refuses writes (`relay.py:357`).
- Tests:
  - `test_no_title_is_written_after_a_stop_a_cancel_a_close_or_an_outcome`
  - `test_a_title_waiting_behind_a_command_is_never_written_after_a_barrier`
  - `test_a_title_claim_that_waits_behind_a_stop_writes_nothing`
  - `test_the_title_cancellation_goes_once_after_the_budget_and_never_after_a_stop`
  - schedule property 3
  - `test_turn_runner::test_a_stop_recorded_after_the_runner_s_first_look_still_keeps_the_message_unwritten`
  - `test_turn_replay::test_a_recorded_terminal_turn_never_replays_a_persons_answer`
- Killed:
  - `R1-claim-refuses-stop`
  - `R1-recheck-after-claim`
  - `R1-recorded-stop-read`
  - `R1-close`
  - `R1-asked-stop-all-guards`
  - `R1-outcome-and-close`
  - `R1-replay-after-recorded-outcome`
  - `R1-message-after-recorded-stop`
- Survived, by design:
  - `R1-asked-stop` (removing `stop_reason`/`stop_at`): the queued interrupt command (`:782`) and `interrupt_requested` (`:769`) still hold it.
  - `R1-outcome` (removing `driver.outcome`): the outcome's close (`:769-770`) and the never-set `accepted` (`:783`) still hold it.

**Rule 2: a title request never delays or blocks the user message, a stop or a steer.**
- Enforced by:
  - After acceptance only (`runner.py:783`).
  - Only with no frame and no command waiting (`:782`).
  - No handover lock around the write (`:822-823`).
  - Never retried (`:812`, `:827-834`).
  - A line that fits the pipe (`titles.py:31, 57-70`).
- Tests:
  - `test_the_title_request_waits_for_acceptance_and_for_every_frame_of_the_turn`
  - `test_a_steer_queued_while_the_title_is_claimed_is_written_first`
  - `test_a_stop_is_recorded_while_a_title_write_is_held_by_the_relay`
  - `test_the_title_line_fits_a_pipe_the_provider_is_not_reading`
  - the line property
  - schedule property 4
- Killed:
  - `R2-outbox-waits`
  - `R2-commands-wait`
  - `R2-acceptance`
  - `R2-no-handover-lock`
  - `R2-line-bound`

**Rule 3: no title request is written for a withheld message.**
- Enforced by: `runner.py:767` (`withheld`), `:783` (the message must be written and accepted), and the driver's stopped-before-send outcome (`:768`).
- Test: `test_a_withheld_message_never_asks_for_a_title` (stop before handover, legacy withhold).
- Killed: `R3-all-guards`.
- Survived, by design: `R3-withheld-only`, because the other guards each still block it.

**Rule 4: a replay never re-sends a title request an earlier runner sent.**
- Enforced by:
  - `runner.py:766` (`replayed_message`, set at `:131` and at the first handshake, `:620`).
  - `store.py:545` (one claim per conversation).
  - Only a claiming runner has a budget, so only it can cancel (`titles.py:94-95`).
  - A cancellation needs a written request (`runner.py:778-779`).
- Tests:
  - `test_a_replay_never_sends_a_title_request_again`
  - `test_only_first_person_message_claims_one_request_and_watch_gets_title`
  - `test_a_title_cancellation_goes_only_for_a_request_the_relay_took`
  - `test_turn_replay` (one `session-title` across restarts)
  - schedule property 1
- Killed:
  - `R4-replayed-message`
  - `R4-claim-once`
  - `R4-cancel-only-written`
  - `R4-replay-all-guards`

**Rule 5: steer frames keep their handover discipline.**
- Enforced by:
  - The steer handover (`runner.py:708-723`, `:875-889`), unchanged by the merge.
  - The resync after a lost title answer, which runs before any frame (`:672-676`).
  - Titles never enter the outbox (`:556-557`).
- Tests:
  - `test_a_steer_after_an_unanswered_title_write_keeps_its_handover`
  - `test_a_lost_refused_or_oversized_title_write_is_never_retried_and_never_fails_the_turn`
  - `test_turn_runner` steer tests and properties
  - schedule property 4 (no title in the outbox)
- Killed: `R5-resync-after-lost-title`, `R5-steer-host-stop`.

**Rule 6: the runner must not depend on relay attributes outside the relay interface.**
- Enforced by: the runner uses `status` (`runner.py:610`), `send` (`:731`, `:733`, `:823`) and `close` (`:357`). Nothing else in `subfleet/` reads `runner.relay`.
- Tests:
  - `test_the_runner_reaches_its_relay_only_through_status_send_and_close` (static, every path)
  - `test_a_relay_with_only_status_send_and_close_carries_a_titled_turn_and_its_stop`
  - every `InterfaceRelay` test
  - the legacy-hold doubles
- Killed: `R6-timeout-attribute`.

### What the rules cannot rule out, and why

- **A stop recorded while a title write is in flight.**
  - The title's last check (`runner.py:819`) comes before `relay.send`. A person's Stop recorded after that check is not blocked, because rule 2 forbids the title from holding the handover lock. Its interrupt follows the title on the pipe.
  - The decision is linearized before the stop: in the store for a recorded stop, and at the last check for an asked stop.
  - Pinned by `test_a_stop_is_recorded_while_a_title_write_is_held_by_the_relay`.
  - The titles review (`2026-09-28-new-session-titles.md`) reports, from inspecting the Claude Code 2.1.280 binary, that the title handler uses the turn's abort signal. If so, the interrupt ends the title too. Not re-verified here.
- **The escalation clock starts late.** It starts when the runner drains the stop (`runner.py:411-412`), not when the stop is asked (`:205-209`). Any relay call already blocking the runner thread (up to the 30 s client timeout, `:124`) delays SIGINT, close and containment by that long.
  - This predates titles.
  - The title's own contribution is now one small write that fits the pipe.
  - Changing when `stop_at` starts would change every stop's policy clock, so it was left alone.
- **Found in review, not changed:**
  - A cancellation queued by `_timers` can still go if the title's answer arrives in the same loop before the write. The late answer is rejected by the store anyway (`store.py:549-560`).
  - Predating titles: a replay after a recorded outcome rebuilds and writes a `refuse:` or `cancel-steer:` frame an earlier runner left unwritten (`runner.py:691` drops only the message and `approval:` frames).

## Load flakiness

The machine ran at load 30-160 (18 cores), with the disk near 1 GB/s and 21,700
transfers/s. Every failure below is outside the conversation code: none of these tests
reaches the runner, the title claim or `titles.py`.
- Each assertion is a wall-clock bound: a wait-hub read count in 0.6 s, a linear-time
  ratio, a 0.5 s deadline, and "condition timed out after 5s".
- The four unit tests, rerun together in one process, failed intermittently: four of six
  reruns had a failure. The one identified was
  `test_wait_rechecks_on_its_own_clock_without_a_commit`, whose hub made one read in
  0.6 s instead of at least two.
- Each of the four run alone passed 5 of 5, at load 84-92.
- The three fake-CLI tests passed 3 of 3 in isolation.

The previous run reported 18 unit failures that "pass on rerun" after an overloaded
run, but its notes do not name them.
- The brief's affected unit set passed in full in every run this session: 845 at
  `5218ecbd`, 858 at `038d5b1c`, and all 866 at HEAD inside the full suite.
- The 26 original WIP-merge failures each passed 3 of 3 at HEAD: the 24 unit tests with
  4 workers, the 2 e2e tests serially.
- The one test the previous run changed for a race (#18) is a setup race in the test,
  not a product defect.

## Final counts

- **The brief's command** (`tests/unit/test_conversation_*.py ... tests/frontend/ tests/e2e/test_conversations.py -k 'not live'`), run in guarded chunks with pytest-xdist added to the local venv only:
  - Unit portion: 845 passed at `5218ecbd` and 858 at `038d5b1c`. At HEAD (`6954e1bd`), 866 tests, all included in the full-suite chunks below, which passed them.
  - `tests/frontend/`: 227 passed at `5218ecbd`.
  - `tests/e2e/test_conversations.py`: 20 passed and 13 skipped at `5218ecbd` and at `038d5b1c`. The skips are person-only requests, which cannot come from inside a Subfleet job (`tests/e2e/test_conversations.py:65-70`).
- **E2e and fake title tests** (`tests/e2e/test_conversation_titles.py`, `tests/fake/test_interactive_claude_titles.py`, and `test_conversations.py`): 33 passed, 13 skipped at `038d5b1c`.
- **Full suite** (`uv run pytest -q`, 7,189 tests) at HEAD `6954e1bd`, in guarded chunks:
  - `tests/unit`: 6,142 passed and 4 failed. The 4 are timing tests; see [Load flakiness](#load-flakiness).
  - `tests/fake`, `tests/process`, `tests/live`: 723 passed, 3 failed and 6 skipped.
    - The 3 failures are 5 s timeouts; each passed 3 of 3 in isolation.
    - The skips: `SUBFLEET_LIVE` is unset, and one process test reports that it is itself QoS-clamped.
  - `tests/e2e`: 65 passed, 15 skipped (person-only, as above).
  - `tests/frontend`: 202 passed at HEAD, including every test that drives the Python daemon:
    - 190 core-probe tests (the brief's 188 plus 2 whose names contain "live");
    - `test_core_draft.py`: 7;
    - `test_reading_view.py`: 5.
  - Three things did not complete within a 10-minute foreground window:
    - `test_core_live` (one test, driving a development daemon). The first attempt's daemon missed the harness's 10 s start; the second did not finish.
    - `test_menu_view.py` (7) and `test_status_model.py` (21). Their Swift compiles ran at about 4% CPU on the saturated disk. Neither file drives the daemon, their Swift inputs are unchanged since `5218ecbd`, and both passed there this session. A second attempt, each file alone, was also stopped during its compile.
- **`app/build.sh`**: none of three attempts finished within the 10-minute foreground window. The last ran alone at load 29. The `-O` build of all app sources never completed under this load, and each attempt was stopped with nothing left running.
  - `app/Sources` and the Info.plist are byte-identical to `5218ecbd`. The previous run reported that build passing (`Built: /private/tmp/claude-titles-app/Subfleet.app`, rc=0); that was not re-verified here.
  - This session, every file in `app/Sources` compiled without `-O` under the probes' model (`SUBFLEET_MODEL_TEST`) and view (`SUBFLEET_VIEW_TEST`) configurations. The app's `@main` entry and the `-O` build were not compiled.

## Process notes

- **No `Workflow`.** The session was headless (no later turns), so the independent review
  ran as a foreground Opus 5.5 subagent.
- **One leftover, found and removed.** An e2e guardian started by this run's frontend chunk
  survived its pytest's guarded kill, because it runs in its own session. Found at about
  20 minutes old, terminated, and its `/private/tmp` state root removed. Nothing this run
  started is still running.

## Appendix: the mutants

- Each row is one in-place edit, run against the tests shown and then restored.
- Line numbers are at HEAD `6954e1bd`; the file is `runner.py` unless the row names another.
- "Killed by" names the test that failed.

| Mutant | Rule | Edit | Result | Killed by |
|---|---|---|---|---|
| R1-claim-refuses-stop | 1 | `store.py:546`: drop the claim's `NOT EXISTS` stop clause | killed | claim-waits-behind-a-stop [recorded-stop, persons-stop] |
| R1-recheck-after-claim | 1 | `:819`: no second `_title_may_go()` after the claim | killed | claim-waits-behind-a-stop [daemon-stop] |
| R1-recorded-stop-read | 1 | `:786`: ignore a recorded `stop_requested_at` | killed | cancellation-goes-once [recorded] (added for this mutant) |
| R1-asked-stop | 1 | `:768`: drop `stop_reason`/`stop_at` from `_title_closed` | survived | the queued interrupt (`:782`) and `interrupt_requested` (`:769`) still hold |
| R1-asked-stop-all-guards | 1 | Drop those, the close checks and the command gate | killed | no-title-after-a-barrier [asked-stop]; waiting-behind-a-command (all 4) |
| R1-close | 1 | `:769-770`: drop the close checks | killed | no-title-after-a-barrier [close] |
| R1-outcome | 1 | `:768`: drop `driver.outcome` | survived | the outcome's close (`:769`) and the never-set `accepted` (`:783`) still hold |
| R1-outcome-and-close | 1 | Drop the outcome and the close checks | killed | no-title-after-a-barrier [close]; waiting-behind-a-command [close, outcome] |
| R1-replay-after-recorded-outcome | 1 | `:691`: never drop the message or answers after a recorded outcome | killed | `test_turn_replay` recorded-terminal-turn test |
| R1-message-after-recorded-stop | 1 | `:853`: ignore a recorded stop at the handover | killed | `test_turn_runner` stop-after-first-look [claude, codex] |
| R2-outbox-waits | 2 | `:782`: send while a frame of the turn waits | killed | title-waits-for-acceptance-and-every-frame |
| R2-commands-wait | 2 | `:782`: send while a command waits | killed | steer-queued-while-the-title-is-claimed |
| R2-acceptance | 2 | `:783`: send before the provider's acceptance | killed | title-waits-for-acceptance-and-every-frame |
| R2-no-handover-lock | 2 | `:823`: write the title under the handover lock | killed | stop-recorded-while-a-title-write-is-held |
| R2-line-bound | 2 | `titles.py:61`: never shorten the line | killed | title-line-fits-a-pipe [emoji, cjk] |
| R3-withheld-only | 3 | `:767`: drop `withheld` | survived | the message was never written or accepted (`:783`); the stopped-before-send outcome (`:768`) |
| R3-all-guards | 3 | Skip `_title_closed` and the written and accepted checks | killed | withheld-message [both] |
| R4-replayed-message | 4 | `:766`: drop `replayed_message` | killed | replay-never-sends-again [False] |
| R4-replay-all-guards | 4 | `:766`: drop `replayed_message` and `recorded` | killed | replay-never-sends-again [False] |
| R4-claim-once | 4 | `store.py:545`: drop `title_requested_at IS NULL` | killed | only-first-person-message-claims-one-request |
| R4-cancel-only-written | 4 | `:778-779`: cancel whether or not the request was written | killed | cancellation-only-for-a-request-the-relay-took [unreached, refused] |
| R5-resync-after-lost-title | 5 | `:676`: no handshake after an unanswered title write | killed | lost-title-write-never-fails-the-turn [reached]; steer-after-an-unanswered-title-write [reached] |
| R5-steer-host-stop | 5 | `:887`: ignore the host's recorded stop at a steer's handover | killed | `test_turn_runner` steer-withdrawn-when-stop-wins [host] |
| R6-timeout-attribute | 6 | `:821`: touch `relay.timeout_s` before the title write | killed | relay-only-through-status-send-close (static); relay-with-only-status-send-close |
