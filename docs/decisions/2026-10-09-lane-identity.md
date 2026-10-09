# Which account a lane's token belongs to (D-ID1, 2026-10-09)

Max delegated Subfleet design calls. This record makes one, for 2.1.12 on
`release/217`. It amends the acceptance contract at C-1.4, C-10.2, C-10.3,
C-10.6, C-10.7, C-11.8 and C-29.6, and it adds C-10.8 and C-10.9.

## What happened

The hub logged defect D-ID1 (`~/reviews/subfleet-hub/defects.md`). claude-7
(labelled max@hivesight.ai), claude-19 and claude-6 (max.ghenis@gmail.com) all
reported the same readings as claude-4 (max@maxghenis.com), within two minutes on
10/5: a weekly reset at 2026-10-10T14:00Z, about 99% weekly and 40% five-hour.
Max's own account sheet says those accounts reset at other times. Subfleet was
counting one account three times and could not reach the other two.

Nothing in Subfleet noticed. C-10.6 binds a Claude lane to the identity
`/api/oauth/profile` reports for its credential. Every Claude lane is a
`claude setup-token` token stored as `claude-quota-<email>`, and a setup token
cannot read its profile. So every lane was `identity_status: enrolled` with
`identity: null`, and its label was only what the operator typed.

## What a setup token can say about itself

Measured on 2026-10-09 with each lane's own token. The token was read inside one
Python process (`agent-secret get`, as the daemon reads it) and never printed;
only statuses, allow-listed response headers and JSON key names were.

| Request | Answer for a setup token |
|---|---|
| `GET /api/oauth/profile` | 403 `permission_error`: "OAuth token does not meet scope requirement any_of(user:profile, user:office)" |
| `GET /api/oauth/account`, `GET /api/oauth/claude_cli/roles` | 403: scope `user:profile` required |
| `GET /v1/organizations/me` | 403: "Authentication method not allowed" |
| `claude auth status --json` (Claude Code 2.1.284, `CLAUDE_CODE_OAUTH_TOKEN`) | `loggedIn: true, authMethod: "oauth_token"`: no email, account or organization |
| `GET /v1/models?limit=1` with `anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20` | 200, header `anthropic-organization-id: <uuid>`. Six of the sixteen items got 403 instead, still with the header (why was not established). |
| `POST /v1/messages/count_tokens` | 200, the same header |
| `POST /v1/messages` (one token) | the same header, plus `anthropic-ratelimit-unified-*`; no account id; spends a turn |
| `GET /api/oauth/usage` | answered by another stack, with no such header (a 429 was seen; a 200 was not tried, to spare the live lane's pacing) |

So the only server-attested identity a setup token yields is its organization,
from `GET /v1/models`, at no model cost. On a Pro or Max plan an account has
exactly one personal organization, so the organization names the account. Seats
of one Team or Enterprise organization share it. An email is never in the
answer: only a full login's profile names email, account and organization
together.

Across the fleet, three keychain items (max@maxghenis.com, max@hivesight.ai and
max.ghenis@gmail.com, the last shared by claude-19 and claude-6) named one
organization; every other item named a distinct one. The desktop app was signed
in as max.ghenis@gmail.com, and the identity it gives its own sessions names
another organization. So the `claude-quota-max.ghenis@gmail.com` item provably
does not hold that account's token. Which of the other two labels is right needs
a profile-scoped login of one of them; every `logins/` folder had expired.

## Decisions

1. **A setup token's identity is its organization** (C-1.4, C-10.6), recorded as
   `org:<org_uuid>` beside the profile's `<account_uuid>:<org_uuid>`. The adapter
   asks the profile first and the organization header when the profile says
   no-scope. An answer is kept per token digest for six hours across adapters
   (the timers build one per read), so the fleet sends one `/v1/models` per token
   every few hours, not one per cycle. A header on a 401 or 5xx answer, one that
   is not an id, and one that contains the credential are no identity.

2. **Identities compare by account** (C-10.7, `lane_identity.same_account`):
   account-level when both sides name an account, organization-level otherwise.
   The desktop's ownership of a lane, a login's binding to lanes (C-9.10), the
   doctor's duplicate check and re-enrolment all use it.

3. **Enrolment verifies, and refuses** (C-10.2). It refuses a token nobody can
   name; a token whose own profile names another email than its keychain item; a
   token that is another enabled lane's account (the D-ID1 shape, naming the
   lane); and a token a profile fact shows to be another email's. It records the
   organization otherwise.

4. **Periodically, every finding is kept** (C-10.6). Every usage read (idle or
   busy), attempt and admission probe goes through `lane_identity.record`. A lane
   that recorded nothing learns its organization once. A later answer naming
   another account is `mismatch`, which only re-enrolment releases. So every
   lane the timers read (enabled, v2-owned, not held) learns its organization in
   its first probe cycle after install, with no operator action; a disabled or
   held lane learns it when it is read again or re-enrolled.

5. **Labels are judged only on profile answers** (C-10.6). A lane its own token
   cannot prove is `enrolled` (label unproven). The fleet's facts are profile
   answers it already keeps: lanes whose own profile bound them, the logins
   `claude-cards.json` reads, and `desktop.identity` events (which now keep the
   organization type). A fact naming the label for the lane's account makes it
   `verified`; a fact naming another email makes it `mismatch`. At organization
   level only a personal organization speaks, and facts that disagree decide
   nothing. Today every login has expired, so no label can yet be proven; the
   moment a login is healed or a home lane enrolled, the labels it speaks to are
   judged.

6. **One account, one candidate** (C-10.8). Lanes whose identities are one
   account are not all candidates. One takes the work: `verified` first, then the
   earliest binding. The others are refused as `identity-shared`, a standing
   refusal. This is derived on every view, not stored, so it heals itself when a
   lane is re-enrolled. It is not a disable either: the label that is wrong is
   unknown, the account's capacity is real, and the lane keeps being read, so a
   re-signed token is seen. A critical alert names the lanes and the repair.

7. **Readings that match too well warn** (C-10.9). Weekly resets are whole hours
   and utilization whole percents, so a pair is twins only when it agrees on two
   windows, or on three values of one in step, and on one value strictly between
   0 and 1. It is a backstop for what identity cannot see (Codex lanes, a token
   whose organization could not be read). A Claude pair with both identities
   recorded is C-10.8's business, so it is left out.

8. **The repair is re-enrolment** (C-10.2). Enrolment used to refuse a credential
   while any binding of it was enabled, so a `mismatch` lane, which C-10.6 said an
   operator releases by re-enrolling, could not be re-enrolled. Now a
   credential's enabled bindings that are `mismatch` or shared may be replaced:
   the new binding disables them. When the account changed, only operator holds
   carry over.

## What Max does to repair D-ID1

For max@hivesight.ai (claude-7) and max.ghenis@gmail.com (claude-19): sign in to
claude.ai as that account, run `claude setup-token`, store the token as
`claude-quota-<email>`, then run `subfleet lanes enroll claude-quota-<email>`.
The new lane is checked against claude-4 at once. If max@maxghenis.com turns out
to be the mislabelled one, the same applies to claude-4.

## Not done

- No new CLI verb. `subfleet lanes list` shows each lane's identity and sharing,
  every probe cycle checks, and `doctor` names the groups.
- A Team seat enrolled with a setup token is indistinguishable from the other
  seats of its organization. Two such lanes are refused as shared. Enrol a Team
  seat as a home lane (a full login), whose profile names the account.
