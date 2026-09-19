# subfleet v2

One supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts, keeps every run as a durable row it owns, and tells the truth about capacity.

This tree is the from-scratch rebuild. The v1 tree at `~/chief-of-staff/subfleet` keeps running until cutover.

- Plan of record: `docs/plan.md` (plan B revision 4 as amended by the grafts from plan A and the round-2 reviews).
- Binding contract for implementation: `docs/acceptance-contract.md`. Every module is built against it; every acceptance test names the clause it proves.
- Inputs: `docs/plan-a.md`, `docs/plan-b-rev4.md`, `docs/comparison.md`, `docs/reports/`.

Native exact-revision agreement gates: [usage and integration notes](docs/gates.md).

Python 3.12+, standard library only. `uv sync --group dev && uv run pytest`.

The implementation includes durable submission and cancellation, guardian receipts
and recovery, both provider adapters, policy-based routing, immutable artifacts,
notices, timers, session continuity, and exact-revision agreement gates. The
[release record](docs/release-gates.md) distinguishes verified checks from the
original 100-job canary and seven-day shadow policy. On 2026-09-19 the operator
explicitly requested a direct cutover with rollback and preservation of running
jobs; this overrides the staged rollout, without claiming those observations passed.

To build and verify locally:

```sh
uv sync --locked --group dev
PATH=/usr/sbin:/sbin:$PATH uv run pytest -q
bin/sf2 --help
```

The default suite uses temporary stores and fake providers. Real-provider tests
require explicit opt-in. GitHub Actions runs the suite on macOS with Python 3.12
and 3.14; process containment is tested on the same operating system as deployment.
`bin/sf2` uses this checkout's environment and ignores inherited Python paths from
v1. Keep it separate from the installed `subfleet` command during validation.

The native macOS menu bar app reads the daemon's `status.json` from
`$SUBFLEET_HOME` (default `~/.subfleet`). Build it with the macOS Swift developer
tools; this creates a local bundle and never installs or launches it:

```sh
app/build.sh                         # build/Subfleet.app
app/build.sh /path/to/build-output   # custom local output directory
uv run pytest -q tests/frontend      # Foundation-only model checks; no GUI
```

The frontend tests decode real daemon JSON for both providers, including stale
or missing readings, offline status, ownership, and identity mismatches. CI also
compiles the full app. Installation and launch remain separate cutover steps.

With an explicitly configured v2 daemon and v2-owned lane, the ordinary flow is:

```sh
bin/sf2 run --task research --tier standard -C /path/to/work -p prompt.md --json
bin/sf2 why JOB_ID
bin/sf2 wait JOB_ID
bin/sf2 runs show JOB_ID --out
bin/sf2 resume JOB_ID 'Continue with these additional instructions.'
```

Resume binds to the source attempt's provider session, lane, model, and workspace.
It refuses unavailable native sessions and active or quarantined source jobs.
For new work, routing only promotes along the task's configured chain. Claude
lanes limited on a stronger model are preferred for eligible cheaper work;
`models.<name>.priority` defines strength across separate task chains. Reserved
Fable capacity, account closures, exclusions, and ownership still control eligibility.

The [migration procedure](docs/migration.md) and
[canary runbook](tools/canary_runbook.sh) cover account transfer and rollback.
They change machine state and must be applied phase by phase after inspecting
the current roster. Read-only validation commands are:

```sh
uv run python tools/canary_check.py --help
uv run python tools/soak_report.py --help
uv run python tools/compare_decisions.py --help
uv run python tools/measure_release_gates.py --jobs 300 --calls 200
```

The measurement command uses a temporary fake-provider fleet. A missing or
incomplete canary/soak record cannot pass, and a failed numeric gate returns a
nonzero exit status. Historical dates in planning documents are provenance;
current evidence belongs in `docs/release-gates.md`.
