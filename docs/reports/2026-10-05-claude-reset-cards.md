# Claude limit-reset cards and promotional credits (C-9.10)

2026-10-05. Question: does any endpoint Claude Code or the desktop app reads now expose Claude
subscription limit-reset cards and promotional credit balances, so Subfleet can track them as it
tracks Codex `rate_limit_reset_credits`? Answer: yes, with two conditions, a full Claude Code login
(scope `user:profile`) and Claude Code's own User-Agent. Nothing was redeemed or claimed. Every read
was a GET, and the only other requests were the heal turns described under "The census".

Evidence lives in `~/reviews/claude-reset-cards-2026-10-05/`:

- `live/` holds the first reads, and `census/` the fleet census and its raw bodies.
- `tools/` holds the scripts that made them.
- `claude-ai-cache/` holds decoded claude.ai responses and frontend chunks from the desktop app's HTTP cache.

The sources for the cards themselves are help article 17007452 ("Reset for free" under Settings,
Usage, on the web or desktop; an unused card is lost if the account cancels or downgrades first) and
help article 17152539 (the cloud-session credit: $250 on Max, claim by Oct 7, expires Nov 4).

## Where Claude Code reads them (2.1.286 bundle)

Byte offsets are into `~/Library/Application Support/Claude/claude-code/2.1.286/f2326db61802/claude.app/Contents/MacOS/claude`.

- `183037790`: the usage reads.
  - The variants are `plain: /api/oauth/usage`, `at_wall: /api/oauth/usage?at_wall=1&skip_spend=1`
    and `cedar_ember: /api/oauth/usage?cedar_ember=1&skip_spend=1`.
  - The CLI sends them only when its login's scopes include `$3`, which is `"user:profile"`
    (`Ep()` at `179329490`, the constant at `176232332`).
- `203412000`: `cedar_ember`, the limit-reset cards.
  - The status is `{eligible, ineligible_reason, at_limit, exhausted, grants, next_grant_id,
    weekly_resets_at, cooldown_until, event_props}`.
  - Each grant is `{id, label, resets_total, resets_left, starts_at, ends_at, clears, paused, usable_now,
    use_requires_limit, percent_used, blocking}`.
  - `ineligible_reason` is one of `config_off, tier, seat, mobile, surface, cli_version, no_grant,
    tenure, other_experiment, unavailable, unknown`.
  - The claim is `POST /api/organizations/{org}/reset_rate_limits` with
    `{program: "cedar_ember", grant_id, request_id}`. Subfleet never calls it.
- `203436000`: `juniper_tide`, a separate weekly session-limit reset experiment ("once a week, still
  counts toward your weekly limit"). It is claimed through the same POST with
  `program: "juniper_tide"`. It was null on every account read here, so the sensor does not track it.
- `183041985` and `196358405`: the `/claim-credit` status check.
  - It is a GET to the endpoint named in the remote config `tengu_swift_lynx`, with
    `auth: "teleport-org"`.
  - That adds `Authorization`, `anthropic-version: 2023-06-01`, `anthropic-client-platform` and
    `x-organization-uuid` (`179089753`, `yBt`).
  - The answer is `{eligible, claimed, state}`. The CLI lower-cases the state, strips a `*_state_`
    prefix and keeps only `not_claimed, pending, active, expired, claimed_elsewhere` (`jse`).
  - It offers a claim when `eligible && !claimed`.
  - The CLI's cached remote config in each login's `.claude.json` names the endpoint
    `/v1/code/promo/cloud_credit`.
- `178778149`: the User-Agent, `claude-cli/<version> (external, <entrypoint>)`.

Claude Code itself never reads the dollar blocks of the usage payload: `iguana`, `necktie` (outside an
emoji table) and `_dollars` do not occur in the bundle. The claude.ai frontend does read them. In its
2026-10-03 build (`claude-ai-cache/cd58f8e42-*`, function `Pt`), `iguana_necktie` is the cloud-session
credit meter, read only once a claim exists. After a claim, the frontend polls usage until the block
appears.

## What the endpoints returned (live, 2026-10-05 ~15:20Z)

- With a lane's setup token, the usage and profile endpoints return 403.
  - Every Claude lane in the store is a setup-token lane with no recorded identity.
  - That matches the 403 `no-scope` history in PR 120's notes.
  - The per-account logins under `~/.subfleet/logins/<email>/` carry `user:profile`.
- `GET /api/oauth/usage?cedar_ember=1&skip_spend=1`, with a full login, compared across two User-Agents:
  - With User-Agent `claude-code/2.1.286` (`live/max@maxghenis.com.cedar_ember.json`) the answer is
    `cedar_ember: {eligible: false, ineligible_reason: "surface", grants: []}`.
  - The same request with `claude-cli/2.1.286 (external, cli)`
    (`live/max@maxghenis.com.cedar_ember.cliua.json`) answers `eligible: true` with the grant
    `opus55-launch-promax-20260921`. Its label is "Claude Opus 5.5 launch: one usage-limit reset for
    Pro and Max", with `resets_total 1`, `starts_at 2026-09-22T16:00Z`, `ends_at 2026-10-22T16:00Z`, and
    it clears `five_hour`, `seven_day` and `seven_day_overage_included`.
  - The sensor's own reads with 2.1.284 got the same answer.
- The same payload carries `iguana_necktie: {limit_dollars: 250, used_dollars: 0, remaining_dollars: 250,
  resets_at: 2026-11-05T07:59Z}`.
  - `GET /v1/code/promo/cloud_credit` on the same accounts returns `{claimed: true, state: "active",
    expires_at: 2026-11-05T07:59Z}` and the claim time.
  - New code-named blocks since 2026-09-18 (`brass_thimble`, `wattle_ember`, `amber_cistern`) were null.
- `GET /api/oauth/profile` returns `organization.{organization_type, rate_limit_tier, subscription_status,
  billing_type}`.
  - Five lapsed accounts read `claude_free` and `canceled`.
  - Their usage reads answered 403 (four accounts) or 429 (max@rules.foundation).
  - The heal turns run on them printed "Your organization has disabled Claude subscription access for
    Claude Code".

## What claude.ai reads that an OAuth login cannot

The claude.ai frontend learns of a scheduled cancellation or downgrade from
`GET /api/organizations/{org}/subscription_details`. It returns `plan_ending_at`, `plan_ending_before`,
`next_charge_date`, `status` and `scheduled_downgrade`.

- Asked with a Claude Code OAuth token on api.anthropic.com, it answers 403, "This endpoint does not
  accept OAuth access tokens" (`oauth_token_not_accepted`).
- The `/api/oauth/organizations/...` spelling is 404.

So the sensor cannot see a scheduled end, and the operator declares it.

The cache shows the credit going with the plan:

- **Org `7c4b7006`.** The org is max@axiom-foundation.org.
  - claude.ai's own `subscription_details` read `status: active, plan_ending_at 2026-10-04T21:43:09Z`.
  - Usage read $250 of credit at 17:17 EDT.
  - At 18:28 EDT (22:28Z, after the end) every meter was null, the credit included.
- **Org `4457aa9d`.** The org is mghenis@gmail.com, which lapsed about 2026-09-30.
  - Usage read $250 at 07:07 EDT on 09-30.
  - Every meter was null at 16:46.

Help article 17152539 says a downgrade keeps the credit. What these two show is that a lapsed plan's
usage stops showing it. The sensor therefore treats money left on a lapsing plan as at risk, as it
does cards, and records it as lost when the lapse is seen.

## The census (2026-10-05) and what it cost

The census script (`tools/census.py`) predates the sensor and was more liberal than the sensor's gate.

- It ran a heal turn on every login whose token had expired, except the two PE logins. That was 13
  turns, six of them on accounts under operator holds: claude-5, -12, -13, -14, -16 and -17.
- None of the 13 completed a model turn:
  - The five lapsed accounts' turns were refused ("organization has disabled Claude subscription
    access").
  - Six logins failed authentication: max.ghenis@gmail, axiom-foundation, farness, optiqal,
    rulesatlas and rulesfoundation.
  - The axiom.org and hivesight turns were refused at the weekly limit, after the CLI had renewed the
    token.
- One earlier heal, run by hand on max@maxghenis.com before the census, ran a Haiku turn to
  completion.

The sensor's gate skips held accounts. A dry run of `refresh` against the live store, with a recording
heal, would have healed only the five lane-backed, unheld logins whose tokens had expired.

What it found:

- **One unused card.** `max@axiom.org` (lanes claude-11, claude-18) holds
  `opus55-launch-promax-20260921`, 1 of 1 left, `usable_now`. The account is at its weekly limit
  (100%, resets Oct 8), and the card ends 2026-10-22T16:00Z.
- **Card used:** `max@hivesight.ai`, `max@maxghenis.com`. Each of the three accounts above has claimed
  the $250 cloud credit (09-24, 09-27, 09-30), unspent, expiring 2026-11-05T07:59Z.
- **Lapsed** (`claude_free`, `canceled`): logpile, openmessage, policybench, rules.foundation,
  mghenis@gmail.
- **Login could not be renewed** (sign in again to read): max.ghenis@gmail, axiom-foundation, farness,
  optiqal, rulesatlas.
- **No login folder holds a login:** thesisinstitute, ubicenter.
- **PE.** The PE personal and PE Team logins were not healed, and were not read.

## How a login is kept readable

A login's access token lasts about eight hours, and only the CLI renews it, from its own store
(C-23.47). The sensor therefore spends one minimal turn under an expired login's folder, then reads:
`claude -p "Reply with exactly: ok"` on Haiku, with no MCP server, in a process group of its own.

- On an account at its limit the turn is refused with the limit message, but the CLI has already
  renewed the token.
- Turns are rationed:
  - never on an account under an operator hold, or with no lane;
  - at most once per `heal_interval_min` (60);
  - with reads every 6 hours, a live account's token is renewed about every other read.
- A login the CLI says it cannot renew ("Failed to authenticate: OAuth session expired and could not
  be refreshed") is `login-dead`. No further turn is spent on it until someone signs in again.
- A heal that never reached the login (a timeout, no CLI, a daemon stop) is retried.

## Not covered

- A cancellation or downgrade scheduled for the end of a billing period is invisible to every endpoint
  an OAuth login can read.
  - It forfeits an unused card.
  - The operator declares it in `<state root>/claude-plan-ends.json`
    (`{"<login or lane id>": "YYYY-MM-DD"}`), and the sensor warns from that.
- The claim deadline of an unclaimed credit is in no payload (Oct 7 for the cloud credit, from the help
  article). The sensor warns from the day it sees an eligible credit not yet claimed.
- The User-Agent gate is the server's rule, observed here. If it changes, cards read as ineligible
  (`surface` or `cli_version`), and `subfleet cards` shows that reason.
