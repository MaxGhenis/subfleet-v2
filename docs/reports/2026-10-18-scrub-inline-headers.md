# D-SCR2: inline credential headers are scrubbed, and #231's three P3s

Base: `473fe2941` on `release/217`, after #231 (D-SCR1, `16b24033a`) merged. Brief: `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-build.md`; its decisions come from that folder's `pr231-review-r1.md`, and section 5's from `~/reviews/subfleet-hub/daemon-log-scrub-2026-10-17/review-r3.md` ("Notes outside this change"). Work stayed in the job's worktree, with no Git remote operations and no sub-agents. Nothing was pushed.

The job ran in two attempts. Attempt a1 (from 11:40) wrote the tests, the fix, C-23.14 and a draft of this report. Its one pytest hold had not finished when the attempt ended. Attempt a2 resumed at 15:58 from the checkpoint `6729cd342`. It ran the hold, fixed the one failure the hold found, and finished this report (see "Holds" and "Status").

"Base" below means this branch's base (release/217 with #231). "release/217" means the scrubber before #231, which `tests/unit/scrub_release217.py` keeps.

## The rules

### 1. Inline credential headers (P2)

- **What is a header.** A header's name is an identifier ending in `authorization` or `cookie`, case-insensitive (`Authorization`, `Proxy-Authorization`, `X-Authorization`, `Cookie`, `Set-Cookie`). It takes a header's value, never a plain one.
  - **At a line start**, after whitespace only, it is `_HEADER_RE`'s, unchanged except that the name may be any such identifier, so `Proxy-Authorization:` at a line start is a header too.
  - **Anywhere else on a line**, unquoted and followed by spaces or tabs and `:` (not `=`), it is an inline header (`_scrub_inline_headers`).
- **Where an inline value starts:** at the first visible character after the colon. If nothing visible follows on that line, it starts on the next line, as the line-start rule's `\s*:\s*` reads it.
- **Where it ends,** whichever comes first:
  1. at the closing quote that matches one opening right before the name: the same kind of quote, escaped by a run of backslashes of the same length, on the value's line (`-H '…'`, `--header="…"`, JSON's `\"…\"`, a repr's `\'…\'`, JSON encoded twice);
  2. otherwise at the end of the value's line.
- **The effect:** the value is redacted whole and counted as a line-start header's, once per secret-looking run.
- **Guards:**
  - A value that already starts with `[REDACTED]` is left alone. This keeps scrubbing idempotent, and daemon.log's folded line keeps the words after a redacted header.
  - A credential key that ends the value, with its value past the line, is left to the named-value rules (`_key_past_line` over the value's span).
  - The value crosses a line break only right after the colon, where `_neutral_break` already refuses a cut.
- **Accepted cost:** an unquoted inline header loses the rest of its line, as a line-start one does. C-23.14 says so.

### 2. A secret that ends in a credential word (P3-1)

- **Headers** keep only the credential word for the named-value rules (`_key_past_line` returns the word's start, `_SENSITIVE_SUFFIX_RE`'s leftmost match). `Cookie: sid=SECRET7abc123token\n====` becomes `Cookie: [REDACTED] token\n=[REDACTED]`.
  - **The cost:** a compound key's own name before its word goes into the redaction. `Cookie: a=…; session_token=` and the value on the next line now keep `token=`, where #231 kept `session_token=`. A key that is itself one word in `_SENSITIVE_WORD` (`client_secret`, `access_token`) is kept whole. The secret and the count are unchanged, and C-23.14 states the cost.
- **`Bearer`:** when the run is refused as a key, the run before the credential word is redacted, its joiners (`._~+/-`) left with the word, when that run reads as a token: at least the 8 characters `_BEARER_RE` reads as one, with a letter and a digit. `invalid Bearer SECRET7a1b2c3d4_token: expired` becomes `invalid Bearer [REDACTED]_token: [REDACTED]`.
- **`mask_approval`** shares `_bearer_refusal`: `curl -H 'Authorization: Bearer SECRET7a1b2c3d4_token:' https://x` is shown as `curl -H 'Authorization: [masked bearer, 22 chars]_token:' https://x`.

### 3. `Bearer`'s cost (P3-2)

- `_BEARER_RE` is `\bBearer\s+([A-Za-z0-9._~+/-]{8,})=*`. `_bearer_refusal` decides in Python, once per match, from the run's end:
  - the run ends in a credential word and an assignment's separator follows;
  - or the run ends in a credential flag (the last `--` of its last identifier, then `_SENSITIVE_KEY`) that may own a value.
- Both branches of the old lookahead could succeed only where the run ends, so one look at the end is enough. Every check there is a C-speed scan: a 17-character suffix search, an anchored separator match, `str.rfind("--")` and two anchored full matches.

### 4. C-23.14

The clause now states:

- the line-start and inline header rules;
- the accepted prose cost;
- the `[REDACTED]` guard and what it costs (a text that already holds the marker before a secret keeps the secret);
- `Bearer`'s refusal, with section 2's token-before-the-word redaction, and that it costs what release/217's pattern does;
- the ownership rule below;
- the flag rule's own-line boundary;
- `curl -u`;
- the token-shaped-key boundary, unchanged.

A change-log bullet heads "Changes in version 3". The cut sentence adds that an inline value crosses a break only between its colon and its first visible character.

### 5. What another rule's redaction encloses still owns its value

**The rule.** A credential key, a credential flag or a `Bearer` that ends a header's value, a `Bearer` run or a plain value, and may own a value past it, is left to its own rule together with what follows it. Its value is then redacted exactly as it would be without the enclosing rule.

**Mechanism: the enclosing rule ends its match before the item.** That is the same kind of fix as #231's key fix, and it is the one that can promise "exactly as without". The item and what follows it are literally left in the text, so the item's own rule reads them later in the pass, as it would with no enclosure. A marker would need every item rule to learn a new input. Changing the pass order cannot help, because each enclosing rule must run before some item rule. A redact-through would re-implement the item rules inside each enclosing rule, which #231's report already rejected for keys.

- **Keys:** `_key_past_line`, as in #231, now for inline headers too, keeping only the credential word.
- **Flags** (`_owning_flag`): the last `--` of the identifier that ends the enclosed span, then `_SENSITIVE_KEY`, followed by whitespace or the text's end (`_owns_past`, one character, on the flag's line). It is used in three places:
  - the header rules (only when the value ends at its line's end, not at a closing quote);
  - `_bearer_refusal`'s flag branch, widened from #231's `[ \t]+[^\s-]`;
  - the plain value (`api_key=x--password SECRET7…`, which leaked in every mode at the base, value on the same line and all).
- **`Bearer`** that ends an inline header's value with its token on the next line. The inline rule runs before `Bearer`, so the inline rule leaves it.
- **Header names that another value swallows.** In the quoted pass's union, a header name and colon that the previous match leaves visible are left to the header rules. One that the match swallows gets a header's value, to its line's end. A key that a header keeps and that is itself a header's name is read as an inline header: by the inline rule straight away, and by its second scan after the line-start rule. Without these, a second scrub changed the first's output.

**The confirmed cause of #233 review r3c.** The line-start header redacted `x --password` to its line's end, so the flag rule never saw `--password`, and daemon.log's fold then put the next line's `SECRET7p` after `Authorization: [REDACTED]`, where no rule reads it.

**A correction to the premise.** "Without the header, the flag rule hides `SECRET7p`" holds only for daemon.log's folded line. The flag rule reads a value on the flag's own line only (`_FLAG_VALUE_RE`). So `x --password\nSECRET7p` keeps the secret in the whole-text, excerpt and cut modes, at the base and now, with an enclosing rule or without one. Under the hub's rule ("exactly as without the enclosing rule"), those cells still show the value. That is pinned as a documented limitation and listed as follow-up 2.

### The composition matrix

5 enclosures, 5 swallowed items and 4 placements. The value is `SECRET7…` after LF, CRLF, LF and two spaces, or CRLF and two spaces. Modes: excerpt, whole, daemon.log line, `detail_line`, and cut blocks (head and tail excerpts cut at each line break, with 4,096-character excerpts). "Same as bare" means the result equals the twin text without the enclosing rule, mode by mode.

| Enclosure | Swallowed item | Base: whole, excerpt, cut | Base: daemon.log line and detail | Now |
|---|---|---|---|---|
| line-start header `Authorization: x …` | `--password`, `--auth-token`, `--client-secret` | shows (so does bare) | **shows (bare hides)** | folded modes hide; others same as bare (show) |
| line-start header | `token:`, `password=` | hidden | hidden | hidden in every mode |
| inline header `sent Authorization: x …` | flags | shows (so does bare) | hidden: not a header at the base | folded modes hide; others same as bare |
| inline header | keys | hidden | hidden | hidden in every mode |
| quoted inline header `curl -H 'Authorization: x …` (unclosed on its line) | flags | shows (so does bare) | hidden: not a header at the base | folded modes hide; others same as bare |
| quoted inline header | keys | hidden | hidden | hidden in every mode |
| `Bearer x.y.z…` | flags | shows (so does bare) | **shows (bare hides)** | folded modes hide; others same as bare |
| `Bearer` | keys | hidden | hidden | hidden in every mode |
| named value `api_key=xyz…` | flags | shows (so does bare) | **shows (bare hides)** | folded modes hide; others same as bare |
| named value | keys | hidden | hidden | hidden in every mode |

Each row holds all four placements, and they agree.
- **At the base:** 36 cells failed the "same as bare" check (the three bold rows: 3 flags × 4 placements each).
- **Now:** all 100 cells equal their twins in every mode, and no key cell shows its value in any mode.
- **Outside the matrix:** a plain value that ends in a flag with its value on the same line (`api_key=xyz--password SECRET7… rest`) showed the value in every mode at the base and is hidden now.

## Decisions and why

- **Section 2, `Bearer`: redact the run before the word, when it reads as a token** (the brief's first choice, with one condition).
  - **The condition:** at least 8 characters, with a letter and a digit.
  - **Why the condition:** redacting unconditionally is not unsafe, since it never shows more. But it turns #231's pinned key paths into over-redaction: `type: bearer\nsecrets/db_password: …` would lose `secrets/db` and its line break (the `Bearer` replacement folds the whitespace). A key's own name before its word (`secrets/db_`, `oauth.`, `service_account_`, `abc123_`) has no digit or is short, while a real token almost always has both. The residual case, a token of 8 or more letters with no digit that ends in a credential word followed by a separator, is about 3% of an event the reviewer put at 1 in 10⁷. C-23.14 states the condition.
  - **What it preserves:** every #231 `Bearer` case keeps its exact output. The `Bearer` keeps the word's joiners (`_token`, `.client_secret`) with the word, so the named-value rules read the same key.
- **Headers keep only the word, with no token-likeness condition** (the reviewer's tested fix, which the brief adopts).
  - **Why no condition here:** unlike `Bearer`, the word-only cut costs a header nothing structural. The line break and the key's separator stay, so the named-value rules read the same key and value. The only loss is a compound key's name before its word (`session_`).
  - **Why not a condition anyway:** a condition would show a secret glued to the word whenever the secret lacked a digit or a letter.
  - **What changed in #231's tests:** the hold found one #231 expectation that pinned `session_token=` (`test_a_header_redacts_what_precedes_the_key_and_counts_each_value`). It now expects `token=`, and its idempotence check covers that output and the `client_secret` one (commit `c3c105d8b`).
- **The guard is `[REDACTED]` only, not every placeholder.**
  - The inline rule runs first among the token rules, before the JWT, prefixed-token, `Bearer` and base64 rules. So a `[REDACTED]` at an inline value's start is one the text already held, never one a JWT or prefixed token produced this pass, which would leave the rest of the value unhidden.
  - The block rules do run before it. So a `[BASE64 DATA OMITTED]` or `[PRIVATE KEY REDACTED]` at a value's start is redacted with the rest, which is still idempotent: the result starts with `[REDACTED]`.
- **Line-start headers keep their behaviour, counts included** (`test_an_ordinary_header_is_redacted_and_counted_exactly_as_before` is unchanged). The inline rule skips every name that starts its line, so the two partition the names.
- **`_HEADER_RE` takes any name ending in `authorization` or `cookie`.** This makes `Proxy-Authorization` at a line start case 2 of the reviewer's rule.
- **An escaped closing quote ends the value at its backslashes.** The plain rule, which then reads the header's name as a key, takes those backslashes as its value's end, as it always took a trailing backslash. So `\"Authorization: Basic …\"` scrubs to `\"Authorization: [REDACTED]"`: hidden and stable, with one backslash fewer.
- **`_neutral_break` needs no change.** No new rule looks past a line break except the inline value between its colon and its first visible character. That is where `_neutral_break` already refuses a cut, because every header name ends in a sensitive word and so is a key to `_key_ends_at`. The ownership checks read one character after the item, on its own line; the closing-quote search and the line-start test stay on the value's line. Shown by placing cuts, below.

## Invariants (corrected)

Each holds for every input; each is executed by the tests named.

1. **No credential value survives**, in any mode:
   - the modes: `scrub_secrets`, the whole-text scrub, `scrub_bounded` with head and tail excerpts cut at each of the text's line breaks, `refusals.scrubbed_line` and `refusals.detail_line`;
   - the values: any value the named-value, flag, `Bearer`, line-start header or inline header rules define, including a secret that ends in a credential word.
   - **Exceptions, documented:**
     - a credential flag's value on the next line, outside daemon.log's folded line;
     - `curl -u user:…`;
     - a key shaped like a token;
     - a text that already holds `[REDACTED]` at an inline header's value before a secret.
   - **Correction:** #231's report stated this invariant without the secret-ending-in-a-credential-word clause, and it was false for those secrets at #231's head. The differential's generator dropped them; it no longer does, and a fifth of its secrets end in a credential word. #231's report now carries a dated correction under its invariant 1 (`docs/reports/2026-10-17-scrub-key-order.md`), its original text kept.
   - Tested by the property, every regression, the matrix and the differential.
2. **Idempotence:** scrubbing a scrubbed text changes nothing, in the excerpt, whole and daemon.log modes. Tested by the property, and by `_assert_stable` in each regression.
3. **Excerpt agreement:** for every line break `_neutral_break` allows, `_scrub(t[:p]) + _scrub(t[p:]) == _scrub(t)`, and each `scrub_bounded` excerpt equals the matching part of the whole block's scrub. Tested by the property.
4. **Monotone against release/217:** on the seeded fuzz, every (text, mode, secret) triple the head leaks, release/217 leaks too. The head leaks no inline-header, line-start-header, `Bearer` or credential-word triple. Tested by the differential.
5. **Ownership:** a key, flag or `Bearer` an enclosing value would take gets, mode by mode, what its twin without the enclosure gets. Tested by the matrix.
6. **Linear work:** four times the input takes well under 8× the CPU, for the inline rule and `Bearer` (11 adversarial units) and for #231's units.
7. **`Bearer`'s constant factor:** at the work bound, the `Bearer` step costs under 4× release/217's pattern on the same text, measured alongside it in one child, and a 1 MiB refusal through `detail_line` costs under 3× release/217's. Tested by `test_the_bearer_rule_costs_what_release_217s_did` and `test_a_mebibyte_refusal_costs_daemon_log_what_release_217_did`.
8. **Refusal equivalence:** `_bearer_refusal` accepts exactly the matches #231's lookahead did, with its flag branch widened as section 5 widens it. Tested by `test_bearer_refuses_exactly_what_the_lookahead_did`.
9. **Unchanged where nothing changed:** ordinary line-start headers, inline `Bearer` tokens outside a header, and non-headers (a quoted key, `authorizations:`, `=`, an empty quoted value) scrub exactly as at release/217, output and count.

## Fuzz and differential numbers

**The generator.** `_fuzz_text` in `tests/unit/test_scrub_key_order.py`:
- assignments (45%), inline headers (15%: 7 quoting forms × 6 names × 4 colons × 6 value forms) and filler (40%), with `====`, `:authority: example.com` and `Proxy-Authorization:` among the fillers;
- a fifth of the secrets end in one of 8 credential words.

Scratch runs: 4,000 texts per seed, three modes (whole, `clean(text, 4000)`, daemon.log line).

| Tree | Seed 225: triples (texts) | Seed 2026: triples (texts) |
|---|---|---|
| release/217 | 2,596 (809): inline-header 1,476, credential-word 547, header 489, `Bearer` 84 | 2,693 (828): 1,551, 582, 461, 99 |
| #231's head (this branch's base) | 2,483 (768): 1,468, 534, 451, 30 | 2,600 (794): 1,546, 567, 436, 51 |
| Head | **0 (0)** | **0 (0)** |

On both seeds:
- head minus release/217 and head minus #231's head are empty;
- #231's head minus release/217 holds 6 and 5 triples, the P3-1 kind (credential-word secrets) and inline headers;
- idempotence: 0 failures in 3 modes;
- splits: 0 disagreements over 4,729 and 4,577 neutral cuts.

**How the fuzz shaped the code.** Before the last three fixes of section 5, the same seed showed what they fix:
- 63, then 41 head triples, all also leaked by release/217: a header name that the quoted pass's union read as a plain key;
- 6, then 24 idempotence failures: a kept key that is a header's name, which a second scrub then read as an inline header.

The committed differential test (seed 225, 4,000 texts, against `scrub_release217`) asserts:
- release/217 leaks more than 50 `Bearer`, 20 header and 50 inline-header triples;
- head minus release/217 is empty;
- head leaks no triple of any kind but "other".

**The property** (Hypothesis, ci profile: 200 examples, derandomized, no database) also passed:
- 6 scratch runs of 400 random examples each (seeds 11 to 16);
- before the `Bearer`-ends-an-inline-value fix, seed 13 found `Authorization: ok Bearer\nSECRET70000`, whose token showed through `detail_line`, and that case is now a regression.

**The callback agrees with #231's lookahead:**
- 0 disagreements over 45,260 texts (every text of up to three pieces after each scheme word, and 20,000 seeded ones), with #231's flag branch;
- with section 5's widened branch the two differ on 531 of them, all a flag followed by a line break, other whitespace or the text's end;
- the committed test compares against the lookahead widened the same way.

## Timings

CPU milliseconds (`thread_time`), best of 5 (3 for the 1 MiB row), at host load 48 to 104, on AC power. The text is `"Bearer " + unit` repeated to the 262,144-character bound. The heartbeat gap is the longest CPU gap a 1 ms heartbeat thread saw during a whole `scrub_secrets`, best of three runs.

| Unit | `Bearer` step: head / #231 / release/217 | Heartbeat gap: head / #231 / release/217 |
|---|---|---|
| `--s` | 2.8 / 73.7 / 2.7 | 6.8 / 69.0 / 8.8 |
| `~--a_` | 2.5 / 58.3 / 2.4 | 8.7 / 58.5 / 12.1 |
| `--client_` | 2.7 / 63.9 / 1.5 | 12.3 / 69.6 / 9.9 |
| `--a` | 2.5 / 73.7 / 1.8 | 7.6 / 79.6 / 6.5 |
| `a_` | 2.8 / 43.9 / 3.0 | 6.5 / 48.4 / 6.3 |
| `token` | 2.5 / 43.3 / 2.4 | 8.6 / 44.4 / 8.7 |
| `x.` | 2.2 / 39.4 / 2.4 | 10.0 / 36.4 / 12.7 |

- **The 1 MiB refusal through `detail_line`** (`"Bearer " + "--s"` repeated): head 72.0 ms, #231 300.5 ms, release/217 52.9 ms. The reviewer measured 241 ms against 48.6 ms. Head is 1.4× release/217 there; the inline rule's case-insensitive word search alone costs about 17 ms per MiB.
- **4× input:** whole scrubs of `"Bearer " + "--s"` repeated take 3.9 ms at 65,536 and 17.5 ms at 262,144 characters, 4.5×; of `token`, 4.4 → 17.4 ms, 4.0×. The linearity tests' own ratios (32,768 → 131,072) ran 3.7× to 4.5× over the 11 units in one direct run.
- **The tests' ratios:** the `Bearer` step read 0.97× to 1.27× release/217 at head, against 13× to 34× under #231's lookahead (bound: 4). The 1 MiB refusal read 1.8× before the inline word search was made a plain scan and 5.2× under the lookahead (bound: 3). The heartbeat read 14.6 and 21.4 ms against 84.4 ms (bound: 50 ms).
- **One cost was found and fixed while building.**
  - The first inline scan anchored its name regex at identifier starts. On a single 1 MiB identifier that one C call took 40 ms.
  - It now searches for the header word (a plain case-insensitive scan), then reads the identifier back on the reversed text.
  - A first `_owning_flag` walked identifiers in Python, which took 40 to 60 ms at the bound. It now uses `str.rfind` and anchored matches.

## Mutations

Each was applied in-process, by executing the mutated module source into `subfleet.sessions.handoff`'s namespace, and the named tests were then called directly. The lookahead mutation ran its child probes from a scratch copy of the package, so no file in the worktree changed while a hold was queued.

| Mutation | Named tests that fail |
|---|---|
| Drop the inline rule | `test_an_inline_header_loses_its_value_in_every_mode` (15 of 18), `…_leaves_a_key_whose_value_is_past_its_line`, `…_counts_as_a_line_start_one` (2), `test_a_line_start_header_is_inline_once_detail_line_prefixes_it`, `test_daemon_logs_folded_line_keeps_the_words_after_a_redacted_header`, `test_a_secret_ending_in_a_credential_word_stays_hidden` (2), `test_a_header_that_does_not_start_a_line_loses_its_value` (2), the property, the differential |
| End an inline value at whitespace | `test_an_inline_header_loses_its_value_in_every_mode` (15), the line-start/`detail_line` test, the count test, the folded-line test, one credential-word case, both `…_does_not_start_a_line_…` cases, the property, the differential |
| #231's key-past-line (whole identifier) | `test_a_secret_ending_in_a_credential_word_stays_hidden` (3 header cases), the differential |
| Restore the regex lookahead | `test_the_bearer_rule_costs_what_release_217s_did` (7 of 7), `test_a_mebibyte_refusal_costs_daemon_log_what_release_217_did`, `test_the_bearer_rule_keeps_other_threads_responsive_at_the_bound` (2 of 2), one `test_the_bearer_rule_keeps_scrubbing_linear` case |
| A header swallows a flag again | `test_a_swallowed_flag_or_key_still_owns_its_next_line_value` (36), `test_a_flag_left_by_its_enclosure_is_read_by_the_flag_rule` (2) |
| A header swallows a line-ending `Bearer` | `test_a_bearer_that_ends_an_inline_value_keeps_its_next_line_token` (3) |
| `Bearer` swallows a flag (#231's same-line flag branch) | the matrix (12), `…_left_by_its_enclosure_…` (1) |
| A plain value swallows a flag | the matrix (12), `…_left_by_its_enclosure_…` (2) |

## Tests

**`tests/unit/test_scrub_inline_headers.py`** (new, 173 cases, counted from the hold's JUnit file). Its regressions leaked at the base. Its guards and documented limitations pin behaviour that must not change:
- **18 inline headers:** every quoting form, three names beyond `Authorization`, a value on the next line, a tab before the colon, two headers on a line, an unclosed quote and an escaped inner quote. Checked in every mode, then for idempotence.
- **Guards:** the `detail_line` case of a line-start header; 4 count cases; daemon.log's folded line; 5 non-headers equal to release/217; 3 inline key-past-line cases.
- **Section 2:** 5 credential-word endings, the approval masker on its own (the masked case and 3 key-shaped runs it still shows), and one case (two inputs) where a key after `Bearer` keeps its name.
- **Section 5:** the 100-cell matrix, 3 ownership cases, and 3 `Bearer`-ends-an-inline-value cases.
- **Documented limitations:** 2 for a flag's next-line value, and `curl -u`.
- **Section 3:** 7 `Bearer` cost ratios, the 1 MiB refusal, 2 heartbeats, 11 linearity units, and the callback-against-lookahead check.
- **At the base** (run without pytest, by calling the test functions with base's `handoff.py` and `redact.py` in place), 85 of the 152 cases that are not timing probes fail. Of the 10 cost cases, 9 fail there: the 7 `Bearer` ratios (13× to 34×), the 1 MiB refusal (5.2×) and the `~--a_` heartbeat (84.4 ms). The `--s` heartbeat read under its 50 ms bound in that run; under the restored-lookahead mutation it failed too.

**`tests/unit/test_scrub_key_order.py`** (changed, still 70 cases):
- #231's header example expects `token=`, not `session_token=` (section 2's cost, fixed after the hold);
- the property draws inline headers in 7 quoting forms, 5 header names (`Proxy-Authorization` and `X-Authorization` among them), secrets that end in a credential word, and separator-first lines; it checks `detail_line` on every text;
- the differential's generator no longer drops credential-word secrets (`:405`) and adds inline headers;
- the two `curl`/inline `Bearer` cases now expect the header's whole value redacted;
- D-SCR1's inline-header limitation test is now a regression;
- the lookahead linearity test is renamed for the callback.

**`tests/cpu_scaling.py`** gains `best_cpu_ratio`. It compares a call's CPU with a reference measured alongside it, so machine speed cancels and no absolute budget is set (D-TM1). **`tests/unit/test_cpu_scaling.py`** pins its scope with one case.

## Holds

Every hold used the shared suite lock (`lockf -k`, no `-t`), the worktree's GIL CPython 3.14.7 `.venv` (`sys._is_gil_enabled()` true), `env -u PYTHONPATH -u SUBFLEET_RETENTION_DORMANT`, scratch `TMPDIR` and `HYPOTHESIS_STORAGE_DIRECTORY`, and an in-process 19-minute `SIGALRM`. Each ran in the foreground, on AC power.

| Hold | Queued | Started | Ended | What ran | Result |
|---|---|---|---|---|---|
| a1 | 13:59:18 | 15:00:35 (load 61) | did not finish: attempt a1 ended | the brief's first group | none recorded |
| a2 | 16:01:48 | 16:12:32 (load 61, 93 GB free) | 16:24:57 | all three of the brief's groups and both new or changed test files (below), one pytest | **1,390 passed, 1 failed**, 12 m 10 s |

**Why hold a2 merged the three groups.** Attempt a2 resumed at 4 h 18 m on the job's clock, which left about 12 minutes before the brief's 4 h 30 m mark for starting holds. It still ran one pytest in one hold, under 20 minutes.

**Hold a2, per file** (JUnit):

| File | Passed | Failed | Seconds |
|---|---|---|---|
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 2.9 |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 18.5 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 6.6 |
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 44.0 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 14.9 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0.9 |
| `tests/unit/test_cpu_scaling.py` | 3 | 0 | 10.1 |
| `tests/unit/test_scrub_inline_headers.py` | 173 | 0 | 37.9 |
| `tests/unit/test_scrub_key_order.py` | 69 | 1 | 25.3 |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 57.6 |
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 465.3 |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 10.6 |
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 1.4 |

- **The cost tests passed at load 61:** all 7 `Bearer` ratios, the 1 MiB refusal, both heartbeats and the 11 linearity units.
- **The one failure** was #231's `test_a_header_redacts_what_precedes_the_key_and_counts_each_value`. It pinned `Cookie: [REDACTED] session_token=` and the code gives `Cookie: [REDACTED] token=`. That is section 2's adopted header fix doing what it says, so the expectation moved (see "Decisions"). The secret and the count were already right.
- **After that fix (`c3c105d8b`), no further hold ran,** because the 4 h 30 m mark had passed. These ran as plain function calls outside pytest and outside the lock:
  - the changed test, which passes;
  - every test in `tests/unit/test_invariants_index.py` and `tests/unit/test_desktop_ledger.py`, which parse `docs/acceptance-contract.md` (29 of 29 pass, module fixtures resolved by hand). Two commits here edit C-23.14, and the brief's holds don't include those files.

**A spot check of the brief's own inputs.** The 13 inputs from sections 1, 2 and 5 went through five modes (excerpt, whole, `scrub_bounded`, `scrubbed_line` and `detail_line`), plus the approval through `mask_approval`. This was a plain script, run on the head and on a `git archive` of the base.
- **At the base:** 59 (input, mode) pairs show `SECRET7`, and the approval shows its token.
- **At the head:** 3 pairs show it: `intro\nAuthorization: x --password\nSECRET7p rest\ntail` in the excerpt, whole and `scrub_bounded` modes. Its twin without the header shows the value in the same three modes, so this is follow-up 2's documented limitation. The folded modes, which leaked at the base, hide it now.

## Commits

The assigned branch `fix/scrub-inline-headers-217` is checked out in the caller's worktree, so the commits are on a detached HEAD in the job's worktree, cut at `473fe2941`. That branch sits at `473fe2941`, so it can fast-forward to the last commit. Subfleet keeps the commits under `refs/subfleet-salvage/`.

- `988a1d11d` test: inline headers, credential-word endings, Bearer's cost and swallowed flags (D-SCR2)
- `8ed254385` fix: inline credential headers are scrubbed; Bearer decides once per match; swallowed flags keep their values (D-SCR2)
- `6729cd342` docs: C-23.14 states the inline header rule, its prose cost, Bearer's refusal and what a swallowed flag keeps (D-SCR2)
- `c3c105d8b` test: #231's header example keeps only the credential word; C-23.14 names the cost (D-SCR2)
- this report, with the dated correction to #231's report, committed after them

## Status

- **Done:** sections 1 to 5 of the brief, the property, the differential, the mutations, the timings, the correction to #231's invariant 1 and this report.
- **Ran under pytest:** the brief's three groups and both new or changed test files, in one hold at `6729cd342`, with 1 failure, fixed by `c3c105d8b`.
- **Not run under pytest at the final head (`c3c105d8b` and this report):**
  - the full hold. After it, only one test's expected strings, one contract sentence and the two reports changed, and no code. The changed test passes as a plain call.
  - `tests/unit/test_invariants_index.py` and `tests/unit/test_desktop_ledger.py`, which pass as plain calls (29 of 29).
- **Ran in-process in attempt a1, not as pytest runs:** the mutations, the base runs of the new tests, and the timing and fuzz scratch runs. That attempt's scratch scripts produced the numbers above; its scratch has since been removed.

## Follow-ups for the hub

1. **`curl -u user:SECRET…`** has no rule (pinned: `test_curl_user_password_has_no_rule_documented_limitation`).
2. **A credential flag whose value is on the next line** keeps it outside daemon.log's folded line, with or without an enclosing rule. It is pinned. Two shapes:
   - `x --password\nSECRET7…`, bare or enclosed;
   - the realistic one, a shell continuation `--password \` and the value on the next line: the flag rule takes the backslash as its value, so the next line's secret shows in every mode, the folded line included.
   Closing either needs a decision on whether a line-ending flag takes the next line's first word (help text such as `--password\n    The password to use.` would lose `The`), and `_neutral_break` would then refuse a break after a line-ending credential flag.
3. **The token-shaped-key boundary** (from #231), unchanged.
