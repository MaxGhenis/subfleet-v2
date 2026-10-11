# Suggested conversations in the Subfleet app

Built 2026-10-10 to 2026-10-11 on a detached checkout of `release/217` at 4c6a8a9c. The branch
`feat/suggested-conversations` is checked out in the caller's worktree, so the commits sit on this
job's detached HEAD; Subfleet keeps them as a salvage ref.

## Status of this report

Test results: see "Tests" below. That section says exactly what ran and what did not.

## What an agent and a person can now do

- An agent proposes a separate conversation with
  `subfleet suggest --title T --tldr S (--brief-file F | --brief -) [--workspace DIR | --no-workspace] [--worktree] [--provider claude|codex] [--model M] [--request-id UUID]`.
  It can also withdraw one (`--withdraw ID`) and list them (`--list`). From a terminal, a person can start
  (`--start ID [--permission P]`) or dismiss (`--dismiss ID`) one.
- The app shows each suggestion in two places:
  - a card in the timeline of the conversation that suggested it, on the turn that suggested it;
  - a **Suggested** section at the top of the sidebar, listing every open suggestion (with or without an
    origin), with a count.
- **Start** creates the conversation as `conversation.create` would, submits the brief as its first message, and
  opens it. It is a worktree conversation when the suggestion asks for one and the folder is a git checkout.
- **Dismiss** closes the suggestion and nothing starts. The card stays and says what happened.

Screenshots (offscreen, 1440 x 900 @2x, dark and light), in this folder:

| File | What it shows |
|---|---|
| `suggestion-card-{dark,light}.png` | The card in the conversation that suggested it; the sidebar's Suggested section with its count |
| `suggestion-card-started-{dark,light}.png` | The same card once started (Started, Open the conversation) |
| `suggestion-detail-{dark,light}.png` | A suggestion with no origin, opened from the sidebar, brief expanded; a Codex suggestion offers only a read-only Start |

## Design as built, and where it departs from the hub's design

The hub's design is implemented. The departures below follow what the code showed; each gives its reason.

1. **No numbered migration; the store stays schema 2.** The `suggestions` table is created with
   `CREATE TABLE IF NOT EXISTS`, and `changes.suggestion_id` is added as a column. This is how the seven wake
   tables (C-24.10), `legacy_hold` and the turn-window columns were added. `ConversationStore` refuses any store
   whose schema is newer than its own (`schema` refusal, exit 1). A step to schema 3 would therefore make every
   retained 2.1.x build refuse `conversations.sqlite3`, and roll-back would lose every conversation, not just
   suggestions. An additive table changes nothing an older build reads; such a build simply ignores the table.
   The job store's schema is untouched.
2. **A fifth state, `starting`.** This is the person's claim. The transaction that moves `open` to `starting`
   also writes the new conversation's row. That makes "dismissed or withdrawn never starts" a property of one
   SQLite transaction, not of call order:
   - a suggestion that has a conversation cannot be dismissed or withdrawn (`too-late`);
   - a dismissed or withdrawn one never gets a conversation (`not-open`).

   `started` is recorded in the transaction that accepts the first message, through a new `on_accept` hook on
   `ConversationStore.submit_message`. So `started` holds exactly when that message exists.
3. **Idempotent start, and the daemon finishes a start cut short.**
   - The conversation's request id is `suggestion:<id>`, and the first message's id is a UUIDv5 of the
     suggestion id.
   - A retry, a second click or a restart converges on one conversation and one message.
   - A start that fails after the claim (git, a crash) keeps the claim and records `start_error`. **Start again**
     finishes it. The daemon also finishes it: a tick step submits the work to the file pool once the claim is
     30 s old, then backs off 2, 4, ... s, up to 5 minutes. This step never runs on the control loop.
4. **Words are scrubbed once, at creation.** Title, summary and brief pass the handoff scrubber (C-23.14) when
   the suggestion is made. The stored brief is therefore what the card shows, what the feed and events carry,
   and what Start sends. The scrub count is returned to the agent and printed by the CLI. Brief newlines are
   normalized as `message_text` normalizes them, so the first message is byte-for-byte the brief.
5. **Origin.** The hub specified the turn's conversation inside a turn and null outside one. One case is added:
   a calling session that a conversation binds counts as that conversation's (the same rule
   `conversation.wake` uses), so a card lands in the conversation the person knows the session as.
   - Per-origin bound for a suggestion with no origin: counted per creating session.
   - Callers with neither an origin nor a session share one bucket.
   - Bounds are 20 per origin and 200 open in all. Both count `open` and `starting`.
6. **Start settings.**
   - Model: the hinted model, or `models.list`'s default for the provider.
   - Effort: null, so C-26.8's default applies.
   - Permission: **Ask**, or read-only for Codex while Codex conversations are.
   - Wider permissions: the card's chevron menu offers them, each behind a confirmation dialog. The daemon
     requires `confirm_widen: true`, as `conversation.create` does.
7. **Model hint.** It must be a model the fleet routes for the provider (`policy_model`, C-26.1) and not
   retired. A short name (`opus`) becomes the `models.list` value. A catalog value the account has not shown
   yet is checked by the driver before sending (C-26.8), as for any conversation.
8. **Folder.** At creation it is checked exactly as the start would check it (`_validate_workspace`), for Ask
   (read-only for Codex). For example, an in-place suggestion on `main` is refused at once with
   `protected-branch`; the agent learns to add `--worktree`.
9. **Feed and events.** Every state change writes, in its own transaction:
   - one `changes` row naming the suggestion. `changes_after` attaches the current view; a suggestion with no
     origin uses conversation id `""`. An older app reads that row as "refresh the list"; the new app never
     treats `""` as a conversation;
   - for a suggestion with an origin, one `suggestion` event on the turn's message, carrying the view and the
     whole brief. If the 64 KiB row cap would be exceeded, it carries the 2,000-character preview instead.
   - Views never carry the request id, digest, start settings or calling job.
10. **Protocol.** The five ops are additive, advertised by the `suggestions.v1` capability with five limits.
    `conversation_schema` stays 1: no shape the app already decodes changed. A watch row gained two optional
    fields, and `TimelineContent` gained a case the app owns.
11. **The app does not journal Start or Dismiss in the outbox.** They are direct calls, like
    `approval.respond`, `conversation.unblock` and `conversation.rename`. Start is idempotent at the daemon, so a
    retry is always safe. A start cut short is finished by the daemon even if the app never asks again.
12. **No notification for a new suggestion.** The Suggested count is the signal. Adding a notification kind
    is a small follow-up if the owner wants one.

### Discoverability (how an agent learns about the verb)

| Mechanism | Verdict |
|---|---|
| `claude -p` argv (`--append-system-prompt`) | Rejected. `tests/unit/test_claude_conversation_mcp_scope.py` pins every turn's argv unchanged (d714). |
| Turn environment | Rejected. The same test pins `env_add`, and agents do not read their environment unprompted. |
| Codex `thread/start` or `turn/start` fields | Rejected. C-24.9's recording pins every frame of 172 histories. A field not verified against the live app-server could fail every Codex turn. |
| Codex hooks override | Rejected. The override is verified against `hooks/list` and TRUST. |
| **`subfleet hook SessionStart`'s `additionalContext`** | **Chosen.** It already writes context inside turns (C-26.13). Claude Code runs it in `-p` stream-json turns, new and resumed (`hooks.py`, from the 2026-09-24 live probes). |

The chosen channel adds one stable line (`hooks.SUGGEST_HINT`). It does so only inside a conversation turn
(`SUBFLEET_TURN_JOB` set) and only when the session is new, cleared or compacted. A resumed turn's transcript
already holds the line, so it is not repeated every turn. It requires the `subfleet hook` entries to be
installed (`daemon install --hooks`), which notice delivery already requires.

**Gap: Codex turns get no line.** Their agents find the verb only in `subfleet --help`. Closing this needs a
live check that the app-server accepts developer instructions, plus a re-recording of C-24.9's fixture. It is
a follow-up, not something to guess at.

## Contract clauses

- **C-25.7** (new): suggested conversations. Covers the ops, who may call each, the row, the bounds, the claim,
  the idempotent start, the daemon's finishing of a claimed start, the feed and event rows, the hint, and the
  stated caller identity.
- **C-29.14** (new): the card, the Suggested section, Start (Ask, wider behind confirmation), Dismiss, live
  updates, and no regression of state or brief.
- **C-25.2**: lists the five ops and `suggestions.v1`.
- **C-25.3**: names `suggestion.create` and `suggestion.start` among the file-pool ops.
- **C-25.6**: adds `suggestion.start` and `suggestion.dismiss` to the person-only list.
- Changes in version 3: a dated entry.
- `docs/desktop/ledger.json`: rows R-14 (C-25.7) and R-15 (C-29.14).

## Invariants (stated in the PR; tested as properties)

`tests/unit/test_suggestion_properties.py` is a Hypothesis state machine over the real service and stores.
- **Rules:** create from two turn origins, two sessions, or no identity; start by a person or an agent,
  optionally cut short after the claim; dismiss by a person or an agent; withdraw by the creator (an earlier or
  later turn of the same conversation) or by another caller; restart the daemon; let the daemon finish claimed
  starts.
- **Bounds:** shrunk to 3 per origin and 7 in all, so generated sequences reach them.
- **Folder admission:** stubbed. `conversation.create`'s own tests cover it, and running it would put git in
  every step.

The invariants, checked after every step:

- **I1.** A suggestion starts at most one conversation (`request_id = suggestion:<id>`), and no two
  suggestions share one.
- **I2.** Only a person's op starts or dismisses. `decided_by` is the person's verdict for `starting`,
  `started` and `dismissed`, and `creator` for `withdrawn`. An agent's start or dismiss is refused
  `person-only` and changes nothing.
- **I3.** A withdrawn or dismissed suggestion never starts: no conversation, no first message, and its state
  never changes again.
- **I4.** Open suggestions stay within the per-origin and global bounds; a create past a bound is refused
  `too-many-suggestions`, exit 7.
- **I5.** A started conversation's only message is exactly the brief the person saw, under the derived
  message id, at seq 1.
- **I6.** `started` holds exactly when that message exists, and the store agrees with the model's state for
  every suggestion.

Example counts: see "Tests".

## Tests

Pending: filled in once the shared suite lock admits this job's runs.

## Known limits

- The caller's turn job and session are taken as the CLI states them, as `conversation.wake` already takes
  them. A process that names another conversation's turn could withdraw that conversation's open suggestions,
  but could never start one: the person-only check reads every ancestor's environment (C-25.6).
- `status.json` and the menu bar do not count suggestions.
- Codex turns are not told about the verb (see Discoverability).
