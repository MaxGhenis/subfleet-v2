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
7. **The cut hides what the whole text hides.** For any text and limit, the secrets either form shows of `cut_whole(text, limit)` are a subset of those it shows of `text`. The cut text is at most `limit` characters plus its marker line; a text holding a reminder tag is the marker line alone.
8. **A group's margins cost nothing.** A group's block shows no value that the same text without margins hides, nor one its line form hides. A one-member group hides whatever its member alone hides.
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
