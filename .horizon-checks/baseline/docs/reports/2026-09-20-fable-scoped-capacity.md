# Fable exhaustion and spare Opus capacity

Two real Fable attempts were rejected with
`rateLimitType=seven_day_overage_included`. Their shared weekly utilization was
55% and 66%, while the separate Fable bucket was 100% and 101%. The adapter
incorrectly created account-wide closures and stored an allowed event's Fable
bucket as account capacity. Either mistake can block a usable Opus lane.

The installed Claude Code 2.1.278 binary explicitly labels
`seven_day_overage_included` as `Fable limit`, `seven_day_opus` as `Opus limit`,
and `seven_day_sonnet` as `Sonnet limit`. Its own stream schema describes the
overage-included weekly window as a per-model bucket. This was inspected on
2026-09-20. Anthropic's [Fable plan documentation](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)
also confirms that another model can use remaining plan capacity after the
Fable allowance is exhausted.

The adapter now gives these named limits their model scopes. Five-hour,
all-model weekly, and unknown rejection types still close the account. Allowed
events store the Fable bucket as a scoped weekly reading. Utilization above one
is stored as exhausted (one); the original value remains in the raw stream and
classification evidence.

Reserve calculations accept a stream's shared and scoped weekly windows only
when both came from the same event, including its attempt identity. An absent
scoped stream window remains unknown. Complete OAuth snapshots retain their
existing absence semantics. Fresh measured reserve restrictions take priority.
When measurement is unavailable, an active, provider-reported model-only
exhaustion establishes that the reserved model cannot use capacity before its
reset. Other models still need a supervised same-model admission probe. No
shared utilization is invented, and account limits, ownership, identity,
desktop protection, exclusions, and concurrency remain enforced.

Regression tests cover the observed event shape, shared exhaustion, all named
closure scopes, incomplete and unrelated stream windows, over-cap readings,
and expired, released, guessed, or operator closures.

Existing live closures require a separate audited correction from their saved
provider evidence. Historic attempts and receipts must remain unchanged.
