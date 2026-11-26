# A terminal is not a person (D-PB1, C-25.6)

2026-10-11. Base: `origin/release/217` at `be7afd599`. Max agreed in d1350 that this is fixed first, before any phone-access bridge adds a new kind of person on the same boundary.

The work is in four commits on top of `be7afd599`:

- `cc004b63a`: the fix and its unit tests.
- `52c10e526`: the end-to-end person, plus the C-25.6, D-8 and ledger text.
- `a43126806`: every e2e daemon knows that person.
- The commit that adds this report.

The branch `fix/person-boundary-terminal` is checked out in the caller's checkout at `be7afd599`, so the job's worktree could not move it. The commits sit on the job's detached HEAD, and Subfleet keeps them under `refs/subfleet-salvage/`.

## The defect

`subfleet/conversations/peers.py` `judge` decides who may run the person-only operations of C-25.6. Before this change it worked in three steps:

1. It vetoed a caller if any process in its current ancestry was a guardian (`subfleet.guardian` in the command line) or carried a Subfleet marker (`SUBFLEET_ATTEMPT=`, `SUBFLEET_JOB=`, or `SUBFLEET_ROOT=<this root>`).
2. It accepted the installed app's executable, read with `proc_pidpath`, and on a development root the development build (D-21).
3. Otherwise it accepted **any caller with a controlling terminal** (`peers.py:108-109` at `be7afd599`).

Step 1 reads only the ancestry as it is now, from `ps`. A process an agent started can drop its markers, hold or open a pseudo-terminal, and outlive its parent. Once launchd adopts it, nothing above it is Subfleet's, so step 3 gave it person authority. On this base that authority covered approving or denying tool calls, reading an approval's full input and nonce, steering, resolving an ambiguous delivery, unblocking, widening permissions, setting `allow_main`, and creating or handing off a conversation above Ask or on main. On #181's branch it also covers starting and dismissing a suggestion. The critique (`critic.md`, shared problem 4) adds that an SSH login, for example over Tailscale SSH, also has a terminal and no markers, so it passed too.

Refusing only callers whose parent is launchd would not close this. The orphan can fork a child with a terminal of its own, and that child's parent is the orphan, not launchd.

## The fix

| File | Change |
|---|---|
| `subfleet/conversations/peers.py:14-21` | The module docstring says a controlling terminal is not a person, and why. |
| `subfleet/conversations/peers.py:36, 43` | `NO_TERMINAL` holds the `ps` spellings of "no terminal". The caller's terminal is read only to word the refusal. |
| `subfleet/conversations/peers.py:101-123` | `judge`: the vetoes are unchanged; then the app's executable (or the D-21 development build) is accepted, and nothing else is. A caller with a terminal is told: "this needs the Subfleet app; a terminal (ttysNNN) is not enough, because a process an agent started can hold one". Any other caller is told "this needs the Subfleet app". |
| `subfleet/conversations/service.py:279-286` | `_person`'s docstring changes. Its refusal does not: `person-only`, exit 7, with the fix "answer it in the Subfleet app". `_person` is `judge`'s only caller, and its nine call sites are the C-25.6 operations. |
| `docs/acceptance-contract.md:466` | C-25.6 drops "or a process with a controlling terminal" and says why. It also names `conversation.create` and `conversation.handoff` above Ask or on main (IR-21), which the code already checked. |
| `docs/acceptance-contract.md:9` | A dated Changes line naming D-PB1 and d1350. |
| `docs/desktop/design.md:185-200` | D-8 says the same. |
| `docs/desktop/ledger.json:281` | Row P-3 notes the change and its tests. |
| `tests/e2e/dev_app.py` | New: the end-to-end tests' person, a development-app stand-in (described below). `NO_COMPILER` and `compiler()` are at lines 66-71. |
| `tests/e2e/conftest.py:21, 97-99, 322-345` | The session fixture `dev_app` compiles the stand-in once. The `e2e` fixture names it in `SUBFLEET_DEV_APP_EXECUTABLE` to every daemon it starts, or records on `E2E.no_person` why it could not. |
| `tests/e2e/test_conversations.py:66-86` | `as_person` sends person-only requests as the stand-in. It skips only on a host with no C compiler; a stand-in that did not compile, or a harness made without the fixture, fails the test. |
| `tests/e2e/test_conversations.py:88-101` | New `as_terminal`: the same request through `script(1)`'s terminal, which is now refused. Four tests use it, for `approval.get`, `approval.respond`, `conversation.unblock`, `message.steer` and `message.resolve`. |
| `tests/e2e/test_conversations.py:162-170` | New: the `e2e` fixture names a compiled stand-in, at a path with spaces, to its daemon. |
| `tests/unit/test_person_boundary.py` | New: the regressions and properties. |
| `tests/unit/test_conversation_peers_attachments.py:63-69` | The old "a terminal is a person" test now says a terminal is not. |

## What changes for Max: every path that relied on terminal acceptance

I listed every operation each `subfleet` entry point and tool sends over the daemon socket, by searching `subfleet/` and `tools/` for each `.call(` site:

- `cli.py`: `capabilities`, `conversation.wake`, `daemon.status`, `kill`, `lanes`, `readings`, `list`, `notice.ack`, `notice.list`, `notice.withdraw`, `pick`, `ping`, `show`, `submit`, `wait`, `why`.
- `client.py`: `list`, the lookup a settled submit makes after a lost answer.
- `operations.py`: `operations`.
- `hooks.py`: `notice.pending`, `notice.mark`, `list`, `wait`.
- `sessions/client.py`: `sessions`, `ping`, `submit`.
- `gate/cli.py`: `gate.start`, `gate.continue`, `gate.poll`.
- `doctor.py`: `daemon.status`, `ping`.
- `tools/measure_release_gates.py`: `daemon.status`, `lanes`, `readings`.
- `tools/store_contention_repro.py`: `ping`, `submit`, `wait`, `list`, `notice.pending`, `show`, `daemon.status`.

None of these is a C-25.6 operation. Outside `subfleet/conversations/` and `subfleet/protocol.py`, the only file that names one is `subfleet/descriptors.py`, which lists `approval.get` among the read-only conversation ops and sends nothing. Every test that reaches `_person`, other than the ones this change adds, replaces it with a stub. These are:

- `tests/frontend/daemon_harness.py`
- `tests/fake/test_folder_spelling.py`
- `tests/unit/test_app_cutover_daemon.py`
- `tests/unit/test_conversation_fifo_reads.py`
- `tests/unit/test_conversation_service.py`
- `tests/unit/test_conversation_steer_service.py`
- `tests/unit/test_legacy_hold.py`

So on this base the shipped CLI changes for no one. The one CLI path that does rely on a terminal is in #181, which is not merged. It is the `suggest` row below.

| Path | Person-only op it sends | Before | After |
|---|---|---|---|
| Every `subfleet` subcommand on this base (the `compat.py` verb table, `cli.py`, sessions kit, gate, retention, doctor, hooks, operations) | none | n/a | Unchanged |
| `subfleet wake` | `conversation.wake`, which is not person-only | Accepted | Unchanged |
| `subfleet handoff` | none: it submits a sessions-kit job and does not call `conversation.handoff` | n/a | Unchanged |
| **`subfleet suggest --start ID` and `subfleet suggest --dismiss ID`** (#181, `feat/suggested-conversations` at `9787fe323`, not on this base) | `suggestion.start`, `suggestion.dismiss`, both through `_person` | Accepted from a terminal; the help says "(a person's, from a terminal)" | **Refused, exit 7, once both changes are in.** The CLI passes the daemon's refusal through (`cmd_suggest` calls `fail(exc.code, str(exc), exc.fix)`), so it prints `subfleet: person-only: starting a suggested conversation is a person's decision: this needs the Subfleet app; a terminal (ttysNNN) is not enough, because a process an agent started can hold one` and `fix: answer it in the Subfleet app`. Its help text needs changing on merge (see "Landing beside #181"). |
| The Subfleet app (`/Applications/Subfleet.app`) | all of them | Accepted | **Unchanged: accepted** |
| A development build (`app/build.sh --dev`, against `python -m tests.e2e.dev_daemon --app <exe>`, or any root other than `~/.subfleet` with `SUBFLEET_DEV_APP_EXECUTABLE`) | all of them | Accepted | **Unchanged: accepted (D-21)** |
| A hand-written socket script run in Terminal (no shipped tool does this) | any | Accepted | **Refused, exit 7**, naming the app |
| An SSH login to the Mac, Tailscale SSH included | any | Accepted | **Refused, exit 7**, same message. This was the only route to these operations from the phone that needed no new code (critique, shared problem 4); it is now closed. |
| An agent's orphan holding a pseudo-terminal, or a terminal child it forks | any | **Accepted (the defect)** | **Refused, exit 7** |
| `tests/e2e/test_conversations.py` `as_person` (14 tests: approvals, questions, unblock, resolve, steer, restart mid-approval) | `approval.get`, `approval.respond`, `conversation.unblock`, `message.resolve`, `message.steer` | Sent through `script(1)`'s terminal | **Routed through the app mechanism**: sent as the development-app stand-in, which the daemon was started knowing (D-21) |
| `tests/frontend/test_core_live.py` | approvals | Already the development app (the Swift probe) | Unchanged |
| Tests that stub `_person` (listed above) | n/a | Stubbed | Unchanged |

For Max: nothing you run from the `subfleet` CLI on this base changes. Approving, steering, resolving, unblocking and widening are now done only in the Subfleet app, or in a development build on a development root. A terminal, an SSH session, or an agent's leftover process whose image is outside the allowed app path set gets exit 7 naming the app. Once #181 lands, `subfleet suggest --start` and `--dismiss` get that refusal too, so suggestions are started and dismissed in the app's Suggested section.

No existing mechanism routes a CLI request through the app. The app reads no command-line arguments, URLs or Apple events: `app/Sources` has no `CommandLine`, `ProcessInfo…arguments`, `onOpenURL`, `openURLs` or `NSAppleEventManager` (searched this session). So of the brief's two options, only refusing with the app named is available.

### The end-to-end person: `tests/e2e/dev_app.py`

The daemon knows the app by the kernel's path for the caller's image (`proc_pidpath`), not by a script's name. So the stand-in is a native client: about 40 lines of C, compiled once per test session with `cc`, that does what the app does on the socket (one request, one reply line). It sits at `…/Subfleet Dev.app/Contents/MacOS/Subfleet Dev`, a path with spaces, as the development build's is.

The `e2e` fixture in `tests/e2e/conftest.py` names it in `SUBFLEET_DEV_APP_EXECUTABLE` to every daemon it starts, in every e2e module, not only `test_conversations.py`. Those daemons run on state roots under `/tmp`, never `~/.subfleet`, so D-21 lets the name widen nothing else. The stand-in runs in a session of its own (`start_new_session=True`) with no controlling terminal, so its image alone admits it.

`as_person` skips only when the host has no C compiler. A stand-in that does not compile, or a harness made with `E2E(...)` instead of the fixture, fails the test with the reason. So a person test cannot pass by skipping. A compile failure leaves the e2e tests that need no person running.

## Landing beside #181

#181 (`feat/suggested-conversations`, local ref at `9787fe323`) is where D-PB1 was found, and it is not on this base. I checked how the two meet, by reading the code and with `git merge-tree`; I ran none of #181's tests.

1. **One new conflict, in `docs/acceptance-contract.md`.** `git merge-tree HEAD 9787fe323` conflicts in two files. `subfleet/conversations/store.py` already conflicts between #181 and `be7afd599`, so that one is not this branch's. The contract has two hunks:
   - In the Changes list, keep both lines.
   - In C-25.6, take this branch's text and add `suggestion.start` and `suggestion.dismiss` (C-25.7) to its list of operations.
2. **#181's strict expected failure flips.** `tests/e2e/test_suggestions_r1b_peer_e2e.py::test_r1b_a_reparented_child_with_its_own_terminal_cannot_decide` is `xfail(strict=True)`, and its docstring says it "must be revisited, the day C-25.6 closes that path". I worked out from the code, without running it, that it starts passing after the merge:
   - The probe's orphan has a launchd parent, no `SUBFLEET_` variables and its own terminal, so no veto applies.
   - Its image is Python, not the app, so `judge` refuses it with exit 7, which is what the probe asserts.

   Under `strict=True` a pass is reported as a failure. Whichever change lands second should drop the marker, so the probe becomes this fix's real-orphan regression. It spawns a real orphan holding a pseudo-terminal, which this job's brief ruled out, so I did not run it here.
3. **The `suggest` help text becomes wrong.** `--start` and `--dismiss` say "(a person's, from a terminal)", and `cmd_suggest`'s docstring says "from a terminal, start or dismiss one". Both should say the operation needs the Subfleet app, or the two flags should go.
4. **#181's e2e person now has a person.** `tests/e2e/test_suggestions_e2e.py` calls `Conversations(e2e).as_person("suggestion.start")`. Before `a43126806`, the stand-in was named only inside `test_conversations.py`, so this test would have skipped every person step with "no C compiler", which was not the reason. With the fixture in `conftest.py` it is sent as the development app. Its module docstring calls `as_person` "a terminal", and should be updated.
5. **The rest of #181 is unaffected.** `r1b_daemon` builds `E2E(...)` itself and makes no person request. #181's unit, fake and property tests stub `_person`, and its frontend tests use the Swift probe as the development app.

## Tests

The regressions are driven through the existing process-inspection fakes (`chain=`, `executable=`). No process with a terminal is started, and no orphan is made.

Before is a `git archive` of `be7afd599` with this branch's two unit test files (and `tests/e2e/dev_app.py`) copied in, run with the worktree's interpreter. After is this branch.

| Regression | Test | Before (`be7afd599`) | After |
|---|---|---|---|
| An orphan (parent launchd) with a PTY and no markers is refused for every person-only op | `test_a_terminal_without_subfleet_ancestry_is_not_a_person[orphan]` and `test_every_person_only_op_refuses_a_terminal_caller_with_exit_7_naming_the_app[orphan-*]` (11 ops) | 12 failed | 12 passed |
| Its forked terminal child is refused | `…[orphans-child]` and `…[orphans-child-*]` | 12 failed | 12 passed |
| A terminal with no Subfleet ancestry (an SSH login, a Terminal window) is refused | `…[ssh-login]`, `…[terminal-window]` and their `-*` cases, plus `test_neither_a_terminal_nor_a_headless_agent_is_a_person` | 25 failed | 25 passed |
| The app's executable is still accepted, and so is the development build on a development root | `test_the_app_is_still_a_person_whatever_its_ancestry_and_terminal` (4), `test_every_person_only_op_lets_the_app_past_its_person_check` (11), `test_the_development_build_is_the_app_only_on_a_development_state_root` | 15 passed, 1 failed (the development-build test, on the base's refusal wording "only the Subfleet app or a terminal may do this", which this PR changes) | 16 passed |
| The marker and guardian vetoes are unchanged | `test_the_marker_and_guardian_vetoes_are_unchanged_and_bind_the_app_too` (24), `test_another_state_roots_marker_is_not_a_veto`, `test_an_unidentified_or_uninspectable_caller_is_refused` | 25 passed, 1 failed (`test_an_unidentified_or_uninspectable_caller_is_refused`: the base accepted an unidentified caller with a terminal, `Verdict(person=True, reason='a terminal (ttys001)')`, the defect itself; all 24 veto cases passed) | 26 passed |

The 11 person-only ops on this base are:

- `approval.get`
- `approval.respond`
- `message.steer`
- `message.resolve`
- `conversation.unblock`
- `conversation.settings` widening
- `conversation.settings` with `allow_main`
- `conversation.create` above Ask
- `conversation.create` with `allow_main`
- `conversation.handoff` above Ask
- `conversation.handoff` with `allow_main`

### Properties

Hypothesis generates the ancestry chains:

- Each chain has 1 to 8 processes with distinct pids, and ends at launchd or at a parent that has vanished.
- Each process gets one of 8 terminal spellings.
- Command lines mix harmless words, Subfleet-looking non-markers and the real markers.
- The caller's image is one of 10 paths, including near misses of the app's.
- The root is none, a development root, or `~/.subfleet`, and the development build is known or not.

| Property | Examples | Before | After |
|---|---|---|---|
| Accepted if and only if the caller's image is the app (or the known development build) and no process in the chain is a guardian or carries a marker | 2,000 | failed (shrunk counterexample below) | 2,000 passed (368 invalid) |
| Giving any process a terminal, or taking one away, never changes the verdict | 1,000 | failed (shrunk counterexample below) | 1,000 passed (186 invalid) |
| A guardian or marker on any one process refuses the caller, whoever it is | 1,000 | 1,000 passed (239 invalid) | 1,000 passed (245 invalid) |

On the base, Hypothesis shrank both failures to a caller that is not the app and has a terminal:

- The first property failed with a four-process chain. The caller (pid 2) holds `ttys000`, its ancestors have no terminal or marker, and its image is the development build while no development build is known (`dev_known=False`), so it is not the app. The base judged it `Verdict(person=True, reason='a terminal (ttys000)')`; the property expects `person=False`.
- The second failed with a single process `Proc(pid=2, ppid=2, tty='ttys000')`: with `ttys000` the base said `person=True`, and with every terminal replaced by `??` it said `person=False`. Giving a terminal changed the verdict.

The veto property passed on the base, as it should: the vetoes are unchanged.

### The end-to-end path, checked from inside a lane

`test_the_end_to_end_tests_development_app_is_the_app_to_the_real_peer_check` compiles the stand-in and has it connect to a test socket. The server reads the caller with the real `LOCAL_PEERPID` and `proc_pidpath`, then judges it with only the ancestry faked marker-free. The stand-in is recognised as the app, with its path's spaces kept, and a Python client on the same socket gets "this needs the Subfleet app". Passed at the head, on the hub's host. On the base it cannot run: `tests.e2e.dev_app` is new in this PR (ImportError).

`test_the_end_to_end_person_is_never_skipped_for_the_wrong_reason` checks the three outcomes of `as_person` when no stand-in is named: it fails, fails with the compile error, or skips only for no compiler. Passed at the head. On the base it cannot run: `tests.e2e.dev_app` is new in this PR (ImportError).

### Runs

The hub ran both files on the host (`git archive` copies, one pytest per hold of the shared suite lock):

- 2026-10-12 08:48Z (`~/reviews/subfleet-hub/host-runs/run-191-tests.log`). At f2b22573d, `tests/unit/test_person_boundary.py` 97 passed and `tests/unit/test_conversation_peers_attachments.py` 44 passed. With both files copied onto be7afd599, the first file failed and the second gave 1 failed, 43 passed.
- 2026-10-12 13:05Z, verbose with statistics (`~/reviews/subfleet-hub/host-runs/run-191-verbose.log`, outputs in `out-191v/`). At f2b22573d both files gave 141 passed. On be7afd599 they gave 57 failed, 84 passed. The tables above come from this run.

The end-to-end person and terminal checks under `tests/e2e/` need a caller outside any Subfleet lane; CI runs them.

## Not changed, and what is left

- **The app's image is the only positive test.** An agent could start the app's executable itself, and that process is the app, so it passes. The app reads no command-line arguments, URLs or Apple events (searched above). This does not prove who wrote queued socket bytes; the exec-before-token-read limitation below remains. Whether code can be injected into the app's process depends on how the build is signed, which this change does not touch and I did not check.
- **The vetoes are as they were.** They read a `ps` snapshot, and stop at pid 1, at a repeated or missing pid, or after 64 processes. `SUBFLEET_ROOT=<root>` matches as a substring, so a daemon on `/Users/m/.subfleet` also vetoes `SUBFLEET_ROOT=/Users/m/.subfleet-dev`. That errs toward refusing.
- **The e2e person and terminal checks did not run here.** Inside a Subfleet lane every ancestor carries the lane's markers, so `as_person` and `as_terminal` skip by design. They run in CI or from a terminal outside Subfleet. This job did not push, so CI has not run them on this head.
- **Phone access (d1350)** would need a new positive authority, such as per-request signatures from a paired device. Nothing here adds one, and a terminal can no longer stand in for it.


## Review round 1: connection-time image identity (2026-10-12)

The review at `3cbd81d5c7c3` confirmed D-PB1 and found two inherited bypasses:
a queued non-app request became authorized if its sender exec'd the app,
and resolving a kernel path with `realpath` could bless a different running
image after a filesystem name was swapped for a symlink. Round 1 closed the
exec case only when the token was saved before exec, and closed the path swap.
The delayed first-token-read case remained; round 2 records it below.

`daemon._connection` saves `LOCAL_PEERTOKEN` (SOL_LOCAL 0, option 6, 32 bytes)
before reading requests and passes that immutable token to every conversation
request. Its word 5 supplies the ancestry pid and its word 7 binds the process
version. `peers.judge` uses `proc_pidpath_audittoken` to inspect that instance;
it refuses a missing token and failed or unavailable lookup, without falling
back to `proc_pidpath`. Once the token has been saved, exec, exit and pid reuse
cannot authorize that connection with a later image. `proc_pidpath` remains a diagnostic helper.

`ConversationService` freezes the configured executable spellings and their
real paths when it is constructed at daemon startup. Neither environment
changes nor later symlink targets change that set. `judge` compares the
kernel answer verbatim; a real symlink launch already reports the target's
canonical path.

The moved-bundle compatibility choice is **restart the app**. Renaming an
allowed running bundle moves its kernel pathname outside the frozen set, so
it is refused with a reason naming the restart. The audit token proves the
process instance but does not attest that a renamed path used to designate
an allowed app. Accepting arbitrary renamed images would require another
trusted identity mechanism. C-25.6 and the module docstring state this choice.

The new `tests/unit/test_person_boundary_identity.py` ports the review's native
probes, compiling `tests/e2e/dev_app.py` into disposable test bundles. It covers
connection token retention, exec into the app after connecting, exited peers,
a mismatched pid version (simulated reuse against a real live process), direct
and symlink launches, a swap immediately after real token lookup, a moved
running bundle, lookup failures and real socket-pair audit-token pid reads.
The existing two unit files now supply synthetic audit tokens alongside their
ancestry and executable fakes; their native client reads a real token.

### Validation in this workspace

The shared suite lock prevented all three baseline runs from starting. Each
file was attempted separately, in the foreground, with its own TMPDIR and
`lockf -k -t 120` on `/private/tmp/claude-501/subfleet-suites.lock`, followed
by the requested Python cleanup. Each returned exit 75 (`already locked`)
before pytest started. No lock was held while waiting for lower host load.

| File under `tests/unit/` | Cases enumerated from source | Executed here |
|---|---:|---|
| `test_person_boundary.py` | 97 | 0; lock queue timed out |
| `test_conversation_peers_attachments.py` | 44 | 0; lock queue timed out |
| `test_person_boundary_identity.py` | 18 | 0; lock queue timed out |

These counts include parametrization and are static counts, not collection
or pass results. All six changed Python files passed syntax compilation, and
`git diff --check` passed. Dependencies matching `uv.lock` were copied from
the caller's environment into this workspace's independent environment;
`UV_NO_SYNC=1` and a workspace-local `UV_CACHE_DIR` keep `uv run pytest` local.
Neither the caller's environment nor a network remote was changed.

The sandbox refused a write to the worktree's `.git` metadata link. Commits
therefore use the assigned workspace's `.git-local`, on
`fix/person-boundary-terminal`, with the original repository's objects as
read-only alternates. The shared index, HEAD and branches were not written.

### Mutation targets

Each mutation is applied alone and restored in a `finally` block. Each uses
one foreground pytest process under a separate lock hold, with a 60-second
queue bound. The selected assertions are the expected failures below; they
are not claimed as observed until the lock admits pytest.

| Mutation | Expected failing cases in `test_person_boundary_identity.py` |
|---|---|
| Compare the returned path through `realpath` again | `test_a_kernel_path_swapped_after_lookup_is_not_resolved_again`; `test_swapping_a_path_to_a_symlink_after_lookup_cannot_change_the_verdict` (2 cases) |
| Replace token lookup with `executable_path(token.pid)` | `test_failed_or_unavailable_token_lookup_is_a_refusal` (3 parameters); `test_a_client_that_execs_into_the_app_after_connecting_is_refused`; `test_a_reused_pid_version_is_refused_even_if_the_current_image_is_the_app` (5 cases) |
| Accept a failed token lookup | The same 5 cases as above, plus `test_a_client_whose_process_is_gone_is_refused` (6 cases) |

| Mutation attempt | Observed result |
|---|---|
| `realpath` | Unrun; lock queue returned exit 75 before pytest started |
| bare pid | Unrun; lock queue returned exit 75 before pytest started |
| accept failed lookup | Unrun; lock queue returned exit 75 before pytest started |

All three mutations were restored. No failure count is claimed from these
unrun attempts. The hub must run the three baseline files and these mutation
selections on the host; the native cases and their compiler were not started
here. All task-owned foreground runners have exited.


## Review round 2: early capture and request-time recheck (2026-10-12)

Base: `9ed60d41dad1`. The hub's host validation of that base passed the identity
file's 18 cases and the boundary/attachment modules' 142 cases. Review round 2
then demonstrated that `LOCAL_PEERTOKEN` read on an accepted socket after exec
returns the new image's token. Saving it in the reader thread left connection
admission and thread startup before the first token read.

The daemon now reads `LOCAL_PEERTOKEN` on the accept thread as the very next
statement after `accept()` returns, before recovery logging, connection-cap
accounting, locking or reader creation. It passes that token through
`_hold_connection`, `_read_connection` and `_connection`; the reader never
replaces it. The app's `DaemonClient.swift` connects to this `daemon.sock`
listener; there is no separate app-facing listener accepting person requests.
The runner relay's listener serves provider traffic, not these operations.

The saved `AuditToken` carries the socket for a fresh read, excluded from token
equality and hashing so poll ownership keeps its existing token identity.
`ConversationService._person` calls `peers.judge` after the full request line
has been read and before acting. `judge` re-reads `LOCAL_PEERTOKEN` and requires
both pid and pid version to match the saved token. An unreadable token or a
difference refuses with `person-only`, exit 7, naming the failed re-read or
changed process/image and telling the caller to reconnect from the app. The
existing ancestry vetoes and saved-token executable lookup still follow.
Non-person operations retain their existing behavior.

### The remaining case and the follow-up

A same-user process that replaces its image with the app's executable before
the daemon reads its token can still be taken for the app, even for request
bytes written by its earlier image. The kernel reports the process as it is
now, so an image check cannot establish which program wrote those bytes.
The check therefore separates the app from Subfleet's own launched processes
only as far as those processes do not do that. The follow-up, an app-held
credential that proves each person-only request, is what closes this case.
C-25.6 and the `peers.py` module docstring now state this explicitly, and the
round-1 claims above have been narrowed to exec after the saved token read.

### Regression coverage

Two native tests create a real socket pair in a disposable Python child and
pass the server end to pytest with `SCM_RIGHTS`, so the peer token identifies
that child. The child writes a complete `message.resolve` request and waits
for the test to release it into exec of the compiled `tests/e2e/dev_app.py`
stand-in. The stand-in's fresh connection is the readiness barrier; there is
no sleep or pid polling. Only ancestry is faked marker-free, as in round 1.
The original queued request goes through the real service person check;
its downstream action is recorded only if the check accepts it.

- A token saved before exec, followed by reading the queued request after
  exec, refuses with exit 7 and records no action.
- A first token read delayed until after exec accepts the queued request.
  This assertion is marked `documented_limitation`, so the credential
  follow-up must deliberately change it.
- Synthetic cases cover matching identity, changed pid, changed pid version
  and a failed token re-read. Another test fixes the order immediately after
  accept; the existing retention test checks every request keeps that token.
- The connection shutdown test wrapper passes the saved token through the
  updated reader signature.

### Validation in the round-2 workspace

All five invocations returned exit 75 after a 120-second shared-lock queue
wait, before pytest started. In total, 0 tests executed in this workspace. Each invocation is a single
foreground `uv run pytest -q` under its own `lockf -k -t 120` hold attempt on
`/private/tmp/claude-501/subfleet-suites.lock`, with its own
`TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)sf-r191f2-$$/`. A task-owned supervisor
bounds the whole invocation to 1080 seconds plus at most 40 seconds of shutdown,
so any hold stays below 20 minutes. There is no wait for calmer host load while
holding the lock. The requested Python `shutil.rmtree` cleanup runs afterward.

| Invocation | Executed in this workspace | Outcome |
|---|---:|---|
| `tests/unit/test_person_boundary_identity.py` | 0 | exit 75; lock queue timed out |
| `tests/unit/test_person_boundary.py` | 0 | exit 75; lock queue timed out |
| `tests/unit/test_conversation_peers_attachments.py` | 0 | exit 75; lock queue timed out |
| `--pyargs tests.unit.test_person_boundary_identity tests.unit.test_person_boundary tests.unit.test_conversation_peers_attachments` | 0 | exit 75; lock queue timed out |
| `tests/unit/test_daemon_connections.py tests/unit/test_daemon_accept.py` | 0 | exit 75; lock queue timed out |

The identity file now has 25 cases counted from source (18 existing, 7 added),
including the two native exec-ordering cases. This is a static count, not a
collection or pass result. All four changed Python files passed syntax
compilation; `git diff --check` passed.

The shared Git metadata remains read-only to the sandbox: an attempted index
write was refused. Commit `bba8c771d` contains the fix, tests and C-25.6 text on
`fix/person-boundary-terminal` in the workspace's `.git-local`, with the caller's
repository objects as read-only alternates. The report is a separate commit
on that same branch. No shared index, caller checkout or network remote was
written. The test environment was copied from the caller's existing
environment into this workspace, its pytest entry point was changed to use
the workspace interpreter, and `UV_NO_SYNC=1` plus workspace-local
`UV_CACHE_DIR` keep these invocations local.


All five requested TMPDIR cleanups completed. No pytest process, compiler or
native probe started, and all task-owned foreground runners have exited.
The hub must run the three required files, their module-name invocation and
the supplemental daemon checks on the host; no runtime pass count is claimed
from this workspace.
