# Rolling back canonical resource identity

A 2.1.11 daemon reading a store written with canonical output keys silently
skips exports whose `out:` key differs from `out:` plus the job's stored
`out_path`. Those jobs retain their `out:`, `native:`, `native-session:` and
`session:…:revive` leases. A new revive of that session is refused until the
leases are released; restarting the old daemon does not repair them.

Prefer draining output jobs on the new daemon before rollback. If that is not
possible, stop the daemon and run this supported repair from this checkout,
using the state root of the installation being rolled back:

```sh
subfleet daemon stop
python3 tools/prepare_identity_rollback.py --root /path/to/state
python3 tools/prepare_identity_rollback.py --root /path/to/state --apply
```

The first invocation previews the changes. The second restores exact output
spellings from jobs and exact native session spellings from manifests, revive
requests and attempts, including the continuation and revive guards. It takes
the daemon lock, updates all keys atomically, records an event, and preserves
holders, acquisition times and deadlines. It refuses unknown spellings or a
collision between different holders; drain those jobs on the new daemon first.
It does not default to or inspect the live home. Keep the daemon stopped until
the older code is installed, then start it normally. This repair also works
for finished jobs whose exports the older daemon already skipped: their
pending exports can complete and release their leases after its next start.

Upgrading again requires no inverse repair. Opening the store with this code
retains old raw rows, and output/native ownership lookups compare them through
canonical identity. Retaining separate rows keeps every legacy alias holder as
a guard when multiple old jobs already own one object. New reservations write
canonical keys. Display paths and session spellings stay unchanged.

Identity failures use exact strings and also guard an identical stored output
path whose quarantine retains a canonical key. A real wall-clock bound for a
hung filesystem syscall remains open (P3-6); the round-three deadline test is
marked xfail. Thread futures cannot cancel blocked filesystem work, and signal
handlers cannot bound daemon worker threads. A supervised, bounded worker
mechanism with an explicit shutdown policy is needed before claiming a bound.
