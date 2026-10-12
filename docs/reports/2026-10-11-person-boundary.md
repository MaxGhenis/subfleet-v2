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

For Max: nothing you run from the `subfleet` CLI on this base changes. Approving, steering, resolving, unblocking and widening are now done only in the Subfleet app, or in a development build on a development root. A terminal, an SSH session, or an agent's leftover process gets exit 7 naming the app. Once #181 lands, `subfleet suggest --start` and `--dismiss` get that refusal too, so suggestions are started and dismissed in the app's Suggested section.

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
| An orphan (parent launchd) with a PTY and no markers is refused for every person-only op | `test_a_terminal_without_subfleet_ancestry_is_not_a_person[orphan]` and `test_every_person_only_op_refuses_a_terminal_caller_with_exit_7_naming_the_app[orphan-*]` (11 ops) | RESULT_ORPHAN_BEFORE | RESULT_ORPHAN_AFTER |
| Its forked terminal child is refused | `…[orphans-child]` and `…[orphans-child-*]` | RESULT_CHILD_BEFORE | RESULT_CHILD_AFTER |
| A terminal with no Subfleet ancestry (an SSH login, a Terminal window) is refused | `…[ssh-login]`, `…[terminal-window]` and their `-*` cases, plus `test_neither_a_terminal_nor_a_headless_agent_is_a_person` | RESULT_TERM_BEFORE | RESULT_TERM_AFTER |
| The app's executable is still accepted, and so is the development build on a development root | `test_the_app_is_still_a_person_whatever_its_ancestry_and_terminal` (4), `test_every_person_only_op_lets_the_app_past_its_person_check` (11), `test_the_development_build_is_the_app_only_on_a_development_state_root` | RESULT_APP_BEFORE | RESULT_APP_AFTER |
| The marker and guardian vetoes are unchanged | `test_the_marker_and_guardian_vetoes_are_unchanged_and_bind_the_app_too` (24), `test_another_state_roots_marker_is_not_a_veto`, `test_an_unidentified_or_uninspectable_caller_is_refused` | RESULT_VETO_BEFORE | RESULT_VETO_AFTER |

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
| Accepted if and only if the caller's image is the app (or the known development build) and no process in the chain is a guardian or carries a marker | 2,000 | PROP1_BEFORE | PROP1_AFTER |
| Giving any process a terminal, or taking one away, never changes the verdict | 1,000 | PROP2_BEFORE | PROP2_AFTER |
| A guardian or marker on any one process refuses the caller, whoever it is | 1,000 | PROP3_BEFORE | PROP3_AFTER |

PROP_COUNTEREXAMPLE

### The end-to-end path, checked from inside a lane

`test_the_end_to_end_tests_development_app_is_the_app_to_the_real_peer_check` compiles the stand-in and has it connect to a test socket. The server reads the caller with the real `LOCAL_PEERPID` and `proc_pidpath`, then judges it with only the ancestry faked marker-free. The stand-in is recognised as the app, with its path's spaces kept, and a Python client on the same socket gets "this needs the Subfleet app". RESULT_DEVAPP

`test_the_end_to_end_person_is_never_skipped_for_the_wrong_reason` checks the three outcomes of `as_person` when no stand-in is named: it fails, fails with the compile error, or skips only for no compiler. RESULT_NOSKIP

### Runs

TEST_RUNS

## Not changed, and what is left

- **The app's image is the only positive test.** An agent could start the app's executable itself, and that process is the app, so it passes. The app reads no command-line arguments, URLs or Apple events (searched above), so it acts only on what is clicked in its window. Whether code can be injected into the app's process depends on how the build is signed, which this change does not touch and I did not check.
- **The vetoes are as they were.** They read a `ps` snapshot, and stop at pid 1, at a repeated or missing pid, or after 64 processes. `SUBFLEET_ROOT=<root>` matches as a substring, so a daemon on `/Users/m/.subfleet` also vetoes `SUBFLEET_ROOT=/Users/m/.subfleet-dev`. That errs toward refusing.
- **The e2e person and terminal checks did not run here.** Inside a Subfleet lane every ancestor carries the lane's markers, so `as_person` and `as_terminal` skip by design. They run in CI or from a terminal outside Subfleet. This job did not push, so CI has not run them on this head.
- **Phone access (d1350)** would need a new positive authority, such as per-request signatures from a paired device. Nothing here adds one, and a terminal can no longer stand in for it.
