# Inline questions, lasting approvals, and personal Stop

Implemented against `52ffde17a42b` in the assigned workspace. No installed app or live daemon was changed or launched.

The person sees question cards directly in the conversation: numbered option buttons with descriptions/previews, multiple selections when requested, Other, Skip, Back/Next and one combined Submit answers. Number keys 1–9 work while choices are focused; typing in the composer or Other stays text input. Sending a composer message leaves the question pending. Tool requests show the full masked request with inline Allow/Deny and optional details/note; notifications offer Allow once for an identified unmasked tool request. Approvals wait indefinitely by default. A personal Stop settles as stopped and lets queued messages proceed once delivery is known; genuinely unknown delivery still needs resolution.

| Change | Location |
| --- | --- |
| Inline tool and question cards, masked review | `app/Sources/UIApprovals.swift:5`, `:123`, `:284` |
| Foundation question state and combined answers | `app/Sources/QuestionCard.swift:13` |
| Option previews and exact provider request ID | `app/Sources/Protocol.swift:818`, `:866` |
| Exact event/list approval binding | `app/Sources/Timeline.swift:561`, `app/Sources/UIModel.swift:527` |
| Guarded Allow once notification | `app/Sources/UINotifications.swift:5`, `app/Sources/UIModel.swift:631` |
| Timeline integration and waiting hint | `app/Sources/UIWindow.swift:301`, `:401` |
| Optional approval clock and validation | `subfleet/policy.py:84`, `:371`; `subfleet/default_policy.json:159`; `subfleet/conversations/runner.py:67`, `:659` |
| Personal Stop settlement and ambiguity resolution | `subfleet/conversations/reconcile.py:158`; `subfleet/conversations/service.py:799`, `:2012` |
| Question and notification probes | `tests/frontend/test_core_question_card.py:1`; `tests/frontend/test_core_approval_notification.py:1` |
| Approval identity regressions | `tests/frontend/test_core_timeline.py:226` |
| Multi-question/composer and finite-timeout e2e cases | `tests/e2e/test_conversations.py:395`, `:424` |

Contract changes: C-24.6–C-24.8 (`docs/acceptance-contract.md:369`) preserve delivery evidence and exempt a recorded personal Stop from unfinished-turn blocking; C-26.9 (`:392`) makes missing/null approval deadlines unlimited while retaining positive finite tool limits; C-27.1/C-27.2 (`:401`) specify exact request binding and inline answers; C-29.9 (`:422`) specifies the guarded notification action. Design D-7 and section 8, plus the resolved approval-ID item in `docs/desktop/app-needs.md`, agree.

Validation:

- Relevant daemon units: 229 cases passed across focused runs. A pre-existing relay test used a 0.3-second sleep; replaced it with an event and verified its passing rerun. An additional broad unit run passed 269 cases before interruption in an unrelated filesystem scan; it is not a completed suite run.
- Conversation e2e: 32 collected and skipped by the existing C-5.3 fixture because this sandbox denies macOS process/boot-identity inspection. New fake-CLI tests cover combined multiSelect answers, a composer send while a question remains pending, finite approval timeout, queue continuation after Stop, and unknown delivery.
- Foundation-only app-core probes: 57 passed, including 20 new question/notification cases and three exact-identity regressions. The initial 53 passes plus four identity checks on the same freshly compiled probe cover all 57 current cases.
- `app/build.sh` passed and produced the signed `build/Subfleet.app`, using `SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk` and workspace-local Swift/Clang module caches. The default SDK 27 build is blocked by this sandbox rejecting the SwiftUI macro helper (`sandbox_apply: Operation not permitted`), including on unchanged MainWindow code. SDK 26.5 requires no sandbox-policy changes. The successful build log is `build/app-build-sdk26-final.log`; existing Sendable/unnecessary-await warnings remain.
- `git diff --check` passed. UI was not launched for visual QA.

The linked checkout’s Git object database is outside the writable sandbox; Git refused its first object write with “Operation not permitted.” The working changes remain in this checkout. Coherent commits with the requested Claude co-author trailer are preserved in a workspace-local Git store and exported as `build/inline-questions-approvals-stop.bundle` and `.patch`, based on `52ffde17a42b`. The assigned checkout HEAD could not be advanced; nothing was pushed and no existing history was rewritten.
