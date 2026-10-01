# Read a busy Codex lane's usage beside its work (decision, 2026-09-30, revision 2)

Resolves MaxGhenis/subfleet-v2#73 for Codex lanes. Amends C-18.1 and adds C-18.3.
Revision 2 answers the two independent reviews of PR #96 and PR #97; see
"Changes since revision 1" at the end.

## What was observed

On 2026-09-30 the operator hold on codex-4 was released at 21:11:00Z. The lane
had a fresh week: a reset credit had been spent on it at 20:41:57Z. From
21:11:15Z admission placed work on it back to back, one attempt at a time, with
gaps of 0 to 15 s between them. `subfleet why` said "eligible but unmeasured".
Its last wham reading was from 2026-09-29T11:41:28Z; codex-1's came every 61 s.
Of the 77 probe cycles between 21:11Z and 22:29Z, none read codex-4. At the
time codex-4 was the only Codex lane under the 0.15 headroom floor, so every
hard-tier Codex job queued behind that one attempt.

Two latches held it there, and each kept the other going:

1. **The timer never reads a busy lane.** `Timers._reserve` refuses a lane with
   an attempt reserved, starting, running or finalizing, or with any
   `lane:<id>:%` lease. A lane that always has work is never read.
2. **The reset credit's override never settles.** A confirmed consume makes
   `ResetCredits.confirmed_override` answer for the lane until a fresh `ok`
   read with `limit_reached: false` reconciles it, or for seven days. While
   it stands, `Daemon._pick` holds out every reading of the lane, so the lane
   is unmeasured, and main and the release installed that day (617892c1) cap an
   unmeasured lane at one attempt. `settle_by_usage` has one caller,
   `Timers._persist`, which only the idle path reached. The six native reset
   credits before this one each settled 64 to 141 s after they were confirmed,
   at their lanes' next timer reads. codex-4 went from its hold straight to
   work, so its override would have stood until 2026-10-07T20:41:57Z.

The loop: busy, so not read; not read, so the override stands; the override
stands, so the lane is unmeasured and capped at 1; capped at 1 with a queue,
so it is busy.

It is not only an unmeasured lane's problem. From 01:37:56Z to 03:52:56Z on
2026-09-29 codex-4 had up to 43 attempts in flight, no timer read, and a
reading that went from 0.5 to 1.0 unseen; ten attempts ended `limited`. A busy
measured lane runs past its headroom floor unread.

## After release 2.1.9 (2026-10-01): uncapped, still unread

Release 2.1.9 (release/217 at f1bd2ab5, with PR #72) was installed at 09:13:39Z
on 2026-10-01, and the policy's interim `reading_ttl_s` of 604800 went back to
the default 120 at the 09:14:52Z restart. PR #72 removed the count caps, so an
unmeasured lane is no longer held to one attempt. It did not change
`Timers._reserve`, and the record shows a busy lane is still never read:

- Four probe cycles span the install: 09:13:05Z (the old daemon, 34 s before
  it) and 09:14:41Z, 09:15:54Z and 09:16:55Z (2.1.9). Each read codex-1, -2,
  -3, -5 and -6 (idle) and none read codex-4, which had four attempts in
  flight from 09:13:41Z. Its last timer read was 09:03:56Z.
- At 09:15:13Z and 09:15:15Z admission judged codex-4 "eligible but unmeasured",
  and a hard-tier job's admission probe (C-11.4) ran on it at 09:15:13Z.
- At 09:15:21Z it was "measured" again, because a conversation turn's stream
  had recorded an `app-server` reading at 09:15:19Z. Detached `codex exec`
  attempts record none, so a busy lane is measured only while conversation
  turns happen to run on it less than 120 s apart.
- Under the old release the same day: codex-4's override was settled at
  03:21:23Z by the one cycle that fell in a two-minute gap after an attempt was
  quarantined, 6 h 10 min after the hold's release. It then went unread again
  from 03:22Z to 06:14Z while it had work.

So on the installed line the loop no longer sustains itself through a cap of 1,
but a busy lane still has no fresh reading: its headroom floor cannot refuse
it, each hard or writable job on it pays an admission probe, and a reset
credit's override on it stands until it happens to go idle at a cycle.

## The rule

Every probe cycle reads every enabled, v2-owned, non-desktop, unlatched Codex
lane that has no `operator-hold` or `auth-dead` closure, busy or idle.

- **An idle lane** is read as before: `slot:0` is held from the reservation
  through publication (C-18.1).
- **A busy Codex lane** (an attempt reserved, starting, running or finalizing,
  or any `lane:<id>:%` lease) is read beside its work. The read is the usage
  GET alone. It takes no lease and writes no reservation event, and it never
  runs a heal turn. It goes through the same per-lane read fence as an idle read
  (`_read_probe`: one read per lane at a time, four at once, a 15 s deadline,
  a late answer dropped). What it found is sorted before anything is
  published:

| What the read found | Published |
|---|---|
| `ok` or `limited`, and the account the credential answered for is the lane's (or the credential names none) | Everything an idle read publishes, through `_persist`: the readings, a `provider-limit` closure or its release, `settle_by_usage` (which ends a confirmed override), and the verdict |
| A credential that names another account (`account_key`, from the home's `auth.json`, differs from the lane's), whatever the request returned | The identity-mismatch path, through `_persist`, as an idle read publishes it: no readings, no settlement, no closure change, the lane disabled |
| `auth-dead`, `revoked`, `expired-token`, `no-auth`, `network-error`, `http-error`, `invalid-response`, `unknown`, or a timeout | Nothing. The lane's verdict, readings, closures and `enabled` stay as they were. The cycle event names the lane and what the read found |

A busy lane's result is published with the cycle's other results, after every
read has finished, and only if the lane is still one `_claim` would read:
enabled, v2-owned and not the desktop's. (A lane's binding to its account never
changes; `Store.put_lane` refuses it.) It is judged and published in one store
transaction (`_publish_busy`): no slot keeps the lane's attempts away, so the
store's write lock does, and nothing can disable the lane, transfer it or
record a limit between the judgement and the commit. A publication that raises
rolls back whole, the settlement with it, and the verdict the lane had is put
back.

**An older answer never undoes a newer limit.** On an idle lane no attempt can
record a closure between the read and its publication, because the read holds
`slot:0`. (An operator's `lanes hold` can, and no publication releases a hold.)
On a busy lane an attempt can. It ends `limited` and `put_closure` does one of
three things: it records a new closure; it extends the lane's open closure in
place (one open closure per lane and scope; the row keeps its id and creation
time); or, when the limit it is given ends no later than the open row's, it
writes nothing. Publishing an older `ok` read would then release that closure,
and `settle_by_usage` would release it too. So a busy read records the lane's
limits as it starts (`_limits`): its open closures (id, scope, reason,
`until_at`) and its attempts that ended `limited`, whose outcome is the only
trace of a limit reported again. If at publication a closure is open that was
not open, exactly so, at the start, or an attempt has ended `limited` since,
the read publishes its readings and verdict but releases no closure and settles
no reset credit. The next cycle reads again. A read may still add or lengthen a
closure, which only makes admission more cautious.

## Why the busy read takes no lease

Issue #73 proposed a lease held by a `probe:timer:` holder from just before the
read until the verdict is published. Admission reads every lease whose holder
starts with `probe:` as a slot block: `_capacity_view` puts the lane in
`unavailable_lanes` (`no-slot`) and counts the lease toward `reserved_probes`.
Such a lease would take the lane from jobs for the length of every read, up to
15 s a minute, on exactly the lanes that have work waiting. The busy read
must never take a slot a job needs, so it holds no lease at all.

## Astra's five findings (plan gate 20260928-204040-plan-72aafe21)

Revisions 2 to 5 of the uncap plan read busy lanes and claimed that a blocking
verdict from such a read was fenced until published. Each finding broke that
claim. This design makes no fence claim. A busy read never publishes a verdict
that only a fence could make safe:

1. **A contended fence.** There is no fence.
2. **A publication error released the fence first.** There is no fence to
   release. A busy read is published in one transaction, so a publication that
   raises leaves the lane exactly as it stood before the read (readings,
   closures, the reset credit and the verdict), and the lane is read again next
   cycle. That holds for the one lane-removing verdict a busy read publishes,
   the mismatch: it rolls back with the rest and the next cycle's read
   publishes it.
3. **An identity mismatch was not fenced.** It is published without one, and
   this design says so. A job admitted between the read and the publication
   runs as it would have had nothing been read. Before this change only an
   idle read could catch it, when the lane happened to be idle at a cycle: a
   Codex attempt carries no identity evidence. Once published, the lane is
   disabled in the same store transaction, so admission's reservation, which
   re-reads the lane row, sees it at once. What could make a busy lane's
   `auth.json` name another account for a moment? This repository shows one
   thing: a half-written file fails to parse, reads as no credential
   (`_read_auth`), and is withheld. That a token refresh keeps the account is
   the Codex CLI's behaviour and is not established here. The live store has
   59,154 Codex timer verdicts from 2026-09-19 to 2026-10-01, over up to 13
   values of `last_refresh` per lane, and no identity mismatch among them. All
   of those were idle reads.
4. **An admission probe's reservation bypassed the fence.** There is no fence.
5. **The gap between the store commit and `Timers.metadata`.** The credential
   latches that live only in `metadata` (`revoked`, `expired-token`, `no-auth`)
   are never published from a busy read. The one lane-removing verdict a busy
   read publishes, the mismatch, writes `enabled = 0` inside the store
   transaction. A busy read's verdict is written to `metadata` inside that
   transaction, before the commit, and admission's reservation runs under the
   same store lock, so a reservation sees the store and `metadata` together.

Why a busy read publishes no credential verdict: its lane's attempts are
renewing the same `auth.json` while it is read. `probe_status` reads a 401 as
`auth-dead` unless the error says "expired" or the token's own expiry has
passed, and publishing that disables the lane until it is re-enrolled. Whether the provider refuses a token once the CLI
has replaced it is not established here, and withholding the verdict loses
nothing that was caught before. An expired token on a busy lane is renewed by
the attempts, and a heal turn beside them would be a second refresher.

What still catches a dead credential on a busy lane: an attempt that ends
`auth-dead` disables the lane and records that verdict at once (C-23.44), and
the lane is then not read until it is re-enrolled. A Codex attempt counts three
failures as a dead credential (`AUTH_RE`: a revoked refresh token, a blocked
organisation, a 401 from the usage endpoint); any other 401 is `transient`. A
busy lane whose credential is dead in another way is caught by its next idle
read, as before this change.

## Scope

**Codex only.** A Claude attempt records `provider` readings when it ends
(C-9.8, the stream's `rate_limit_event`), so a busy Claude lane is measured
again at every attempt's end. A Claude usage read also costs a profile request,
is paced against an endpoint that answers bursts with long Retry-After holds
(C-9.9), and its `expired-token` path needs a heal turn. Reset credits, and so
overrides, are Codex's (C-23.7).

**Pacing.** `_pace_usage` is Claude's spacing (C-9.9) and is unchanged. Codex
reads were never paced. A busy lane is read once per cycle, as it would be if
idle, so the wham endpoint gets no more requests than a fleet of idle lanes
sends today.

**Unchanged.** Held, `auth-dead`, disabled, desktop, non-v2 and revoked-epoch
lanes are still never read. Keepalive still waits for an idle lane. The idle
path keeps `slot:0` through publication.

**Offline.** Every Codex read in a cycle counts toward "offline" (every Codex
read a network error), whether or not it was published. A fleet whose Codex
lanes are all busy is no longer "online" by default.

## Alternatives not taken

- **(b) Persist wham readings from the admission probe (C-11.4).** That probe
  runs only before `workspace-write` or `hard` work on an unmeasured lane. Once
  the lane is measured the probes stop, the readings age out, and the lane is
  unmeasured again: it oscillates. The read would also add up to 15 s of HTTP
  inside the admission pass, which blocks admission. It would still need the
  mismatch check and `settle_by_usage`, both of which `_persist` already does.
- **#73's lease through the read and the publication.** See "Why the busy read
  takes no lease".
- **Busy Claude reads.** See Scope.

## Invariants and the tests that check them

`tests/unit/test_timers_busy_read.py` (example tests, a Hypothesis property over
lane occupancy and read results, and a busy-versus-idle differential). The
daemon-level cap test is in `tests/fake/test_timers_busy_admission.py`.

1. **No slot.** A busy read acquires, releases and holds no lease, and writes no
   reservation. The lease table during and after the read equals the table
   before it. The next attempt on the lane takes the same slot it would have
   taken without the read.
2. **Account fence.** No provider reading, closure release or settlement is
   written for a read that answered for another account.
3. **No credential verdict from a busy read.** A busy read never disables a lane
   except for a mismatch, never sets `revoked_epoch` or a latching
   `probe_status`, and never runs a heal turn.
4. **Liveness.** A busy, readable Codex lane is read on the first cycle it is
   due, whatever its occupancy (detached attempts, their leases, an admission
   probe, a conversation turn or its lease). A busy lane under a confirmed
   override is settled, and so measured, by its first read that is `ok` and
   open, is published within `reading_ttl_s` of being taken (`_fresh_usage`),
   and has no limit reported on the lane during it. In the incident no limit
   was reported on codex-4 before its override settled. Its slot cap is then
   the measured cap.
5. **Equivalence.** For `ok` and `limited` reads of the lane's own account, a
   busy read publishes the same readings, closures, settlement and verdict an
   idle read of the same answer would.
6. **Never read.** Desktop, disabled, non-v2, held and `auth-dead` lanes are not
   read, busy or idle.
7. **No older answer undoes a newer limit.** A closure recorded or extended on
   the lane during a busy read, or reported again by an attempt that ended
   `limited`, is still open after the read is published, and ends no sooner.
8. **One transaction.** A busy read's judgement, settlement and publication
   commit together or not at all, and a failed one puts the lane's verdict back.

`tools/busy_read_mutations.py` holds twenty mutations of the change, each
removing one rule. Each fails at least one test (run on both lines at this
revision).

## Relation to the reset-credit picker (PR #33)

The picker's candidate filter trusts the timer's verdict (`row['probe']`) and
checks no closure (#33, comment 5919734141). This change keeps busy lanes'
verdicts current, so a busy lane is no longer judged on an old `limited`, and a
credit spent on a lane that goes straight to work settles at the lane's first
open read instead of holding the lane unmeasured for up to seven days. It does not read
held lanes, so a held lane's verdict still goes stale. Keeping held lanes out
of the picker is #33's closure check.

## Changes since revision 1 (independent reviews of PR #96 and PR #97)

Two reviews ran on Subfleet lanes: Opus 5.5 on PR #96 and GPT-6.1 Sol on PR #97.

- **Judged and published in one transaction** (both reviews, reproduced; the
  port's review requested changes for it). Revision 1 judged a busy read and
  then published it in separate transactions, so an attempt's limit recorded in
  between was released. `_publish_busy` closes it.
- **A limit reported again** (review of #96, P2). `put_closure` writes nothing
  for a limit that ends no later than the open row's, so the closure fence
  missed it. The fence now includes the lane's attempts that ended `limited`.
- **A failed publication puts back the verdict the lane had** (P2), and that
  verdict is read under the store lock.
- **Whatever raises before the read is a withheld read**, not a failed cycle.
- **Publication asks what `_claim` asks**: enabled, v2-owned, not the desktop's.
  The account comparison is gone, since a lane's binding never changes.
- **Tests**: conversation-turn occupancy (a turn's attempt, a `slot:turn-<n>`
  lease), the reported-again limit in the property test, a second thread's
  limit waiting for the publication, and the mutation list in `tools/`.
- **Statements corrected**: what catches a dead credential on a busy lane; what
  is and is not established about a token refresh; the conditions on "settled
  by one read"; the earlier credits' settlement times; which of the four
  2026-10-01 cycles ran on 2.1.9.

Not taken: publishing a mismatch the moment its read returns, rather than with
the cycle. It would shorten the unfenced window by at most the cycle's length
and add a second publication path.
