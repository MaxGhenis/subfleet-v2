# Fake acceptance progress

## State

Building the isolated fake provider and daemon acceptance tests for lane/core.

## Done

- Read the acceptance contract, shared contracts/schema/protocol/adapter seams,
  design narrative, and the specified v1 files without executing v1 commands.
- Agreed with the daemon implementation on the crash boundary hook and guardian
  receipt delay injection for deterministic recovery tests.

## Next

- Add the fake provider and adapter, then commit and push this coherent step.
- Add socket/process fixtures and named C-4/C-5/C-6/C-7/C-8/C-15/C-16 tests.
- Run the fake suite within the C-20.2 60-second budget and report results.
