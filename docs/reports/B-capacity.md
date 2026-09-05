<!-- Lane report B-capacity · run 20260905-063311-sfplan-b-capacity-astra · GPT-6 Astra (~/.codex-4), 44m57s · read-only lane dispatched by the planning session 2026-09-05; verbatim -->

## 1. Capacity signals, freshness, confidence, and consumers

**The Claude percentages are uncapped ratios against historical local token totals. They are not measurements of subscription utilization.** Unknown utilization is deliberately eligible for dispatch, but several pickers apply different rules to it. (`subfleet/capacity.py:466`, `subfleet/capacity.py:1062`, `subfleet/capacity.py:1197`.)

I audited `master` at `57a851a`, read the requested code, tests, and state, and reproduced the quoted percentages using the ledger at **2026-09-05 06:00 EDT**. The initial snapshot read was generated at **06:33:29**. External writers subsequently advanced the files during this audit, including a 07:03:44 history sample. No files were changed, provider requests made, tests run, or agents dispatched.

Paths below are relative to `~/chief-of-staff/subfleet`, except:

- **S** = `~/chief-of-staff/state/subfleet`
- **D** = `~/.local/state/delegate`

**The main capacity path**

`report()` calls `collect()`. It obtains cached/live provider rows, reads the usage ledger, reapplies current Codex gates, constructs Claude lane estimates, adds unfinished-run counts, and produces family summaries. The live cache contains provider probe results, not the completed Claude lane estimates. Its default TTL is 120 seconds; reuse also requires an unchanged enrollment fingerprint and cached secret-availability data. (`subfleet/capacity.py:915`, `subfleet/capacity.py:1398`, `subfleet/capacity.py:1432`.)

| Provider and signal | What it means | Freshness and actual label | Consumers and evidence |
|---|---|---|---|
| Codex: wham usage | Server-reported utilization, window duration/reset, `allowed`, `limit_reached`, plan, reset-credit counts | Probe timestamp; capacity cache normally ≤120 seconds. Numeric windows are `live`. The row itself is labeled `live` even when its probe failed | Capacity rows, snapshot, pickers, reset policy, watchdog. `subfleet/codex.py:252`; `subfleet/capacity.py:705`, `subfleet/capacity.py:715`, `subfleet/capacity.py:787` |
| Codex: rollout `rate_limits` | Previously received provider quota telemetry in rollout JSONL | Latest scanned observation, labeled `observed` in snapshot. Snapshot picker accepts fallback only within 30 minutes and marks it `stale`; capacity’s normal live-row path does not use this positive fallback | Snapshot fallback and `cmd_pick`. `subfleet/codex.py:577`, `subfleet/codex.py:737`; `subfleet/snapshot.py:95`, `subfleet/snapshot.py:436` |
| Codex: rollout usage-limit error | Evidence a request hit a limit, sometimes with a parsed retry/reset time | Scanner defaults to 24 hours; capacity requests 12 hours. Only a future parsed reset gates dispatch. No numeric confidence label | `recent_errors`, short-window gate, errors display and watchdog. `subfleet/codex.py:762`; `subfleet/capacity.py:1409`; `subfleet/reset_policy.py:311` |
| Codex: revoked-auth observation | Historical refresh-token failure | Recent error window; distinct from the current wham result. No quota confidence label | Diagnostics and healing; it does not override a currently successful live probe. `subfleet/codex.py:577`; `subfleet/snapshot.py:74`, `subfleet/snapshot.py:434`; `subfleet/watchdog.py:99` |
| Codex: runner cooldown | Immediate local suppression while rollout/wham evidence catches up | Normally 15 minutes, account scope, retained only while future | Runner writes; capacity and snapshot picker gate. `bin/subfleet-codex:571`; `subfleet/cli.py:1278`; `subfleet/reset_policy.py:324` |
| Codex: reset credit | Server entitlement and confirmed reset action | Counts come from wham; policy checks the credit-list endpoint before consuming. Successful consume can produce `reset-confirmed` windows while wham propagates | Picker, watchdog, policy and history. `subfleet/codex.py:221`, `subfleet/codex.py:336`; `subfleet/reset_policy.py:452`, `subfleet/reset_policy.py:574` |
| Codex: historical burn estimate | Estimated future unused weekly capacity | Uses recent 6/24-hour history and reset-aware monotonic segments. This is a projection, not a dispatch quota measurement | Capacity-expiry display and watchdog alerts, not waterfall ranking. `subfleet/capacity_expiry.py:139`, `subfleet/capacity_expiry.py:177`; `subfleet/snapshot.py:202` |
| Claude desktop login: OAuth usage | Server five-hour, weekly, model-specific windows and scoped limits | Same-run probe or capacity cache. Numeric windows `live`; desktop display can also use older readings with source and `stale` metadata | Capacity, snapshot, renderer, model gates and alerts. `subfleet/claude.py:412`, `subfleet/claude.py:452`; `subfleet/capacity.py:810` |
| Claude desktop: last successful OAuth payload | Cached successful response | Original `checked_at`; no hard expiry in its reader. Freshest-source selection marks readings stale after 180 minutes | Desktop snapshot/display fallback. `subfleet/claude.py:518`, `subfleet/claude.py:564`, `subfleet/claude.py:567`; `subfleet/snapshot.py:270` |
| Claude desktop: statusline tap | Provider percentages/reset times supplied to a terminal statusline | Tap timestamp; `statusline_state()` calls ≤30 minutes fresh. Once included in `pick_live_source`, the shared stale threshold is 180 minutes | Desktop snapshot/display and derived weekly reset. `bin/subfleet-statusline:67`; `subfleet/claude.py:594`; `subfleet/snapshot.py:275` |
| Claude: transcript activity/window derivation | Approximate session spans and inferred five-hour boundary; weekly reset rolled forward from an observed reset | Activity scan defaults to 48 hours. Five-hour timing is `derived`; weekly timing is `observed` or `derived` | Desktop fallback display. It supplies timing, not remaining capacity. `subfleet/claude.py:732`, `subfleet/claude.py:749`, `subfleet/claude.py:778`; `subfleet/snapshot.py:286` |
| Claude: transcript limit-error scan | Session/weekly/usage-limit messages and parsed resets | Snapshot scans 24 hours, then selects a future reset. A current OAuth five-hour reading below 95% clears the inferred active limit | Desktop verdict, errors and alerts. This scan is not bound to a lane email. `subfleet/claude.py:648`, `subfleet/claude.py:722`; `subfleet/snapshot.py:300` |
| Claude setup-token lanes: transcript usage ledger | Completed-attempt input, cache and output token totals | Recomputed over trailing five hours/seven days, using the ledger record’s completion timestamp. No denominator means percentage `null`, confidence `estimated` | Claude capacity rows, rankings and keepalive activity suppression. `subfleet/capacity.py:357`, `subfleet/capacity.py:421`, `subfleet/capacity.py:619`, `subfleet/capacity.py:1145` |
| Claude setup-token lanes: learned denominator | Largest historical account-wide hard-limit token sum for each window | **No expiry, reset boundary, model/plan epoch, or age restriction.** Resulting ratios are labeled `observed` | Percentage calculation and exhaustion gate. `subfleet/capacity.py:466`, `subfleet/capacity.py:1145` |
| Claude setup-token lanes: hard-limit observation | A runner-classified rc=4, with model, rolling totals and reset | Future explicit reset, otherwise one-hour fallback. New model-scoped observations gate that model and do not calibrate account capacity | Capacity model states, CLI and delegate. `subfleet/capacity.py:647`, `subfleet/capacity.py:1042`, `subfleet/capacity.py:1154` |
| Claude: persisted cooldown | Account or normalized-model suppression | Future timestamps only when collected. rc=4 uses reset/fallback; rc=5 sets a 30-day account cooldown | Capacity, Claude pickers and delegate. `subfleet/capacity.py:545`, `subfleet/capacity.py:577`, `subfleet/capacity.py:647`, `subfleet/capacity.py:991` |
| Claude: keepalive marker | A successful short Haiku request | Marker implies an “open” window ending exactly marker time + five hours, labeled `observed`. It contributes no token count | Five-hour reset annotation; keepalive scheduling. It can overwrite even a live five-hour reset/confidence label. `subfleet/capacity.py:443`, `subfleet/capacity.py:1147`; `subfleet/keepalive.py:363` |
| Claude: keepalive auth state | Latest success/failure evidence, including run outcomes | No simple TTL; later evidence can supersede earlier failure | Suppresses keepalive requests. It is not directly imported as a capacity eligibility gate. `subfleet/keepalive.py:102`, `subfleet/keepalive.py:304`; `subfleet/snapshot.py:336` |
| Claude: delegate just-in-time usage probe | OAuth GET using the selected lane’s enrolled token | Ten-second timeout; five-minute **process-local** memo. Successful numeric result labels row `jit-probe`; any unsuccessful/non-numeric response remains unknown | Delegate’s model-aware candidate selection. `subfleet/delegate.py:423`, `subfleet/delegate.py:433`, `subfleet/delegate.py:458` |
| Both: unfinished-run count | Local concurrency/load evidence | Re-read from run metadata; on this master, unfinished records have no age or PID-liveness bound | Claude ordering and unknown-lane concurrency gate; Codex displays it but does not spread waterfall load. `subfleet/run_ledger.py:983`; `subfleet/capacity.py:1287`; `subfleet/delegate.py:475`; `subfleet/snapshot.py:477` |

There is no Claude reset-credit mechanism in the inspected paths. Codex capacity uses no Claude-style token-denominator estimate or statusline/keepalive capacity measurement. (`subfleet/capacity.py:715`, `subfleet/capacity.py:1066`; `subfleet/keepalive.py:287`.)

**Confidence and freshness are not consistently enforced.** Claude’s `observed` label can mean either a server-independent ratio or a keepalive-derived reset. `snapshot.accounts` stamps compatibility records with snapshot generation time, while `status --cached` loads a snapshot without a TTL. Codex’s picker age-checks fallback observations but accepts `verdict == "ok"` without checking the live observation’s age. (`subfleet/capacity.py:1145`, `subfleet/snapshot.py:14`, `subfleet/cli.py:62`, `subfleet/snapshot.py:434`.)

There are also identity gaps: the saved OAuth payload contains no account identity, and statusline telemetry contains a session ID but no email. Both can enter the active account’s fallback source competition. (`subfleet/snapshot.py:254`, `bin/subfleet-statusline:81`, `subfleet/snapshot.py:270`.)

## 2. Reproducing the percentages above 100%

**All four quoted percentages reproduce exactly from the existing ledger at 06:00 EDT.**

The calculation is:

```text
usage total = input_tokens
            + cache_creation_input_tokens
            + cache_read_input_tokens
            + output_tokens

window numerator = sum(completed-attempt totals within trailing window)
denominator      = maximum historical account-wide hard-limit window total
display percent  = round(100 × numerator / denominator, 2)
```

Within a transcript, the last occurrence of each `message.id` wins. The ledger then charges the entire parsed transcript at the attempt’s completion timestamp. `_ratio()` has no upper bound; the renderer rounds numeric percentages to whole numbers. ([`subfleet/capacity.py:357`](/Users/maxghenis/chief-of-staff/subfleet/subfleet/capacity.py:357), `subfleet/capacity.py:421`, `subfleet/capacity.py:466`, `subfleet/capacity.py:619`, `subfleet/capacity.py:1062`, `subfleet/render.py:298`.)

| Lane/window at 06:00 EDT | Rolling tokens | Learned denominator | Calculated percentage | Table |
|---|---:|---:|---:|---:|
| `max@axiom-foundation.org`, five hours | 94,130,053 | 7,887,474 | 1193.41% | **1193%** |
| Same lane, seven days | 392,342,026 | 7,887,474 | 4974.24% | **4974%** |
| `max@optiqal.ai`, five hours | 121,734,602 | 16,409,292 | 741.86% | **742%** |
| Same lane, seven days | 335,675,948 | 16,409,292 | 2045.65% | **2046%** |

The denominators come from **August 22**, and each record supplies the same total for both windows:

- Axiom: `7,887,474` for both, observed August 22 at 21:52:40 +02:00, reset August 23 at 00:30 +02:00. [S/lane-usage.jsonl:226](/Users/maxghenis/chief-of-staff/state/subfleet/lane-usage.jsonl:226).
- Optiqal: `16,409,292` for both, observed August 22 at 13:12:35 +02:00, reset an hour later. [S/lane-usage.jsonl:179](/Users/maxghenis/chief-of-staff/state/subfleet/lane-usage.jsonl:179).

For Axiom, the nine five-hour records sum as follows:

```text
18,346,816 + 17,119,503 + 6,978,049 + 6,066,857 + 7,732,561
+ 19,668,801 + 6,992,886 + 4,911,436 + 6,313,144
= 94,130,053

100 × 94,130,053 / 7,887,474 = 1193.411934…%
100 × 392,342,026 / 7,887,474 = 4974.24188…%
```

Those nine records are `S/lane-usage.jsonl:1526`, `:1540`, `:1543`, `:1555`, `:1568`, `:1598`, `:1604`, `:1609`, and `:1616`. The weekly computation uses the same function over seven days, including the earlier September 4 records beginning at `S/lane-usage.jsonl:1291`.

| Candidate explanation | Finding |
|---|---|
| Percentage/fraction units bug | **Not the cause of these four numbers.** They are exactly the intended `100 × tokens / historical_tokens` arithmetic. OAuth parsing also copies provider percentage values without multiplying by 100. `subfleet/capacity.py:1062`; `subfleet/claude.py:452` |
| Unbounded estimate | **Yes.** The ratio is uncapped. Headroom is separately clamped to 0–100, so a 4974% row becomes zero headroom and `exhausted`. `subfleet/capacity.py:683`, `subfleet/capacity.py:1187` |
| Stale hard-limit observation | **Yes, as a permanent denominator.** Its old reset no longer blocks the lane, but `learned_capacities()` keeps its totals forever. `subfleet/capacity.py:466`, `subfleet/capacity.py:1042` |
| Missing reset handling | **Yes, in the estimate.** Rolling sums ignore observed/provider reset boundaries. A reset can clear a cooldown while the same trailing-window numerator remains large. `subfleet/capacity.py:421`, `subfleet/capacity.py:991` |
| Invalid capacity calibration | **The central modeling error.** One hard-limit event stores both rolling totals without proving which window bound the request. It then treats those local totals as two subscription capacities. `subfleet/capacity.py:647`, `subfleet/capacity.py:466` |
| Model/token accounting mismatch | The numerator mixes models and counts cached-input, uncached-input and output tokens equally. The normal usage record does not retain model identity. No provider quota conversion is established by this code. `subfleet/capacity.py:381`, `subfleet/capacity.py:619` |
| Failure to learn from newer larger observations | New observations contain model scopes, so they are deliberately excluded from account calibration. Much larger September 4 Opus totals therefore do not replace August denominators. `subfleet/capacity.py:475`; `S/lane-usage.jsonl:1455`, `S/lane-usage.jsonl:1466` |

The tests explicitly preserve this behavior: calibrated ratios are labeled `observed`, while model-scoped hard limits neither calibrate nor globally block an account. (`tests/test_capacity.py:612`, `tests/test_capacity.py:678`.)

By the initial **06:33** snapshot, Axiom had advanced to **1306.48% / 5319.92%**, and Optiqal to **616.60% / 2045.65%**. That is ordinary ledger arrival/window aging under this formula, not evidence that the earlier table was transcribed incorrectly. (`S/snapshot.json:2378`, `S/snapshot.json:2454`; new Axiom completion at `S/lane-usage.jsonl:1621`.)

## 3. Why unknown lanes say OK, and how pickers handle them

**The initial saved snapshot contains eight enrolled `? ?` lanes, rather than seven.** Seven have no current model cooldown; the eighth, PolicyEngine, has model cooldowns that the compact lane table does not display.

| Unknown lane | Weekly ledger tokens in the 06:33 snapshot | Compact status |
|---|---:|---|
| `max@axiom.org` | 676,129,694 | OK |
| `max@farness.ai` | 271,189,705 | OK |
| `max@maxghenis.com` | 748,939,039 | OK |
| `max@policybench.org` | 725,238,738 | OK |
| `max@policyengine.org` | 190,147,564 | OK, despite model cooldowns |
| `max@rules.foundation` | 1,599,382,709 | OK |
| `max@thesisinstitute.org` | 2,878,470,270 | OK |
| `max@ubicenter.org` | 805,951,694 | OK |

Evidence: `S/snapshot.json:2395`, `:2409`, `:2440`, `:2471`, `:2485`, `:2499`, `:2513`, `:2527`. PolicyEngine’s cooldowns are in [D/cooldowns.json:36](/Users/maxghenis/.local/state/delegate/cooldowns.json:36): Fable until 07:27:59, Opus and Sonnet until 08:10.

The saved state supports “eight unknown, seven without model cooldowns.” It does not establish why the earlier description counted seven: history stores aggregate enrolled/dispatchable counts, not per-lane utilization. (`subfleet/watchdog.py:664`; `S/history.jsonl:2389`.)

**`?` means no capacity denominator, not no recorded use.** These lanes have no live lane reading and no qualifying historical account-wide calibration. Their `used_percent` remains `None`. If enrolled, the secret exists, and no global gate applies, `_claude_rows()` sets `status="ok"` and explicitly accepts `score is None` as dispatchable. The renderer prints `?` for the percentage and `OK` for that global status. (`subfleet/capacity.py:1145`, `subfleet/capacity.py:1183`, `subfleet/capacity.py:1197`; `subfleet/snapshot.py:42`; `subfleet/render.py:298`.)

The intended optimistic behavior is tested directly: “uncalibrated lane is available with null score.” (`tests/test_capacity.py:634`.)

**There is no single picker behavior.**

| Path | Treatment of unknown |
|---|---|
| Capacity family summary, `_best_dispatchable` | Removes the active desktop lane whenever any other dispatchable lane exists. Within the remaining pool, known scores beat unknown; unknowns sort by unfinished-run count, then weekly/five-hour raw tokens, then ID. Thus an unknown non-desktop lane can beat a measured desktop lane by policy. **No model gate is applied here.** `subfleet/capacity.py:1315` |
| `claude-pick`, `_capacity_lane_ranking` | Known scores sort before unknown because the first key is `score is None`. Unknown is not converted to empty usage. Known utilization plus desktop handicap sorts ascending; unknowns use unfinished count and raw tokens. This path does **not** run the delegate’s JIT probe/concurrency filter. `subfleet/cli.py:282`, `subfleet/cli.py:312`, `subfleet/cli.py:326` |
| Model-aware delegate | Applies account/model gates, then attempts an OAuth usage probe for unknown lanes. If still unknown, the lane is eligible only with **zero** unfinished runs. Sorting then prioritizes Fable-stranded capacity for non-Fable work, non-desktop lanes, known scores, headroom, unfinished count and raw tokens. `subfleet/delegate.py:458`, `subfleet/delegate.py:499`, `subfleet/delegate.py:553` |
| Older `claude.rank_lanes` | Only numeric, successful live probes qualify; unknown lanes are excluded. This is not the current capacity-backed `claude-pick` implementation. `subfleet/claude.py:270`, `subfleet/claude.py:309`; `subfleet/cli.py:326` |

The `0.0` fallback inside sorting does **not** make unknown rank first: the preceding boolean segregates unknowns after known scores. (`subfleet/cli.py:314`; `subfleet/delegate.py:578`.)

There are consequential inconsistencies:

- The family summary can name PolicyEngine “best” while its requested model is cooled. Model-aware pickers reject that model; model-blind `claude-pick` rejects a lane with **any** model cooldown. (`subfleet/capacity.py:1315`; `subfleet/cli.py:238`; `tests/test_claude_pick.py:374`.)
- Delegate’s blind filter treats **all** non-numeric probe outcomes alike, including invalid-token, throttling and HTTP errors. An idle lane survives that probe failure. (`subfleet/delegate.py:450`, `subfleet/delegate.py:474`.)
- A successful JIT probe collapses the worst-window score back into **identical five-hour and weekly percentages**, discarding the distinction between the actual windows. (`subfleet/delegate.py:488`.)
- `select_semantic_model()` accepts the first model whose state is not `False`; `None` is optimistic. An unknown lane excluded for being busy can contribute to a “no model capacity” result, although concurrency exclusion is not proof of quota exhaustion. (`subfleet/delegate.py:657`, `subfleet/delegate.py:684`.)

## 4. Codex waterfall, gates, reset credits, and scan cost

**Codex dispatch concentrates work on the eligible account whose weekly capacity expires soonest.**

```text
dispatch_score = −max(0, seconds until weekly reset)
```

A reset in one day scores `−86,400`; one in four days scores `−345,600`. Higher wins. Missing reset produces `None`. A past reset scores zero but does not itself refresh utilization or revive an exhausted account. (`subfleet/capacity.py:694`, `subfleet/capacity.py:1315`; `subfleet/delegate.py:532`.)

Snapshot ranking sorts directly by weekly reset ascending, unknown reset last, then stale flag and home. Usage determines eligibility, not waterfall preference; unfinished count and app protection are displayed metadata. Tests explicitly require earlier reset to beat lower usage and require unfinished count not to change the order. (`subfleet/snapshot.py:409`, `subfleet/snapshot.py:480`; `tests/test_pick.py:31`, `tests/test_pick.py:99`, `tests/test_pick.py:107`.)

**Window classification matters.** Wham’s primary slot can contain the weekly window. The code classifies durations of at most six hours as short and longer durations as weekly. The sampled accounts reported only weekly windows; missing five-hour data is not evidence of an unused five-hour allowance. (`subfleet/codex.py:231`, `subfleet/codex.py:238`; `S/capacity-live-cache.json:13`, `:62`, `:111`, `:160`, `:219`, `:268`.)

The initial 06:33 evidence was:

| Lane | Weekly used | Weekly reset, EDT | Dispatch consequence | Available/applicable credits |
|---|---:|---|---|---:|
| `.codex-1` | 100% | Sep 11, 17:39 | Server limited | 1 / 1 |
| `.codex-2` | 97% | Sep 8, 20:47 | Only 3% headroom, below local floor | 2 / 0 |
| `.codex-3` | 97% | Sep 7, 07:04 | Only 3% headroom, below local floor | 2 / 0 |
| `.codex-4` | 5% | Sep 12, 06:18 | **Only eligible lane** | 1 / 0 |
| `.codex-5` | 100% | Sep 6, 22:28 | Server limited | 2 / 2 |
| `.codex-6` | 100% | Sep 10, 22:50 | Server limited | 1 / 1 |

Evidence: `S/history.jsonl:2390`; reset/credit fields at `S/capacity-live-cache.json:6`, `:55`, `:104`, `:153`, `:212`, `:261`. The later 07:03 history sample has `.codex-4` at 13%, with the same eligibility outcome. (`S/history.jsonl:2391`.)

This is **three lanes with applicable credits, totaling four applicable credits**, versus nine available entitlements across all six accounts.

The gate sequence excludes duplicate accounts, missing/bad authentication, free plans, server rejection, and known headroom below 5%. Future short-window rollout limits or local cooldowns can exclude an otherwise healthy weekly account. Exactly 5% headroom passes the current capacity floor. (`subfleet/capacity.py:715`, `subfleet/capacity.py:1021`; `subfleet/snapshot.py:425`; `tests/test_capacity.py:433`.)

A past rollout auth-revocation observation does not veto a current successful wham response. Conversely, a failed live probe can use recent rollout utilization in the snapshot picker, but not in the capacity-backed delegate’s positive eligibility path. (`subfleet/snapshot.py:95`, `subfleet/snapshot.py:434`; `subfleet/capacity.py:715`.)

**Reset redemption reverses the ordering objective.** Spending uses the nearest reset first; redeeming uses the **furthest reset first**. Policy triggers when no lane is dispatchable or aggregate eligible weekly headroom falls below 15%, with a default 30-minute minimum interval. Candidates must be server-limited and hold applicable credits. App-shadowed candidates are deferred when an unshadowed concrete credit exists. (`subfleet/reset_policy.py:23`, `subfleet/reset_policy.py:149`, `subfleet/reset_policy.py:193`, `subfleet/reset_policy.py:568`.)

For the sampled limited lanes, the reset candidate order is `.codex-1`, `.codex-6`, `.codex-5`. With `.codex-4` holding 95% headroom, the default policy is not triggered. These are deductions from the sampled table and `subfleet/reset_policy.py:149`, `subfleet/reset_policy.py:209`.

Consumption is serialized, preceded by a credit-list GET, and accepted only for the expected successful consume response. The policy persists the reset before polling wham, clears the lane cooldown, and ignores rollout limit observations predating that redemption. If propagation lags, it can synthesize zero-used windows labeled `reset-confirmed`, including a guessed five-hour window. (`subfleet/codex.py:422`; `subfleet/reset_policy.py:269`, `subfleet/reset_policy.py:311`, `subfleet/reset_policy.py:452`, `subfleet/reset_policy.py:622`.)

**The September 2 stat storm and fix**

On the audited master:

1. `_recent_rollouts()` globs every rollout and calls `stat()` before filtering by modification age. (`subfleet/codex.py:512`.)
2. The content cache saves parsing work, but discovery still happens on every scan. New/truncated files are grepped in batches of 50; grown files use incremental reads. Cache pruning can call `exists()` on old paths. (`subfleet/codex.py:617`, `subfleet/codex.py:627`, `subfleet/codex.py:649`.)
3. A capacity-cache hit still rescans recent errors. A miss can scan them once while constructing Codex rows and again during `collect()`. (`subfleet/capacity.py:773`, `subfleet/capacity.py:1409`.)

The historical fix is commit **`57e46d8`**, which records:

| Historical measurement | Recorded count |
|---|---:|
| One dispatch, total stat calls | 72,799 |
| Rollout discovery, twice across roughly 30,000 files | 60,930 |
| Scan-cache prune `exists()` | 5,030 |
| Transcript resolution | 3,019 |
| Run-ledger pruning | 2,433 |
| Warm dispatch after fix | 612 stats, 0.19 seconds |

These are the commit’s incident measurements, not timings rerun during this audit.

The fix adds filename/date pruning with 48-hour slack, explicitly includes recently resumed rollouts, memoizes each home’s sweep for 60 seconds, uses a bounded five-second lock with stale-memo fallback, and removes filesystem existence checks from age-based cache pruning. Historical evidence: `57e46d8:subfleet/codex.py:515`, `:533`, `:742`, `:805`, `:844`, `:953`. These historical paths refer to the package file beneath the audited subdirectory.

**That fix is not on audited master.** `git branch --all --contains 57e46d8` showed only `claude/nifty-mahavira-661962` and `claude/quirky-sutherland-6b88de`. The master implementation remains the un-memoized scanner at `subfleet/codex.py:649`. The rebuild should not assume the incident fix is already deployed here.

## 5. State files and locks touched by the capacity path

The inventory below covers capacity decisions, their signal producers, reset/healing operations, and monitoring outputs. Provider credentials are read through their existing stores; I did not inspect or reproduce secret values. Default path definitions are in `subfleet/paths.py:47` and `subfleet/paths.py:91`.

| File/resource | Reader | Writer and locking |
|---|---|---|
| `S/capacity-live-cache.json` | Capacity; Claude desktop fallback | `_cache_rows()`, atomic replacement, no serialization lock. `subfleet/capacity.py:915`; `subfleet/claude.py:531` |
| `S/lane-usage.jsonl` | Capacity estimates/gates; keepalive | Runner accounting and keepalive append; no ledger lock. `subfleet/capacity.py:326`, `:344`, `:619`; `subfleet/keepalive.py:363` |
| `D/cooldowns.json` | Capacity, delegate, Codex short-window policy | Main `store_lane_cooldown`/clear use **`D/cooldowns.json.lock`**, exclusive flock and atomic replacement. Legacy delegate `_save_cooldowns()` bypasses that lock and writes directly. `subfleet/capacity.py:577`, `:597`; `subfleet/reset_policy.py:324`; `subfleet/delegate.py:183` |
| `S/rollout-scan-cache.json` | Codex scanner | Scanner atomically replaces it; no whole-scan lock on master. `subfleet/codex.py:649` |
| `S/snapshot.json` | Cached CLI/status, indirectly legacy desktop lookup | Watchdog atomically replaces it. `subfleet/cli.py:62`; `subfleet/delegate.py:215`; `subfleet/watchdog.py:662` |
| `S/claude-oauth-raw.json` | Last-success desktop fallback | Successful `snapshot.build()` OAuth probe, atomic replacement. `subfleet/claude.py:518`; `subfleet/snapshot.py:249` |
| `S/claude-statusline.json` | Claude statusline reader | Statusline tap, temporary file then replace, no lock. `subfleet/claude.py:594`; `bin/subfleet-statusline:67` |
| `S/claude-statusline-history.jsonl` | Diagnostic history; no capacity decision reader in inspected path | Statusline appends when usage changes sufficiently, no lock. `bin/subfleet-statusline:93` |
| `S/claude-statusline-invoked.json` | Tap’s own freshness/throttle check | Statusline invocation marker, throttled to roughly once/minute, temporary replacement. `bin/subfleet-statusline:39` |
| `S/keepalive.json` | Keepalive, snapshot, cached CLI overlay | Keepalive run and auth-clear operation use **`S/keepalive.json.lock`**, exclusive flock. `subfleet/keepalive.py:275`, `:429`, `:456`; `subfleet/snapshot.py:336`; `subfleet/cli.py:62` |
| `S/reset-policy.json` | Reset evaluator; per-lane reset suppression of old errors | Redemption under **`S/reset-policy.json.lock`**, exclusive flock, atomic replacement. `subfleet/reset_policy.py:269`, `:282`, `:302` |
| `S/history.jsonl` | Capacity-expiry projection | Watchdog appends without flock; reset events append with flock **on the history file itself**. `subfleet/capacity_expiry.py:53`; `subfleet/watchdog.py:694`; `subfleet/reset_policy.py:334` |
| `S/runs/<run>/meta.json` | Unfinished counts, keepalive activity/auth evidence, reset event handling | Run ledger start/update/finish/event operations; writers use **`S/runs/.lock`** and atomic metadata replacement. Count readers are unlocked. `subfleet/run_ledger.py:55`, `:84`, `:249`, `:358`, `:983`; `subfleet/keepalive.py:56` |
| `S/refresh-probes.json` | Watchdog auth-heal suppression | Watchdog records attempts/results and revoked-token latch; atomic state writes. `subfleet/watchdog.py:99` |
| `S/alerts.json` | Watchdog alert deduplication/recovery | Watchdog writes alert state. `subfleet/watchdog.py:700`, `:742` |
| `S/brief.md` | Human monitoring output | Watchdog renders/writes it. `subfleet/watchdog.py:697` |
| `D/rotation.json` | Legacy optimistic Fable picker | Same picker writes last-used email directly, no lock. The current `main()` routes through capacity selection instead. `subfleet/delegate.py:190`, `:231`, `:850` |
| `D/decisions.jsonl` | Delegate diagnostics | Delegate appends routing decisions, including a capacity view, without a lock. `subfleet/delegate.py:279`, `:968` |
| `claude-accounts.json`, `codex-accounts.json` | Roster/enrollment, secret-service references, account protection and reset settings | Configuration inputs; Claude enrollment command writes its roster. `subfleet/claude.py:54`; `subfleet/codex.py:137`; `subfleet/reset_policy.py:39`; `subfleet/cli.py:468` |
| `~/.claude.json` | Active desktop identity | Provider-owned; audited capacity reads it. `subfleet/claude.py:43` |
| `~/.claude/cc-mirror-accounts.json` | Additional known Claude identities | Mirror-owned roster input. `subfleet/claude.py:64` |
| `~/.claude/cc-mirror-state.json` | Snapshot mirror-health check | Mirror-owned heartbeat, inspected via mtime; not quota evidence. `subfleet/paths.py:79`; `subfleet/claude.py:147` |
| `~/.codex-N/auth.json`, app `~/.codex/auth.json` | Lane identity, request authentication, app-shadow detection | Provider-owned. Probe code reads; separate sanctioned CLI refresh can update provider auth. `subfleet/codex.py:59`, `:137`, `:483` |
| `CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` | Codex usage/error scanner | Codex CLI writes. `subfleet/codex.py:512`, `:577` |
| `~/.claude/projects/<project>/<session>.jsonl`, or resolved workdir transcript | Completed-run token parser; active error/activity scans | Claude CLI writes. `subfleet/capacity.py:357`, `:489`; `subfleet/claude.py:630`, `:749` |
| Output-stem `.result.json` and `.err.log` | Synchronous runner accounting hook reads their tails for limit/error/reset text | Claude runner writes them and calls accounting before retry overwrites. `bin/subfleet-claude:228`, `:578`; `subfleet/cli.py:546` |
| macOS `Claude Code-credentials`; `agent-secret` services | Active OAuth probe; lane-secret presence and JIT inference-token retrieval | External credential stores. No direct filesystem database path is assumed here. `subfleet/claude.py:76`, `:384`; `subfleet/capacity.py:905` |

Atomic JSON writes also create temporary sibling files named from the target before replacement. Atomic replacement prevents partial JSON, but does not serialize competing writers. (`subfleet/util.py:95`.)

The September 2 branch adds **`S/rollout-scan-memo.json`** and **`S/rollout-scan.lock`**. They belong to that historical fix, not this master capacity path. (`57e46d8:subfleet/paths.py:124`; `57e46d8:subfleet/codex.py:844`.)

## 6. What ground truth exists per provider today

| Provider/authentication | Verified ground truth | Limits of that verification |
|---|---|---|
| Codex subscription account | `GET https://chatgpt.com/backend-api/wham/usage`, using the lane’s bearer credential and account ID, returns actual quota windows and server admission flags. Saved responses demonstrate working utilization and reset-credit counts | This is the integration implemented and evidenced locally, not a claim of a public stable API contract. Additional rate-limit records are parsed but not comprehensively incorporated into capacity gating. `subfleet/codex.py:252`, `:303`; `subfleet/capacity.py:715`; `S/capacity-live-cache.json:6` |
| Active Claude desktop login | `GET https://api.anthropic.com/api/oauth/usage` with bearer auth and `anthropic-beta: oauth-2025-04-20`. Saved successful payload initially reported 46% five-hour, 24% weekly and scoped data | Raw fallback lacks identity binding. Parsing supports known window/scoped structures, not an arbitrary future schema. `subfleet/claude.py:412`, `:452`; `subfleet/snapshot.py:254`; `S/claude-oauth-raw.json:1` |
| Claude setup token | Inference success and actual model rejection are established signals. The shared session verified usage-endpoint 403s; repository comments and runner handling explicitly distinguish setup-token inference from usage-endpoint access | **No working standalone setup-token quota endpoint was verified in this audit.** The existing beta header is already sent. A 403 from this usage endpoint does not by itself establish that inference is unavailable. `subfleet/snapshot.py:264`; `subfleet/claude.py:412`; `bin/subfleet-claude:492` |
| Claude CLI JSON result | Token usage and cost/result metadata exist in the supported JSON/SDK result interface | Token accounting is not subscription headroom. I did not verify five-hour/seven-day remaining-capacity fields in this installation’s final `--output-format json` result. Local runner consumes `result`/error success fields, not a quota object. `bin/subfleet-claude:807`; [official headless documentation](https://code.claude.com/docs/en/headless) |
| Claude SDK/CLI streamed rate-limit events | Official SDK definitions expose rate-limit status, reset time, window type and utilization | **Verified interface, unverified emission for these setup-token lanes and their installed CLI version.** This is a concrete investigation target, not grounds to populate their current table. [Official SDK types, `ResultMessage` and `RateLimitInfo`](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py#L1200), [official TypeScript rate-limit event reference](https://code.claude.com/docs/en/agent-sdk/typescript#sdkratelimitevent) |
| Claude statusline | Official statusline fields include subscription used percentages and reset timestamps after a response | This is a terminal statusline integration, not a verified headless setup-token polling endpoint. The local tap’s account identity is also missing. `subfleet/claude.py:594`; `bin/subfleet-statusline:81`; [official statusline documentation](https://code.claude.com/docs/en/statusline) |

Official authentication documentation describes setup tokens as credentials for model requests. That supports the capability distinction, but does not prove that no future or undocumented telemetry interface exists. I found **no verified response-header method for these setup-token lanes** in the inspected implementation or official material. The current probe code does not capture response headers as quota evidence. (`subfleet/claude.py:412`; [official authentication documentation](https://code.claude.com/docs/en/authentication).)

A relevant units distinction for a rebuild: the official SDK’s `RateLimitInfo.utilization` is documented as a **0–1 fraction**. The existing OAuth parser treats its `utilization` fallback as an already scaled percentage. These are different source schemas and must receive different adapters. This does not explain this morning’s ledger-derived ratios. (`subfleet/claude.py:452`; [official SDK rate-limit types](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py#L1253).)

**What the current runner actually extracts**

- It runs Claude with `--output-format json`, checks success/error/result content and records each completed child attempt synchronously. (`bin/subfleet-claude:578`, `bin/subfleet-claude:807`.)
- Accounting resolves the transcript and sums per-message usage, deduplicated by message ID. It does not derive subscription utilization from a provider field. (`subfleet/capacity.py:357`, `subfleet/capacity.py:489`, `subfleet/capacity.py:619`.)
- Error/result tails supply textual reset hints. Hard-limit phrases produce rc=4; authentication classification produces rc=5; hard limits can trigger immediate repicking in automatic mode. (`subfleet/cli.py:546`; `bin/subfleet-claude:842`, `bin/subfleet-claude:861`.)
- The reset parser expects clock forms such as `9:00pm`; unsupported forms can fall back to one hour. A recent event can therefore yield a locally guessed cooldown. (`subfleet/util.py:66`; `subfleet/capacity.py:529`, `subfleet/capacity.py:647`.)

The honest setup-token statement today is: **we can observe successful work, token accounting, and failures; numerical remaining quota is unknown unless a verified provider telemetry source supplies it.**

## 7. Rebuild recommendation: the minimal honest capacity model

**Use one eligibility engine over timestamped provider observations and local execution state. Keep quota, admission, and concurrency separate.**

The existing code already demonstrates why: one field called `observed` covers incompatible evidence, a global OK can hide model rejection, and different pickers select different lanes from the same rows. (`subfleet/capacity.py:1145`, `subfleet/capacity.py:1237`, `subfleet/capacity.py:1315`, `subfleet/cli.py:312`, `subfleet/delegate.py:574`.)

| Store | Minimal content |
|---|---|
| Lane identity | Provider, stable account identity, credential reference/epoch, enrollment, desktop-reservation policy |
| Provider quota observation | Account, model/surface scope, bucket ID, native utilization and units, normalized percentage, duration/reset if reported, observation time, source and expiry |
| Probe capability/result | Endpoint supported/unsupported/unknown for this credential type; last attempt, response category and next permitted attempt |
| Admission observation | Requested model, success or typed rejection, observed time, provider reset if supplied; distinguish quota rejection, authentication failure and temporary throttling |
| Local backoff | Scope, reason, expiry, source event; explicitly mark guessed expiry |
| Execution lease | Run/attempt ID, lane/model, start, heartbeat/expiry and terminal result; atomically reserve unknown-lane concurrency |
| Reset event | Confirmed consume, affected account/windows, timestamp and propagation status |
| Optional usage analytics | Idempotent per-attempt/message token components, kept separate from subscription quota |

This replaces the current permanent calibration, completion-time rolling approximation, and unbounded unfinished-record counting as dispatch evidence. (`subfleet/capacity.py:421`, `subfleet/capacity.py:466`; `subfleet/run_ledger.py:983`.)

**Probe strategy**

| Provider | Proposed behavior |
|---|---|
| Codex | Probe wham per distinct account with a small TTL and shared refresh coordination. Preserve missing windows as missing. Consume credits only under the existing one-at-a-time, furthest-reset-first policy; a confirmed reset remains an event until actual windows are observed |
| Claude desktop login | Probe OAuth usage with explicit account/credential binding. Keep model/surface buckets and their own timestamps |
| Claude setup-token lanes | Record usage-endpoint 403 as an unsupported quota-probe capability, so every new dispatcher does not repeat the same failed request. Validate streamed SDK rate-limit events against the actual installed CLI and setup tokens. Until validated, keep remaining quota unknown |
| All lanes | Ingest request outcomes immediately. Maintain precise model/account scope and replace textual parsing with structured provider events where verified |

These changes address the existing repeated JIT GET, synthetic duplicated windows, unbound fallback payload, and reset-confirmed guessed windows. (`subfleet/delegate.py:433`, `subfleet/delegate.py:488`; `subfleet/snapshot.py:254`; `subfleet/reset_policy.py:479`.)

**Use both probe-then-dispatch and optimistic dispatch with fast failure.**

For expensive work, stale authentication, or recovery after a limit, perform a small request using the **actual requested model** when numerical probing is unavailable. A Haiku success should establish Haiku admission, not Opus headroom. For ordinary work on a recently successful unknown lane, let the task’s first provider request serve as its admission probe, with prompt rejection handling and immediate scoped rerouting. Current keepalive proves only its Haiku request; current runners already have the basic hard-limit rerouting mechanism. (`subfleet/keepalive.py:171`; `bin/subfleet-claude:861`.)

Proposed ranking:

1. Apply explicit account, requested-model and surface gates, plus valid local backoffs.
2. Apply the user’s desktop-reservation policy consistently.
3. Among fresh measured eligible lanes, preserve Codex’s weekly-expiry waterfall; use actual relevant headroom for Claude.
4. Among unknown eligible lanes, use available execution leases, recent successful admission and fair rotation. Start conservatively with one concurrent run per unknown lane.
5. Treat unknown as **eligible but unmeasured**. Never assign it zero utilization or unlimited capacity.

The existing blind-lane concurrency check is a useful starting policy, but it needs an atomic lease rather than an unlocked count of indefinitely unfinished records. (`subfleet/delegate.py:475`; `subfleet/run_ledger.py:983`.)

**Confidence labels should describe evidence, not optimism.**

| Label | Meaning |
|---|---|
| `provider` | Utilization/reset explicitly reported by the provider, with source and age |
| `stale-provider` | Same evidence beyond its freshness horizon |
| `admission-observed` | This model recently succeeded or was rejected; numerical quota remains unknown |
| `local-backoff` | A local routing decision, with explicit expiry and reason |
| `derived` | Forecast/timing inference for display only |
| `unknown` | No verified numerical capacity measurement |

Display separate fields for **quota**, **eligibility**, **model restrictions**, **age**, and **busy state**. “Unknown quota, eligible to try” is accurate; “OK” alone currently conflates those questions. (`subfleet/render.py:298`; `subfleet/snapshot.py:42`.)

**Drop the following from capacity decisions:**

| Drop | Reason |
|---|---|
| Learned token-capacity denominators and their percentages | They reproduce the false 1193–4974% readings and have no validated quota conversion. `subfleet/capacity.py:466`, `:1062` |
| Raw transcript-token totals as a headroom ranking proxy | Mixed models, cache components and completion-time windows are not comparable remaining subscription allowances. `subfleet/capacity.py:381`, `:421`, `:1308` |
| Keepalive-derived exact quota resets | A successful request timestamp is weaker evidence than a server reset. `subfleet/capacity.py:443`, `:1147` |
| Global, account-unbound statusline/transcript inference as authoritative capacity | The current fallback can mix identities and session evidence. `subfleet/snapshot.py:270`; `subfleet/claude.py:648` |
| Multiple independent ranking implementations | They disagree about desktop priority, model cooldowns, probing and concurrency. `subfleet/capacity.py:1315`; `subfleet/cli.py:312`; `subfleet/delegate.py:574` |
| Broad filesystem discovery on the dispatch critical path | Master still pays the rollout discovery cost despite content caching. Adopt the existing memo fix as an interim repair; ingest telemetry incrementally in the rebuild. `subfleet/codex.py:512`, `:649`; `57e46d8:subfleet/codex.py:844` |
| Fabricated numeric windows after partial evidence | JIT headroom is copied into both windows; reset confirmation can manufacture a five-hour window. Preserve the actual observation and its uncertainty instead. `subfleet/delegate.py:488`; `subfleet/reset_policy.py:479` |

Keep token ledgers for accounting, keepalive markers for activity, history for projections, and failure events for routing. **Only verified provider quota telemetry should populate a percentage gauge.**