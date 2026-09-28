# Phone bridge implementation report

Built in the assigned detached checkout of `52ffde17`. The design was written
before implementation: [DESIGN-phone.md](../DESIGN-phone.md).

## What is built

| Area | Implementation |
| --- | --- |
| Outbound | `subfleet/phone.py:90`: one asynchronous worker; only approvals, questions, person blocks, and explicitly requested completions. Cards name conversation, lane/account and a scrubbed excerpt. |
| Delivery | `subfleet/phone.py:297`: durable send claims, `say` classes/keys, real message-ID receipts, bounded calls; ambiguous delivery stays recorded instead of being blindly resent. |
| Resolution | `subfleet/phone.py:335`: existing cards are edited after phone or app answers/withdrawals; question progress and final answers are shown. |
| Persistence | `subfleet/conversations/store.py:161`: schema 3 adds card mappings, question progress, duplicate-reply receipts and one-shot subscriptions. Migration preserves conversations. |
| Inbound | `subfleet/conversations/phone.py:178`: `tap` uses the existing approval response path; `:194` implements replies and duplicate recovery; `:35` ownership and `:256` subscriptions. Question progress persists and events carry `source: phone`. |
| CLI and daemon | `subfleet/phone_cli.py:14`, `subfleet/conversations/service.py:847`, `subfleet/protocol.py:27`: socket operations, CLI receipts and capability discovery. |
| Policy | `subfleet/policy.py:23`: validated `phone.telegram.enabled/events`; defaults on for approvals/questions/blocks. |
| Authentication | `subfleet/conversations/peers.py:90`: exact poller-parent recognition after existing guardian/attempt checks. Owner-chat filtering remains in CoS. |
| Contract | `docs/acceptance-contract.md:435`: C-31.1, and an explicit cross-reference in C-25.6. |

The baseline has no live-steer operation. Replies truthfully queue through
`message.submit`; they do not clear safety blocks. Ordinary completions send
nothing. `phone notify CONVERSATION_ID` is one-shot and requires `done` enabled
in the event policy.

## Chief-of-staff patch

[cos-tg-poller.patch](cos-tg-poller.patch) changes only the existing poller:
owner-filtered `sf:` callbacks call `phone tap`; replies consult `phone owns`
before CoS command parsing and pass their update ID to `phone reply`. Unknown
cards preserve CoS handling; failures are visible; edited owned replies are
ignored. Arguments never pass through a shell. The patch adds no listener.

[README.md](README.md) records the exact CLI contract and orchestrator rollout.
Tests apply the exact patch to the pinned original poller fixture. Nothing was
written into the real chief-of-staff checkout and the patch remains unapplied.

## Validation

The focused unit/patch suite and runnable fake-CLI workflow cover offered scopes,
policy defaults and off switches, quiet-hour holding, persistence/restarts,
duplicate taps/replies, question selection and free text, app resolution edits,
queued replies, source attribution, delivery failures, and silent ordinary
completion. The final combined run passed **298 tests**, with **5 skipped**:

| Tests | Result |
| --- | --- |
| `tests/unit/test_phone_outbound.py` | 35 passed |
| `tests/unit/test_phone_inbound.py` | 35 passed |
| `tests/unit/test_phone_cli_policy.py` | 22 passed |
| `tests/unit/test_phone_poller_patch.py` | 14 passed |
| Existing policy, conversation store and peer/attachment unit tests | 188 passed |
| `tests/fake/test_phone_bridge.py` | 4 passed |
| `tests/e2e/test_phone.py` | 5 skipped (sandbox process inspection) |

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  --basetemp=.pytest_cache/phone-final \
  tests/unit/test_phone_cli_policy.py tests/unit/test_phone_inbound.py \
  tests/unit/test_phone_outbound.py tests/unit/test_phone_poller_patch.py \
  tests/unit/test_policy.py tests/unit/test_conversation_store.py \
  tests/unit/test_conversation_peers_attachments.py \
  tests/fake/test_phone_bridge.py tests/e2e/test_phone.py -q --tb=short -rs
```

`git diff --check` passed. Python compilation/parsing and `phone --help` passed.

The runnable workflow uses the actual patched poller, Subfleet CLI, Unix socket,
conversation service/store, Claude response serialization, a fake provider CLI
subprocess and a file-only fake say. Its scheduler/runner lifecycle and peer
census are explicit test doubles. Real peer rules have independent unit tests.

The broader regression selection passed **440**, with **8 failures** involving
denied macOS `ps`/`sysctl`/boot identity inspection. Running those exact eight
tests against an isolated archive of untouched `52ffde17` reproduced **all 8**.
The five full-process phone e2e cases are present but **skipped** by the existing
harness because macOS boot identity is unavailable in this sandbox.

## Workspace and delivery status

No real Telegram messages were sent. No personal daemon or runtime state was
used. All executable tests used temporary state, fake provider tools and file
transports. No caller checkout or chief-of-staff files were modified.

The design commit was attempted first. Git could not create the index lock in
the common Git directory outside the writable workspace (`Operation not
permitted`). All coherent changes therefore remain uncommitted, as the task
explicitly permits. No history was rewritten and nothing was pushed.
