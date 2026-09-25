# subfleet v2

One supervised daemon that dispatches delegated agent work across several Claude and Codex subscription accounts, keeps every run as a durable row it owns, and tells the truth about capacity.

This tree is the from-scratch rebuild. Retained public commands use native v2
implementations; obsolete private v1 worker callbacks refuse explicitly. Historical
v1 records remain available without delegating commands to the old installation.

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

The tracked `bin/codex` PATH shim uses the installed native entrypoint at
`~/.local/share/subfleet/current/venv/bin/subfleet`. After installing and verifying
that release, replace the old shim from the reviewed checkout:

```sh
cp -P ~/bin/codex ~/bin/codex.before-v2.$(date +%Y%m%dT%H%M%S)
install -m 755 bin/codex ~/bin/codex.v2
mv -f ~/bin/codex.v2 ~/bin/codex
```

Interactive Codex passes through. For `exec`, `e`, and `review`, a missing
`CODEX_HOME` requires a successful native `pick codex`; explicit `-m`/`--model`
flags scope the recommendation. A failed pick never starts Codex. Picking is
advisory and reserves no slot; use `subfleet run` for supervised work. Every
noninteractive home passes the subscription-only API-key check, including when
the retired `SUBFLEET_ALLOW_API_LANE` override is set. Noninteractive launches also
remove inherited `CODEX_API_KEY` and `OPENAI_API_KEY`, matching daemon launches.
`SUBFLEET_NO_AUTOPICK` requires an explicit home. Raw `exec resume` and `exec fork` also require their
original `CODEX_HOME`; managed continuations use `subfleet resume JOB_ID`.

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

Before every Codex launch except an isolated review (`-I`, which runs Codex
with `--ephemeral --ignore-user-config` and its own inspection, C-23.3) the
daemon runs the never-rules guard preflight (`subfleet/guard/preflight.py`,
contract C-14.2): it checks the operator's `<state root>/guard/never-rules-hook.sh`
bytes and the pinned Codex version against `<state root>/guard/TRUST`, then asks a scratch-home `codex app-server` for
`hooks/list` and refuses the launch with exit code 7 unless the guard is listed,
enabled and trusted. The portable package contains no default security policy:
missing, malformed, or mismatched overlay files refuse launches with code 7.
Stage your reviewed overlay before installing the core-only release; the private
repository's [overlay migration instructions](docs/private-guard-overlay.md)
preserve existing installations and in-flight hook paths. Fake-provider tests
stage an explicitly synthetic guard only inside their temporary state roots.
Two v1 settings apply:

- `CODEX_GUARD_PREFLIGHT_TIMEOUT` — seconds allowed for each of the two Codex
  calls, `codex --version` and the `hooks/list` answer (default 60; worst case
  is therefore twice the value plus a two-second reap). Set it in the daemon's
  launchd environment; the daemon process is what runs the probe. A probe that
  does not answer in time is reported as a *timeout* (trust unverified), an
  app-server that dies or cannot be driven as a *probe* failure with its stderr,
  and a missing binary, home or workdir as an *environment* problem — never as
  guard-file drift.
- `SUBFLEET_CODEX_GUARD_CACHE` — where verified verdicts are kept (default
  `<state root>/guard-cache`; a relative value resolves under the state root,
  never the daemon's working directory, C-2.1). A verdict is reused only while
  the Codex version, the lane home, the override string, the reviewed overlay pins and the seeded
  `config.toml` and `hooks.json` are unchanged, and never past 30 days
  (C-23.5); a refusal is never cached, and a marker stamped in the future is
  discarded. Delete the directory to force re-verification.

Every preflight writes `guard-preflight.json` into the attempt directory (kind,
cached, elapsed seconds, time to the app-server's first byte, probe pid and exit
status, deadline, the request and response lines and the app-server's stderr
tail) and one `guard preflight …` line to `daemon.log`. `subfleet doctor`
reports the effective deadline and cached-verdict count; `subfleet doctor
--live` runs the preflight for every enabled Codex lane with the `codex` on the
shell's PATH, and a pass there writes the marker the daemon will reuse, because
the key does not include the executable's path.

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

A Codex weekly window starts at its first request after a reset, not at the
reset, and an idle lane's next reset slides a day for each idle day. Each probe
cycle therefore touches every v2 Codex lane whose weekly clock has not started:
one read-only Luna turn through the same guarded launch path as any probe, at
most once an hour per lane, followed by a re-probe and one service notice
(C-18.3). `subfleet status` and `subfleet lanes list` mark such lanes `clock not
started`. To inspect or act now:

```sh
subfleet lanes touch --dry-run        # what a touch pass would do, and why
subfleet lanes touch codex-4          # touch one lane now, whatever its readings say
subfleet lanes touch --all            # every lane whose clock has not started
```

Set `timers.touch_unstarted` to `false` in `policy.json` to stop automatic
touches; the daemon then warns about each lane whose clock has not started.

To hand several briefs to lanes in one call, list them in a TOML or JSON manifest.
Paths are relative to the manifest; an entry overrides `[defaults]`, which
override the other flags on the command line:

```toml
# handoff.toml
label = "codex handoff"

[defaults]
model = "fable"
sandbox = "workspace-write"
in_place = true

[[jobs]]
prompt = "spm-annual-chronicle.md"
workdir = "~/work/chronicle-task-branch"
out = "out/spm-annual-chronicle.md"

[[jobs]]
prompt = "tariff-p5-commerce.md"
workdir = "~/work/tariff-task-branch"
```

```sh
subfleet run --batch handoff.toml                       # prints one job id per line
subfleet run --batch handoff.toml --request-id h-0920   # repeatable: entry n is h-0920-n
```

Every entry is validated before the first submit. After that a refused entry
does not stop the others. One session may hold several writable jobs at once
when each writes in a different checkout; a second writer in one checkout, and a
second live instance of the same session, are refused.

If the usage endpoint cannot measure reserved capacity, an operator can authorize
one new job on an exact enrolled lane and model, with a recorded reason and
evidence. This does not assert that Fable is exhausted or that quota is available:

```sh
subfleet run --task research --tier standard -m opus -a claude-13 \
  --allow-unmeasured-reserve 'Operator authorizes this Opus job despite unavailable reserve telemetry; quota remains unverified.' \
  -C /path/to/work -p prompt.md --dry-run --json
```

Inspect the dry-run decision, then omit `--dry-run` to submit. The same lane and
model must pass a fresh admission probe before the job can run, including for
standard read-only work. This option bypasses only an **unmeasured** reserve
verdict: known closures, measured reserve restrictions, identity protection,
ownership, exclusions, desktop protection, and concurrency limits still apply.
There is no account rotation or model promotion. The reason (up to 2,000
characters; do not include secrets) is stored with the job and its audit event.
Authorization is not inherited by another job or a resume.

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
