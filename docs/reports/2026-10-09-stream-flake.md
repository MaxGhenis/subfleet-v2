# D-F5: Claude conversation stream flake

The ordering expectation is fixed. **The required e2e load proof remains
incomplete:** this execution sandbox denies `ps` and `sysctl`, so the real e2e
never passes its C-5.3 process-inspection guard. Skips are not successful loops.

## Cause and sequences

The failed predicate is `kinds[-1] == "turn.completed"`; `accepted` and `text`
are present. [CI run 37902206664](https://github.com/MaxGhenis/subfleet-v2/actions/runs/37902206664)
shows both Python 3.12 and 3.14 failing with the prefix
`status, served, status, accepted, served, text.delta, …` and final event `served`.
The CI log truncates the intervening events.

`tests/fake/interactive_claude.py:181` handles `get_settings` on its reader
thread (:204), independently of the main thread's acknowledgement, canned
reply and result (:345–360, :430). Scheduling can put its settings response
after `result`. The real driver explicitly retains that response after ending
the turn (`subfleet/conversations/claude_turn.py:401`, :517).

A constructed late-settings stdout trace fed to the real driver yields the
complete failing sequence:

```text
status, served, status, accepted, served, text.delta, text, turn.completed, served
```

The final `served` contains `{"effort":"high"}`; the outcome remains `complete`.
An early settings response instead gives:

```text
status, served, status, served, accepted, served, text.delta, text, turn.completed
```

These are constructed driver traces, not observed local e2e failures.

## Contract and fix

The test imposed an order the contract does not promise. C-26.5
(`docs/acceptance-contract.md:450`) says: “Output after it is attached to the
same message and never changes the outcome.” C-26.8 (:453) records the settings
the provider reported. Neither makes completion the last conversation event.
C-24.2 orders message acceptance by client predecessor, not independent
settings responses relative to provider acknowledgements.

`tests/e2e/test_conversations.py:146` now checks each message's acknowledgement,
exact canned reply and successful completion, preserving the order of those
frames emitted by the fake provider's main thread. It checks increasing,
unique event cursors (C-25.4, :440; replay identity in C-26.6, :451), and permits
later settings evidence. Both turns use this assertion (:174, :184).
No product behavior or contract was changed.

`tests/unit/test_conversation_stream_assertion.py` drives the real driver with
four settings-response positions, including after completion, and rejects
missing events, reordered replies, duplicate cursors and failed completions.
`tools/stream_flake_pytest.py` repeats tests in one pytest process and captures
the full conversation event rows on failure.

## Verification

Python: CPython 3.14.7 free-threaded. Fresh `TMPDIR` was under Darwin's user
temp root, outside home, with no component named `tmp`. One pytest process ran
at a time. Load was one recorded process, PID **936**, with 16 SHA-256 burner
threads (64 KiB payloads), kept alive across the before/after phases. Its
supervisor terminated that recorded PID and reaped it (`returncode: -15`).

| Real e2e phase | Repetitions | Passed | Failed | Skipped | Failure rate |
|---|---:|---:|---:|---:|---|
| Original, no added load | 50 | 0 | 0 | 50 | Not measurable |
| Original, with burner | 50 | 0 | 0 | 50 | Not measurable |
| Fixed, same burner | 100 | 0 | 0 | 100 | Not measurable |

The **constructed late-settings driver trace**, under that same load, failed
the original predicate **50/50** times and passed the corrected assertion
**100/100** times. This forces the identified interleaving; it is not a measured
natural flake rate or a substitute for the requested e2e proof.

The driver and regression suite passed **70 tests under load**. The final
regression verification passed **10/10 tests** after the burner stopped.
Raw repetition outcomes, logs, driver events and burner records are under
[`2026-10-09-stream-flake/`](2026-10-09-stream-flake/).

For the outstanding e2e proof on a host permitting process inspection:

```sh
.venv/bin/python -m pytest -q -p tools.stream_flake_pytest \
  --stream-loops=100 --stream-results="$TMPDIR/stream-results.jsonl" \
  tests/e2e/test_conversations.py::test_a_claude_conversation_streams_completes_and_continues_in_the_same_session
```

Use the same task-owned load on the original and fixed revisions, with the
fresh Darwin `TMPDIR` required by the task.

## Delivery

Fix commit: `27cde9e582a631d7c35306c9a353709ee2bcab71` on
`fix/stream-e2e-flake`, committed in workspace-owned `.git-local` because shared
git metadata is outside the writable roots. The delivery bundle is
`docs/reports/2026-10-09-stream-flake.bundle`, with prerequisite
`6805af2dedd760c2cd087e765ff9d9c016aebd47`, exporting
`refs/heads/fix/stream-e2e-flake`. Its exact delivery head is printed by
`git bundle list-heads`; the report commit follows the fix commit.
