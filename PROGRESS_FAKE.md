# Fake acceptance progress

## State

Fake provider and socket acceptance harness are implemented; daemon integration
is pending the concurrent daemon implementation.

## Done

- Read the acceptance contract, shared contracts/schema/protocol/adapter seams,
  design narrative, and the specified v1 files without executing v1 commands.
- Agreed with the daemon implementation on the crash boundary hook and guardian
  receipt delay injection for deterministic recovery tests.
- Committed the fake provider and injectable adapter (4e96c47). Push failed with
  `Could not resolve host: github.com`; no guard or network restriction was bypassed.
- Added an isolated subprocess/socket harness and named admission, cancellation,
  containment, export, notice, singleton, and malformed request acceptance tests.
- Added the C-20.3 crash matrix covering reserved, starting, running, finalizing,
  terminal, notice, export, and salvage boundaries.

## Next

- Integrate with daemon implementation and resolve any contract failures.
- Run the fake suite within the C-20.2 60-second budget and report results.
