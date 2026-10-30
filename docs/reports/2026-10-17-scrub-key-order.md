# D-SCR1: the scrubber's token and header rules must not take a credential key

Base: `3fbc4b7c5` on `release/217`, after #225 and #227 merged. Its scrubber is byte-identical to #225's reviewed head `b2dc9946b`. Brief: `~/reviews/subfleet-hub/scrub-order-2026-10-17/brief-build.md`; defect: that folder's `pr225-review-r2.md`, first section. Work stayed in the job's worktree, with no Git remote operations and no sub-agents. Nothing was pushed.

## The defect

`_scrub` runs the token rules (`_JWT_RE`, `_PREFIXED_TOKEN_RE`, `_BEARER_RE`, `_LONG_BASE64_RE`, `_HEADER_RE`, `_URL_PASSWORD_RE`) before the named-value passes (`_scrub_named_values`: quoted, plain, flags). When a token rule replaces a credential key, the pass that follows no longer sees that key, and the key's value stays in the text. Two rules did this in realistic text:

- `_BEARER_RE`: its `\s+` crosses line breaks, and its token class reads identifiers, so the key after a `Bearer` scheme word became the "token".
- `_HEADER_RE`: it takes the rest of its line, even when that is only a key whose value is on the next line.

At release/217, each of the review's inputs leaks in all three modes (the whole text, an excerpt, and the daemon.log line):

| Input | release/217, whole text |
|---|---|
| `token_type: Bearer\naccess_token: SECRET7realaa2` | `token_type: Bearer [REDACTED]: SECRET7realaa2` |
| `token_type=bearer refresh_token=SECRET7realaa7` | `token_type=Bearer [REDACTED]SECRET7realaa7` |
| `auth:\n  scheme: bearer\n  client_secret: SECRET7realaa3` | `auth:\n  scheme: Bearer [REDACTED]: SECRET7realaa3` |
| `upstream said:\nAuthorization: client_secret=\nSECRET7hdraa4` | `upstream said:\nAuthorization: [REDACTED]\nSECRET7hdraa4` |

The last also leaks in daemon.log. Once `suggestions._scrub_line` folds the lines, the header no longer starts a line, so the second scrub does not catch it either.

## Decision 1: `Bearer` refuses a token that begins with a credential key

```python
_BEARER_RE = re.compile(
    r"(?i)\bBearer\s+(?![A-Za-z0-9._~+/-]*?(?:" + _SENSITIVE_WORD + _ASSIGN_SEPARATOR_RE.pattern
    + r"|--" + _SENSITIVE_KEY + r"[ \t]+[^\s-]))[A-Za-z0-9._~+/-]{8,}=*")
```

This is the reviewer's lookahead, `(?![A-Za-z0-9_-]*?` + `_SENSITIVE_WORD` + `\s*[:=])`, with three widenings. Each widening has a regression that fails both at release/217 and under the reviewer's exact pattern:

1. **The prefix spans the token's own characters (`.`, `/`, `~`, `+`), not only identifier characters.** A token like `oauth.client_secret` ends in a key the plain rule reads (`client_secret`). With the narrower prefix, `auth.scheme=bearer\noauth.client_secret=SECRET7…` still leaks. On the rebuilt fuzz (seed 225, 20,000 texts), with the header change applied, the reviewer's exact pattern still leaks 6 triples in 2 texts (`BEARER\na.bAccess_Token : (SECRET7…)`); the widened one leaks none.
2. **The separator is the assignment rule's own, `_ASSIGN_SEPARATOR_RE`.** It includes a key's closing quote, escaped or not (`token_type: Bearer\naccess_token\": SECRET7…`). Reusing the pattern keeps the lookahead and the rule it protects from drifting apart.
3. **Flags count.** `Bearer --api-key SECRET7…` and `auth=bearer --client-secret SECRET7…` lost the flag's key to `Bearer`, and the flag pass then left the value. The alternative mirrors `_SENSITIVE_FLAG_RE` (`--` and `_SENSITIVE_KEY`, fully) and `_FLAG_VALUE_RE` (`[ \t]+` and a value not starting with `-`).

**`_SENSITIVE_WORD`, not `_SENSITIVE_KEY`, in the assignment alternative.**
- After the lazy prefix, `_SENSITIVE_WORD` already reads every compound key: `db_password` is the prefix `db_` plus the word `password`. That is exactly how the assignment rules read a key: an identifier ending in a sensitive word (`_SENSITIVE_SUFFIX_RE`).
- `_SENSITIVE_KEY` adds nothing there except its own nested repetition (`[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*`) inside the lazy one, which is quadratic. Measured with whole-text scrubs at 512 → 2,048 repetitions of the unit, the `_SENSITIVE_KEY` variant took 14.1 → 241 ms on `Bearer a_a_…` (17.1×), 13.0 → 218 ms on `Bearer a-a-…` (16.8×) and 145 → 2,193 ms on `Bearer tokentoken…` (15.1×). The chosen pattern takes 3.0× to 5.9× for 4× input (table below).
- The flag alternative keeps `_SENSITIVE_KEY` because the flag rule fully matches it after `--`. There it is anchored at a `--` and cannot run past the next `--`, so its work stays linear (the `Bearer --a` row below, and `Bearer --client_…` in the single-call measurement after it).

**`_neutral_break` is unchanged.** Its `Bearer` rule still refuses a break after `Bearer` when a token character follows. That is a superset of where the new pattern can match, and refusing more breaks is safe. The lookahead looks past a line break only between a key (or its closing quote) and its `:` or `=`, or between those and the value. The key rules refuse a cut in both places. The docstring now says this. Splitting at every break `_neutral_break` allows agrees with the whole-text scrub on 20,000 fuzz texts (23,621 cuts), at head and at release/217 alike, and the property checks the same on every example.

**Not done: moving the plain pass before the token rules** (hub decision 3). Run as a mutation, it leaks `curl -H 'Authorization: Bearer SECRET7tokenabc123' https://x`, as the reviewer showed. `test_a_bearer_token_stays_redacted[curl]` fails under it, and so does the property.

## Decision 2: a header leaves a key whose value is past its line to the named-value rules

The header rule's replacement is now `_header_value`. Through `_key_past_line`, it asks whether the line's last identifier is a credential key whose separator (`_ASSIGN_SEPARATOR_RE`, read from the key's end) reaches past the end of the line to a value (`_PLAIN_VALUE_RE` or an opening quote). Only the last identifier can qualify: between it and the line end, the separator allows nothing but a closing quote, whitespace and `:` or `=`.
- If so, the header redacts only what precedes the key, and keeps the key (with its opening quote and escaping backslashes) and what follows it on the line.
- When nothing precedes the key, the header leaves the line whole. The plain rules then read `Authorization`, `Cookie` or `Set-Cookie` as a key, whose plain value is the key and separator. Their nested-value rule (`a=xtoken\n= y`) takes the value on the next line: `Cookie: token:\nSECRET7…` becomes `Cookie: [REDACTED]\n[REDACTED]`.
- If not, the header redacts its line exactly as before.

**Why "leave the key" rather than "redact the header through to the value".** It is the smaller change, and it defines nothing new.
- "The value after that key" is whatever the named-value rules take, so leaving the key to them uses their own patterns: separator, plain value, quoted value, unions, the nested-value rule.
- Redacting through would need the header rule to re-implement those rules, so that a header match could end on a later line. It would also need a custom substitution loop, because `re.sub` cannot extend a match from its replacement.
- The leave-the-key form closes the review's input, all 24 header shapes (below) and every header triple of the rebuilt fuzz. The redact-through form was not built, so no fuzz number is claimed for it.

Three details keep the scrub idempotent and the counts right:
- **The line's trailing whitespace still goes**, as it always did with the redaction. Without that, `Cookie:\nSet-Cookie: \n…` scrubbed to `Cookie:\n[REDACTED] ` and scrubbed again to `Cookie:\n[REDACTED]`. Ten such idempotence failures in 20,000 fuzz texts showed this before the fix.
- **A space parts the redaction from a key that adjoined what it hid.** `Authorization:a=SECRET7…; \"x.credentials\"\n: …` keeps ` credentials\"` with the space. Otherwise the header's own plain value ran on into the key (`[REDACTED]credentials\`), dropping it and leaving a lone `"`.
- **`_Tally` counts nothing for a replacement that changes only trailing whitespace.** That is the header leaving its line to the named-value rules. Every other replacement differs from its match by more than trailing whitespace, so their counts are unchanged.

Ordinary headers are unchanged in output and count: their value is on their line; a key with no separator after it; a key whose value is on its line; a separator that ends the text; an earlier placeholder. `test_an_ordinary_header_is_redacted_and_counted_exactly_as_before` compares `(text, count)` with release/217's scrubber in both modes, for ten such headers. The `_Tally` rule that a header holding several values counts each still holds (`Cookie: a=SECRET7…; session_token=\nSECRET7…` counts 2).

## The contract

C-23.14 now states the rule, scoped to what the code guarantees: the `Bearer` and header rules never take a credential key that a named-value rule would read. The clause names what each rule now does. It also names the rule's boundary: the JWT, prefixed-token and long base64 rules read only their own token shapes, so a key of such a shape is still read as a token. The cut sentence says both new decisions look past a line break only after a key, where no excerpt is cut. A change-log bullet heads "Changes in version 3".

## Invariants

Each holds for every input, and each is executed by the tests named:
1. **No credential value survives.** No value the named-value, `Bearer` or header rules define (a `SECRET7` value after a key and a separator, after a flag, after `Bearer`, or in a header) survives in any mode. The modes are `scrub_secrets` on the excerpt rules, the whole-text scrub, `scrub_bounded` with head and tail excerpts cut at each of the text's line breaks, `refusals.scrubbed_line`, and `refusals.detail_line`. Exception: the documented limitations below. Tested by the property, every regression, and the differential.
   - **Correction (2026-10-20, D-SCR2, #231 review r1 P3-1): false as written.** At this PR's head, a secret that ends in a credential word was read as a key and survived. Examples: `invalid Bearer SECRET7a1b2c3d4_token: expired`, and `Cookie: sid=SECRET7abc123token` with `====` on the next line. The differential's generator dropped exactly those secrets, so invariant 4 below held only on a fuzz without them. D-SCR2 hides them and states the corrected invariant in `docs/reports/2026-10-18-scrub-inline-headers.md`.
2. **Idempotence.** Scrubbing a scrubbed text changes nothing, in the excerpt, whole and daemon.log modes. Tested by the property and the header tests.
3. **Excerpt agreement.** For every line break `_neutral_break` allows, `_scrub(t[:p]) + _scrub(t[p:]) == _scrub(t)`. Each `scrub_bounded` excerpt equals the matching part of the whole block's scrub. Tested by the property, with excerpts shrunk to the text's length; the regressions use real 131,072-character excerpts.
4. **Monotone against release/217.** On the seeded fuzz, every (text, mode, secret) triple the head leaks, release/217 leaks too. Tested by the differential.
5. **Linear work.** Four times the input does well under 8× the work: for the Bearer rule (`test_the_bearer_rule_keeps_scrubbing_linear`, `test_the_bearer_lookahead_keeps_scrubbing_linear` before #239), and for ordinary identifiers (`test_scrubber_work_scales_linearly_for_long_ordinary_identifiers`, unchanged, CPU-timed). Since #239 the scrub runs release/217's frozen pipeline (`scrub_release`, the lazy lookahead included) and then the head's, whose `_bearer_refusal` reads a run's end once. Since D-SCRUB-LINEAR (2026-10-26) the Bearer rule's tests count rather than time, and each count grows under 5× for 4× the input:
   - the characters each regex call of both pipelines reads (`tests/scrub_work.py`, on #233's `tests/regex_reads.py`), and no scrub reads more than 256 a character (the seven runs read at most 66);
   - the `subfleet` lines it runs;
   - the instructions the CPU retires for the plain scrub (`test_the_bearer_scrub_retires_linear_instructions`, `tests/cpu_scaling.py`), at 2,048, 8,192, 32,768 and 131,072 characters, where the CPU's counter is exposed. CI's `macos-15` virtual machines hide it, so CI skips this test on both interpreters (run 40399635625); it runs on a Mac that is not a virtual machine, such as the host the lanes run on.

   The reads and lines miss C-level `str` work and a regex call C code makes on a pattern no stand-in holds; the instructions count both (#266 review r1), so on CI that work goes unchecked. A cheap C-level quadratic (a slice per identifier) grows 4.3×, 5.4× and 8.4× over the three steps since #239 put release's pipeline first (4.6× and 6.1× over the first two before), hence the four sizes (#266 review r2, round 4). None of the three sees growth like n log n (about 4.7× for 4×), or input shapes other than the seven Bearer runs: five tokens repeated, and a run ending in a credential word and separator or glued to a credential flag, which `_bearer_refusal` refuses. Timed, the lookahead's CPU ratio failed shared CI runners at about 9× on unchanged code (runs 39682771461 and 40326659520).
6. **Unchanged where nothing changed.** Ordinary headers and inline `Bearer` tokens scrub exactly as at release/217, output and count.

## Fuzz and differential numbers

The reviewer's generator was not kept, so `tests/unit/test_scrub_key_order.py` rebuilds its shape: 7 key quotings, 11 separators (four crossing a line break), 16 value forms, and mixed filler including `Cookie:`, `Bearer`, `--token`, `Authorization:`, a reminder and backslashes. Its counts are its own, not the reviewer's 70 triples in 28 texts. The scratch runs used the same generator at 20,000 texts per seed, in three modes: whole text, `clean(text, 4000)` and `refusals.scrubbed_line`.

| Tree | Seed 225: triples (texts) | Seed 2026: triples (texts) | Idempotence failures |
|---|---|---|---|
| release/217 | 709 (265): 477 after `Bearer`, 221 header, 11 other | 734 (273): 495, 231, 8 | 0 |
| Head | **0 (0)** | **0 (0)** | 0 |
| Head, reviewer's exact lookahead | 6 (2), all `Bearer` | 0 | 0 |
| Head, `_SENSITIVE_KEY` lookahead | 0 | 0 | 0 (quadratic, above) |
| Head, header change reverted | 273 (117) | 274 (119) | 0 |

On both seeds the head leaks no triple that any other tree does not. "Other" in the release/217 row counts leaks whose secret has no `Bearer` or `name:` header text before it. The head fixes them too, and reverting only the header change brings all 11 back, so they are header leaks whose header the classification missed.

**The differential test** uses seed 225 and 4,000 texts, against `tests/unit/scrub_release217.py`. release/217 leaks 119 triples in 45 texts (88 after `Bearer`, 31 after a header, 0 other); the head leaks 0. The test asserts that release/217 has more than 50 `Bearer` and more than 20 header triples, so the fuzz is known to exercise both. It runs in about 2 to 3 s of CPU.

**Excerpt splits:** 20,000 texts (seed 77), all 23,621 neutral breaks: 0 disagreements at head, 0 at release/217.

**A wider property search** (scratch, not committed): 3 × 600 random examples with the final strategy (seeds 11 to 13), and 3 × 600 with the strategy before `b249a15a7` (seeds 1 to 3). No counterexample.

## Timing

CPU milliseconds (`thread_time`), best of 3, at host load 30 to 80, so ratios matter more than absolute values. Columns are n = 4,096 / 16,384 / 65,536 repetitions of the unit.

| Input | Head, whole text | Head, daemon.log | release/217, whole text |
|---|---|---|---|
| `Bearer ` + `a_` × n | 1.4 / 5.2 / 27.6 | 2.1 / 5.5 / 27.3 | 0.5 / 1.8 / 4.7 |
| `Bearer ` + `token` × n | 4.6 / 24.1 / 89.2 | 2.9 / 19.1 / 71.5 | 1.2 / 4.7 / 14.7 |
| `Bearer ` + `--a` × n | 3.7 / 12.2 / 49.0 | 2.3 / 8.9 / 48.9 | 0.4 / 2.2 / 8.8 |
| `Bearer ` + `x.` × n | 2.5 / 8.8 / 45.9 | 3.8 / 12.5 / 49.4 | 1.9 / 10.6 / 33.0 |
| `(Bearer a_a_a_a_ )` × n/4 | 13.9 / 60.6 / 271.7 | 33.0 / 133.4 / 464.6 | 9.3 / 48.9 / 196.4 |
| `Authorization: ` + `a_` × n + `token=\n…` | 4.2 / 14.4 / 60.7 | 2.4 / 10.7 / 62.9 | 1.0 / 3.2 / 13.3 |
| `Cookie: k token=\n v\n` × n/8 | 15.0 / 44.6 / 225.2 | 14.4 / 79.9 / 314.4 | 9.3 / 42.0 / 163.9 |

- **From 16,384 to 65,536, every head row grows 3.0× to 5.9× for 4× the input:** linear, like release/217 (2.5× to 4.2× on the same rows).
- **The lookahead costs more per character than release/217's pattern.** Over the 262,144-character work bound, the slowest single `_BEARER_RE.sub` call measured 53 ms of CPU (`Bearer ` + `--client_` repeated), against under 4 ms at release/217. `re` holds the GIL for that call. That is under the 0.1 s bound `test_scrubber_keeps_other_python_threads_responsive` sets for its own input, but closer to it. A faster form (a possessive token run, then per-word lookbehinds generated from `_SENSITIVE_WORD`) is possible if the hub wants the margin back.
- **`test_scrubber_work_scales_linearly_for_long_ordinary_identifiers` keeps its ratio; the lookahead does not change it.** Its inputs (`a_`, `a-`, `a.` repeated) contain no `Bearer` and no header. `_BEARER_RE.sub` costs no more on them at head than at release/217 (20.5 against 38.7 µs on `a_` × 2,048, i.e. noise). Over 20 trials of the test's own probe, `a_`'s ratio had median 3.86 and max 4.56 at head, against median 3.54 and max 5.22 at release/217; no trial reached the 8× bound. It failed once in hold 1, at load about 80 (0.172 ms → 1.581 ms), and all three of its cases passed when hold 2 re-ran it.

## Mutations

Each was applied in-process by monkeypatching the module attribute, leaving the worktree file untouched; the named tests were then called directly.

| Mutation | Fails |
|---|---|
| Drop the lookahead (release/217's `_BEARER_RE`) | 3 of 4 `test_the_reviews_inputs_lose_their_values_in_every_mode` (the three `Bearer` inputs), 10 of 10 `test_bearer_never_takes_a_credential_key`, the property, the differential |
| Revert the header change (`group(1) + REDACTED`) | `…_in_every_mode[header-key-value-next-line]`, 24 of 24 `test_a_header_never_takes_a_key_whose_value_is_past_its_line`, `test_a_header_redacts_what_precedes_the_key_and_counts_each_value`, 3 of 3 `test_a_kept_key_is_parted_from_the_redaction_before_it`, the property, the differential |
| Move the plain pass before the token rules | `test_a_bearer_token_stays_redacted[curl]` (and `[header]`, `[inline-header]`), the header count test, 2 glued-key cases, 2 ordinary headers, the property |

The property caught the third mutation only after commit `b249a15a7` added inline `curl -H 'Authorization: Bearer …'` parts to its generator.

## Tests

**`tests/unit/test_scrub_key_order.py`** (new): 70 cases.
- **The review's four inputs:** in every mode, with real 131,072-character excerpts cut at each line break and at the text's end, plus `detail_line`'s exact line.
- **Ten `Bearer` shapes:** across a break or on one line, a dotted or path key, an escaped closing quote, two flags, a prefixed key.
- **Six inline tokens that must stay redacted:** `Authorization: Bearer`, `curl -H '…'`, an inline header, a dict, a padded token, a token before a colon.
- **24 header shapes:** the header's value on its line or the next × the key alone or after another value × bare, quoted or escape-quoted key × separator ending the key's line or starting the next. A quoted key alone is the header's own quoted value, so its case is the one that leaked in the fuzz: the quoted rule's union pulled a later key onto the header's line.
- **Header outputs and counts:** exact output and count for what precedes a key, three glued keys, and ten ordinary headers equal to release/217 in output and count.
- **The property:** Hypothesis, `derandomize=True`, 200 examples, no database.
- **The differential.**
- **Five linear-work probes.**
- **Five documented limitations** (below).

All 44 cases that are not guards fail at release/217. That was run without pytest, by calling the test functions against release/217's `handoff.py`. The other 26 pass there by design: inline tokens 6, ordinary headers 10, linearity 5, documented limitations 5.

**`tests/unit/scrub_release217.py`** (new): release/217's scrubber, verbatim, for the differential. Like `reset_credits_release217.py`, it is not a test module.

## Documented limitations (outside D-SCR1, marked `documented_limitation`)

- **A header that does not start a line keeps its second word.** The header rule reads only a header that starts a line. Inline, `Authorization` is just a key whose plain value is its first word, so `curl -H 'Authorization: Basic SECRET7…' https://x` and `sent Cookie: a=1 SECRET7… upstream` keep the secret in every mode. A refusal whose words begin with such a header keeps it in `detail_line`, whose prefix puts the header mid-line. release/217 does the same. The fix needs a decision about where an inline header's value ends (a closing quote, the line), so it is left to the hub. The property skips `detail_line` for texts that begin with a header line, and says so.
- **A key shaped like a token is read as one.** The JWT, prefixed-token and long base64 rules take `sk-proj-abcdefghijklmnopq_token=`, `eyJ….…_token=` and 160 base64 characters ending in `token=`, so the value on the next line stays. These are not realistic keys. Guarding those rules the way `Bearer` is guarded would scan every identifier of every text, so C-23.14 states the boundary instead.

## Holds

Each hold was one foreground pytest under its own `lockf -k` hold of `/private/tmp/claude-501/subfleet-suites.lock`, with at most 1 worker (no xdist) and a 1,140 s in-process alarm. Runs used GIL Python 3.14.7 from the worktree's `.venv` (`uv sync --locked --group dev`), with `PYTHONPATH` and `SUBFLEET_RETENTION_DORMANT` unset, Hypothesis storage in scratch and `-p no:cacheprovider`. The host was on AC power, with load 50 to 80 and 13 to 15 `lockf` waiters.

Every hold ran the tree at `b249a15a7`. Nothing in code or tests changed after it.

| Hold | Started | Files | Result |
|---|---|---|---|
| 1 | 14:11 | `test_sessions_handoff`, `test_daemon_log_scrubbing`, `test_conversation_redact`, `test_scrub_key_order` | 281 passed, 1 failed (423 s) |
| 2 | 15:27 | `test_round10_refusal_context`, `test_handler_refusals`, `test_daemon_refusals_cli`, `test_protocol_secrecy`, `tests/fake/test_record_secrecy`, and hold 1's failed test again (all three of its cases) | 751 passed (140 s) |
| 3 | 15:42 | `tests/fake/test_state_contract`, `test_test_module_names`, and the scrubber's other callers and contract readers: `test_sessions_handoff_excerpts`, `test_suggestion_secret_properties`, `test_conversation_handoff_briefs`, `test_store_ids`, `tests/fake/test_sessions_end_to_end`, `test_invariants_index`, `test_desktop_ledger` | 315 passed (701 s) |

The brief's `tests/unit/test_refusals*.py` matches no file in this tree. Hold 2 ran the refusal and secrecy files the r2 reviewer ran with the two it names.

| File | Passed | Failed |
|---|---:|---:|
| `tests/unit/test_scrub_key_order.py` | 70 | 0 |
| `tests/unit/test_sessions_handoff.py` | 116 | 1 (hold 1: `test_scrubber_work_scales_linearly_for_long_ordinary_identifiers[a_]`, 9.2× against its 8× bound at load about 80; its three cases passed in hold 2) |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 |
| `tests/unit/test_handler_refusals.py` | 315 | 0 |
| `tests/unit/test_daemon_refusals_cli.py` | 8 | 0 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 |
| `tests/fake/test_record_secrecy.py` | 44 | 0 |
| `tests/fake/test_state_contract.py` | 37 | 0 |
| `tests/unit/test_test_module_names.py` | 1 | 0 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 |
| `tests/unit/test_suggestion_secret_properties.py` | 7 | 0 |
| `tests/unit/test_conversation_handoff_briefs.py` | 28 | 0 |
| `tests/unit/test_store_ids.py` | 27 | 0 |
| `tests/fake/test_sessions_end_to_end.py` | 33 | 0 |
| `tests/unit/test_invariants_index.py` | 25 | 0 |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 |

**Run outside pytest** (no lock hold):
- the base-failure check (44 of 70 fail against release/217's `handoff.py`);
- the three mutations;
- the fuzz and split runs;
- the timing runs;
- a Python 3.12.14 check of the review's inputs and the `curl` form (same outputs).

**Not run:**
- the full suite, and pytest on Python 3.12;
- the frontend and e2e tests;
- CI (no network).

## Commits

Commits are on a detached HEAD in the job's worktree, which was cut at `3fbc4b7c5`. The assigned branch `fix/scrub-key-order-217` is checked out in the caller's worktree, so it could not be checked out here, and its ref was left alone. It sits at `3fbc4b7c5`, so it can fast-forward to the last commit. Subfleet keeps the commits under `refs/subfleet-salvage/`.

- `894febf23` test: Bearer and header rules never take a credential key (D-SCR1)
- `2df07bd90` fix: Bearer and header rules leave credential keys to the named-value rules (D-SCR1). This includes C-23.14 and its change-log bullet.
- `b249a15a7` test: the key-order property also draws inline Authorization: Bearer headers
- this report, committed after them

## Status

Done.
- **Leaks:** the four review inputs, ten `Bearer` shapes and 24 header shapes leak at release/217 and are clean at head in every mode.
- **Fuzz:** the rebuilt fuzz leaks nothing at head (base: 709 and 734 triples).
- **Tests:** every hold passed, apart from one timing outlier that passed on re-run.

**Follow-ups for the hub:**
1. The inline-header limitation (`curl -H 'Authorization: Basic …'`, and a header first in a refusal's words under `detail_line`). It leaks today, at release/217 as at head.
2. Whether to guard the JWT, prefixed-token and base64 rules against token-shaped keys.
3. If wanted, a faster `Bearer` lookahead, to win back GIL margin at the work bound.

**Cleanup:** the worktree's `.venv`, bytecode caches and the scratch folder were removed after the last hold. No `.hypothesis` folder was left, and no process of this job is running. Nothing was pushed.
