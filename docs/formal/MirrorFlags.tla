---------------------------- MODULE MirrorFlags ----------------------------
(***************************************************************************)
(* The desktop sidebar mirror's flag protocol (subfleet/sessions/mirror.py, *)
(* `sync_flags`), for one session and one boolean flag (isArchived;        *)
(* isStarred runs through the same loop with its own bootstrap value).      *)
(*                                                                          *)
(* Each account folder holds a copy of the session's record. The merge base *)
(* (mirror-flags.json) holds the last synced value. A pass is a sequence of *)
(* steps, because the app can write between any two:                       *)
(*   PassDecide  reads every copy and decides by the merge base;            *)
(*   PassCheck   is the pre-check: if a copy the pass would write no longer *)
(*               holds what it read, nothing is written and the base kept;  *)
(*   PassWrite   writes one copy, in path order. It fails if the app or the *)
(*               user rewrote that copy since the pre-check; the copies     *)
(*               already written and not rewritten since are then put back, *)
(*               and the base is kept. After the last write the base        *)
(*               advances.                                                  *)
(* A pass can be cancelled only between PassDecide and PassCheck: the code  *)
(* has no cancellation point inside the publish. A pass that cannot read    *)
(* every copy holds the session (writes nothing, keeps the base), which is *)
(* a Cancel here.                                                          *)
(*                                                                          *)
(* The app holds in memory the record of the folder it loaded (as it was on *)
(* disk at that load) and of folders where a session still runs from an     *)
(* earlier account. A user action changes the loaded folder's copy and the  *)
(* app's memory; an app save writes memory; Focus is an app save that keeps *)
(* the flag. With StaleSaves = FALSE the app never saves a flag that        *)
(* differs from its file; with TRUE it may (the known limit in              *)
(* docs/reports/2026-09-24-mirror-load-gap.md).                             *)
(*                                                                          *)
(* Not modeled: an app rename landing between the code's last signature     *)
(* check and its own rename, a window of one syscall.                       *)
(*                                                                          *)
(* tests/mirror_flags_model.py is the executable twin of this module; it is *)
(* explored exhaustively by tests/unit/test_mirror_flags_model.py, and      *)
(* tests/unit/test_mirror_flags_stateful.py holds the implementation to it. *)
(* TLC has not been run on this module (Max, 2026-09-25: skip it for now;   *)
(* it can join CI later).                                                   *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Accounts,     \* the account folders, numbered in path order, e.g. 1..3
          StaleSaves,   \* may an app save put back a value it held from before?
          None,         \* a model value: no value yet
          Conflict      \* a model value: the user set both values

ASSUME /\ Accounts \subseteq Nat /\ Accounts # {}
       /\ None \notin BOOLEAN /\ Conflict \notin BOOLEAN /\ None # Conflict

Values == BOOLEAN

VARIABLES
    copy,      \* copy[a]: the flag in account a's file
    base,      \* the merge base: the last synced value, or None
    loaded,    \* the account folder the app has loaded
    mem,       \* mem[a]: the app's in-memory flag for a's copy, or None
    phase,     \* "idle", "decided" or "publishing"
    snap,      \* snap[a]: what the pass read
    decided,   \* the value the pass decided
    pending,   \* the copies still to write (while "publishing")
    written,   \* the copies this publish has written (while "publishing")
    touched,   \* the copies the app or user rewrote since the pre-check
    clean,     \* ghost: nothing was rewritten since PassDecide
    settled,   \* ghost: the value a clean publish converged to, or None
    intent     \* ghost: what the user set since the last converging publish

vars == <<copy, base, loaded, mem, phase, snap, decided, pending, written, touched,
          clean, settled, intent>>

TypeOK ==
    /\ copy \in [Accounts -> Values]
    /\ base \in Values \cup {None}
    /\ loaded \in Accounts
    /\ mem \in [Accounts -> Values \cup {None}]
    /\ phase \in {"idle", "decided", "publishing"}
    /\ snap \in [Accounts -> Values]
    /\ decided \in Values
    /\ pending \subseteq Accounts
    /\ written \subseteq Accounts
    /\ touched \subseteq Accounts
    /\ clean \in BOOLEAN
    /\ settled \in Values \cup {None}
    /\ intent \in Values \cup {None, Conflict}

\* sync_flags: agreement wins; otherwise the change from the base wins; with no
\* base yet, archived-anywhere (TRUE) wins (v1's rule for the historical backlog).
Decide(c, b) ==
    LET vals == {c[a] : a \in Accounts} IN
    IF Cardinality(vals) = 1 THEN CHOOSE v \in vals : TRUE
    ELSE IF b = None THEN TRUE ELSE ~b

Init ==
    /\ \E v \in Values : copy = [a \in Accounts |-> v]
    /\ base = None
    /\ loaded \in Accounts
    /\ mem = [a \in Accounts |-> IF a = loaded THEN copy[a] ELSE None]
    /\ phase = "idle"
    /\ snap = copy
    /\ decided = FALSE
    /\ pending = {} /\ written = {} /\ touched = {}
    /\ clean = TRUE
    /\ settled = None
    /\ intent = None

\* An app or user write to a's file: a publish in progress must not write over it.
Touch(a) ==
    /\ touched' = IF phase = "publishing" THEN touched \cup {a} ELSE touched
    /\ clean' = FALSE

\* The app loads another account's folder: it reads the file.
Load(a) ==
    /\ a # loaded
    /\ loaded' = a
    /\ mem' = [mem EXCEPT ![a] = copy[a]]
    /\ UNCHANGED <<copy, base, phase, snap, decided, pending, written, touched, clean,
                   settled, intent>>

\* The user changes the flag in the loaded account's sidebar.
UserSet(v) ==
    /\ copy[loaded] # v
    /\ copy' = [copy EXCEPT ![loaded] = v]
    /\ mem' = [mem EXCEPT ![loaded] = v]
    /\ settled' = None
    /\ intent' = IF intent \in {None, v} THEN v ELSE Conflict
    /\ Touch(loaded)
    /\ UNCHANGED <<base, loaded, phase, snap, decided, pending, written>>

\* The app saves a record from memory. Only a stale memory changes the flag.
AppSave(a) ==
    /\ StaleSaves
    /\ mem[a] # None
    /\ mem[a] # copy[a]
    /\ copy' = [copy EXCEPT ![a] = mem[a]]
    /\ Touch(a)
    /\ UNCHANGED <<base, loaded, mem, phase, snap, decided, pending, written, settled,
                   intent>>

\* An app save that keeps the flag (a focus or activity update). It changes
\* nothing the protocol reads, except that a publish must not write over it.
Focus(a) ==
    /\ phase = "publishing"
    /\ a \notin touched
    /\ touched' = touched \cup {a}
    /\ clean' = FALSE
    /\ UNCHANGED <<copy, base, loaded, mem, phase, snap, decided, pending, written,
                   settled, intent>>

PassDecide ==
    /\ phase = "idle"
    /\ phase' = "decided"
    /\ snap' = copy
    /\ decided' = Decide(copy, base)
    /\ clean' = TRUE
    /\ UNCHANGED <<copy, base, loaded, mem, pending, written, touched, settled, intent>>

Dirty == {a \in Accounts : snap[a] # decided}
Held == \E a \in Dirty : copy[a] # snap[a]

\* A publish that went through: the base advances.
Finish(c) ==
    /\ phase' = "idle"
    /\ base' = decided
    /\ settled' = IF \A a \in Accounts : c[a] = decided THEN decided ELSE None
    /\ intent' = IF \A a \in Accounts : c[a] = decided THEN None ELSE intent
    /\ pending' = {} /\ written' = {} /\ touched' = {}

PassCheck ==
    /\ phase = "decided"
    /\ IF Held
       THEN /\ phase' = "idle"
            /\ UNCHANGED <<base, settled, intent, pending, written, touched>>
       ELSE IF Dirty = {}
            THEN Finish(copy)
            ELSE /\ phase' = "publishing"
                 /\ pending' = Dirty
                 /\ written' = {}
                 /\ touched' = {}
                 /\ UNCHANGED <<base, settled, intent>>
    /\ UNCHANGED <<copy, loaded, mem, snap, decided, clean>>

Target == CHOOSE a \in pending : \A b \in pending : a <= b
RollsBack == phase = "publishing" /\ Target \in touched

PassWrite ==
    /\ phase = "publishing"
    /\ IF Target \in touched
       THEN /\ copy' = [a \in Accounts |->
                           IF a \in written \ touched THEN snap[a] ELSE copy[a]]
            /\ phase' = "idle"
            /\ pending' = {} /\ written' = {} /\ touched' = {}
            /\ UNCHANGED <<base, settled, intent>>
       ELSE LET c == [copy EXCEPT ![Target] = decided] IN
            /\ copy' = c
            /\ IF pending = {Target}
               THEN Finish(c)
               ELSE /\ pending' = pending \ {Target}
                    /\ written' = written \cup {Target}
                    /\ UNCHANGED <<phase, base, settled, touched, intent>>
    /\ UNCHANGED <<loaded, mem, snap, decided, clean>>

Cancel ==
    /\ phase = "decided"
    /\ phase' = "idle"
    /\ UNCHANGED <<copy, base, loaded, mem, snap, decided, pending, written, touched,
                   clean, settled, intent>>

Next ==
    \/ \E a \in Accounts : Load(a) \/ AppSave(a) \/ Focus(a)
    \/ \E v \in Values : UserSet(v)
    \/ PassDecide \/ PassCheck \/ PassWrite \/ Cancel

Spec == Init /\ [][Next]_vars

---------------------------------------------------------------------------
(* Properties. Each is also checked, step by step over every reachable      *)
(* state of the twin, by tests/unit/test_mirror_flags_model.py.             *)

\* Idempotence: a converged state with its base decides itself.
Idempotence ==
    [][PassDecide /\ base \in Values /\ (\A a \in Accounts : copy[a] = base)
       => decided' = base]_vars

\* Change wins, both ways: when every copy that differs from the base holds
\* the same value, that value is decided.
ChangeWins ==
    [][PassDecide /\ base \in Values
         /\ Cardinality({copy[a] : a \in {x \in Accounts : copy[x] # base}}) = 1
       => decided' # base]_vars

\* No lost update: the mirror writes only a copy nobody rewrote since it last
\* checked it (a pending copy) or wrote it (a written one).
NoLostUpdate ==
    [][(PassCheck \/ PassWrite) =>
         \A a \in Accounts : copy'[a] # copy[a] =>
             /\ a \notin touched
             /\ (copy[a] = snap[a] \/ a \in written)]_vars

\* All or nothing: a held session writes nothing and keeps its base; a failed
\* write puts back every copy the publish wrote, except one rewritten since,
\* and keeps the base.
AllOrNothing ==
    /\ [][PassCheck /\ Held => copy' = copy /\ base' = base]_vars
    /\ [][PassWrite /\ RollsBack =>
            /\ base' = base
            /\ \A a \in written \ touched : copy'[a] = snap[a]
            /\ \A a \in Accounts \ written : copy'[a] = copy[a]]_vars

\* Base agreement: after a publish that went through, the base is the value
\* decided and every copy the pass read differently holds it, unless the app
\* or the user rewrote it since.
BaseAgreement ==
    [][((PassCheck /\ ~Held /\ Dirty = {})
        \/ (PassWrite /\ ~RollsBack /\ pending = {Target}))
       => /\ base' = decided
          /\ \A a \in Dirty \ touched : copy'[a] = decided]_vars

\* Intent wins: with a base, a pass decides what the user last set since the
\* last publish that converged. This is "no resurrection" for a user's change,
\* and unlike NeverUndoSettled it is exercised with an honest app (a user
\* action clears settled). Expected to fail with StaleSaves = TRUE.
IntentWins ==
    [][PassDecide /\ base \in Values /\ intent \in Values => decided' = intent]_vars

\* Cancellation safety: a cancelled pass changes no copy and no base.
CancellationSafety ==
    [][Cancel => copy' = copy /\ base' = base]_vars

\* Never undo a settled value (the brief's "no resurrection", both ways): once
\* a clean publish converged every copy and no user has acted since, no pass
\* writes any other value. Expected to hold with StaleSaves = FALSE and to fail
\* with TRUE, as the twin finds.
NeverUndoSettled ==
    [][PassWrite /\ settled # None
       => \A a \in Accounts : copy'[a] # copy[a] => copy'[a] = settled]_vars

\* Convergence: a pass with nothing rewritten since its decision ends with
\* every copy and the base equal to the value decided. (The twin checks the
\* same by running one such pass from every idle state.)
ConvergesInOnePass ==
    [][(PassCheck \/ PassWrite) /\ clean /\ phase' = "idle"
       => /\ \A a \in Accounts : copy'[a] = decided
          /\ base' = decided]_vars
=============================================================================
