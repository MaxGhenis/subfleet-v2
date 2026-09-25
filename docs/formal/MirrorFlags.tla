---------------------------- MODULE MirrorFlags ----------------------------
(***************************************************************************)
(* The desktop sidebar mirror's flag protocol (subfleet/sessions/mirror.py, *)
(* `sync_flags`), for one session and one boolean flag (isArchived;        *)
(* isStarred is the same code path).                                        *)
(*                                                                          *)
(* Each account folder holds a copy of the session's record. The merge base *)
(* (mirror-flags.json) holds the last synced value. A pass is two steps,    *)
(* because the app can write between them: PassDecide reads every copy and  *)
(* decides by the merge base; PassPublish writes the copies that read       *)
(* differently, all or nothing, only if each is still what the pass read,   *)
(* and advances the base. A pass can be cancelled between the two; the code *)
(* has no cancellation point inside publish.                                *)
(*                                                                          *)
(* The app holds in memory the record of the folder it loaded (as it was on *)
(* disk at that load) and of folders where a session still runs from an     *)
(* earlier account. A user action changes the loaded folder's copy and the  *)
(* app's memory; an app save writes memory. With StaleSaves = FALSE the app *)
(* never saves a flag that differs from its file; with TRUE it may (the     *)
(* known limit in docs/reports/2026-09-24-mirror-load-gap.md).              *)
(*                                                                          *)
(* tests/mirror_flags_model.py is the executable twin of this module, and   *)
(* tests/unit/test_mirror_flags_stateful.py holds the implementation to it. *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Accounts,     \* the account folders, e.g. {a1, a2, a3}
          StaleSaves    \* may an app save put back a value it held from before?

None == "none"
Values == {TRUE, FALSE}

VARIABLES
    copy,      \* copy[a]: the flag in account a's file
    base,      \* the merge base: the last synced value, or None
    loaded,    \* the account folder the app has loaded
    mem,       \* mem[a]: the app's in-memory flag for a's copy, or None
    phase,     \* "idle" or "decided"
    snap,      \* snap[a]: what the pass read (meaningful while "decided")
    decided,   \* the value the pass decided (meaningful while "decided")
    settled    \* ghost: the value a clean publish converged to, or None

vars == <<copy, base, loaded, mem, phase, snap, decided, settled>>

TypeOK ==
    /\ copy \in [Accounts -> Values]
    /\ base \in Values \cup {None}
    /\ loaded \in Accounts
    /\ mem \in [Accounts -> Values \cup {None}]
    /\ phase \in {"idle", "decided"}
    /\ snap \in [Accounts -> Values]
    /\ decided \in Values
    /\ settled \in Values \cup {None}

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
    /\ settled = None

\* The app loads another account's folder: it reads the file.
Load(a) ==
    /\ a # loaded
    /\ loaded' = a
    /\ mem' = [mem EXCEPT ![a] = copy[a]]
    /\ UNCHANGED <<copy, base, phase, snap, decided, settled>>

\* The user changes the flag in the loaded account's sidebar.
UserSet(v) ==
    /\ copy[loaded] # v
    /\ copy' = [copy EXCEPT ![loaded] = v]
    /\ mem' = [mem EXCEPT ![loaded] = v]
    /\ settled' = None
    /\ UNCHANGED <<base, loaded, phase, snap, decided>>

\* The app saves a record from memory. Only a stale memory changes the flag.
AppSave(a) ==
    /\ StaleSaves
    /\ mem[a] # None
    /\ mem[a] # copy[a]
    /\ copy' = [copy EXCEPT ![a] = mem[a]]
    /\ UNCHANGED <<base, loaded, mem, phase, snap, decided, settled>>

PassDecide ==
    /\ phase = "idle"
    /\ phase' = "decided"
    /\ snap' = copy
    /\ decided' = Decide(copy, base)
    /\ UNCHANGED <<copy, base, loaded, mem, settled>>

Dirty == {a \in Accounts : snap[a] # decided}
Held == \E a \in Dirty : copy[a] # snap[a]

PassPublish ==
    /\ phase = "decided"
    /\ phase' = "idle"
    /\ IF Held
       THEN UNCHANGED <<copy, base, settled>>
       ELSE /\ copy' = [a \in Accounts |-> IF a \in Dirty THEN decided ELSE copy[a]]
            /\ base' = decided
            /\ settled' = IF \A a \in Accounts : copy'[a] = decided
                          THEN decided ELSE None
    /\ UNCHANGED <<loaded, mem, snap, decided>>

Cancel ==
    /\ phase = "decided"
    /\ phase' = "idle"
    /\ UNCHANGED <<copy, base, loaded, mem, snap, decided, settled>>

Next ==
    \/ \E a \in Accounts : Load(a) \/ AppSave(a)
    \/ \E v \in Values : UserSet(v)
    \/ PassDecide \/ PassPublish \/ Cancel

Spec == Init /\ [][Next]_vars

---------------------------------------------------------------------------
(* Properties. Each is also checked, state by state, by                    *)
(* tests/unit/test_mirror_flags_model.py.                                  *)

\* Idempotence: a converged state with its base decides itself.
Idempotence ==
    [][PassDecide /\ (\A a, b \in Accounts : copy[a] = copy[b]) /\ base \in Values
         /\ (\A a \in Accounts : copy[a] = base)
       => decided' = base]_vars

\* Change wins, both ways: when every copy that differs from the base holds
\* the same value, that value is decided.
ChangeWins ==
    [][PassDecide /\ base \in Values
         /\ Cardinality({copy[a] : a \in {x \in Accounts : copy[x] # base}}) = 1
       => decided' # base]_vars

\* No lost update: the mirror writes only copies that are still as it read them.
NoLostUpdate ==
    [][PassPublish => \A a \in Accounts : copy'[a] # copy[a] => copy[a] = snap[a]]_vars

\* All or nothing: a held batch writes nothing and keeps the base.
AllOrNothing ==
    [][PassPublish /\ Held => copy' = copy /\ base' = base]_vars

\* Base agreement: after a publish that went through, the base is the value
\* decided and every copy the pass read differently holds it.
BaseAgreement ==
    [][PassPublish /\ ~Held => base' = decided /\ \A a \in Dirty : copy'[a] = decided]_vars

\* Cancellation safety: a cancelled pass changes no copy and no base.
CancellationSafety ==
    [][Cancel => copy' = copy /\ base' = base]_vars

\* Never undo a settled value (the brief's "no resurrection", both ways): once
\* a clean publish converged every copy and no user has acted since, no pass
\* writes any other value. Expected to hold with StaleSaves = FALSE and to fail
\* with TRUE; tests/mirror_flags_model.py checks both exhaustively (TLC not run).
NeverUndoSettled ==
    [][PassPublish /\ settled # None
       => \A a \in Accounts : copy'[a] # copy[a] => copy'[a] = settled]_vars

\* Convergence: one uninterrupted pass from any idle state leaves every copy
\* and the base equal (checked as a state predicate over the decision).
ConvergesInOnePass ==
    phase = "idle" =>
        LET d == Decide(copy, base) IN
        \A a \in Accounts : (IF copy[a] # d THEN d ELSE copy[a]) = d
=============================================================================
