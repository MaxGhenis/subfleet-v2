# Provider active-attempt cap (D-RT1)

Status: implementation and validation in progress.

Policy: `providers.<provider>.max_active_attempts` accepts a positive integer or
null. An absent key is null (off); shipped default policy bytes remain unchanged.
The chosen operating value remains Max's decision.

Invariants to implement and verify:

- Reservation counts the provider's `reserved`, `starting`, `running`, and
  `finalizing` attempts across all lanes inside the reserving transaction.
- Detached jobs and conversation turns both count and are held at the cap:
  D-RT1's evidence counted turns, and both use the affected provider runtime.
- Probes are exempt from this provider cap.
- Held jobs acquire no lease, preserve admission order with their peers, and
  explain the exact policy key and provider count in `why` and status.
- Other providers remain unaffected; policies without the key retain their
  exact-byte hash and prior admission decisions.

Validation: pending. Tests will run one foreground pytest process at a time under
the shared suite lock, with each hold below 20 minutes.
