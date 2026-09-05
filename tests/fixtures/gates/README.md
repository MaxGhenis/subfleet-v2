# Archived v1 gate replay fixtures

These three gate directories were copied from the read-only v1 state root on
2026-09-05. The source inventory was inspected using `ls -t | head -5`; the
newest plan was unfinished, so the latest completed plan was selected alongside
two completed PR gates. Every source file was read separately before copying.
No v1 command or provider was executed.

| Directory | Evidence retained |
| --- | --- |
| `20260905-073432-plan-1a3697dc` | Four plan rounds, including changes requested and a blocked round, ending in approval. |
| `20260905-131118-pr-8668d420` | Two abandoned/blocked PR rounds followed by one approved round at the same revision. |
| `20260905-093217-pr-aff2fcda` | Eight historical PR rounds across changed heads, ending in approval. |

The complete directory layouts are retained, including gate and certificate
JSON, per-round immutable artifact metadata, snapshots or patches, prompts,
responses, peer output, raw result JSON, attestation sidecars, and empty locks
and stderr logs. Local home paths become `/redacted/home`. The copy process
scrubbed private keys, JWTs, prefixed API tokens, Bearer values, cookie and
authorization headers, and credential-valued JSON fields. Variable references
such as `$GH_PUSH_TOKEN` are source text, not credentials, and remain visible.

JSON formatting was normalized, while certificate field values remain those
recorded by v1 after the same redaction. Issuance timestamps and fingerprint
metadata are historical evidence. Redaction of source paths inside snapshot
text can change its byte count and digest, so these archives are used to replay
recorded certificates, never as artifacts submitted for a new approval.

The eight-round fixture preserves v1's recorded unlimited cap. Replaying an
archive creates no new peer round and does not change C-23.53's four-round
default for v2 admission. The wire verdict remains `changes_requested`, as in
v1; C-23.9's prose uses `changes-requested` for the same state.
