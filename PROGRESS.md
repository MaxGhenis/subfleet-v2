# PROGRESS — lane/claude-adapter

Milestone 2: the Claude provider adapter, the rate-limit stream sensor, attestation,
and the fixture corpus. Clause numbers are `docs/acceptance-contract.md`.

## State

Complete. `uv run pytest -q` is green (263 passed, 4 live tests skipped) and the
milestone-2 acceptance rows in C-21 are covered by named tests. The integrator's
report is `docs/lanes/claude-adapter-report.md`.

## Done

**Reconnaissance**

- Read the contract (C-6.7, §9, §10, C-12.4 to C-12.8, C-14.3, §20, §21),
  `subfleet/contracts.py`, `subfleet/adapters/base.py`, the experiment-0 report, the
  installed CLI's flag list, and `VERSIONS.md`.
- Read v1, read-only: `bin/subfleet-claude` (auth probe, `prefer_transcript_text`,
  `model_matches_requested`, `check_model`, the launch line and `PERM_ARGS`, the text
  classifier), `subfleet/claude.py`, `subfleet/capacity.py` (`record_lane_run`,
  `_resolve_transcript`), `subfleet/util.py` (`parse_reset_clock`), `subfleet/delegate.py`.
- Established the `~/.claude/projects/` encoding empirically (`/`, `.` and `_` each
  become `-`, case preserved), verified against a live transcript's own `cwd` field.
- Recovered the authoritative stream-json schemas from the installed Claude Code
  2.1.260 binary's embedded validators, and the real message strings the classifier
  matches, so nothing in the parser or the fixtures is invented.
- Scanned the newest 150 v1 run directories for real Claude artifacts: 121 Claude runs,
  of which only one keeps a non-empty `err.log`. v1 does not retain the error text of a
  text-classified limit, so those fixtures are synthetic and say so.

**Built**

1. `subfleet/adapters/claude_stream.py` — a pure, tolerant stream-json parser.
2. `subfleet/adapters/claude.py` — `ClaudeAdapter`: credentials, enrolment, probing,
   readings, launch, classification, attestation, deliverable, resume.
3. `subfleet/contracts.py` — one additive field: `Launch.notes`.
4. `tests/fixtures/claude/` — 19 cases plus the generator that records their provenance.
5. `tests/bin/claude` — the fake provider.
6. `tests/unit/`, `tests/process/`, `tests/live/` — 263 tests, every docstring citing
   the clause it proves (C-20.5).

## Next

Nothing outstanding in this lane. Open questions for the integrator are listed in
`docs/lanes/claude-adapter-report.md`; the two that need a decision are the
`Reading.window` value `"admission"` (a vocabulary addition C-9.1 does not enumerate)
and whether the daemon publishes `stream.jsonl` by calling `link_raw_stream`.
