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

**faulthandler stays.** CPython's documentation says that faulthandler shows "only the filename, the function name and the line number … (no source code)". `Python/traceback.c`'s `dump_frame` writes `  File "<co_filename>", line <n> in <co_name>` for each frame and nothing else. I read both on 2026-10-18; this sandbox has no local CPython docs or source, so I fetched docs.python.org and the 3.14 branch's `Python/traceback.c` on GitHub. `test_faulthandler_writes_frames_only` runs `faulthandler.dump_traceback` into a real daemon's log while an exception carrying a secret is being handled and a local holds it. The log shows the frame and no secret, no exception class and no source line.

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
- **Cost.** Each record is now scrubbed twice in the line form, under the handler's lock. In one probe, 1,000 short lines took about 355 ms, so about 0.36 ms a line. Scrubbing is linear in the text, and a record's text is not bounded before it is scrubbed, as `refusals.scrubbed_line` already did for refusal details. Review r2 of #225 measured about 1.7 s for its worst shape at 1.3 million characters in daemon.log mode, so a record of several megabytes would hold the handler's lock for seconds.
- **A record channel noticed, not changed.** I read this but did not execute it. `runner.py`'s steer failure puts `str(exc)` into `_miss_steer`'s reason, which becomes the message's `state_reason` (`steer-missed: input-unavailable: …`) and a `steer.missed` event. C-25.7 limits `state_reason` to classes, errnos and our words. Whether `store.set_state` scrubs that reason is unverified.
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
