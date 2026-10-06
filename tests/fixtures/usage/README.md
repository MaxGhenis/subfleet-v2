# C-12.10 usage fixtures

Copied read-only from retained provider streams on 2026-10-03/04. Payload text,
emails, request ids, session ids and tool content are removed; remaining usage
and model accounting counters retain the provider's values. Synthetic message
ids in the Claude fixture preserve the first and last request of each segment.

- `claude-multi-result.jsonl`: `~/.subfleet/jobs/20260928-152259-turn-cv-1790623379825-92907e0c81c5/a1/stdout`.
  The deduplicated main-request `(input, cache_creation, cache_read)` sums before
  its first result were `(394, 784128, 98621699)`, exactly that result's usage.
  The sole main request between results was `(4, 1218, 784128)`, exactly the
  second result's usage. The output counters were 327997 then 1373. The segments
  are disjoint; sum `result.usage`. `modelUsage` is cumulative: its second
  output counter is 657781, up by 1373 from 656408, and its input/cache counters
  rise by the second result's counters too. Keep the final map without summing.
- `codex-original.jsonl`: `~/.subfleet/jobs/20261003-103129-review-256b/a1/stdout`.
- `codex-resumed.jsonl`: `~/.subfleet/jobs/20261003-121437-review-256b/a1/stdout`,
  the preceding job's resume on the same native thread. Input/output were
  `(5979585, 16842)` then `(9898307, 36762)`; another resume at
  `~/.subfleet/jobs/20261003-142740-review-256b/a1/stdout` reported
  `(10880689, 41629)`. Older retained resume chains sometimes decrease, so
  monotonicity alone cannot establish disjoint attempt totals. The provider's
  exec implementation copies `ThreadTokenUsage.total` into `turn.completed`:
  [usage_from_last_total](https://github.com/openai/codex/blob/main/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L108),
  after replacing its last snapshot on each usage notification.
  [The cold resume test](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_resume.rs)
  (`cold_paginated_resume_restores_usage_without_loading_turns`) proves that a
  resumed thread can restore earlier totals. Keep resumed exec totals labelled
  `cumulative_thread: true`; do not subtract earlier attempts.
- `codex-app-turn.jsonl`: `~/.subfleet/jobs/20260924-170050-turn-cv-1790283629170-509df036a3d5/a1/stdout`.
  Its previous turn at `20260924-170029-turn-cv-1790283629170-509df036a3d5/a1`
  used the same native thread and reported total=last input 14712/output 5.
  This turn reported total=last input 14734/output 5, so those app-server
  processes did not restore the prior counters. All six retained Codex turns
  inspected, through `20261001-051515-turn-cv-1790846115021-d5e2a266deb9/a1`,
  reported total=last. Newer provider cold resumes can restore totals, as above;
  therefore the parser labels historical/uncertain initial total scope rather
  than assuming every app-server process resets. Later own-turn notifications
  replace prior snapshots; `last` is the latest request, not the whole turn.

No live files or daemon state were written.
