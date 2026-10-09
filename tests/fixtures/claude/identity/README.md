# Profile-endpoint fixtures (C-10.6)

Four answers `https://api.anthropic.com/api/oauth/profile` can give a lane's own
credential. `tests/fake/profile.py` serves them; nothing here ever holds a token.

| fixture | what the endpoint did | the check that follows |
| --- | --- | --- |
| `profile-ok.json` | 200 naming the account the lane recorded | `verified` |
| `profile-mismatch.json` | 200 naming another account | `identity-mismatch` |
| `profile-no-scope` | 403, as a setup token gets | the organization probe decides (below) |
| `profile-unavailable` | the request never completed | `identity-unverified` |

`profile-mismatch.json` is the identity triple from the 2026-09-05 incident,
copied from `~/axiom-eng-briefs/opus-expiry-20260905/identity-fix-live-result.json`:
the credential the fleet was reading really belonged to `max@axiom.org` while the
lane it was attributed to was recorded as RulesAtlas. `profile-ok.json` is the
RulesAtlas identity that lane recorded, so the pair reproduces the incident
exactly: same lane, same credential, one answer that matches and one that does not.

## The organization probe (D-ID1, 2026-10-09)

When the profile answers 403, the adapter asks `GET /v1/models` with the same
token and reads its `anthropic-organization-id` header (`_urlopen_org`).
`tests/fake/profile.py` answers it per bearer: the organization `derived_body`
names for that token, unless `SUBFLEET_FAKE_ORG` says otherwise (`<key>=<org id>`
puts two tokens in one organization, D-ID1's shape; `none`, `401`, `500` and
`error` are the answers that name nothing). In unit tests `tests/conftest.py`'s
`org_opener` stands in, naming the fixture lane's organization by default. Then:

| recorded identity | organization answer | the check |
| --- | --- | --- |
| none (label only) | any organization | `identity-enrolled`, and the lane records `org:<uuid>` |
| none (label only) | no answer | `identity-enrolled`, as before D-ID1 |
| `org:<uuid>` | the same organization | `identity-enrolled` |
| `org:<uuid>` or `<account>:<org>` | another organization | `identity-mismatch` |
| `<account>:<org>` | its organization | `identity-unverified` (an organization cannot say which account) |
| any | no answer | `identity-unverified` |
