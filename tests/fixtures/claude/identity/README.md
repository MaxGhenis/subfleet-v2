# Profile-endpoint fixtures (C-10.6)

Four answers `https://api.anthropic.com/api/oauth/profile` can give a lane's own
credential. `tests/fake/profile.py` serves them; nothing here ever holds a token.

| fixture | what the endpoint did | the check that follows |
| --- | --- | --- |
| `profile-ok.json` | 200 naming the account the lane recorded | `verified` |
| `profile-mismatch.json` | 200 naming another account | `identity-mismatch` |
| `profile-no-scope` | 403, as a setup token gets | `identity-enrolled`, or `identity-unverified` for a lane that had recorded an identity |
| `profile-unavailable` | the request never completed | `identity-unverified` |

`profile-mismatch.json` is the identity triple from the 2026-09-05 incident,
copied from `~/axiom-eng-briefs/opus-expiry-20260905/identity-fix-live-result.json`:
the credential the fleet was reading really belonged to `max@axiom.org` while the
lane it was attributed to was recorded as RulesAtlas. `profile-ok.json` is the
RulesAtlas identity that lane recorded, so the pair reproduces the incident
exactly: same lane, same credential, one answer that matches and one that does not.
