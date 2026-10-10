--------------------------- MODULE MirrorActivity ---------------------------
(***************************************************************************)
(* The desktop sidebar mirror's date sync (subfleet/sessions/mirror.py,     *)
(* `activity_targets` and the publish in `sync_flags`), for one session.    *)
(*                                                                          *)
(* Each account folder holds a copy of the session's record, and in it the  *)
(* date the sidebar shows (lastActivityAt). The app writes that date only   *)
(* where it runs the session, so the other folders' copies keep the date    *)
(* they were copied with. The mirror raises them in the flag protocol's own *)
(* publish (MirrorFlags.tla), as a sequence of steps:                       *)
(*   PassDecide  reads every copy and decides which to raise: a copy more   *)
(*               than Lag behind the newest goes to one before the newest.  *)
(*               A pass may decide nothing for the session (PassDefer): it  *)
(*               is archived, or the pass's bound on sessions was spent on  *)
(*               others further behind;                                     *)
(*   PassCheck   is the pre-check. A date that moved since the read does    *)
(*               not hold the session; the check only drops a copy that     *)
(*               already holds the target or more;                          *)
(*   PassWrite   writes one copy, in path order. It fails if the app rewrote *)
(*               that copy since the pre-check; the copies already written  *)
(*               and not rewritten since are then put back to what the      *)
(*               pre-check read.                                            *)
(* What holds a session is a flag field that moved, or a copy that cannot   *)
(* be read; a held session writes nothing, which is a Cancel here.          *)
(*                                                                          *)
(* `also` is the set of copies the same publish writes for a flag's sake    *)
(* (arbitrary: the flag is MirrorFlags.tla's business). A write of one      *)
(* changes no date, but it can fail and put the batch back.                 *)
(*                                                                          *)
(* The app is modeled as in MirrorFlags.tla. Turn is the session running in *)
(* a folder the app holds: the date there becomes now, later than any date  *)
(* written before, so no date is ever past the clock (the code takes one    *)
(* that is for no voice, a case outside this module).                       *)
(* With StaleSaves = TRUE the app may save a date it held                   *)
(* from before the mirror raised the copy. Unlike a flag, that old date is  *)
(* no one's change: it lowers one copy, the mirror never spreads it, and    *)
(* the next pass raises the copy again.                                     *)
(*                                                                          *)
(* tests/mirror_activity_model.py is the executable twin of this module; it *)
(* is explored exhaustively by tests/unit/test_mirror_activity_model.py,    *)
(* and tests/unit/test_mirror_activity_stateful.py holds the implementation *)
(* to it and to the flag twin together. SANY and TLC 2.19 checked this      *)
(* module on 2026-10-10, in the reviews of PR #167;                         *)
(* docs/reports/2026-10-10-mirror-stale-dates.md has the runs. TLC is not   *)
(* part of CI.                                                              *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Accounts,     \* the account folders, numbered in path order, e.g. 1..3
          StaleSaves,   \* may an app save put back a date it held from before?
          Lag,          \* how far behind the newest a copy may be
          Jump,         \* a turn's date is this much later than any before it
          ClockMax,     \* the latest date a turn may write (bounds the model)
          None          \* a model value: no date

ASSUME /\ Accounts \subseteq Nat /\ Accounts # {}
       /\ Lag \in Nat /\ Lag >= 1
       /\ Jump \in Nat /\ Jump > Lag
       /\ ClockMax \in Nat
       /\ None \notin Nat

Dates == 0..ClockMax

VARIABLES
    act,       \* act[a]: the date in account a's file
    loaded,    \* the account folder the app has loaded
    mem,       \* mem[a]: the date the app holds for a's copy, or None
    clock,     \* the latest date any turn has written
    phase,     \* "idle", "decided" or "publishing"
    snap,      \* snap[a]: what the pass read
    target,    \* target[a]: the date the pass decided to raise a to, or None
    also,      \* the copies the publish also writes, for a flag
    checked,   \* checked[a]: what the pre-check read
    pending,   \* the copies still to write (while "publishing")
    written,   \* the copies this publish has written (while "publishing")
    touched,   \* the copies the app rewrote since the pre-check
    clean,     \* ghost: nothing was rewritten since the decision
    fresh      \* ghost: a pass left every copy within Lag, and no turn since

vars == <<act, loaded, mem, clock, phase, snap, target, also, checked, pending,
          written, touched, clean, fresh>>

Max(S) == CHOOSE x \in S : \A y \in S : y <= x
Newest(d) == Max({d[a] : a \in Accounts})
WithinLag(d) == \A a \in Accounts : Newest(d) - d[a] <= Lag
Leaders(d) == {a \in Accounts : d[a] = Newest(d)}

\* activity_targets: a copy more than Lag behind the newest is raised to one
\* before the newest.
Targets(d) == [a \in Accounts |-> IF Newest(d) - d[a] > Lag THEN Newest(d) - 1 ELSE None]

TypeOK ==
    /\ act \in [Accounts -> Dates]
    /\ loaded \in Accounts
    /\ mem \in [Accounts -> Dates \cup {None}]
    /\ clock \in Dates
    /\ phase \in {"idle", "decided", "publishing"}
    /\ snap \in [Accounts -> Dates]
    /\ target \in [Accounts -> Dates \cup {None}]
    /\ also \subseteq Accounts
    /\ checked \in [Accounts -> Dates]
    /\ pending \subseteq Accounts
    /\ written \subseteq Accounts
    /\ touched \subseteq Accounts
    /\ clean \in BOOLEAN
    /\ fresh \in BOOLEAN

Init ==
    /\ act = [a \in Accounts |-> 0]
    /\ loaded \in Accounts
    /\ mem = [a \in Accounts |-> IF a = loaded THEN 0 ELSE None]
    /\ clock = 0
    /\ phase = "idle"
    /\ snap = act
    /\ target = [a \in Accounts |-> None]
    /\ also = {}
    /\ checked = act
    /\ pending = {} /\ written = {} /\ touched = {}
    /\ clean = TRUE
    /\ fresh = FALSE

\* An app write to a's file: a publish in progress must not write over it.
Touch(a) ==
    /\ touched' = IF phase = "publishing" THEN touched \cup {a} ELSE touched
    /\ clean' = FALSE

\* The app loads another account's folder: it reads the file.
Load(a) ==
    /\ a # loaded
    /\ loaded' = a
    /\ mem' = [mem EXCEPT ![a] = act[a]]
    /\ UNCHANGED <<act, clock, phase, snap, target, also, checked, pending, written,
                   touched, clean, fresh>>

\* The session runs in a folder the app holds: the date there becomes now.
Turn(a) ==
    /\ mem[a] # None
    /\ clock + Jump <= ClockMax
    /\ clock' = clock + Jump
    /\ act' = [act EXCEPT ![a] = clock + Jump]
    /\ mem' = [mem EXCEPT ![a] = clock + Jump]
    /\ fresh' = FALSE
    /\ Touch(a)
    /\ UNCHANGED <<loaded, phase, snap, target, also, checked, pending, written>>

\* The app saves a record from memory. Only a stale memory changes the date.
AppSave(a) ==
    /\ StaleSaves
    /\ mem[a] # None
    /\ mem[a] # act[a]
    /\ act' = [act EXCEPT ![a] = mem[a]]
    /\ Touch(a)
    /\ UNCHANGED <<loaded, mem, clock, phase, snap, target, also, checked, pending,
                   written, fresh>>

\* An app save that keeps the date: it matters only to a publish in progress.
Focus(a) ==
    /\ phase = "publishing"
    /\ a \notin touched
    /\ touched' = touched \cup {a}
    /\ clean' = FALSE
    /\ UNCHANGED <<act, loaded, mem, clock, phase, snap, target, also, checked, pending,
                   written, fresh>>

Decide(goals) ==
    /\ phase = "idle"
    /\ phase' = "decided"
    /\ snap' = act
    /\ target' = goals
    /\ also' \in SUBSET Accounts
    /\ clean' = TRUE
    /\ UNCHANGED <<act, loaded, mem, clock, checked, pending, written, touched, fresh>>

PassDecide == Decide(Targets(act))
PassDefer == Decide([a \in Accounts |-> None])

\* The batch after the pre-check: a copy still below its target, or one a flag
\* is written to.
ToWrite == {a \in Accounts : target[a] # None /\ act[a] < target[a]} \cup also

Idle ==
    /\ phase' = "idle"
    /\ also' = {}
    /\ pending' = {} /\ written' = {} /\ touched' = {}

PassCheck ==
    /\ phase = "decided"
    /\ IF ToWrite = {}
       THEN /\ Idle
            /\ fresh' = (fresh \/ WithinLag(act))
            /\ UNCHANGED checked
       ELSE /\ phase' = "publishing"
            /\ checked' = act
            /\ pending' = ToWrite
            /\ written' = {}
            /\ touched' = {}
            /\ UNCHANGED <<also, fresh>>
    /\ UNCHANGED <<act, loaded, mem, clock, snap, target, clean>>

Target == CHOOSE a \in pending : \A b \in pending : a <= b
RollsBack == phase = "publishing" /\ Target \in touched
Raised(a) == IF target[a] # None /\ act[a] < target[a] THEN target[a] ELSE act[a]

PassWrite ==
    /\ phase = "publishing"
    /\ IF Target \in touched
       THEN /\ act' = [a \in Accounts |->
                          IF a \in written \ touched THEN checked[a] ELSE act[a]]
            /\ Idle
            /\ UNCHANGED fresh
       ELSE LET d == [act EXCEPT ![Target] = Raised(Target)] IN
            /\ act' = d
            /\ IF pending = {Target}
               THEN /\ Idle
                    /\ fresh' = (fresh \/ WithinLag(d))
               ELSE /\ pending' = pending \ {Target}
                    /\ written' = written \cup {Target}
                    /\ UNCHANGED <<phase, also, touched, fresh>>
    /\ UNCHANGED <<loaded, mem, clock, snap, target, checked, clean>>

\* A pass cancelled before it publishes, or a held session: nothing written.
Cancel ==
    /\ phase = "decided"
    /\ phase' = "idle"
    /\ also' = {}
    /\ UNCHANGED <<act, loaded, mem, clock, snap, target, checked, pending, written,
                   touched, clean, fresh>>

Next ==
    \/ \E a \in Accounts : Load(a) \/ Turn(a) \/ AppSave(a) \/ Focus(a)
    \/ PassDecide \/ PassDefer \/ PassCheck \/ PassWrite \/ Cancel

Spec == Init /\ [][Next]_vars

---------------------------------------------------------------------------
(* Properties. Each is also checked, step by step over every reachable      *)
(* state of the twin, by tests/unit/test_mirror_activity_model.py.          *)

\* A raise is a raise, and never to the newest date or past it: the copy the
\* app last ran the session in stays the only newest one.
RaiseBelowNewest ==
    [][(PassDecide \/ PassDefer) =>
         \A a \in Accounts : target'[a] # None
             => act[a] < target'[a] /\ target'[a] < Newest(act)]_vars

\* The same at the write: a later date the app saved since the read can make
\* the newest later, but nothing the mirror writes reaches the newest it read.
WriteBelowNewest ==
    [][PassWrite /\ ~RollsBack =>
         \A a \in Accounts : act'[a] # act[a] => act'[a] < Newest(snap)]_vars

\* Idempotence: with every copy within the lag, nothing is decided.
Idempotence ==
    [][(PassDecide \/ PassDefer) /\ WithinLag(act)
       => \A a \in Accounts : target'[a] = None]_vars

\* Bounded lag: a pass that decides for the session decides for every copy
\* more than the lag behind.
BoundedLag ==
    [][PassDecide => \A a \in Accounts : Newest(act) - act[a] > Lag
                         => target'[a] # None]_vars

\* Never lowered: a write that goes through lowers no copy.
NeverLowered ==
    [][PassWrite /\ ~RollsBack => \A a \in Accounts : act'[a] >= act[a]]_vars

\* The lead is kept: a write that goes through changes neither the newest
\* date nor which copies hold it. The mirror invents no date.
LeaderKept ==
    [][PassWrite /\ ~RollsBack
       => Newest(act') = Newest(act) /\ Leaders(act') = Leaders(act)]_vars

\* No lost update: the mirror writes only a copy nobody rewrote since its
\* pre-check (a pending copy) or since the mirror wrote it.
NoLostUpdate ==
    [][(PassCheck \/ PassWrite) =>
         \A a \in Accounts : act'[a] # act[a] => a \notin touched]_vars

\* All or nothing: a failed write puts back every copy the publish wrote,
\* except one rewritten since, to what the pre-check read; the pre-check and a
\* decision write nothing.
AllOrNothing ==
    /\ [][(PassDecide \/ PassDefer \/ PassCheck) => act' = act]_vars
    /\ [][PassWrite /\ RollsBack =>
            /\ \A a \in written \ touched : act'[a] = checked[a]
            /\ \A a \in Accounts \ written : act'[a] = act[a]]_vars

\* Cancellation safety: a cancelled pass, or a held session, changes no copy.
CancellationSafety == [][Cancel => act' = act]_vars

\* Convergence: a pass that decided for the session, with nothing rewritten
\* since its decision, ends with every copy within the lag. (The twin checks
\* the same by running one such pass from every idle state.)
ConvergesInOnePass ==
    [][(PassCheck \/ PassWrite) /\ clean /\ phase' = "idle" /\ target = Targets(snap)
       => WithinLag(act')]_vars

\* Stays fresh: once a pass left every copy within the lag, each stays there
\* until the session next runs. Expected to hold with StaleSaves = FALSE and
\* to fail with TRUE (the known limit), as the twin finds.
StaysFresh == [](fresh => WithinLag(act))
=============================================================================
