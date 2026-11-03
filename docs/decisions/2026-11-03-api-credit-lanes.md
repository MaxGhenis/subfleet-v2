# API credit lanes

2026-11-03, Subfleet hub. Status: design, build pending.

## The ask

Each Max plan comes with a monthly Claude API credit ($200 on Max 20x). The credit
expires at the end of the billing cycle and never rolls over. The `claude-credits` tool
(public README: github.com/MaxGhenis/claude-credits) stores one API key per plan and
spends the soonest-expiring credit first. Max approved asking the hub for a Subfleet
lane kind that:

- runs agent work on those keys, so the credit takes work that would otherwise draw on
  subscription lanes;
- spends the soonest-expiring credit first;
- falls back to the existing lanes once every key's credit is spent.

## What the credit pays for

`claude-credits` tested this on 2026-10-22:

| Request | Result |
|---|---|
| Messages API with no system prompt, or our own neutral one | covered |
| Messages API with a system prompt that starts "You are Claude Code, Anthropic's official CLI for Claude." | refused: 400 "credit balance is too low" |
| Messages API with the Agent SDK identity line as the system prompt | refused the same way |
| `claude -p` with the key | refused |
| Managed Agents, including `agent_toolset_20260401` running bash | covered |
| Message Batches | covered |

So a credit lane can't run the `claude` CLI. It needs an agent loop that Subfleet owns,
with its own system prompt, or Managed Agents. Rewriting Claude Code's system prompt
through a proxy is out: the README forbids it, and so does the hub.

## Decision: our own tool loop first, Managed Agents later

**Phase 1 runs a Messages API tool loop as the attempt's provider process.** It works
in the job's worktree like every other lane, so diffs, commits, salvage, retention,
`--push-branch` and the review publish rules all apply unchanged. Host tools (uv, the
test suites, the guard) are present.

**Phase 2 may add Managed Agents** for read-only reviews of pushed branches. Anthropic
hosts the sandbox, which takes CPU and memory off a host that about 30 sessions share.
It can't see local-only worktrees or the host's guard, so it doesn't fit build jobs.

## Lanes and credentials

- **New provider `claude-api`.** Each enrolled credit account gets one lane, with
  `account_key` `claude-api:<email>` and a `Credential` of provider `claude-api`, kind
  `keychain-token`, ref `claude-api-<email>`. That ref names the agent-keychain item
  where `claude-credits` stores the key.
- **Resolution.** `credentials.resolve_credential` gains a `claude-api` branch. It reads
  the ref through the agent-secret helper, the way `keychain_command` already reads
  `claude-quota-` refs, and returns `{"ANTHROPIC_API_KEY": value}`. It refuses with
  `credential-refused`:
  - any kind other than `keychain-token`. An `env` credential would read the daemon's
    own `ANTHROPIC_API_KEY`, which is a metered key and never a credit key;
  - a ref that doesn't match `claude-api-<email>`;
  - a value that isn't a standard API key: an OAuth token (`sk-ant-oat…`) or an Admin
    key. The rules mirror `claude_credits.classify`;
  - a value whose fingerprint (sha256 prefix, `claude_credits.fingerprint`) differs from
    the fingerprint the `claude-credits` registry holds for that email.
- **The registry.** `~/.config/claude-credits/registry.json` holds no secrets. Subfleet
  reads it, never writes it, and treats a read or parse failure as "expiry unknown".
- **Enrollment is explicit.** The operator enrolls one account at a time, and only an
  email the registry holds with a fingerprint. The registry records no plan type, so
  nothing marks a Team plan automatically. The hub enrolls personal Max plans only.
  Team credit (the PolicyEngine plan) stays out: using it for other work needs Max's
  go, and Subfleet can't tell whose work a job is.
- **Other providers.** Codex and Claude launches never carry `ANTHROPIC_API_KEY`. The
  D-ENV3 environment allowlist (#270) should already ensure that; a test pins it.

## Spending order

`scheduler.rank_key` orders candidates by band, then measured status, then the
reserves, then the binding seven-day reset, then headroom, in-flight count and lane
id. Credit lanes have no usage windows, because a standard key can't read its balance.
`ranking_usage` skips readings whose utilization is None ("Do not assume zero
utilization"), so every credit lane would rank as unmeasured with reset "9999", and
the lane id would decide. That's the wrong order.

New rule: for a `claude-api` model, `rank_key` is

`(desktop, credit_expires_at, status_rank, band, in_flight, lane_id)`

- `credit_expires_at` is the next renewal from the registry's `renews_day`, by the same
  rule as `claude_credits.days_left`: a renewal on today's date means the credit is new,
  so its expiry is the following renewal. An unknown renewal sorts last.
- `status_rank` puts a lane whose last attempt succeeded before one that hasn't run.
- Expiry comes before band on purpose. Spreading work across lanes spends later credit
  while the soonest credit expires unspent. Taking the soonest first is never worse.
- The lane's slot cap bounds concurrency on one key. Rate limits aren't the
  constraint: the Thesis organization reported 1,000 requests a minute and 400,000
  output tokens a minute.

A differential test checks the order against a frozen copy of `claude_credits.order`,
restricted to admissible lanes.

## Exhaustion and fallback

- **Exhaustion closes the lane.** "Your credit balance is too low" comes back as a 400.
  When the harness gets it, it sends one neutral 1-token Haiku request on the same key,
  the same test `claude-credits mark` runs:
  - **Fails the same way:** exhaustion. `classify` returns a closure with
    `ClosureReason.CREDITS` (the enum already has it), scope `account`, until the
    plan's next renewal (`claude_credits.exhausted_until`). On the renewal day itself,
    or with no renewal day known, it lasts one day.
  - **Succeeds:** the credit refused the request by kind. Our own system prompt should
    make that impossible, so the attempt fails as `credit-refused-by-kind`, the lane
    stays open, and the daemon raises an alert.
- **Workspace caps.** A colleague's shared workspace answers "You have reached your
  specified workspace API usage limits". That closes the lane for one day, because the
  cap holder can raise it at any time.
- **No admission probe.** C-11.4 probes unmeasured lanes before writable or hard work,
  so a launch doesn't burn a window that has no reader. Credit lanes have no window to
  burn: a request refused for credit costs nothing. Credit lanes skip that probe, the
  first request of an attempt does its job, and `adapter.probe` returns no readings.
  The daemon makes no HTTP calls to Anthropic; all API traffic stays in the harness.
- **Fallback runs through the policy chain.** New aliases `opus-api`, `sonnet-api` and
  `haiku-api` name provider `claude-api` with the same model ids. A chain lists one
  first in a preference list, such as `build.standard = [["opus-api", "opus"]]`. With
  every credit lane closed, the scheduler takes the next model in the list. Changing
  the live policy is Max's call: the hub files it with `cos add-decision` once the lane
  works. Until then a job opts in with `-m opus-api`.

## The harness

`python -I -m subfleet.api_agent` runs as the attempt's provider process. The guardian
owns it like any other provider. Adapters build its `Launch` and never spawn it.

- **Client.** It calls the official `anthropic` Python SDK, pinned, with hashes in
  `uv.lock`. Subfleet has no runtime dependencies today (`dependencies = []`), so the
  SDK goes in an optional `api` extra that the release venv installs. Only the harness
  imports it; the daemon never does.
- **Requests.** Streaming. Adaptive thinking with the job's effort; the build takes the
  exact fields from the claude-api skill's Python docs, not from memory.
  - The system prompt is Subfleet's own text. It never contains either identity line,
    "Claude Code" or "Agent SDK". The job's prompt goes in the first user turn.
  - Prompt caching on the system prompt and the tool definitions.
- **Tools.** The client tools `bash_20250124` (name `bash`) and `text_editor_20250728`
  (name `str_replace_based_edit_tool`), and nothing else in phase 1.
- **Tool execution:**
  - Commands run under a macOS seatbelt profile. Writes reach only the worktree, the
    attempt folder and a private TMPDIR, and network follows `job.network`. Two
    candidates: `codex sandbox` (codex-cli 0.159.0, "Run commands within a
    Codex-provided sandbox", which reuses Codex's tested profile but ties this lane to
    the TRUST-pinned Codex binary), or a Subfleet-owned `sandbox-exec` profile. The
    build tests both against the same matrix and picks one.
  - Every command first goes through the TRUST-pinned never-rules hook
    (`~/.subfleet/guard/never-rules-hook.sh`), with the input shape Codex's PreToolUse
    hook gets. A deny becomes the tool result. Writable reviews add the publish hook,
    as C-12.3 requires for Codex.
  - The editor resolves every path with realpath and refuses anything outside the
    worktree or the attempt folder.
  - Commands get the allowlisted launch environment with no credential variables. The
    harness reads the key once at start, removes it from `os.environ` before any tool
    runs, and never writes it anywhere.
- **History.** Append-only: Opus 5.5 keeps thinking blocks valid only in an unedited
  conversation. Long attempts use server-side compaction (beta `compact-2026-01-12`)
  and append each full `response.content`. The history persists as `messages.jsonl` in
  the attempt folder, so a resume can continue from it.
- **Limits.** Each attempt has a cap on turns and on dollars, priced from the token
  counts each response reports. The attempt's wall-clock limit applies as usual.
- **Records.**
  - `stream.jsonl` holds one event per request: request id, the served `model`, usage
    (input, output, cache read and write), stop reason, tool calls with exit codes, and
    errors with status and type. It never holds the key or any request header.
  - `last.md` holds the final assistant text.
  - Outcome evidence records the lane, which says whose credit paid.
- **Attestation.** The `model` field of every response is the served model. A response
  from any other model fails attestation.
- **Retries.** The SDK retries 408, 409, 429 and 5xx. A credit 400 ends the attempt as
  described under exhaustion; the daemon routes the job's next attempt.

## Invariants

The PR states these. Property tests (Hypothesis) cover the first five; the containment
test covers the last.

1. **Credit keys only.** For any roster and any daemon environment, including one that
   sets `ANTHROPIC_API_KEY`:
   - every `claude-api` launch carries exactly the key registered for its lane's email;
   - no other launch carries `ANTHROPIC_API_KEY`;
   - no `claude-api` launch runs the `claude` binary.
2. **No identity prompt.** For any job prompt and any effort, no request's `system`
   field contains either identity line, "Claude Code" or "Agent SDK" after case
   folding and whitespace normalization.
3. **Soonest expiry first.** For any set of credit lanes (renewal days, statuses,
   in-flight counts, closures), the chosen lane has the earliest expiry among
   admissible lanes, with unknown renewals last. It agrees with `claude_credits.order`.
4. **Fallback only when the credit is gone.** For a chain `[X-api, X]`, the scheduler
   picks `X` only when no credit lane is admissible, and always picks `X` when a credit
   closure holds every credit lane and `X` has an admissible lane.
5. **Closure length.** A credit closure ends at the next renewal strictly after the
   day the harness saw the exhaustion, or one day later when the renewal is unknown or
   falls that day.
   It agrees with `claude_credits.exhausted_until`.
6. **The key stays in the harness.** A run against a fake API asserts the key appears
   in none of the following:
   - tool subprocess environments;
   - `stream.jsonl`, `last.md`, stdout, stderr or `messages.jsonl`;
   - attempt notes or the store.

   Like `claude-credits`' own test, it checks the raw key and every 12-character run.

## Contract

The build adds section 31, "API credit lanes", to `docs/acceptance-contract.md`: the
provider, credential resolution, spending order, exhaustion closure, the C-11.4
exemption, the harness and its records. It merges contract conflicts with the hub's
`merge_contract.py`.

## Rollout

1. **Build** on a lane pinned to Opus, since it handles credentials and process
   isolation, on `feat/credit-lane-217`, as a draft PR against `release/217`.
2. **Review** on an independent Opus lane, then merge on the usual gates.
3. **Install** in the next release slot, with a daemon restart through the install kit.
   The hub coordinates the slot with Fleet ops while the r2 release runs (to 11/07).
4. **Enroll** the registry's personal plans that have keys.
5. **Smoke test:**
   - a read-only Haiku job pinned to `haiku-api`, which should land on the
     soonest-expiring lane;
   - a writable Sonnet job in a scratch repository;
   - one forced exhaustion against a fake API in CI.
6. **First work:** Axiom encoding (with the Axiom engineering hub) and Thesis forecasts
   pin `-m opus-api` or `-m sonnet-api`.
7. **Policy:** `cos add-decision` for Max to put the credit aliases first in the
   chains.

## Timing

About $400 expires on 11/05–11/06, before this can ship. Work that already calls the
plain Messages API can spend it today by running under `claude-credits exec`. About
$2,400 more expires by 11/20, so a lane working by about 11/09 catches most of it.

## Open for the build

- Which sandbox to use, from the test matrix above.
- Whether a conversation's history replays on another organization's key. If it
  doesn't, a resume on another lane restarts from the worktree state.
- A per-lane slot cap for `claude-api`, since unmeasured lanes fall under
  `max_in_flight_unmeasured`.
- How `subfleet lanes` and `subfleet status` show credit expiry and closures.
