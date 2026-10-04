# Offline harness for subfleet-fanout.js

Runs the workflow script with a stub `agent()` to render the three Bash
commands, then executes them against `fake-subfleet.sh`, which replays the
JSON shapes `subfleet run --batch --json` and `subfleet wait --json` printed
on 2026-09-22 and 2026-10-04 and never touches the daemon. Needs `bun`.

```sh
H=$(mktemp -d); mkdir -p "$H/bin"; cp fake-subfleet.sh "$H/bin/subfleet"; chmod +x "$H/bin/subfleet"
export PATH="$H/bin:$PATH" FAKE_STATE="$H" SUBFLEET_FANOUT_RETRY_S=1
bun validate.mjs ../../../../.claude/workflows/subfleet-fanout.js        # argument validation and result shaping
bun render.mjs ../../../../.claude/workflows/subfleet-fanout.js "$H" '{"label":"t","requestId":"rq-1","stateDir":"'"$H"'/s","items":[{"task":"review","tier":"standard","dir":"/Users/me/repo","promptPath":"/p.md","outPath":"'"$H"'/s/out-a.md","name":"a"}]}'
bash "$H/step0.sh"; bash "$H/step1.sh"; bash "$H/step2.sh"; bash "$H/step2.sh"
```

`SCENARIO` on the fake selects a failure: `refuse-b` (entry b refused, exit
7), `submit-down` (exit 69 at submit), `wait-down-first` (exit 69 on the
first wait), `lost-c` (job c ends `lost`, exit 125), `unknown-id` (exit 2),
`no-name` (wait rows without `name`). The scenario matrix run on 2026-10-04
is described in the report.
