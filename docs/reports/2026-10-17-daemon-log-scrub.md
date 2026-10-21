# daemon.log is scrubbed where it is written (D-LOG1)

Base: `release/217` at `3fbc4b7c5`, after #225 merged. The assigned branch, `fix/daemon-log-scrub-217`, is checked out in the caller's worktree, so git would not check it out in this job's worktree. The commits are on this worktree's detached HEAD, and Subfleet keeps them under `refs/subfleet-salvage/`. Nothing is pushed.

## The defect

Reviews r1 and r2 of #225 found that only the request responder's path scrubbed what it logged (`refusals.log_detail` → `detail_line` → `scrubbed_line`). Everything else could reach daemon.log raw:

- **The handler.** The daemon's handler was a plain `logging.StreamHandler`, so nothing scrubbed there.
- **Raw `exc` formatting.** Thirteen sites in `conversations/runner.py`, `conversations/service.py`, `conversations/wakes.py` and `daemon.py` formatted a caught exception into a log line.
- **Uncaught exceptions.** The daemon's stdout and stderr are daemon.log, and nothing installed `sys.excepthook`, `threading.excepthook` or `sys.unraisablehook`. Python's default hooks wrote tracebacks there raw.
- **The reviewer's input.** `TurnRunner._failed(RuntimeError("app-server said: password='SECRET7runner9\n\""))` wrote the secret. The regression test below runs it.
- **C-26.10.** `_suggestion_errors` swapped the `ConversationError` for a `SuggestionError` that carries only the template, so a refused `suggestion.start` dropped its own words. C-26.10's "go to daemon.log only, scrubbed" was false for it. There was no secrecy impact.

I found three more raw writers while reading the code:

- **logging's error report.** When a handler cannot write a record, logging's `Handler.handleError` prints the record's raw message and arguments to `sys.stderr`, which is daemon.log.
- **Records no handler takes.** `folders.identity_fallback` logs through `logging.getLogger("subfleet.folders")`, which has no handler, so `logging.lastResort` wrote the path and OS error to stderr raw.
- **Warnings.** `warnings.showwarning` writes a warning's message and its source line to stderr.

## The hub's decisions, as built

### 1. Scrub at the handler: a `logging.Formatter` subclass

`subfleet/daemon_log.py` has `ScrubbingFormatter`, installed by `ScrubbingStreamHandler`. `Daemon.__init__` makes its handler from that class. I chose a formatter over a filter plus formatter for three reasons:

- A formatter sees the whole formatted record, including the traceback. It formats `exc_info` itself every time and never trusts an `exc_text` another formatter cached.
- It can return a fixed line in place of a record it cannot scrub, without mutating the caller's record. It formats a copy.
- One class holds the rule, so "remove the formatter" is one mutation with named failing tests.

What it writes:

- **A one-line message,** with the line its format builds around it, in C-25.5's line form: `refusals.scrubbed_line`, which scrubs the whole text, folds it, scrubs again and cuts at `LOG_LINE_MAX` (2,000) without splitting a marker.
- **A message of several lines, a traceback (`exc_info`) or a stack (`stack_info`)** in the block form, `scrubbed_block`. It is scrubbed whole with `scrub_secrets(text, strip_reminders=True, whole=True)` and keeps its lines. Messages of several lines get this form, not the line form, because the lock watch's holder and waiter stacks (C-3.6) are logged as messages; folding them into one 2,000-character line would destroy them.

**Why neither form can leave a secret the other would hide.** `scrubbed_block(t)` computes `clean = scrub(t)`, `folded = fold(clean)` and `again = scrub(folded)`. It writes `clean` when `again == folded`, and `again` otherwise. The line form's text before its cut is exactly `scrub(fold(scrub(t)))`, which is `again`. So:

- When the block is `clean`, `fold(block) = folded = again`.
- When it is `again`, it is already the line form.

Either way, folded, the block is exactly the uncut line form. A value that holds no whitespace is in a text exactly when it is in that text folded, so it is in the block exactly when it is in the uncut line form. The cut only drops text. The property test checks `fold(scrubbed_block(t)) == fold(refusals.scrubbed_line(t, ∞))` for arbitrary text and for credential-shaped text.

The fallback is real, not only defensive. A seeded fuzz of 20,000 texts found 246 where folding lets a header rule take more, for example `\n  Cookie\n:\t"SECRET7…"\n\ntail`. Folded, `Cookie : [REDACTED]` takes `tail` too, so the block form writes that one line.

**A scrubber that fails.** `format` never raises. If scrubbing raises, or the record's message cannot be rendered (`log.info("%s %s", one)`), it writes one fixed line:

`[scrub failed] ERROR record from subfleet.daemon.4371 (RuntimeError) left out: RecursionError`

The line names the record's level, its logger and the class of any exception it carries, then what failed. The level and logger name are checked against `[A-Za-z0-9_.:-]{1,128}`, and the classes go through `refusals.class_name`. The handler's `handleError` writes `[log write failed] …` in the same shape, in place of logging's report with the raw arguments.

### 2. Uncaught exceptions: process hooks

`install_process_hooks()` installs five hooks. Each writes through a `ScrubbingFormatter` to the process's stderr as it is at that moment:

- **`sys.excepthook`** writes `uncaught exception`, then the scrubbed traceback.
- **`threading.excepthook`** writes `Exception in thread <name>`, then the traceback. It says nothing of `SystemExit`, as threading's own hook doesn't.
- **`sys.unraisablehook`** writes Python's `err_msg` and the object's repr, then the traceback.
- **`warnings.showwarning`** writes the formatted warning in the block form. A warning written to a caller's own file goes to the previous hook.
- **`logging.lastResort`** is a scrubbing stderr handler at WARNING.

It returns `restore()`, which puts back each hook only if it is still ours.

**Where it runs.** `daemon.main` installs the hooks after parsing its arguments and before anything else. `daemon start` launches `_daemond_argv(root)`, which is the `subfleetd` console script (`subfleet.daemon:main`) or `-m subfleet.daemon`. The launchd plist runs the same argv, with `StandardOutPath` and `StandardErrorPath` both daemon.log. So both paths run `main`.

**Restore on close.** `Daemon.close()` does restore process state: it hands SIGUSR1 back, because `Daemon.__init__` took it. The hooks are different. `main` installs them, not `Daemon`, so an in-process `Daemon` (the tests, an embedder) installs none and `close()` has nothing to put back. `main` keeps them for the life of the process, because an exception that ends `main` and an unraisable exception during interpreter shutdown are both written after `main` returns. Restoring them at the end of `main` would reopen exactly that window. Hooks installed before `main` parses its arguments would stay in a test process that calls `main([])` in-process (`test_live_store_guard`). They would replace pytest's thread and unraisable collectors for the rest of the session, so the hooks go in after the arguments.

**faulthandler stays.** CPython's documentation says that faulthandler shows "only the filename, the function name and the line number … (no source code)". `Python/traceback.c`'s `dump_frame` writes `  File "<co_filename>", line <n> in <co_name>` for each frame and nothing else. I read both on 2026-10-18; this sandbox has no local CPython docs or source, so I fetched docs.python.org and the 3.14 branch's `Python/traceback.c` on GitHub. `test_faulthandler_writes_frames_only` runs `faulthandler.dump_traceback` into a real daemon's log while an exception carrying a secret is being handled and a local holds it. The log shows the frame and no secret, no exception class and no source line. **Corrected in round 2:** on Python 3.14 an all-threads dump (`daemon stacks`, SIGUSR1) also writes each thread's name (`Thread 0x… [name]`), for a thread Python started; review r1 of #233 found it. Every daemon thread's name is fixed words or ids (round 2, §5).

### 3. The call sites

One commit, `3dc31b6a9`. Each site logs `refusals.scrubbed_words(exc)` (`<class>: <scrubbed text>`) in place of the raw exception.

| File | Sites the reviews listed | Others the site lint found |
|---|---|---|
| `conversations/runner.py` | a runner's failure (`_failed`); an approval the driver refused | a steer's failure; a model catalog not recorded; a refused frame |
| `conversations/service.py` | a handoff fence lift; a catalog run not started; a turn's start snapshot | |
| `conversations/wakes.py` | the PR wake worker; a wake request refused; the batched PR poll; the final wake replay | |
| `daemon.py` | submit's two workdir inspections; a retention error; a pin left as it is | the wait hub; the turn-hold note; a route refusal at admission; two SIGUSR1 warnings |
| `provider_env.py` | | an unusable CA bundle |
| `folders.py` | | the identity fallback (through logging's last resort); `4a5486138` moves its `refusals` import to the top, since `3dc31b6a9`'s reason for a lazy one was wrong |

Two of these sites cut text, and the cut now comes after the scrubbing:

- **The retention error** is scrubbed before its 300-character cut (`refusals.scrubbed_line(text, 300)`). The old `[:300]` could cut a secret into a fragment the scrubber no longer recognizes.
- **The turn-hold note** is scrubbed and then cut the same way.

`test_no_daemon_module_logs_a_caught_exception_raw` runs an AST lint over `subfleet/`. It flags every log call whose argument holds a caught exception other than through a scrubber, its class, or its errno or exit status. A caught exception is an `except … as` name, a parameter named `exc`, or a parameter typed as an exception. At base it finds 20 such sites; at head it finds none. It doesn't follow an exception into another variable: the retention error dict and the route refusal's `exc.cause` were found by reading the code.

### 4. `suggestion.start` (C-26.10)

`_suggestion_errors` now calls `refusals.log_detail(self.log, op, exc)` before it swaps the error. It skips the call only when the words and repair are already the template the wire shows (`_template_only`). The op name comes from the handler's name (`op_suggestion_start` → `suggestion.start`). The wire reply is unchanged: exactly the template.

The responder's suggestion branch for a `ProtocolError` or `AdapterError` now logs its detail the same way. Every other op's branch already did.

### 5. Contract and the round-2 report

- **C-25.7:** its daemon.log sentence opens as the hub asked: "The details of the request responders', a job's and a turn submit's refusals reach it through `refusals.detail_line`". It then says:
  - every line and traceback the daemon logs is scrubbed at the handler, in the line or block form;
  - a record that cannot be scrubbed becomes the fixed marker;
  - `daemon.main` installs the five hooks, named;
  - the other writers are named.
  `subfleet/daemon_log.py` joins the clause's module list.
- **Change log:** a bullet at the top of "Changes in version 3".
- **`docs/reports/2026-10-15-suggestions-p3.md`, round 2's "Tests run":** it now records the hub's host run at `89b9653ac` (1,273 passed: 212, 748 and 313) and CI's 10 of 10 at `b2dc9946b`. Both figures are as the hub's brief gives them; I could not check them, since this job has no network remotes.

## Invariants

Stated for every input. Each is executed by a test named in "Tests per file".

1. **No secret survives.** For any message and exception text built from `SECRET7` credential shapes, nothing the handler writes contains a `SECRET7` value. The shapes are the ones `test_daemon_log_scrubbing.py` builds: mixed-case, quoted, repr-quoted and double-encoded keys, `=`, `: ` and ` = ` separators, and ten value styles. This rests on the scrubber's rules; see "Outside this change".
2. **Each form is exact.** For any record, the output is the message's line form (one line) or block form (several lines), then each traceback and stack in the block form. A one-line record's output equals `refusals.scrubbed_line(message)` exactly; that is the differential.
3. **The two forms agree.** For any text `t`, `fold(scrubbed_block(t)) == fold(refusals.scrubbed_line(t, ∞))`. The bounded line is a prefix of the uncut one, apart from its `…` mark.
4. **Formatting is total.** `format` never raises (bar `BaseException`s such as `KeyboardInterrupt`). A record is always written, either scrubbed or as the fixed marker, and the caller's record is left as it was.
5. **Restoring is safe.** `install_process_hooks()` followed by `restore()` leaves all five hooks as they were. `restore()` leaves alone any hook someone else installed since.

## Tests per file

All runs used Python 3.14.7 with the GIL, in a fresh `.venv` (`uv sync --locked --group dev`), with no xdist. Each pytest ran in its own hold of the shared lock, in the foreground, under a 1,140 s in-process alarm. Hypothesis ran derandomized and bounded at 40 examples per property, with no database.

### New files

- **`tests/unit/test_daemon_log_handler.py` (24 cases):**
  - the differential (Hypothesis) and three one-line examples;
  - the property (Hypothesis) over messages and exception texts;
  - the fold invariant (Hypothesis) over arbitrary text;
  - two inputs that fold further;
  - a lock-watch-shaped message of several lines;
  - a chained traceback;
  - a stack;
  - a cached `exc_text`;
  - the scrubber raising;
  - a message that cannot be rendered;
  - a failed write;
  - the marker's checked names;
  - each of the five hooks, a thread's `SystemExit`, and `restore()`;
  - a source check that `Daemon.__init__` and `main` use the module.
- **`tests/unit/test_daemon_log_sites.py` (24 cases):**
  - the reviewer's `TurnRunner._failed` input;
  - a runner loop that raised (through `_run`);
  - an approval the driver refused;
  - a handoff fence lift (service);
  - the PR wake worker (wakes);
  - a pin left as it is (daemon).
  Each of those goes through the handler a real `Daemon` makes. Then:
  - a careless raw site, which only the handler covers;
  - faulthandler's frames only;
  - `suggestion.start` with an EACCES doubt and a secret-shaped `HOME`;
  - real `subfleetd` processes: `subfleet daemon start` with a policy whose error names a secret; the plist's own `ProgramArguments`, folder, environment and output paths, as launchd runs them; and a `subfleetd` that raises in a thread, a finalizer, a warning and its main thread;
  - the site lint, and 11 cases of the lint telling raw from scrubbed.

### Runs at head

| Hold | Files | Result |
|---|---|---|
| 1 (brief) | `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 315, `test_daemon_log_handler` 24, `test_daemon_log_sites` 24 | 508 passed, 18 s |
| 2 (brief) | `test_conversation_service` 98, `test_suggestions` 29, `test_suggestions_r1b_adversarial` 30, `test_suggestions_r3_channels` 14, `test_suggestions_review_boundaries` 7, `test_suggestions_review_multiline` 6, `test_suggestions_review_response` 3, `test_turn_runner` 76, `test_conversation_wakes` 21, `test_review_pr127_pr_wakes` 7 | 291 passed, 104 s |
| 2b (added) | `test_suggest_cli` 7, `test_suggest_hint` 10, `test_suggestion_error_boundaries` 22, `test_suggestion_properties` 1, `test_suggestion_secret_properties` 7, `test_suggestion_structural_errors` 40, `test_protocol_secrecy` 322, `tests/fake/test_record_secrecy` 44, `test_codex_ca_bundle` 46, `tests/fake/test_codex_ca_bundle_fake` 6, `test_descriptor_review_fixes` 56, `test_daemon_init_release` 2, `tests/fake/test_daemon_stacks` 6, `test_lockwatch` 20, `test_live_store_guard` 324 | 913 passed, 395 s |
| 3 (brief) | `test_daemon_log_handler` 24, `test_daemon_log_scrubbing` 86, `test_daemon_log_sites` 24, `test_daemon_verbs` 20 | 154 passed, 13 s |
| 4 (brief) | `tests/fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 (added: the contract changed) | 63 passed, 5 s |
| collect | the whole suite, `--collect-only` | 15,037 collected, no errors |
| 5 (added) | the fake-daemon tests that read daemon.log: `test_admission_route_isolation` 93, `test_admission_visibility` 43, `test_daemon_descriptors` 13, `test_daemon_launch` 9, `test_descriptor_exhaustion` 2, `test_gate_end_to_end` 11, `test_notice_header` 23, `test_provider_qos` 2, `test_reset_credit_incident` 19, `test_salvage_adversarial` 8, `test_salvage_finalization` 47, `test_turn_trees` 12, `test_workspace_transient` 24, `test_workspace_enospc` 1 (all `tests/fake`), and `tests/unit/test_daemon_accept` 5 | 312 passed, 156 s |
| 6 (added, after `4a5486138`) | `test_folders` 23, `test_filesystem_identity_agreement` 40, `tests/fake/test_canonical_identity` 24 and its `_fix2` to `_fix5` (21, 18, 11, 14), `test_daemon_log_sites` 24, `test_daemon_log_handler` 24 | 198 passed, 1 xfailed, 89 s |
| 3.12 (added) | Python 3.12.14 in a scratch venv: `test_daemon_log_handler` 24, `test_daemon_log_sites` 24, `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 315, `test_daemon_verbs` 20 | 528 passed, 40 s |

Holds 1 to 5 and the 3.12 run ran before `4a5486138`, which only moves `folders.py`'s `refusals` import to the top of the module; hold 6 ran after it.

Notes on the brief's globs:

- Group 2's `test_conversation_runner*.py` and `test_wakes*.py` match no file. I ran `test_turn_runner.py`, `test_conversation_wakes.py` and `test_review_pr127_pr_wakes.py` in their place.
- Group 3's `tests/fake/test_daemon_restart*.py` matches no file.

### Runs at base

These ran at `efecc03da`: the tests commit, with `subfleet/` exactly as `3fbc4b7c5` has it.

- **`test_daemon_log_sites.py`:** 11 failed and 12 passed. The failures:
  - all six site regressions;
  - `suggestion.start` (no detail line in daemon.log);
  - `daemon start`, launchd and the four-channel process (each secret in daemon.log raw);
  - the site lint, which found 20 raw sites.

  The 12 that passed are faulthandler's frames, which is a confirmation and not a regression, and the lint's 11 self-cases. The careless-site case was added after this run (`d8038b05a`). It failed under the formatter mutation below.
- **`test_daemon_log_handler.py`:** the file errors at collection, because `subfleet.daemon_log` does not exist at base.

### Mutations

| Mutation | Result |
|---|---|
| M1: remove `setFormatter` from `ScrubbingStreamHandler` | 11 of `test_daemon_log_handler.py` fail. Among them are `test_a_one_line_record_is_exactly_its_line_form` (the differential), `test_nothing_the_handler_writes_keeps_a_secret_and_each_form_is_exact` (the property) and `test_a_scrubber_that_raises_writes_the_fixed_marker`. The six site regressions still pass, because each site now scrubbed its own exception; so I added `test_a_secret_a_careless_site_logs_raw_is_scrubbed_by_the_daemons_handler`, which fails under M1 (next row). |
| M1 and M2 together: also remove all five hook installs | 9 fail: the careless-site case (M1), and the five hook cases (thread, main thread, unraisable, warning, last resort) plus the three real-process cases (`daemon start`, launchd, the four-channel `subfleetd`), from M2. Each hook case triggers exactly one hook and goes through `_StderrHandler`, whose own formatter M1 leaves alone, so each failure belongs to its own hook. The five removals ran together, not one per hold. |
| M3: revert `runner.py`'s `_failed` site and drop the `_suggestion_errors` log | `test_the_reviewers_turn_runner_failure_leaves_no_secret` and `test_a_turn_runner_whose_loop_raises_leaves_no_secret` still pass, because the handler covers the reverted site, as the hub expected. `test_no_daemon_module_logs_a_caught_exception_raw` fails, and so does `test_a_refused_suggestion_start_logs_its_own_words_scrubbed`. |

### Not run

- the full suite (it collects: 15,037 tests, no errors);
- Python 3.12 beyond the files in the 3.12 row;
- `tests/e2e`, `tests/frontend` and `tests/process`;
- launchd itself. The launchd case runs the plist's own program, folder, environment and output paths, not `launchctl`.

## Outside this change

- **The `Bearer` and header rule order** (#225 review r2's follow-up, P2). `token_type: Bearer\naccess_token: SECRET7…` and `Cookie: x_auth-token :\n SECRET7…` still leak, in the line form and the block form alike. The forms agree, but the scrubber misses the value. The property's filler text cannot spell a header, so that known class doesn't decide it.
- **Over-masking in tracebacks.** In the block form a traceback is scrubbed whole, so a source line that opens a quoted credential value (`raise E("password='" + …`) is masked up to the matching quote, lines between included (C-23.14's whole-text rule). Some of the traceback can be lost, but never a value. The tests keep secret shapes out of `raise` lines for this reason.
- **Cost** (bounded in round 2, §4). Each record is now scrubbed twice in the line form, under the handler's lock. In one probe, 1,000 short lines took about 355 ms, so about 0.36 ms a line. Scrubbing is linear in the text, and a record's text is not bounded before it is scrubbed, as `refusals.scrubbed_line` already did for refusal details. Review r2 of #225 measured about 1.7 s for its worst shape at 1.3 million characters in daemon.log mode, so a record of several megabytes would hold the handler's lock for seconds.
- **A record channel noticed, not changed** (fixed in round 2, §1). I read this but did not execute it. `runner.py`'s steer failure puts `str(exc)` into `_miss_steer`'s reason, which becomes the message's `state_reason` (`steer-missed: input-unavailable: …`) and a `steer.missed` event. C-25.7 limits `state_reason` to classes, errnos and our words. Whether `store.set_state` scrubs that reason is unverified.
- **Writers before the hooks.** argparse's errors, and `home_path`'s when `--state-root` is absent, are written before `main` installs the hooks. `daemon start` and the plist always pass a fixed `--state-root`.

## Commits

- `efecc03da` test: daemon.log is scrubbed at its handler, its hooks and its call sites (D-LOG1)
- `71ee6adf7` fix: scrub every daemon.log record at the handler; scrubbing process hooks (D-LOG1)
- `3dc31b6a9` fix: each daemon.log site logs a caught exception through scrubbed_words (D-LOG1)
- `40aa58dce` fix: a refused suggestion op's own words reach daemon.log, scrubbed (C-26.10)
- `d8038b05a` test: a real Daemon's handler scrubs a secret a site passes raw (D-LOG1)
- `727b516f0` docs: C-25.7 says every daemon.log line is scrubbed at the handler and names the hooks
- `4a5486138` fix: folders imports refusals at the top; the lazy import's reason was wrong
- this report

## Round 2: review r1 of #233

Review r1 approved #233 with one P2 and four P3s. This round fixes all five and what the P2's sweep found. Base: this job has no network, so nothing was fetched; the last fetched `origin/release/217` is `3fbc4b7c5`, which the branch already holds, so there was nothing to merge.

### 1. A steer's reason (P2) and the sweep

`TurnRunner._steer` sent a steer whose input or driver failed back to the queue as `input-unavailable: <the exception's words>`. Those words went, unscrubbed and unbounded, to the message's `state_reason`, its `changes` row, the driver's and the runner's `steer.missed` events, the turn's steer facts and the receipt `_receipt` gives clients. The reason is now `input-unavailable: ` and `refusals.fixed_error(exc)`: the class and, for an OS error, its errno and `os.strerror` text. The daemon.log line keeps `scrubbed_words`, as before.

`test_a_failed_steer_s_reason_names_its_class_and_errno_never_its_words` runs the reviewer's input (`RuntimeError("driver said: token=SECRET7steer9 " + "x"*3000)`), an `OSError` naming a secret-shaped path and a `ValueError`, for Claude and Codex. It checks every one of those records and that none of the store's rows holds the secret or the `x` run. All six cases failed at base.

**The sweep.** An AST sweep of `subfleet/` for a caught exception rendered (an f-string, `str`, `repr`, `%`, `.format`, `.args`, `.strerror`) outside a log call or a scrubber finds 260 sites. Most are the CLI's own output to its caller or an exception raised with a template. I read each one in `conversations/` and `daemon.py` whose text lands in a stored or client-visible field.

Fixed, each with a regression that failed at base:

- **`wakes.from_final`'s `wake-refused` event** (its `detail`, which the app's timeline shows) said `Wake request refused: {exc}`, including `run <what the agent wrote> is not this conversation's`. It now shows the wake module's own words only for a `bad-wake` or `wake-id-conflict` refusal (`TIMELINE_REFUSALS`), and the class otherwise. A lint test checks that every such refusal in `wakes.py` is raised with a literal message; the run refusal no longer names the id. Tests: `test_a_refused_wake_request_s_timeline_detail_holds_our_words_only` (4 cases; the 2 with caller or exception words failed at base) and `test_every_wake_refusal_the_timeline_may_show_is_fixed_words`.
- **`Daemon._finish_push_workspace`'s `job.workspace_failed` notice** held sqlite's words (`{type(exc).__name__}: {exc}`). It names the class, as a job's records must. Test: `test_a_completion_write_failure_s_notice_names_its_class_only`.

Read, not changed:

- `runner.py`'s two `_unacknowledged(f"…: {exc}")` calls reach only daemon.log, through `_relay_lost`'s warning, which the handler scrubs. The stop reason is the fixed `relay-failed`.
- `store.py`'s title error is logged at debug level only.
- `service.py`'s `turn.diff` answers `_unavailable(exc.reason, str(exc))`. A `diff.Unavailable` message is our words with a tree id.
- `legacy.py`'s import item `detail=str(exc)` holds `insert_legacy_history`'s refusals: our words and a state name.
- `daemon.py`'s route refusal (`job.route_refused`, C-6.12) names the job's own pin. `procs.InspectionError`'s words are ours, as the site's comment says. A host push error is `PushError`'s fixed words with the branch the store holds.
- A Codex steer the provider refused keeps the provider's error message, scrubbed and cut to 300 characters by `redact.bounded_text`. That is provider text, not exception text.
- An adapter's attestation evidence (`classify.codex_attest`, `ClaudeAdapter._assistant_rows`) holds an OS error's text with the transcript's path, in the attempt's `finalization.json`. It is not a `state_reason`, an event or a job's detail. It is a candidate for the same treatment; I left it, since it is outside this sweep's fields.

### 2. The hooks in a test process (P3)

An autouse fixture in `tests/conftest.py`, `process_hooks_restored`, saves `sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`, `warnings.showwarning` and `logging.lastResort` before every test and puts them back after it. So `test_daemon_accept` and `test_stop_watchdog`, which run `main` in-process, no longer leave the daemon's hooks in place of pytest's collectors, and neither will a future test.

`test_a_test_that_runs_main_leaves_pytest_its_own_hooks` runs the reviewer's sequence in its own pytest, with the suite's conftest as a plugin (`-p tests.conftest`) and both warnings made errors: a control thread failure, `install_process_hooks()`, a thread failure and a failing finalizer. It reads the JUnit report. At head, all three failures are reported. At base, only the control was.

### 3. Exception groups (P3)

Python writes a margin (`    | `) before each line of a group's members. When a value is on the line after its key, the scrubber takes the `|` for the value. `scrubbed_block` now detects a group's traceback by its member separator (`+---------------- 1 ----------------`). It scrubs the text a second time with the margins taken off, as the plain traceback reads, and keeps, line by line, only the runs both readings show. A margin-free line's run is written only if the same line of the margined block holds it, else `[REDACTED]`, and the line keeps its margin. When scrubbing joined lines (a value spanning lines, `Bearer` and its token), the readings no longer pair line by line. Then the margin-free text is written whole, without margins, by the same rule.

A fuzz of 4,500 groups found something the review's framing missed. With several members, a group's traceback is one text, scrubbed whole, so a quote in one member can close a value another member opened, as in any chained traceback. The group can then show a secret that one member's plain traceback hides, but the same text without margins shows it too. That is the whole-text scrubber's behaviour, not the margin's. So the regressions are:

**Correction (round 3).** The paragraph above is wrong. Review r2 reran the original per-member property on 4,500 random groups: all 29 failures were duplicate secrets. The suffix strategy shrinks two members' secrets to the same string, so another member showed that exact string bare. With distinct secrets there were no failures in 22,000 groups, and no input shows a quote in one member closing a value another opened. Round 3 restores the per-member property for several members with distinct secrets (`test_a_several_member_group_never_shows_a_secret_its_member_alone_hides`, round 3 §5). The third bullet below is superseded.

- `test_an_exception_group_hides_what_its_member_alone_hides`: the reviewer's four shapes, one or two deep, raised or only constructed. All 16 cases failed at base.
- `test_a_one_member_group_never_shows_a_secret_its_member_alone_hides`: a property, with the four shapes and the credential shapes. It failed at base.
- `test_a_group_s_margins_never_cost_a_secret`: a property over several members. The block never shows a secret that the margin-free text hides, or one its line form hides. It failed at base.
- `test_a_group_s_block_never_shows_a_value_its_line_form_hides`: arbitrary member texts. It passes at base too, since that is the forms' invariant.

### 4. The size bound (P3)

**The bound.** `daemon_log.RECORD_SCRUB_MAX` is 16,384 characters: a record's line, traceback and stack together, each cut to its share before anything is scrubbed. A part shorter than an even share leaves the rest to the others. 16,384 keeps a lock watch's report whole (two stacks of 40 and 12 frames, about 8,000 characters).

**The cut** is `handoff.cut_whole(text, limit)`, beside `scrub_bounded`. It keeps whole lines of the head and the tail, each at most `limit // 2`, joined by `… [N characters omitted] …`. A cut is at a line break that `scrub_bounded`'s `_neutral_break` allows. That rule alone is not enough here, for two reasons:

- `scrub_bounded` resolves the multi-line blocks (reminders, private keys, data URIs) over the whole text first, then scrubs its excerpts within lines. The daemon's forms scrub whole, and a whole-text scrub masks a quoted value across lines, to the next quote of its kind.
- Resolving reminders first would change the rule order: whole-text mode masks quoted values before it strips reminders, and a reminder can hold the closing quote.

So a cut is also:

- **Outside any quoted value.** For each pairing kind (`_quoted_ends`' quote after an even backslash run, and `_escaped_quote_ends`' quote after a run of a given length), the last quote before the cut must not open a value when a quote of that kind follows it. Read loosely, any quote after `:` or `=` may open one. In the tail, what precedes the window is read back at most 64 quotes, else it is unknown, and unknown means no cut.
- **Outside private keys.** The head never starts one and the tail never ends one.
- **Outside a data URI's body.** In the head, a body is the run after a `data:…;base64,` prefix. In the tail, the first run usually began before the window: it is a body only when the last `;base64,` before it is followed by body characters alone. My first version counted such a run as a body whenever it reached back past 2 KiB, and since letters, digits, spaces and line breaks are all body characters, a text of plain words then kept no tail. A probe of the rules found that, and `test_a_long_run_of_plain_words_still_keeps_its_tail` covers it.

Two more cases keep only the marker:

- **A text holding a reminder tag** is not cut, since stripping one joins its two sides.
- **A text that opens with a header**, when the head would keep none of it, is not cut either. Folded, as the line form folds it, the whole text is that header's value. The fuzz found this case: 3 counterexamples in 16,000 texts before the rule.

A part with no such break in reach, such as one long line, keeps only the marker.

**The lock.** With the scrub bounded, Python's own `formatException` still ran under the handler's lock. For 4 MiB of three-character lines that took about 130 ms. The reviewer also recommended scrubbing outside the lock. `ScrubbingStreamHandler.handle` now filters and formats a record (Python's formatting and the scrub) first, then takes the lock only to write it. One behaviour change: two threads' records are written in the order their formatting ends, each whole. daemon.log lines carry no timestamps.

**Measured** on Python 3.14.7 at load 16 to 25, at head, median (and maximum) of 5 to 7 runs:

| Record | At base (review r1) | At head |
|---|---|---|
| 4 MiB of frame lines, message, through the handler | 4.4 to 6.0 s | 22 ms (30) |
| 4 MiB of frame lines, message, traceback and stack, through the handler | — | 45 ms (55) |
| 7 MiB of dense credential lines, three parts, through the handler (the worst shape) | — | 57 ms (64) |
| Three 4 MiB parts, the formatter alone, on four shapes (three runs) | — | 25 to 37 ms (54) |
| 4 MiB of `'"` lines, three parts, through the handler | — | 108 ms (127), mostly Python's `formatException` of 1.4 million lines, in the record's own thread; the lock is held only to write |
| A short line logged behind a 4 MiB record | 4.55 to 5.08 s | 0.1 ms (0.2) |

`cut_whole` itself takes 1.8 ms for a 4 MiB part of frame or credential lines, 17.7 ms for 4 MiB of `'"` lines. So the scrub of the worst shapes measured stays under 60 ms, and nothing a record does holds the lock but its write. (Review r1 measured 1 MiB at 1,075 to 1,509 ms.)

**Regressions:**

- `test_a_four_mib_record_is_written_bounded_and_scrubbed_within_the_time_bound`: message, traceback, or both. The secrets at both ends and in the middle are scrubbed, the marker is written, the scrubber never sees more than the bound, and the best of three runs is under 100 ms.
- `test_a_short_line_waits_for_a_four_mib_record_no_longer_than_the_bound`.
- `test_a_four_mib_line_with_nowhere_safe_to_cut_keeps_only_its_marker`.
- `test_a_record_s_cut_never_shows_a_secret_its_whole_text_hides`: a differential property with 200 examples over lines that span a cut, in both forms, with the length bound.
- `test_a_cut_never_falls_inside_a_match_that_spans_lines`: a quoted value, an escaped one, a private key and a data URI, each at the head and at the tail.
- `test_a_record_over_the_bound_holding_a_reminder_keeps_only_its_marker`.
- `test_a_record_is_scrubbed_before_the_handler_s_lock_is_taken`.
- `test_a_long_run_of_plain_words_still_keeps_its_tail` (1,000, 40,000 and 4 MiB of words).

All of these failed at base. The short line waited 4.55 to 5.08 s there, and the lock case found the lock held.

**Each rule is needed.** Outside pytest, I switched off one rule of `cut_whole` at a time and ran the same differential fuzz, 7,500 texts per mutant:

| Rule switched off | Counterexamples |
|---|---|
| Quoted values | 165 |
| Data URI bodies | 99 |
| Reminder tags | 74 |
| Leading header | 8 |
| Private keys | 0 in the fuzz, whose key blocks are short. Without the rule, `test_a_cut_never_falls_inside_a_match_that_spans_lines`' private-key case writes `SECRET7pem9`. |

With every rule on, the final code gave none in 16,000 fuzzed texts, and the versions of this round's code since the header rule gave none in 92,000.

### 5. faulthandler's thread names (P3)

C-25.7's sentence, `daemon_log.py`'s docstring and §2 above now say that on Python 3.14 an all-threads dump also names each thread Python started. `test_faulthandler_writes_frames_only` now also dumps all threads with a named one waiting, and finds `[subfleet-probe]` on 3.14.

`test_every_daemon_thread_s_name_is_fixed_or_ids_only` is an AST lint. Every `Thread`, `Timer` and `ThreadPoolExecutor` in `subfleet/` is named by a literal of `[a-z][a-z0-9-]*`, by Python's default, or by the turn runner's `turn:{self.attempt_id}`, and no thread is renamed later. The lint also checks that a turn's attempt id is `<stamp>-turn-<hex and dashes>/a<n>`, since `ids.job_id` cuts the slug at 40 characters, and that the service names a turn's job `turn-<conversation id>`.

Both tests pass at base. Nothing leaked; they guard the corrected statement. Two errors in my first version of them were found by running them at base and corrected in `cd97034ad`: 3.14 names threads only in an all-threads dump, and the uuid can be cut.

### Invariants, round 2

Each holds for every input and is executed by the tests named above.

6. **A steer's reason is fixed.** For any exception, the reason is exactly `input-unavailable: ` and `refusals.fixed_error(exc)`, in every record a client reads.
7. **The cut hides what the whole text hides** (restated in round 3). For any text and limit, the secrets the block form shows of `cut_whole(text, limit)` are a subset of those it shows of `text`, and for a text without a group's margins so are the line form's. That holds folded or not, and for a group's text also without its margins. The cut text is at most `limit` characters plus its marker line; a text holding a reminder tag is the marker line alone, and so is a side whose window holds more than eight escaped quote kinds. Round 2's statement broke on a credential flag whose value began the kept tail (review r2).
8. **A group's margins cost nothing** (restated in round 3). A group's block shows no value that the same text without margins hides, nor one its line form hides. A member's secret that its plain traceback hides, its group's hides too: for one member, and for several members with distinct secrets. That holds when the record is cut, since whether a part is a group's is read before the cut. Round 2's statement broke on a group longer than the bound (review r2).
9. **The lock covers only the write.** No scrub, and no formatting, runs while the handler's lock is held.

Invariant 3 is restated accordingly. For a text without a group's margins, `fold(scrubbed_block(t)) == fold(refusals.scrubbed_line(t, ∞))`, as before. A group's block may hide more, never less.

### Tests run, round 2

All runs used a fresh `.venv` (`uv sync --locked --group dev`, Python 3.14.7 with the GIL), plus a scratch Python 3.12.14 venv for the 3.12 rows. Each pytest ran in its own hold of the shared lock, in the foreground, under a 1,140 s in-process alarm, with no xdist. I ran the venv's `python -m pytest`, the same environment `uv run` uses, so that a hold could not end waiting on uv's cache lock. Hypothesis ran derandomized and bounded, with no database; no `.hypothesis` folder was left.

**At base.** These used a `git archive` of `a7aff971e`'s `subfleet/` with this round's tests:

| Hold | What | Result |
|---|---|---|
| base 1 | the 16 new cases at `e26a7f807` | 45 failed, 3 passed. The passes were the forms' line-form invariant and the two wake inputs whose words were already fixed. |
| base 2 | the rewritten group properties, the thread-name lint, faulthandler | 2 failed (both group properties), 3 passed (the invariant, and the two guards that pass at base by design) |
| base 3 | the lock case | failed: the lock was held |

**At head:**

| Hold | Files | Result |
|---|---|---|
| 1 (brief group 1) | `test_daemon_log_handler` 59, `test_daemon_log_sites` 25, `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 315 | 544 passed, 20 s |
| 2 (brief group 2) | `test_conversation_service` 98, `test_turn_runner` 82, `test_conversation_wakes` 26, the six `test_suggestions*.py` 89 | 295 passed, 61 s |
| 3 (brief group 3, and the sweep's files) | `test_daemon_accept` 5, `test_stop_watchdog` 26, `test_daemon_verbs` 20, `test_host_push_storage_retry_budget` 61, `test_host_push_store_completion_pr183` 2, `test_review_pr127_requests` 17, `test_review_pr127_pr_wakes` 7, `test_review_r6_probes` 30, `test_continuation_r2` 11 | 179 passed, 98 s |
| 4 (brief group 4, and the contract's index) | `tests/fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 | 63 passed, 3 s |
| 3.12 | Python 3.12.14: group 1, `test_turn_runner`, `test_conversation_wakes`, `test_host_push_storage_retry_budget` | 713 passed |
| collect | the whole suite, `--collect-only` | 15,086 collected, no errors |
| 5 (final code: all four groups and hold 3's extras, merged) | as holds 1 to 4 and 3; `test_daemon_log_handler` 60 | 1,082 passed, 203 s |
| 6 | `test_suggest_cli` 7, `test_suggest_hint` 10, `test_suggestion_error_boundaries` 22, `test_suggestion_properties` 1, `test_suggestion_secret_properties` 7, `test_suggestion_structural_errors` 40, `test_protocol_secrecy` 322, `tests/fake/test_record_secrecy` 44, `test_codex_ca_bundle` 46, `tests/fake/test_codex_ca_bundle_fake` 6, `test_descriptor_review_fixes` 56, `test_daemon_init_release` 2, `tests/fake/test_daemon_stacks` 6, `test_lockwatch` 20, `test_live_store_guard` 324, `test_daemon_connections` 30 | 943 passed, 245 s |
| 7 | the fake-daemon files that read daemon.log (round 1's hold 5: 14 files, 309 cases), `test_folders` 23, `test_filesystem_identity_agreement` 40 | 369 passed, 1 failed: see below |
| 8 | `tests/fake/test_gate_end_to_end` alone | 11 passed, 3 s |
| 3.12 | Python 3.12.14 at final code: group 1, `test_turn_runner`, `test_conversation_wakes`, `test_host_push_storage_retry_budget`, `test_daemon_accept`, `test_stop_watchdog` | 745 passed |
| 9 (after the plain-words fix) | group 1 (`test_daemon_log_handler` 63), `test_protocol_secrecy` 322, `tests/fake/test_record_secrecy` 44, `test_daemon_verbs` 20 | 934 passed, 36 s |
| 3.12 | Python 3.12.14, group 1 after the plain-words fix | 548 passed |

**Hold 7's failure.** `test_plan_peer_process_finalizes_through_real_daemon` timed out in the harness's 5 s wait for a daemon to answer, at load about 15. The daemon had started and written its first two lines through the new handler. Alone, in hold 8, the file passed 11 of 11 in 3 s. I read it as the load-sensitive harness wait, not this change.

**Holds 1 to 4** ran before `24a2ea2e1` (the cut's two cheaper scans), `b55ab8295` (formatting before the lock) and `6c2a67d96` (plain words). Hold 5, the 3.12 run at final code and hold 9 ran after them; hold 9 and the last 3.12 row after the plain-words fix.

**Not run:**

- the full suite (it collects 15,086 tests with no errors);
- `tests/e2e`, `tests/frontend` and `tests/process`;
- Python 3.12 beyond the files named above;
- a real `launchctl`.

I didn't query CI, since this job has no network.

### Commits, round 2

- `e26a7f807` test: review r1 of #233: a steer's reason, a group's margin, a record's size, the hooks, thread names
- `98f2ef0c1` fix: a steer's reason, a wake refusal's detail and a job's notice name our words or the class
- `b832faeb2` test: each test ends with the process hooks it started with
- `20e6963b0` fix: an exception group's traceback hides what its members' plain tracebacks hide
- `cd97034ad` test: faulthandler names a thread in an all-threads dump; a turn's job id may cut its uuid
- `03679e1e9` test: a group's property is per member alone, and its margins never cost a secret
- `870a78527` fix: the handler scrubs at most 16,384 characters of a record, cut where no match crosses
- `31d654541` docs: daemon_log says a record is cut first, a group's block may hide more, and faulthandler names threads
- `827df1c51` docs: C-25.7 states the record bound, the group block, the steer reason and faulthandler's thread names
- `24a2ea2e1` perf: cut_whole scans for reminder tags once and looks 2 KiB back for a data URI body
- `784be6d22` test: a record is scrubbed before the handler's lock is taken
- `b55ab8295` fix: the handler formats a record before it takes its lock; only the write holds it
- `e2b80d3e3` docs: C-25.7 says a record is formatted before the handler's lock, which only the write holds
- `89f692ad7` docs: the bound's measured cost is a median under 40 ms and at most 54 ms
- `330ee77b0` test: a long run of plain words before the tail still leaves a tail
- `6c2a67d96` fix: cut_whole reads a long run of plain words before the tail as a data URI body only after `;base64,`
- this report's round 2

Nothing is pushed.

## Round 3: review r2 of #233

Review r2 requested changes: one P2 and four P3s, plus two small costs of round 2's choices. This round fixes all of them, and two more things its own tests found.

**Base.** This job has no network. The last fetched `origin/release/217` is still `d72597b5c`, which the branch has held since `2b64f76a3`, so there was nothing to merge. (The local `release/217` ref, `b053e3de8`, is older and behind it.)

### 1. The cut's cost (P2)

Review r2 measured 3.6 to 5.5 s for one `cut_whole` on its head input and 4.5 to 4.8 s on its tail input. The causes were the ones it named:

- **Head.** `_whole_head_cut` ran `_event_from`, a regex scan of everything after the head, once for each escaped quote kind open at a candidate cut. The input had 122 such kinds.
- **Tail.** `_whole_tail_cut`'s `opened.setdefault(kind, _event_before(...))` evaluated `_event_before` at every line start, because Python evaluates the default first.

Writing the cost test found a third cause: **long runs of blank lines.** Each candidate line start read its neighbours anew. In the tail, `_last_visible` walked back over the whitespace in Python at every line start, and `_WHITESPACE_RUN_RE` scanned the run ahead of it. At base, a record of a key followed by blank lines did not finish within the test's budget of 3 million traced lines, and its 4 MiB form ran past a 30 s timeout. Blank lines after a credential flag also leaked at base, through the flag P3 below.

**The design.**

- **A real cache.** Each side computes each quote kind's event at most once. The head keeps `beyond[kind]`, whether a quote of the kind follows the window. The tail keeps `before[kind]`, whether the last quote of the kind before the window may open a value.
- **The cap.** `handoff.CUT_KINDS_MAX` is 8. When a side's window holds quotes of more than eight distinct escaped kinds (a quote with the length of the run of backslashes before it), that side keeps only the marker. So a side computes at most ten events: eight escaped kinds and the two plain ones. C-25.7 states the cap.
- **Bounded looks past a window.** `_event_from` and `_event_before` read at most `_LOOK` (64) quotes of the kind's character, using C `find`/`rfind` of the kind's needle (its quote, after a run at least its length). Past that, the answer is unknown, and no cut crosses what is unknown.
- **Each line start's neighbours, once.** `_around` finds the last visible character before each line start and the first after it in one pass over the window's lines. Starts between the same two visible characters share one verdict (a cache keyed by the pair).
- **C scans for whitespace.** `_last_visible` and the new `_run_end` strip in C in growing steps (64, 128, ... 8,192 characters), then scan the rest at most once: nine calls at most, whatever the run's length.
- **The tail's first data-URI run.** That run began before the window. It is a body when the last `,` before the window ends a `;base64,` with only body characters after it: one `rfind`, then at most one more scan. Before, the cut looped in Python over every `;base64,` in the text.

So the cut's work is linear in its two windows, plus a fixed number of C scans of the rest, plus at most 20 event computations, each at most 64 C scans.

**Counted, not timed.** `test_a_cut_s_work_does_not_grow_with_the_text` traces one `cut_whole` call. It counts the lines of `handoff` run, the C functions `handoff` calls (every scan of the text is one) and the event computations, with the collector paused. Each count is the same for 64 KiB and for 512 KiB of each shape, and at most 20 event computations, 40 × 16,384 lines and 20 × 16,384 C calls.

| Shape (4 MiB in the clock rows) | Lines, C calls, events at head (64 KiB = 512 KiB) | At base |
|---|---|---|
| review r2's head input | 11,371, 4,537, 0 (the head keeps only the marker) | 122 events |
| the same with `'` | 11,371, 4,537, 0 | 122 events |
| review r2's tail input (120 kinds) | 13,653, 6,322, 0 (the tail keeps only the marker) | over 3 million lines |
| the same with eight kinds | 150,122, 33,963, 10 | 10,882 events |
| `'"` lines (quote-dense) | 374,238, 150,217, 2 | passes: it was cheap at base too |
| JSON with escaped and double-escaped quotes | 116,964, 50,551, 3 | passes: it was cheap at base too |
| blank lines after a key | 401,402, 155,667, 0 | over 3 million lines |
| blank lines after `Bearer` | 133,943, 51,955, 0 | over 3 million lines |
| blank lines after a flag | 200,654, 77,821, 0 | leaks its value |

The counts at head are from a direct run of the test's counter at the final code. Under pytest, a collection's callbacks had once added six C calls at the first size, which is why the counter pauses the collector and counts only the calls `handoff` makes.

**Measured** on this host, Python 3.14.7 with the GIL, at load about 10, median (and maximum) of 7 runs for the cut and 5 for the handler, in ms:

| Shape, 4 MiB | `cut_whole`, one part | Handler: message | Handler: traceback | Handler: message, traceback and a stack |
|---|---|---|---|---|
| review r2's head input | 13.7 (14.8) | 21.9 (25.7) | 39.8 (47.5) | 50.4 (61.7) |
| the same with `'` | 10.5 (13.9) | 15.5 (23.8) | 27.6 (47.2) | 58.1 (66.6) |
| review r2's tail input | 8.9 (13.9) | 19.1 (24.7) | 38.1 (49.3) | 57.3 (67.4) |
| the same with eight kinds | 18.1 (19.5) | 24.0 (33.0) | 51.1 (58.2) | 60.1 (65.1) |
| quote-dense | 20.3 (34.7) | 25.0 (30.4) | 111.4 (127.1) | 130.9 (139.4) |
| JSON | 7.2 (12.1) | 27.5 (28.4) | 34.6 (50.8) | 44.3 (51.9) |
| blank lines after a key | 20.5 (23.7) | 12.2 (20.7) | 206.5 (218.7) | 202.2 (211.3) |
| blank lines after `Bearer` | 16.8 (22.0) | 14.5 (22.6) | 104.1 (118.7) | 125.8 (151.0) |
| blank lines after a flag | 10.0 (10.9) | 10.5 (11.1) | 151.1 (162.7) | 172.7 (202.0) |

- **The cut** of a 4 MiB part takes at most 35 ms on every shape, over two sessions; an earlier session measured 35.4 ms at most.
- **Records:** a message is under 35 ms. A traceback of up to 250,000 lines is under 70 ms (median 28 to 51 ms), and so is a record with both.
- **What goes over 100 ms:** the tracebacks of 1.4 to 4.2 million lines. Splitting them: Python's own `formatException` took 124 to 253 ms (median), before any cut. The cut and scrub took 21 to 39 ms (median), 62 ms at most.
- **Outside the lock:** `formatException` runs in the record's own thread, as round 2 found, and the handler's lock covers only the write.
- **At base:** review r2 measured 4.5 to 11 s for these inputs.

**Regressions.**

- `test_a_cut_s_work_does_not_grow_with_the_text`, for the nine shapes above.
- `test_a_four_mib_record_of_a_cost_shape_is_written_within_a_second`, for each shape as a message and as a traceback, the better of two runs. This is a generous guard, not a tight one (D-TM1). At base, 12 of its 16 first cases failed. Six took 4.7 to 9.7 s, four passed the 30 s timeout, and two leaked. The eight-kind cases pass at base on the clock; the count catches them.
- `test_a_side_with_more_than_eight_escaped_kinds_keeps_only_the_marker`: eight kinds are read, and a ninth keeps only the marker, on both sides.

**What the first cost test missed.** At first, round 2's eager `setdefault` survived as a mutant. Under the cap, the reviewer's tail input keeps only the marker before any event is computed. The eight-kind form of that input refuses every line start under the cap. With it, the mutant fails at 10,882 events.

### 2. What folding joins (P3)

Both forms fold the text to one line and scrub it again. Folded, `--password\nSECRET` is `--password SECRET`, which the flags rule masks. `_neutral_break` only guards the shapes `scrub_bounded` needs, and `scrub_bounded` never folds.

**The rule is derived from the flags rule itself.** `_folds_to_value` refuses a break when:

- the identifier before it is a credential flag by the rule's own predicate (`_flag_start`, which `_scrub_named_values` now calls too); and
- the rule's own value pattern (`_FLAG_VALUE_RE`) matches a space followed by the first visible character after the break.

So a `-p`-style short flag is no break: the flags rule needs a `--` and does not mask its value, in either form. If the rule ever masks short flags, the break rule follows it.

An identifier longer than 256 characters with no `--` in its last 256 reads as a flag when it ends in a sensitive word.

**Which rules folding joins.** `test_every_scrub_rule_has_an_example_and_only_the_folding_rules_fold` gives every rule an example split where a line break may fall. That covers each pattern in `_BLOCK_RULES` and `_TOKEN_RULES` (now named, so the test reads them), reminders, and `_scrub_named_values`' three passes. For each example, the test checks whether only folding joins the two parts: the unfolded whole-text scrub shows the secret and the line form hides it. That holds for exactly two rules:

- **The flags rule.**
- **A private key whose first and last lines are split:** `-----BEGIN RSA\nPRIVATE KEY-----`. `_PEM_RE` has literal spaces. The cut now reads those lines fold-tolerantly (`_PEM_START_FOLDED_RE`, `_PEM_END_FOLDED_RE`). A differential test checks that they match a line exactly when `_PEM_RE`'s own lines match it folded, over 18,522 spellings.

Every other rule's split is joined by the rule itself (`\s` in it) or not at all. A new rule fails the test until it has an example. `test_a_cut_never_parts_what_a_rule_or_folding_joins` then cuts each example inside its gap: five gaps, at the head's end, the tail's start and the text's start. In neither form does the cut show a secret the whole text hides. At base, it fails for the flags rule and the private key.

**The rebuilt generator found a third join: a group's margins.** The block form also scrubs a group's text with its margins removed (round 2's `_group_block`). There, a split private key, an escaped JSON value or a key and its value join across a margin that the margined text shows. `_margins_blanked` makes the cut read each line-leading margin's `|` or `+` as a space, in place. The cut then crosses the margin-free reading only where that reading allows. A margin's `|` only stops a match that a space would let run, so the margined reading never hides what the blanked one shows. When a text holds no `  |` or `  +`, this costs two C scans. Of the generator's 16,000 texts, this alone accounts for 649 leaking ones (mutant 5 below).

**Regressions.**

- `test_a_credential_flag_whose_value_starts_the_kept_tail_stays_hidden` uses review r2's input, through the handler.
- `test_a_flag_whose_value_starts_the_kept_tail_stays_out_of_daemon_log` and `test_a_flag_whose_value_starts_a_traceback_s_kept_tail_stays_out_of_daemon_log` run it through a real `Daemon`, read back from its daemon.log: once as a message, and once as a traceback lined up so its kept tail starts at the value. Both failed at base.
- `lines_with` holds the flag shape, a CRLF variant and the split private key.

### 3. Exception groups over the bound (P3)

The separator search ran on the cut text. When a group's own lines filled the kept head and the kept tail started inside its last member, the cut kept no separator, and the margin was read as the value again.

- **The handler now decides on each uncut part** (`_scrubbed`). Any part holding a separator's mark, `+` and sixteen `-`, is read as a group. That is one C scan of the part, and reading a text as a group only hides more.
- **`scrubbed_block` also reads any text with a line that starts with a group's margin as a group.** Either rule alone covers review r2's three shapes; I checked each with the other switched off. Mutant 4 below removes both.

Regressions, each failing at base:

- `test_a_group_longer_than_the_bound_keeps_its_group_handling`: review r2's three shapes, raised or only constructed, through the handler.
- `test_a_group_over_the_bound_is_written_to_daemon_log_scrubbed`: the same through a real `Daemon`.
- `test_a_process_that_raises_a_group_over_the_bound_writes_it_scrubbed`: a real child, its stderr a daemon.log, raising the group uncaught through `install_process_hooks`. The secret is built off the `raise` line.
- `test_a_block_with_a_group_s_margins_is_read_as_a_group`.

When scrubbing joins `Bearer` and its token across two member lines, the group is written without margins, as round 2's `_group_block` does. The secret stays hidden.

### 4. C-25.7 and the code (P3)

- **The stale carve-out is gone.** "A workspace completion SQLite could not record, which keeps SQLite's message" no longer applies: since round 2 (`98f2ef0c1`) that notice names the class. The change log says so.
- **The stop snapshot is listed.** C-25.7 now names C-5.8a's managed stop snapshot (`daemon._dump_stop_stacks`, a raw `os.write`) among daemon.log's other writers. It names every thread on every Python version and gives each frame's file, line and function, never a local, a source line or an exception's message. The thread-name lint covers the names.
- **runner.py's two lines** (`_handshake`'s `relay status:` and `_transmit`'s `frame <tag>:`) now pass `refusals.fixed_error(exc)` into `_unacknowledged`'s message, which `_relay_lost` logs.
- **The site lint follows a parameter to the log call it reaches.**
  - `logged_parameters` finds, per module and to a fixpoint, each function parameter whose value reaches a log call: passed raw, in a string built from it, or passed on to another such parameter (`_unacknowledged(why)` → `_relay_lost(f"{why}; …")` → `log.warning(…, why)`).
  - A call that passes a caught exception raw, in an f-string, `%`, `.format` or `str()` of it, to such a parameter fails the lint. So does a local such a string is assigned to.
  - `test_the_site_lint_fails_runner_py_s_old_lines` restores the two old lines in runner.py's current source. The lint then fails it at exactly those two lines, and passes the current file.
- **The extended lint found one more site.** The daemon's reader formatted a pool's thread-start `RuntimeError` into `_count_connection`'s detail, which it logs (`daemon.py`, the `unscheduled` count). That now uses `refusals.scrubbed_words`. It also read two scrubbed sites as raw: `refusals.detail_line` (which scrubs whole) and `isinstance(exc, …)`. Both now pass through.
- **The lint's reach:** functions of the same module, matched by name. A message built in one module and logged in another is not followed.
- **Rewritten:** the bound's sentence, the group sentence, and invariants 7 and 8 (round 2's "Invariants" above, restated in place).

### 5. The several-member group property (P3)

Round 2's §3 above now carries a correction. `test_a_several_member_group_never_shows_a_secret_its_member_alone_hides` replaces round 2's margins-only property. For groups of two or three members, one or two deep, raised or only constructed, every secret is made distinct with a fixed-width tag after `SECRET7`, so no secret is another's or a piece of one. Whatever a member's plain traceback hides, the group's hides too.

- **`over` case:** the group's own 1,200 lines and its last member's 1,000 lines put the record over the bound, so its separators fall out of what is kept.
- **Kept from round 2:** the margin-free and line-form checks, for groups under the bound.
- **At base:** it fails, on the `over` cases. Under the bound it held at base, as review r2 found.

### 6. Two costs of round 2

- **Wake.** With up to 16 run targets, `subfleet wake` could not say which run was refused. The refusal now names the target by its position: `run target 3 of 16 is not this conversation's`. The words are numbers `register` computes, never the id. The structural test allows only `position` and `count` in a refusal's f-string, and checks that `register` binds them from `enumerate` and `len`.
- **Marker line.** A one-line message whose cut keeps only the marker loses the logger's own words. The marker line now names the record's level and logger, fixed metadata, never its words: `… [4,194,339 characters omitted] … (WARNING record from subfleet.daemon.test-handler)`.

### Invariants, round 3

Invariants 7 and 8 are restated in place above. Each of these holds for every input and is executed by the tests named.

10. **The cut's regexes read only its windows; past them it reads a fixed number of passes of C scans and at most 20 needle looks.** (Restated in rounds 5 and 6. As first written, "the cut's work does not grow with the text", it was false: review r3 of #233 found three regexes reading the whole text, which counting each as one C call had hidden. Round 5's "at most 12 passes" was false too: review r4 found that each quote kind open at a candidate cut costs a look over the text, up to ten a side, and that the meter counted a data URI body's match to the window's end.) For any text and limit, the regexes `cut_whole` runs read at most `16 × RECORD_SCRUB_MAX` characters in all (a character class's match counted by what it reads), none more than twice the bound in one call, and no more at 16 MiB than at 1 MiB. Past the windows its `str` scans are a fixed number of passes over the text plus at most `2 × (CUT_KINDS_MAX + 2)` = 20 needle looks, each reading the text at most once. On the round-2 cost shapes the lines it runs and its C calls are the same at 64 KiB and 512 KiB. Its cost is a measured ratio to plain text (round 6, §1): at most 6.7 times, on runs of short margin-only lines, an accepted cost; review r4's quote kinds 2.2 to 2.7 times. Tests: `test_at_16_mib_no_regex_reads_past_the_cut_s_windows`, `test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads`, `test_a_cut_s_work_does_not_grow_with_the_text`; the clock guards.
11. **Folding parts nothing a cut keeps.** No cut falls where folding the text to one line joins what is before the break to what is after into a match. Every scrub rule has an example, and the folding ones are exactly the flags rule and the private-key lines. Tests: `test_every_scrub_rule_has_an_example_and_only_the_folding_rules_fold`, `test_a_cut_never_parts_what_a_rule_or_folding_joins`, the generator.
12. **A logged exception's words are scrubbed at every site.** No daemon module passes a caught exception raw to a log call, or to a parameter of its own functions whose value reaches one. Tests: `test_no_daemon_module_logs_a_caught_exception_raw`, `test_the_site_lint_fails_runner_py_s_old_lines`.

### The rebuilt generator

`fuzz_text` builds texts of 1 to 40 lines from these shapes, among plain lines (`  + more` among them), with a limit of 40 to 400:

- the cut shapes of `lines_with`;
- credential flags (`--password`, `--db-token`, `x--api-key`, `--client-secret`, and `--password-file` and `-p`, which the rule does not mask), with their values a line or more on;
- JSON with plain, escaped and double-escaped quotes;
- split private keys;
- a group's margins (`  | `, `    | `) behind a separator;
- a leading `Authorization:`, `Cookie:` or `Set-Cookie:` header;
- CRLF line ends.

Each text is checked two ways:

- **The cut text, in the daemon's forms.** It shows no secret the uncut text shows nowhere. The block form, and the line form for a text without margins (folded, the two agree only without margins, invariant 3).
- **Through the handler.** A record cut at the text's limit shows no secret the same record uncut hides.

`test_the_rebuilt_generator_finds_review_r2_s_leaks_shapes` checks that it reaches the flag, margin and header shapes.

| Code | Texts | Cut | Leaking |
|---|---|---|---|
| base (`2b64f76a3`) | 16,000 | 13,091 | 1,269: 293 without group margins, 976 with |
| this round without the flag break rule (mutant 3) | 16,000 | 13,091 | 416: 92 without margins, 324 with |
| this round without the margin reading (mutant 5) | 16,000 | 13,091 | 649, all with margins |
| final, through pytest under the lock (`SUBFLEET_CUT_FUZZ=16000`) | 16,000 | 13,091 | 0 |
| final, a direct run on 16 other seeds | 16,000 | 12,995 | 0 |

The CI run is a bounded slice of 300 texts over three seeds. Before the margin reading, those 300 found a leak at each of the three seeds.

### Mutations

Each was applied to a copy of the final code and run against the named tests, one hold each:

| Mutation | Failed |
|---|---|
| 1. Round 2's eager `setdefault` in `_whole_tail_cut` | `test_a_cut_s_work_does_not_grow_with_the_text[tail-eight-kinds]`: 10,882 event computations. It survived until that shape was added (§1). |
| 2. No kind cap (`_too_many_kinds` never true) | `test_a_cut_s_work_does_not_grow_with_the_text[head, head-single-quotes, tail]`: 122 events; `test_a_side_with_more_than_eight_escaped_kinds_keeps_only_the_marker` |
| 3. No flag break rule (`_folds_to_value` false) | 8: the flag test through the handler, both through a real `Daemon`, `[flags]` of the per-rule test, the generator at all three seeds, and the count test's blank lines after a flag |
| 4. Group detection after the cut (round 2's: the separator in the cut text only) | 16: every over-bound group case through the handler, a real `Daemon` and the child; the margin-line cases; the several-member property |
| 5. Margins read as they are (`_margins_blanked` a no-op) | `test_the_rebuilt_generator_finds_no_leak`, all three seeds |

### Tests per file, round 3

New or changed:

- **`tests/unit/test_daemon_log_handler.py`, 120 cases:**
  - the cost count, nine shapes;
  - the clock guard, 18 cases;
  - the cap;
  - the flag shape;
  - the rule table;
  - the per-rule cut, 12 rules;
  - the private-key differential;
  - the whitespace list;
  - over-bound groups, 6 cases;
  - margin lines, 3 cases;
  - the generator, its coverage check and 3 seeds;
  - the several-member property;
  - the marker line, and `lines_with` with three more shapes;
  - round 2's two spies, which now pass `group` on.
- **`tests/unit/test_daemon_log_sites.py`, 42 cases:**
  - the flag shape through a real `Daemon`, twice;
  - the over-bound group, three shapes;
  - the child process, three shapes;
  - the lint's runner.py case;
  - eight more lint snippets.
- **`tests/unit/test_conversation_wakes.py`, 27 cases:** the run target's position, and the structural test.

### Runs, round 3

All runs used a fresh `.venv` (`uv sync --locked --group dev`, Python 3.14.7 with the GIL).

- **One pytest per hold.** Each pytest ran alone in its own hold of the shared lock, in the foreground, as the venv's `python -m pytest` (the same environment as `uv run`), with no xdist. Hypothesis ran with each file's own bounded, derandomized settings and no database.
- **Waiting fairly for the lock.** Another job held the lock in back-to-back holds, and polling for it with `lockf -t 0` kept losing it to that job's blocked `lockf`. So the later holds waited blocked in `flock(2)`, under an alarm, as `lockf` itself waits, then ran pytest as a child. They are holds of the same lock.
- **At base.** These ran on a tree of `2b64f76a3`'s `subfleet/` with this round's tests.

| Hold | What | Result |
|---|---|---|
| base 1 | the new non-cost cases | 42 failed, 26 passed. The 26: the ten rules' per-rule cases folding does not join, 12 lint snippets (round 2's 11 and one alias case; the lint is test code), three timeline inputs whose words were already fixed, and the generator's coverage check. Seven lint snippets failed on an indentation error of mine, fixed in `552c796cc`. |
| base 2 | the cost count and the clock guard | 18 failed, 6 passed: the quote-dense and JSON shapes, cheap at base too |
| base 3 | the eight-kind shape | the count failed at 10,882 events; its two clock cases passed |
| 1 | brief group 1: `test_daemon_log_handler` 120, `test_daemon_log_sites` 42, `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 315 | 622 passed, 30 s |
| 2 | brief group 2: `test_conversation_service` 98, `test_turn_runner` 82, `test_conversation_wakes` 27, the six `test_suggestions*` 89 | 296 passed, 195 s |
| 3 | brief group 3: `test_sessions_handoff` 117, `test_sessions_handoff_excerpts` 153 | 270 passed, 83 s |
| 4 | brief group 4: `test_daemon_accept` 5, `test_stop_watchdog` 29, `test_daemon_verbs` 20 | 54 passed, 63 s |
| 5 | brief group 5, and two more: `tests/fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 (the contract changed), `test_descriptor_review_fixes` 56 (`_count_connection`'s site) | 119 passed, 25 s |
| fuzz | the generator, `SUBFLEET_CUT_FUZZ=16000` | 4 passed, 39 s |
| m1 to m5 | the mutations above | each failed as listed |
| 3.12 | brief group 1 on Python 3.12.14, a scratch venv made offline from uv's cache | 622 passed, 38 s |
| collect | the whole suite, `--collect-only` | 15,170 collected in 484 files, no errors |
| final | brief groups 1 and 3 again, at `6d56b55f2`, after `import bisect` moved to the module's imports | 892 passed, 120 s |

**Code after the runs.** Hold 1 ran at `ff91e8ece`. Holds 2 to 5 ran at `e5d59955d`; after it, only `test_daemon_log_handler.py` changed, and then `6d56b55f2` moved `import bisect` to the top of `handoff.py`, after which groups 1 and 3 passed again. Two earlier group-1 holds failed:

- **First (8 failed):** round 2's spies, the collector's six C calls, and the generator's margin leaks. These are fixed in `ad2bfdb46` and `1e7a985a3`.
- **Second (1 failed):** the lint's `isinstance` case, fixed in `618161bb2`.

**Not run:**

- the full suite;
- `tests/e2e`, `tests/frontend` and `tests/process`;
- Python 3.12 beyond brief group 1;
- a real `launchctl`;
- CI, since this job has no network.

### Commits, round 3

- `ff34adb7b` test: review r2 of #233: the cut's cost, a flag's value, a group over the bound, the lint, distinct group secrets
- `da0db6db4` fix: a record's cut does bounded work and never parts what folding joins
- `68b477930` fix: a record's part is read as an exception group before its cut; a marker-only line names its level and logger
- `39c8e7551` fix: an exception formatted into a message another function logs uses fixed or scrubbed words
- `4d2244353` fix: a refused run target is named by its position, never its id
- `552c796cc` test: the lint cases' indentation, a joined group's margins, a 30 s guard timeout
- `ad2bfdb46` test: spies take `group`; the work count pauses the collector and counts handoff's calls; group text's forms
- `1e7a985a3` fix: the cut reads an exception group's margins as spaces
- `618161bb2` test: the site lint reads `isinstance` of an exception as carrying no words
- `e5d59955d` docs: C-25.7 states the round-3 cut, the group decision, the marker line, the stop snapshot and the wake position
- `1eccdc799` test: a cost shape that keeps the tail loop refusing under the kind cap
- `ff91e8ece` test: the generator's seeds split SUBFLEET_CUT_FUZZ exactly
- `b103bf06f`, `b89c5b920` docs: this report's round 3
- `6d56b55f2` style: handoff imports bisect with its other modules

Nothing is pushed.

## Round 4: the merge with release/217 (#222)

The hub merged release/217 (`f99da54b0`, with #222's launch-environment allowlist, D-ENV1) into round 3's head as `6cbaea975`, and its host run there had two failures. This round starts from `6cbaea975`. **Base.** This job has no network. The last fetched `origin/release/217` is still `f99da54b0`, which `6cbaea975` holds, so there was nothing to merge.

### 1. What the merge did, as executed

The two tests trigger with a policy whose admission key is `token=SECRET7policy9`. At `6cbaea975`, with this round's tests (the base runs below):

- **`daemon start` refuses before it spawns.** #222 added a pre-launch check (`cmd_daemon_start` → `daemon_env` → `load_policy`, C-11.1 "by `daemon start` before it spawns"). It raised `PolicyError`, and `fail(Exit.INVALID_INPUT, str(exc))` printed `subfleet: /tmp/sf-…/policy.json: admission.token=SECRET7policy9: is not an admission setting (desktop_recent_s, …)` to the CLI's stderr and returned 2. The daemon never ran and daemon.log was never created. So the line the hub saw came from **the pre-launch check, not the log tail**. That is why `cli.main(["daemon", "start"])` returned 2, not 69.
- **The daemon's own refusal.** `dbfd33c64` made `daemon.main` catch the same `PolicyError` and `print(f"subfleet daemon: {exc}", file=sys.stderr)`. Under launchd that stderr is daemon.log, and a bare `print` bypasses the process hooks. So `subfleetd` exited 2, not 1, and daemon.log held `subfleet daemon: /private/tmp/sf-…/policy.json: admission.token=SECRET7policy9: is not an admission setting (…)`. The same happens under `daemon start` when policy.json changes after the CLI's check and before the daemon reads it (the regression below).
- **The held lock's line before it.** `print(str(exc), file=sys.stderr)` for `DaemonUnavailable` was the same kind of write. Its words are fixed today ("another daemon holds daemon.lock"), and round 3's C-25.7 listed it among daemon.log's other writers.

### 2. The daemon's own writes go through the handler's formatter

- **`daemon_log.write_scrubbed(text, level)`** writes a line to the process's stderr, as it is at the call, through `_StderrHandler`, exactly as the handler writes a record of it:
  - cut to `RECORD_SCRUB_MAX`, then the line form or the block form;
  - a fixed `[scrub failed]` line when the scrubber raises, and a fixed line when the write fails;
  - it never raises.
- **`daemon_log.scrubbed(text)`** returns that text, for the CLI.
- **The process hooks** share `_report` with `write_scrubbed`. Their behaviour is unchanged.

**Sites fixed:**

- `daemon.main`'s refused policy or roster (exit 2) now writes `subfleet daemon: <root>/policy.json: admission.token=[REDACTED] is not an admission setting (…)` to daemon.log, with no traceback (C-11.1).
- `daemon.main`'s held lock (69).
- `policy.resolve_model`'s retired-alias note, which predates #222. The daemon writes it on a submit (`daemon.py`, `resolve_model(self.policy, model)`), on an admission pass (`scheduler.prepare`) and in `picker`. Both names in it are the policy's own text.

**The colon.** The brief's example keeps a colon after `[REDACTED]`, but the scrubber reads a colon right after a value as part of the value (`SECRET7policy9:`), so the line reads `admission.token=[REDACTED] is not an admission setting (…)`. I left the scrubber alone, because a value may hold a colon. C-25.7 says so.

**The stream lint** (`test_daemon_log_sites.py`, `stream_sites`). In every module of `subfleet/` except the front ends, the lint fails on:

- any `print`;
- any `write` or `writelines` on `sys.stdout`, `sys.stderr`, `sys.__stdout__`, `sys.__stderr__`, or a bare `stdout` or `stderr`;
- any of `traceback`'s printers.

It fails these whatever they write. It also fails a write past the handler, `os.write` included, that passes a caught exception raw, directly or through a parameter of the module's own functions (round 3's `logged_parameters`, now taking a sink). `os.write` with fixed words or class names stays allowed, because C-5.8a's stop path writes daemon.log's own descriptor that way.

- **Exempt** (`TERMINAL_MODULES`): `cli.py`, `compat.py`, `hooks.py`, `importer.py`, `prune_decisions.py`, `gate/cli.py` and `sessions/cli.py`, plus `daemon_log.py`, the writer itself. `test_the_terminal_modules_are_front_ends_a_daemon_does_not_load` checks three things: each exempt module writes its streams, so none is exempt for nothing; a fresh `import subfleet.daemon` loads none of them; and a module named but absent fails.
- **At `6cbaea975`** the whole-tree lint fails with exactly five findings: `daemon.py:9768` and `:9773`, each as a `print` past daemon_log and as `passes exc raw`, and `policy.py:850`'s `print`.
- **`test_the_stream_lint_fails_dbfd33c64_s_line`** puts `main`'s two old lines back into today's daemon.py. The lint then fails it at exactly those two lines, four findings in all, and passes the file as it is.

**The child lint** (`inherited_streams`). Every `subprocess` start in a daemon module sets both stdout and stderr, or captures them, and never to `None` or a `sys` stream. A start that passes on its function's `**options` is read at each call of that function (`spawn_guardian`, whose one call sets both to `DEVNULL`). Removing them there fails the lint. All 32 starts pass today. The guardian's `posix_spawn` (`procs.spawn_disclaimed`) opens /dev/null on fds 0, 1 and 2, which I read in the code.

### 3. What `daemon start` prints is scrubbed the same way

- **The pre-launch refusal** prints `subfleet: <root>/policy.json: admission.token=[REDACTED] is not an admission setting (…)`, still exit 2 and still before any spawn. Each held-lane note goes through `daemon_log.scrubbed` too.
- **The tail.** `_log_tail`, which `daemon start` and `daemon install` print when the socket does not appear, scrubs daemon.log's whole text as one record before it takes the last 20 lines. "Whole" means cut to the record bound first, at `handoff.cut_whole`'s safe breaks, so a 4 MiB file costs what one record does. It adds each line's `  | ` margin after the scrub, so the scrub never reads the margin as an exception group's.
  - **Whoever wrote the file:** a daemon before D-LOG1, or a value on the line after its key, is hidden in the tail.
  - **Folding:** where folding lets the scrubber find more (a credential flag with its value on the next line), the tail is the one folded line, as the handler writes such a block.
- **C-25.7** now says `daemon start`'s output is in scope, although it is a person's terminal, because it is the daemon's text. **C-11.1** says the daemon's startup refusal is written as the handler writes a record, with no traceback, and that `daemon start` prints its own the same way. The change log records both.
- **Not changed:** `daemon logs` still prints daemon.log as it is. It is the file's viewer, and `--follow` streams one line at a time, where a value on the line after its key cannot be told from a word. It is listed under "Outside this change", below.

### 4. The retargeted tests

- **The refusal path:**
  - `test_under_daemon_start_a_refused_policy_is_exit_2_and_scrubbed_before_any_spawn`: exit 2, one scrubbed line, no spawn, no daemon.log.
  - `test_under_daemon_start_the_daemon_s_own_refusal_reaches_daemon_log_scrubbed`: `cli.daemon_env` is wrapped to write the refused policy after the real check returns, so the real `subfleetd` reads it. The CLI returns 69, its note says `exited 2`, and daemon.log and the CLI's tail both hold the scrubbed line, with no traceback.
  - `test_under_launchd_a_refused_policy_is_exit_2_and_reaches_daemon_log_scrubbed`: the plist's program, folder, environment and output paths. It exits 2, daemon.log holds the scrubbed line, and so does `_log_tail`, as `daemon install` would print it.
  - `test_daemon_main_writes_a_refused_start_as_the_handler_writes_a_record[lock, policy]`: in-process, the exact line `scrubbed` returns.
- **Uncaught exceptions under both real launches.** `daemon start` and the plist run `<interpreter> -E -P subfleetd`, which reads no `PYTHONPATH`, so round 3's sitecustomize trigger cannot reach them. A venv's own `sitecustomize.py` would not do either: Homebrew's Python has one in its standard library, which comes first on `sys.path`, as I found when a probe of it did not run.
  - **The trigger** (`_failing_interpreter`): a venv of the test's base interpreter whose `.pth` files run under `-E -P` too. `00-this-checkout.pth` calls `site.addsitedir` on the test venv's site-packages, which is this checkout's editable `subfleet`. `zz-failing-startup.pth` imports `FAILING_STARTUP`, the old `SITECUSTOMIZE`, which in a `subfleetd` process only replaces `descriptors.raise_open_file_limit` with a function that fails four ways: in a thread, in a finalizer, as a warning, and in the main thread. `sys.executable` is patched to the venv's Python, so `_daemond_argv` and `_plist` start it as they would start any interpreter.
  - **The two tests** keep their names. `daemon start` returns 69 with `exited 1`, launchd's run exits 1, and daemon.log holds all four failures scrubbed. The CLI's tail holds none of their secrets. Round 3's direct-exec test keeps its `PYTHONPATH` sitecustomize.

### 5. The sweep of #222's code

I read #222's diff (`3b833a456..6cbaea975`, `subfleet/`) for any daemon-side write of exception or config text that skips the handler:

| Site | Where it goes | Verdict |
|---|---|---|
| `daemon.main`'s `PolicyError` print (`dbfd33c64`) | stderr, daemon.log | **fixed**: `write_scrubbed` |
| `cli.cmd_daemon_start`'s refusal and held-lane notes (`daemon_env`) | the CLI's stderr | **fixed**: `daemon_log.scrubbed` |
| `Daemon._open_lanes`'s `held_lane_message`, `_seed_lanes`'s repair line | `self.log`, the handler | scrubbed there; names only |
| `policy.check_lane_credentials`'s refusals | `PolicyError`: the two sites above | names only. The scrubber leaves the shapes #222's tests use unchanged (`E2E_LANE_NINE_TOKEN`, `HOME`, `SUBFLEET_FAKE_LANE_NINE`, `credential_ref:`, `env.pass[1]:`, checked by hand), and their 49 + 39 cases pass |
| `provider_env`'s CA-bundle warning | `self.log`, the handler | already `refusals.scrubbed_words(exc)` |
| `credentials.resolve_credential`'s refusals | `AdapterError`, a handler refusal | fixed words and reasons; its template on the wire, its detail through `refusals.detail_line` |
| `launch_env` | `ValueError("environment pass-through requires exact variable names")`, `environment_report` | fixed words; names only |
| `guard.preflight`'s `_Refusal(CONFIG, str(exc))` | its verdict | runs only when no `env` is passed (`doctor`); every daemon call passes one (`daemon.py` `_guard_override` at its three calls) |
| `guardian.py`'s new `Popen` calls | the guardian's own files | the guardian's stdio is /dev/null |
| every `subprocess` start in a daemon module (32) | the child's streams | all set; held by the child lint |

The one site found outside #222 is `policy.py:850`, the retired-alias note above.

### 6. What the whole suites found

- **`tests/unit/test_host_push_store_interleavings.py`, four cases.** These have failed on this branch since round 2. `98f2ef0c1` (#233 review r1) made `job.workspace_failed`'s notice name the class (`refusals.fixed_error`), as C-25.7 requires, and updated `test_host_push_storage_retry_budget.py`. These two tests still looked for sqlite's message (`str(error) in notices`). They now check `workspace completion could not be recorded: <class>`, with the message absent: `d44dc5600`, tests only. Neither the merge nor this round caused them. Round 3 did not run the whole suite.
- **`tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps`, one case.** This is a random finding. Without `CI` set, the file uses its random `dev` profile, and seed `3032905352982056795255603331366813964` fails C-4.1's P2 non-interference check: `… is held behind 20260924-120000-haiku, whose own hold {'reason': 'no-slot', 'for_good': ['disabled'], …} holds back nobody`, shrunk. It fails the same way on a `git archive` of `6cbaea975`, and under `CI=1`, CI's derandomized profile, the file passes (11 of 11). So it is admission's, it predates this round, and CI's examples do not reach it. It is listed under "Outside this change" below.

### Invariants, round 4

Each holds for every input and is executed by the tests named.

13. **A daemon module writes its stdout and stderr only through `daemon_log`.** No `print`, standard-stream `write` or traceback printer outside the front ends, and no exception's words to `os.write`. Tests: `test_no_daemon_module_writes_its_standard_streams_past_daemon_log`, `test_the_stream_lint_fails_dbfd33c64_s_line`, 16 lint cases.
14. **`scrubbed` is the handler's line.** For any text, `daemon_log.scrubbed(text) + "\n"` is what the daemon's handler writes for a record of it. This is a differential test, Hypothesis, any text up to 300 characters, one line or several. Test: `test_scrubbed_is_the_handler_s_line_for_a_record_of_any_text`.
15. **No credential shape survives `scrubbed`.** This is round 2's assignment strategy. Test: `test_scrubbed_keeps_no_secret_of_any_credential_shape`.
16. **`write_scrubbed` never raises.** It writes to stderr as it is at the call, and writes a fixed line when the scrubber or the stream fails. Tests: the three `test_write_scrubbed_*` cases.
17. **No child a daemon module starts inherits the daemon's stdout or stderr.** Tests: `test_no_child_a_daemon_module_starts_writes_to_daemon_log`, 9 lint cases.

### Tests per file, round 4

New or changed:

- **`tests/unit/test_daemon_log_sites.py`, 82 cases** (round 3: 42):
  - the three refusal-path tests and `daemon.main`'s two refusals;
  - the two retargeted uncaught-exception tests;
  - the tail through `daemon start`, two shapes, and `_log_tail` itself, four cases;
  - the stream lint: the tree, the front ends, 16 cases, and `dbfd33c64`'s line;
  - the child lint: the tree and 9 cases.

  The two old policy-trigger tests are replaced by the retargeted ones under the same names.
- **`tests/unit/test_daemon_log_handler.py`, 125 cases** (round 3: 120): the `scrubbed` differential and secrecy properties, and `write_scrubbed`'s stream, marker and failed-write cases.
- **`tests/unit/test_host_push_store_interleavings.py`, 26 cases:** two tests changed, four cases.

### Runs, round 4

All runs used a fresh `.venv` (`uv sync --locked --group dev`, Python 3.14.7 with the GIL), each pytest in its own hold of the shared lock, in the foreground, as the venv's `python -m pytest`, with no xdist (it is not in the lock file) and `PYTHONPATH` unset. **Base runs:** the four changed source files checked out at `6cbaea975` in the worktree, with this round's tests, then restored.

| Hold | What | Result |
|---|---|---|
| base 1 | the 48 new cases, at `8269e0142`'s tests | 19 failed, 29 passed: the uncaught-exception tests, the lint cases, the front-end check. The child lint read the preflight's and the guardian's own `stderr` files as the daemon's; that was fixed in `780ffe1ba` |
| head 1 | the same 48, at `780ffe1ba`'s tests with the fix in the worktree, committed unchanged as `6db97a441` and `71e09e6c8` | 48 passed |
| 1 | brief group 1: `test_daemon_log_handler` 125, `test_daemon_log_sites` 82, `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 317 | 669 passed, 28 s |
| 2 | brief group 2: `test_cli` 222, `test_daemon_accept` 5, `test_launch_env_allowlist` 27, `_helpers` 20, `_mutations` 41, `_policy` 26, `_round2` 67, `_round3` 67, `_round4` 40, `_round5` 39 | 554 passed, 40 s |
| 3 | brief group 3: `test_conversation_service` 98, `test_turn_runner` 82, `test_conversation_wakes` 28 | 208 passed, 60 s |
| 4 | brief group 4, and three more: `tests/fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 and `test_desktop_ledger` 4 (the contract changed), `tests/fake/test_launch_env_daemon` 49 (#222's in-process `daemon.main` refusals) | 116 passed, 5 s |
| suite | every file of `tests/unit` and `tests/fake`, six holds of at most 17.5 minutes (§ below) | `tests/unit`: 319 files, 12,943 cases: 12,942 passed, 1 skipped (`test_host_push_build`). `tests/fake`: 106 files, 1,848 cases: 1,845 passed, 2 xfailed (`test_admission_properties`, `test_canonical_identity_fix3`), 1 failed (`test_admission_stateful`, §6) |
| seed | the stateful seed at head and at base, without shrinking | both failed, the same check |
| CI profile | `tests/fake/test_admission_stateful.py` under `CI=1` | 11 passed, 28 s |
| base 2 | the new cases and `test_host_push_store_interleavings`, at `d44dc5600`'s tests | 18 failed (13 sites, 5 handler), 56 passed (30 sites, all 26 host-push) |
| base 3, 4 | the refusal and tail cases, after `1a298b0e1` and `5f6e42a60` put each secret check first | 11 failed, then 3, each on the secret: the CLI's `admission.token=SECRET7policy9`, and daemon.log's `subfleet daemon: /private/tmp/sf-…/policy.json: admission.token=SECRET7policy9: …` under `daemon start` and launchd |
| m1, m2 | the mutations below | each failed as listed |
| 3.12 | brief group 1 and `test_host_push_store_interleavings` on Python 3.12.14, a scratch venv made offline from uv's cache | 695 passed, 35 s |
| final | `test_daemon_log_sites` at `5f6e42a60` | 82 passed |

**Code at the runs.** `subfleet/` has been the same since `71e09e6c8`. Holds 1 to 4 and the first three suite holds ran at `a82bbcfd6`. The last three suite holds ran at `d44dc5600`, and they re-ran `test_host_push_store_interleavings.py` whole (26 passed). After that, only the order of asserts in `test_daemon_log_sites.py` changed, and the file passed again at `5f6e42a60`.

**The suite holds.** Each hold ran pytest in-process over the files not yet complete (`hold.py`, in scratch, removed), with JUnit XML. A file counts as complete only when all its collected cases are in the XML; a file a hold cut short ran again whole in the next.

- **Collection:** 14,791 cases in 425 files, no errors.
- **Hold 3 ran past its alarm.** A test took over `ITIMER_REAL`, as the stop watchdog's tests do. I sent SIGINT to my own pytest at 17 min 32 s, under the 20-minute cap. Later holds stop on a watchdog thread that sends SIGINT to the main thread (`signal.pthread_kill`) instead. The Bash tool moved that hold's call to the background at its 600 s limit; I waited on it in the foreground and started nothing else.
- **Hold 5's pytest raised at session end.** Its stop landed in a fixture's teardown. Its JUnit XML was whole (2,585 cases, none failed), and the hold was settled from it.
- **One deviation from one pytest per hold.** The three seed replays (21 s in all) ran one after another inside a single hold.
- **Hypothesis profile.** Hypothesis used each file's own settings. `test_admission_stateful.py` therefore ran its random `dev` profile, since `CI` was not set; CI runs its derandomized `ci` profile, also run above.

### Mutations, round 4

The base runs are the main control: every regression above fails on `6cbaea975`'s source. Two more:

- **m1:** `daemon_log.scrubbed` returns `refusals.scrubbed_line(text)`, the line form, for every text. 8 failed: the differential, the marker case, the four `_log_tail` cases, and the two `daemon start` process tests, whose tails fold.
- **m2:** `write_scrubbed` writes to `sys.__stderr__` instead of the stream at the call. 10 failed: two handler cases, `daemon.main`'s two, and six of #222's own `tests/fake/test_launch_env_daemon.py` refusal cases.

### Not run, round 4

- `tests/e2e`, `tests/frontend` and `tests/process`;
- the whole suites on Python 3.12: only brief group 1 and the host-push file ran there;
- a real `launchctl`: the launchd tests run the plist's program as launchd would;
- CI, since this job has no network.

### Outside this change

- **`daemon logs`** prints daemon.log as it is (§3). Scrubbing a viewer that follows one line at a time is a design question of its own.
- **The stateful admission finding** (§6, seed `3032905352982056795255603331366813964`) is C-4.1's. It reproduces on `6cbaea975`.

### Commits, round 4

- `0893e8404` test: #233 round 4: the merge's policy refusal, daemon start's output and writes past the handler
- `8269e0142` test: read daemon_log.scrubbed at call time, so the file collects on a tree without it
- `780ffe1ba` test: a module's own stdout or stderr file is no inherited stream; main's refusal test fails on the leak first
- `6db97a441` fix: the daemon's own writes to stderr go through the handler's formatter
- `71e09e6c8` fix: daemon start prints a refusal and its tail of daemon.log scrubbed
- `a82bbcfd6` docs: C-25.7 and C-11.1 state the daemon's own writes and daemon start's output scrubbed (#233 round 4)
- `d44dc5600` test: a push workspace's store-failure notice names sqlite's class, never its words
- `1a298b0e1` test: each refusal and tail case checks the secret first, so on a tree without the fix it fails on the leak
- `5f6e42a60` test: the refusal check names the line that holds the secret before it looks for the scrubbed words
- this report's round 4

The commits are on this worktree's detached HEAD: the caller's checkout holds `fix/daemon-log-scrub-217`, so Subfleet keeps them under `refs/subfleet-salvage/`. Nothing is pushed.

## Round 5: review r3 of #233

Review r3 (`REQUEST_CHANGES`) found no leak and asked for two P3s; the hub took the optional third and the reviewer's note on `daemon logs`, and one rule-table gap. This round starts from `cce8f1af7`.

**Merge.** `origin/release/217` as the hub last fetched it (`473fe2941`: #235, #236, #234, #226) merged into `cce8f1af7` as `5824f8d87`, without a fetch. The one conflict was the contract's change log, as the reviewer found. Both sides' entries are kept: this branch's four D-LOG1 entries, then release's ten (D-ST1's two C-20.6 entries and its round-9 one, D-KILL2's two C-5.5 entries, #236's and #230's four C-26.4 entries, D-ADM3's), then the shared history. Release's stray blank line between its D-ADM3 entry and the shared C-23.14 entry was dropped, so the list stays one list. `subfleet/daemon.py`, `subfleet/relay.py` and the tests merged cleanly. The three daemon-write lints (`test_no_daemon_module_logs_a_caught_exception_raw`, `test_no_daemon_module_writes_its_standard_streams_past_daemon_log`, `test_no_child_a_daemon_module_starts_writes_to_daemon_log`) read every module of `subfleet/` but the front ends, so the merged `daemon.py` and #236's `relay.py` among them, and pass (holds 1 and 2).

### 1. The cut's cost (P3-1): windowed margins

The three whole-text passes are gone, and so is a fourth, which I found by reading the cut for every read past a window (no profile was run this round).

- **Margins.** `handoff._Reading` holds the text as the cut reads it. `_MARGIN_MARK_RE` blanks the margins only within the two windows (`_blanked`: the head's first `limit // 2 + 1` characters, and the tail from its first line start; each slice with the one character after it that a mark's lookahead reads). Between the windows the text is as it is. Every look for the last or next visible character that may run past a window goes through `_Reading.before` or `_Reading.after`, which `_neutral_break` now takes as its look-back (`before=`), `_around`, `_quote_events` and `_event_before` call, and `_leads_with_header` uses:
  - A look stops at the first visible character, as `_last_visible` and `_run_end` do. Where that character is a `|` or `+`, `_is_margin_mark` asks whether it is a margin's mark (an even run of two or more spaces from its line start before it, and a space, a line break or the end after it); if so, the look goes on past it.
  - From a mark's line, it passes the whole lines beyond that hold only whitespace and marks, in C scans: `_margin_lines` maps the span to one class byte a character (`bytes.translate`), checks no other character and no bar after a bar or other whitespace, removes the spaces and checks every bar follows a line break, and counts the bars at even columns with `expandtabs(2)` (each bar a tab adds one character at an even column). The spans grow from 64 characters, doubling, then halve after the first that fails, so the work is linear in the run. Each run found is kept (`spans`), so the head's look past its window, the leading header's look from the start and the tail's look back read a run once between them.
- **Data URI body.** The tail's check that the text between a `;base64,` and its first line is all body characters was a regex search (`_NOT_BODY_RE`) that could read most of the text. `_body_between` does it in the same C scans, with the marks read as spaces. It was not among the reviewer's three; I list it as the fourth whole-text pass, found by reading the cut for every read past a window.
- **Reminder tag.** `_holds_reminder_tag`: no `r>` or `R>` (two C scans) means no tag. Otherwise the text is lowered after `_REMINDER_FOLDS` (`İ`, `ı` and `ſ`, the only characters other than ASCII cases that the regex, which ignores case, takes for a tag letter; with them replaced `str.lower` keeps every position), `str.find` finds the tag, and `_REMINDER_TAG_RE` confirms it within its 17 or 18 characters. A test checks every code point.
- **Leading header.** `_leads_with_header` matches the header's name at the text's first visible character (`_HEADER_NAME_RE`, at most 13 characters) and looks for the `:` past it. Nothing backtracks: the regex's `\s*` is now the look, which is one C scan of the run.

**The bound, counted.** `test_at_16_mib_no_regex_reads_past_the_cut_s_windows` meters one cut: every compiled pattern in `handoff` counts the span each call may read (`pos` to `endpos`; a substitution all its string), and the text (and the reading's `read`) counts what each `str` call scans or copies (`find` and `rfind` to where they stop, `count`, `in`, `lower`, `replace`, `isascii`, `encode` and slices). For each of review r3's shapes, plain text and the nine round-2 cost shapes, at 1 and at 16 MiB:

| Shape | Regex characters, 1 MiB and 16 MiB | Widest regex call | `str` passes over the text, 16 MiB |
|---|---|---|---|
| plain | 49,106 and 49,106 | 8,177 | 3.0 |
| margin-only-lines | 65,538 and 65,538 | 8,194 | 5.0 |
| margin-only-lines-spaced | 65,524 and 65,524 | 8,194 | 5.0 |
| member-lines | 65,582 and 65,582 | 8,194 | 4.0 |
| leading-spaces | 10 and 10 | 2 | 6.0 |
| run-of-lt | 53,258 and 53,258 | 8,192 | 2.0 |
| exception-group | 65,465 and 65,465 | 8,194 | 4.0 |
| round2-blank-lines-after-a-flag | 49,279 and 49,279 | 8,191 | 5.0 |
| round2-blank-lines-after-a-key | 49,244 and 49,244 | 8,192 | 6.0 |
| round2-blank-lines-after-bearer | 49,255 and 49,255 | 8,192 | 5.0 |
| round2-head | 49,100 and 49,100 | 8,178 | 3.0 |
| round2-head-single-quotes | 49,100 and 49,100 | 8,178 | 3.0 |
| round2-json-dense | 65,486 and 65,486 | 8,184 | 2.0 |
| round2-quote-dense | 70,986 and 70,986 | 8,190 | 2.0 |
| round2-tail | 50,206 and 50,206 | 8,183 | 3.0 |
| round2-tail-eight-kinds | 61,132 and 61,132 | 8,189 | 3.0 |

At `cce8f1af7` the margins' substitution, the reminder search and the header's match each read the whole text, whatever its length. The test asserts at most `16 × RECORD_SCRUB_MAX` regex characters, no call wider than twice the bound, regex reads no greater at 16 MiB than at 1 MiB (plus the bound), and at most 12 passes of `str` scans. **On `cce8f1af7`'s source** it fails for every shape, already at its first size, 1 MiB (hold 4, below: it read 2.1 to 4.2 million characters by regex against the 262,144 allowed, the reminder search and the header's match each reading the whole text, and on the margin shapes the substitution too).

**Measured** (`tools/measure_cut_cost.py`, best of five CPU times, `time.process_time`, Python 3.14.7 with the GIL, one hold, load about 14 to 17 on the host's 18 cores):

| shape | MiB | cut ms | cut / plain | cce8f1af7 cut ms | handler, message ms | handler, traceback ms | Python's formatting ms | traceback / plain |
|---|---|---|---|---|---|---|---|---|
| plain | 1 | 3.6 | 1.00 | 3.2 | 24.0 | 26.2 | 3.1 | 1.00 |
| margin-only lines `  \|\n` | 1 | 11.7 | 3.30 | 77.9 | 25.8 | 43.5 | 17.5 | 1.66 |
| margin-only lines `  \| \n` | 1 | 14.0 | 3.94 | 74.2 | 24.1 | 26.8 | 9.1 | 1.02 |
| member lines `  \| member line\n` | 1 | 4.7 | 1.32 | 14.0 | 33.4 | 26.0 | 5.3 | 0.99 |
| leading spaces | 1 | 1.7 | 0.49 | 34.0 | 1.6 | 1.8 | 0.7 | 0.07 |
| run of `<` | 1 | 2.2 | 0.63 | 23.7 | 8.1 | 11.5 | 4.2 | 0.44 |
| raised exception group (its traceback) | 1 | 3.7 | 1.04 | 22.0 | nan | 24.7 | 5.6 | 0.94 |
| plain | 4 | 8.6 | 1.00 | 10.3 | 17.6 | 28.7 | 11.5 | 1.00 |
| margin-only lines `  \|\n` | 4 | 44.2 | 5.14 | 315.3 | 57.0 | 127.2 | 48.5 | 4.43 |
| margin-only lines `  \| \n` | 4 | 45.1 | 5.24 | 270.8 | 49.3 | 124.3 | 41.1 | 4.33 |
| member lines `  \| member line\n` | 4 | 8.7 | 1.01 | 73.5 | 29.3 | 42.8 | 12.4 | 1.49 |
| leading spaces | 4 | 7.9 | 0.92 | 115.2 | 6.6 | 12.8 | 2.6 | 0.44 |
| run of `<` | 4 | 4.7 | 0.55 | 93.1 | 10.3 | 28.8 | 22.5 | 1.00 |
| raised exception group (its traceback) | 4 | 13.9 | 1.62 | 54.8 | nan | 51.0 | 15.5 | 1.78 |
| plain | 16 | 29.4 | 1.00 |  | 49.9 | 136.0 | 59.4 | 1.00 |
| margin-only lines `  \|\n` | 16 | 185.7 | 6.33 |  | 237.8 | 519.4 | 278.8 | 3.82 |
| margin-only lines `  \| \n` | 16 | 173.3 | 5.91 |  | 188.2 | 459.5 | 237.0 | 3.38 |
| member lines `  \| member line\n` | 16 | 49.1 | 1.67 |  | 51.6 | 141.1 | 77.3 | 1.04 |
| leading spaces | 16 | 38.1 | 1.30 |  | 24.1 | 27.2 | 8.2 | 0.20 |
| run of `<` | 16 | 12.6 | 0.43 |  | 20.3 | 98.0 | 74.6 | 0.72 |
| raised exception group (its traceback) | 16 | 34.0 | 1.16 |  | nan | 126.0 | 75.8 | 0.93 |

- **Linear on every shape, within the noise.** From 4 to 16 MiB the cuts grow 2.4 to 5.6 times (plain text 3.4). The largest step is the member lines' (8.7 to 49.1 ms). Reading the code for that shape, what it reads past the windows is the two `r>` scans, one `rfind` for a comma, the looks' copies and the 16 MiB `read` that blanking a window builds; the counting test shows 4.0 passes of the text at both sizes. So I read the step as the cost of touching a fresh 16 MiB copy, not as a pass that grows faster than the text.
- **Against plain text of the same size** (`cut / plain`): member lines 1.01 to 1.67, leading spaces 0.49 to 1.30, the run of `<` 0.43 to 0.63, the raised exception group 1.04 to 1.62. Against round 4's cut: the margin-only lines 77.9 → 11.7 ms at 1 MiB and 315.3 → 44.2 ms at 4 MiB (review r3: 1,594 ms at 16 MiB, now 185.7), leading spaces 115.2 → 7.9 ms, the run of `<` 93.1 → 4.7 ms, the group 54.8 → 13.9 ms at 4 MiB.
- **The one shape still well above plain text** is a run of lines holding only whitespace and margins: 3.3, 5.1 and 6.3 times plain text at 1, 4 and 16 MiB (11.7, 44.2, 185.7 ms), linear. Past a window, each of those lines' marks must be told from a `|` or `+` that is not one, and that takes about a dozen C scans over the run (`_margin_lines`), against plain text's two or three. C-25.7 states it as an accepted cost.
- **The round-2 cost shapes** at 4 MiB, in hold 5 (load about 11 to 16): every cut 0.59 to 1.00 times plain text; for the blank lines after a key as a traceback, Python's formatting is 205.4 ms of the handler's 210.7.

| shape | MiB | cut ms | cut / plain | cce8f1af7 cut ms | handler, message ms | handler, traceback ms | Python's formatting ms | traceback / plain |
|---|---|---|---|---|---|---|---|---|
| plain | 4 | 12.6 | 1.00 | 9.1 | 19.1 | 36.1 | 17.7 | 1.00 |
| margin-only lines `  \|\n` | 4 | 49.3 | 3.90 | 312.0 | 58.8 | 148.2 | 68.3 | 4.11 |
| margin-only lines `  \| \n` | 4 | 39.8 | 3.15 | 303.7 | 55.3 | 113.3 | 50.6 | 3.14 |
| member lines `  \| member line\n` | 4 | 8.5 | 0.67 | 64.7 | 27.7 | 53.6 | 14.6 | 1.49 |
| leading spaces | 4 | 6.0 | 0.47 | 108.2 | 6.1 | 6.8 | 2.5 | 0.19 |
| run of `<` | 4 | 4.1 | 0.33 | 62.8 | 9.1 | 25.7 | 13.5 | 0.71 |
| round 2: head | 4 | 7.5 | 0.59 | 8.6 | 14.7 | 30.3 | 12.9 | 0.84 |
| round 2: tail | 4 | 7.5 | 0.59 | 9.2 | 13.6 | 29.6 | 16.0 | 0.82 |
| round 2: tail-eight-kinds | 4 | 9.0 | 0.72 | 10.9 | 14.8 | 32.4 | 12.6 | 0.90 |
| round 2: quote-dense | 4 | 12.6 | 1.00 | 10.6 | 14.4 | 101.5 | 85.6 | 2.82 |
| round 2: json-dense | 4 | 7.9 | 0.62 | 6.4 | 16.7 | 27.2 | 10.4 | 0.75 |
| round 2: blank-lines-after-a-key | 4 | 11.8 | 0.94 | 13.5 | 19.4 | 210.7 | 205.4 | 5.84 |
| round 2: blank-lines-after-bearer | 4 | 10.3 | 0.81 | 20.3 | 11.0 | 102.9 | 74.0 | 2.85 |
| round 2: blank-lines-after-a-flag | 4 | 12.5 | 0.99 | 9.3 | 13.9 | 143.0 | 130.1 | 3.97 |
| raised exception group (its traceback) | 4 | 7.1 | 0.56 | 66.1 | nan | 58.6 | 16.9 | 1.62 |

- **The handler.** As a traceback, a record costs what Python's own formatting costs plus 19 to 241 ms at 16 MiB (plain text: 77); the group's 6.0 MB traceback in review r3 (234 to 317 ms there) is here 126 ms at 16 MiB of member text, 76 of it Python's formatting.

**The differential.** `tests/reference_cut_cce8f1af7.py` is `cut_whole` as of `cce8f1af7`, with every function and constant it reaches, copied verbatim. The windowed cut's output equals it byte for byte on:

- review r3's shapes and the round-2 cost shapes, at 4 KiB, 64 KiB and 1 MiB, at limits 400 and 16,384 (96 cases);
- the rebuilt generator (`fuzz_text`) and the same with #231's 12 header shapes inserted (`header_fuzz_text`: the value before the key or not, the key bare, quoted or escape-quoted, its separator ending the key's line or starting the next, with either colon), 300 texts each in CI, three seeds;
- the group generator (`MEMBERS`, `distinct_member`, `group_of`, the group's own 1,200 lines when `over`), as a Hypothesis property at limits 200 to 16,384;
- a Hypothesis property built to differ: runs of up to 600 margin-only lines at depths one to three, near misses among them (an odd run, a tab, a second bar, a carriage return, a wide space), the cut rules' pieces with and without margins, leading whitespace and margins, reminder-like prefixes in each case the regex reads (`<sYstem-reminder>`, `ſ`, `ı`, `İ`, unterminated), CRLF, at limits 40 to 600 that put the windows' edges inside the runs.
- And everywhere else the round's tests call `cut_whole`: the handler and sites tests' cuts are now the windowed cut's, and they pass (holds 1 and 2).

**No input differs.** Besides the tests, direct runs outside pytest while building found no difference either: 3,000 `fuzz_text` texts, 6,000 texts of long margin runs and near misses at limits 40, 120 and 400, and 600,000 random small texts for the three C-scan predicates. I found no input that could not be made equal, so there is no list of differing inputs: every look past a window computes exactly what the reference's fully blanked text gives. `test_the_reading_s_looks_are_the_blanked_text_s` checks `before`, `after`, the leading header and the reminder test against `_last_visible`, `_run_end` and the two regexes on the fully blanked text, for any windows; `test_the_margin_scans_read_what_the_margin_regex_blanks` checks `_is_margin_mark`, `_margin_lines` and `_body_between` against the substitution, 2,000 examples.

### 2. The clock assertions (P3-2)

- **`TIME_BOUND_S`** (`test_daemon_log_handler.py`) is 1 s, the generous guard, at both its assertions: `test_a_four_mib_record_is_written_bounded_and_scrubbed_within_the_time_bound` and `test_a_short_line_waits_for_a_four_mib_record_no_longer_than_the_bound`. The counting tests carry the bound.
- **The sweep.** Those were the only wall-clock assertions under 1 s this PR adds or changes. The others are `test_a_four_mib_record_of_a_cost_shape_is_written_within_a_second` (1 s) and `test_a_log_tail_is_its_file_scrubbed_whole_then_cut_to_its_lines` (10 s); the rest are timeouts.
- **One more, found in hold 1.** `test_a_four_mib_record_of_a_cost_shape_is_written_within_a_second[blank-lines-after-a-key-traceback]` took 1.004 s at load about 55. That case is almost all Python's own formatting of a traceback of four million lines. It, and this round's guard for review r3's shapes, now take the better of two CPU times less Python's own `formatException` of the same traceback (`handler_cpu_beyond_formatting`), under 1 s.

### 3. `daemon start`'s tail keeps its lines (P3-3)

`daemon_log.scrubbed_tail(text, count)` is what a person reads of daemon.log. It cuts the file's text as the handler cuts a record (to `RECORD_SCRUB_MAX`, or for more lines than its tail holds to twice what they hold and the bound more), scrubs it in the block form with its lines kept, and keeps its last `count` lines. **Lines kept:** `scrubbed_block(..., keep_lines=True)` scrubs whole, folds and rescrubs as before; where folding finds more, it writes the block's lines with each whitespace-free run only where the folded rescrub shows it, else `[REDACTED]` (`_kept_block`), as `_group_block` does for margins, and a group's margin-free reading is kept the same way. So the tail is the file's last lines, and a value the folded rescrub hides stays hidden where it stands. On review r3's 60-line log, `_log_tail` gives its last 20 lines; with the flag among them, `[REDACTED] --lane codex-0` in place of the value's line. C-25.7's "that is the one folded line" is replaced.

### 4. `daemon logs` is scrubbed (the reviewer's note)

- **Without `--follow`:** the range is `scrubbed_tail(text, N)`, exactly as `daemon start`'s tail. No flag prints it raw; the file is there to read.
- **With `--follow`:** the file is read once and followed from there, so no line falls between the read and the follow. `daemon_log.LogFollower` holds whole lines until a break after them is settled (`handoff.settled_end`): no match of the whole-text scrub could cross it, whatever is written next. That is `cut_whole`'s rules with nothing known past the text and no window: a break whose next visible character is not written yet is judged for `:` and for a token character (the two kinds `_neutral_break` and `_folds_to_value` read), so a line that ends in a credential flag, a key, its separator or `Bearer` waits; a value a quote may open stays open until its kind's next quote; a private key until its last line; a data URI until a character its body cannot hold; and nothing after a reminder tag settles. Settled lines are written by `scrubbed_block(..., keep_lines=True)`, each run only where the same lines scrubbed after up to 8,192 characters of what was already written (`FOLLOW_CONTEXT`) show it too; a margin's mark alone is kept. The file's own unsettled end, as following starts, is held the same way. ^C writes what is held.
- **The residual limits** (C-25.7): held text past 16,384 characters (`FOLLOW_HOLD_MAX`) is written as it is, so the part of a quoted value, private key or data URI longer than that, or of text after a reminder tag, written before its end may show what its end would hide (`test_held_text_past_the_bound_is_written_as_it_is_documented_limitation`); a match that starts more than 8,192 characters before the lines being written is not seen; and a held line is written only when more is written or following stops.
- **What the follower reads as following starts:** the file's last `FOLLOW_CONTEXT + FOLLOW_HOLD_MAX` characters (24 KiB), for the settled break.
- **Cost:** scrubbing a range costs what the whole-text scrub of that much text costs, about 1.0 to 1.2 s a MiB (best of three CPU times, one hold):

| range MiB | lines | `daemon logs` ms | ms per MiB |
|---|---|---|---|
| 1 | 13,443 | 1192 | 1192 |
| 4 | 53,773 | 4003 | 1001 |
| 16 | 215,092 | 17914 | 1120 |

  The default 40 lines: at about 80 characters a line the file is cut to about 22 KiB (twice the range and the bound more), which at that rate is about 25 ms of scrubbing, plus the cut's C scans of the file.

### 5. The rule table (the reviewer's gap)

`_HEADER_RE` had one mid-text `Cookie:` example. `HEADER_EXAMPLES` adds #231's shape (`Cookie: a=…; session_token=` with its value on the next line, which the named-value rules take whole, folded or not), and `LEADING_HEADER_EXAMPLE` a header opening the text, whose next line only folding joins to it. The rule-table test checks that the mid-text examples do not fold, and that the leading one folds there and only there (`cut_whole`'s leading-header rule). The per-rule cut test runs all three at the head's end, the tail's start and the text's start, after every gap.

### Invariants, round 5

Invariant 10 is restated in place above. Each holds for every input and is executed by the tests named.

18. **The windowed cut is the cut as of `cce8f1af7`.** For any text and limit, `cut_whole`'s output equals the reference's. Tests: the four `test_the_windowed_cut_is_the_reference_cut_*` tests.
19. **The reading's looks are the blanked text's.** For any text and windows, `before`, `after`, the leading-header test and the reminder test give what `_last_visible`, `_run_end` and their regexes give on the text with every margin blanked; the C scans agree with `_MARGIN_MARK_RE` and the body class. Tests: `test_the_reading_s_looks_are_the_blanked_text_s`, `test_the_margin_scans_read_what_the_margin_regex_blanks`, `test_the_reminder_folds_are_every_character_the_tag_regex_takes_for_a_letter`.
20. **What a person reads of daemon.log hides what the handler's forms hide.** For any log, the tail; and, within the follower's stated limits (no text held past 16,384 characters, no match reaching back more than 8,192), what `--follow` writes, in any pieces, show no secret that the whole log's block with its lines kept hides; for a log with no credential-shaped record they are its raw lines. Tests: the four properties in `test_daemon_log_tail.py`.

### Tests per file, round 5

New or changed:

- **`tests/unit/test_daemon_log_cut_windows.py`, 137 cases, new:**
  - the counting test at 1 and 16 MiB, 16 shapes;
  - the CPU guard for review r3's shapes at 4 MiB, message and traceback, 14 cases;
  - the differential on the shapes at 4 KiB, 64 KiB and 1 MiB and two limits, 96 cases; on the two fuzz corpora, 3 seeds each; on the group generator; and the property where margins cross the windows;
  - the C scans against the margin regex and the body class; the reading's looks against the blanked text; the reminder folds over every code point.
- **`tests/unit/test_daemon_log_tail.py`, 16 cases, new:** review r3's 60-line tail and the same with the flag among the last 20 lines; `daemon logs` over an older daemon's raw lines; `daemon logs -f` over pieces written one at a time; the follower's six cases (a flag held until its value, a key written past the bound, a value open as following starts, ^C, a private key, a reminder and a data URI held until they end) and the documented limitation past the bound; four properties (the tail and following, never showing a secret the folded rescrub hides, and raw lines when nothing is credential-shaped).
- **`tests/unit/test_daemon_log_handler.py`, 127 cases** (round 4: 125): `TIME_BOUND_S` 1 s; the round-3 cost guard in CPU beyond Python's formatting (`handler_cpu_beyond_formatting`); the rule table's `_HEADER_RE` examples, two more cases.
- **`tests/unit/test_daemon_log_sites.py`, 82 cases:** the tail through `daemon start` and `_log_tail` keep their lines (six cases' expectations).
- **`tests/reference_cut_cce8f1af7.py`:** the reference cut.
- **`tools/measure_cut_cost.py`** and **`tools/daemon_log_cut_mutations.py`:** the measurement and the mutation sweep.

### Runs, round 5

All runs used a fresh `.venv` (`uv sync --locked --group dev`, Python 3.14.7 with the GIL), each in its own hold of the shared lock, in the foreground (the Bash tool moved each call to the background at its 600 s limit; I waited on it in the foreground with a bounded poll and started nothing else), as the venv's `python`, with no xdist, `PYTHONPATH` unset and `HYPOTHESIS_PROFILE=ci`. Each pytest ran in-process with a 19-minute alarm and JUnit XML for per-file counts. Lock waits were 20 to 45 minutes at load 30 to 110.

| Hold | What | Result |
|---|---|---|
| m0 | the cost measurement, all sizes with the reference at 16 MiB too, at load 70 to 112 | stopped by me after 10 minutes: it had had 1 min 41 s of CPU, and could not have finished in the hold's 20. No table |
| 1 | brief group 1 and the two new files, at `b219280b7` with `779e9ff13`'s change in the tree | 822 passed, 2 failed, 273 s: the round-3 cost guard at 1.004 s (§2), and a follower test whose premise `779e9ff13` changed. Both fixed in `21fe7cc82` |
| 2 | brief groups 1 to 4 and the two new files, one pytest, at `21fe7cc82` (the code as committed since) | **1,477 passed**, 315 s: `test_daemon_log_handler` 127, `test_daemon_log_sites` 82, `test_daemon_log_scrubbing` 86, `test_round10_refusal_context` 59, `test_handler_refusals` 317, `test_daemon_log_cut_windows` 137, `test_daemon_log_tail` 16, `test_scrub_key_order` 70, `test_sessions_handoff` 117, `test_sessions_handoff_excerpts` 153, `test_cli` 222, `test_daemon_verbs` 20, `test_offline` 33, `tests/fake/test_state_contract` 37, `test_test_module_names` 1 |
| 3 | `tools/measure_cut_cost.py` at 1, 4 and 16 MiB (the reference at 1 and 4), then `--logs` at 1, 4 and 16 MiB, load 14 to 17 | the tables in §1 and §4 |
| 4 | the counting test and `test_daemon_log_tail.py` on `cce8f1af7`'s source (`git archive`, the new test files copied in, run there with this venv) | 32 failed of 32. The counting test, every shape, on the bound already at 1 MiB (§1); the four named tail and `daemon logs` regressions on their own assertions (one line instead of 20, `SECRET7` printed raw); the other 12 on functions `cce8f1af7` lacks |
| 5 | `tools/daemon_log_cut_mutations.py`, then the measurement of the round-2 shapes at 4 MiB | below, and §1 |
| 6 | the other files that import what this round changed, and `test_invariants_index` and `test_desktop_ledger` (the contract changed): 15 files, queued at 07:04, the last hold started | **804 passed**, 135 s: `test_protocol_secrecy` 322, `test_sessions_cli` 142, `test_sessions_transcripts` 71, `test_outcome_unknown` 58, `test_suggestions_r1b_adversarial` 30, `test_conversation_handoff_briefs` 28, `test_store_ids` 27, `test_invariants_index` 25, `test_lockwatch` 20, `test_conversation_redact` 9, `test_suggestion_secret_properties` 7, `test_desktop_ledger` 4; `tests/fake/test_sessions_end_to_end` 33, `test_reset_credit_incident` 19, `test_daemon_launch` 9 |

### Mutations, round 5

`tools/daemon_log_cut_mutations.py`, in hold 5: each fix put back in-process, the named tests called with fixtures the tool builds, the baseline as control.

| Mutation | Named tests failed |
|---|---|
| baseline | none of 8 |
| margins blanked over the whole text (`whole-margins`) | the counting test on all four shapes it runs (margin-only lines, the group, leading spaces, the run of `<`): the substitution read 1,048,576 characters at 1 MiB |
| the leading header by round 4's backtracking regex (`backtracking-header`) | the counting test on margin-only lines and leading spaces (the regex read the whole text). On the group and the run of `<` the mutated match never runs: the cut asks for a leading header only when the head keeps nothing visible (`reading.after(0) >= head`), and both texts begin with a visible character |
| the reminder by its regex over the whole text (`whole-reminder-search`) | the counting test on all four shapes |
| the tail folded to one line (`folded-tail`) | `test_daemon_start_s_tail_keeps_its_last_twenty_lines`, `test_a_flag_s_value_in_the_tail_stays_hidden_with_the_lines_kept`, `test_daemon_logs_prints_an_older_daemon_s_raw_lines_scrubbed` |
| `daemon logs` printing daemon.log raw (`raw-logs`) | `test_daemon_logs_prints_an_older_daemon_s_raw_lines_scrubbed`, `test_daemon_logs_follow_prints_new_lines_scrubbed`, each on `SECRET7` |

"Drop the possessive quantifier" (the brief's wording) is `backtracking-header` here: the fix is not a possessive `\\s*+` but a bounded name match at the first visible character with a look past it, so putting the old regex back is the mutation.

### Not run, round 5

- **The whole suites.** Of `tests/unit`'s 329 files, 26 ran (2,183 cases, all passed: holds 2 and 6); of `tests/fake`'s 106, 4 ran (98 cases, all passed). The other 303 and 102 files did not run. Lock waits ran 20 to 45 minutes a hold at load 30 to 110, and this job's wall left no room for the five or six 20-minute holds the two directories need; round 4 ran them whole, at `a82bbcfd6` and `d44dc5600`. What did run is every briefed file, and every other file of the two directories that imports what this round changed (`sessions.handoff`, `daemon_log`, the scrubber) or reads the contract. The scrubber's own functions did not change; `_neutral_break` gained an optional parameter whose default is the old behaviour; `cli.py` changed only `_log_tail` and `cmd_daemon_logs`.
- `tests/e2e`, `tests/frontend` and `tests/process`;
- Python 3.12;
- CI, since this job has no network.

### Where this round departed from the brief

- **One timing probe outside the lock.** Before the first hold, a 5-second script timed the C primitives the design needed (`str.lower`, `bytes.translate`, `str.replace` with many matches) outside the lock, at load about 4. Every measurement reported here ran under a hold.
- **The first measurement hold was stopped** (m0 above) when it could not finish within 20 minutes; it was rerun smaller (the reference at 1 and 4 MiB only; review r3 measured it at 16 MiB) in hold 3.
- **Hold 2 merged the brief's four groups into one pytest**, with many waiters on the lock; hold 5 ran the mutation sweep and a measurement one after the other, neither a pytest.
- **Holds ran the venv's `python`**, not `uv run`, so a queued hold could not end on uv's cache lock.
- **The whole suites did not run** (§ Not run).

### Commits, round 5

- `5824f8d87` Merge release/217 (#235 #236 #234 #226) into fix/daemon-log-scrub-217
- `cc7f45cdb` test: the cut reads margins only where it looks, the tail keeps its lines, and daemon logs is scrubbed (#233 review r3)
- `7beb6392d` fix: the daemon.log cut reads a group's margins only in its windows and where a look reaches (#233 review r3, P3-1)
- `b219280b7` fix: daemon start's tail keeps its lines, and daemon logs is scrubbed, with and without --follow (#233 review r3)
- `779e9ff13` fix: daemon logs --follow also holds the file's unsettled end as it starts, and keeps a margin's mark
- `21fe7cc82` test: the 4 MiB clock guards count CPU beyond Python's own formatting; the follower's start and context cases
- `9d799c252` docs: C-25.7 states the cut's cost as a measured ratio to plain text, the tail's kept lines, and daemon logs scrubbed (#233 review r3)
- `68b11ebfe` tools: put round 5's fixes back one at a time and see which named test fails (#233 review r3)
- this report's round 5

The commits are on this worktree's detached HEAD, as in round 4: Subfleet keeps them under `refs/subfleet-salvage/`. Nothing is pushed.


## Round 6: review r4 of #233

Review r4 (`REQUEST_CHANGES`) found no leak: secrecy held on every sweep, and the windowed cut equalled `cce8f1af7`'s on 30,615 fuzz texts and 17,324 window-edge texts. It asked for three P3s: two of C-25.7's cost bounds were false for some inputs, a reminder tag in daemon.log blanked the tail, and the count test missed the fourth pass. The hub took the reviewer's note on `--follow` too. This round starts from `1cf653dff`. The fix-6 job stopped at its six-hour wall (two host freezes took about 1 h 45 m of it) with four commits and uncommitted docs; a continuation job applied that work whole, filled in what it left to measure, and ran the tests (§ Runs, round 6). `origin/release/217` had moved past `473fe2941`, which round 5 merged, by one merge, #237 (`a26d08336`, C-16.7's connection tests: `tests/unit/test_daemon_connections.py` only). This branch does not touch that file, so it merged without conflict (`dfdff9eb8`); the contract was not part of it.

The code past the cut's windows did not change this round, as the hub decided. Its only changes in `subfleet/sessions/handoff.py` are new functions (`reminder_tags`, `quoted_value_mask`), `settled_end`'s new hold (§4) and docstrings.

### 1. The cut's stated bounds (P3-1)

**What was wrong.**
- **Passes.** `_event_from` and `_event_before` each scan until they find their needle, so each quote kind open at a candidate cut can cost a pass over the text, up to ten kinds a side. C-25.7 said "at most 12 passes" in all; the reviewer's shape read 19.9 to 20.0. The cost stays linear.
- **Data URIs.** The meter counted each `_DATA_BODY_RE.match` to the window's end, so 480 prefixes read 2,012,537 regex characters against `REGEX_BOUND`'s 262,144, though the cut takes milliseconds.
- **Three sentences.** Python's formatting "costs more than the cut" was false for leading spaces and long and short margin lines. The `cce8f1af7` baseline mixed review r3's median wall times with best-of-five CPU times. `daemon logs`' cost was measured on one admission line repeated.

**The statement, as the code gives it** (the same words in C-25.7, `daemon_log`'s docstring, `cut_whole`'s and invariant 10). Past its windows the cut reads only in C scans: a fixed number of scans of the text, plus at most 20 needle looks.
- The fixed scans include, where a look past a window meets a group's margins, a few scans of each run of margin-only lines, each run read once (`_Reading.spans`).
- A needle look is one for each quote kind open at a candidate cut: two plain kinds and at most eight escaped ones a side. Each is at most 64 finds of its quote, each starting past the last, so together they read the text at most once; `_event_before`'s look at the character before the quote it found reads below where its finds stopped.

**The count** (`test_at_16_mib_no_regex_reads_past_the_cut_s_windows`). The meter now keeps the needle looks apart, each its own count, and counts what a look reads of a run of margin lines (`_Reading._back`, `_forward`) among the fixed scans. A pattern that is one character class repeated (`[A-Za-z0-9+/=\s]+`, `\s*`) is metered on `match` by what it reads: its match and the character after it, within `endpos`. The test asserts, at 1 and 16 MiB:
- at most `16 × RECORD_SCRUB_MAX` regex characters, none more than twice the bound in one call, and no more at 16 MiB than at 1 MiB (as before);
- at most 20 needle looks, each at most one pass and `2 × RECORD_SCRUB_MAX` characters (`LOOK_SLACK`: its finds' overlaps, the backslash runs it counts and its look's steps);
- the rest at most 8 passes (`FIXED_PASSES`). That number is the guard on these shapes, which read 2 to 6. C-25.7 states only that the number is fixed: reading the code, the scans past the windows outside the looks are a fixed list (the reminder check, the reading's copy, the looks from each window's edge, the tail's comma and body check, the leading header's look), each at most a pass or two of copies, and each run of margin lines read once.

Review r4's three shapes join the 16 (`REVIEW_R4_SHAPES`): the quote kinds; a data URI body from the head into the tail's window (P3-3); and 480 prefixes. Metered at 1 MiB (16 MiB gives the same regex counts, which the test asserts):

| Shape | Regex characters | Widest call | Needle looks | Widest look, passes | Fixed passes | Of them, margin runs | All passes |
|---|---|---|---|---|---|---|---|
| data-uri-body | 57,308 | 8,186 | 0 | 0.00 | 4.05 | 0.00 | 4.05 |
| data-uri-prefixes | 51,990 | 8,177 | 0 | 0.00 | 4.03 | 0.00 | 4.03 |
| exception-group | 65,465 | 8,194 | 0 | 0.00 | 4.04 | 0.00 | 4.04 |
| leading-spaces | 10 | 2 | 0 | 0.00 | 6.03 | 0.00 | 6.03 |
| margin-only-lines | 65,538 | 8,194 | 0 | 0.00 | 5.09 | 1.00 | 5.09 |
| margin-only-lines-spaced | 65,524 | 8,194 | 0 | 0.00 | 5.09 | 1.00 | 5.09 |
| member-lines | 65,582 | 8,194 | 0 | 0.00 | 4.06 | 0.00 | 4.06 |
| plain | 49,106 | 8,177 | 0 | 0.00 | 3.07 | 0.00 | 3.07 |
| quote-kinds | 49,241 | 8,190 | 18 | 0.99 | 3.07 | 0.00 | 20.93 |
| round2-blank-lines-after-a-flag | 49,279 | 8,191 | 0 | 0.00 | 5.04 | 0.00 | 5.04 |
| round2-blank-lines-after-a-key | 49,244 | 8,192 | 0 | 0.00 | 6.04 | 0.00 | 6.04 |
| round2-blank-lines-after-bearer | 49,255 | 8,192 | 0 | 0.00 | 5.04 | 0.00 | 5.04 |
| round2-head | 49,100 | 8,178 | 0 | 0.00 | 3.05 | 0.00 | 3.05 |
| round2-head-single-quotes | 49,100 | 8,178 | 0 | 0.00 | 3.05 | 0.00 | 3.05 |
| round2-json-dense | 65,486 | 8,184 | 3 | 0.00 | 2.32 | 0.00 | 2.32 |
| round2-quote-dense | 70,986 | 8,190 | 2 | 0.00 | 2.76 | 0.00 | 2.76 |
| round2-tail | 50,206 | 8,183 | 0 | 0.00 | 3.06 | 0.00 | 3.06 |
| round2-tail-eight-kinds | 61,132 | 8,189 | 10 | 0.00 | 3.13 | 0.00 | 3.14 |
| run-of-lt | 53,258 | 8,192 | 0 | 0.00 | 2.08 | 0.00 | 2.08 |

- **The quote kinds** read 20.9 passes, 18 of them its needle looks (nine kinds of `'` open in the head, nine of `"` in the tail), each 0.99 of a pass. Review r4 read 19.9 on its own version of the shape.
- **The 480 prefixes** read 51,990 regex characters, against the 2,012,537 the round-5 meter counted.
- `test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads` holds both: the 18 looks, each at least half a pass, and at least 480 body matches metered, together within twice the bound. A meter that stopped seeing `_DATA_BODY_RE` fails it (mutation `unmetered-data-body`, below).

**Measured** (`tools/measure_cut_cost.py`, hold M: best of five CPU times, `time.process_time`, Python 3.14.7 with the GIL; load 20 to 27). The cut as of `cce8f1af7` is measured the same way at all three sizes, so the baseline is now one measure:

| Shape | cut ms, 1 / 4 / 16 MiB | cut / plain, 1 / 4 / 16 MiB | cce8f1af7 cut ms, 1 / 4 / 16 MiB | Python's formatting ms, 1 / 4 / 16 MiB |
|---|---|---|---|---|
| plain | 3.6 / 8.7 / 35.7 | 1.00 / 1.00 / 1.00 | 3.7 / 11.2 / 41.2 | 4.5 / 14.7 / 76.9 |
| margin-only lines `  \|\n` | 14.6 / 48.1 / 240.5 | 4.02 / 5.55 / **6.73** | 79.8 / 296.3 / 1,266.0 | 17.0 / 73.3 / 249.4 |
| margin-only lines `  \| \n` | 16.2 / 43.5 / 182.7 | 4.47 / 5.02 / 5.12 | 71.3 / 290.9 / 1,164.7 | 13.3 / 58.7 / 265.0 |
| margin-only lines, long and short | 5.7 / 22.1 / 107.6 | 1.58 / 2.55 / 3.01 | 33.0 / 153.0 / 630.9 | 2.5 / 7.5 / 40.0 |
| quote kinds (review r4) | 7.8 / 22.5 / 97.7 | 2.16 / 2.60 / 2.74 | 8.3 / 27.8 / 112.5 | 4.0 / 20.0 / 83.9 |
| data URI body into the tail's window | 4.2 / 7.9 / 35.9 | 1.17 / 0.91 / 1.00 | 6.4 / 17.9 / 94.0 | 6.7 / 28.6 / 160.6 |
| 480 data URI prefixes | 3.4 / 11.1 / 57.7 | 0.94 / 1.28 / 1.61 | 6.5 / 31.5 / 130.4 | 5.0 / 14.8 / 96.8 |
| member lines `  \| member line\n` | 2.8 / 8.7 / 40.9 | 0.78 / 1.00 / 1.14 | 15.8 / 64.2 / 352.2 | 3.4 / 23.4 / 93.1 |
| leading spaces | 1.6 / 8.8 / 34.9 | 0.43 / 1.02 / 0.98 | 27.9 / 123.6 / 468.4 | 0.8 / 2.7 / 10.8 |
| run of `<` | 1.4 / 6.8 / 16.5 | 0.39 / 0.78 / 0.46 | 19.1 / 92.0 / 315.5 | 3.1 / 20.8 / 69.3 |
| raised exception group (its traceback) | 2.6 / 8.0 / 22.5 | 0.71 / 0.92 / 0.63 | 15.3 / 64.5 / 303.9 | 3.5 / 17.9 / 75.2 |

- **The worst ratio is 6.73**, margin-only lines at 16 MiB (240.5 ms), the accepted cost; round 5 measured 6.3 and review r4 6.20 on the same shape at 16 MiB, at other loads. Besides the two short margin-only shapes (4.02 to 6.73), every shape is at most 3.01 times plain text: review r4's quote kinds 2.16 to 2.74, its data URI shapes 0.91 to 1.61.
- **The baseline is now one measure.** The cut as of `cce8f1af7` is the best of five CPU times too, at all three sizes, and its output equalled this tree's on every row (the tool asserts it). It costs 1.0 to 1.3 times this cut on plain text and the quote kinds, 1.5 to 2.8 times on the data URI shapes, and 4.4 to 19 times on the margin, member-line, leading-space, `<` and group shapes, which round 5's windows made cheaper.
- **Python's own formatting** (`formatException` of a `RuntimeError` whose message is the text) costs 0.3 to 4.5 times the cut. It costs less than the cut on leading spaces (10.8 against 34.9 ms at 16 MiB), long and short margin lines (40.0 against 107.6) and the quote kinds (83.9 against 97.7) at every size, and on the spaced margin-only lines at 1 MiB (13.3 against 16.2); more on every other row. Round 5's sentence that it "costs more than the cut" is replaced in C-25.7 by this.
- Load during the hold was 20 to 27 on 18 CPUs; the 1 and 4 MiB rows took 33 s, the 16 MiB rows 80 s, all of them inside the budget.

**`daemon logs`, on a realistic daemon.log** (`realistic_log`: admission lines, tracebacks with quoted words, exception groups behind their margins, warnings with quotes; best of three CPU times, the same hold):

| file MiB | lines | whole range ms | ms per MiB | `-n 40` ms | `-n 40`, orphan tag on line 1, ms | `-n 40`, whole reminder on line 1, ms |
|---|---|---|---|---|---|---|
| 1 | 15,370 | 2,372 | 2,372 | 61.7 | 41.1 | 271.6 |
| 4 | 61,361 | 9,243 | 2,311 | 68.3 | 59.0 | 923.7 |
| 16 | 245,732 | 36,942 | 2,309 | 135.9 | 110.5 | 2,286.4 |

- **A range costs 2.31 to 2.37 s of CPU a MiB** on this log, against round 5's 1.0 to 1.2 s on one admission line repeated, and review r4's 2.53 to 2.59 s on its own realistic log. C-25.7 now gives this figure.
- **The default 40 lines cost 62, 68 and 136 ms** at 1, 4 and 16 MiB (review r4: 39, 65 and 99 ms), against round 5's "about 25 ms": what grows with the file is the cut's C scans of it.
- **An orphan tag on line 1** is marked before the cut and costs no more than the noise between rows (41 to 111 ms, best of three; the assertion beside it holds that its last 40 lines equal the untagged file's).
- **A whole reminder on line 1** costs the quoted-value pass over the file, twice here since the log holds a group's margins: 272, 924 and 2,286 ms, 4 to 17 times the untagged file. A log this daemon writes holds no tag (the handler marks them), so only a file an older daemon wrote pays it, and it is linear.

### 2. A reminder tag no longer blanks the tail (P3-2)

**The defect.** `scrubbed_tail` cut daemon.log with `cut_whole`, which keeps only its marker for any text holding a tag. The handler wrote orphan tags through (`turn failed: model said: <system-reminder>…`), and daemon.log is never rewritten, so one such line hid every later tail: `daemon start`'s and `daemon logs` at its default `-n 40`.

**(a) The handler.** `ScrubbingFormatter.format` returns `neutralised(...)` of the scrubbed record. The scrub strips each reminder whose two tags the record holds, as before. Every tag left, an orphan in any case or fold (`İ`, `ı`, `ſ`), is replaced by `REMINDER_MARK`, `[reminder-tag]`. `handoff.reminder_tags` finds them as `_holds_reminder_tag` finds the first: in C scans of the folded, lowered text, each confirmed by the regex. Their test checks them against `_REMINDER_TAG_RE.finditer` on 2,000 small texts of the tag's letters, the folds and both cases. The marker holds no `<` or `>`, so replacing tags cannot make one. daemon.log this daemon writes, through the handler, the process hooks and `write_scrubbed`, holds no tag.

**The marker** is one run with no whitespace, quote, `,`, `;`, `:`, `=`, `/` or `@`, and no key's ending. So every scrub rule reads it as it reads a tag, or hides more around it.
- The brief's example, `[reminder tag]`, has a space. Read before a cut, as below, `password=<system-reminder>SECRET7…` in an older daemon's raw line would have become `password=[reminder tag]SECRET7…`, whose value the plain-value rule ends at the space: `tag]SECRET7…` would show. With `[reminder-tag]` the value stays one run.
- The close tag holds a `/`, which ends a URL's user or password; the marker does not, so a URL rule may take more around it, never less.

**(b) The tail and `daemon logs`.** A file short enough not to be cut is scrubbed whole, as before, and its output neutralised. A file that must be cut is read with its reminders resolved first (`_reminders_resolved`), so the cut meets no tag:
- **No tag:** the text itself. The tail is round 5's, byte for byte.
- **No opening tag before a closing one** (orphans only, or closing tags before opening ones): no reading of the text strips anything. Masking quoted values, taking a group's margins off and folding each remove tags or keep them in order, never add or reorder one, and stripping needs an opening tag before a closing one. So each tag is replaced by the marker, which reads as the tag does.
- **Otherwise** each reminder the whole-text scrub strips is taken out, found as that scrub finds it. The scrub's first pass masks quoted values across lines, and only then strips reminders (review r4's point). So `handoff.quoted_value_mask` runs that pass, `_scrub_named_values(..., quoted=True, multiline=True)` itself, with a replacement that keeps every length: a character the text lacks, for each masked one. A tag in a masked value is no tag to the strip. `_stripped` then pairs the tags left as `_linear_sub` does (the first opening tag, the first closing one after it, and on). Taking each pair out joins what was on either side, as the strip does. Then every tag left is marked.
- **The marker rule stays** (None: the cut keeps only its marker, as at round 5) in two cases. One is when the text read without a group's margins, as the block form also reads it, would strip other reminders: a key and its `=` that a margin parted are one key there. The other is when taking reminders out makes a tag where none was (`<sys`, a reminder, `tem-reminder>`). The tests build both.

Why that hides what the whole-text scrub hides, as I read the scrub. A value runs from its opening quote to the next quote of its kind, so a value that holds a tag of a reminder the strip takes would have masked that tag: each value lies inside a stripped reminder or holds none of its tags. Outside the reminders, the first pass's openers and pairings are the same on the resolved text and on the original: a quote before a reminder whose pair lies inside it opens no value (if it did, the value would hold the reminder's opening tag), so pairing it past the reminder, as taking the reminder out does, changes no value; and a join can only add an opener, which hides more. The strip then has nothing left to take on the resolved text, where on the original it takes the same reminders out, and the later rules see the same text but for values a join opened, with the marker where a tag was. This is an argument, not a proof; the differential below executes it on the corpora and the edge generator.

**Why the record keeps its rule** (C-25.7 says so). `cut_whole` still keeps only its marker for a record over the bound that holds a tag. Its words may reach a model, through an agent reading `daemon logs`. Knowing which tags a quoted value hides needs the quoted-value pass over the whole text, and the cut's regexes read only its windows. And a record is cut once, when it is written, so the marker loses that record's words alone; the tail rereads the whole file every time.

**`--follow`.** The follower holds after a reminder tag, as before; a reminder then closes within what it holds and is stripped, or the bound passes. What it writes, and the context it checks beside, is neutralised after the scrub.

**Cost.** Without a tag the tail reads two more C scans of a file it must cut (`reminder_tags`' checks for `r>` and `R>`; where either occurs, a lowered copy and two finds). With orphans only, finding the tags and the marked copy. With a reminder to take out, the quoted-value pass over the file, twice with a group's margins: on the realistic log of §1, `-n 40` costs 272, 924 and 2,286 ms at 1, 4 and 16 MiB with a whole reminder on its first line, against 62, 68 and 136 ms with none.

### 3. The count test's fourth pass (P3-3)

The data URI body shape, `"data:image/png;base64," + "QUJDREVG\n" * n + "end.\n"`, reaches the tail's window: its body runs from the head to the end. It joins the count test, and passes: the tail's check `_body_between` reads the body in C scans. `tools/daemon_log_cut_mutations.py`'s `body-by-regex` puts round 4's `_NOT_BODY_RE.search(text, comma + 1, first)` back, and fails `test_at_16_mib_no_regex_reads_past_the_cut_s_windows[data-uri-body]`: the restored search read 1,040,364 characters in one call at 1 MiB, 1,097,672 in all, against `REGEX_BOUND`'s 262,144 (hold B). No other named test sees it, as review r4 found.

### 4. `--follow` holds a private key's split first line (the reviewer's note)

A line ending `-----BEGIN RSA` settled before `PRIVATE KEY-----` arrived, so only the 8,192-character context covered that case: review r4 saw 8,300 and 9,000 blank lines between them show the key. `settled_end` now treats a private key's first line still being written as open, like a key whose last line has not come. That is the text's last `-----BEGIN`, when `-----BEGIN` and uppercase words, with any whitespace between and after, run from it to the text's end (`_PEM_START_PREFIX_RE`, possessive, so it cannot backtrack). Only the last `-----BEGIN` can still start one: an earlier one has the last one's `-` after it, which no word of a first line holds. When a line comes that cannot continue it, the break settles. `test_a_private_key_s_first_line_is_held_until_it_can_no_longer_become_one`, `test_a_split_first_line_past_the_context_still_hides_its_key` (9,000 blank lines between) and `test_daemon_logs_follow_holds_a_split_first_line` hold it.

The reviewer's other notes are left as they are, as the hub decided: records the scrub collapses, and a first-line `Cookie:`.

### Invariants, round 6

Invariant 10 is restated in place above. Each holds for every input and is executed by the tests named.

21. **No reminder tag reaches daemon.log through the handler, or any tail or `daemon logs` output.** For any record, and for any log and count (and, following, any pieces), the output holds no match of `_REMINDER_TAG_RE`. Tests: `test_the_handler_writes_no_reminder_tag`, `test_an_orphan_tag_reaches_daemon_log_as_the_marker_through_a_real_handler`, `test_a_log_too_short_to_cut_shows_each_tag_as_the_marker`, `test_no_tag_reaches_a_tail_and_no_secret_the_whole_scrub_hides`, `test_no_tag_reaches_what_daemon_logs_follow_writes`, `test_reminder_tags_are_the_tag_regex_s_matches`.
22. **A tagged log keeps its lines, and hides what the whole-text scrub hides.** For any log the tail shows no secret that the whole log's block with its lines kept hides, a tag on any line included; a tag no longer leaves it only the marker, except in the two cases §2 names. Tests: `test_a_tag_on_the_first_line_no_longer_blanks_the_tail`, `test_daemon_logs_keeps_its_lines_after_a_tagged_first_line`, `test_a_tag_among_the_last_lines_is_its_marker_and_a_whole_reminder_is_stripped`, `test_a_tag_a_quoted_value_masks_is_none_to_the_strip`, the differential, the two properties.
23. **Without a tag, nothing changed.** For any log with no tag, the tail is round 5's byte for byte (`tests/reference_tail_1cf653dff.py`). Tests: the differential.
24. **The quoted mask is the scrub's first pass.** `quoted_value_mask` masks, character for character, the spans `_scrub_named_values`' quoted pass replaces, and keeps the rest. Test: `test_the_quoted_mask_is_the_scrub_s_first_pass`.

**The differential.** `test_the_tail_is_round_5_s_without_a_tag_and_hides_what_the_whole_scrub_hides_with_one` runs over three corpora, three seeds each, each text's limit as the tail's bound (`RECORD_SCRUB_MAX` set to it) and a count of 1 to 6: `fuzz_text`, the same with #231's header shapes, and the same with tags among its lines. `test_the_tail_on_review_r4_s_window_edges` runs over an edge generator built after review r4's description (its code was not kept). That generator has margin runs at depths one to three with `+` marks, Unicode whitespace and near misses, tables and values holding `|` or `+`, tags in each fold and near misses, whole reminders, leading headers after Unicode whitespace, data URI bodies, quoted values, flags and split private keys, and CRLF. On a text with no tag the tail equals round 5's; on a tagged one it shows no tag and no secret the whole text's scrub hides. What the resolution did on those texts (hold M, outside pytest, the tests' own seeds and draws), for the tagged texts long enough to be cut: in `fuzz_text`, 31 of its 300 texts took reminders out (2 more were tagged but short enough to be scrubbed whole); with #231's headers, 32 (and 1); with tags among the lines, 120 took reminders out and 93 marked orphans (64 whole, 23 drew no tag); in the edge generator, 191 and 51 (26 whole, 32 no tag). None fell back to the marker rule, so the fallbacks are held by their two built tests only.

### Tests per file, round 6

New or changed:
- **`tests/unit/test_daemon_log_reminders.py`, 101 cases, new:**
  - the first-line regressions, 10 tag forms at `-n` 5, 14 and 40 (30), and through `daemon logs` (15);
  - a tag among the last lines and through `daemon start`'s tail (10);
  - a log too short to cut, each form among its lines, through the tail and `daemon logs -n 40` (10; added by the continuation, so that the marking after the scrub has a test of its own: in the other tail tests the log is resolved before its cut and nothing is left to mark);
  - the handler, each form (10), and through a file;
  - the follower's split first line (three);
  - the masked tags (four) and the two fallbacks;
  - the C scans against the regex, and the quoted mask;
  - the differential (9 and 3) and the two properties.
- **`tests/unit/test_daemon_log_cut_windows.py`, 159 cases** (round 5: 137): review r4's three shapes in the count test and the shape differential (3 and 18 more), and the meter's test.
- **`tests/reference_tail_1cf653dff.py`:** round 5's tail.
- **`tools/daemon_log_cut_mutations.py`, `tools/measure_cut_cost.py`:** below and §1. The continuation added two mutants, `tail-unmarked-after-scrub` and `follow-not-neutralised`, so that each place that marks tags has its own; and `measure_cut_cost.py` keeps the rows it measured when a hold's cap stops it.

### Runs, round 6

Every run used a scratch venv (`uv sync --locked --group dev`, Python 3.14.7 with the GIL) and the venv's `python` (not `uv run`, so a queued hold cannot end on uv's cache lock), with `PYTHONPATH` unset, `HYPOTHESIS_PROFILE=ci` and no xdist. Each ran in its own hold through the hub's `suite_hold.sh`, in the foreground: the Bash tool moved each call to the background at its 600 s limit, and I waited on its log in the foreground with a bounded poll and started nothing else. Each pytest ran in-process with a 19-minute alarm and JUnit XML for per-file counts. Slot B stayed closed (the host's free disk was under 50 GB), so every hold took slot A, after waits of 1 to 32 minutes, at load 18 to 55 on 18 CPUs. The tree each pytest ran differs from its commit only in documentation.

| Hold | What, at | Result |
|---|---|---|
| A | brief groups 1 to 4 and `test_daemon_log_reminders.py`, one pytest, at `61fb5d307` | **1,600 passed**, 466 s: `test_daemon_log_handler` 127, `test_daemon_log_sites` 82, `test_daemon_log_scrubbing` 86, `test_daemon_log_cut_windows` 159, `test_daemon_log_tail` 16, `test_daemon_log_reminders` 101, `test_round10_refusal_context` 59, `test_handler_refusals` 317, `test_scrub_key_order` 70, `test_sessions_handoff` 117, `test_sessions_handoff_excerpts` 153, `test_cli` 222, `test_daemon_verbs` 20, `test_offline` 33, `tests/fake/test_state_contract` 37, `test_test_module_names` 1 |
| M | the count table (§1), the resolution census (§2), `tools/measure_cut_cost.py` at 1 and 4 MiB and then at 16 MiB, the cce8f1af7 cut at all three, then `--logs` at 1, 4 and 16 MiB; no pytest | the tables in §1, 6 minutes |
| B | `tools/daemon_log_cut_mutations.py` (no pytest), then round 6's regressions on `1cf653dff`'s source, one pytest | the mutations below; 81 of 88 cases failed on `1cf653dff` (below) |
| U1 | `tests/unit` and `tests/fake` as directories, the 16 files of hold A left out, one pytest, at `66c23542c` | **4,973 passed**, stopped by the alarm at 1,140 s (36% of the collection), in `test_guard_trust.py`. 104 files complete. The whole collection of both directories ran first, with no error |

**On `1cf653dff`'s source** (hold B, and hold U2 for the masked-tag case: `git archive 1cf653dff`, round 6's test files copied in, run there with this venv, so the tests run against round 5's code):
- **Review r4's count shapes under round 5's stated bounds** (round 5's own count test, with review r4's three shapes added): the quote kinds fail its 12 passes, and the 480 data URI prefixes its `REGEX_BOUND`, as review r4 found. The data URI body passes there: it is P3-3's guard against a mutant, not a regression.
- **The tag regressions:** a tag on the first line, all 30 (`scrubbed_tail` at 5, 14 and 40 lines, each form); `daemon logs -n 5/14/40` after one, all 15; a tag among the last lines, all 10; the handler writing a tag, 8 of 10 forms, and through a real handler; a log too short to cut, 8 of 10 forms; the masked-tag cases, all 4; and both properties. The four that pass there are the whole-reminder forms of the handler test and the short-log test, which round 5 already stripped: controls.
- **The split first line:** all three follower tests fail there.

### Mutations, round 6

`tools/daemon_log_cut_mutations.py`: each fix put back in-process, the named tests called with fixtures the tool builds, the baseline as control. Hold B ran 19 named tests; hold U2 ran the sweep again with `unmasked-strip` and its named test added (20). Both found the same on the 19 tests both ran.

| Mutation | Named tests failed (of 20) |
|---|---|
| baseline | none |
| round 5's: `whole-margins` | the count test on margin-only lines, the group, leading spaces, the run of `<`, and the data URI body |
| `backtracking-header` | the count test on margin-only lines, leading spaces and the data URI body |
| `whole-reminder-search` | the count test on all five of its shapes |
| `folded-tail` | the three round-5 tail tests; the tag on the first line (orphan, whole reminder); `daemon logs` after one; the masked-tag case |
| `raw-logs` | the two round-5 `daemon logs` tests; `daemon logs` after a tag; the short log through `daemon logs`; `daemon logs -f` with a split first line |
| **round 6's:** `tail-not-neutralised` (round 5's tail: no resolution, no marks) | the tag on the first line (both forms); `daemon logs` after one; the short log; the masked-tag case |
| `handler-not-neutralised` | `test_the_handler_writes_no_reminder_tag[orphan-open]`, `test_an_orphan_tag_reaches_daemon_log_as_the_marker_through_a_real_handler` |
| `tail-unmarked-after-scrub` (the continuation's) | `test_a_log_too_short_to_cut_shows_each_tag_as_the_marker[orphan-open]` |
| `follow-not-neutralised` (the continuation's) | `test_daemon_logs_follow_holds_a_split_first_line` |
| `unmasked-strip` (the continuation's: reminders taken out without the quoted pass first) | `test_a_tag_a_quoted_value_masks_is_none_to_the_strip[last-MASKED_CLOSE]`: the secret the whole-text scrub hides shows |
| `body-by-regex` (P3-3: round 4's `_NOT_BODY_RE.search`) | `test_at_16_mib_no_regex_reads_past_the_cut_s_windows[data-uri-body]`: 1,097,672 regex characters at 1 MiB |
| `unmetered-data-body` (`_DATA_BODY_RE` out of the meter's sight) | `test_the_quote_kinds_cost_a_look_each_and_the_meter_counts_the_body_by_what_it_reads`: no body match metered |
| `pem-prefix-not-held` | the three follower tests of the split first line |

### Not run, round 6

- **The rest of the whole suites**, while holds U2 and later were running: this section is updated as each ends.
- `tests/e2e`, `tests/frontend` and `tests/process`;
- Python 3.12;
- CI, since this job has no network.

### Where this round departed from the brief

- **The fix-6 job's uncommitted work was applied whole.** It was the round's docs (C-25.7, this section, invariant 10, `daemon_log`'s docstring) and the measurement tool's stop handling. Everything it stated was checked against the code and the runs here, and its placeholders were filled from hold M. Three of its sentences were changed: that `origin/release/217` had not moved (it had, by #237); that the round-2 shapes made "no look" (three of them make 2 to 10, each a small part of a pass); and a census of 2,496 tagged texts its job ran but did not keep, replaced by the census hold M computed.
- **Added beyond the brief:** a test of a log too short to cut, and three mutants (`tail-unmarked-after-scrub`, `follow-not-neutralised`, `unmasked-strip`), so that each place that marks or resolves tags fails a named test of its own. The masked-tag test asked for more lines than its log held, so its tail never cut and never resolved; it asks for three now.
- **Hold A merged the brief's four groups into one pytest**, and holds B, M and U2 ran a tool and a pytest, or several tools, one after the other in one hold, never two at once.
- **One input was checked outside the lock**, a single call of under a second: whether a reminder that joins `pass` and `word` into a key re-pairs later quotes in the tail. It does not; the quoted rule masks the union of overlapping matches.
- **The host's lid closed** during the first queued hold A, which never started; it was rerun.

### Commits, round 6

- `1b037ce06` test: the cut's count states looks apart and meters a body match by what it reads; a reminder tag no longer blanks the tail (#233 review r4)
- `b86b70e90` fix: no reminder tag reaches or blanks daemon.log's readers; daemon logs -f holds a split private-key first line (#233 review r4)
- `f2899de1e` tools: round 6's mutants, and the cost measured on review r4's shapes and a realistic daemon.log (#233 review r4)
- `c1fd633b0` docs: cut_whole states its cost as the code gives it; settled_end names the first line's hold apart from the data URI loop (#233 review r4)
- `dfdff9eb8` Merge origin/release/217 (a26d08336, #237) into the D-LOG1 branch
- `e008cfded` tools: measure_cut_cost keeps the rows it measured when a hold's cap stops it (#233 review r4)
- `61fb5d307` test: a log too short to cut shows each tag as the marker; mutants for the tail's and the follower's marking alone (#233 review r4)
- `fdbf52954` docs: C-25.7 states the cut's cost as the code gives it, measured; a tag no longer blanks daemon.log's tail (#233 review r4)
- `66c23542c` test: the masked-tag case cuts its log, so it tests the resolution; a mutant strips without the quoted pass (#233 review r4)
- this report's round 6

The commits are on this worktree's detached HEAD, as in rounds 4 and 5: Subfleet keeps them under `refs/subfleet-salvage/`. Nothing is pushed.
