---------------------------- MODULE MirrorFlags ----------------------------
(***************************************************************************)
(* The desktop sidebar mirror's flag protocol (subfleet/sessions/mirror.py, *)
(* `sync_flags`), for one session and one boolean flag (isArchived;        *)
(* isStarred runs through the same loop with its own bootstrap value).      *)
(*                                                                          *)
(* Each account folder holds a copy of the session's record. The merge base *)
(* (mirror-flags.json) holds the last decision and each copy's reference:   *)
(* the value that decision gave the copy or read there. A copy votes only   *)
(* when it differs from its own reference, so the mirror's own writes never *)
(* vote (review round 5, finding F2). A pass is a sequence of steps,        *)
(* because the app can write between any two:                              *)
(*   PassRecover resolves a publish record a failed or crashed pass left;   *)
(*   PassDecide  reads every copy and decides;                              *)
(*   PassCheck   is the pre-check: if a copy the pass would write no longer *)
(*               holds what it read, nothing is written or recorded;        *)
(*               otherwise the publish record is saved (the write-ahead     *)
(*               record, mirror-publish.jsonl) and the publish starts;      *)
(*   PassWrite   writes one copy, in path order. It fails if the app or the *)
(*               user rewrote that copy since the pre-check; the rollback   *)
(*               then starts;                                               *)
(*   RollbackWrite puts one written copy back, newest first, unless it was  *)
(*               rewritten since the mirror's write;                        *)
(*   PassCommit  writes the merge base: the record, resolved by what landed. *)
(* The code renames a prepared temporary into place for each write, and the *)
(* record names it: a temporary still standing proves the rename never ran, *)
(* so `landed` and `restored` are facts on disk, and they survive a crash.  *)
(* A pass can be cancelled only between PassDecide and PassCheck: the code  *)
(* has no cancellation point inside the publish. A pass that cannot read    *)
(* every copy holds the session, which is a Cancel here.                    *)
(*                                                                          *)
(* Faults (Faults = TRUE): a put-back write that raises                     *)
(* (RollbackWriteFails), a merge-base write that raises (CommitFails), and  *)
(* a crash after the pre-check (Crash). The record outlives each one.       *)
(*                                                                          *)
(* The app holds in memory the record of the folder it loaded (as it was on *)
(* disk at that load) and of folders where a session still runs from an     *)
(* earlier account. A user action changes the loaded folder's copy and the  *)
(* app's memory; an app save writes memory; Focus is an app save that keeps *)
(* the flag. With StaleSaves = FALSE the app never saves a flag that        *)
(* differs from its file; with TRUE it may (the known limit in              *)
(* docs/reports/2026-09-24-mirror-load-gap.md).                             *)
(*                                                                          *)
(* Two ghosts say what the user wants. `intent` is the 2026-09-25 one: what *)
(* the user set since the last publish that converged, or Conflict once     *)
(* they set both values. `latest` is causal: the value of the user's latest *)
(* action, unless a value the user had not seen when they acted still       *)
(* stands against it (Conflict). Knowledge follows values: `sup[a]` is the  *)
(* set of user actions copy a's value derives from and `know[a]` those it   *)
(* knows of; a value the mirror writes derives from the copies it read that *)
(* held the decided value. The bootstrap rule (archived-anywhere, with no   *)
(* base) may override the user by design: both ghosts are checked only by a *)
(* pass with a base, a bootstrap decision clears `intent`, `latest` guards  *)
(* only actions taken with a base, and a bootstrap decision is itself an    *)
(* actor, known only where its writes are seen.                             *)
(*                                                                          *)
(* Not modeled: an app rename landing between the code's last signature     *)
(* check and its own rename, a window of one syscall.                       *)
(*                                                                          *)
(* tests/mirror_flags_model.py is the executable twin of this module; it is *)
(* explored exhaustively by tests/unit/test_mirror_flags_model.py (the      *)
(* causal ghost to a state limit), and                                      *)
(* tests/unit/test_mirror_flags_stateful.py holds the implementation to it. *)
(* TLC has not been run on this module (Max, 2026-09-25: skip it for now).  *)
(* The action ids behind `latest` are unbounded; TLC would need a view that *)
(* renumbers them, as the twin's _canon does.                               *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Accounts,     \* the account folders, numbered in path order, e.g. 1..3
          StaleSaves,   \* may an app save put back a value it held from before?
          Faults,       \* may a put-back or base write raise, or the process crash?
          None,         \* a model value: no value yet
          Conflict      \* a model value: the user's wishes cannot be ordered

ASSUME /\ Accounts \subseteq Nat /\ Accounts # {}
       /\ None \notin BOOLEAN /\ Conflict \notin BOOLEAN /\ None # Conflict

Values == BOOLEAN
Phases == {"idle", "decided", "publishing", "rolling", "committing"}

VARIABLES
    copy,      \* copy[a]: the flag in account a's file
    base,      \* the merge base: the last decision, or None
    ref,       \* ref[a]: copy a's reference (None with no base)
    loaded,    \* the account folder the app has loaded
    mem,       \* mem[a]: the app's in-memory flag for a's copy, or None
    phase,
    snap,      \* snap[a]: what the pass read
    decided,   \* the value the pass decided
    pending,   \* the copies still to write (while "publishing")
    written,   \* the copies this publish has written
    back,      \* the written copies still to put back (while "rolling")
    touched,   \* the copies the app or user rewrote since the pre-check
    failed,    \* a write found its copy rewritten: the publish did not go through
    wal,       \* the publish record, or None
    landed,    \* the record's targets whose rename ran (a fact on disk)
    restored,  \* the record's targets put back since (a fact on disk)
    mine,      \* ghost: the copies whose file is the mirror's own last write
    smine,     \* ghost: mine at the snapshot
    clean,     \* ghost: nothing was rewritten since PassDecide
    settled,   \* ghost: the value a clean publish converged to, or None
    intent,    \* ghost: what the user set since the last converging publish
    latest,    \* ghost, causal: the value of the user's latest action
    sup, know, \* ghost provenance per copy
    dsup, dknow, ssup, sknow,  \* ghost provenance of the decision and the snapshot
    nextId     \* ghost: the next action id

vars == <<copy, base, ref, loaded, mem, phase, snap, decided, pending, written, back,
          touched, failed, wal, landed, restored, mine, smine, clean, settled, intent,
          latest, sup, know, dsup, dknow, ssup, sknow, nextId>>

Ghosts == <<mine, smine, clean, settled, intent, latest, sup, know, dsup, dknow, ssup,
            sknow, nextId>>

TypeOK ==
    /\ copy \in [Accounts -> Values]
    /\ base \in Values \cup {None}
    /\ ref \in [Accounts -> Values \cup {None}]
    /\ loaded \in Accounts
    /\ mem \in [Accounts -> Values \cup {None}]
    /\ phase \in Phases
    /\ snap \in [Accounts -> Values]
    /\ decided \in Values
    /\ pending \subseteq Accounts /\ written \subseteq Accounts /\ back \subseteq Accounts
    /\ touched \subseteq Accounts /\ failed \in BOOLEAN
    /\ landed \subseteq Accounts /\ restored \subseteq Accounts
    /\ mine \subseteq Accounts /\ smine \subseteq Accounts
    /\ clean \in BOOLEAN
    /\ settled \in Values \cup {None}
    /\ intent \in Values \cup {None, Conflict}
    /\ latest \in Values \cup {None, Conflict}
    /\ sup \in [Accounts -> SUBSET Nat] /\ know \in [Accounts -> SUBSET Nat]
    /\ nextId \in Nat

\* sync_flags (`_decide_flag`): a copy votes when it differs from its
\* reference; votes that agree win; with no vote the last decision stands;
\* votes that disagree fall back to the change from it, and with no base
\* archived-anywhere (TRUE) wins (v1's rule for the historical backlog).
Decide(c, r, b) ==
    LET votes == {c[a] : a \in {x \in Accounts : c[x] # r[x]}} IN
    IF votes = {} THEN b
    ELSE IF Cardinality(votes) = 1 THEN CHOOSE v \in votes : TRUE
    ELSE IF b = None THEN TRUE ELSE ~b

\* `_resolve_publish`: the record a publish leaves, from what its files show
\* now. None reached (none landed, or every one put back): nothing changes.
\* Otherwise the decision stands, and a target it did not reach keeps what its
\* file holds as its reference (the value read there, or the decided one).
Reached(w) == {t \in w.targets : t \in landed /\ t \notin restored}
Resolve(w) ==
    IF w.targets # {} /\ Reached(w) = {}
    THEN [base |-> w.base, ref |-> w.ref]
    ELSE [base |-> w.decided,
          ref |-> [a \in Accounts |-> IF a \in w.targets \ Reached(w) THEN copy[a]
                                      ELSE w.decided]]
WentThrough(w) == Reached(w) = w.targets

Init ==
    /\ \E v \in Values : copy = [a \in Accounts |-> v]
    /\ base = None /\ ref = [a \in Accounts |-> None]
    /\ loaded \in Accounts
    /\ mem = [a \in Accounts |-> IF a = loaded THEN copy[a] ELSE None]
    /\ phase = "idle" /\ snap = copy /\ decided = FALSE
    /\ pending = {} /\ written = {} /\ back = {} /\ touched = {} /\ failed = FALSE
    /\ wal = None /\ landed = {} /\ restored = {}
    /\ mine = {} /\ smine = {} /\ clean = TRUE /\ settled = None
    /\ intent = None /\ latest = None
    /\ sup = [a \in Accounts |-> {}] /\ know = [a \in Accounts |-> {}]
    /\ dsup = {} /\ dknow = {} /\ ssup = sup /\ sknow = know
    /\ nextId = 0

InFlight == phase \in {"publishing", "rolling", "committing"}

\* An app or user write to a's file: a publish in progress must not write
\* over it, and the file is no longer the mirror's own write.
Touch(a) ==
    /\ touched' = IF InFlight THEN touched \cup {a} ELSE touched
    /\ mine' = mine \ {a}
    /\ clean' = FALSE

Load(a) ==
    /\ a # loaded
    /\ loaded' = a
    /\ mem' = [mem EXCEPT ![a] = copy[a]]
    /\ UNCHANGED <<copy, base, ref, phase, snap, decided, pending, written, back, touched,
                   failed, wal, landed, restored>>
    /\ UNCHANGED Ghosts

\* The values that could still stand against a user's action setting v: other
\* copies holding the other value, a decision still to be written, and a value
\* a rollback could put back.
Against(v) ==
    {sup[c] : c \in {x \in Accounts \ {loaded} : copy[x] # v}}
    \cup (IF phase \in {"decided", "publishing"} /\ decided # v THEN {dsup} ELSE {})
    \cup (IF phase = "publishing" THEN {ssup[b] : b \in {x \in written : snap[x] # v}}
          ELSE IF phase = "rolling" THEN {ssup[b] : b \in {x \in back : snap[x] # v}}
          ELSE {})

UserSet(v) ==
    /\ copy[loaded] # v
    /\ LET knows == know[loaded] \cup {nextId} IN
       /\ latest' = IF base = None THEN None
                    ELSE IF \A s \in Against(v) : s \subseteq knows THEN v ELSE Conflict
       /\ sup' = [sup EXCEPT ![loaded] = {nextId}]
       /\ know' = [know EXCEPT ![loaded] = knows]
    /\ nextId' = nextId + 1
    /\ copy' = [copy EXCEPT ![loaded] = v]
    /\ mem' = [mem EXCEPT ![loaded] = v]
    /\ settled' = None
    /\ intent' = IF intent \in {None, v} THEN v ELSE Conflict
    /\ Touch(loaded)
    /\ UNCHANGED <<base, ref, loaded, phase, snap, decided, pending, written, back, failed,
                   wal, landed, restored, smine, dsup, dknow, ssup, sknow>>

\* The app saves a record from memory. Only a stale memory changes the flag;
\* its value comes from before the mirror's write, so no one knows of it.
AppSave(a) ==
    /\ StaleSaves
    /\ mem[a] # None
    /\ mem[a] # copy[a]
    /\ copy' = [copy EXCEPT ![a] = mem[a]]
    /\ sup' = [sup EXCEPT ![a] = {nextId}] /\ know' = [know EXCEPT ![a] = {nextId}]
    /\ nextId' = nextId + 1
    /\ Touch(a)
    /\ UNCHANGED <<base, ref, loaded, mem, phase, snap, decided, pending, written, back,
                   failed, wal, landed, restored, smine, settled, intent, latest, dsup,
                   dknow, ssup, sknow>>

\* An app save that keeps the flag (a focus or activity update).
Focus(a) ==
    /\ a \in mine \/ (phase \in {"publishing", "rolling"} /\ a \notin touched)
    /\ Touch(a)
    /\ UNCHANGED <<copy, base, ref, loaded, mem, phase, snap, decided, pending, written,
                   back, failed, wal, landed, restored, smine, settled, intent, latest,
                   sup, know, dsup, dknow, ssup, sknow, nextId>>

\* The merge base is written. Finish: a publish went through, so a converged
\* state settles and the intent since the last convergence is delivered.
Record(b, r, finish) ==
    /\ base' = b /\ ref' = r
    /\ wal' = None /\ landed' = {} /\ restored' = {}
    /\ settled' = IF finish THEN (IF \A a \in Accounts : copy[a] = b THEN b ELSE None)
                  ELSE settled
    /\ intent' = IF finish /\ \A a \in Accounts : copy[a] = b THEN None ELSE intent

Idle ==
    /\ phase' = "idle" /\ pending' = {} /\ written' = {} /\ back' = {}
    /\ touched' = {} /\ failed' = FALSE /\ smine' = {}

PassRecover ==
    /\ phase = "idle" /\ wal # None
    /\ Record(Resolve(wal).base, Resolve(wal).ref, WentThrough(wal))
    /\ UNCHANGED <<copy, loaded, mem, phase, snap, decided, pending, written, back, touched,
                   failed, mine, smine, clean, latest, sup, know, dsup, dknow, ssup, sknow,
                   nextId>>

PassDecide ==
    /\ phase = "idle" /\ wal = None
    /\ LET d == Decide(copy, ref, base)
           supporters == {a \in Accounts : copy[a] = d}
           s == UNION {sup[a] : a \in supporters}
           k == UNION {know[a] : a \in supporters} IN
       /\ decided' = d
       /\ dsup' = IF base = None THEN s \cup {nextId} ELSE s
       /\ dknow' = IF base = None THEN k \cup {nextId} ELSE k
       /\ nextId' = IF base = None THEN nextId + 1 ELSE nextId
    /\ phase' = "decided" /\ snap' = copy /\ smine' = mine
    /\ ssup' = sup /\ sknow' = know
    /\ intent' = IF base = None THEN None ELSE intent
    /\ clean' = TRUE
    /\ UNCHANGED <<copy, base, ref, loaded, mem, pending, written, back, touched, failed,
                   wal, landed, restored, mine, settled, latest, sup, know>>

Dirty == {a \in Accounts : snap[a] # decided}
Held == \E a \in Dirty : copy[a] # snap[a]

PassCheck ==
    /\ phase = "decided"
    /\ IF Held
       THEN /\ Idle
            /\ UNCHANGED <<base, ref, wal, landed, restored, settled, intent>>
       ELSE IF Dirty = {}
            THEN /\ Idle
                 /\ Record(decided, [a \in Accounts |-> decided], TRUE)
            ELSE /\ phase' = "publishing" /\ pending' = Dirty /\ written' = {}
                 /\ back' = {} /\ touched' = {} /\ failed' = FALSE
                 /\ wal' = [decided |-> decided, targets |-> Dirty, ref |-> ref,
                            base |-> base]
                 /\ landed' = {} /\ restored' = {}
                 /\ UNCHANGED <<base, ref, settled, intent, smine>>
    /\ UNCHANGED <<copy, loaded, mem, snap, decided, mine, clean, latest, sup, know, dsup,
                   dknow, ssup, sknow, nextId>>

Target == CHOOSE a \in pending : \A b \in pending : a <= b

PassWrite ==
    /\ phase = "publishing"
    /\ IF Target \in touched
       THEN /\ phase' = IF written = {} THEN "committing" ELSE "rolling"
            /\ back' = written /\ pending' = {} /\ failed' = TRUE
            /\ UNCHANGED <<copy, written, landed, mine, sup, know>>
       ELSE /\ copy' = [copy EXCEPT ![Target] = decided]
            /\ sup' = [sup EXCEPT ![Target] = dsup] /\ know' = [know EXCEPT ![Target] = dknow]
            /\ landed' = landed \cup {Target}
            /\ mine' = mine \cup {Target}
            /\ written' = written \cup {Target}
            /\ pending' = pending \ {Target}
            /\ phase' = IF pending = {Target} THEN "committing" ELSE "publishing"
            /\ UNCHANGED <<back, failed>>
    /\ UNCHANGED <<base, ref, loaded, mem, snap, decided, touched, wal, restored, smine,
                   clean, settled, intent, latest, dsup, dknow, ssup, sknow, nextId>>

BackTarget == CHOOSE a \in back : \A b \in back : a >= b

\* Put back the newest written copy, unless it was rewritten since the
\* mirror's write; `fails`: the put-back raises and the mirror's write stands.
PutBack(fails) ==
    /\ phase = "rolling"
    /\ back' = back \ {BackTarget}
    /\ phase' = IF back = {BackTarget} THEN "committing" ELSE "rolling"
    /\ IF fails \/ BackTarget \in touched
       THEN UNCHANGED <<copy, restored, mine, sup, know>>
       ELSE /\ copy' = [copy EXCEPT ![BackTarget] = snap[BackTarget]]
            /\ restored' = restored \cup {BackTarget}
            /\ mine' = IF BackTarget \in smine THEN mine \cup {BackTarget}
                       ELSE mine \ {BackTarget}
            /\ sup' = [sup EXCEPT ![BackTarget] = ssup[BackTarget]]
            /\ know' = [know EXCEPT ![BackTarget] = sknow[BackTarget]]
    /\ UNCHANGED <<base, ref, loaded, mem, snap, decided, pending, written, touched, failed,
                   wal, landed, smine, clean, settled, intent, latest, dsup, dknow, ssup,
                   sknow, nextId>>

RollbackWrite == PutBack(FALSE)
RollbackWriteFails == Faults /\ PutBack(TRUE)

PassCommit ==
    /\ phase = "committing"
    /\ Idle
    /\ Record(Resolve(wal).base, Resolve(wal).ref, WentThrough(wal))
    /\ UNCHANGED <<copy, loaded, mem, snap, decided, mine, clean, latest, sup, know, dsup,
                   dknow, ssup, sknow, nextId>>

\* The merge-base write raises, or the process dies: what is on disk stays.
CommitFails ==
    /\ Faults /\ phase = "committing" /\ Idle
    /\ UNCHANGED <<copy, base, ref, loaded, mem, snap, decided, wal, landed, restored>>
    /\ UNCHANGED <<mine, clean, settled, intent, latest, sup, know, dsup, dknow, ssup,
                   sknow, nextId>>
Crash ==
    /\ Faults /\ InFlight /\ Idle
    /\ UNCHANGED <<copy, base, ref, loaded, mem, snap, decided, wal, landed, restored>>
    /\ UNCHANGED <<mine, clean, settled, intent, latest, sup, know, dsup, dknow, ssup,
                   sknow, nextId>>

Cancel ==
    /\ phase = "decided" /\ Idle
    /\ UNCHANGED <<copy, base, ref, loaded, mem, snap, decided, wal, landed, restored>>
    /\ UNCHANGED <<mine, clean, settled, intent, latest, sup, know, dsup, dknow, ssup,
                   sknow, nextId>>

Next ==
    \/ \E a \in Accounts : Load(a) \/ AppSave(a) \/ Focus(a)
    \/ \E v \in Values : UserSet(v)
    \/ PassRecover \/ PassDecide \/ PassCheck \/ PassWrite \/ RollbackWrite
    \/ PassCommit \/ Cancel
    \/ RollbackWriteFails \/ CommitFails \/ Crash

Spec == Init /\ [][Next]_vars

---------------------------------------------------------------------------
(* Properties. Each is also checked, step by step over every reachable      *)
(* state of the twin, by tests/unit/test_mirror_flags_model.py.             *)

Converged == \A a \in Accounts : copy[a] = base /\ ref[a] = base
Moved == {copy[a] : a \in {x \in Accounts : copy[x] # ref[x]}}

\* Idempotence: a converged state with its base decides itself.
Idempotence ==
    [][PassDecide /\ base \in Values /\ Converged => decided' = base]_vars

\* Change wins, both ways: when every copy that differs from its reference
\* holds the same value, that value is decided.
ChangeWins ==
    [][PassDecide /\ base \in Values /\ Cardinality(Moved) = 1 => decided' \in Moved]_vars

\* No lost update: the mirror writes only a copy nobody rewrote since it last
\* checked it (a pending copy) or wrote it (a written one); resolving, failing
\* and crashing write no copy at all.
NoLostUpdate ==
    [][(PassCheck \/ PassWrite \/ RollbackWrite \/ RollbackWriteFails \/ PassCommit
        \/ PassRecover \/ CommitFails \/ Crash) =>
         \A a \in Accounts : copy'[a] # copy[a] =>
             /\ a \notin touched
             /\ (copy[a] = snap[a] \/ a \in written)]_vars

\* The record comes first: no write moves the base or a reference, and a
\* publish starts only once its record is saved.
RecordBeforeWrites ==
    /\ [][(PassWrite \/ RollbackWrite \/ RollbackWriteFails) => base' = base /\ ref' = ref]_vars
    /\ [][PassCheck /\ phase' = "publishing" => wal' # None /\ base' = base /\ ref' = ref]_vars

\* All or nothing: a held session writes and records nothing; a put-back
\* restores its copy unless the copy was rewritten since; a publish every
\* write of which was put back keeps the base and every reference.
AllOrNothing ==
    /\ [][PassCheck /\ Held => copy' = copy /\ base' = base /\ ref' = ref /\ wal' = wal]_vars
    /\ [][RollbackWrite /\ BackTarget \notin touched => copy'[BackTarget] = snap[BackTarget]]_vars
    /\ [][PassCommit /\ failed /\ Reached(wal) = {} => base' = base /\ ref' = ref]_vars

\* Base agreement: after a publish that went through, the base and every
\* reference are the value decided, and every copy the pass read differently
\* holds it unless the app or the user rewrote it since.
BaseAgreement ==
    [][((PassCheck /\ ~Held /\ Dirty = {}) \/ (PassCommit /\ ~failed))
       => /\ base' = decided
          /\ \A a \in Accounts : ref'[a] = decided
          /\ \A a \in Dirty \ touched : copy'[a] = decided]_vars

\* The mirror's own writes do not vote (F2): a copy whose file is the
\* mirror's own last write holds its reference.
OwnWritesDoNotVote ==
    [][PassDecide /\ base \in Values => \A a \in mine : copy[a] = ref[a]]_vars

\* Intent wins: with a base, if the user set only one value since the last
\* publish that converged, a pass decides that value; a user who set both is
\* exempt (Conflict). Expected to fail with StaleSaves = TRUE.
IntentWins ==
    [][PassDecide /\ base \in Values /\ intent \in Values => decided' = intent]_vars

\* Latest wins: with a base, a pass decides the value of the user's latest
\* action unless a value they had not seen stands against it. This is what F2
\* broke. Expected to fail with StaleSaves = TRUE.
LatestWins ==
    [][PassDecide /\ base \in Values /\ latest \in Values => decided' = latest]_vars

\* Cancellation safety: a cancelled pass changes no copy, base or reference.
CancellationSafety ==
    [][Cancel => copy' = copy /\ base' = base /\ ref' = ref]_vars

\* Never undo a settled value: once a clean publish converged every copy and
\* no user has acted since, no pass writes any other value.
NeverUndoSettled ==
    [][(PassWrite \/ RollbackWrite \/ RollbackWriteFails) /\ settled # None
       => \A a \in Accounts : copy'[a] # copy[a] => copy'[a] = settled]_vars

\* Convergence: a pass with nothing rewritten since its decision ends with
\* every copy, the base and every reference equal to the value decided. (The
\* twin checks the same by running one such pass from every idle state.)
ConvergesInOnePass ==
    [][PassCommit /\ clean /\ ~failed
       => /\ \A a \in Accounts : copy'[a] = decided
          /\ base' = decided /\ \A a \in Accounts : ref'[a] = decided]_vars
=============================================================================
