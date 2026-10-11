# Suggested conversations: fix round 1 (review r1b of #181)

Base head `a2460a011`. Review: `~/reviews/subfleet-2110/suggested-conversations/review-r1b.md`
and `review-r1b-full.md` (REQUEST CHANGES, three P1s and two P2s). Commits:

- `9e35fa8a8` the daemon half: Start owns its conversation, nothing precedes the brief, errors scrubbed.
- `bf745658c` the app half: merge by feed sequence, render only the daemon's plan; contract re-check.
- this report and the test results (the head named at the end).

## The findings, each fix and its regression

### P1-1: an agent's message could become the first message (`service.py:1086`)

Before: after a crash between the `starting` claim and the brief, an agent's
`message.submit` to the new conversation was accepted (and dispatched as a turn),
and the person's retry failed `out-of-order`; an agent that submitted the exact
brief under the derived id, then more work, let the retry reach `started` with
two messages (I5 broken).

Fix:

- `suggestions.admit_message` (`subfleet/conversations/suggestions.py`), called
  inside the transaction of every message write: `ConversationStore.submit_message`
  (so `message.submit`, wakes, failover continuations, unblock notes, tombstones)
  and `insert_legacy_history` (`subfleet/conversations/store.py`). While a
  `starting` suggestion names the conversation, anything but that suggestion's own
  Start (`starts_suggestion=sid`) is refused, exit 7 `conversation-starting`,
  whatever its id, text or predecessor; the brief itself is accepted only into an
  empty conversation (`conversation-not-empty`), and `finish_in` accepts only the
  id the claim chose (`not-the-brief`). The check and the insert share one
  transaction, and the brief's acceptance records `started` in that same
  transaction, so no ordering admits another message first.
- The first message's id is no longer `uuid5(suggestion id)`, which any caller
  could compute and take first (anywhere: message ids are global, so a squat in
  another conversation would also have blocked every Start with
  `message-id-conflict`). The claim transaction chooses a random UUID and keeps it
  in the private `start_message_id` column; no view carries it until it is
  accepted as `started_message_id`.
- Why not one transaction for row and brief: a worktree conversation's worktree
  is named by the conversation id and cut by git, which cannot run inside the
  store transaction, and C-24.3 publishes the text before the row. The
  admission check closes the window instead: in it nothing can be written.

Regressions (`tests/unit/test_suggestions_r1b_adversarial.py`):
`test_r1b_an_agents_message_cannot_poison_a_claimed_start` (the review's exact
sequence; before: accepted, retry `out-of-order`; after: refused with exit 7, retry
`started` with only the brief), `..._takes_the_brief_first_never_reaches_started_with_two_messages`
(the I5 variant), `test_r1b_the_poisoned_first_message_never_dispatches` (before:
one turn job for the agent's message; after: none until the brief, then the
brief's), `test_r1b_no_writer_puts_a_message_before_the_brief` (every writer),
`test_r1b_the_first_message_id_is_private_until_started_and_cannot_be_taken_first`.
The state machine (`tests/unit/test_suggestion_properties.py`) now has an agent
submitting to every suggestion's conversation at every step (new id, the old
derived id, the private id as if leaked, the brief's own text, any predecessor)
and starts cut short inside the brief's own transaction; I5 is now "while starting,
no message; once started, the brief is seq 1 and everything else is later", and an
agent's submit while starting must be refused `conversation-starting`.

### P1-2: an agent could pre-create the conversation Start adopted (`suggestions.py:326`)

Before: `claim` looked up `conversations.request_id = 'suggestion:<id>'` and
adopted what it found; an agent could create that request id first, in another
folder, and receive the person's approved brief there.

Fix: `Suggestions.claim` always inserts a new conversation, with no request id,
in the claim's own transaction; `started_conversation_id`, written there, is the
only link, and nothing is ever found by key. `ConversationService._check_request_id`
refuses request ids beginning `suggestion:` to `conversation.create` and
`conversation.handoff` (exit 2 `reserved-request-id`). The no-folder scratch folder
was `sha256("suggestion:<id>")`, the very folder an agent's create with that
request id would get; it is now `suggestion-<sha256(id)>`, a name create never
makes (create's are 64 hex digits).

Regressions: `test_r1b_an_agent_cannot_reserve_the_conversation_a_start_makes`
(before: Start reused the agent's conversation in `injected` with its title; after:
the create and handoff are refused, and even a conversation an earlier build let
take the name is ignored: Start writes its own in the advertised folder with the
suggestion's title), `test_r1b_the_scratch_folder_is_one_no_create_can_name`. In
the state machine an agent creates conversations at every step, in the reserved
namespace too, and I1 now says every conversation is the fixture's, an agent's,
or one a claim made, each claim's distinct.

### P1-3: unscrubbed secrets reached the feed (`suggestions.py:356`)

Before: `note_error` only truncated; a failing post-checkout hook's stderr
(through `git worktree add`) put a recognised token in `suggestion.list`, the
watch feed and the origin's events, and in the error Start returned.

Fix: `suggestions.scrub_error` scrubs the whole text with C-25.5's scrubber
(`scrub_secrets`, the handoff scrubber C-23.14) first, then folds it to one line
of at most `ERROR_MAX` = 500 characters, so a token straddling the cut never
leaves an unrecognised fragment. It runs on every `start_error` (`note_error`,
whatever its caller passed), on the error `op_suggestion_start` returns (any
`ConversationError` raised during a start: folder checks, git, hooks, worktree
creation; other exceptions reach clients only as "operation failed"), on the
daemon's log line when its own finishing fails, and on `decided_by`.
`_cut_worktree` now scrubs `git worktree add`'s stderr before keeping its last 300
characters (which also covers `conversation.create`'s error). A folder path or
caller session the scrubber would change is refused at create (`bad-workspace`,
`bad-session-id`), so no stored string can hold one.

Regressions: `test_r1b_a_failing_hooks_secret_never_reaches_the_list_feed_events_or_error`
(the review's hook; before: the token in all three transports; after: `[REDACTED]`
everywhere), `test_r1b_a_start_error_is_one_bounded_line_with_the_secret_scrubbed_before_the_cut`.
Properties (`tests/unit/test_suggestion_secret_properties.py`), for secrets of every
family the scrubber knows (provider and forge tokens, AWS and Google keys, JWTs,
bearer tokens, URL passwords, sensitive assignments) set between separators at any
place in up to 2.4 KB of text: `scrub_error` leaves none and is one line within
500 characters (500 examples); a real failing hook's output never reaches the row,
the listing, the by-id listing, the watch feed, the origin's events, the returned
error or the log, before or after the daemon's own retry (40 examples, real git);
the same for a folder check before the claim, an OS error and a refusal after it,
and a store error inside the brief's transaction (150 examples); a secret-shaped
folder or session is refused and makes nothing (100 examples). "Never appears" is
checked as: no string anywhere in those answers is one the scrubber would change
(the scrubber is idempotent on its own output). The state machine's I7 checks the
same over every step, with failures that say secrets.

### P2-4: an older list dropped a suggestion the feed had delivered (`Suggestions.swift:179`)

Fix: `suggestion.list` returns `watch_seq`, the feed's AUTOINCREMENT high-water mark
read under the same lock as the rows (`suggestions.feed_seq`). The app keeps each
suggestion's newest feed sequence (`ConversationStoreState.suggestionSeqs`: a feed
row's, or the read of the newest answer that carried it). `apply(suggestions:)`
removes a known open suggestion missing from a listing only when the listing was
read at or after that sequence, and `apply(suggestionRows:readAt:)` (listings and
by-id reads, `UIModel.loadSuggestionBrief`) never replaces a suggestion with an
answer read before its latest feed row.

Regressions (`tests/frontend/test_core_suggestions_r1b.py`, real daemon answers
folded by the compiled Swift core): the review's sequence (before: 0 cards, cursor
1; after: the card stays), a listing as new as the feed still drops one that
closed and aged out, and an older answer never rewinds a change within a state
(`start_error` arriving after the read).

### P2-5: a card promised a worktree Start would not make (`Suggestions.swift:169`)

Fix: the daemon decides where a suggestion starts when it is made and keeps it:
`workspace_kind` (`worktree` when one was asked for and the folder is a git
checkout, with `repository`, the checkout's top level; else `in-place`), in the
view. Start honours it: an in-place plan stays in place even if the folder later
became a checkout, and a worktree plan whose folder stopped being one is refused
(exit 7 `not-a-repository`, the suggestion staying open), never run in place. The
card (`suggestionWhereWords`) renders only the plan: "a new worktree on its own
branch" (or "of <repository>" from a subfolder), "in place", "in place: not a git
checkout, so no worktree", or "A new scratch folder"; the icon follows the plan.
`subfleet suggest` also tells the agent when its worktree request will run in place.

Regressions: `test_r1b_the_card_says_where_start_will_run_and_start_does_that`
(five plans, their words and icons, then Start's actual kind for each),
`test_r1b_where_a_suggestion_starts_is_decided_when_it_is_made` (both changes of
the folder after creation).

## Contract re-check (C-25.6, C-25.7, C-29.14)

Each sentence was compared with the code read this session.

- C-25.6 is true of `peers.judge`: the ancestry walk (`ps -axo pid,ppid,tty`),
  markers read from each process's environment (`ps -E`), `proc_pidpath` for the
  app, a controlling terminal otherwise. Unchanged.
- C-25.7: rewritten where the code changed (the claim, the first message's id, the
  admission rule, the reserved namespace, the plan, the scrubbed and bounded
  errors, `watch_seq`, the refused folder and session). Two sentences were not
  true and are fixed: "No agent path starts a conversation" claimed more than
  C-25.6 gives (a process that double-forks out of its guardian's tree and takes a
  new terminal passes the peer check, for every person-only op); it now says only a
  caller C-25.6 accepts starts one, and no agent-open op does. "after 2, 4, ... s"
  was false (the code waited 4, 8, 16 s): the code now waits 2, 4, 8, ... s, at most
  300 (`test_r1b_the_daemon_retries_a_claimed_start_after_2_4_8_seconds_at_most_every_5_minutes`).
- C-29.14: the card's "where" and the merge rule rewritten as above; the rest
  (two places, Starting…/why/Start again/Started/Dismissed/Withdrawn, permissions
  behind confirmation, list read on connect and with the conversation list) holds.

Re-checked at `9787fe323`, after the finishing round, against the code read this
session (`suggestions.py` whole; in `service.py` the suggestion ops, `_suggestion_caller`,
`_check_request_id`, `_cut_worktree`'s stderr handling, `_finish_start`, `_resume_start`;
`peers.judge`; `Suggestions.swift` and the `UIModel` calls). No sentence was found false.

- C-25.6: unchanged since `a2460a011`, as is `peers.py`.
- The finishing round's C-25.7 sentences:
  - "A folder path, caller session or calling job the C-25.5 scrubber would change
    is refused ... (exit 2 `bad-workspace`, `bad-session-id`, `bad-calling-job`)":
    `_suggestion_workspace` checks the canonical folder (`holds_secret`), and
    `op_suggestion_create` checks the canonical session and the job as stated.
    `ConversationError`'s default code is 2.
  - "a cut ... (a title's, a summary's, an error's, a Start's recorded verdict's or a
    listed brief's) moves back a word until it leaves none": `bounded`. It is used by
    `normalize` (title, tldr), by `scrubbed_line` (each `start_error` through
    `scrub_error`; `decided_by` in `claim`) and by `view` (a listing's preview).
  - "so no string in a suggestion's row, view, feed row or event is one the scrubber
    recognizes": the other strings are generated ids and timestamps, validated
    provider, model and settings values, and the three refused fields. One more is
    a dismissal's or withdrawal's `decided_by`, which is not scrubbed but is always
    a fixed text: `peers.judge`'s "the Subfleet app" or "a terminal (<tty>)", or
    "creator". `repository` is git's top level of the checked folder, a prefix of
    it, and is not checked on its own.
- C-25.7's "every write of a message ... checks in its own transaction": the
  writers are `submit_message` and `insert_legacy_history` (both call
  `admit_message`) and `create_handoff`. A handoff inserts messages only into the
  new conversation it creates in the same transaction, which no claim can name.
- C-29.14's merge rule: the app reads a whole listing (`apply(suggestions:)`) only
  in `refreshSuggestions`, which is unfiltered, so a missing open suggestion there
  has closed. A by-id read goes through `apply(suggestionRows:readAt:)`.
- An observation, not a contract matter: a git failure inside a Start returns
  `_cut_worktree`'s fix text, "repeat conversation.create with the same
  request_id", which does not apply to a suggestion's conversation (it has no
  request id). Production code is left unchanged; no test shows a defect.

## Adopted probes

The review's probe code is kept under unique basenames, each asserting the fixed
behaviour: `tests/unit/test_suggestions_r1b_adversarial.py` (from
`test_review181_adversarial.py`, `test_review181_claim_poison_followup.py`,
`test_review181_bounds_visibility.py`, `local_unresolved_probes.py`,
`local_workspace.py`), `tests/frontend/test_core_suggestions_r1b.py` (the Swift
probes) and `tests/e2e/test_suggestions_r1b_peer_e2e.py` (the peer probes). Two
adaptations: the scrub-once check now patches `normalize` (Start legitimately
scrubs its errors and `decided_by`, so patching `scrub_secrets` no longer isolates
"the brief is scrubbed once"); the rollback probe takes the base package from
`git archive 4c6a8a9c` and skips where the commit is absent. The review's
observation that NUL, ESC and bidi controls survive in the words is not adopted as
an assertion (it records current behaviour, not a requirement). The reparented-PTY
probe is kept as a strict expected failure: `peers.judge`, read this session,
accepts such a process (its chain ends at launchd, it has no markers and has a
terminal), for every person-only op; closing that is C-25.6's work, not this PR's.

## Invariants (stated for the PR)

I1 one conversation per started suggestion, made by its claim, no two shared, and
every conversation accounted for; I2 only a person's verdict starts or dismisses;
I3 closed never starts and never changes; I4 the per-origin and global bounds;
I5 nothing before the brief, the brief first; I6 `started` exactly when the brief
exists; I7 no recognised secret in any suggestion row, view, feed row or event.
Property-tested in the state machine (300 examples x 40 steps) and the four
secret properties above; differential: the Swift core folds the daemon's own
answers (frontend tests).

## Test results

Head `9787fe323`. Its code is `d59808686`'s: `9787fe323` changed only the
contract. Python 3.14.7, free-threaded build (the worktree's `uv` environment),
on a host at load average 150 to 550 shared with a dozen other lanes. Each run
held the shared suite lock (`/private/tmp/claude-501/subfleet-suites.lock`)
for at most 19 minutes. The runner (`fix-r1-runs/hold_queue.py`) stops a run at
that limit, along with any daemon it left behind. Logs are in `fix-r1-runs/`
beside this report, untracked like the earlier lanes'.

### Suites at the head

| Suite | Files | Result |
| --- | --- | --- |
| Unit, CLI, hook, fake, ledger | `tests/unit/test_suggestions.py`, `test_suggest_cli.py`, `test_suggest_hint.py`, `test_suggestions_r1b_adversarial.py`, `tests/fake/test_suggestions_fake.py`, `tests/unit/test_desktop_ledger.py` | 81 passed, 386 s (see the note below) |

The first row was run by the previous finishing lane (job
`20261011-132418-pr181-fix1-finish`, lock held from 15:54:03). Its worktree's
`subfleet/`, `tests/` and `app/Sources` were compared file by file with this
head's and are identical, so that run covers this head. Its log is
`head-unit-suggestions.log` in that job's `fix-r1-runs/`. The same hold then ran
the secret properties: two of five passed before the hold's 1,140 s limit
stopped it. They are re-run here, one property per hold.
