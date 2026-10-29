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
  - *Correction (fix round 1, 2026-10-22):* this did not hold at `d3a19518b`. A line-start header whose whole value is the next line's header name left that name to a rule that takes one word (#239 review r1, P2-1), and the quoted pass's header value swallowed a `Bearer` or flag ending its line (P3-1). See "Fix round 1" below; the text above is kept as written.

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
  - *Correction (fix round 1, 2026-10-22):* the inline rule's second scan now reads names at a line start too, so the two no longer partition them. Ordinary line-start headers still scrub and count exactly as at release/217 (that test is unchanged and passes). A line-start header whose whole value is a header's name now leaves the name to the inline rule (P2-1).
- **`_HEADER_RE` takes any name ending in `authorization` or `cookie`.** This makes `Proxy-Authorization` at a line start case 2 of the reviewer's rule.
- **An escaped closing quote ends the value at its backslashes.** The plain rule, which then reads the header's name as a key, takes those backslashes as its value's end, as it always took a trailing backslash. So `\"Authorization: Basic …\"` scrubs to `\"Authorization: [REDACTED]"`: hidden and stable, with one backslash fewer.
- **`_neutral_break` needs no change.** No new rule looks past a line break except the inline value between its colon and its first visible character. That is where `_neutral_break` already refuses a cut, because every header name ends in a sensitive word and so is a key to `_key_ends_at`. The ownership checks read one character after the item, on its own line; the closing-quote search and the line-start test stay on the value's line. Shown by placing cuts, below.

## Invariants (corrected)

*Fix round 1 (2026-10-22):* #239 review r1 executed counterexamples to invariant 1 (the refused-`Bearer` converse against release/217; P2-1, P3-1 and P3-2 against 473fe2941), 2 (P3-4), 3 (P3-3) and 5 (P3-5). The text below is kept as written. The round's four invariants, which replace these, are under "Fix round 1".

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

## Fix round 1 (#239 review r1, 2026-10-22)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix1.md`. **Review:** that folder's `review-r1.md`, REQUEST_CHANGES with one P2 and seven P3s, every one executed, and a prototype fix (A to D) that was not kept. This round wrote A to D from the review's description, added a fifth change (E) for P3-5, and corrected C-23.14 and this report. The work stayed in the job's worktree, with no Git remote operations, no sub-agents and nothing pushed. All test values are dummy `SECRET7…` strings.

### The merge

`origin/release/217` at `e345f0803` (#212, #223, #232, #238 and #243), as the hub last fetched it, merged in `db97df9a4`.
- **One conflict:** `docs/acceptance-contract.md`, "Changes in version 3". Both sides had added bullets at its top. Every bullet from both sides is kept: this branch's C-23.14 bullet first, then release's bullets in release's order.
- **No clause body conflicted;** release's edits to other clauses merged cleanly.
- **No overlap with the scrubber:** release changed none of this PR's files: `subfleet/sessions/handoff.py`, `subfleet/conversations/redact.py`, the daemon.log paths (`subfleet/refusals.py`, `subfleet/conversations/suggestions.py`), the scrub tests, `tests/cpu_scaling.py` and this report.

### Findings: cause and fix

**P2-1: a header name a line-start header swallows.**
- **Cause:** `_HEADER_RE` reads a value on the next line when its own line has none, so `X-Authorization:` took `Authorization:` as its value. That value is a key whose value is past its line (`_key_past_line`), so `_header_redaction` left it whole. The inline rule's second scan ran only when `_HEADER_RE` had changed the text, and skipped a name that starts its line anyway. So the plain rule read `Authorization` as a key and took one word of its value (`Basic`).
- **Fix C:** the second scan always runs after `_HEADER_RE`, with `line_start=True`, so it reads names at a line start.
- **Fix D:** in `_header_redaction`, with `rescan` set and a kept key that is the whole value and a header's name, return `text[start:key], key`. The scan stops before the name and then reads it as a header of its own.
- **Result:** `X-Authorization:\n[REDACTED]\n  [REDACTED]`, as 473fe2941 gives. The `Authorization:\nCookie:` shape, which leaked at 473fe2941 too (from #231), is closed as well.

**P3-1: the quoted pass's header extension swallowed a line-ending `Bearer` or flag.**
- **Cause:** for a header name inside an unclosed quoted value, the extension runs the value to the line break. It then asked `_owning_flag` about the break itself, not the last visible character, and never checked `Bearer`.
- **Fix A:** the extension asks `_owned_item` (`_owning_flag`, then `_BEARER_END_RE`) at the line's last visible character, as `_header_redaction` does. The value then ends at the visible end before the item, so whitespace between them stays.
- **Result:** `secret:'Cookie:\nx Bearer\nSECRET7…` becomes `secret:[REDACTED] Bearer [REDACTED]`, as at 473fe2941. The trailing-space flag case scrubs exactly as at 473fe2941, so its daemon.log line hides the value again.

**P3-2: a flag glued to another flag's value.**
- **Cause:** the plain rule leaves a value that is itself a credential flag (`api_key=--password`) to the flag rule. The flag rule's value then swallowed `x--token`, and it skipped any identifier starting inside the previous value. So `--token`'s own value showed.
- **Fix B:** the flag pass skips a flag only when the flag itself (its last `--`) starts inside the previous flag's value. A value ends before a credential flag glued to its end (`_owning_flag` over the value), as a plain value's already did.
- **Result:** `api_key=--password [REDACTED]--token [REDACTED]`. The `BEARER …credentials--password z--client-secret  SECRET7…` input, which leaked at 473fe2941 too (release/217 hid it), is closed.

**P3-3: excerpts disagreed with the whole scrub.**
- **Cause:** after `Bearer` joins `sent Authorization: Bearer` to its token's line, only the second inline scan could take the rest of the joined line. That scan ran only if some line-start header anywhere in the text had changed. A whole scrub with `Cookie: a` in its head read the line again; the tail excerpt, without it, did not.
- **Fix C:** the second scan always runs, so whether a line is read again never depends on the rest of the text.
- **Regression:** at the real bound (308,144 characters, the header 710 characters from the end), the excerpt and the whole scrub both hide both secrets, and the tail excerpt is the whole scrub's last lines.

**P3-4: idempotence.**
- **Cause:** in `Bearer\nabc1pi_keyAuthorization:--secret x`, a partial `Bearer` cut (`_bearer_refusal` redacts the token-like run before the credential word) left `Authorization:` after a placeholder. The first inline scan had skipped that name as a line start, and `_HEADER_RE` cannot match after `[REDACTED]`. So only a second scrub read the header.
- **Fix C:** the second scan reads it in the same scrub, giving `Bearer\n[REDACTED]Authorization:[REDACTED]` once and for all.

**P3-5: an unclosed quoted value swallowed a line-ending `Bearer` or flag.**
- **Cause:** a quoted value with no closing quote is redacted to its line's end, `Bearer` and flags included. This leaked at release/217, at 473fe2941 and at `d3a19518b` alike.
- **Fix E (closed rather than documented):** the unclosed value ends before a flag or `Bearer` at its line's last visible character (`_owned_item`), keeping the whitespace. When the value is the item alone, the candidate is left to the item's rule, as the plain branch does.
- **Result:** `api_key='xyz Bearer\nSECRET7…` scrubs exactly as its twin `api_key=xyz Bearer\nSECRET7…` does, in every mode. That covers single, double and escaped quotes, LF and CRLF, and indented next lines.
- **What still shows:** a flag's next-line value shows outside daemon.log's folded line, exactly as for the twin (the flag rule's own-line boundary, below).

**P3-6: C-23.14 and this report overstated.** Corrected in C-23.14 (change-log entry of 2026-10-22):
- **The swallowed-header sentence** is replaced. A header's name is a key whose rule is the inline header rule, which reads the text again after the line-start rule, names at a line start included, always.
- **The excerpt sentence** now says what holds: each excerpt scrubs as the within-line rules scrub the whole block there. A whole-text scrub, which also masks a quoted value across line breaks, can remove more. For example, in `api_key='x\nSECRET7… y'` the whole-text scrub hides the secret and an excerpt shows it, as at 473fe2941.
- **The flag sentence** now says "a later line, past any number of blank lines" (daemon.log's fold joins every run of whitespace). It also says that a shell continuation (`--password \` and the value on the next line) shows in every mode, the folded line included, because the flag rule takes the backslash as the flag's value. That case is not fixed here; it stays follow-up 2.
- **The refused-`Bearer` converse is stated:** `invalid Bearer SECRET7_token: expired` keeps the seven-character `SECRET7`, as at 473fe2941; release/217 hid it by taking the key too.
- **This report:** dated corrections sit under the old "Header names that another value swallows" and "Line-start headers keep their behaviour" bullets and the old invariants. Each original text is kept.

**P3-7:** `_matrix()` is wrapped in `tuple()` in `tests/unit/test_scrub_inline_headers.py`, as is the new `_unclosed_matrix()`. The hold has no `PytestRemovedIn10Warning`.

**Cost of C.** A second inline scan of every text would have added the inline rule's whole cost again. That rule's case-folded forward search for `authorization|cookie` cost 12 to 20 ms per MiB (scratch probe, under the wrapper). The scan now reads the reversed text from each `:`, which the regex engine finds by a plain search. The reversal and the scan together cost 1.2 to 2.5 ms per MiB, so two scans cost well under the one they replace. The new last-visible reads (A and E) share one walk per position (a memo in `_scrub_named_values`), since an unclosed value per kind of escaped quote can put hundreds of candidates on one line.

### Invariants

*Correction (fix round 2, 2026-10-23):* #239 review r2 executed counterexamples to three of these at `6bda95d54`. Invariant 1's one stated exception was not the only one: P2-1 to P2-3 showed values 473fe2941 hides, and check 2 found two more that release/217 hid. Invariant 2 is false, at 473fe2941 too (`cookie:AUTHORIZATION :` with `E` on the next line scrubs differently twice); round 2 replaces it with monotonicity. Invariant 4 failed for a line-ending `Bearer` that is a key's value inside an unclosed quote (P3-2). The text below is kept as written; round 2's five invariants, under "Fix round 2", replace these.

Each is stated for every input, with its stated exceptions, and executed by the tests named.

1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or release/217 hides. The modes are the excerpt and whole-text scrubs, `clean`, daemon.log's line alone and as a refusal's detail, and blocks cut at each line break.
   - **The one stated exception:** the refused-`Bearer` converse, which 473fe2941 shows too and release/217 hid. A run before a credential word that does not read as a token (fewer than eight characters, or no letter or no digit) is shown with its key (`test_a_refused_bearers_short_run_before_a_credential_word_shows_documented_limitation`).
   - **Tested by:** `test_scrub_no_new_leak.py` (both generators and the property, against both references, in the five modes but the cut blocks, which the split check of invariant 3 covers), `_new_leaks` (all modes, cut blocks included) in every round-1 regression, and the scratch run with the cut blocks below.
2. **Idempotence:** a second scrub changes nothing, in the excerpt, whole-text and daemon.log-line modes. Tested by the same sweeps and property, `_assert_stable` in the regressions, and the P3-4 regression.
3. **Excerpts agree with the whole:** at every line break `_neutral_break` allows, `_scrub(t[:p]) + _scrub(t[p:]) == _scrub(t)`, and each `scrub_bounded` excerpt is the matching part of the block's scrub. So an excerpt never shows a value the within-line rules over the whole block remove. A whole-text scrub can remove more (multi-line quoted values).
   - **Tested by:** the sweeps and the property (every allowed cut), #231's property, and the real-bound regression, which also checks the whole-text scrub there.
4. **Ownership:** a key, credential flag or `Bearer` that ends an enclosing value is redacted as its bare twin is, mode by mode. The enclosing values are a header's value, a `Bearer` run, a plain value, a flag's value, or an unclosed quoted value.
   - **The stated limits in C-23.14:** a flag reads its value on its own line only, so a value on a later line shows outside daemon.log's folded line, enclosed or bare alike; a shell continuation shows everywhere.
   - **Tested by:** the 100-cell section 5 matrix, the 72-cell unclosed matrix (`test_an_unclosed_quote_does_not_take_what_a_swallowed_item_owns`), and the P3-1, P3-2 and P3-5 regressions.

### Fuzz and differential

**The generators.**
- **The builder's:** `_fuzz_text`, seeds 225 and 2026, 4,000 texts each.
- **The review's, reconstructed:** its code was not kept, so `_review_text` in `tests/unit/test_scrub_no_new_leak.py` is built from the shapes the review executed. Those shapes are: unclosed quotes around header names, a `Bearer` or flag ending a line (with trailing spaces and carriage returns), glued flags, header names on consecutive lines, YAML pairs, partial `Bearer` cuts, blank lines and a shell continuation.
- **Seeds:** the review's 4101, 4102 and 9001, plus this round's 239 and 2392, at 1,800 texts each, so the review's three seeds make up its 9,000 texts per tree.
- **Its counts are its own.** It is not the review's generator, and the review's 113 split disagreements cannot be reproduced.

**What each text is checked for:**
- head-only leaks against each reference in five modes (excerpt, whole, `clean`, daemon.log line, daemon.log detail);
- idempotence in three modes;
- split agreement at every allowed cut.

Scratch run, outside the lock:

| Generator, seed | Texts / secrets / allowed cuts | `d3a19518b`: leaks vs 473fe2941 / vs release/217 (texts) | `d3a19518b`: idempotence failures | `d3a19518b`: split texts (cuts) | Head: all four |
|---|---|---|---|---|---|
| review, 4101 | 1,800 / 2,964 / 3,930 | 35 / 64 (15) | 66 | 9 (16) | 0 |
| review, 4102 | 1,800 / 2,839 / 3,909 | 43 / 51 (14) | 68 | 17 (31) | 0 |
| review, 9001 | 1,800 / 2,913 / 3,956 | 35 / 45 (10) | 74 | 12 (22) | 0 |
| review, 239 | 1,800 / 2,992 / 3,950 | 6 / 30 (7) | 60 | 17 (28) | 0 |
| review, 2392 | 1,800 / 2,917 / 4,015 | 27 / 41 (10) | 57 | 18 (35) | 0 |
| builder, 225 | 4,000 / 7,100 / 4,729 | 0 / 0 (0) | 0 | 0 (0) | 0 |
| builder, 2026 | 4,000 / 7,158 / 4,577 | 0 / 0 (0) | 0 | 0 (0) | 0 |

- **The builder's fuzz** shows nothing at `d3a19518b` either, as the review found. Its cut counts (4,729 and 4,577) are the builder's own.
- **With the cut blocks as modes too** (a block cut at each line break, head and tail, for all three trees): 2,000 review texts (400 per seed) and 1,000 builder texts (500 per seed) give 0 head-only leaks.
- **Committed** (`test_scrub_no_new_leak.py`, run in hold 1b):
  - the builder's two seeds at 2,000 texts each;
  - the review generator's five seeds at 1,200 each;
  - a bounded Hypothesis property over the same pieces (ci profile: 200 examples, derandomized, no database);
  - a check that the generator reaches every shape the review found.
  At `d3a19518b` the review generator's sweep and the property fail; the builder's sweep passes there, a guard.

### Mutations

Each change was dropped alone, in-process (the mutated source executed into `subfleet.sessions.handoff`'s namespace), and the round's tests were called directly, timing tests aside.

| Mutation | Named tests that fail |
|---|---|
| Drop A (the extension reads the break, no `Bearer`) | `test_a_bearer_or_flag_ending_a_swallowed_headers_line_keeps_its_value` (3 of 3); the review generator's sweep |
| Drop B (the old nested skip; no glued end) | `test_a_flag_glued_to_another_flags_value_keeps_its_own_value` (3 of 3); the review generator's sweep |
| Drop C (the second scan only after a changed `_HEADER_RE`, no line starts) | `test_a_header_name_a_line_start_header_swallows_takes_a_headers_value` (5 of 5), `test_a_header_name_a_partial_bearer_cut_exposes_is_read_in_the_same_scrub`, `test_an_excerpt_agrees_with_the_whole_scrub_at_the_real_bound`; the sweep and the property |
| Drop D (no stop before a whole-value header name) | `test_a_header_name_a_line_start_header_swallows_takes_a_headers_value` (5 of 5) |
| Drop E (an unclosed value takes the line's item) | `test_an_unclosed_quoted_value_leaves_a_line_ending_bearer_or_flag_its_value` (5 of 5), `test_an_unclosed_quote_does_not_take_what_a_swallowed_item_owns` (48 of 72: every flag and `Bearer` cell) |
| Drop the last-visible memo | `test_an_unclosed_value_reads_its_lines_end_once`: 188.6× release/217 (1.92 s against 10 ms; bound 4). Run under the wrapper from a scratch copy of the tree with the memo removed, since its child probe imports the tree's own files; the head passes it in the same hold. |

The differential cannot catch E: P3-5 leaked at both references, so "no new leak" holds without it, which is why E has its own regressions and matrix.

### Cost

The review's three rows, re-measured in one timing hold under the wrapper. The hold ran in slot B, after the group 2 and 3 pytest, from 00:51 to 00:56, at load 39 to 52.
- **Method:** each tree ran in its own process, against release/217's frozen scrubber measured alongside it, so load drift cancels. The `Bearer` step and the 1 MiB refusal are `best_cpu_ratio`: the best of 5 batches, 3 for the 1 MiB row. The heartbeat gap is the best of 3 whole `scrub_secrets` runs.
- **The texts:** the 262,144-character bound, `"Bearer " + unit` repeated. For the 1 MiB row, `"Bearer " + "--s"` repeated to 1,048,576 characters, through `refusals.detail_line`; release/217's is `scrubbed_line` of the same prefixed text.

| Row | Head (this round) | `d3a19518b` (reviewed head) | `473fe2941` (#231) |
|---|---|---|---|
| `Bearer` step, 7 units: ms, then ratio to release/217 | 1.74–2.62 ms, **0.84–1.14×** | 1.97–2.94 ms, 0.84–1.16× | 31.2–63.6 ms, 16.0–32.2× |
| Heartbeat gap, `~--a_` and `--s`: ms (release/217 alongside) | **8.0 and 5.2** (11.6 and 9.3) | 12.0 and 11.2 (8.7 and 7.3) | 47.3 and 49.7 (14.2 and 6.1) |
| 1 MiB refusal through `detail_line`: ms (release/217), ratio | 41.7 (36.8), **1.14×** | 76.9 (54.0), 1.42× | 246.3 (46.3), 5.32× |

- **No ratio to release/217 got worse.** The `Bearer` step is unchanged: this round does not touch it, and the review measured 1.5–1.8 ms at both. The heartbeat gaps are below release/217's in the same run. The 1 MiB refusal fell from 1.42× (1.36× in the review) to 1.14×, because both inline scans together now cost less than the one forward scan did.
- **4× input:** `test_inline_headers_and_bearer_keep_scrubbing_linear` grew to 17 units, 6 of them for this round's paths (A to E, and the reversed scan's colons). All passed in hold 1b, as did the unclosed-value cost test above.

### Tests

**Every hold:**
- **The wrapper:** `suite_hold.sh`, with this job's GIL CPython 3.14.7 venv (`sys._is_gil_enabled()` true) and `env -u SUBFLEET_RETENTION_DORMANT`. Each hold had its own `TMPDIR` under the job's scratch, and `HYPOTHESIS_STORAGE_DIRECTORY` pointed there too. There was no `.hypothesis` folder and no `.pytest_cache`.
- **The bound:** one pytest per hold, through `uv run python pt.py`, a short `pytest.main` launcher. It raises `KeyboardInterrupt` at 19 minutes, so pytest stops and still writes its JUnit file, and exits hard 30 s later.
- **Groups 2 and 3** ran as one pytest, with the timing script after it in the same hold, to save one queue wait. The queue then held about a dozen holds for two slots. The holds that ran waited 0 to 43 minutes; U2 waited 75 minutes and got no slot. `test_invariants_index.py` and `test_desktop_ledger.py`, which parse the contract this round edits, were added to group 3.
- **The new differential module** ran in hold 1b, with group 1. Group 1's slow excerpts file ran alone, in hold 1a, so that each hold stayed under 20 minutes.
- **Timing:** the brief's scrub and refusal tests ran at the fix (`d831052d1`) or later; the code did not change after it, and `d4af48b8a` changed only the contract.

**The briefed groups: 1,524 passed, 0 failed.**
**Hold 1b (slot A, queued 23:12, ran 23:14:41 to 23:24:07 at load 55; 559 passed, 9 m 21 s)**

| File | Passed | Failed | Errors | Skipped | Seconds |
|---|---|---|---|---|---|
| `tests/unit/test_conversation_redact.py` | 9 | 0 | 0 | 0 | 2.5 |
| `tests/unit/test_daemon_log_scrubbing.py` | 86 | 0 | 0 | 0 | 35.3 |
| `tests/unit/test_scrub_inline_headers.py` | 273 | 0 | 0 | 0 | 184.0 |
| `tests/unit/test_scrub_key_order.py` | 70 | 0 | 0 | 0 | 44.4 |
| `tests/unit/test_scrub_no_new_leak.py` | 4 | 0 | 0 | 0 | 194.7 |
| `tests/unit/test_sessions_handoff.py` | 117 | 0 | 0 | 0 | 68.9 |
| **6 files** | **559** | **0** | **0** | **0** | 530 |

**Hold 1a (slot A, queued 23:24, ran 00:07:24 to 00:13:50 at load 42; 153 passed, 6 m 23 s)**

| File | Passed | Failed | Errors | Skipped | Seconds |
|---|---|---|---|---|---|
| `tests/unit/test_sessions_handoff_excerpts.py` | 153 | 0 | 0 | 0 | 379.8 |
| **1 files** | **153** | **0** | **0** | **0** | 380 |

**Hold 2 (slot B, queued 00:14, ran 00:46:09; groups 2 and 3 in one pytest, 812 passed, 4 m 58 s; then the timing script to 00:56:35)**

| File | Passed | Failed | Errors | Skipped | Seconds |
|---|---|---|---|---|---|
| `tests/fake/test_record_secrecy.py` | 44 | 0 | 0 | 0 | 111.2 |
| `tests/fake/test_state_contract.py` | 37 | 0 | 0 | 0 | 30.5 |
| `tests/unit/test_cpu_scaling.py` | 3 | 0 | 0 | 0 | 37.7 |
| `tests/unit/test_desktop_ledger.py` | 4 | 0 | 0 | 0 | 0.1 |
| `tests/unit/test_handler_refusals.py` | 317 | 0 | 0 | 0 | 78.4 |
| `tests/unit/test_invariants_index.py` | 25 | 0 | 0 | 0 | 1.8 |
| `tests/unit/test_protocol_secrecy.py` | 322 | 0 | 0 | 0 | 12.4 |
| `tests/unit/test_round10_refusal_context.py` | 59 | 0 | 0 | 0 | 8.2 |
| `tests/unit/test_test_module_names.py` | 1 | 0 | 0 | 0 | 0.1 |
| **9 files** | **812** | **0** | **0** | **0** | 280 |

**The whole of `tests/unit` and `tests/fake`, in batches.** Files already run in the holds above were left out. The rest were split alphabetically by size, and after U1 the batches were reordered: the 28 files that call the scrubber or its consumers (suggestions, conversation briefs, sessions, approvals) went next, as U2.

**Batch U1 (slot B, ran 00:56:37 to 01:16:12 at load 39; stopped at 19 minutes; 1,827 tests in 45 of its 52 files)**

| File | Passed | Failed | Errors | Skipped | Seconds |
|---|---|---|---|---|---|
| `tests/unit/test_admission_uncapped.py` | 27 | 0 | 0 | 0 | 131.5 |
| `tests/unit/test_agent_secret_credentials.py` | 7 | 0 | 0 | 0 | 0.1 |
| `tests/unit/test_app_cutover_daemon.py` | 13 | 0 | 0 | 0 | 85.8 |
| `tests/unit/test_approval_commit.py` | 6 | 0 | 0 | 0 | 11.6 |
| `tests/unit/test_approval_resolution.py` | 33 | 0 | 0 | 0 | 50.7 |
| `tests/unit/test_boot_identity.py` | 10 | 0 | 0 | 0 | 0.4 |
| `tests/unit/test_capacity_desktop.py` | 20 | 0 | 0 | 0 | 0.4 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 | 0 | 0 | 0 | 2.1 |
| `tests/unit/test_capacity_steer_route.py` | 22 | 0 | 0 | 0 | 3.9 |
| `tests/unit/test_capacity_view.py` | 20 | 0 | 0 | 0 | 0.2 |
| `tests/unit/test_census_counted.py` | 5 | 0 | 0 | 0 | 0.2 |
| `tests/unit/test_census_identity_uncertainty.py` | 8 | 0 | 0 | 0 | 0.1 |
| `tests/unit/test_census_procargs.py` | 52 | 0 | 0 | 0 | 1.8 |
| `tests/unit/test_census_recycled_pid.py` | 25 | 0 | 0 | 0 | 12.9 |
| `tests/unit/test_census_responsibility_failures.py` | 20 | 0 | 0 | 0 | 0.3 |
| `tests/unit/test_census_responsibility_identity.py` | 17 | 0 | 0 | 0 | 2.3 |
| `tests/unit/test_census_responsibility_races.py` | 34 | 0 | 0 | 0 | 15.3 |
| `tests/unit/test_census_survivors_review.py` | 14 | 0 | 0 | 0 | 0.6 |
| `tests/unit/test_ci_retry_known_flakes.py` | 72 | 0 | 0 | 0 | 141.5 |
| `tests/unit/test_ci_shard_plugin.py` | 48 | 0 | 0 | 0 | 61.7 |
| `tests/unit/test_claude_adapter.py` | 170 | 0 | 0 | 0 | 22.2 |
| `tests/unit/test_claude_attest.py` | 19 | 0 | 0 | 0 | 4.1 |
| `tests/unit/test_claude_cards.py` | 96 | 0 | 0 | 0 | 131.0 |
| `tests/unit/test_claude_cards_wiring.py` | 15 | 0 | 0 | 0 | 4.0 |
| `tests/unit/test_claude_conversation_mcp_scope.py` | 8 | 0 | 0 | 0 | 0.9 |
| `tests/unit/test_claude_identity.py` | 30 | 0 | 0 | 0 | 3.3 |
| `tests/unit/test_claude_large_stream.py` | 30 | 0 | 0 | 0 | 4.0 |
| `tests/unit/test_claude_mcp.py` | 32 | 0 | 0 | 0 | 4.0 |
| `tests/unit/test_claude_mcp_launch.py` | 11 | 0 | 0 | 0 | 12.1 |
| `tests/unit/test_claude_stream.py` | 60 | 0 | 0 | 0 | 7.9 |
| `tests/unit/test_claude_synthetic_error.py` | 63 | 0 | 0 | 0 | 9.6 |
| `tests/unit/test_claude_turn.py` | 69 | 0 | 0 | 0 | 10.8 |
| `tests/unit/test_claude_usage.py` | 28 | 0 | 0 | 0 | 2.7 |
| `tests/unit/test_claude_v1_parity.py` | 6 | 0 | 0 | 0 | 0.6 |
| `tests/unit/test_cli.py` | 248 | 0 | 0 | 0 | 106.0 |
| `tests/unit/test_cli_batch.py` | 18 | 0 | 0 | 0 | 7.8 |
| `tests/unit/test_cli_turns.py` | 23 | 0 | 0 | 0 | 17.5 |
| `tests/unit/test_client_availability.py` | 22 | 0 | 0 | 0 | 0.9 |
| `tests/unit/test_codex_adapter.py` | 172 | 0 | 0 | 0 | 39.4 |
| `tests/unit/test_codex_ca_bundle.py` | 46 | 0 | 0 | 0 | 19.8 |
| `tests/unit/test_codex_ca_bundle_provider.py` | 6 | 0 | 0 | 0 | 7.0 |
| `tests/unit/test_codex_probe.py` | 55 | 0 | 0 | 0 | 6.7 |
| `tests/unit/test_codex_shim.py` | 23 | 8 | 0 | 0 | 83.4 |
| `tests/unit/test_codex_turn.py` | 67 | 0 | 0 | 0 | 14.8 |
| `tests/unit/test_codex_turn_completed.py` | 39 | 0 | 0 | 0 | 12.1 |
| **45 files** | **1819** | **8** | **0** | **0** | 1056 |

- **U1's 8 failures** are all in `tests/unit/test_codex_shim.py`: `subprocess.TimeoutExpired` after 5 s while the `bin/codex` shell shim started its stand-in Python under host load. Neither the shim nor its test changed between 473fe2941 and this head (`git diff` is empty), and the shim never reaches the scrubber. The file was put first in batch U3 for a rerun.
- **U1's 7 files left by its 19-minute stop:** `test_compat.py`, `test_continuation_r2.py`, `test_conversation_catalog.py`, `test_conversation_catalog_lifecycle.py`, `test_conversation_connections.py`, `test_conversation_diff.py`, `test_conversation_fifo_reads.py`.

- **Batch U2** (the 28 scrub-adjacent files) was queued at 01:16:12 and had no slot by 02:31:20, the job's 4 h 30 m mark for starting holds, so it was stopped. U3 to U6 and F1 to F3 were never queued.

**Per directory:**
- **`tests/unit`:** 332 files. 59 ran: the 14 briefed files, 559 + 153 + 731 tests, all passed, and U1's 45, 1,819 passed and 8 failed. **273 did not run**, U2's 26 unit files among them.
- **`tests/fake`:** 112 files. 2 ran (`test_record_secrecy.py` 44 and `test_state_contract.py` 37, both passed). **110 did not run**, U2's 2 fake files among them.


### Status

- **Done:**
  - the merge of release/217;
  - A to D as the review described them, and E for P3-5;
  - every review input as a regression, each failing at `d3a19518b`;
  - the frozen 473fe2941 scrubber and the no-new-leak differential (two generators and a bounded property);
  - C-23.14's amendments with a change-log entry;
  - this section and the dated corrections above;
  - the mutations and the cost rows.
- **Ran under pytest at this head:** the brief's three groups (group 1 in two holds, groups 2 and 3 in one), 1,524 tests, all passed. Batch U1 of the whole-suite run: 1,827 tests in 45 files, 8 failed, all `test_codex_shim.py` timeouts (above). They were not rerun.
- **Not run:**
  - 273 of 332 `tests/unit` files and 110 of 112 `tests/fake` files: the queue gave no slot after 01:16;
  - the `test_codex_shim.py` rerun;
  - Python 3.12;
  - CI (no network, nothing pushed).
- **Ran outside the lock, as plain scripts, each under about 30 s of CPU:** the mutations, the direct pre-fix runs of the new tests, and the scratch fuzz and cut-mode runs. The cost rows and the cost mutation ran under the wrapper.
- **Follow-ups for the hub:** unchanged from the list above.
  - **Follow-up 2** now also covers the shell continuation, which C-23.14 states. It shows in every mode at release/217, 473fe2941 and this head (`test_a_shell_continuation_after_a_flag_shows_its_value_documented_limitation`).
  - **New:** the full `tests/unit` and `tests/fake` runs, and the `test_codex_shim.py` rerun.

## Fix round 2 (#239 review r2, 2026-10-23)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix2.md`. **Review:** that folder's `review-r2.md`, REQUEST_CHANGES with three P2s and three P3s, each executed, and a 26-line prototype (P1 to P4) that the review tested in probes. This round implemented P1 to P4 as the review shows them, applied the hub's four decisions, merged release/217 at `c2e080e44`, and corrected C-23.14, C-25.7 and this report. The work stayed in the job's worktree: no Git remote operations, no sub-agents, nothing pushed. All test values are dummy `SECRET7…` strings.

**Names in this section.** `c2e080e44` is release/217 as merged here, with #233 (D-LOG1). Its scrubber is 473fe2941's with D-LOG1's changes, none of which changes what a scrub returns: on every text this round compared (the 5,430 matrix cells, the 720 ownership texts, 1,500 joined-cell texts and the 10,000 round-1 sweep texts), `c2e080e44` and the frozen `scrub_473fe2941` gave the same output in all five modes. So the committed tests use `scrub_473fe2941` for both. `3fbc4b7c5` is the scrubber before #231, which the earlier sections call release/217 (`tests/unit/scrub_release217.py`); it is still checked where round 1 checked it. `6bda95d54` is this branch's head before this round.

### The merge

`origin/release/217` at `c2e080e44` (#233, D-LOG1, rounds r3 to r7), merged in `8a9f217fc`.
- **`docs/acceptance-contract.md`:** resolved by `merge_contract.py HEAD c2e080e44`, which exited 0 ("254 clauses, none duplicated"). This branch's two C-23.14 change-log entries come first, then release's. `grep -o '^- \*\*C-[0-9]*\.[0-9]*\*\*' … | sort | uniq -d` prints nothing.
- **`subfleet/sessions/handoff.py`, two hunks, by hand:**
  - *Helpers.* This branch's `_owns_past` to `_inline_value_end` sit beside release's `_BLOCK_RULES` and `_TOKEN_RULES`. `_TOKEN_RULES` is again the whole ordered list of token rules, with `_bearer_value` for `Bearer`, because D-LOG1's rule-example test derives the rules from it.
  - *`_scrub`.* It applies `_TOKEN_RULES` one rule at a time: the inline header rule reads first, as this branch did, and again right after `_HEADER_RE`.
  - *Auto-merged and checked:* release's `_flag_start`, `spans`, `_run_start` and `_neutral_break(before)` beside this branch's changes to the same functions.
- **Changes the merge needed so both sides' guarantees hold:**
  - *D-LOG1's leading-header cut rule* (`_leads_with_header`) read release's three header names. A text opening with `X-Authorization:` is, folded to one line, all that header's value under this branch's `_HEADER_RE`, so the rule now reads the wider name: a C scan of the name's characters (`_HEADER_NAME_CHARS`, which a test checks against every code point the header regex reads), then its word in at most 13 characters. Its test's regex is widened to match, and its looks are checked on the wider names too.
  - *D-LOG1's rule-example test* names the inline header rule as a pass, with an example. The rule does not fold: its value crosses a line break only after its colon, which no cut parts.
  - *The cut and the inline rule (fixed in `41801663f`, below).* At the merge, a header whose whole value was a credential flag (`sent Cookie: --password` and the value on the next line) kept the flag. Daemon.log's line form then rescrubbed the folded text, and the header's value ran to the record's end. With that header in the part `cut_whole` leaves out, the cut record showed what the uncut one hid. A scratch differential found 332 of 1,500 records; D-LOG1's own generators never draw that shape, and its tests passed at the merge (hold M1 below).

### Findings: cause and fix

**P2-1: a value shaped like a credential flag was shown.**
- **Cause:** `_owning_flag` accepted any `--` and a sensitive key that whitespace or the end follows. Every rule that defers to it (the plain value, a header's value, `Bearer`, fixes A, B and E) left that text in place, even when the flag's name was itself the secret (`token:--SECRET7abc1_token`).
- **Fix P4:** a flag whose name reads as a secret (`_value_runs`: six or more word characters or `-` with a letter and a digit) owns nothing, so the enclosing rule takes it.
- **The hub's ruling, fail closed:** P4 alone opened cells where 473fe2941 hid the second secret in daemon.log (`sent Cookie: x --SECRET7abc1_token` and `SECRET7nxt9` on the next line). So the rule that takes a secret-named flag also redacts the value daemon.log's folded line would give it (`_secret_flag`, `_fold_value`): the next run past any whitespace and blank lines, unless it starts with `-`. That run ends before a key, flag or `Bearer` that ends it and owns a value past it (`_kept_span`), so no other rule's key is taken from it. The plain and quoted passes, a glued flag, a header and `Bearer` (`_bearer_sub`, a search loop equal to `re.sub` that can also redact the folded value) all do this.
- **Result:** `token:[REDACTED]`, `Bearer [REDACTED]`, `Authorization: [REDACTED]`, `api_key=[REDACTED]`, `--password [REDACTED]`, `secret:[REDACTED]`; `sent Cookie: [REDACTED]` and `[REDACTED]` on the next line, in every mode. P2-1's partial input, with a dummy value (`token: SECRET7h2--super-secret`), gives `token: [REDACTED]--super-secret`: the value is hidden, and `--super-secret`, a flag glued to it, is a flag's (C-23.14).
- **Over-redaction accepted:** `Bearer --passwordSECRET7a1_token tail`, which `test_bearer_refuses_exactly_what_the_lookahead_did` pinned as refused, is now `Bearer [REDACTED] [REDACTED]`. The test is re-pinned: the accepted matches are the lookahead's and those whose run ends in a secret-named flag.

**P2-2: a `Bearer` a quoted range's union swallowed lost its token.**
- **Cause:** a later key inside a quoted range, whose plain value was a line-ending `Bearer` (`token:Bearer`), passed the quoted pass's union check, and its value was unioned into the range. The plain branch deferred only to flags.
- **Fix P1:** in the quoted pass, a `Bearer` ending a key's plain value is an item too (`_owned_item`): the candidate is left to the `Bearer` rule.
- **Result:** `secret:[REDACTED] [REDACTED] rest`, `passwd:[REDACTED]\nBearer [REDACTED]` and `Authorization:[REDACTED]` for the review's three inputs; 473fe2941 hides each secret too.

**P2-3: a quoted inline header whose value started on the next line took that line's own header.**
- **Cause:** the value start crossed the line break, `_inline_value_end` ended the value at the closing quote on that line, and the next line's header was never read: `_HEADER_RE` cannot match after `[REDACTED]`, and the second scan skips a value that starts with it.
- **Fix P2:** a header whose value starts on the next line, at a header's name, is skipped; that name is read on its own.
- **Result:** `curl -H "Cookie:\n[REDACTED] [REDACTED]`, as 473fe2941 gives, and the JSON-escaped form alike.

**P3-1: fix E's line-end check was super-linear.**
- **Cause:** each unclosed value called `_owned_item`, whose `rfind("--")` scanned back to that value's opening. A line can hold about √(2n) unclosed values, so the work was O(n^1.5).
- **Fix P3:** an unclosed value, and a header name a quoted match swallows, read the line's end once (`owned_before`, a memo of `_owned_item(text, 0, last)` per line end, filtered to the value's span).
- **Result:** on the review's adversary, the quoted pass grows 3.07 times from 69,000 to 267,000 characters (6.10 at `6bda95d54`, 3.24 at `c2e080e44`), and `_owned_item` runs once per line end (`test_owned_before_reads_each_line_end_once`). The cost table is below.

**P3-2: a line-ending `Bearer` that is a key's value inside an unclosed quote.**
- **Cause:** P2-2's mechanism: the key's plain value (`Bearer`) was unioned into the unclosed range.
- **Fix:** P1. `api_key='x token:Bearer\nSECRET7abc12345` now gives `api_key=[REDACTED] [REDACTED]` in every mode, hiding what its twin hides (`api_key=[REDACTED] token:[REDACTED] [REDACTED]`). No limit needs stating.
- **Scope:** a 360-cell ownership matrix (below) has 260 failures at `6bda95d54` and none now.

**P3-3: C-23.14 and this report overstated.** Corrected in C-23.14 (change-log entry of 2026-10-23), and by a dated note under round 1's invariants:
- **The line-start header sentence** now adds that an inline header whose value would start on the next line leaves that line's header to it, and that a header takes more than its line only where stated.
- **Idempotence** is replaced by monotonicity: a second scrub never shows what the first hid, but it may hide more (`cookie:AUTHORIZATION :` and a value on the next line).
- **The suffix-search sentence** now says what is linear: the unclosed values and swallowed header names on a line read its end once.
- **The converse** is stated in its one form, the key form, and the flag form is hidden (check 2).

**Check 2: the two regressions against release/217 that the review found besides the converse.**
- **The flag form of the converse,** `Bearer SECRET7--secret x`. Cause: `_bearer_refusal` refused the whole run when the run before the flag was not token-like. Fix: the run glued before a refused flag is redacted, token or not. Result: `Bearer [REDACTED]--secret [REDACTED]`. `Bearer x/--api-key …` now loses `x` too (re-pinned in `test_scrub_key_order`), and approvals mask the same run.
- **A refused `Bearer` ending a header line,** `Authorization: Bearer\nclient_secret=x SECRET7abc13 tail`. Cause: the header kept `Bearer` as an item that owns its next line, but the `Bearer` rule refused the run there as a key, so nothing took the rest of that line; release/217 hid it only because its `Bearer` joined the two lines. Fix: a header that a `Bearer` ends, whose run there `Bearer` refuses, takes that line to its end (`_refused_bearer`), ended before a key, flag or `Bearer` that owns a value past it. Result: `Authorization: [REDACTED]\n[REDACTED]`. The first version took the whole line and broke `proxy_authorization: "Bearer\n  password=\n…`, whose key's value is on the line after; the round-1 sweep (seed 9001) found it, and `_kept_span` closed it.
- **The stated converse** (`invalid Bearer SECRET7_token: expired`) stays the one exception. It shows at 473fe2941 and `c2e080e44` too, so against them it is no leak at all; only `3fbc4b7c5` hid it.

**The cut (the merge).**
- **Cause:** above. A header whose whole value is a credential flag kept the flag, so daemon.log's folded rescrub read the header's value to the record's end.
- **Fix:** such a header takes the flag, and the value the folded line gives it (`_header_redaction`): `Cookie: [REDACTED]` and `[REDACTED]` on the next line, in every mode. The value is hidden where 473fe2941 showed it outside daemon.log.
- **Excerpts:** a header or secret-named flag that takes a value on a later line needs that break uncut, so `_neutral_break` no longer allows a cut between a credential flag and a visible character other than `-` after the break. `cut_whole` already refused those breaks (`_folds_to_value`), so its cuts are unchanged.
- **Result:** 0 of 1,500 records in the scratch differential, and the committed tests (`test_a_header_left_out_of_a_cut_…`, `test_no_cut_record_shows_…`) fail at `8a9f217fc` and pass now.

**Found by this round's own generator (fixed in `01d3ada87`).** Joining two to four cells of the matrices (the "joined cells" generator, `test_joined_cells_show_nothing_a_reference_hides`) found two shapes that `41801663f` still showed where 473fe2941 hid them, in 2 of 2,500 texts:
- **A secret-named flag with no rule around it** (`--password --db-SECRET7k9-password` and `SECRET7nxt9` on the next line). P4 makes enclosing rules take such a flag, but a bare one was still the flag rule's, which shows a flag's name; 473fe2941 hid it only where a union or a header took the whole line. Fix: the flag pass redacts a flag whose name reads as a secret itself, with the value daemon.log's folded line gives it (`--password [REDACTED]` and `[REDACTED]`; `deploy --SECRET7abc1_token value` becomes `deploy [REDACTED] [REDACTED]`).
- **A line-start header whose value an earlier rule had redacted** (`Authorization: \" SECRET7…` before a bare carriage return, then `--password --passwordSECRET7a1_token` and the value on the next line). The unclosed escaped quote's redaction ended at the CR, the line-start rule, applied by `re.sub`, could not reach past its line, and the second inline scan skips a value that starts with `[REDACTED]`. Fix: the line-start rule is applied by `_header_sub`, a search loop equal to `re.sub` whose header may reach past its line as the inline rule's does (`_header_redaction`'s `reach`).
- **After both:** 0 leaks, 0 monotone failures and 0 split disagreements in 4,500 joined texts over seeds 2392, 239 and 4101. The four regressions fail at `41801663f`.

### Invariants

*Correction (fix round 3, 2026-10-24):* #239 review r3 executed counterexamples to invariant 1 at `902cfe90a`. P2-A and P2-B, both from this round, and P3-B and P3-C, older, showed values that 473fe2941 and `c2e080e44` hide: 5,046 of the review's 61,440 rules cells and 900 of its 1,760 header-name cells. "After both: 0 leaks" above held for this round's own generators only. The text below is kept as written; round 3's five invariants, under "Fix round 3", replace these.

Each holds for every input, with the stated exception, and is executed by the tests named.

1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides. The modes are the excerpt and whole-text scrubs, `clean`, daemon.log's line alone and as a refusal's detail (`detail_line`), and excerpts cut at each allowed break (invariant 3).
   - **The one stated exception:** the refused-`Bearer` converse in its key form. It is no exception against these two references, which show it too; against `3fbc4b7c5` it is the only one (`test_the_stated_converse_is_the_one_exception`).
   - **Tested by:** `test_scrub_review_r2.py` (every review r2 input, check 2's two, the 21 P4-opened inputs, the three matrices), `test_scrub_no_new_leak.py` (round 1's sweeps and property), and the cut tests above.
2. **Monotone:** a second scrub never shows a character the first hid. With markers and whitespace taken out of both, the second scrub's characters are a subsequence of the first's, case aside (`_revealed`). Exact idempotence does not hold: 9 of the 6,000 rebuilt-review sweep texts scrub differently a second time, each only losing more (a `\r` before a header's line end, or a refused `Bearer`'s run that folding later joins to a flag), and none of the builder's 4,000.
   - **Tested by:** every matrix cell, the round-1 sweeps and property (`_check`), the key-order property, and review r2's P3-3 inputs.
3. **Excerpts agree with the whole:** at every line break `_neutral_break` allows, the two excerpts' scrubs joined equal the whole text's (`_scrub`, the within-line rules). A whole-text scrub can still remove more (a quoted value across lines).
   - **Tested by:** `_splits` over every matrix cell and every review input, the round-1 sweeps and property, and #231's property.
4. **Ownership:** a value in quotes, or a flag's value, is redacted as its bare twin is, mode by mode, within C-23.14's stated limits: a non-secret flag reads its value on its own line only, so a value on a later line shows outside daemon.log's folded line, enclosed or bare alike, and a shell continuation shows everywhere.
   - **Tested by:** the 360-cell ownership matrix (P3-2's class: unclosed single, double and escaped quotes, and a header name a quoted match swallows, each against the text without the quote), round 1's 100-cell and 72-cell matrices, and the P3-2 regression.
5. **Cost:** every unit's growth at 4× input is at most about 4×, and review r2's three cost rows are within 1.3× of `c2e080e44` (table below).

### The rebuilt generators

The review kept none of its generators; these are rebuilt from what it says of them, at its sizes, and committed in `test_scrub_review_r2.py`.

| Matrix | Built from | Cells | At `6bda95d54`: leaks vs 473fe2941 = `c2e080e44` / vs `3fbc4b7c5` | Review r2's count | This head |
|---|---|---|---|---|---|
| Flag | 16 enclosures × 6 flag-shaped values × 8 endings | 768 | 300 / 309 | 408 / 428 | 0 / 0 |
| Union | 7 enclosures × 6 keys × 3 separators × 5 `Bearer` forms | 630 | 260 / 260 | 222 of 270 in 3 enclosures | 0 / 0 |
| Next-line header | 7 openers × 6 outer × 6 inner names × 4 values × 4 tails | 4,032 | 1,728 / 1,728 | 1,728 | 0 / 0 |
| Ownership (P3-2) | 4 enclosures × 90, each against its unquoted twin | 360 | 260 fail | 300 | 0 |

- **No cell** at this head has a split disagreement or a second scrub that reveals anything.
- **The P4-opened cells:** with the fold redaction dropped (P4 alone), 20 flag-matrix cells show a value 473fe2941 hides (the review found 10 in its matrix): four `Bearer` cells with the value on the same line, and sixteen inline-header cells with it on the next line, after blank lines, after trailing spaces or after CRLF. With the line-start header form, they are the 21 `P4_OPENED` regressions.
- **The round-1 sweeps at this head:** 0 leaks against both references, 0 monotone failures, 0 split disagreements over the rebuilt-review generator (seeds 4101, 4102, 9001, 239 and 2392, 1,200 texts each) and the builder's (225 and 2026, 2,000 each).

### Mutations

Each change was removed on its own, from a copy of the fixed package (`01d3ada87`), and `test_scrub_review_r2.py` run against it as a plain script (75 cases):

| Removed | Named tests that fail |
|---|---|
| P1 (a quoted key's line-ending `Bearer`) | the three P2-2 inputs, the P3-2 input and its twin test, the ownership and union matrices, the joined cells, one combined-found input (9) |
| P2 (the next line's own header) | the two P2-3 inputs, the next-line matrix, the P2-3 pin, the joined cells (5) |
| P3 (one look per line end) | `test_owned_before_reads_each_line_end_once`, and the growth test at 10.75 (2) |
| P4 (a secret-named flag owns nothing) | the pin of `sent Cookie: x --SECRET7abc1_token` and its next line, and the bare-CR header input (2) |
| The fold redaction (decision 2) | the 21 P4-opened inputs, the flag matrix, the joined cells, three pins, two combined-found inputs (28) |
| The flag form of the converse | both `Bearer SECRET7--secret` inputs, the flag matrix, its pin (4) |
| The refused-`Bearer` header line | check 2's input, its pin, the joined cells (3) |
| The header's lone flag | the four flag shapes of the cut test, the cut differential, its pin (6) |
| The excerpt break after a flag | 17 next-line P4-opened inputs (split disagreements), the flag matrix, the joined cells, three combined-found inputs (22) |
| The bare secret-named flag | three combined-found inputs (3) |
| The line-start header's reach | the bare-CR header input (1) |

P4 alone now fails only two tests: since `01d3ada87` the flag pass redacts a secret-named flag wherever it is, so most of what P4 hides is hidden twice. Run at `41801663f`, before that, dropping P4 failed 30 of 69.

### Cost

Under the wrapper, at the end of hold A2 (load 105 to 111), each tree measured in the same process, the trees taken in turn, best of five (three for the 1 MiB row), CPU time:

| Row | This head | `c2e080e44` | Head / `c2e080e44` | `6bda95d54` | Review r2 (head / release) |
|---|---|---|---|---|---|
| Excerpt scrub, 262,144 characters of P3-1's adversary | 10.1 ms | 10.1 ms | 1.00 | 73.9 ms | 73.8–80.7 / 11.7–15.0 ms |
| 1 MiB refusal through `detail_line` | 45.4 ms | 43.8 ms | 1.04 | 547.5 ms | 457 / 40.8 ms |
| Growth at 4× input, P3-1's adversary (excerpt) | 3.41 | 3.49 | – | – | 6.31 / 3.84 |

- **Growth at 4× input** (65,536 to 262,144 characters), seven units (the excerpt and whole-text scrubs, the quoted, plain and flag passes, the inline header scan, `detail_line`) on three shapes (P3-1's adversary, a line of header names a quoted match swallows, and a block of secret-named flags with folded values): 2.82 to 4.67 for this head, against 2.62 to 5.60 for `c2e080e44`, except one 5.22 (`detail_line` on the flag-fold shape, 2.88 for `c2e080e44` in the same run). Re-measured outside the lock, best of nine: 4.51 for this head and 4.27 for `c2e080e44` at 65,536 to 262,144, and 3.96 and 3.72 at 32,768 to 131,072. Both rise a little at the larger size, where `detail_line` reaches the excerpt bound.
- **A scratch probe** of 4,000 then 16,000 header names inside one quoted match (each a candidate whose value runs to the line's end) grows 4.15 for this head, 4.37 for `6bda95d54` and 3.66 for `c2e080e44`: the swallowed-header branch shares P3's memo.
- **Invariant 5 holds:** each row is within 1.3× of `c2e080e44` (1.00 and 1.04), and growth stays about 4×.

### Tests

**Holds.** Each pytest ran alone through `suite_hold.sh`, with the venv's Python 3.14 calling `pytest.main` in process (an alarm stops it at 19 minutes), from a `git archive` export of the commit named, under `env -u PYTHONPATH -u SUBFLEET_RETENTION_DORMANT -u SUBFLEET_JOB -u SUBFLEET_ATTEMPT`, with the brief's `TMPDIR` and Hypothesis folder. No plugin ran in parallel (the venv has no xdist). Queue waits were 3 to 28 minutes for the holds that ran. Two earlier holds were stopped while still queued: one on `41801663f` (36 minutes), to fold two holds into one, and one on `72f3f067f` (28 minutes), because the joined-cells generator changed the code (`01d3ada87`).

| Hold | Commit | Files | Result |
|---|---|---|---|
| M1 (slot B, 16:49) | `8a9f217fc`, the merge | the eight `tests/unit/test_daemon_log*.py` | 606 passed, 5 skipped, 11 m 22 s |
| A2 (slot A, 18:39) | `17f55edd9` | the 16 files below, then the cost rows | 1,446 passed, 18 m 53 s |
| B | `17f55edd9` | the seven other `test_daemon_log*.py` | queued at 19:00; see the status below |

**Per file, hold A2 (all passed):** `test_scrub_review_r2.py` 75 (new), `test_scrub_inline_headers.py` 273, `test_scrub_key_order.py` 70, `test_scrub_no_new_leak.py` 4, `test_conversation_redact.py` 9, `test_sessions_handoff.py` 117, `test_daemon_log_scrubbing.py` 86, `test_round10_refusal_context.py` 59, `test_handler_refusals.py` 317, `test_protocol_secrecy.py` 322, `fake/test_record_secrecy.py` 44, `test_cpu_scaling.py` 3, `fake/test_state_contract.py` 37, `test_test_module_names.py` 1, `test_invariants_index.py` 25, `test_desktop_ledger.py` 4.

**Per file, hold M1 (at the merge):** `test_daemon_log_cut_windows.py` 169, `test_daemon_log_first_pass.py` 25, `test_daemon_log_handler.py` 128, `test_daemon_log_mutation_tool.py` 4, `test_daemon_log_reminders.py` 101, `test_daemon_log_scrubbing.py` 86, `test_daemon_log_sites.py` 77 passed and 5 skipped, `test_daemon_log_tail.py` 16.

**Per directory:** `tests/unit`: 14 of 344 files ran at this head (hold A2), and the eight `test_daemon_log*.py` at the merge (M1), one of them in both: 21 distinct files. `tests/fake`: 2 of 112 (`test_record_secrecy.py` and `test_state_contract.py`, in A2).

**Plain scripts outside the lock** (each probe under about 40 s of CPU, the matrix and sweep probes up to about 75 s): the mutations, the pre-fix runs of the new tests (at `6bda95d54`, `8a9f217fc` and `41801663f`), the matrix and sweep counts, the cut-mode and cut-record differentials, the `c2e080e44` against `scrub_473fe2941` comparison, and a dry run of the cost script.

### Status

- **Done:**
  - the merge of release/217 at `c2e080e44`, with the changes both sides' guarantees needed;
  - P1 to P4 as the review shows them; the fold redaction (decision 2); check 2's two; P3-2 closed by P1 (decision 4); the merge's cut; the two shapes the joined-cells generator found;
  - every review r2 input, check 2's two and the 21 P4-opened inputs as regressions, each failing at `6bda95d54`, and the cut and joined-cells regressions failing at `8a9f217fc` and `41801663f`;
  - the three rebuilt generators, the ownership matrix and the joined-cells generator as tests;
  - C-23.14 and C-25.7 with change-log entries, and this section with a dated correction to round 1's invariants;
  - the mutations and the cost rows.
- **Not run:** D-LOG1's seven other `test_daemon_log*.py` files at this head (hold B was queued at 19:00; they ran at the merge, M1); `test_sessions_handoff_excerpts.py`; the rest of `tests/unit` (323 files) and `tests/fake` (110 files); Python 3.12; CI (no network, nothing pushed).
- **Follow-ups for the hub:** unchanged, with these additions:
  - **The stated converse** (the key form) is still the one value a reference (`3fbc4b7c5` only) hides that this scrubber shows.
  - **Over-redaction to watch:** a header whose whole value is a credential flag, a secret-named flag anywhere, and a header that a refused `Bearer` ends now each take a value on a later line, in every mode; `Bearer x/--api-key …` loses `x`. Each is the hub's fail-closed ruling or needed by the merge; none has shown up in daemon.log yet.

## Fix round 3 (#239 review r3, 2026-10-24)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix3.md`. **Review:** that folder's `review-r3.md`, REQUEST_CHANGES with two P2s and three P3s, each executed, and a seven-line prototype the review measured. This round implemented the prototype as the review shows it (P2-A, P2-B), fixed P3-B and P3-C as it proposes, merged release/217 at `63c64d6b3`, and corrected C-23.14 and this report (P3-A). The work stayed in the job's worktrees: no Git remote operations, no sub-agents, nothing pushed. All test values are dummy `SECRET7…` strings.

**Two runs.** The first run (job `20261024-105448-pr239-fix3`, 15:13 to 17:46Z on 2026-10-24) made the merge (`89e4cca16`), the tests (`4b5c6efdb`) and the fix (`85cf2af35`). Then the host lost its network and the run stopped, with its holds and probes unfinished; none of its results is used here. The second run (b, job `20261024-193042-pr239-fix3b`, from 01:19Z on 2026-10-25) kept those three commits unchanged after re-running every check they rest on. It corrected two docstrings in the new test file (`9b05f00db`), amended C-23.14 (`78591f4c4`), added invariant 5's count test (`ca17ca12e`), wrote this section, and ran every hold, probe, mutation and cost run below.

**Names in this section.**
- **release/217** is `63c64d6b3` (#244, D-PRR1). Its scrubber is `c2e080e44`'s: #244 changes nothing under `subfleet/sessions/` (`git diff c2e080e44 63c64d6b3 -- subfleet/sessions/` is empty).
- **The references.** On all 65,360 texts of this round's four matrices, the real `473fe2941` and `c2e080e44` trees and the frozen `scrub_473fe2941` give the same output in all five modes (run b's dumps, compared hash by hash; both trees match `git show` file for file). So the tests use `scrub_473fe2941` for both, as round 2's do. `3fbc4b7c5` (`scrub_release217`, the scrubber before #231) is still checked.
- **`902cfe90a`** is this branch's head before this round.

### The merge

`origin/release/217` at `63c64d6b3` (#244), as the hub last fetched it, merged in `89e4cca16`. Run b checked it again:
- **`docs/acceptance-contract.md`, the one conflict:** `merge_contract.py 902cfe90a 63c64d6b3`, re-run, exits 0 ("merged against 3b13b36b7: 254 clauses, none duplicated") and writes exactly the committed file. `grep -o '^- \*\*C-[0-9]*\.[0-9]*\*\*' … | sort | uniq -d` prints nothing, after the merge and after this round's amendment.
- **Every other file the merge changes** is `63c64d6b3`'s version: #244's report and PR body, `home_paths.py`, `prune_decisions.py`, `tests/conftest.py`, three test helpers and four test files.
- **The scrubber:** nothing under `subfleet/sessions/` differs between `902cfe90a` and the merge, or between `c2e080e44` and `63c64d6b3`.

### Findings: cause and fix

**P2-A: a span `_kept_span` bounds took a secret-named flag's name and left its value.**
- **Cause:** `_kept_span` ended a span only before an item `_owned_item` returns. That goes through `_owning_flag`, which since round 2's P4 returns None for a flag whose name reads as a secret. So a fold (`_fold_value`, used by the flag rule and by the header, `Bearer`, plain and quoted folds) or a refused `Bearer`'s line (`_refused_bearer`) that ended in such a flag swallowed the flag, but not the value daemon.log's fold gives it. The flag pass, whose cursor was then past the flag, never read it.
- **Fix (the prototype's first line):** `_kept_span` also ends before any credential flag that ends the span and may own a value past it (`_ending_flag`), a secret-named one included. The flag rule then takes that flag with its own fold.
- **Result:** the review's five inputs, in every mode: `[REDACTED]\n[REDACTED][REDACTED] [REDACTED]` for `--SECRET7a1_token\nKEY=--db3-password SECRET7c3`, and the same shape for the digit-named pair; `Authorization: [REDACTED]\n[REDACTED][REDACTED] [REDACTED]`; `x cookie: [REDACTED]\n[REDACTED] [REDACTED]\n[REDACTED]`; and `Cookie:[REDACTED]\n[REDACTED]\t[REDACTED]\n[REDACTED]` for the `detail_line` input.

**P2-B: a fold that started at a header's name ate the name, and the rest of that line showed.**
- **Cause:** when the next line is a line-start header whose value is glued to the colon (`Cookie:x …`), `_fold_value`'s run is `Cookie:x`. `_key_past_line` reads only a key that ends the run, so the whole run went. `_header_sub`'s cursor, or the `Bearer` and inline rules before it, were then past the name, and nothing read the rest of the line as a header.
- **Fix (the prototype's second part):** when the run starts at a header's name (`_header_name_at`), the fold takes that header's line to its last visible character, through `_kept_span`.
- **Result:** `Cookie: [REDACTED]\n[REDACTED]`, `Bearer [REDACTED]\n[REDACTED]` and `x\nAuthorization: [REDACTED]\n[REDACTED]` for the review's three inputs, in every mode.
- **The narrow form, measured again.** Decision 1 kept the line fold over the narrow form (the fold returning None at a header's name) for the leak the review measured: a secret held in a header's name. With P3-B in place, that leak is gone either way:
  - Over the header-name matrix, the P3-B matrix, the rules slice and 4,500 joined texts (13,124 texts), the narrow form shows nothing either reference hides.
  - It differs from the line fold only by keeping the next line's header name (`Cookie:[REDACTED]` where the fold gives `[REDACTED]`), in 1,760, 890 and 13 of those excerpts.
  - The review's leak needs both changes gone. With the narrow form and P3-B dropped, `deploy --SECRET7abc1_token\nSECRET7nxt9Authorization: Basic x` shows `SECRET7nxt9Authorization` in daemon.log's line and `detail_line`, against both references. Either change alone hides it, and its regression pins the output P3-B gives.
  - The line fold stays, as decided. That it over-redacts the next header's name where the narrow form would not is a follow-up below.

**P3-B: a header name holding a secret, glued to a closing quote, showed.**
- **Cause:** the quoted pass's header-name clause keeps the name out of the union and leaves it to the inline rule, which keeps names whole.
- **Fix:** where the part of a header's name before its header word holds a run that reads as a secret (`_value_runs`), the inline rule redacts that part, whatever becomes of the value: a value it redacts, or one it skips (at the text's end, already redacted, empty at a closing quote, or the next line's own header). Its second scan reads names at a line start too, so a line-start header loses it there as well.
- **Result:** `secret:"[REDACTED]"[REDACTED]Authorization: [REDACTED]` in every mode; `x\nSECRET7ab1Authorization: v` gives `x\n[REDACTED]Authorization: [REDACTED]`, and `SECRET7ab1Cookie:` gives `[REDACTED]Cookie:`.

**P3-C: a quoted inline header that ended at the quote opening a flag's quoted value took the flag and left its value.**
- **Cause:** `_header_redaction` checked for an owning item only when the value ended at a line break or the text's end, never at the closing quote the inline rule ends a quoted value at.
- **Fix:** when a closing quote ends the value, it also looks for a credential flag that ends it (`_owning_flag`). A flag that whitespace parts from the quote is kept with that whitespace, so the flag rule takes the quoted value after the quote, as with no header around it. A `Bearer` there still owns nothing (its rule reads no token that starts with a quote), and nor does a flag glued to the quote.
- **Result:** `curl -H 'Cookie: [REDACTED] --password [REDACTED]` in every mode, as its bare twin `--password 'SECRET7c3'` gives `--password [REDACTED]`. The review's 72 rules-matrix cells of this class are among the 0 below.

**P3-A: C-23.14 and this report overstated.** Corrected in C-23.14 (change-log entry of 2026-10-24), and by a dated note under round 2's invariants:
- **"the one value a reference hides that this scrubber shows"** now reads "is known to show: on every text of the generators the tests run, … no other shows". This section names those generators and counts them (invariant 1).
- **"either also takes the value daemon.log's folded line would give it"** now says what that value is: the next run, or, at a header's name, that header's whole line. It ends before any credential flag that ends it, which the flag rule takes with its own value. That is what makes the sentence true for a flag inside another fold or a refused `Bearer`'s line (P2-A).
- **"ended before a key, flag or `Bearer` that owns a value past it"** now names the header-line case (P2-B) and the ending flag (P2-A). The refused `Bearer`'s line "ends as a folded value is".
- **New sentences** state P3-B (a header name's secret part goes) and P3-C (a flag before a closing quote owns the quoted value after it).

**Decision 4, fail closed, unchanged.** An ordinary flag name that holds a digit among six or more word characters or `-` reads as secret-named, so it goes with the value its fold gives it:
- `deploy --k8s-token\necho done` becomes `deploy [REDACTED]\n[REDACTED] done`;
- `kubectl --oauth2-token abc --verbose` becomes `kubectl [REDACTED] [REDACTED] --verbose`.

Both are pinned in the excerpt and whole-text scrubs (`test_a_digit_in_a_flag_s_name_over_redacts_as_stated`); run b saw the same redactions in all five modes. C-23.14 now states them as the cost of failing closed.

### Invariants

Each is stated for every input, with its stated exception and scope, and is executed by the tests named. They replace round 2's.

1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides. The modes are the excerpt and whole-text scrubs, `clean`, daemon.log's line alone and as a refusal's detail (`detail_line`), and excerpts cut at each allowed break (invariant 3).
   - **The one stated converse:** the refused-`Bearer` key form (`invalid Bearer SECRET7_token: expired` keeps `SECRET7_token`). It shows at 473fe2941 and `c2e080e44` too, so against them it is no leak; only `3fbc4b7c5` hid it, and against `3fbc4b7c5` it is the only one.
   - **Scope: what it holds over.** It is checked over these generators, at this head, against both references, and 0 show anything else:
     - **This round:** the rules matrix (61,440 cells, run whole once in run b's probe and once under pytest; a seeded 6,144-cell slice in the unit tier), the header-name matrix (1,760), the P3-B matrix (720), the P3-C matrix (720 texts, each against its twin), and 200 drawn joins of their pieces (Hypothesis, ci profile).
     - **Round 2:** the flag (768), union (630) and next-line (4,032) matrices, the ownership matrix (360 pairs), 400 joined cells and 400 cut records.
     - **Round 1:** the builder's fuzz (4,000 texts), the rebuilt review-r1 generator (6,000), and its drawn property (200).
     - **Every executed input** of reviews r1, r2 and r3, each a regression.
   - **Beyond these generators** the invariant is a claim, not a measurement. The review's own corpora (19,100 texts, one P3-B text left under its prototype) were not kept, so they were not re-run.
   - **Tested by:** `test_scrub_review_r3.py`, `test_scrub_review_r2.py`, `test_scrub_no_new_leak.py` and the cut tests.
2. **Monotone:** a second scrub never shows a character the first hid (`_revealed`). Exact idempotence still does not hold, as round 2 states.
   - **Tested by:** `_unstable` over every cell of every matrix above (0 reveals over the 61,440 rules cells too), the round-1 sweeps and property, and the key-order property.
3. **Excerpts agree with the whole:** at every line break `_neutral_break` allows, the two excerpts' scrubs joined equal the whole text's. The header-line fold stops at its line's last visible character and crosses no further break.
   - **Tested by:** `_splits` over every matrix cell (0 over the whole rules matrix) and every review input.
4. **Ownership:** a value in quotes, or a flag's value, is redacted as its bare twin is, mode by mode, within C-23.14's stated limits (round 2's, unchanged).
   - **P3-C's class** is now in it: a flag's quoted value after a quoted inline header's closing quote, 720 cells against their twins (`test_a_flag_s_quoted_value_after_a_quoted_header_is_hidden_as_its_twin_s`).
   - **Tested by:** that matrix, round 2's 360-cell ownership matrix, and round 1's 100-cell and 72-cell matrices.
5. **Cost:** every unit's work grows at most about 4× at 4× input, and review r3's cost rows stay within 1.3× of `c2e080e44` (table below).
   - **The rows** hold in both runs.
   - **The counts** behind the new paths grow at most 4× (`test_the_new_folds_read_each_line_once`). The fold at a header's name and the P3-B check run once per header or fold. The quoted pass keeps no cursor, but its union takes the rest of a line once a key's fold has, so later keys on that line add no fold. A call count over 200 and 800 such folds on one line, before an 8,000- and a 32,000-character identifier, found one `_kept_span` call per scrub and one walk of the identifier.
   - **Timed growth** at 4× ranged past 4 for both trees alike under this host's load, so the timings confirm no more than the counts do.

### The rebuilt generators

The review kept none of its generators. The first run rebuilt them in `test_scrub_review_r3.py` from what the review says of them, at its sizes. Their cells are this file's own, so their counts at `902cfe90a` differ from the review's.

| Matrix | Built from | Cells | `902cfe90a`: show a value both references hide | Review r3's count at `902cfe90a` | This head |
|---|---|---|---|---|---|
| Rules | 16 leads × 4 secret-named flags × 8 gaps × 20 runs × 6 tails | 61,440 | 11,520 (7,536 in all five modes), and 300 more against `3fbc4b7c5` only | 5,046 | 0 / 0 |
| Rules, unit-tier slice | seed 2393 | 6,144 | 1,091 | – | 0 / 0 |
| Header-name | 11 folding leads × 5 gaps × 8 header lines × 4 tails | 1,760 | 560 (380 in all five modes) | 900 | 0 / 0 |
| P3-B | 6 leads × 4 names × 10 values × 3 tails | 720 | fails | 1 input | 0 / 0 |
| P3-C | 4 openers × 4 headers × 5 flags × 3 gaps × 3 tails, each against its twin | 720 | fails | 72 cells of the rules matrix | 0 |
| Drawn joins | 1 to 3 pieces of the four, joined (Hypothesis, ci profile) | 200 | fails | – | 0 |

- **"0 / 0"** is against 473fe2941 (= `c2e080e44`) and against `3fbc4b7c5`.
- **No cell at this head** has a split disagreement or a second scrub that reveals anything: 0 of each over the whole rules matrix, which run b ran in four probe shards (155 s of CPU) and again under pytest at `ca17ca12e` (190 s). It stays out of the default run behind `SF_SCRUB_FULL_MATRIX=1`: the repository has no `slow` marker, and `test_host_push_build`'s benchmark is opted in the same way.
- **Round 2's generators stay green at this head:** the 768-cell flag, 630-cell union and 4,032-cell next-line matrices, the ownership matrix and the joined cells (hold 1a below).

### Mutations

Each change was taken out on its own from an export of `85cf2af35`, and the test modules run against it as plain scripts.
- **The round-3 changes** run `test_scrub_review_r3.py`'s inputs, pins and matrices, but not its 6,144-cell slice or the drawn joins (18 cases).
- **Round 2's changes** run all of `test_scrub_review_r2.py` (75 cases) and round 3's inputs and pins (13).

| Removed | Named tests that fail |
|---|---|
| P2-A, `_kept_span`'s `_ending_flag` line | the five P2-A inputs (5) |
| P2-B, the header-line fold (the fold reads the visible run, as at `902cfe90a`) | the three P2-B inputs, the header-name matrix (4) |
| P2-B as the narrow return-None form | the three P2-B inputs, by their pinned output (3). No input of them shows a secret: see P2-B above |
| The narrow form and P3-B both (run b) | the three P2-B pins, the P3-B input, the narrow-fix input (now showing `SECRET7nxt9Authorization`), the P3-B matrix (6) |
| P3-B, a header name's secret part | the P3-B input, the narrow-fix input's pin, the P3-B matrix (3) |
| P3-C, the closing-quote check (`quoted = False`) | the P3-C input, the P3-C twin matrix (2) |
| Round 2's P1 | 9 of round 2's, as round 2 counted: the three P2-2 inputs, the P3-2 input and twin, the ownership and union matrices, the joined cells, one combined-found input |
| Round 2's P2 | 5 of round 2's: the two P2-3 inputs, the next-line matrix, the P2-3 pin, the joined cells |
| Round 2's P3 | 2 of round 2's: the growth test (10.17) and `test_owned_before_reads_each_line_end_once` |
| Round 2's P4 | 2 of round 2's: one pin and the bare-CR combined-found input |
| Round 2's fold redaction (decision 2) | 28 of round 2's, and 2 of round 3's inputs (P2-A's header with a lone flag, P2-B's `Bearer`) |
| All of this round (`902cfe90a`'s `handoff.py`) | 15 of round 3's 20: the ten inputs, the four matrices and the drawn joins. The narrow-fix input and the two over-redactions hold there, as they should |

### Cost

Two runs under the wrapper, each tree measured in the same process, the trees taken in turn, CPU time:
- **Run 1 (hold p3, 03:27 to 03:38Z):** the rows best of five (three for the 1 MiB row), and seven units on ten shapes at 65,536 and 262,144 characters. Run b's plain probes were still running for its first three minutes.
- **Run 2 (hold p5, 03:57 to 04:06Z):** nothing else of run b's running. The rows best of seven (five for the 1 MiB row), the excerpt on every shape (best of seven), `detail_line` from 256 KiB to 1 MiB, and a best-of-nine re-measure, at two size pairs, of every unit run 1 put past 4.5×.

The ten shapes are cost.py's three earlier ones (P3-1's adversary, a line of header names a quoted match swallows, a block of secret-named flag folds), one for each of this round's paths (a fold ending in a flag, folds at a header's name, one long header line, secret header names, flags before a closing quote), and two adversaries run b added for the header-line fold. Those two put many folds that start at a header's name on one line, from quoted keys or from `Bearer`, before an identifier half the text long that ends in a credential word.

| Row | This head | `c2e080e44` | Head / `c2e080e44` | Review r3 (head; prototype) |
|---|---|---|---|---|
| Excerpt scrub, 262,144 characters of P3-1's adversary | 9.81; 17.85 ms | 9.85; 15.08 ms | 0.996; 1.184 | 0.76–1.24; 1.01–1.07 |
| 1 MiB refusal through `detail_line` | 37.0; 47.7 ms | 39.5; 44.9 ms | 0.938; 1.062 | 0.81–1.26; 0.95–1.28 |
| Excerpt growth at 4× input, nine shapes (run 2; the tenth below) | 1.99–7.48 | 3.60–6.78 | – | 2.84–4.36 for head, 2.97–4.57 for `c2e080e44` |
| `detail_line`, 256 KiB to 1 MiB, four shapes (run 2) | 2.44–5.09 | 3.50–5.14 | – | 3.72–4.10 |

- **The rows** are within 1.3× of `c2e080e44` in both runs (0.996 and 1.184; 0.938 and 1.062), so no fix is past the brief's bound.
- **Growth at 4× is noisy on this host,** for both trees alike:
  - Run 1 put head's units at 2.29–6.94 and `c2e080e44`'s at 2.01–7.69, leaving out the rows under 1 ms at 65,536 characters.
  - The best-of-nine re-measure put head at 2.64–7.78 and `c2e080e44` at 1.90–6.24.
  - Of the 26 head units it re-measured, two are above 4.6 at both size pairs:
    - the flag pass on round 2's secret-named flag folds: 5.49 and 4.75, against 3.90 and 5.12 for `c2e080e44`;
    - the whole-text scrub on run b's `Bearer` folds at header names: 4.61 and 4.82, against 4.94 and 5.47 for `c2e080e44`.
  - One more shape is no growth at all: the excerpt of one long header line falls to 0.16× (0.14× for `c2e080e44`), because at 262,144 characters a line longer than an excerpt keeps nothing.
- **The counts behind invariant 5** are exact where the timings are not (`test_the_new_folds_read_each_line_once`, eight lines, one per new path). From 1× to 4× input, `_kept_span` runs at most 4× as often, and the Python-level identifier walk grows 3.91× to 4.00×, in the excerpt, whole-text and `detail_line` modes. A run b probe at 200 and 800 folds per line, before an 8,000- and a 32,000-character identifier, found one `_kept_span` call per scrub on the one-line shapes.
  - **Correction (2026-10-25, fix round 4, review r4 P3-3):** these counts did not back linearity. Their trailer, `"a" * tail + "password"`, is one run of base64 characters, which `_LONG_BASE64_RE` replaces with `[BASE64 OMITTED]` before the header, plain and flag passes read it, so no fold walked it. And no unit put several flag-rule folds on one line. So review r4's MQ2, a flag rule whose cursor stops at the flag and computes every later fold on the line again, passed all eight cases. Round 4's count test adds that unit and a trailer no token rule takes, and kills MQ2 (round 4's Cost, below).
- **Absolute cost at 262,144 characters:** head's excerpt is 0.31× to 1.91× of `c2e080e44`'s across the ten shapes (run 2). It costs more only where it redacts what `c2e080e44` leaves: 1.91× for flags before a closing quote, 1.76× for folds ending in a flag and for folds at a header's name, 1.47× for round 2's secret-named flag folds. That is a constant factor, not growth.
- **Not done:** a third hold timing every unit at 16× input (16,384 to 262,144 characters) was queued at 04:07Z. Run b stopped it unrun at 04:20Z to leave the one slot the host offered to holds 2 and 3.

### Tests

**Holds.** Each pytest ran alone through `suite_hold.sh`, from a `git archive` export of the commit named.
- **The run:** the venv's Python 3.14.7 called `pytest.main` in process (`hold.py`, which an alarm stops at 19 minutes), under `env -u PYTHONPATH -u SUBFLEET_RETENTION_DORMANT -u SUBFLEET_JOB -u SUBFLEET_ATTEMPT`, with the brief's `TMPDIR` and Hypothesis folder. The venv has no xdist.
- **Slots:** slot B was shut for most of the night (free disk 37 to 49 GB, under its 50 GB floor), so holds queued for slot A, from under a minute to 60 minutes each.
- **The driver:** after hold 1a, `holds.py` ran each group with `-v` and deselected every test an earlier hold had reported, so a hold the alarm stopped would leave its rest to the next.

| Hold | Commit, slot, start | Files (tests) | Result |
|---|---|---|---|
| 1a | `85cf2af35`, B, 01:52Z | `test_scrub_review_r3` 21, `test_scrub_review_r2` 75, `test_scrub_no_new_leak` 4, `test_conversation_redact` 9 | 97 passed and 1 skipped (the opt-in whole matrix). Under load about 100 to 130, the alarm stopped it at 19 m 3 s, with two `no_new_leak` tests and all of `conversation_redact` left |
| 1a's rest and 1c | `85cf2af35`, A, 02:39Z | those two, `test_conversation_redact` 9, `test_scrub_key_order` 70, `test_sessions_handoff` 117 | 198 passed, 8 m 25 s |
| 1b | `85cf2af35`, A, 03:14Z | `test_scrub_inline_headers` 273 | 273 passed, 12 m 46 s |
| 2 | `85cf2af35`, A, 04:26Z | the eight `test_daemon_log*.py` files (612) | 607 passed, 5 skipped, 2 m 51 s |
| 3 | `85cf2af35`, A, 04:30Z | `test_sessions_handoff_excerpts` 153, `test_handler_refusals` 317, `test_protocol_secrecy` 325, `fake/test_record_secrecy` 44, `fake/test_state_contract` 37, `test_test_module_names` 1 | 877 passed, 3 m 11 s |
| Round 3's file | `ca17ca12e`, B, 04:52Z | `test_scrub_review_r3` 29, the whole matrix opted in (`SF_SCRUB_FULL_MATRIX=1`) | 29 passed, 4 m 0 s; the whole matrix took 190 s |
| 4 | `85cf2af35`, B, 05:24Z | review r3's hold 4 (`test_conversation_wakes` 28, `test_host_push_storage_retry_budget` 61, `test_host_push_store_interleavings` 26, `test_turn_runner` 83, `fake/test_daemon_stacks` 6, `test_cpu_scaling` 3, `test_round10_refusal_context` 59), and #244's four (`test_session_credential_env` 47, `test_prune_restore` 20, `test_prune_217` 9, `test_live_store_guard_round3` 22) | 363 passed, **1 failed**, 13 m 15 s, under load 94 to 103 |

- **The five skips** (hold 2) are `test_daemon_log_sites.py`'s real-daemon tests. `_subfleetd` skips them because the venv's `subfleet` is the worktree's, not the export's, as in round 2's hold M1. Two of them run the daemon under launchd, so run b did not run them against this desktop some other way.
- **The failure** (hold 4) is `tests/fake/test_daemon_stacks.py::test_daemon_stacks_prints_every_thread_of_the_live_daemon`. The fake daemon did not answer `daemon.status` within the harness's 5-second start window (`Harness.start`). Its kept state root held a `daemon.log` with only the start-up line ("open-file limit 1048576 …"), and the hold ran at load 94 to 103.
  - **Its cause is not established.** This round's diff changes only function bodies in `handoff.py` and adds no import-time work, and review r3 reports the same file passing in its hold 4 at `902cfe90a`.
  - **A re-run of the file** was queued at 05:40Z. It had not got the lock by the 05:49Z mark, so run b stopped it unrun; the test stays unconfirmed either way.
  - **The kept root** (`/tmp/sf-failed/25264`) was this hold's, and run b removed it after reading it.
- **No code changed after `85cf2af35`,** the commit the holds ran at. `9b05f00db` changes docstrings, `78591f4c4` and `c6d9630e1` the contract, `ca17ca12e` adds the count test (run at its own commit, above), and the last commit adds this section.

**Per file** (the outcomes the holds' `-v` lines report, at `85cf2af35` but where named): `test_conversation_redact.py` 9; `test_conversation_wakes.py` 28; `test_cpu_scaling.py` 3; `test_daemon_log_cut_windows.py` 170; `test_daemon_log_first_pass.py` 25; `test_daemon_log_handler.py` 128; `test_daemon_log_mutation_tool.py` 4; `test_daemon_log_reminders.py` 101; `test_daemon_log_scrubbing.py` 86; `test_daemon_log_sites.py` 77, 5 skipped; `test_daemon_log_tail.py` 16; `test_handler_refusals.py` 317; `test_host_push_storage_retry_budget.py` 61; `test_host_push_store_interleavings.py` 26; `test_live_store_guard_round3.py` 22; `test_protocol_secrecy.py` 325; `test_prune_217.py` 9; `test_prune_restore.py` 20; `test_round10_refusal_context.py` 59; `test_scrub_inline_headers.py` 273; `test_scrub_key_order.py` 70; `test_scrub_no_new_leak.py` 4; `test_scrub_review_r2.py` 75; `test_scrub_review_r3.py` 20 and 1 skipped at `85cf2af35` (hold 1a), 29 at `ca17ca12e`; `test_session_credential_env.py` 47; `test_sessions_handoff.py` 117; `test_sessions_handoff_excerpts.py` 153; `test_test_module_names.py` 1; `test_turn_runner.py` 83; `fake/test_daemon_stacks.py` 5 and **1 failed**; `fake/test_record_secrecy.py` 44; `fake/test_state_contract.py` 37.

**Per directory** (16,611 tests collected):
- **`tests/unit`:** 29 of 351 files ran whole. 2,329 passed and 6 skipped (the five real-daemon tests and the opt-in whole matrix, which then passed at `ca17ca12e`), of 14,474 tests. The other 322 files (12,139 tests) did not run.
- **`tests/fake`:** 3 of 113 files ran whole. 86 passed and 1 failed, of 2,137 tests. The other 110 files (2,050 tests) did not run.

**Plain scripts outside the lock,** as round 2's probes ran, with at most four workers. Slot A was most of the night's only slot, and the brief puts only pytest and timing runs under the wrapper. They ran:
- the mutations and the `902cfe90a` baseline;
- the whole matrices at head and at `902cfe90a`;
- the reference dumps;
- the narrow-form differential;
- the call counts.

One race: at 03:27Z the probe chain's in-lock hold p1, finding its work already done, removed the probe's variant folder while the fold-redaction mutant ran. That run's 41 failures were discarded, and the mutant re-ran clean (28).

### Status

- **Done:**
  - the merge of release/217 at `63c64d6b3`, re-checked in run b;
  - P2-A and P2-B as the review's prototype shows them, and P3-B and P3-C as it proposes;
  - decision 4, stated and pinned;
  - P3-A, in C-23.14 (with its change-log entry) and here, with a dated note under round 2's invariants;
  - review r3's ten executed inputs as regressions, each failing at `902cfe90a`, and the narrow-fix input and the two over-redactions pinned (they hold at `902cfe90a` too, as they should);
  - the two rebuilt generators, the P3-B and P3-C matrices and the drawn joins as tests, and the whole rules matrix run, in a probe and again under pytest (0 of 61,440 cells each time);
  - invariant 5's count test, the mutations, and the cost rows, twice.
- **Failed:** one fake test in hold 4, `test_daemon_stacks_prints_every_thread_of_the_live_daemon`: the fake daemon missed its 5-second start window under load 94 to 103. Its cause is not established, and it was not re-run (Tests, above).
- **Not run:**
  - 322 of `tests/unit`'s 351 files (12,139 tests) and 110 of `tests/fake`'s 113 (2,050 tests). Run b's hold driver had them next, in the order of its plan: 41 files whose names touch scrubbing, handoffs, refusals or conversations first (1,094 tests), then the rest. Its first hold of those was queued at 05:38Z and stopped unrun at 05:40Z for the failed test's re-run;
  - the five real-daemon tests of `test_daemon_log_sites.py` (above);
  - the 16× timing hold (Cost, above);
  - the review's own corpora, which were not kept;
  - Python 3.12;
  - CI (no network, nothing pushed).
- **Follow-ups for the hub:** round 2's, with these:
  - **The header-line fold over-redacts a header's name.** `Cookie: --password` with `Cookie:x SECRET7…` on the next line becomes `Cookie: [REDACTED]` and `[REDACTED]`. The narrow form, with P3-B, gives `Cookie:[REDACTED]` on that line, and it hid the same secrets on the 13,124 texts run b compared it on (P2-B, above). Decision 1 holds; a switch is the hub's call.
  - **Fail closed's digit names** (decision 4) over-redact ordinary flags such as `--k8s-token` and `--oauth2-token`, with the value their fold takes. This is stated in C-23.14; whether it shows up in daemon.log is for the hub to watch.
  - **The stated converse** (the key form) is still the one value a reference (`3fbc4b7c5` only) hides that this scrubber is known to show.

## Fix round 4 (#239 review r4, 2026-10-25)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix4.md`, with `brief-fix4-cont.md` for the continuation. **Review:** that folder's `review-r4.md`, REQUEST_CHANGES with four P3s and no leak against either reference over 213,596 texts, a measured four-line fix for P3-1, and two mutants (MQ1, MQ2). This round built the hub's four decisions, found and fixed one more class beyond the review (a fold, a refused `Bearer`'s line or the `Bearer` rule reaching into a later line-start header), and corrected C-23.14 and this report. The work stayed in the job's worktrees: no Git remote operations, no sub-agents, nothing pushed. All test values are dummy `SECRET7…` strings.

**Two runs.** The first (job `20261025-083053-pr239-fix4`) made `738ddb3fe` (the tests), `f9c2dda19` (P3-1), `a253c9790` (P3-4 a), `5ad476a8d` (P3-4 b), `394c66497` (a pin) and `d427fe004` (C-23.14). Between 18:36 and 18:44Z the host lost its network and the run stopped, with tests for the line-start class uncommitted and no code for it; none of its probe or hold results is used here. The continuation (job `20261025-125156-pr239-fix4-cont`, from 20:13Z) read each of those commits against the brief, applied the saved diff (`b609af3e4`, its mechanism comment corrected and its matrix widened), widened the class (`dba062c8c`), fixed it (`f100b523e`), amended C-23.14 (`f3179a865`, `a493c30dc`), found and fixed P3-1's cost (`a2154df7b`, `ce77f5d77`), wrote this section, and ran every probe, mutation and cost run below. Its holds got no slot (Tests, below).

**Names in this section.**
- **`c469f44b6`** is this branch's head before this round: the reviewed `79e2d4988` and the hub's two merges of release/217 (`45cd76826` at `8f1c65f69`, `c469f44b6` at `0ab7cebaf`).
- **The references** stay `473fe2941` and `c2e080e44`. The tests use the frozen `scrub_473fe2941` for both and check `3fbc4b7c5` (`scrub_release217`) too; the probes below ran the real trees, from `git archive`.
- **`902cfe90a`** is the head before round 3.

### The merges

`git diff 79e2d4988 c469f44b6 -- subfleet/sessions/` shows only release's own changes, from #221 (D-SH1): in `handoff.py`, its imports, `sandbox_for`'s policy routing (`resolve_sandbox`, `git=`) and `handoff()`'s committed-repository probe. No line of the scrubber changes.

### Findings: cause and fix

**P3-1: the header-line fold skipped the header rule of the line it took.**
- **Cause:** round 3's P2-B made a fold whose run starts at a header's name (`_header_name_at`) take that header's line through `_kept_span`, which ends the span before a credential key, flag or `Bearer` that ends the line and owns a value past it. The rule that folded (the header, inline, `Bearer` or quoted rule) was then past the header, so a header whose whole value is a lone flag never took the value on the line after. `Cookie: --password\nCookie: --api-key\nSECRET7c3` showed `SECRET7c3` in the excerpt, whole and `clean` modes; `902cfe90a` hid it in all five, and both references show it.
- **Fix (`f9c2dda19`, review r4's measured lines):** when `_kept_span` would end the line early, the fold takes nothing, and the line's own header rules read it.
- **Result:** the six inputs (the review's and its five sibling leads) give their lead's redaction, then `Cookie: [REDACTED]` and `[REDACTED]`, in every mode. All 13 of round 3's pins hold: round 3's file passes whole at this head, as plain calls (Tests, below). Invariant 6 makes it a property.
- **Its cost, found this round (decision 1 proved wrong in a test):** the cost run below put two units' growth at 4× at 315× and 69×. Each line of those units ends where `cost.py` cuts the repetition, in a credential flag glued to the long trailer, which owns a value past the line, so every fold at a header's name on the line takes nothing. Every such fold computed that afresh through `_kept_span`, whose key check walks the line's last identifier in Python, so the work was the folds times the identifier. Round 3's line fold had taken the line at the first fold, and its rule's cursor skipped the rest.
  - **The counterexample:** `-H 'Cookie: --SECRET7a1_token 'Cookie:x ` 1,000 times, then `--` and a 40,008-character identifier ending `password` (80,010 characters): 12.5 s at `d427fe004` and `f3179a865`, 19 ms at `c469f44b6`; 839, 2,792 and 12,510 ms at 20,010, 40,010 and 80,010 characters, so quadratic.
  - **The smallest change that keeps the decision:** the fold still takes nothing there, but the answer is decided once per line (`_ENDS_EARLY`, `ce77f5d77`). A later fold at a header's name on that line, before where the span would have ended, takes nothing without the walk: the same item ends its span. Only that answer is kept, so a fold that takes its line still computes it, MQ2 stays quadratic and the count test still kills it. `_scrub` clears it at its end, so no text outlives its scrub there.
  - **Result:** 19, 36 and 68 ms at the three sizes. The new count test (`test_folds_before_a_line_ending_in_an_owning_flag_read_the_line_once`, `a2154df7b`), two units before a flag trailer, grew 15.6× at 4× input before the change and 4× after. Over all 89,084 texts of the differential below, every mode's output is the same before and after it (hashed whole, not only what shows). C-23.14's sentence on linear scans names it.

**P3-2: the over-redaction failing closed costs, stated.** Fail closed stays the policy, so nothing is narrowed. C-23.14's sentence on that cost (`d427fe004`) names each kind review r4 found, and `test_failing_closed_over_redacts_as_stated` pins each at its exact output in the excerpt and whole-text scrubs and, folded, in daemon.log's line and `detail_line`:
- **the line fold past a quoted header name's closing quote:** `curl --oauth2-token "Authorization: Bearer abc" -X POST https://api.example.com` becomes `curl [REDACTED] [REDACTED]`; `deploy --SECRET7a1_token "Cookie:x" --password SECRET7p9` becomes `deploy [REDACTED] [REDACTED]`, the `--password` flag's name with it;
- **P3-B's prefix rule on a name prefix that holds a digit:** `X-Auth0-Authorization: Basic x` becomes `[REDACTED]Authorization: [REDACTED]`; `curl -H 'X-Amz1-Cookie: a=b' https://example.com` becomes `curl -H '[REDACTED]Cookie: [REDACTED]' https://example.com`; `session_v2_cookie: abc123` becomes `[REDACTED]cookie: [REDACTED]`;
- **decision 4's kind, chained through P2-A's stop:** `--SECRET7a1_token\nKEY=--db3-password\necho done` becomes `[REDACTED]\n[REDACTED][REDACTED]\n[REDACTED] done`;
- **and the one already in the follow-ups** (`394c66497`): the line fold takes the next header's name, so `Cookie: --password\nCookie:x ok` becomes `Cookie: [REDACTED]\n[REDACTED]`.

**P3-3: the call-count test missed a quadratic fold in the flag rule.**
- **Cause:** no unit put several flag-rule folds on one line, since the inline header rule takes an unquoted header's rest of line first. And the trailer `"a" * tail + "password"` is one run of base64 characters, which `_LONG_BASE64_RE` replaces before the header, plain and flag passes read it.
- **Fix (`738ddb3fe`):** the unit `--SECRET7a1_token 'Cookie:x' ` (`flag-folds-sharing-a-line`), and a trailer no token rule takes, `("a" * 99 + "_") * (n // 100) + "password"` (`_trailer`). MQ2 is committed as a mutant (`_mq2`), which scrubs exactly as the head does (`test_mq2_scrubs_as_the_head_does`) and which the count test kills (`test_the_count_test_kills_mq2`).
- **`detail_line`'s counts** now come from its prefixed scrub (`suggestions._scrub_line`), all of its work that grows with the input. Through `refusals.detail_line`, the line bound's check scrubs at most the 2,000 characters the line keeps, a step of 434 walk calls on the lone-flag header lines once the line passes that bound, between 100 and 150 units: there the walk grew 4.13× (3,346 to 13,830), past the 4.05 bound, and through `_scrub_line` 4.00× (3,346 to 13,396), measured this run.
- **Counts at this head**, 50 units before a 2,008-character trailer and 200 before an 8,008-character one: the walk on the new unit goes from 2,159 to 8,609 (3.99×), with one `_kept_span` call per scrub, in all three modes, and every other unit grows at most 4×. MQ2's walk goes from 100,600 to 1,602,400 (15.9×) and fails it. With round 3's eight units and trailer, MQ2 passes all eight, so the test at `c469f44b6` did not catch it.
- **The report's claim** that round 3's counts back linearity is corrected by a dated note under round 3's Cost.
- **MQ1** (a copy of the rest of the text on every header-line fold) is equivalent within the work bound: review r4 measured no growth it could see within 262,144 characters, the most any rule but the block rules reads at once. No test is built for it, as the brief decides.

**P3-4 (a): the prefix rule missed a line-start name parted from its colon by other whitespace.**
- **Cause:** P3-B's prefix rule ran only in the inline rule, which reads a name before spaces or tabs and `:`. `_HEADER_RE` also reads a line-start name before any whitespace and `:` (`\s*:`), and kept it whole.
- **Fix (`a253c9790`):** `_header_sub` applies the rule (`_secret_prefix`) to every name it reads.
- **Result:** `x\nSECRET7ab1Cookie` and `: v` after a line break, `\x0b`, `\x0c`, `\xa0` or `\r` give `x\n[REDACTED]Cookie`, the separator, and `: [REDACTED]`, in every mode.

**P3-4 (b): daemon.log's line kept the next line's words after a name the prefix rule had redacted.**
- **Cause:** once the prefix rule left `[REDACTED]Cookie:` at the folded line's start, `_HEADER_RE` no longer read it (`[` is no name character), and the inline rule skips a value that starts with `[REDACTED]`.
- **Fix (`5ad476a8d`):** the second scan reads such a name, at a line start, as the line-start rule reads one: its value goes, already redacted or not.
- **Result:** daemon.log's line for `SECRET7ab1Cookie: x\nSECRET7c3` is `[REDACTED]Cookie: [REDACTED]`, as `902cfe90a`'s was with the name whole. The other modes keep the next line, `[REDACTED]Cookie: [REDACTED]\nSECRET7c3`, as both references do.

**Found beyond review r4: a fold, a refused `Bearer`'s line or the `Bearer` rule reached into a later line-start header.**
- **Cause:** `_HEADER_RE` reads a line-start header whose name its colon parts by any whitespace; `_header_name_at`, which a fold consults, reads only spaces and tabs there. So with `Cookie: --password`, then `Set-Cookie` and `: v SECRET7v2` on the next two lines, the fold's run `Set-Cookie` went through `_kept_span`. That ended it before the header word, a key whose value is past the run, or took the whole name where the plain rule cannot read the value (`;v …`). Either way no header rule read the name again, and the plain rule took only `v`. The line a header's refused `Bearer` takes (`_refused_bearer`) ended inside the name the same way, and the `Bearer` rule took the part of the name before its credential word when that read as a token (`SECRET7ab1Set-`).
- **Scope:** at `c469f44b6` and `902cfe90a`, `SECRET7v2` shows in the excerpt, whole, `clean` and `detail_line` modes, where both references hide it. In this round's 5,760-cell matrix of the class, `c469f44b6` shows the value in 2,925 cells, 1,560 of them where both references hide it, and 75 more where `3fbc4b7c5` does. Review r4 did not report it. The first run's saved diff held tests for the fold part and no code. P3-1's fix also routed one join into the class (`ROUTED` in the tests): `902cfe90a` and `c469f44b6` hid its `SECRET7v2`, and with P3-1's fix, at `d427fe004`, it showed in the excerpt and `clean` modes (both references show it).
- **Fix (`f100b523e`):** `_parts_line_start_header` says when a span from a run on a later line would end before the colon of a header `_HEADER_RE` reads at that line's start. There the fold (`_fold_value`), the refused `Bearer`'s line (`_refused_bearer`) and the `Bearer` rule's partial take (`_bearer_refusal`) take nothing, and the line-start rule takes the header's value whole, and its name's secret part (P3-4 a). A refused `Bearer`'s line that already reaches the colon on the run's own line (`\x0b`, `\xa0` or `\r` before `:`) is unchanged and still takes that line whole. A first form that ignored where the colon is showed 75 values `c469f44b6` hid (`x Cookie: Bearer`, then `SECRET7ab1Set-Cookie\x0b: v SECRET7v2`) and was not kept.
- **Result:** `Cookie: [REDACTED]\nSet-Cookie\n: [REDACTED]` and `Authorization: [REDACTED]\nSet-Cookie\n: [REDACTED]`, as in both references, and `Authorization: [REDACTED]\n[REDACTED]Cookie\n: [REDACTED]` where both references show both secrets. No cell of the 5,760 shows the value at this head.
- **Against the brief's decisions:** this is the smallest change I found that keeps decision 1 (P3-1's fix) and decision 4's intent (no change shows what a reference hides), since the join P3-1's fix routed is one of the class's cells. C-23.14 states it in a new sentence after the refused-`Bearer` one, with two executed examples (`f3179a865`).

### Invariants

Each holds for every input, with its stated exception and scope, and is executed by the tests named. Invariants 1 to 5 are round 3's, re-run over round 3's generators and this round's classes.

1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides.
   - **The stated converse** is unchanged: `invalid Bearer SECRET7_token: expired` keeps `SECRET7_token`, which both references show and only `3fbc4b7c5` hid.
   - **Scope:** the generators below, round 3's, round 2's and round 1's (each named in its round's section), and every executed input of reviews r1 to r4, each a regression. The real-tree differential below adds a rebuild of review r4's corpus classes and the whole rules matrix.
   - **Tested by:** `test_scrub_review_r4.py`, `test_scrub_review_r3.py`, `test_scrub_review_r2.py`, `test_scrub_no_new_leak.py` and the cut tests.
2. **Monotone:** a second scrub never shows a character the first hid (`_unstable`). Exact idempotence still does not hold, as round 2 states.
3. **Excerpts agree with the whole** at every line break `_neutral_break` allows (`_splits`). Nothing this round adds crosses a break: a fold that leaves a line-start header takes nothing past the line it starts from.
4. **Ownership:** a value in quotes, or a flag's value, is redacted as its bare twin is, within C-23.14's limits (round 3's twin matrices, unchanged).
5. **Cost:** every unit's work grows at most about 4× at 4× input (the count test, now killing MQ2), and review r4's cost rows stay within 1.3× of `c2e080e44` (Cost, below).
6. **A lone flag survives a fold:** a header whose whole value is a lone credential flag takes the next line's value in every mode, whatever fold took its line, as C-23.14 says.
   - **Property:** review r4's six leads × 5 gaps × 6 header names (`Cookie`, `Authorization`, `Set-Cookie`, `Proxy-Authorization`, `X-Authorization`, `cookie`) × 3 lone flags (`--api-key`, `--password`, `--client-secret`) × 5 gaps, each before `SECRET7c3`: 2,700 cells, none of which shows any `SECRET7` value in any mode.
   - **Tested by:** `test_a_header_s_lone_flag_takes_the_next_line_s_value_whatever_fold_took_its_line`.

### The generators

**This round's, as tests** (`test_scrub_review_r4.py`):

| Matrix | Built from | Cells | `c469f44b6` | This head |
|---|---|---|---|---|
| P3-1's class | 13 folding leads (round 3's 11 and two of review r4's six) × 5 gaps × 8 header lines that end in an owning item × 3 gaps × 2 values | 3,120 | 270 show a value `902cfe90a` hid; 0 a reference hides | 0 / 0 |
| Invariant 6, a lone flag | review r4's six leads × 5 gaps × 6 header names × 3 lone flags × 5 gaps | 2,700 | all 2,700 show `SECRET7c3` (the excerpt among their modes) | no `SECRET7` value in any mode |
| The line-start class | 16 leads (13 folding, 3 refused `Bearer`s) × 5 gaps × 3 names × 8 separators × 3 values | 5,760 | 2,925 show the header's value: 1,560 where both references hide it, 75 more where `3fbc4b7c5` does | no `SECRET7v2` in any mode; 0 / 0 |

"0 / 0" is against 473fe2941 (= `c2e080e44`) and against `3fbc4b7c5`, with no excerpt cut that disagrees with the whole text and no second scrub that reveals anything.

**Round 3's and round 2's, kept green** (`test_scrub_review_r3.py`, `test_scrub_review_r2.py`, as plain calls; Tests, below): the rules matrix's seeded 6,144-cell slice, the 1,760-cell header-name matrix, the P3-B and P3-C matrices (720 each) and the drawn joins; round 2's 768-cell flag, 630-cell union and 4,032-cell next-line matrices, the ownership matrix and the joined cells.

**The real-tree differential** (plain scripts, three workers, the real `git archive` trees of `473fe2941`, `c2e080e44`, `3fbc4b7c5`, `902cfe90a` and `c469f44b6` against this head, all five modes; the head's rows also check every allowed excerpt cut and a second scrub):
- **A rebuild of review r4's corpus classes,** 27,644 texts: this round's three matrices (11,580); 10,064 more on P3-4's edges (9 leads that put a secret-holding name at a line start, mid-line, after a fold or after `[REDACTED]`, × 4 names × 14 separators before the colon, among them a line break, a blank line, `\r\n`, `\x0b`, `\x0c`, `\xa0`, `\r`, `\x1c` and `\u2028`, × 5 values × 4 next lines: 10,080, of which 16 are already in the matrices); and 6,000 seeded joins (seed 4239) of two or three pieces of every generator of rounds 2 to 4, by ten joints that include `\x0b` and `\xa0`. Review r4's own corpus of 213,596 texts was not kept, so this stands in for it.
  - **This head:** 0 texts show anything 473fe2941, `c2e080e44` or `3fbc4b7c5` hides; 0 splits; 0 reveals. The two reference trees give the same output on every text.
  - **`c469f44b6`:** 1,992 texts show a value both references hide: the 1,560 line-start cells and 432 joins. So the class beyond review r4 is in its joins too.
  - **Against the heads before:** this head shows nothing `902cfe90a` or `c469f44b6` hid, but in three joins (`detail_line` only, each a `SECRET7c3` that both references show too). Each holds a line-start header whose name a fold cut at `c469f44b6` (the class above), which left that header's lone `--password` unredacted. In daemon.log's folded line the inline rule then read that name as a header and took the rest of the line, a later `SECRET7c3` with it. This head redacts the header and its flag in the first scrub, so the folded line's inline value starts with `[REDACTED]` and is left alone, keeping the words after it, as C-23.14 states and both references keep them. The first of the three: `Cookie: --password\nSECRET7ab1Authorization\u2028: --password\n\nSECRET7c3\n\nx\r\nxSECRET7ab1-Cookie\x1c: x\n\nSECRET7c3`, whose excerpt at this head hides the first `SECRET7c3`, which `c469f44b6` showed.
- **The whole rules matrix,** 61,440 cells, run once (the brief's full run): at this head, 0 cells show anything 473fe2941, `c2e080e44` or `3fbc4b7c5` hides, 0 splits, 0 reveals, and the reference trees agree on every cell. It stays out of the default run behind `SF_SCRUB_FULL_MATRIX=1`, as round 3 left it.

### Mutations

Each change was taken out on its own, in a copy of the worktree's `subfleet` at `f100b523e`, before the per-line answer (which changes no output; P3-1, above), and the named test modules were run against it as plain calls, three variants at a time, outside the lock (as rounds 2 and 3 ran theirs).
- **This round's changes** run `test_scrub_review_r4.py` but for its three largest matrices (31 cases), and P3-1's the lone-flag matrix too. The line-start fix and its parts run its nine inputs, the routed join and its 5,760-cell matrix (11).
- **Round 3's changes** run round 3's inputs, pins and size checks (15) and the same 31 of round 4.
- **Round 2's changes** run `test_scrub_review_r2.py` but for its six largest matrices (56 cases), and round 3's 15.
- **The head**, as the control, passes every case of every set: 0 failures.

| Removed | Named tests that fail |
|---|---|
| P3-1's fix (`f9c2dda19`; the fold takes the header's line through `_kept_span` again) | the six P3-1 inputs and invariant 6's lone-flag matrix (7) |
| P3-4 (a), the prefix rule in `_header_sub` | the five P3-4 (a) inputs and the `bearer-token-like-name` pin (6) |
| P3-4 (b), the second scan's read of `[REDACTED]Cookie:` at a line start | the P3-4 (b) daemon.log line (1) |
| MQ2 (the flag rule's cursor stops at the flag) | `test_the_new_folds_read_each_line_once[flag-folds-sharing-a-line]` (1). With round 3's eight units and trailer it passes all eight |
| The per-line answer (`_ENDS_EARLY`), as at `f3179a865` | `test_folds_before_a_line_ending_in_an_owning_flag_read_the_line_once`, both units (2): the walk grows 15.6× |
| This round's line-start fix, all three parts | the nine line-start inputs, the routed join, the 5,760-cell matrix (11) |
| … its fold part (`_fold_value`) | the five fold inputs, the routed join, the matrix (7) |
| … its refused-`Bearer` part (`_refused_bearer`) | the two refused-`Bearer` inputs, the `bearer-token-like-name` pin, the matrix (4) |
| … its `Bearer` part (`_bearer_refusal`) | the two token-like-name pins, the matrix (3) |
| Round 3's P2-A, `_kept_span`'s `_ending_flag` line | the five P2-A inputs, the chained over-redaction pin (6) |
| Round 3's P2-B, the header-line fold | the three P2-B inputs, the three line-fold over-redaction pins, `test_the_count_test_kills_mq2` (7) |
| P2-B as the narrow return-None form | the same seven (7) |
| The narrow form and P3-B both | the three P2-B inputs, the P3-B input, the narrow-fix input, the five P3-4 (a) inputs, the P3-4 (b) line, the six line-fold and prefix-rule pins, the `bearer-token-like-name` pin, the routed join (19) |
| Round 3's P3-B, in the inline rule and in `_header_sub` | the P3-B input, the narrow-fix input, the five P3-4 (a) inputs, the P3-4 (b) line, the three prefix-rule pins, the `bearer-token-like-name` pin, the routed join (13) |
| Round 3's P3-C, the closing-quote check | the P3-C input (1) |
| Round 2's P1 | 6 of round 2's: three P2-2 inputs, the P3-2 input and its twin, one combined-found input |
| Round 2's P2 | 3 of round 2's: the two P2-3 inputs and one pin |
| Round 2's P3 | 1 of round 2's: `test_owned_before_reads_each_line_end_once` |
| Round 2's P4 | 2 of round 2's: one pin and the bare-CR combined-found input |
| Round 2's fold redaction | 26 of round 2's and 2 of round 3's inputs (28) |

- **Two notes on the runs.** For the two P2-B mutants, `test_the_count_test_kills_mq2` fails by raising pytest's `Failed`, which the plain runner does not catch, so that module's summary line is missing; it was the module's last case. And the round-2 counts are lower than round 3's (9, 5, 2, 2, 28), which ran round 2's file whole.
- **At `c469f44b6` itself,** `test_scrub_review_r4.py` but for the MQ2 count case fails 23 of 33: every regression of this round. The 10 it passes are the seven stated over-redactions, the P3-1 class generator (P3-1 was no leak against the references), the routed join (`c469f44b6` hid it) and MQ2's same-output check.

### Cost

`cost.py` loads this head and `c2e080e44` in one process and measures each in turn, in CPU time: the rows best of seven (five for the 1 MiB row), and the excerpt scrub of each unit at 65,536 and 262,144 characters, best of five. The units are P3-1's adversary (round 2's `_adversary`), the count test's nine, and four lines of this round's classes. **These ran as plain probes outside the lock,** as review r4's did, at load 30 to 90: the wrapper's hold was queued at 22:10Z and again at 23:08Z, and had not got a slot when this was written (Tests, below).

| Row | This head (`ce77f5d77`) | `c2e080e44` | Head / `c2e080e44` | Before the per-line answer (`f3179a865`) | Review r4 |
|---|---|---|---|---|---|
| Excerpt scrub, 262,144 characters of P3-1's adversary | 8.44 ms | 8.68 ms | 0.973 | 1.032 | 1.234, 0.901, 1.264 |
| 1 MiB refusal through `detail_line` | 27.53 ms | 27.06 ms | 1.017 | 1.001 | 0.988, 1.133, 1.031 |
| Excerpt growth at 4× input, 16 units | 1.33–5.19, and 31.21 on one unit (below) | 2.50–5.55 | – | 2.62–4.50, and 315.59 and 69.29 on two units | 2.48–8.67 |

- **The rows are within 1.3× of `c2e080e44`,** before and after the per-line answer.
- **The two outliers before the per-line answer** were P3-1's cost (Findings, above). After it, `flag-folds-sharing-a-line` grows 4.45×.
- **The one outlier after it, 31.21× on `quoted-inline-at-header-names`,** compares two shapes: where `cost.py` cuts the repetition, the 65,536-character line ends cleanly, so its first fold takes the line, and the 262,144-character line ends in a flag glued to the trailer. With the same ending at both sizes (whole units, then the trailer, half the text each), the two affected units grow 3.18× to 4.60× at this head and 2.31× to 5.49× at `c2e080e44`.
- **Absolute cost, that re-measure at 262,144 characters:** this head is 0.44× to 1.17× of `c2e080e44` on three of those four shapes, and 2.66× (217 ms against 81.6 ms) on quoted inline headers at header names before a line that ends in a flag. That is a constant factor, from 4.29× growth: this head redacts each header there with its flag, where `c469f44b6`'s line fold took the line at once (40 ms on `cost.py`'s cut shape). It is not one of the review's rows.
- **The counts** are under P3-3 above: every count unit grows at most 4×, and so do the two new ones.
- **MQ1** was not measured again; see P3-3.

### Tests

**Holds: none ran.** A driver in this job's process tree queued them one pytest at a time through `suite_hold.sh`, each from a `git archive` export (`hold.py`: `pytest.main` in process with `-v` and an alarm at 19 minutes, under `env -u PYTHONPATH -u SUBFLEET_RETENTION_DORMANT -u SUBFLEET_JOB -u SUBFLEET_ATTEMPT`, with the brief's `TMPDIR` and Hypothesis folder), in the brief's order: round 4's and round 3's files; `test_daemon_log_sites.py` and the three contract guards; round 2's file, `test_scrub_no_new_leak.py` and `test_scrub_key_order.py`; `test_scrub_inline_headers.py`; then the files that import `subfleet.sessions` or the scrubber, in four more holds.
- **The first queue,** at `f100b523e` from 21:59Z, had no slot in 69 minutes. It was stopped at 23:07Z, when P3-1's cost fix changed the code.
- **The second,** at `ce77f5d77` (the contract hold at `a493c30dc`, the same code) from 23:08Z, had no slot by 00:00Z, when it was stopped, 13 minutes before the job's 4-hour mark. About eight other holds were waiting throughout, slot A's holders ran for 27 to 51 minutes each, and slot B was shut whenever load passed 120 and taken by others when it opened.
- **The cost hold,** queued at 22:10Z and again at 23:08Z, had no slot either (Cost, above).

**Plain calls instead, outside the lock** (one process, the way the probes and mutations ran, with a runner that expands `parametrize` and `pytest.param` and gives `monkeypatch` and `tmp_path`; not a substitute for the holds):

| File | At `ce77f5d77` (the final code) | At `f3179a865` (outputs identical) |
|---|---|---|
| `test_scrub_review_r4.py` | 36 passed | 34 passed (the two cost cases came after) |
| `test_scrub_review_r3.py` | 29 passed, 1 skipped (the opt-in whole matrix, run as a probe: 0 of 61,440) | 29 passed, 1 skipped |
| `test_scrub_review_r2.py` | – | 75 passed |
| `test_scrub_no_new_leak.py` | 4 passed | – |
| `test_scrub_key_order.py` | 70 passed | – |
| `test_scrub_inline_headers.py` | 273 passed | – |
| `test_conversation_redact.py` | 9 passed | – |
| `test_sessions_handoff.py` | 50 passed; its other 95 cases need its `home`, `repo` and `policy` fixtures, which the runner does not give, so they did not run | – |
| `test_test_module_names.py` | – | 1 passed |

**Per directory:**
- **`tests/unit`:** 8 whole (every file above but `test_sessions_handoff.py`) and 50 of `test_sessions_handoff.py`'s 145 cases of its 370 files ran, as plain calls; none ran under pytest. The other files, and the five real-daemon tests of `test_daemon_log_sites.py`, did not run.
- **`tests/fake`:** none of its 117 files ran, the contract guard `test_state_contract.py` among them.
- **Also not run:** the whole suite's collection (this round's totals are not counted), Python 3.12, and CI.

### Status

- **Done:**
  - the hub's four decisions: P3-1's measured fix, P3-2's kinds stated and pinned, P3-3's count test with MQ2 committed and killed, and P3-4 (a) and (b), with C-23.14 worded to match;
  - the class found beyond review r4, fixed in all three of its paths, with its regressions and a 5,760-cell matrix, and stated in C-23.14;
  - invariant 6 as a 2,700-cell property, and invariants 1 to 5 re-run over round 3's generators and this round's classes;
  - every regression failing at `c469f44b6`, every pin, the mutations, the real-tree differential and the whole rules matrix;
  - C-23.14's amendments with their change-log entry, and this report's correction under round 3's Cost.
- **Found and fixed beyond the brief:** P3-1's cost, quadratic in the folds on a line that ends in an owning flag, decided once per line now (decision 1's smallest change, Findings above).
- **Not run:**
  - every hold: none got a slot, in a first queue of 68 minutes and a second of 52, stopped at 00:00Z; the plain calls in Tests stand in, outside the lock;
  - the cost rows under the wrapper (they ran as plain probes, within the bar);
  - 362 of `tests/unit`'s 370 files whole (`test_invariants_index.py` and `test_daemon_log_sites.py` among them), 95 cases of `test_sessions_handoff.py`, and all 117 files of `tests/fake` (`test_state_contract.py` among them);
  - the five real-daemon tests of `test_daemon_log_sites.py`, Python 3.12, and CI (no network, nothing pushed);
  - review r4's own corpora, which were not kept; the rebuild above stands in.
- **Follow-ups for the hub:** round 3's, with these:
  - **Three `detail_line` values `c469f44b6` hid show again** (the differential, above). Both references show them, and C-23.14 states why: an inline value that already starts with `[REDACTED]` is left alone in daemon.log's folded line. They were hidden only because the fold this round fixed left a header's lone flag unredacted for that line to take.
  - **A refused `Bearer`'s line still ends a header's lone flag early, as P3-1's fold did.** `Authorization: Bearer`, `Set-Cookie: --api-key` and `SECRET7c3` on three lines shows `SECRET7c3` in the excerpt, whole and `clean` modes, as at `902cfe90a`, at `c469f44b6` and in both references, so it is no leak and no regression. C-23.14's lone-flag sentence claims the fold case only. Taking P3-1's rule to `_refused_bearer` too would close it; that is the hub's call.
  - **The two over-redactions round 3 listed** (the line fold takes the next header's name; digit-named flags) stand, now with P3-2's kinds stated beside them.

## Fix round 5 (#239 review r5, 2026-10-26)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix5.md`. **Review:** that folder's `review-r5.md`, REQUEST_CHANGES at `8fde0c4ad`: one P2 (P2-1) with a measured three-edit fix, and four P3s. This round built the hub's five decisions. Two of them proved wrong or incomplete in a test, and each got the smallest change that keeps its intent (below). The round also corrected C-23.14 and wrote this section. The work stayed in the job's worktree (job `20261026-030059-pr239-fix5`, from 10:04Z): no Git remote operations, no sub-agents, nothing pushed. All test values are dummy `SECRET7…` strings.

**Names in this section.**
- **`8fde0c4ad`** is this branch's head before this round: round 4's `0851355ea` and the hub's merge of release/217 at `946ea9f96`. No release merge came in this round.
- **The references** stay `473fe2941` and `c2e080e44`. The tests use the frozen `scrub_473fe2941` for both and check `3fbc4b7c5` (`scrub_release217`) too. The probes ran the real trees from `git archive`.
- **`70aafdcaf`** is the code head: no later commit changes `subfleet/`.

### Findings: cause and fix

**P2-1: a header the line-start rule reads across a line break left its value to no rule.**
- **Cause:** as review r5 found it. `_HEADER_RE`'s `\s*:\s*` crosses a line break on either side of the colon. Round 4 handled a break *before* the colon on three paths. It missed two things: the inline rule's next-line value, and a value that starts on the line *after* the colon, for a fold at a header's name and for a refused `Bearer`'s line.
- **Fix (`008508bbe`, the review's three measured edits):**
  - `_fold_value`'s header branch takes nothing where `_parts_line_start_header` holds;
  - `_parts_line_start_header` also holds when a line break lies between the span's end and the header's value;
  - the inline rule's next-line skip also leaves a header to the line-start rule when `_HEADER_RE` matches at the value's line start.
- **Result:** the five inputs give 473fe2941's exact output in the excerpt and whole modes, and no mode shows `SECRET7v2` (`test_review_r5_s_p2_1_inputs_are_hidden_in_every_mode`).
- **The class as a property:** the review's generator is rebuilt from what it states: 22 leads × 6 gaps × 6 names × 15 separators × 8 values, 95,040 cells, plus tails and neighbours (`p2_1_class`, `_p2_1_drawn`).
  - It runs as a seeded 3,000-cell slice with invariants 1 to 3, and as a Hypothesis property under the suite's ci profile. The whole class runs with `SF_SCRUB_FULL_MATRIX=1`, as round 3's whole rules matrix does.
  - At `8fde0c4ad`, 2,394 of the 95,040 cells show a value both references hide: 1,470 inline headers whose value would start on the next line, 486 folds, 294 at a line-start `Authorization:` whose value would start on the next line, and 144 refused `Bearer`s. The slice holds 73 of them.

**P3-1: the refused-`Bearer` case, and the two regressions against `c469f44b6`.**
- **Decision 2 as given (`ea97e51bd`):** review r5's rule in `_refused_bearer` returns the line only when `_kept_span` keeps it whole. It closed the refused-`Bearer` lone-flag case and the first regression (`Authorization: Bearer`, `SECRET7ab1Set-Cookie : --password`, `SECRET7c3`), and changed round 3's two refused-`Bearer` pins to less redaction, as the hub accepted.
- **It proved wrong over this round's corpora (the decision's counterexample).** Applied to every refused `Bearer`'s line, it also left lines that are no header's to their own rules, where a word no rule owns showed. The minimal input is `Authorization: Bearer` and `client_secret=x SECRET7abc13 --password` on two lines. It showed `SECRET7abc13` in the excerpt, whole, `clean` and `detail_line` modes (4 of the round's 10,000 joins). `8fde0c4ad` and `c469f44b6` hid it, and both references show it. The hub had accepted the less redaction on the premise that no value shows.
- **The smallest change that keeps the intent (`70aafdcaf`):** P3-1's rule exactly as the fold applies it. Only where the run is a header's name (`_header_name_at`) does a line that would end early take nothing. Both cases the decision closes are header lines and stay closed. Every other line ends before its item, as before. So round 3's two pins are unchanged from `8fde0c4ad` (restored), and the counterexample is a regression (`test_a_refused_bearer_s_line_that_is_no_header_s_keeps_its_redaction`).
- **The second regression was still open after decisions 1 and 2:** `Cookie: --password`, `Set-Cookie:`, `Cookie: --api-key`, `SECRET7m1`. It is review r5's open case of the lone-flag sentence (`Authorization:`, `Cookie: --api-key`, `SECRET7c3`). Round 4's fold fix leaves `Set-Cookie:` to the line-start rule, which read its value from the next line and ended it before the lone flag.
  - **The smallest change that closes both (`be6fb4330`):** `_header_sub` leaves a header whose value would start on a later line than its colon, at that line's own header, to that header, and takes nothing. This is exactly the inline rule's next-line skip, with decision 1's two-part test. The name's secret part still goes.
  - **Result:** `Set-Cookie:`, `[REDACTED] [REDACTED]` and `[REDACTED]`, in every mode. The named-value rule takes `Cookie:` as `Set-Cookie`'s plain value.
- **The lone-flag sentence:** now true for every case its property draws. Round 4's 2,700 cells (folds), and this round's 10,800: 13 folding leads, 5 refused `Bearer`s and 6 headers whose value would start on the next line, × 5 gaps × 6 names × 3 lone flags × 5 gaps. At `8fde0c4ad`, 1,650 of the 10,800 showed `SECRET7c3`: 1,200 refused `Bearer`s and all 450 at a line-start `Authorization:`. At this head, none does. Round 4's "(now true)" is corrected in its change-log entry. *Corrected in round 6:* the property never draws two cases the sentence then covered, a quoted header on the next line and a next-line header that isn't at the value's start. Both show the value, as in both references, and C-23.14 now names them (review r6, P3-1; Fix round 6).

**P3-2: an excerpt cut disagreed with the whole text (invariant 3).**
- **Fix (`5aa31d5b5`):** the prefix rule keeps a name's leading `-`, at both places it runs (the inline rule and `_header_sub`). The flag rule's fold, the scrub's last pass, therefore refuses `-[REDACTED]Cookie:` as an excerpt cut before it does. `--1_token` and `-1_kenCookie:` give `[REDACTED]` and `-[REDACTED]Cookie:` whole and cut alike.
- **One more part, found by its mutant:** the second scan reads `-[REDACTED]Cookie:` at a line start as it reads `[REDACTED]Cookie:` there (round 4's P3-4 b).
  - Without it, daemon.log's line for `-SECRET7ab1Cookie: x` and `SECRET7c3` showed `SECRET7c3`. `8fde0c4ad` hid it and both references show it, and no test saw the difference.
  - It is pinned now (`3cc613d31`).
- **Pins:** none changed.
- **Result:** no excerpt cut disagrees with the whole text over the 500 dash-led cells or the round's 243,680 corpus texts.

**P3-3: `_ENDS_EARLY` per scrub (`b7ac93c23`).**
- **Change:** it is a `threading.local`, and `_scrub` and `quoted_value_spans` drop it in `try/finally` (`_forget_ends_early`). The rules move unchanged into `_scrub_rules`. The comment that said `_scrub` alone clears it is corrected.
- **Tests:** after `quoted_value_spans` and after a `_scrub` that raises, nothing is held. Two threads that take turns at every fold keep the single-thread counts: `_kept_span` once and the walk 104,019, on the 80,010-character shape.
- **The same test with one shared slot:** both threads show 1,000 `_kept_span` calls and a walk of 40,075,008. With strict turns every fold misses, where review r5's natural interleaving measured 2–3×.

**P3-4: stated, not fixed; the review's scope was short.**
- **The input:** `\nAuthorization:"--1_token` and `=r SECRET7c5`. The review measured it in `detail_line` only. At `8fde0c4ad` and at this head, it shows `SECRET7c5` in the excerpt, whole and `clean` modes and in `detail_line`. Both references hide it in all five.
- **Mechanism, traced pass by pass:**
  - Both references' quoted pass masks `"--1_token` and the fold's `=r` as one span, which joins the two lines, so the line-start header then takes the rest of the joined line. In a whole-text scrub their span can run on past later lines.
  - This scrubber ends the quoted value before the secret-named flag (C-23.14), and the flag rule takes the flag with its folded run only. *Corrected in round 6:* review r6 traced it otherwise: the quoted pass takes the flag and its run as two spans, and the flag rule never sees them (its P3-2). The case is fixed in round 6, so C-23.14 no longer states it.
  - Unquoted (`Authorization:--1_token`), both references show it too.
- **Decision 5's condition fails:** changing the folded line's skip cannot close all four inputs. A prototype that drops that skip hides `SECRET7c5` in `detail_line` only, and the excerpt, whole and `clean` modes still show it. So the case is stated beside the converse in C-23.14, and the four inputs are pinned at today's output in all five modes (`test_a_quoted_header_s_flag_folding_onto_an_assignment_shows_the_next_word_as_stated`, marked `documented_limitation`).
- **Scope in the round's corpus:** 104 texts (4 targeted, 39 memo-fuzz, 61 joins). All have this shape, and all showed at `8fde0c4ad` too. One of them also shows a later `SECRET7c3` in the whole-text and `detail_line` modes, by the same mechanism: the references' whole-text quoted span runs on to the end.

**The 27 daemon.log-only trailing values (review r5).** The review's corpus was not kept. In this round's 243,680 texts, three show a trailing `SECRET7t5` in daemon.log's line or `detail_line` that `8fde0c4ad` hid, and both references show all three. Each holds a header that `8fde0c4ad` left unredacted in the first scrub (this round's P2-1 class). The folded line's inline rule then read that header and took the rest of the line. This head redacts the header in the first scrub, so the folded line's inline value starts with `[REDACTED]` and is left alone. That is the folded-line trade C-23.14 states, and the change-log entry names them as such. *Restated in round 6:* "3" is this round's corpus only. Review r6 counted 763 of its 53,825 line-start texts that be6fb4330's skip makes show in the line modes (against that head without the skip), and round 6's own change adds 389 of its 48,758; all are in the line modes and shown by both references (Fix round 6).

### Invariants

Each holds for every input, with its stated exceptions and scope. Each is executed by the tests named.

1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides.
   - **Two stated exceptions:** round 2's converse (`invalid Bearer SECRET7_token: expired`, a value only `3fbc4b7c5` hid) and P3-4's quoted-value join. *Round 6:* P3-4's join is fixed, so the converse is again the one exception.
   - **Scope:** every generator of rounds 1 to 5 and the round's corpus (below).
   - **Tested by:** `test_scrub_review_r5.py` to `_r2.py`, `test_scrub_no_new_leak.py` and the cut tests.
2. **Monotone:** no second scrub reveals anything over the corpus (0 of 243,680).
3. **Excerpts agree with the whole** at every cut `_neutral_break` allows. P3-2's case is fixed, and 0 of 243,680 corpus texts split.
4. **Ownership:** round 3's twin matrices pass unchanged.
5. **Cost:** every count unit grows at most about 4× at 4× input, and review r5's rows stay within 1.3× of `c2e080e44` (Cost, below). The per-line answer is per thread, so concurrent scrubs keep the single-thread counts.
6. **A lone flag survives whatever takes its header's line:** a fold, a refused `Bearer`'s line, or a header whose value would start on that line. *Round 6:* but for two cases review r6 found, which C-23.14 now names.
   - **Executed as properties:** 2,700 cells (round 4) and 10,800 (this round). No `SECRET7` value shows in any mode.

### The corpus

Plain scripts, three workers, outside the lock. The texts ran through the real `git archive` trees of `70aafdcaf`, `473fe2941`, `c2e080e44`, `8fde0c4ad`, `c469f44b6` and `3fbc4b7c5`, in all five modes. The head's rows also check every allowed excerpt cut and a second scrub.

| Set | Texts | Head: shows what both references hide | `8fde0c4ad` | `c469f44b6` | Head: splits / reveals |
|---|---|---|---|---|---|
| P2-1's class (`p2_1_class`) | 95,040 | 0 | 2,394 | 7,362 | 0 / 0 |
| Its tails and neighbours | 24,156 | 0 | 933 | 2,697 | 0 / 0 |
| Round 2's four matrices (flag, union, next-line, ownership pairs) | 6,150 | 0 | 0 | 0 | 0 / 0 |
| Round 3's whole rules matrix | 61,440 | 0 | 0 | 0 | 0 / 0 |
| Round 3's header-name, P3-B and P3-C (pairs) matrices | 3,920 | 0 | 0 | 0 | 0 / 0 |
| Round 4's three matrices | 11,580 | 0 | 0 | 1,560 | 0 / 0 |
| This round's lone-flag leads and dash-led names | 11,300 | 0 | 0 | 0 | 0 / 0 |
| Every regression input of rounds 2–5 | 97 | 4 (P3-4) | 9 | 16 | 0 / 0 |
| Memo-fuzz, seed 5239 (lines of folds at header names, ending or not in an owning item) | 19,997 | 39 (P3-4) | 945 | 1,160 | 0 / 0 |
| Seeded joins, seed 5240 (two or three texts of every pool, ten joints) | 10,000 | 61 (P3-4) | 213 | 808 | 0 / 0 |
| **Total** | **243,680** | **104, all P3-4** | **4,494** | **13,603** | **0 / 0** |

- **The two reference trees** give the same output in every mode on every text. Against `3fbc4b7c5` the head shows nothing beyond those same 104.
- **Against `8fde0c4ad`:**
  - the head hides more in 21,102 texts;
  - it shows something `8fde0c4ad` hid in 3: the folded-line trade above, in daemon.log's two modes only, each shown by both references too.
  - Before the `70aafdcaf` correction, decision 2 as given showed 4 more (P3-1, above).
- **Against `c469f44b6`:** the head shows something `c469f44b6` hid in 169 texts, all shown by both references too. Most are round 4's fold fix, which takes nothing where `c469f44b6`'s fold took a whole header line.

### Mutations

Each change was taken out on its own, in a copy of `70aafdcaf`'s `subfleet`, and `test_scrub_review_r5.py` (24 cases, 1 opt-in skip) ran against it as plain calls outside the lock. Round 3's file also ran against decision 2 as given. The head, as the control, passes all 24.

| Mutant | Killing tests |
|---|---|
| Decision 1: P2-1's three edits out | P2-1's five inputs, the class slice and the drawn property (7) |
| Decision 2: the refused-`Bearer` line at a header's name ends early again | the two refused-`Bearer` regressions and the 10,800-cell lone-flag property (3) |
| Decision 2 as given (every refused `Bearer`'s line) | `test_a_refused_bearer_s_line_that_is_no_header_s_keeps_its_redaction` (1); round 3's two refused-`Bearer` pins (2) |
| The line-start next-line skip out | the two line-start regressions and the lone-flag property (3) |
| Decision 3: the prefix rule takes the leading `-` again | P3-2's cut, the 500 dash-led cells and the dash-led daemon.log line (3) |
| Decision 3's `led` part out | the dash-led daemon.log line (1; it survived every case before that test was added) |
| Decision 4: one shared slot, cleared only at `_scrub`'s end | the two leftover-text tests and the two-thread count test (3) |
| Decision 5's alternative: the folded line's skip dropped | P3-4's four pins (4) |

### Cost

`cost.py` ran one persistent process per tree (`70aafdcaf`, `8fde0c4ad`, `c2e080e44`), as plain probes outside the lock at load 26 to 37. Each sample asks every tree in turn, alternating the order, so drift falls on all alike. The times are CPU (`thread_time`), with 7 samples per row, in two runs.

| Row | Head (min / median, ms) | `c2e080e44` | Head / `c2e080e44` (min, median) | `8fde0c4ad` / `c2e080e44` |
|---|---|---|---|---|
| Excerpt, round 2's adversary, 261,666 characters | 8.16 / 9.94; 10.60 / 13.45 | 7.66 / 11.68; 12.71 / 14.70 | 1.066, 0.851; 0.834, 0.915 | 1.080, 0.878; 1.035, 1.039 |
| 1 MiB `detail_line`, `"Bearer " + "--s"` (the report's adversary row) | 33.4 / 51.4; 52.8 / 55.9 | 193.9 / 268.9; 286.0 / 293.4 | 0.172, 0.191; 0.185, 0.191 | 0.174, 0.161; 0.159, 0.181 |
| 1 MiB `detail_line`, ordinary header lines (six forms) | 1,338 / 1,572; 1,359 / 1,528 | 1,098 / 1,261; 1,109 / 1,325 | 1.218, 1.247; 1.225, 1.153 | 1.311, 1.240; 1.196, 1.165 |

- **The three rows are within 1.3× of `c2e080e44`** in both runs, and this head is within noise of `8fde0c4ad` on each. Review r5's 1 MiB shapes were not kept, so the header row is this round's own: six ordinary forms (line-start, inline `curl -H` and `wget --header`, in prose).
- **One more row, not the review's:** a 1 MiB `detail_line` of this round's class shapes costs 1.70× and 1.74× (min) of `c2e080e44`. That is the same at `8fde0c4ad` (1.60×, 1.42×), so it is a constant factor this round does not change.
- **The quadratic shape** is round 4's 80,010-character shape at 20,010 to 160,010 characters.
  - **Head:** 14.6, 29.5, 65.3 and 119.8 ms (min), growing 2.02, 2.22 and 1.84× per doubling; the second run grew 2.50, 2.27 and 1.82×. That is linear.
  - **`c2e080e44`:** 1.9–4.1× faster (min, both runs), a constant factor at every size, as review r5 found (2.7–3.6×).

### Tests

**Red at `8fde0c4ad`,** under the lock (10:32:27Z, slot B, load 20): `test_scrub_review_r5.py` as first committed (`ef5b1a95c`) had 17 failed, 5 passed and 1 skipped.
- **Failed:** every P2-1, P3-1, P3-2 and P3-3 case.
- **Passed:** the class's size check, and P3-4's four pins of today's output.
- **The thread test there:** each thread walked 40,075,008 times, against 104,019 alone.

The two regressions added later were run at `8fde0c4ad` as plain calls:
- the dash-led daemon.log line fails there (`8fde0c4ad` drops the `-`);
- the refused-`Bearer` counterexample passes there and fails at `b7ac93c23` (decision 2 as given).

**Holds, at `70aafdcaf`** (the code head; only tests and docs change after it), one pytest per hold. The guards also ran again after the docs commits (Status).

| Hold | Lock | Files and results |
|---|---|---|
| 1, 11:40:16Z | slot A, load 22 | `test_scrub_review_r4` 36 passed, `_r3` 29 passed and 1 skipped (the opt-in matrix), `_r2` 75 passed, `_r5` 23 passed and 1 skipped (the opt-in class): 163 passed, 2 skipped |
| 2, 11:42:42Z | slot A, load 21 | `test_scrub_inline_headers` 273 passed |
| 3, 11:44:26Z | slot A, load 24 | `test_daemon_log_cut_windows` 177 and `test_daemon_log_handler` 128: 305 passed |
| 4, 11:46:08Z | slot A, load 43 | `test_scrub_no_new_leak` 4, `test_scrub_key_order` 70, `test_cpu_scaling` 3, `fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25: 140 passed |

- **The same four holds at `b7ac93c23`** (decision 2 as given) also passed (10:41Z to 10:51Z), with one exception. In hold 1, `test_unclosed_values_sharing_a_line_end_read_it_once` (a CPU-time growth bound) read 6.26 against 5.5 at load 31.9. Rerun once under the lock at load 37.4, it passed. Side-by-side probes put that ratio at 2.1–5.1 for the head and 2.3–9.1 for `8fde0c4ad`.
- **The dash-led daemon.log test** (`3cc613d31`) came after these holds, and ran in the final hold (Status).

### Status

- **Done:**
  - **Decision 1** as given.
  - **Decision 2:** narrowed to header lines on a counterexample, plus the line-start next-line skip that closes its second regression.
  - **Decisions 3 and 4** as given, plus the `led` part of 3 that its mutant found.
  - **Decision 5's** stated-and-pinned branch, with the review's scope corrected.
  - **The rest:** C-23.14 and its change-log entry, the corpus, the mutants, the cost rows and the holds.
- **Final holds, at `61a60ca02`** (after every test and docs commit; no code change since `70aafdcaf`):
  - hold 1 again (12:20:34Z, slot B, load 57): 164 passed, 2 skipped, the dash-led daemon.log test among them;
  - hold 4 again (12:27:25Z, slot A, load 61): 140 passed.
  - Holds 2 and 3 were not repeated: neither their files nor the code changed after `70aafdcaf`.
- **Not run:**
  - the rest of `tests/unit` and `tests/fake` beyond the brief's files (CI runs them);
  - `test_daemon_log_sites.py`'s real-daemon tests;
  - Python 3.12;
  - the cost rows under the lock;
  - review r5's own corpora (not kept; this round's stands in);
  - a sweep of the mutation driver (the Budget hold).
- **Follow-ups for the hub:** round 4's, with these.
  - **P3-4's join, stated not fixed.** Closing it in every mode would mean masking a quoted value's secret-named flag and its fold as one span across the line break, as the references do. That changes the fold's shape everywhere a quoted value ends in such a flag. *Round 6:* fixed with review r6's `p34f`, which changes what ends a quoted value only where the flag's name is a key whose separator is on the next line.
  - **The 169 texts where this head shows a value `c469f44b6` hid** (all shown by both references): round 4's fold fix and this round's narrowing, measured, not leaks.

## Fix round 6 (#239 review r6, 2026-10-26)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix6.md`. **Review:** that folder's `review-r6.md`, REQUEST_CHANGES at `1ad7da99c`: one P2 (P2-1, round 5's P3-4 and its class, a regression against the live release, with a measured fix, `p34f`), and three P3s. This round built the hub's six decisions as given; none proved wrong in a test. One choice was left open: the review gives the P3-4 generator's core size but not its shape (below). The work stayed in the job's worktree (job `20261026-095419-pr239-fix6`, from 17:05Z): no Git remote operations, no sub-agents, nothing pushed. All test values are dummy `SECRET7…` strings.

**Names in this section.**
- **`1ad7da99c`** is this branch's head before this round. **`6d7e4ada9`** is the code head: no later commit changes `subfleet/`.
- **The references** stay `473fe2941` and `c2e080e44`; the tests use the frozen `scrub_473fe2941` for both and check `3fbc4b7c5` where `_new_leaks` does. **Release** is the live `be0c2a311`. The probes ran the real trees from `git archive`.

### Findings: cause and fix

**P2-1: a quoted header value's flag whose name is a key past its line (round 5's P3-4, and its class).**
- **Cause, as review r6 traced it:** in `\nAuthorization:"--1_token` and `=r SECRET7c5` on the next line, the quoted pass took the flag and the fold's run as two spans, and the line-start header kept the flag's name as a key whose `=` is past the line. Both references join the key's value to the quoted value instead, so the header takes the rest of the joined line. The ordinary-flag form (`\nAuthorization:"--password` and `=r SECRET7c5`) showed the same word by the same route. Release `be0c2a311` hides all of them. Round 5's own trace, that the flag rule takes the flag with its run, was wrong (its P3-2); that line of round 5's section now carries the correction.
- **Fix (`6d7e4ada9`, review r6's `p34f` as given):** in `_scrub_named_values`'s unclosed-quote branch, after `owned_before(...)`, a flag that ends the line ends nothing when `_key_past_line` finds a key there, at or after the quoted value's start. The union then takes the flag with that key's value, as both references do and as the head already did for any other such key (`\nAuthorization:"x password` and `=r SECRET7c5` was hidden at `1ad7da99c`). `_key_past_line` is read once per line end, in a `keyed` memo beside `owned`.
- **The nine inputs:** round 5's four and review r6's five siblings (three ordinary flags; two quoted headers on the line after a header whose value would start there). Each now gives both references' exact output in all five modes (`test_review_r6_s_p2_1_inputs_give_both_references_output_in_every_mode`). Both references show `SECRET7c5` in one place: `detail_line` of `Authorization:`, `'Cookie: --api-key` and `=r SECRET7c5`. This scrubber now shows it there too, and nowhere else.
- **Round 5's four P3-4 pins** now hold both references' output, and are no longer a documented limitation (`test_a_quoted_header_s_flag_folding_onto_an_assignment_hides_the_next_word_as_both_references_do`).
- **The class as a property (`p3_4_class`):** review r6's generator, PRE × HEADER × QUOTE × VAL × FLAG × GAP × RUN × REST × TAIL (8 × 10 × 4 × 3 × 6 × 5 × 11 × 6 × 4, 7,603,200 cells).
  - **The core:** the review gives its size, 6,720 cells, but not its shape. Here it is every lead, header, quote, value and flag at round 5's gap, run and rest (`\n`, `=r`, ` SECRET7c5`), 5,760 cells, plus every header, quote and flag after the `\n` lead with each other run that starts with `=` or `:` (`:r`, `=`, `:`, `=SECRET7r9`), 960 cells. It holds the seven inputs of the generator's shape.
  - **The draws:** 30,000 seeded draws over the whole product (seed 6239), each kept once and only if it holds a secret: 33,740 texts in all. The review's 33,808 differ only in the core's shape.
  - **By default** it runs a seeded 600-cell sample, with invariants 1 to 3 against both references (`_p3_4_failures`). At `1ad7da99c` 45 of the 600 show a value both references hide.
  - **With `SF_SCRUB_FULL_MATRIX=1`** it runs all 33,740. Run once, foreground, as a plain script in three worker processes (`_p3_4_failures` over 30 interleaved chunks, 134 s): **0 texts show a value both references hide, 0 splits, 0 reveals.** At `1ad7da99c` the same run found 2,678 (0 splits, 0 reveals).
- **What it shows that `1ad7da99c` hid (the decision's trade):** counted on this round's corpus (below), 389 texts: 365 of the P3-4 class and 24 of the 15,000 joins (the review measured 184 and 24 on its own). All 389 are in daemon.log's line modes only (386 in both, 3 in `detail_line` alone). Each value is one both references show there. In 366 of them this scrubber's output is both references' exact output in every mode; the other 23 are joins.
  - For example, `\napi_key:"--password` and `=r SECRET7c5` now scrub as both references scrub them in every mode (`api_key:[REDACTED] SECRET7c5`). At `1ad7da99c`, the excerpt left `"--password` visible (`\napi_key:"--password` and `=[REDACTED] SECRET7c5`), and daemon.log's folded line hid the rest of the joined line.
  - 337 of the 365 class texts are plain keys (`api_key:`, `password=`, `token: `); 28 are headers.
  - C-23.14 names them with the folded-line trade, per corpus (decision 5, below).

**P3-1: two cases the lone-flag sentence does not reach (stated, not closed).**
- Both references show the value in both, and so do `8fde0c4ad` and `c469f44b6`:
  - a quoted header on the next line: `Authorization:`, `'Cookie: --api-key` and `SECRET7c3` give `Authorization:`, `[REDACTED] --api-key` and `SECRET7c3`;
  - a next-line header that isn't at the value's start: `x Cookie:`, `x Cookie: --api-key` and `SECRET7c3` give `x Cookie:`, `[REDACTED] --api-key` and `SECRET7c3`.
- Each shows `SECRET7c3` in the excerpt, whole and `clean` modes. C-23.14's lone-flag sentence names both. Round 4's change-log entry no longer says "now true", nor that round 5 closes the rest; round 5's entry says the sentence holds but for these two.
- Pinned at today's output, mode by mode (`test_a_lone_flag_the_sentence_does_not_reach_shows_its_value_as_stated`, a documented limitation).
- The mechanisms are review r6's traces (the quoted pass reads `Authorization:` and `'Cookie:` as a key and its unclosed value, ended before the flag; the inline rule takes `x Cookie:` as the first header's value, up to the flag). This round did not trace them itself, so C-23.14 states the outputs only.

**P3-2: C-23.14's P3-4 sentence.** The class is closed, so the exception is removed rather than restated. The converse is again the one value a reference hides that this scrubber is known to show. A new sentence states the rule `p34f` adds, with an executed example. Round 5's section keeps its mechanism line with a dated correction.

**P3-3: both halves of `be6fb4330`'s skip are pinned.**
- **M4** (the skip's secret-prefix redaction out): `x`, `SECRET7ab1Cookie`, `:` and `Cookie: v SECRET7c3` on four lines then shows `SECRET7ab1` in the excerpt, whole and `clean` modes; the head hides it.
- **M7** (the `_HEADER_RE` half out): `Authorization:`, `Cookie`, `: --api-key` and `SECRET7c3` on four lines then shows `SECRET7c3` in the same three modes; the head hides it.
- Both references show each value, so only a pin of today's output sees either mutant (`test_the_next_line_skip_s_halves_are_pinned`). Each pin fails under its mutant and passes at the head, before and after the fix.
- **M6** (the `_header_name_at` half out) survives every test, round 6's included. It is probably equivalent, for review r6's reason: the quoted pass takes quoted names on the next line first. No input found tells it from the head.
  - *Correction (fix round 7, 2026-10-26):* M6 is not equivalent (review r7, P3-3). `_HEADER_RE` needs a value after the colon (`.+$`) and `_header_name_at` doesn't, so the halves differ where the next line is a name with nothing after its colon: `Cookie:` and `SECRET7ab1Cookie :` on two lines give `Cookie:` and `[REDACTED] :` at the head, and `Cookie:` and `[REDACTED]` under M6, in the excerpt, whole and `clean` modes. Neither shows a secret. Round 7 pins that output (`test_the_next_line_skip_s_halves_are_pinned[next-line-name-with-nothing-after-its-colon]`), which kills M6.

**Decision 5: the folded-line trade's scale, per corpus.** Round 5 called it "3 of 243,680", which is its own corpus only. Per corpus:
- **Round 5's corpus:** 3 of 243,680 texts, against `8fde0c4ad`.
- **Review r6's line-start generator:** 763 of 53,825 texts that `be6fb4330`'s skip makes show, against that head without the skip (`noskip`; 1,168 against `8fde0c4ad`, with round 5's P2-1 edits).
- **This round's corpus:** 389 of 48,758, against `1ad7da99c` (above).

All of them are in daemon.log's two line modes only, and each is a value both references show there. C-23.14's change log states this, and round 5's section carries the restatement.

### Invariants

Each holds for every input, with its stated exceptions and scope.
1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides.
   - **One stated exception:** round 2's converse (`invalid Bearer SECRET7_token: expired`, a value only `3fbc4b7c5` hid). P3-4's join is fixed.
   - **Scope:** every generator of rounds 1 to 6 (the holds below) and this round's corpus: 0 of 48,758 show a value both references hide, and 0 show a value `3fbc4b7c5` hides.
   - *Correction (fix round 7, 2026-10-26):* the invariant did not hold at this head beyond that scope. Review r7's P2-1 family showed a value both references and release hide in 5,095 of its 51,371 texts (and 2 of its 15,000 joins), so the converse was not yet the one stated exception. Round 7 closes the family; its section states the invariant's new scope.
2. **Monotone:** no second scrub reveals anything over the corpus (0 of 48,758).
3. **Excerpts agree with the whole** at every cut `_neutral_break` allows (0 of 48,758 split).
4. **Ownership:** round 3's twin matrices pass unchanged (hold 1b).
5. **Cost:** review r6's three rows stay within 1.3× of `c2e080e44` on the median, and the quadratic shape stays linear (Cost, below). The `keyed` memo is needed: without it the excerpt row reads 1.353× (min) in run 1.
6. **A lone flag survives whatever takes its header's line:** a fold, a refused `Bearer`'s line, or a header whose value would start on that line, but for the two cases C-23.14 now names (P3-1).

### The corpus

Plain scripts, three workers, outside the lock. The texts ran through the real `git archive` trees of `6d7e4ada9`, `1ad7da99c`, `473fe2941`, `c2e080e44`, release `be0c2a311` and `3fbc4b7c5`, in all five modes. The head's rows also check every allowed excerpt cut and a second scrub.

| Set | Texts | Head: shows what both references hide | `1ad7da99c` | Release | Head: splits / reveals | Head shows what `1ad7da99c` hid |
|---|---|---|---|---|---|---|
| The P3-4 class (`p3_4_class`, less the 7 regression inputs it holds) | 33,733 | 0 | 2,671 | 0 | 0 / 0 | 365 |
| Seeded joins, seed 6240 (two or three pieces of every generator of rounds 2 to 6, ten joints) | 15,000 | 0 | 180 | 0 | 0 / 0 | 24 |
| Round 5's and round 6's regression inputs, and the review's `x password` contrast | 25 | 0 | 9 | 0 | 0 / 0 | 0 |
| **Total** | **48,758** | **0** | **2,860** | **0** | **0 / 0** | **389** |

- The two reference trees give the same output in every mode on every text. The head shows nothing that `3fbc4b7c5` or release hides.
- **Against `1ad7da99c`:** the head hides more in 4,040 texts, and shows something `1ad7da99c` hid in the 389 above.
- **`1ad7da99c`'s 2,860, by the modes they show in:** 1,331 in the excerpt, whole, `clean` and `detail_line` modes; 637 in all five; 547 in the excerpt, whole and `clean` modes; 341 in the excerpt and `clean` modes; 4 in other sets.

### Mutations

Each change was made on its own in a copy of `6d7e4ada9`'s tree (M4 and M7 also on `1ad7da99c`'s, for the red runs), and `test_scrub_review_r6.py` (15 cases, 1 opt-in skip) ran against it as plain calls outside the lock. The head, as the control, passes all 15.

| Mutant | Killing tests |
|---|---|
| `p34f`'s check removed | the nine P2-1 inputs and the class sample (10); in `_r5`, the four P3-4 pins |
| The `keyed` memo removed (`_key_past_line` read per candidate) | no test (its output is the head's); the cost row: the excerpt row at 1.353× (min) in run 1 and 1.172× in run 2, against the head's 1.103× and 1.098× |
| M4: the skip's secret-prefix redaction out | `test_the_next_line_skip_s_halves_are_pinned[secret-prefix-goes]` (1) |
| M7: the skip's `_HEADER_RE` half out | `test_the_next_line_skip_s_halves_are_pinned[line-start-header-read-across-its-colon]` (1) |
| M6: the skip's `_header_name_at` half out (not in the brief) | none this round. *Correction (fix round 7):* not equivalent, and round 7's pin `next-line-name-with-nothing-after-its-colon` kills it (P3-3, above) |

### Cost

`cost.py` loads every tree in one process, and each sample asks every tree in turn, with the order alternating sample by sample. Each tree's `subfleet` modules are put back in `sys.modules` before its calls, because `refusals.scrubbed_line` imports `suggestions` when it is called. Times are CPU (`thread_time`); 11, 7 and 5 samples per row. The shapes are review r6's: `_adversary(712)` (261,666 characters); `_adversary(1440)` cut to 1 MiB; and six ordinary header lines (two at a line start, `curl -H`, `wget --header`, prose, `Set-Cookie`) repeated to 1 MiB. The 1 MiB shapes go through `refusals.detail_line`.

| Row | Head ÷ `c2e080e44`, run 1 (min / median) | Run 2 | `1ad7da99c`, run 1 / run 2 | Without the memo, run 1 / run 2 |
|---|---|---|---|---|
| Excerpt, 261,666 characters | 1.103 / 1.048 | 1.098 / 1.009 | 1.231 / 1.064; 0.946 / 1.029 | 1.353 / 1.206; 1.172 / 1.133 |
| 1 MiB `detail_line`, adversary | 0.877 / 0.992 | 0.899 / 0.969 | 1.123 / 1.063; 1.339 / 0.946 | 1.124 / 1.072; 1.316 / 0.974 |
| 1 MiB `detail_line`, headers | 1.205 / 1.172 | 1.420 / 1.159 | 1.066 / 1.122; 1.622 / 1.245 | 1.034 / 1.122; 1.297 / 1.155 |

- **Run 1** ran at load 64 to 98 (18:19Z), **run 2** at load 25 to 33 (19:21Z).
- **Run 2's header row reads 1.420 on the min.** `c2e080e44` had one fast sample there (876 ms against a median of 1,209 ms), and `1ad7da99c` reads 1.622 on the same samples. All three medians are within 1.3×. A third run of that row alone, 15 samples (19:23Z, load 24 to 28, the host on battery), read 1.222 / 1.182 for the head and 1.296 / 1.200 for `1ad7da99c`.
- **The quadratic shape** is round 4's 80,010-character shape at 20,010, 40,010, 80,010 and 160,010 characters (min, ms):
  - **Head:** 16.4, 30.6, 84.0 and 212.3 in run 1; 22.7, 53.3, 94.2 and 161.6 in run 2.
  - **`c2e080e44`:** 5.6, 12.4, 29.5 and 73.9; 7.8, 12.2, 26.7 and 52.1.
  - **`1ad7da99c`:** 15.4, 31.1, 70.7 and 187.6; 23.6, 51.6, 72.5 and 187.1.
  - Each tree grows about 7 to 13 times over an 8 times larger input, `c2e080e44` alike, and no doubling more than 2.8 times. The head stays a constant 2.5 to 4.4 times `c2e080e44` at every size, as `1ad7da99c` does (2.4 to 4.2). That is linear.

### Tests

**Red at `1ad7da99c`,** under the lock (17:22:27Z, slot A, load 130): `test_scrub_review_r6.py` as first committed (`44e3d08cf`) had 10 failed, 5 passed and 1 skipped.
- **Failed:** the nine P2-1 inputs, each showing `SECRET7c5` where both references hide it, and the class sample (45 of its 600 cells).
- **Passed:** the class's size check, and the four pins of today's output (P3-1's two and P3-3's two).
- **Decision 4's two pins under their mutants,** as plain calls on `1ad7da99c`'s tree: M4 fails `secret-prefix-goes` (it shows `SECRET7ab1` in the excerpt, whole and `clean` modes), and M7 fails `line-start-header-read-across-its-colon` (it shows `SECRET7c3` in the same three). Both pass at the head. M6 passes both.

**Holds, at `6d7e4ada9`** (the code head), under the lock. Each of review r6's four holds is split so no one pytest passes 10 minutes: by file, or with `-k` for hold 2. Hold 4's guards ran after the C-23.14 commit (`364e577b2`).

| Hold | Lock | Files and results |
|---|---|---|
| 1a, 17:59:05Z, 476 s | slot A, load 95 | `test_scrub_review_r4` 36, `_r5` 24 and 1 opt-in skip, `_r6` 15 and 1 opt-in skip |
| 1b, 18:09:56Z, 145 s | slot A, load 65 | `test_scrub_review_r3` 29 and 1 opt-in skip |
| 1c, 18:15:06Z, 127 s | slot A, load 18 | `test_scrub_review_r2` 75 |
| 2a, 18:33:30Z, 69 s | slot B, load 54 | `test_scrub_inline_headers`, all but the bound and cost tests: 162 |
| 2b, 18:35:02Z, 374 s | slot A, load 50 | `test_scrub_inline_headers`, the bound and cost tests: 111 |
| 3a, 18:43:27Z, 242 s | slot A, load 78 | `test_daemon_log_cut_windows` 177 |
| 3b, 18:48:28Z, 430 s | slot A, load 63 | `test_daemon_log_handler` 128 |
| 4a, 18:56:30Z, 272 s | slot A, load 97 | `test_scrub_no_new_leak` 4, `test_cpu_scaling` 3 |
| 4b, 19:05:56Z, 186 s | slot A, load 85 | `test_scrub_key_order` 70 |
| 4c, 19:17:38Z, 176 s | slot A, load 33 | `fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 |

In total, 897 passed, 3 were skipped (the opt-in full forms) and none failed.

### Status

- **Done:** the six decisions as given, the red runs, the full generator run, the corpus, the mutants, the cost runs (two, and a third of the header row) and every hold.
- **Not run:**
  - the opt-in full forms as tests (the P3-4 class's ran as a plain script, above; P2-1's 95,040-cell class and round 3's rules matrix were not rerun);
  - review r6's own corpora beyond the P3-4 generator (not kept; this round's stands in), and the line-start generator behind its 763;
  - the rest of `tests/unit` and `tests/fake` beyond the brief's files, `test_daemon_log_sites.py`'s real-daemon tests, Python 3.12 and CI;
  - the cost rows under the lock;
  - a sweep of the mutation driver (the Budget hold).
- **Follow-ups for the hub:** round 5's, less P3-4's join, with these.
  - **The lone-flag sentence's two named cases** (P3-1), stated and pinned, not closed; both references show them.
  - **M6** survives, probably equivalent (P3-3). *Correction (fix round 7):* it is not equivalent; round 7 pins the input that tells it from the head, and that pin kills it.

## Fix round 7 (#239 review r7, 2026-10-26)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix7.md`. **Review:** that folder's `review-r7.md`, REQUEST_CHANGES at `36590532c`. It found one P2 (P2-1, a family the PR shows and release hides, with a measured fix, `p34h`) and three P3s. The review's generators and scripts are in `review-r7-tools/`, and this round rebuilt every corpus with its `gen.py`. This round built the hub's five decisions as given. One proved impossible as worded, and the smallest change that keeps its intent is described below (the strata). The job ran in two attempts in its own worktree (job `20261026-192232-pr239-fix7`). Attempt a1, from 02:22Z, committed the red tests, the fix and the three P3s' tests. Attempt a2, from 04:00Z, ran the holds, the full generator, the corpora, the mutants and the cost rows, and wrote C-23.14 and this section. There were no Git remote operations and no sub-agents, and nothing was pushed. All test values are dummy `SECRET7…` strings.

**Names in this section.**
- **`407ff5010`** is this branch's head before this round: round 6's `36590532c` plus the hub's merge of `release/217` at `7b1251693`. Its scrubber is `36590532c`'s: the merge changes nothing in `handoff.py` or `suggestions.py` (it changes `refusals.py`).
- **`95420ecaf`** is the code head; no later commit changes `subfleet/`.
- **The references** stay `473fe2941` and `c2e080e44`, and the tests use the frozen `scrub_473fe2941` for both. **Release** is the live `be0c2a311`. The corpora ran each commit's own `subfleet/` tree.

### Findings: cause and fix

**P2-1: a credential flag after a header name that an unclosed quoted value swallows.**
- **The inputs,** both minimized by review r7:
  - (A) `Authorization:"Authorization:`, `--password` and `:r SECRET7c5` on three lines;
  - (B) `Set-Cookie:"Authorization`, `:--1_token` and `=r SECRET7c5`.
  - At `407ff5010` (A) gave `Authorization:[REDACTED]`, `--password` and `:[REDACTED] SECRET7c5`, and (B) gave `Set-Cookie:[REDACTED]` and `[REDACTED] SECRET7c5`, in the excerpt, whole and `clean` modes. Both references and release give `Authorization:[REDACTED]` and `Set-Cookie:[REDACTED]` there.
- **The cause, as review r7 traced it:** `p34f` added round 6's check at only one of the quoted pass's three item sites, the unclosed-quote branch. The other two sites still ended the value before the flag:
  - the swallowed header name's value (`owned_before`), in (A);
  - a key's plain value within a quoted match (`_owned_item` and `_secret_flag`), in (B).
  - In both, the flag's name is a key whose separator is on the next line. The references join the three lines, and the header takes the rest of the joined line.
- **Fix (`95420ecaf`, review r7's `p34h` as given):** after each of the two sites, the same `_key_past_line` check, run only when there is an item or fold to clear, and read through the `keyed` memo. A flag whose name is such a key, at or after the value's start, ends nothing there. The union then takes the flag with the key's value.
- **The pins:** (A), (B) and review r7's two seeded joins (seed 7241) give both references' output in every mode, with no split and no reveal (`test_review_r7_s_p2_1_inputs_give_both_references_output_in_every_mode`, `test_review_r7_s_two_joins_show_nothing_both_references_hide`).
  - (A) and (B) show `SECRET7c5` in `detail_line` only, as both references do.
  - In `detail_line` the joins hide more than the references: the quotes the references keep, and in the second join also `SECRET7c5`, which both references show there.
- **Both sites are needed** (Mutations, below): without site 1, (A) and both joins fail; without site 2, (B) does.
- **The family as a property (`family_class`):** review r7's generator, lead × quote × inner name × gap × separator × flag × gap × run × rest × tail (11 × 4 × 10 × 4 × 4 × 7 × 3 × 6 × 5 × 4, 2,217,600 cells). Its 24,640-cell core and 30,000 draws at seed 7243 give 51,371 texts, the review's `fam2` corpus text for text.
  - **By default** it runs a 600-cell sample: 69 strata and 531 seeded draws (`family_sample`). At `407ff5010`, 117 of the 600 show a value both references hide, all 69 strata among them.
  - **With `SF_SCRUB_FULL_MATRIX=1`** it runs all 51,371. This round ran it once, foreground, as a plain script in three worker processes (`_p3_4_failures` over three interleaved slices, 206 s). **0 texts show a value both references hide, with 0 splits and 0 reveals.** The corpus run below adds release: 0 there too.
- **The strata (the decision that could not be built as worded).** The brief asked that every part value contribute at least one cell that leaks at `36590532c`. In the whole family, 12 of the 58 part values have no leaking cell (`FAMILY_NEVER_SHOWN`: eight leads, the empty quote, `x --password`, the run `r` and the empty rest). So the strata are a greedy cover of every part value that does leak (46), each mode set (2) and each pair of two parts' values (916) that the family's 5,095 leaking cells hold. That is every shape the whole class fails for. The test checks that the strata hold every part value but those 12.
- **Every round's head shows the family,** by review r7's runs (`8ed254385` to `1ad7da99c`); this round ran only `407ff5010` and `1ad7da99c`, and each shows it in 5,095 texts.

**P3-1: the P3-4 sample holds a cell of every shape that leaked.**
- `P3_4_STRATA` is now a greedy cover, built the same way, of every part value, mode set and pair of values that the class's 2,678 cells failing at `1ad7da99c` hold: 48 strata, then 552 seeded draws. 17 part values have no failing cell in the whole class (`P3_4_NEVER_SHOWN`), for the reason given under P2-1.
- **Review r7's two missed cells are pinned** in every mode against both references (`test_review_r7_s_cells_the_sample_missed_give_both_references_output_in_every_mode`): `cookie:\"--api-key` and `  = x SECRET7c5"` (RUN `=`), and `cookie:"x --1_token`, an empty line and `:r SECRET7c5'` (REST `" SECRET7c5'"`). Each passes at the head. At `1ad7da99c` each shows `SECRET7c5` in the excerpt, whole and `clean` modes, where both references hide it.
- **Coverage, as review r7 measured it** (its `coverage.py`, against the shapes its own 93,521-text P3-4 corpus leaks at `1ad7da99c`):

  | | Mode sets | Part values | Pairs of values | Cells failing at `1ad7da99c` |
  |---|---|---|---|---|
  | The whole class (33,740) | 4 / 4 | 40 / 40 | 699 / 699 | 2,678 |
  | Round 6's sample (600) | 4 / 4 | 38 / 40 | 468 / 699 | 45 |
  | This round's sample (600) | 4 / 4 | **40 / 40** | **699 / 699** | **104** (all 48 strata among them) |

- **The family's sample, the same way** (against the whole family's leaking cells at `407ff5010`): 2 / 2 mode sets, 46 / 46 part values and 916 / 916 pairs.

**P3-2: the `keyed` memo is tested.**
- **The probe:** review r7's input, `_adversary(300)` with its last word replaced by ` --` + 40,000 × `a` + `_password` and `=x SECRET7c5` on the next line (88,474 characters). Its 300 unclosed values share one line end. Both references also show `SECRET7c5` in every mode here, so no mode shows a new leak.
- **The count test** (`test_unclosed_values_sharing_a_line_end_read_the_key_past_it_once`), in the excerpt, whole and `detail_line` modes: `handoff._key_past_line`, monkeypatched with a counter, is called once per line end. The head makes 1 call in each mode. With the memo removed at all three sites, 300.
- **The linear pairs** (`test_the_memo_s_probe_scrubs_in_linear_time`): twice the values (to 226,924 characters) and twice the identifier (to 128,474), each through `_assert_linear`. The identifier walk also stays under the text's length in every mode.
  - **The head** walks the identifier once: 40,012 `_identifier_char` calls, and 80,012 at twice the identifier, at 16 to 39 ms.
  - **Without the memo** it walks it once per value: 12,003,600 calls on 88,474 characters (2.2 to 2.3 s), 24,007,200 at twice the values and 24,003,600 at twice the identifier.
  - Each pair grows one factor of that product, so `_assert_linear`'s 4× bound passes it. Only the walk bound sees the quadratic.
- **The count holds with `p34h`'s two sites in place:** they read through the same memo, and the memo-off mutant removes it at all three.
- *Note (fix round 8, 2026-10-27, from review r8's check 2):* the count test reaches only `p34f`'s site, the unclosed values. With the memo removed at `p34h`'s two sites alone, every test passes and the output is unchanged: review r8 found no input where the memo matters there (at most two lookups shared a line end over 215,830 texts, and a `\r`-separated probe stays linear). C-23.14's memo sentence now says the test counts the unclosed values only.

**P3-3: M6 is not equivalent.**
- `_HEADER_RE` needs a value after the colon (`.+$`), and `_header_name_at` needs only the name and `[ \t]*:`. So the skip's two halves differ when the next line is a name with nothing after its colon.
- `Cookie:` and `SECRET7ab1Cookie :` on two lines give `Cookie:` and `[REDACTED] :` at the head, and `Cookie:` and `[REDACTED]` under M6, in the excerpt, whole and `clean` modes. Neither shows a secret.
- The input is pinned at today's output in the five modes (`test_the_next_line_skip_s_halves_are_pinned[next-line-name-with-nothing-after-its-colon]`), which kills M6. Round 6's three lines that called M6 probably equivalent now each carry a dated correction.

### C-23.14

- **The rule `p34h` adds,** with (A) and (B) as executed examples: a flag that ends the value of a header's name that a quoted match swallows, or a key's plain value within a quoted match, and whose name is a key whose separator is on the next line, ends nothing.
- **The converse sentence** ("the one value a reference hides that this scrubber is known to show") was false at `36590532c`. It is true at the head on every text this round ran, as measured below (corrected 2026-10-27: only there; review r8's generators found four more families, and round 8 replaces the sentence): the head shows nothing that 473fe2941, `c2e080e44` or release hides. Against `3fbc4b7c5` (run on the regression inputs, the joins and the family) it shows only the converse (41 texts, in each only the `SECRET7_token` of a key after `invalid Bearer` or `token_type: Bearer`). The sentence now names this section.
- **The change log** gains round 7's entry, and round 6's entry says its claim held only from this round.

### Invariants

Each holds for every input, with its stated exceptions and scope.
1. **No new leak:** in no mode does the head show a `SECRET7` value that 473fe2941 or `c2e080e44` hides.
   - **One stated exception:** round 2's converse (`invalid Bearer SECRET7_token: expired`, a value only `3fbc4b7c5` hid). Review r7's P2-1 family is closed.
   - **Scope:** every generator of rounds 1 to 7 (the holds below), the whole family (51,371 cells, run in full), and review r7's eight corpora (311,789 texts). 0 of them show a value either reference or release hides, and 0 show a value `3fbc4b7c5` hides on the 69,024 texts it ran (the regression inputs, the joins and the family) but the converse.
   - *Correction (fix round 8, 2026-10-27):* beyond that scope the invariant did not hold at this head. Review r8's generators found 598 of 146,793 new texts where the head showed a value release and both references hide (P2-1 to P2-4), so the converse was not the one exception. Round 8 makes the invariant hold by construction; its section gives the argument and the measured scope.
2. **Monotone:** no second scrub reveals anything over the corpora (0 of 311,789), nor over the whole family.
3. **Excerpts agree with the whole** at every cut `_neutral_break` allows (0 of 311,789 split).
4. **Ownership:** round 3's twin matrices pass unchanged (hold 2b).
5. **Cost:** review r7's three rows stay within 1.3× of `c2e080e44` on the median in three runs, and the quadratic shape stays linear (Cost, below). The `keyed` memo is needed, and a test now counts it (P3-2).
6. **A lone flag survives whatever takes its header's line,** but for the two cases C-23.14 names (round 6's P3-1); their pins pass unchanged.

### The corpora

These are review r7's eight sets, rebuilt with its `gen.py`. They ran as plain scripts (its `drive.py`, `worker.py` and `analyze.py`) in three worker processes, outside the lock, through every tree in all five modes. The head's rows also check every allowed excerpt cut and a second scrub.

| Set | Texts | Head: what both references hide | Head: what release hides | `1ad7da99c`: what both references hide | Head: splits / reveals | Head shows what `1ad7da99c` hid |
|---|---|---|---|---|---|---|
| Regression inputs | 2,653 | 0 | 0 | 9 | 0 / 0 | 0 |
| Builder's P3-4 class | 33,740 | 0 | 0 | 2,678 | 0 / 0 | 365 |
| Review r7's P3-4 generator | 93,521 | 0 | 0 | 9,865 | 0 / 0 | 1,252 |
| Adversarial texts aimed at `p34f` | 55,404 | 0 | 0 | 1,455 | 0 / 0 | 2,701 |
| Line-start | 56,275 | 0 | 0 | 0 | 0 / 0 | 0 |
| Refused `Bearer` | 3,825 | 0 | 0 | 0 | 0 / 0 | 0 |
| Seeded joins | 15,000 | 0 | 0 | 1,026 | 0 / 0 | 86 |
| P2-1's family | 51,371 | 0 | 0 | 5,095 | 0 / 0 | 0 |
| **Total** | **311,789** | **0** | **0** | **20,128** | **0 / 0** | **4,404** |

- **The references** give the same output on every text, and release gives `c2e080e44`'s on every text.
- **Against `407ff5010`** (round 6's scrubber), the head hides more in 6,508 texts: 6,506 of the family and the 2 joins. It shows nothing `407ff5010` hid. `407ff5010` shows the family's value in 5,095 texts, by mode: 4,704 in the excerpt, whole and `clean` modes, and 391 in the excerpt and `clean` modes. Those are review r7's counts.
- **What the head shows that `1ad7da99c` hid** is round 6's folded-line trade, unchanged by this round. It totals 4,404 texts, each a value that both references and release show.
  - 4,318 are in both line modes and 86 are joins. Of the joins, 68 are in both line modes and 18 in `detail_line` alone.
  - Per set they are review r7's counts exactly (365, 1,252, 2,701 and 86).

### Mutations

Each mutant is one edit in a copy of `95420ecaf`'s tree. The named test modules ran against each as plain calls, outside the lock (the Budget hold: the brief's named mutants only). The head, the control, passes every case.

| Mutant | Cases run | Killing tests |
|---|---|---|
| `p34h`'s site 1 removed (the swallowed header name's value) | `_r7` (11) | 4: (A), both joins, the family sample |
| `p34h`'s site 2 removed (a key's plain value within a quoted match) | `_r7` (11) | 2: (B), the family sample |
| `p34f`'s check removed (round 6's) | `_r7`, `_r6`, `_r5` (54) | 17: both joins; 8 of r6's 9 inputs, the P3-4 sample and its two missed cells; `_r5`'s four P3-4 pins |
| The `keyed` memo removed, at all three sites | `_r7` (11) | 5: the three count tests and the two linear pairs |
| M4: the skip's secret-prefix redaction out | `_r6` (19) | 1: `secret-prefix-goes` |
| M6: the skip's `_header_name_at` half out | `_r6` (19) | 1: `next-line-name-with-nothing-after-its-colon` |
| M7: the skip's `_HEADER_RE` half out | `_r6` (19) | 1: `line-start-header-read-across-its-colon` |

With `p34f` removed, r6's ninth input (`Authorization:`, `'Cookie: --api-key` and `=r SECRET7c5`) still passes, so one of `p34h`'s sites now hides it too. This round did not trace which.
- *Traced (fix round 8, 2026-10-27, review r8's check 4):* it is site 1 (`handoff.py:529-533`, the swallowed header name's value), which clears the flag for the swallowed name `Cookie` when `p34f` (`:502-505`) is removed. At the head `p34f` handles the input first, so site 1 is never reached there: deliberate redundancy. With both removed the head's own scrub shows `SECRET7c5` (`Authorization:`, `[REDACTED] api-key` and `=[REDACTED] SECRET7c5`), and 12 cases fail, this input among them. Round 8 rechecked it against the composed scrub: release/217's output hides the value, so every mode hides it with either or both removed, a third layer.

### Cost

These are review r7's three rows with its `cost.py`: head ÷ `c2e080e44`, min / median, CPU `thread_time`. The three trees run in persistent processes, interleaved with the order alternating, 9 samples a row (4 for the quadratic shape). `36590532c` ran alongside as the old head. The runs were outside the lock: run 1 at 05:22Z (load 30 to 34), run 2 at 05:24Z (load 22 to 30) and run 3 at 05:25Z (load 31 to 33).

| Row | Run 1 | Run 2 | Run 3 | `36590532c`, runs 1 / 2 / 3 |
|---|---|---|---|---|
| Excerpt, 261,666 characters | 1.380 / 1.122 | 1.080 / 1.067 | 1.071 / 1.056 | 1.352 / 0.923; 0.862 / 1.017; 1.121 / 1.058 |
| 1 MiB `detail_line`, adversary | 0.808 / 0.940 | 0.922 / 1.001 | 1.042 / 1.017 | 1.084 / 0.947; 0.795 / 0.932; 1.026 / 1.054 |
| 1 MiB `detail_line`, headers | 1.220 / 1.207 | 1.249 / 1.224 | 1.306 / 1.257 | 1.165 / 1.216; 1.109 / 1.163; 1.244 / 1.234 |

- **Every median is within 1.3×;** the worst is 1.257 (headers, run 3). Two minimums are above it:
  - excerpt, run 1: 1.380 (`36590532c` read 1.352 on the same samples);
  - headers, run 3: 1.306 (`36590532c` read 1.244).
- **The excerpt row,** going by the code (not measured), reaches neither new site: its values are all quoted (review r7's reading).
- **The quadratic shape** (round 4's shape at 20,010, 40,010, 80,010 and 160,010 characters; head, min, ms):
  - **Head:** 16.2, 33.9, 73.1 and 160.4 in run 1; 22.0, 59.1, 123.2 and 204.9 in run 2; 15.4, 38.8, 45.6 and 161.5 in run 3.
  - **`c2e080e44`:** 6.6, 16.6, 27.4 and 59.7; 10.9, 22.6, 39.1 and 80.6; 7.0, 9.4, 29.9 and 47.4.
  - The head grows 9.3 to 10.5 times over an 8 times larger input, and `c2e080e44` 6.8 to 9.0 times. The head stays 1.5 to 4.1 times `c2e080e44` at every size, as `36590532c` does (1.8 to 3.3). That is linear.

### Tests

**Red, as plain calls** (review r7's `runcases.py`, the test modules against a tree's `subfleet`):
- **At `407ff5010`,** `test_scrub_review_r7`'s P2-1 cases: 5 of 5 failed. They are (A), (B), both joins, and the family sample (117 of its 600 cells, the 69 strata among them). Each input shows `SECRET7c5` where both references hide it: (A) and (B) in the excerpt, whole and `clean` modes, and the two joins in the excerpt and `clean` modes.
- **At `1ad7da99c`** (read-only), `test_scrub_review_r6`'s two missed cells and the P3-4 sample: 3 of 3 failed. The sample's 104 failing cells include all 48 strata.
- **The count test and the linear pairs:** 5 of 5 pass at the head and fail with the memo removed (300 calls; 12,003,600 walk steps).
- **M6's pin** passes at the head and fails under M6 (`[REDACTED] :` against `[REDACTED]`).
- At the head all of these pass.

**Holds, at `95420ecaf`'s code,** under the lock. These are review r7's eight holds plus the new `_r7`. Hold 2 is split with `-k` into its two files, and hold 4 into four parts, so no pytest passes 10 minutes. Hold 8's guards ran after the C-23.14 commit (`c68fb1f08`). Each hold ran under `suite_hold.sh`'s two-slot lock, entered by a bounded-wait wrapper (`hold.py`, the same `lockf -s -k -t 0` try) so that no wait outlived the tool's window. A wait that was not admitted was retried.

| Hold | Lock, load | Duration (pytest) | Files and results |
|---|---|---|---|
| 1, 04:05:09Z | slot B, 32 | 84 s | `_r6` 19 and 1 opt-in skip; `_r5` 24 and 1 opt-in skip |
| new, 04:07:21Z | slot A, 40 | 24 s | `_r7` 11 and 1 opt-in skip |
| 2a, 04:08:17Z | slot A, 38 | 117 s | `_r4` 36 (`-k test_scrub_review_r4`) |
| 2b, 04:11:06Z | slot A, 44 | 165 s | `_r3` 29 and 1 opt-in skip (`-k "not test_scrub_review_r4"`) |
| 3, 04:14:40Z | slot A, 63 | 68 s | `_r2` 75 |
| 4a, 04:35:00Z | slot B, 38 | 19 s | `test_scrub_inline_headers`, `-k a_swallowed_flag_or_key_still_owns`: 100 |
| 4b, 04:36:22Z | slot A, 37 | 39 s | `test_scrub_inline_headers`, `-k an_unclosed_quote_does_not_take`: 72 |
| 4c, 04:49:00Z | slot B, 36 | 13 s | `test_scrub_inline_headers`, the rest but the bound and cost tests: 73 |
| 4d, 05:09:36Z | slot A, 43 | 65 s | `test_scrub_inline_headers`, the bound and cost tests (`linear`, `costs`, `responsive`, `real_bound`): 28 |
| 5, 05:12:01Z | slot B, 39 | 83 s | `test_daemon_log_cut_windows` 177 |
| 6, 05:15:03Z | slot A, 48 | 51 s | `test_daemon_log_handler` 128 |
| 7, 05:20:23Z | slot B, 25 | 115 s | `test_scrub_no_new_leak` 4, `test_cpu_scaling` 3 |
| 8, 05:34:01Z | slot A, 31 | 37 s | `test_scrub_key_order` 70, `fake/test_state_contract` 37, `test_test_module_names` 1, `test_invariants_index` 25 |

In total, 912 passed, 4 were skipped (the opt-in full forms) and none failed.

### Status

- **Done:** the five decisions, with the strata as described above, the red runs, the full family run, review r7's eight corpora, the seven named mutants, three cost runs and every hold.
- **Not run:**
  - the other opt-in full forms as tests: the P3-4 class, P2-1's 95,040-cell class and round 3's rules matrix. The P3-4 class ran in full as corpus texts.
  - `3fbc4b7c5` beyond the regression inputs, the joins and the family;
  - the rest of `tests/unit` and `tests/fake` beyond the brief's files, Python 3.12 and CI;
  - the cost rows under the lock;
  - mutants beyond the seven named, and any mutant against `_r2` to `_r5` but `p34f`'s against `_r5`.
- **Follow-ups for the hub:** round 6's, less M6.
  - **The lone-flag sentence's two named cases** (round 6's P3-1), stated and pinned, not closed; both references show them.

## Fix round 8 (#239 review r8, 2026-10-27)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix8.md`. **Review:** that folder's `review-r8.md`, REQUEST_CHANGES at `be4f090b6`: four P2 families (598 of 146,793 new texts where the head showed a `SECRET7` value that release and both references hide), pinned red in `test_scrub_review_r8` (`94344839f`). Rounds 5 to 8 each closed the families their review found, and each next review's generators found more, so the hub's decision for this round was to make "the head shows nothing release hides" hold by construction, keeping the head's gains, rather than fix a ninth set of families. This round built that, with one change to decision 1's mechanism, described below with its counterexample. It ran in its own worktree (job `20261027-044159-pr239-fix8`) from 12:06Z. There were no Git remote operations and no sub-agents, nothing was pushed, and every test value is a dummy `SECRET7…` string.

**Names in this section.**
- **`b0fd3ce8c`** is this branch's head before this round: round 7's `be4f090b6`, review r8's red test `94344839f`, and the hub's merge of `release/217` at `2c981cba5`. Its scrubber is `be4f090b6`'s.
- **Release** is release/217 at `2c981cba5`. Its scrubbing half is byte for byte that of `be0c2a311`, which review r8 ran, and gives `c2e080e44`'s output on every text (review r8).
- **The head's pipeline** for a mode is that mode as `be4f090b6` ran it: this round changes none of the head's rules, only what each mode writes.

### Decision 1: the frozen copy and the composition

- **The frozen copy** (`subfleet/sessions/scrub_release.py`): `handoff.py`'s lines 59-152 and 236-1568 at `2c981cba5` (blob `f78955e05`: the constants and patterns, the rules, `scrub_secrets`, `scrub_bounded`, the cuts daemon.log uses, `truncate` and `clean`), then `redact.py`'s lines 121-160 (blob `bde628b71`: `mask_approval`). Only the imports differ. `test_scrub_release_frozen` pins the kept lines by their sha256 everywhere and, where the clone holds the two blobs (a full clone; CI's checkout is one commit), reads them from the blobs too. The test reads the module as imported, so a run against another tree reads that tree's copy.
- **As worded, decision 1 failed decision 2.** The brief's composition, `head_mode(release_mode(x))`, holds decision 1 by construction, and was built and measured first (`c0`, every mode composed so, the head's pass run with no reminder stripped):
  - 0 of the 461,248 texts showed a value release's same mode hides;
  - but 91,978 lost a gain: the composed head showed a value `be4f090b6` hides. The texts the head hides that release shows fell from 130,712 to 62,146.
  - **The counterexample, by family:** release's rule takes an item (a credential flag, a key or `Bearer`) whose value lies past what it redacted, and the head's pass then sees only `[REDACTED]` where the item was, so no rule of the head can own the value. `token:--SECRET7abc1_token SECRET7nxt9` becomes `token:[REDACTED] SECRET7nxt9` in release; `be4f090b6` gives `token:[REDACTED] [REDACTED]`. So do `Authorization: Bearer` with `client_secret=x SECRET7abc13 tail` on the next line, and `sent Cookie: --password` with `SECRET7a1 x` on the next.
  - One family could be fixed in the head's rules: release's key rule reads an inline header's name as a key and redacts only its first word (`curl -H "Authorization: [REDACTED] SECRET7abc" x`), and the head's inline rule then left a value that starts redacted. A fix (skip only a value of placeholders alone) closed it. The item families cannot be fixed that way: what owned the value is gone from the text the head's pass reads.
- **The smallest change that keeps both decisions' intent** (built): the head's pipeline reads the text, beside release's, and each mode writes release's output but for each whitespace-free run of it that the head's output of the same mode, of the same text, does not hold, which becomes `[REDACTED]` (`handoff.shown_by_both`). Release's whole pipeline for the mode still runs first and is what is written; the head's decides only what more of it to hide. This is the rule `daemon_log._kept_block` already applies to a block and its folded rescrub.
  - **The modes:** `scrub_secrets` (the excerpt and whole scrubs, `redact.scrub`, `ids`, `diff`), `scrub_bounded` and `clean` (`redact.bounded`, the handoff and Codex briefs), `suggestions._scrub_line` (daemon.log's folded line, `detail_line`), `suggestions.scrubbed_line` and a suggestion's title and summary (release's line cut first, as release cuts it, the mark set aside, then masked and cut again; corrected 2026-10-28, round 10: the cut's part-word became `[REDACTED]`, so release's line is now masked first, then cut once, review r9b P2-2, see Fix round 10), `normalize`'s brief (its line-end normalisation between two passes), and daemon.log's block form (`scrubbed_block`, plain, kept-lines and group). `mask_approval` masks what release's masking leaves (`_head_mask_approval` on release's output), which keeps the mask format.
  - **Each mode's pipeline is its whole one**, fold and second scrub included, so the composition never sits between a scrub and its fold (review r8, P2-1, is that case: composing each scrub lets the head's first pass take what release's second pass reads).
  - **The bounded mode** is composed piece by piece. `_released_pieces` runs release's `scrub_bounded` step by step with release's own helpers (block resolution, excerpt cuts, scrub, trim, marker, `truncate`'s pieces) and returns the pieces, which join to release's output exactly (a Hypothesis test). Each scrubbed piece is masked against the head's `_scrub` of the same text release scrubbed (the block-resolved text, or the same excerpt of it; the head's block resolution is release's). Markers and the elision are kept as release wrote them. Where the placeholders make the result longer than `limit`, `truncate` bounds it again. (Corrected 2026-10-28, round 9: within the budget, `truncate`'s pieces broke C-23.36, since a cut through a run leaves a piece the head's scrub holds only whole; such a text is now one piece, and `truncate` cuts the composed scrub. See Fix round 9.) (Corrected 2026-10-28, round 10: past the budget the same held for release's cut excerpts, and the second `truncate` cut through release's marker, review r9b P2-1. Each excerpt is now masked whole, then cut as release cuts its own, with release's marker, so the result fits `limit` and nothing cuts it again. See Fix round 10.)
  - **The count** is release's or the head's, whichever replaced more.
  - **daemon.log's tail** (`_taken_out`) takes reminders out only where release's first pass and the head's agree on them, since both scrubs now read the text.
- **What it costs in prose:** a run either pipeline hides goes whole, so a run that holds a secret and punctuation loses the punctuation (`curl -H 'Authorization: Basic SECRET7…' https://x` is written `curl -H 'Authorization: [REDACTED] [REDACTED] https://x`; the head's own output kept the closing quote), and the head's narrower redactions (`Cookie: [REDACTED] token=[REDACTED]`) show only where release's output shows the same runs. The pins in the test modules are updated to what the modes write; every rewritten pin shows no value the old pin hid (the rewrite tool refused any that would).

### The argument, mode by mode

For every mode the written output is `shown_by_both(r, h)`, where `r` is release's output of the mode (release's pieces, for the bounded mode; release's cut line, for `scrubbed_line`; since round 10, release's uncut excerpts and line, which the mode cuts after) and `h` the head's output of the same mode from the same text. `shown_by_both` is `re.sub(r"\S+", keep, r)`: each maximal whitespace-free run of `r` is written unchanged when `h` holds it as a run, and as the ten characters `[REDACTED]` otherwise; the whitespace between runs is `r`'s.
1. **Nothing release hides comes back.** Every character written is a character of `r` in its place and order, or one of a `[REDACTED]` written for a whole run of `r`. A `SECRET7` value has no whitespace, so where one shows it lies inside a run that was kept whole: a run of `r`. So it shows only where release's mode shows it.
2. **Nothing the head hides shows.** A run is kept only if `h` holds it, so a value that shows lies inside a run of `h`.
3. **The bounded mode** applies the same to each scrubbed piece; release's markers and elision are its own words, and a second `truncate` only cuts. (Round 9: within the budget it is now the whole-text mode, cut by `truncate`, whose cut can fall elsewhere than release's; see Fix round 9.) (Corrected 2026-10-28, round 10: a second `truncate` did not only cut; on the excerpt path it cut through release's marker, and wrote placeholder fragments, review r9b P2-1. Every piece is now masked before it is cut, and only once; see Fix round 10.) **`scrubbed_line`** cuts release's line first, sets the mark aside, masks, puts the mark back and cuts again; cuts only remove. (Round 10: it now masks release's line, then cuts it once, `bounded(shown_by_both(released, head), limit, mark)`; see Fix round 10.) **daemon.log's block form** masks release's block, its lines, margins and folding kept, with the head's block.
4. **`mask_approval`:** the head's pass on release's output replaces a whole match with its mask or leaves it, and writes nothing of its own but the mask's words, so no character release masks comes back; and it reads every token shape release leaves as it would without release's pass.
- **Executed:** `test_scrub_release_first` checks each mode's output against this formula, checks over r8's nine inputs and a 300-string sample of every review's regression strings that no mode shows a value release's or the head's same mode hides, and checks by Hypothesis that every mode only replaces release's runs (`_revealed`), writes no `SECRET7` run release's output lacks, and shows no value the head's hides.
- **Not covered by construction:** daemon.log's record cut (`cut_whole`, before either form, on records over 16,384 characters) is the head's, and both forms read what it keeps; the cut-window tests measure it, as before.

### Review r8's findings

Each of the nine (`test_scrub_review_r8`) shows nothing both references hide, in every mode; every mode now writes release's output there, and the head's runs hide more of it. By mode, the head shows the value only where both references do:

| Input | Shown (both references) | Shown now |
|---|---|---|
| P2-1 `secret:'cookie:`, `x --privatekey`, `:=,SECRET7c5` | excerpt, whole, `clean` | none |
| P2-1 `token:"Authorization:`, `x --token`, `==,SECRET7c5` | excerpt, whole, `clean` | none |
| P2-2 `Authorization:"Authorization`, `:Bearer =r SECRET7c5` | `detail_line` | `detail_line` |
| P2-2 `…`, `:Bearer SECRET7` | `detail_line` | `detail_line` |
| P2-2 `…`, `:--password \"=r SECRET7c5` | `detail_line` | `detail_line` |
| P2-3 `Authorization:x --token`, `:=;SECRET7c5` | excerpt, whole, `clean` | excerpt, whole, `clean` |
| P2-3 `cookie:x --token`, `=''SECRET7c5` | excerpt, whole, `clean` | excerpt, whole, `clean` |
| P2-4 `Authorization:""Cookie:apikey`, `=r SECRET7c5` | `detail_line` | `detail_line` |
| P2-4 `Cookie:'secret`, `=Bearer Seytoker SECRET7c5` | `detail_line` | `detail_line` |

- **P2-4, which review r8 could not trace, is closed by construction:** release's pipeline hides the value in four modes and the head's runs are only removed from it. Its two pinned inputs, mode by mode: `Authorization:""Cookie:apikey` and `=r SECRET7c5` give `Authorization:[REDACTED]` in the excerpt, whole, `clean` and daemon.log line modes, and `… [REDACTED] SECRET7c5` in `detail_line`; `Cookie:'secret` and `=Bearer Seytoker SECRET7c5` give `Cookie:[REDACTED]` in those four and `… Cookie:[REDACTED] [REDACTED] SECRET7c5` in `detail_line`. Review r8's third P2-4 input ("its multi-line form") is not pinned and its text is not in the review, so this round did not run it by name; its family is in the new joins, where nothing shows that release hides (below).
- **`FIX_R8c.patch` is not applied** (decision 6's fallback is not needed).

### Decision 2: the head's gains

- **Gains lost** (texts where `be4f090b6` hides a value the head now shows): **0 by construction**, and 0 measured over all 461,248 texts (below).
- **The PR's improvement** (texts the head hides that release shows): 130,712 texts at `be4f090b6`, 130,859 now (+147: where release's own pipeline hides a value the head showed, which the composition now hides too).
- **No fix to the head's rules was needed;** the inline-rule fix above was dropped with the composition it was for. The head's rules are `be4f090b6`'s.

### Invariants

Each holds for every input, by construction or as measured, with its scope.
1. **No new leak, by construction:** in no mode does the head show a character release's same mode hides, nor a `SECRET7` value the head's own pipeline hides (the argument above). (Round 9: a bound within the budget now cuts where `truncate` cuts the written text, so it can keep a character release's own cut leaves out, never one either scrub takes out; see Fix round 9.) **Measured:** 0 of the 461,248 corpus texts show a value release hides, and 0 show one `be4f090b6` hides; `be4f090b6` showed 598 that release hides, review r8's count exactly, here through the frozen copy run in the same process (a differential of the copy over all 461,248).
2. **Monotone:** no second scrub reveals anything (0 of 461,248, in the excerpt and whole modes and daemon.log's line, uncut and cut). **Not idempotent:** a second scrub changes 18,974 texts, by joining placeholders the first wrote: release's header rule reads `Cookie: [REDACTED] [REDACTED]` as one value and writes `Cookie: [REDACTED]` (excerpt 18,784, whole 17,892, `clean` 18,784, line modes 31). C-23.14 already says scrubbing is monotone, not idempotent; `be4f090b6`'s count was not measured.
3. **Excerpts agree with the whole:** at every break both scrubbers allow a cut, no excerpt shows a value the whole-text scrub hides (0 of 461,248). 2,389 texts differ there, each by an excerpt hiding a run the whole keeps: a run is kept only if the head's output of its own excerpt holds it.
4. **The leak-free classes stay so:** every text where `be4f090b6` showed no `SECRET7` value in any mode still shows none (0 of 461,248 show one).
5. **Cost:** within 2.5 times release's on every row's median in five runs, and linear (Cost, below).

### The corpora

These are review r8's corpora table, all 461,248 texts: review r7's eight sets rebuilt with `gen.py` from `36590532c`'s tests, and review r8's four with `gen8.py` from `be4f090b6`'s, by their seeds (each count is the review's). They ran as plain scripts outside the lock, four worker processes (`mworker.py`, `drive8.py` and `an8.py` in this round's scratch): `be4f090b6`'s tree in its own processes, the composed head with release's five modes through the frozen copy in the same process. Splits were measured separately, at every break both scrubbers allow (`--splits-only`, at the code head).

| Set | Texts | `be4f090b6`: shows what release hides | Now: shows what release hides | Gains lost | Head hides, release shows (`be4f090b6` → now) | Splits / excerpt shows what the whole hides | Reveals | Not idempotent |
|---|---|---|---|---|---|---|---|---|
| r7: regression inputs | 2,653 | 0 | 0 | 0 | 1,135 → 1,135 | 0 / 0 | 0 | 44 |
| r7: builder's P3-4 class | 33,740 | 0 | 0 | 0 | 4,235 → 4,235 | 32 / 0 | 0 | 753 |
| r7: reviewer's P3-4 generator | 93,521 | 0 | 0 | 0 | 12,679 → 12,679 | 80 / 0 | 0 | 1,676 |
| r7: adversarial texts aimed at `p34f` | 55,404 | 0 | 0 | 0 | 14,711 → 14,711 | 0 / 0 | 0 | 4,041 |
| r7: line-start | 56,275 | 0 | 0 | 0 | 32,512 → 32,512 | 18 / 0 | 0 | 1,792 |
| r7: refused `Bearer` | 3,825 | 0 | 0 | 0 | 2,210 → 2,210 | 0 / 0 | 0 | 0 |
| r7: seeded joins | 15,000 | 0 | 0 | 0 | 9,999 → 10,000 | 773 / 0 | 0 | 665 |
| r7: P2-1's family | 51,371 | 0 | 0 | 0 | 3,840 → 3,840 | 63 / 0 | 0 | 3,594 |
| r8: regression strings (r7's module) | 2,666 | 0 | 0 | 0 | 1,137 → 1,137 | 0 / 0 | 0 | 44 |
| r8: site 1 | 68,464 | 311 | 0 | 0 | 25,615 → 25,710 | 370 / 0 | 0 | 4,569 |
| r8: site 2 | 63,329 | 197 | 0 | 0 | 15,211 → 15,238 | 4 / 0 | 0 | 44 |
| r8: new joins | 15,000 | 90 | 0 | 0 | 7,428 → 7,452 | 1,049 / 0 | 0 | 1,752 |
| **Total** | **461,248** | **598** | **0** | **0** | **130,712 → 130,859** | **2,389 / 0** | **0** | **18,974** |

- **The first column** is review r8's 598 exactly (311, 197 and 90), here against release through the frozen copy: the copy gives release's tree's output on every text the review counted.
- **The head as the brief worded it** (`c0`, the head's pipeline on release's output) on the same 461,248 texts: 0 showed what release hides, 91,978 lost a gain, and the head hid what release shows in only 62,146.
- **Splits at release's own breaks** (before the excerpts were cut where both allow): 57,230 texts, and in a 3,546-cut sample 1,307 excerpts showed a value the whole hid, all past a break the head refuses (`6bda19b07` fixes it).

### Cost

Review r8's rows with its `cost.py`: the head ÷ `c2e080e44` (min / median, CPU `thread_time`, 9 samples a row, the three trees in persistent processes interleaved with the order alternating), and `be4f090b6` alongside. Runs 1 to 3 ran the composition's first snapshot and runs 4 and 5 the code head; the two differ only in a long text's excerpt cuts, which no row reaches (the excerpt row's text is within the work bound, and the `detail_line` rows scrub whole). All ran outside the lock at load 47 to 143: run 1 from 14:20Z, runs 2 and 3 from 14:57Z together, runs 4 and 5 from 15:26Z together.

| Row | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | `be4f090b6`, medians |
|---|---|---|---|---|---|---|
| Excerpt, 261,666 characters | 2.138 / 1.980 | 2.085 / 2.050 | 2.098 / 1.913 | 2.035 / 2.076 | 2.150 / 2.082 | 1.020 to 1.126 |
| 1 MiB `detail_line`, adversary | 1.843 / 2.050 | 2.061 / 2.042 | 2.018 / 2.034 | 2.054 / 2.029 | 2.037 / 2.049 | 1.008 to 1.041 |
| 1 MiB `detail_line`, headers | 2.396 / 2.195 | 2.245 / 2.395 | 2.274 / 2.292 | 2.248 / 2.353 | 2.238 / 2.350 | 1.162 to 1.285 |

- **Every median is within 2.5×** in all five runs; the worst is 2.395 (headers, run 2). No minimum passes 2.5 either (the worst, 2.396).
- **Why about 2×:** each mode runs release's whole pipeline and the head's whole pipeline over the same text, and `be4f090b6` alone reads about 1.0 to 1.3× release; the run mask adds a split and a set lookup per run.
- **The quadratic shape** (round 4's, 20,010 to 160,010 characters; head, min, ms): 22.3, 66.8, 136.6 and 273.5 in run 1; 21.8, 44.5, 86.3, 179.9 in run 2; 21.6, 46.9, 86.0, 178.2 in run 3; 28.2, 49.6, 127.9, 170.8 in run 4; 22.3, 59.5, 88.5, 174.1 in run 5. That grows 6.1 to 8.3 times over an 8 times larger input in runs 2 to 5 (12.3 in run 1, where `c2e080e44` grew 12.2): linear.
- **The tests' bound:** `tests/cpu_scaling.COST_RATIO_MAX` is 2.5, and the three guards that compare a whole scrub with a reference use it. The 1 MiB refusal is compared with release's own line through the frozen copy, no longer with the scrubber before #231, since every mode now runs release's #231 lookahead. The responsiveness test keeps the 50 ms gap for the head's own pass, and bounds the whole scrub's longest gap by 2.5 times release's own: release's lookahead is one regex call of about 80 to 100 ms at the bound (release 83 to 103 ms a scrub, the head's pass 15 to 36 ms, the pre-#231 scrubber 12 to 34 ms, measured this round). `test_cpu_scaling` pins that the bound reads twice the reference's work under it and four times over it.

### Mutations

Each mutant is one edit in a copy of the code head's `subfleet/` (`mut9.py`). The named modules ran against each as plain calls (`runcases.py`), outside the lock. The head, the control, passes every case.

| Mutant | Cases | Killed by |
|---|---|---|
| Release's pass removed: excerpt and whole (`scrub_secrets` writes the head's output) | 35 | 12: the formula and no-leak tests for both modes, the Hypothesis property, 5 of r8's nine |
| … the bounded mode (`scrub_bounded` writes the head's) | 35 | 9: `clean`'s formula and no-leak tests, the pieces property, the Hypothesis property, 5 of r8's nine |
| … daemon.log's line (`_scrub_line` the head's) | 35 | 9: both line modes' formula and no-leak tests, the Hypothesis property, 4 of r8's nine (P2-1, P2-3) |
| … `scrubbed_line`'s cut (the head's line cut) | 35 | 3: its formula and no-leak tests, the Hypothesis property |
| … a suggestion's brief (`normalize` the head's) | 25 | 3: the brief's formula and no-leak tests, the Hypothesis property (the brief's mode first called `_released_first` directly, which missed this mutant; it now calls `normalize`) |
| … daemon.log's block form | 35 | 4: both block forms' formula and no-leak tests |
| … `mask_approval` (the head's masking alone) | 25 | 1: `test_an_approval_keeps_the_mask_release_writes`, added for it (`Bearer SECRET7--secret`: release masks 22 characters, the head 14). Over 22,673 texts the two differed in 82 |
| The head's pass first (release's scrub on the head's output) | 35 | 12: as release's pass removed |
| The composition at the base function (daemon.log's line folds between two composed scrubs) | 35 | 6: both line modes' formula and no-leak tests, r8's two P2-3 inputs |
| The frozen copy edited by one rule (release's URL-password rule left out) | 35 | 1: the frozen copy's digest |
| r8's survivor: the memo removed at `p34h`'s two sites | 46 | survives, as in review r8: the same output on every input tried |
| r8's survivor: `p34h`'s guard removed | 46 | survives: equivalent by the code (clearing nothing changes nothing) |
| r8's survivor: `>=` for `>` at both sites | 46 | survives: review r8 found the same output on 183,164 texts |

- **Re-judged:** the three survivors change only the head's own pipeline, and every mode writes release's output kept for the runs the head's holds, so none of them can show a value release hides; their equivalence stands as review r8 judged it.
- **Review r8's check 4, rechecked** (round 7's Mutations, traced): with `p34f` removed, and with `p34f` and site 1 removed, every mode still hides r6's ninth input, which release's own pipeline hides.

### C-23.14

- **The guarantee,** restated: each mode runs release's pipeline for that mode and the head's, both over the same text, and writes release's output kept for the runs the head's holds; invariant 1 now holds by construction. The rules the clause states are the head's pipeline, and each example gives its output, which a mode writes only where release's output holds the same runs.
- **Review r8's check 6:** the converse sentence now says `3fbc4b7c5` is no reference and release's own pipeline shows the value too; the round-7 entry's "as both references do" is limited to the excerpt, whole and `clean` modes and its "true now" to review r7's corpora, each with a dated correction; round 6's "(not so until round 7)" is corrected; round 7's invariant 1 carries a dated correction for its scope (above).
- **The two report notes:** the memo count test reaches only `p34f`'s site (now said, in round 7's P3-2 and in C-23.14's memo sentence), and the pass round 7 left untraced is traced (round 7's Mutations, from review r8's check 4, rechecked against the composed scrub).
- **The `Bearer` cost sentence** now says each mode also runs release's own rule, #231's lookahead.
- **The change log** gains round 8's entry.

### Tests

**Red first.** `test_scrub_release_first` (committed first, `9b0c028e5`) fails at `b0fd3ce8c`: there is no frozen copy and no composition. Review r8's nine cases (`94344839f`) were red at `be4f090b6`, by design. Each mutant above that removes release's pass from one mode, or composes at the base function, fails a named case of `test_scrub_release_first` or review r8's file.

**Holds.** The lock admitted nothing in 30 minutes at 13:01Z to 13:31Z (load up to 123, the second slot closed), so I stopped my own waiter, and every hold below ran directly, outside the lock, one pytest each, three at a time from 15:30Z, at load 96 to 143. Counts are the collected cases; a hold with no failure line passed them all, but for the opt-in skips (`SF_SCRUB_FULL_MATRIX`).

| Hold | Started, load | Files | Cases | Result |
|---|---|---|---|---|
| 1 | 15:30Z, 96 | `_r8`, `_r7`, `_r6`, `_r5` | 66 | passed |
| 2 | 15:45Z, 135 | `_r4` | 36 | passed |
| 3 | 15:59Z, 100 | `_r3` | 30 | passed |
| 4 | 16:06Z | `_r2` | 75 | not finished: stopped at 16:18Z for the time box (an earlier full run, before the pins were rewritten, showed only the 4 pins since rewritten) |
| 5a | 15:31Z, 106 | `test_scrub_inline_headers`, `-k a_swallowed_flag_or_key_still_owns` | 100 | passed |
| 5b | 15:39Z, 142 | the same, `-k an_unclosed_quote_does_not_take` | 72 | passed |
| 5c | 15:45Z, 122 | the same, the rest but the bound and cost tests | 73 | passed |
| 5d | after 5c, about 130 | the same, the bound and cost tests | 28 | 27 passed; the 1 MiB refusal's probe passed its 60 s timeout (load, below) |
| 6 | 15:31Z, 105 | `test_daemon_log_cut_windows` | 177 | passed |
| 7 | 15:43Z, 110 | `test_daemon_log_handler` | 128 | 123 passed; 2 patch-target failures, fixed after (below); the three 4 MiB time-bound cases at load (below) |
| 8 | 16:00Z, 107 | `test_scrub_no_new_leak`, `test_cpu_scaling` | 8 | passed |
| 9 | 15:56Z, 107 | `test_scrub_key_order`, `fake/test_state_contract`, `test_test_module_names`, `test_invariants_index` | 133 | passed |
| new | 16:06Z, 93 | `test_scrub_release_first`, `test_scrub_release_frozen` | 27 | passed (the frozen pin read the blobs: this clone holds them) |

- **Load timings, not this round's code:** the 1 MiB refusal's cost probe (5d) passed its 60 s subprocess timeout at load about 130; run in-process at load 112 it reads 1.07 against release's own line (bound 2.5). The 4 MiB record tests (7) passed their 1.0 s best-of-three bound at load 110 to 134; the `message` case, rerun alone at load 106, passed. No bound was changed.
- **A fix after its hold:** hold 7's two raising-scrubber tests patched `daemon_log.scrub_secrets`, a name the module no longer imports; they now patch `daemon_log._head_scrub_secrets`, the block form's head pass, and were not rerun in a hold.
- **Outside the brief's holds:** before the composition settled, one run (from 14:12Z, an earlier state of the tree) of the other modules that call the scrubber (`test_conversation_redact`, the daemon.log modules, `test_sessions_handoff*`, the suggestion modules) showed one failure by 54%; it was not finished or rerun.

### Status

- **Built:** decisions 1 to 5 and 7, decision 1 with the change above (the head's pipeline reads the text, and release's output is kept for the runs the head's output holds), and the long-text excerpt cut where both scrubbers allow. Decision 6's fallback (`FIX_R8c.patch`) is not built.
- **The deviations,** each with its counterexample above: decision 1's mechanism (the brief's wording lost the head's gains in 91,978 texts); idempotence (18,974 texts change on a second scrub, which only joins placeholders; the contract's "monotone, not idempotent" stands); and the cost guards rebased on release's own scrub (the pre-#231 reference is about 5.7 times cheaper on the 1 MiB refusal, since release's #231 lookahead now runs in every mode).
- **Not run:** hold 4 (`_r2`) to the end after its pins were rewritten; holds 5d and 7 again after the load-timing failures and hold 7's patch fix; any hold under the lock; the other modules that call the scrubber (one early run, unfinished); `test_core_live`, Python 3.12, CI and the rest of `tests/unit` and `tests/fake`; `be4f090b6`'s idempotence count; review r8's third P2-4 input by name.
- **For the hub:** daemon.log's record cut (`cut_whole`) is the head's, before both forms, so the by-construction claim covers what the forms write of the cut text; and the prose cost (a run either pipeline hides goes whole) changes the PR's headline examples (`curl -H 'Authorization: [REDACTED] [REDACTED] https://x`).

## Fix round 9 (CI run 40545353102, 2026-10-28)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix9.md`. **CI:** run 40545353102 at `67674bb14` (round 8 and the hub's merge of `release/217` at `c9430763f`, which changed none of the scrubber's code: in `refusals.py` it adds one refusal message): both shard-2 jobs, on Python 3.12 and 3.14, failed `test_within_the_budget_it_is_scrub_then_truncate`, C-23.36's property that a text within the budget is bounded as `truncate(scrub_secrets(text), limit)`, with `parts=['password = '] * 3`, `limit=30`, `strip=False`. Round 8 had not run that module (its "Not run" list). This round ran in its own worktree (job `20261028-050056-pr239-fix9`) from 09:09Z, with no Git remote operations and no sub-agents; every test value is a dummy.

### The counterexample, classified

- **The path is the within-budget one.** The text is 33 characters, under the budget of 262,144, so `_released_pieces` scrubs it whole (`handoff.py`, its `len(text) <= release.MAX_SCRUB_CHARS` branch). With a limit, round 8 then cut release's stripped scrub into `truncate`'s pieces, `p`, the marker and `=`, and kept each piece for the runs the head's scrub of the whole text holds.
- **The outputs at `67674bb14`,** executed:
  - `scrub_bounded(text, 30)` was `'[\n… [9 characters omitted] …\n='`, count 2;
  - `truncate(scrub_secrets(text)[0], 30)` is `'p\n… [6 characters omitted] …\n='`, count 2, and so is release's own `scrub_bounded(text, 30)`;
  - `scrub_secrets(text)` is `'password = [REDACTED] = [REDACTED] = '`, count 2: release's and the head's own scrubs agree on it.
- **The cause:** the head's scrub holds `password` as a run, but not `p`, the part of it that release's cut left, so `shown_by_both` wrote `[REDACTED]` for the piece. The result was then 39 characters, over the limit, so `truncate` cut it again: `[`, 9 characters omitted, `=`.
- **So the hub's decision 3 applies, not decision 2:** C-23.36 was broken by round 8, on its own path. The property's texts (at most 40 fragments, about 1,200 characters) never reach the excerpt path.
- **How often:** of 5,000 random cases drawn as the property draws them (its fragments, up to 40, limits 0 to 400, either strip), 1,866 differed at `67674bb14`, each where release's scrub is past the limit; none differ now.

### The fix

- **`_released_pieces`** returns a text release scrubs whole (within the budget, or a longer text once its blocks are resolved) as one piece: release's scrub before its `truncate`, stripped first when there is a limit, as `truncate` strips. `scrub_bounded` already bounds what it writes with `truncate` where that is past the limit. So within the budget the bounded mode is exactly `truncate(scrub_secrets(text), limit)`, and it is release's output wherever the two scrubbers write the same, as on CI's case.
- **Nothing else changes:** the excerpts of a longer text, the composition and every other mode are round 8's. (Round 10 changed the excerpts' bound and `scrubbed_line` to the same order; see Fix round 10.)

### The argument for the bounded mode, within the budget

The written output is `truncate(shown_by_both(r, h), limit)`, with `r` release's scrub of the whole text and `h` the head's.
1. **`shown_by_both(r, h)`** shows no character release's scrub takes out and no value the head's hides (round 8's argument).
2. **`truncate`** writes a head and a tail of that text, each a slice of it, with a marker line between them, or the elision alone. So every character it writes outside the marker is one of the composed text, in place, and the marker's line breaks keep the two slices from joining into a run.
3. **What changes from round 8:** the cut is made in the composed text. Where the head's runs change the length, it falls elsewhere than release's cut of its own scrub, so the bound can keep a value that both scrubbers show and that release's own `clean` cut out, or cut out one release's kept. It never keeps a value either scrub takes out, nor a piece of one: release's own cut can (`--SECRET7a` below), since the composed text holds no such run.

An executed case (from the corpora below), `clean` at 96: the text is `x`, `X-Authorization: x --SECRET7a1_token`, `'r`, `SECRET7t5` and a tab, `api_key:--api-key` with a carriage return, and `:r SECRET7c5`, on six lines.
- Release's own `clean` cuts its scrub (103 characters, stripped) to `x`, `X-Authorization: [REDACTED] --SECRET7a`, the marker for 7 characters, `CTED]` and `:[REDACTED] SECRET7c5`.
- The composed scrub is 96 characters, within the limit, so the bound keeps all of it: `x`, `X-Authorization: [REDACTED] [REDACTED]`, `[REDACTED]`, `SECRET7t5` and its tab, `[REDACTED]`, and `:[REDACTED] SECRET7c5`.

`SECRET7t5`, which both scrubs show, is kept, though release's cut left it out. The start of `SECRET7a1_token`, which release's cut showed, is not.

### Measured

**Review r8's corpora at `clean`'s own limit.** Release's stripped scrub (reminders stripped, as `clean` strips them) is longer than 4,000 characters for none of the 461,248 texts; the longest is 635. That is the only branch this round changed, so every mode writes the same as at `67674bb14` on every text, and round 8's table stands: 0 texts show what release hides (598 at `be4f090b6`), 0 lose a gain, the head hides what release shows in 130,859, 2,389 split with 0 excerpts showing what the whole hides, 0 reveal and 18,974 are not idempotent. Executed below on 105,515 of them, the two trees' `clean` at 4,000 are identical on every text.

**A cut study.** The corpora's texts are short, so `clean` at 4,000 never cuts them. The study ran `clean` at 48, 96, 192 and 4,000 characters, at `67674bb14` (round 8) and now, on 105,515 texts:
- every text of review r7's refused-`Bearer` set, P2-1's family and seeded joins;
- review r8's new joins and both regression sets;
- the first 2,500 of each other set.

It used `cw9.py`, `drive9.py` and `an9.py` (kept with the holds' logs and the mutants in `~/reviews/subfleet-hub/scrub-inline-2026-10-18/fix9-tools/`), four worker processes, outside the lock. Counts are of texts.

| Limit | Cut through the changed branch | Now ≠ `truncate(scrub_secrets)` | Round 8 ≠ now | Now = release's `clean` (round 8) | Shows what a whole-text scrub takes out, now (round 8) | Shows what release's `clean` cut out, now (round 8) | Now shows what round 8 left out (and the reverse) |
|---|---|---|---|---|---|---|---|
| 48 | 60,277 | 0 | 60,246 | 62,660 (37,540) | 0 (0) | 294 (0) | 308 (0) |
| 96 | 17,110 | 0 | 16,976 | 58,470 (56,021) | 0 (0) | 249 (0) | 353 (6) |
| 192 | 260 | 0 | 260 | 58,309 (58,305) | 0 (0) | 24 (0) | 26 (0) |
| 4,000 | 0 | 0 | 0 | 58,309 (58,309) | 0 (0) | 0 (0) | 0 (0) |

- **"A whole-text scrub"** is release's, the head's own or the composed one; each is 0 separately.
- **Release's own cut:** every value the bound now shows that release's own `clean` cut out is one release's whole-text scrub shows (294, 249 and 24, all of them). Round 8 showed none, since it kept only release's pieces; that is the property this round gives up for C-23.36's equality. The two cannot both hold wherever the composed scrub's length differs from release's: the equality puts the cut in the composed text, and release's own cut falls elsewhere.
- **Matching release:** the bound now writes release's own `clean` exactly more often than round 8 did, at every limit that cuts, since a text both scrubbers scrub alike is now cut alike.

### Tests

- **Red first** (`ec9027ade`): `test_within_the_budget_ci_40545353102_password_thrice_at_30_is_scrub_then_truncate` pins CI's case by name, with and without reminders stripped, and as `clean`. `test_within_the_budget_a_text_its_limit_cuts_is_the_whole_scrub_cut` draws a limit under the whole-text scrub's length, so every case is cut or elided. The first property now records which of `truncate`'s paths each case takes. At `67674bb14` all three failed (one hold, below), the second on CI's text too, at 36.
- **The pieces property** (`test_release_s_pieces_join_to_release_s_output`) now says what the pieces are within the budget: release's scrub, which `truncate` bounds to release's output. It also checks that the bounded mode is the whole-text mode, cut, and shows no value release's whole scrub hides.
- **Excerpts keep the head's gains** (`485b71399`). The hub's ruling is that an excerpt may hide more than the whole-text scrub, never less. Its property placed no value that release shows and the head hides, so a mutant that let an excerpt show one survived it. Two such shapes are now among its shapes (`token:--x_token` and `sent Cookie: --password` before a value). `test_an_excerpt_hides_what_only_the_head_s_scrub_hides` pins both in the head and the tail excerpt, with a count no lower than release's.


**Holds** (one pytest each, at most one worker; loads are 1-minute averages at the start). The lock admitted five, four of them before 11:12Z. The other eight waited 31 to 41 minutes unadmitted (the second slot was often closed at load over 120), so I stopped my own waiters and ran those directly, outside the lock, as the rules allow. The host's load made every run slow: the same files took three to ten times as long as in review r8's holds.

| Hold | Started, load | Files | Cases | Result |
|---|---|---|---|---|
| red | 09:46Z, lock A, 101 | the three red tests and one other `within_the_budget` test, at `ec9027ade` (the code at `67674bb14`) | 4 | 3 failed, as they should; 1 passed |
| 1 | 10:08Z, lock A, 82 | `test_sessions_handoff_excerpts` (all of it at `ec9027ade`), `test_scrub_release_first`, `test_scrub_release_frozen` | 182 | 181 passed; `test_the_wrapper_walk_matches_the_single_pattern` failed because I edited the module while the hold ran (Hypothesis read the changed source for its digest), and passed in 1b-i. 62 minutes |
| 2 | 10:30Z, lock B, 85 | `_r8`, `_r7`, `_r6`, `_r5`, `_r2` | 141 | 138 passed, 3 opt-in skips (`SF_SCRUB_FULL_MATRIX`) |
| 3a | 10:52Z, lock B, 104 | `_r4` | 36 | passed |
| 8 | 11:43Z, direct after 31 minutes queued, 80 | the two within-budget properties at 2,000 examples each (below) | 2 | passed |
| 1b-i | 11:48Z, direct after 31 minutes queued, 85 | `test_sessions_handoff_excerpts -k "wrapper_walk or within_the_budget or only_the_head_s_scrub_hides"`, the module as committed | 9 | passed |
| 6a | 11:53Z, direct after 40 minutes queued, 172 | `test_scrub_key_order` and the three guards | 133 | passed |
| 1b-ii | 11:59Z, direct (the rest of 1b), 31 | the excerpt property with its two new shapes | 1 | passed (14 minutes) |
| 5b | 12:00Z, lock B, 41 | `test_scrub_inline_headers`: `an_unclosed_value_reads_its_lines_end_once`, `inline_headers_and_bearer_keep_scrubbing_linear`, `bearer_refuses_exactly_what_the_lookahead_did` | 19 | passed |
| 6b | 12:10Z, direct after 41 minutes queued, 33 | `test_scrub_no_new_leak`, `test_daemon_log_scrubbing`, `test_sessions_handoff` | 235 | passed |
| 4 | 12:20Z, direct after 40 minutes queued, 65 | `test_scrub_inline_headers` but for its five bound and cost tests (its 1 MiB refusal test, renamed in round 8, ran here) | 245 | passed |
| 3b | 12:32Z, direct after 40 minutes queued, 78 | `_r3` | 30 | 29 passed, 1 opt-in skip |
| 5a | 12:44Z, direct after 37 minutes queued, 35 | `test_scrub_inline_headers`: `the_bearer_rule_costs_what_release_217s_did`, `the_bearer_rule_keeps_other_threads_responsive_at_the_bound` | 9 | passed |

- **Every case of `test_sessions_handoff_excerpts` ran,** 159 as committed: hold 1 ran the 155 it held then, and 1b ran the one that failed there, the 4 new ones and the property whose shapes changed.
- **Every scrub module ran** (`git ls-files 'tests/**/test_*scrub*'`, thirteen), as did `test_sessions_handoff*.py` and the three guards.
- **Over 10 minutes:** holds 1, 2, 3a, 6b, 4, 3b and 1b-ii ran longer than 10 minutes at these loads. Hold 1b-ii is one test; the others I split no further.
- **The properties at 2,000 examples** (hold 8, `--hypothesis-show-statistics`), counts of valid cases:
  - **`test_within_the_budget_it_is_scrub_then_truncate`:** 2,000 passed; 1,471 not cut, 222 cut, 307 elided (343 more invalid, Hypothesis's own).
  - **The cut property:** 2,000 passed; 556 cut, 1,444 elided (562 more invalid, 38 of them its `assume`).
  - **In the module's own run (200 examples each, hold 1b-i):** the cut property drew 62 cut and 138 elided cases. The first property cuts about 11% of its valid cases (222 of 2,000 above), so its own 200 draw about 22.

### Mutations

Each mutant is one edit in a copy of `58b5e0eb2`'s `subfleet/`, with this round's tests. The named tests ran against each as plain calls (review r7's `runcases.py`), outside the lock.

| Mutant | Killed by |
|---|---|
| Round 8's bounded mode (the code at `67674bb14`) | the property, the cut property and CI's pin (the red hold) |
| The excerpt path returns `truncate` of the whole text's scrub (`_released_pieces` scrubs every text whole) | 3 of 7: `test_an_oversized_text_keeps_a_scrubbed_head_and_tail_within_the_work_bound`, `test_a_line_longer_than_an_excerpt_is_left_out_whole`, the pieces property |
| The excerpt path drops one value from what it hides (each excerpt keeps its first run that the head's scrub of it does not hold) | the pieces property, and 2 of the 4 cases of `test_an_excerpt_hides_what_only_the_head_s_scrub_hides` (the `Cookie` shape, head and tail; in the other the first such run is `token:[REDACTED]`, which holds no value). `test_an_excerpt_never_shows_a_value_the_whole_scrub_removes` (80 cases) did not kill it. |
| The within-budget path skips the scrub (the text is its own scrub) | 5 of 27: the property, the cut property, CI's pin, `clean`'s no-leak test in `test_scrub_release_first`, the pieces property |

### C-23.14 and C-23.36

- **C-23.14's mode sentence** said every mode shows no character that release's same mode hides, `scrub_bounded` and `clean` among them. A bound within the budget now cuts where `truncate` cuts the written text, so the sentence now names that cut. It says what each scrub takes out, by construction, and that the cut can keep a character release's own cut leaves out, never one either scrub takes out.
- **C-23.36** says nothing about the bounded scrub itself. The test that cites it states the equality, which holds again, so its wording is unchanged.
- **The change log** gains round 9's entry. Round 8's three sentences here that describe the bounded mode as release's pieces each carry a dated note.

### Status

- **Built:** the hub's decision 3. Within the budget the bounded scrub is again exactly `truncate(scrub_secrets(text), limit)`, with CI's case pinned by name and red first. The excerpt property, and a new test, now check that excerpts keep the head's gains. Decision 2 (restating the property) was not needed: its precondition was right, since the case is on the within-budget path.
- **What this gives up, and why:** round 8's bound kept only release's own cut pieces, so it never kept a value release's own `clean` cut out. Now the cut is the composed text's, which C-23.36's equality requires. In the cut study that keeps such a value in 294, 249 and 24 texts at 48, 96 and 192 characters. Each value is one that release's scrub of the whole text shows. Neither scrub's hidden values ever show (0 at every limit).
- **Round 8's counts** stand: no corpus text reaches the changed branch at `clean`'s 4,000.
- **Not run:**
  - CI, Python 3.12, `test_core_live` (it can't run on a lane), and the rest of `tests/unit` and `tests/fake`;
  - the cut study over all 461,248 texts (it ran on 105,515: six sets whole, and the first 2,500 of each other);
  - `be4f090b6` in the cut study;
  - the mutants under pytest: they ran as plain calls of the named tests.

## Fix round 10 (#239 review r9b, 2026-10-28)

**Brief:** `~/reviews/subfleet-hub/scrub-inline-2026-10-18/brief-fix10.md`. **Review:** r9b, REQUEST_CHANGES at `f5cbd1df7`. It upheld round 9's ruling within the budget and found two P2s from the same cut-before-compose order on the paths round 9 left alone, and three P3s; its pins (`tests/unit/test_scrub_review_r9b.py`) and `FIX_R9b.patch` came with it. This round ran in its own worktree (job `20261028-154036-pr239-fix10`) from 19:45Z on `b34ba3b2a`: round 9's `f5cbd1df7`, the hub's merge of release/217 (`5fb178278`) and the pins. There were no Git remote operations and no sub-agents, and every test value is a dummy.

### The findings, executed at `b34ba3b2a`

- **P2-1, the excerpt path with a limit** (`_released_pieces`). Release's excerpts were cut to the limit first, then each was kept for the runs of the head's scrub of the whole excerpt. That scrub holds no part-word, so the part-word at each cut became `[REDACTED]`, the result passed the limit, and `truncate` cut it again, through release's marker. Review r9b's four inputs (its marker probe, run on both trees):

| Input (review r9b's) | Limit | Round 9's code (`b34ba3b2a`) | Release | Now |
|---|---|---|---|---|
| the excerpts module's oversized text | 60,000 (`TEXT_EVENT_MAX`) | `[REDACTE`, then `… [6 characters omitted] …` | 342,115 | 342,115 |
| 300,000 characters of prose | 4,000 | `… [`, then `… [6 characters omitted] …`, then `ACTED]` | 296,034 | 296,034 |
| the same | 8,000 (`recent_excerpt`'s cap for a tool result) | `[REDA`, then `… [13 characters omitted] …`, then `DACTED]` | 292,034 | 292,034 |
| the same | 600 | `[REDACTE`, then `… [6 characters omitted] …` | 299,434 | 299,434 |

Each output was within its limit, and each written marker counted what the second cut took from the first result. Now each output is release's own, character for character, and holds one marker.

- **P2-2, `scrubbed_line`** (`_line_bounded`: daemon.log's lines, a refusal's words, a suggestion's title and summary). Release's folded line was cut first, then kept for the runs of the head's uncut line, so the part-word at the cut became `[REDACTED]` or was dropped:

| Line (review r9b's pins) | Limit | Round 9's code wrote | Release's cut, and now |
|---|---|---|---|
| `OSError: [Errno 2] No such file or directory: '/Users/someone/…'` | 60 | `…directory: [REDACTED]…` | `…directory: '/Users/someo…` |
| seventy `x`s | 60 | `[REDACTED]…` | 59 `x`s and `…` |
| `the scheduler refused to start because the configuration file was missing` | 60 | `…the configuration …` | `…the configuration fi…` |
| `job 1234 failed: ` and a path, sixty times | 2,000 | `job 1234 failed: [REDACTED]…` | the line's first 1,999 characters and `…` |

Both scrubbers write each of these lines alike, so release's own cut is what the mode should write; it now does.

### The fix

- **`FIX_R9b.patch`, its code lines unchanged** (`978615669`):
  - `_released_pieces`, past the budget with a limit: each excerpt is kept for the runs the head's scrub of it holds (`shown_by_both`), then cut as release cuts its own. Its pieces are returned already written, so `scrub_bounded` joins them as they are.
  - `_line_bounded`: `bounded(shown_by_both(released, head), limit, mark)`, round 9's rule for `clean`.
  - The patch's tag comments are replaced by comments and docstrings that say what the code does.
- **One deviation, for the hub's "every over-budget marker gives release's own count"** (`6adaa6ae7`). The patch counted the marker from the composed excerpts. Where the head's scrub of an excerpt hides more than release's, that is release's rule applied to a longer or shorter text, so the count differs from release's. The counterexample, with both scrubbers' excerpts shrunk to 90 characters at limit 4,000: a text whose head excerpt holds `x Cookie:` above `password = SECRET7f6 and more words`, which the head's scrub hides whole and release's shows in part, wrote `… [94 characters omitted] …` where release writes 121. Of 1,562 such over-budget texts whose cuts both scrubbers make alike, 315 differed, by 1 to 38 characters. The smallest change that keeps the decision's intent: the marker is what release's own cut of the same excerpts leaves out, as it already was without a limit, and the written excerpts are still composed first, then cut. Release's three cut lines are now one helper, `_cut_excerpts`, used for both. Measured on the same texts: 0 of 1,562 differ.
- **Where "no output contains a split `[REDACTED]`" holds, and where it can't.** On review r9b's inputs, no output holds one, and none holds a cut marker or a second marker. A single cut can still fall inside a placeholder, as release's own cut and C-23.36's `truncate` can. Both are kept: equality with release's output, and C-23.36's equality, require that cut. So the claim holds as: no fragment comes from a second cut, and where both scrubbers write the same excerpt, every fragment is release's own. Measured at small bounds (below): 0 new fragments where both scrub each excerpt alike; 55 where the head hides more or cuts more cautiously (round 9's code: 356); release itself writes one in 285 of the 2,141.

### The argument for the bounded modes

**Past the budget, with a limit,** the written output is `h′ + m + t′`, stripped:
- `h′` and `t′` are `_cut_excerpts(shown_by_both(R_h, H_h).strip(), shown_by_both(R_t, H_t).strip(), usable)`;
- `R_*` and `H_*` are release's and the head's scrubs of the same excerpt;
- `m` is release's marker, counting what `_cut_excerpts(R_h.strip(), R_t.strip(), usable)` leaves out.

The four claims:
1. **Nothing release hides comes back, and nothing the head hides shows:** round 8's argument holds for each composed excerpt, and `h′` and `t′` are a prefix and a suffix of them.
2. **The length holds:** `h′` is at most 60% of `usable`, and `t′` at most the rest. `m` is no longer than the longest marker, since its count is at most the text's length. So the result is within `limit`, and nothing cuts it again.
3. **The marker is release's:** it is release's own count wherever both scrubbers cut alike, whatever the head's scrub hides.
4. **The trade:** the cut is made in the composed text, so it can keep a character release's own cut left out, but never one either scrub takes out, as round 9's cut within the budget.

**`scrubbed_line`** is `bounded(shown_by_both(released, head), limit, mark)`. `bounded` keeps a prefix, retreats from a placeholder the cut would split and from a fragment that still holds a secret's shape, then adds the mark. So it shows nothing either line hides, splits no `[REDACTED]`, and keeps to `limit`. Where both lines agree, it is release's own cut exactly.

### Measured

**Review r9b's corpora, rebuilt** with review r8's `gen.py`: `r7_line_start` (seed 7240, 56,275 texts) and `r7_bearer` (3,825), 60,100 in all, as review r9b had them. Review r9b's `cw9.py` and `an9.py` ran unchanged on them, with "before" at round 8's `67674bb14` as in review r9b's table, and a third tree at round 9's `b34ba3b2a`:

| Limit | Now ≠ `truncate(composed whole)` | Shows what any whole scrub hides | Round 8 ≠ now | Shows what release's `clean` cut out | Now = release's `clean` (round 8) | Round 9 ≠ now |
|---|---|---|---|---|---|---|
| 48 | 0 | 0 | 31,829 | 858 (all 858 are values release's whole scrub shows) | 25,798 (16,420) | 0 |
| 4,000 | 0 | 0 | 0 | 0 | 25,649 (25,649) | 0 |

Every column review r9b reported as 0 is 0, and every other cell is review r9b's. Each corpus text scrubs within the budget, where this round changed nothing, so `clean` is round 9's on every text.

**`scrubbed_line` on the same 60,100 texts** (`sl10.py`, `an10.py`; new this round, with round 9's tree beside it), at 60 characters and at `ERROR_MAX`, 500:
- **At 60** (release's folded line is longer in 7,567 texts):
  - **0 everywhere it must be:** outputs other than `bounded(shown_by_both(released, head), 60, "…")`, outputs over the limit, outputs that show a value release's line or the head's hides, and outputs with a `[REDACTED]` fragment (release's own cut has none either).
  - **Where both lines agree** (30,120 texts): 0 outputs differ from release's own cut, and 0 write a `[REDACTED]` it lacks. Round 9's code differed in 328 of them (P2-2).
  - **The trade:** 826 outputs show a value release's own cut left out, each one release's line shows. Round 9's showed none, since it kept only what release's cut kept.
  - **Against round 9:** round 9's output differs from now in 5,094 texts, and shows nothing now hides.
  - **Release's own cut, matched exactly:** now in 31,865 texts, round 9 in 33,471. Where the lines differ, round 9 kept release's cut whenever the head's line held its runs, while the cut now falls in the composed line.
- **At 500** no corpus line is cut, and now and round 9 write the same.

**Excerpts at small bounds** (`probe_excerpt2.py`, seed 10239): 3,000 random texts of 16 secret-bearing and ordinary line shapes, both scrubbers' excerpts shrunk to 40, 90 or 400 characters, limits 30 to 4,000, either strip. 2,141 are over the budget. Each case is classed by whether both scrubbers cut alike and whether they scrub both excerpts alike:

| Class (texts) | Equal to release's output: round 9 → now | Marker ≠ release's: round 9 → now | A `[REDACTED]` fragment release's output lacks: round 9 → now |
|---|---|---|---|
| both cut alike, both excerpts scrubbed alike (1,045) | 737 → 1,045 | 251 → 0 | 137 → 0 |
| both cut alike, the head hides more (813) | 142 → 316 | 330 → 0 (315 with `FIX_R9b.patch`'s count) | 180 → 50 |
| the two cut differently, the head's cut the more cautious (283) | 48 → 114 | 221 → 142 (different excerpts) | 39 → 5 |

- **Every class, now:** 0 outputs over the limit, 0 with two markers (round 9: 2), and 0 that show a value release's whole scrub or the head's hides.
- **The fragments left** are single cuts falling inside a placeholder the head's scrub added. Release's own output holds a fragment in 285 of these texts.

**Holds and corpora ran on this round's head, `c2289e05b`'s code** (the text commits change no code).

### Tests

- **Red, then green.** Review r9b's eight pins failed at `b34ba3b2a`, as they should (hold red: 19:59:30Z, slot B, load 64, 8 failed in 39 s), and pass at the fix (hold g: 21:01:59Z, slot A, load 112, 8 passed in 66 s).
- **The three cut-first cases** (decision 2) now assert the rule: the output is the composition cut, it shows nothing either scrub hides, and it keeps to its limit. None was deleted.
  - **`[scrubbed_line]` of the mode test:** its composition, `_cut_line`, is `bounded(shown_by_both(released, head), limit, mark)`. The mode's release and head pipelines are now the two uncut lines, so the hiding test compares it with the scrubs, not release's cut. The test now also checks each bounded mode's length.
  - **The pieces property** compares the mode with release's own `scrub_bounded`, its scrubs composed first and its cuts both scrubbers' (`_composed_release`), the marker's count set aside, and checks release's marker wherever both scrubbers cut alike past the budget. It keeps the join to release's output where nothing cuts the pieces, or where both scrub each excerpt alike, and checks that no value either scrubber's same mode without a limit hides shows, and the length.
  - **The order check** reads `scrubbed_line` against release's uncut line, and checks the composition and the length.
- **The excerpts module's oversized-text test** reads one marker with release's count, where it accepted any marker (`test_sessions_handoff_excerpts.py:139`; it had passed with `… [6 characters omitted] …`).
- **`_assert_stable`** (P3-2) is monotone: a second scrub's `SECRET7` runs are a subset of the first's, as review r9b's `x Cookie:\nx Cookie:\n;v SECRET7c3` needs.
- **At 2,000 examples** (hold long, review r9b's module and two more):
  - C-23.36's property: 1,445 not cut, 234 cut, 321 elided;
  - the cut property: 569 cut, 1,431 elided;
  - the excerpt property with both scrubbers' bounds shrunk: 2,000, all over the budget (633, 719 and 648 at 40, 90 and 400);
  - the pieces property: 1,453 within the budget and 547 past it;
  - the order check: 818 with `scrubbed_line` cut, 1,182 not.

**Holds** (one pytest each, at most one worker; loads are 1-minute averages at the start). The host ran at loads 45 to 144, so the second slot was often closed. Each lane's driver ran its holds in turn, and two lanes ran side by side. Two holds waited the full 30 minutes and then ran directly, outside the lock, as the rules allow; they are marked direct.

| Hold | Started, slot, load | Files | Cases | Result |
|---|---|---|---|---|
| red | 19:59:30Z, B, 64 | `test_scrub_review_r9b`, at `b34ba3b2a` | 8 | 8 failed, as they should (39 s) |
| g | 21:01:59Z, A, 112 | `test_scrub_review_r9b` | 8 | passed (66 s) |
| s1 | 21:19:47Z, A, 113 | `test_scrub_release_first`, `test_scrub_release_frozen` | 27 | passed (7 min) |
| h3 | 21:28:26Z, A, 144 | `test_daemon_log_cut_windows` | 177 | passed (17 min) |
| long | 21:48:27Z, A, 69 | the 2,000-example module (scratch, not committed) | 6 | passed (7 min 49 s) |
| h4a | 21:56:35Z, A, 103 | `test_daemon_log_handler` but its 4 MiB, parts and group tests | 56 | passed (5 min) |
| guards | 22:02:14Z, A, 94 | `tests/fake/test_state_contract.py`, `test_test_module_names`, `test_invariants_index` | 63 | passed (5 min 24 s), after the long module was removed |
| h4b | 22:08:23Z, A, 140 | `test_daemon_log_handler`'s five 4 MiB bound tests | 5 | passed (81 s) |
| s2a | 22:10:46Z, A, 105 | `test_sessions_handoff_excerpts` but its excerpt property and its 84 name-above-the-tail cases | 74 | passed (13 min 31 s) |
| s3a2 | 22:29:05Z, B, 78 | `test_scrub_inline_headers` but its swallowed-item matrix and its 28 bound and cost tests (every `_assert_stable` caller is here) | 145 | passed (3 min 56 s) |
| h4c | 22:38:49Z, B, 80 | `test_daemon_log_handler`'s 4 MiB cost-shape tests | 18 | passed (2 min 49 s) |
| h4d1 | 22:45:49Z, B, 115 | five of `test_a_cut_never_parts_what_a_rule_or_folding_joins` | 5 | passed (3 min 43 s) |
| h4d2 | 22:52:46Z, A, 123 | four more | 4 | passed (6 min 58 s) |
| s3b | 23:03:56Z, direct, 104 | `test_scrub_inline_headers`'s bound and cost tests | 24 of 28 | 16 passed, 8 failed; stopped at the 25-minute cap, before pytest printed the failures; rerun as s3b-rerun |
| h7 | 23:34:39Z, direct, 56 | `test_conversation_handoff_briefs`, `test_conversation_redact`, `test_store_ids` | 64 | passed (33 s) |
| s2b | 23:36:56Z, A, 61 | `test_sessions_handoff_excerpts`'s name-above-the-tail cases | 84 | passed (6 min 8 s) |
| h8a | 23:41:37Z, B, 64 | `test_suggestions_r1b_adversarial`, `test_suggestion_secret_properties` but its two slow tests | 34 | passed (2 min 25 s) |
| s3b-rerun | 23:56:55Z, A, 28 (queued 23:41:52Z) | `test_scrub_inline_headers`'s `Bearer` cost, 1 MiB refusal and linear tests: every case `s3b` failed or did not reach, with their siblings | 25 | passed (1 min 42 s) |

- **`s3b`'s failures, by position** (the run was stopped before pytest's summary, so the ids are read from the collection order): `test_the_bearer_rule_costs_what_release_217s_did[~--a_]`, `test_a_mebibyte_refusal_costs_daemon_log_at_most_the_cost_bound_over_release_s`, and 6 of 13 `test_inline_headers_and_bearer_keep_scrubbing_linear` cases; 4 more of those did not run.
  - **All of them passed on the rerun at load 28,** with the 4 that had not run; with `s3b`'s own 16 passes, all 28 of those cases have passed on this head.
  - **Two of the three tests run no code this round changed.** The `Bearer` cost test times one rule on a text within the budget. The linear test times `scrub_secrets` within the budget, where this round changed nothing.
  - **The refusal test reaches `_line_bounded`.** So its test body ran by hand on round 9's tree and the head's, alternating, three times each, from 23:36Z at loads 59 to 108: head 1.07, 1.18 and 1.19, against round 9's 1.16, 1.18 and 1.25 (`COST_RATIO_MAX` is 2.5). Composing the whole line before the cut costs nothing measurable here.
- **Over 10 minutes:** `h3`, `s2a` and `s3b` ran longer than 10 minutes at these loads; each was split no further.

### Mutations

Each mutant is one edit in a copy of the head's `subfleet/` (the tests are the head's). The named tests ran against each as plain calls (`runcases.py`), outside the lock, from 21:26Z at loads 66 to 131.

| Mutant | Killed by |
|---|---|
| M0: round 9's code (`b34ba3b2a`'s `handoff.py` and `suggestions.py`) | review r9b's 8 pins, the 3 restated `test_scrub_release_first` cases, and the oversized-text test's marker |
| M1: the excerpt path scrubs every text whole | 3 of 3: the oversized-text test, the line-longer-than-an-excerpt test, the pieces property |
| M2: an excerpt keeps one value the head hides (with a limit, and without) | the pieces property, and the two `Cookie` cases of `test_an_excerpt_hides_what_only_the_head_s_scrub_hides` (2 of 4) |
| M3: the within-budget path skips the scrub | the property, the cut property, CI's pin (3 of 4), and the pieces property |
| M4 (review r9b's): a run kept if it appears anywhere in the head's text | **Not equivalent** (review r10): it keeps a run of release's output that is only part of one of the head's runs, so above `echo SECRET7co1_more` it writes the token in `token:--x_token SECRET7co1` that the head hides. `test_scrub_review_r10` kills it; every older value check reads by substring. |
| M5: runs split on ASCII spaces only | the pieces property |
| M6: the head's mask before release's | `test_an_approval_keeps_the_mask_release_writes` |
| M7: the within-budget piece not stripped | the property |
| M8 (new): cut before compose in `_line_bounded` (round 9's code there) | review r9b's 4 P2-2 pins, `test_each_mode_is_…[scrubbed_line]`, the order check |
| M9 (mine): `FIX_R9b.patch`'s marker count | the pieces property only; review r9b's pins and the oversized-text test pass, since on prose both scrubbers write each excerpt alike |
| M10 (mine): round 9's excerpt path alone | review r9b's 4 P2-1 pins, the pieces property, the oversized-text test |

### C-23.14 and C-23.36

- **Review r9b's corrections, as its check 6 gives them:**
  - the excerpts are cut "where both scrubbers allow a cut (the head's, which refuses every break release refuses)" (P3-3);
  - the head's hiding holds per value, not per place, with review r9b's example as each scrub writes it (P3-1).
- **The mode sentence** now says each bound writes its text kept for the head's runs before it cuts it: within the budget, `truncate` of the whole-text scrub; past it, each excerpt cut as release cuts its own, with release's marker and count; a suggestion's line, masked, then cut. The trade, a cut that can keep a character release's own cut left out but none either scrub hides, now covers every bound.
- **The rules' marker sentence** ("joined by a marker giving the characters left out") is true again after the fix, and is unchanged.
- **C-23.36** is unchanged.
- **The change log** gains round 10's entry. Round 8's sentences here that describe the cut-first order (its modes list, its bounded-mode bullet, and its argument's item 3, "a second `truncate` only cuts") each carry a dated note, as does round 9's "Nothing else changes".

### Status

- **Built:** the hub's five decisions, with one deviation in decision 1.
  - **The excerpt path and `scrubbed_line` now compose first, then cut,** as `clean` within the budget did since round 9 (`FIX_R9b.patch`, its code lines unchanged).
  - **The deviation:** the over-budget marker is release's own count, not the composed excerpts', because the hub's "every over-budget marker gives release's own count" failed in 315 of 1,562 texts at small bounds with the patch alone.
  - **Review r9b's eight pins** went from red to green.
- **What it gives up, as round 9 did:** a bound can keep a value release's own cut left out, but never one either scrub hides. On the corpora that is 858 texts for `clean` at 48 (unchanged from round 9) and 826 for `scrubbed_line` at 60 (new). Each value is one release's own whole-text scrub, or its own line, shows.
- **What it doesn't claim:** a single cut can still fall inside a placeholder the head's scrub added (55 of 2,141 over-budget texts at small bounds, against 356 at round 9's code). Release's own cut and C-23.36's `truncate` cut the same way, and both equalities need that cut. No output holds a cut marker or a second marker.
- **Not run** (the brief's 4-hour mark for starting holds came at 23:45Z):
  - review r9b's holds `test_daemon_log_sites` (83), `test_status_hold_source` (135), and of `test_daemon_log_handler` its group tests (34) and six of its parts tests (`_SYSTEM_REMINDER_RE`, `_URL_PASSWORD_RE`, `flags`, `inline`, `plain`, `quoted`); 88 of `test_daemon_log_handler`'s 128 cases ran;
  - `test_suggestion_secret_properties`'s two slow tests (`…any_start_failure…`, `…shaped_like_a_secret…`), as in review r9b;
  - `test_scrub_inline_headers`'s swallowed-item matrix (100 cases). It reaches `scrubbed_line` only on lines within its 2,000-character limit and `scrub_bounded` only without a limit, where this round's code writes what round 9's did by construction (the changes are a cut line's order and the excerpt path with a limit);
  - `test_sessions_handoff_excerpts`'s excerpt property at its own size (14 minutes in round 9). Its body ran at 2,000 examples with both scrubbers' bounds shrunk (hold long), which reaches the changed path;
  - CI, Python 3.12, `test_core_live` (it can't run on a lane), and the rest of `tests/unit` and `tests/fake`;
  - the mutants under pytest: they ran as plain calls of the named tests;
  - review r8's 461,248-text corpora beyond the two sets rebuilt here, and a cost table like review r9b's.
- **Kept:** the study tools, the logs of every hold and probe, and the mutants' diffs are in `~/reviews/subfleet-hub/scrub-inline-2026-10-18/fix10-tools/`.
