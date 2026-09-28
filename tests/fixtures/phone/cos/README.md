# Pinned chief-of-staff poller and safe say double

`bin/tg-poller` is the unmodified read-only snapshot of
`~/chief-of-staff/bin/tg-poller` inspected on 2026-09-28. The patch regression
tests apply `docs/desktop/phone/cos-tg-poller.patch` to a temporary copy. They do
not need chief-of-staff installed and never execute the live poller.

`bin/say` is a small fake using the real gateway's `SAY_TRANSPORT=file:/path`
JSONL pattern. It refuses every other transport. Set `COS_HOME` and the file
transport to temporary paths, and optionally `SAY_ARGV_FILE` to inspect CLI
arguments. `SAY_RESULT` and `SAY_RETURN_CODE` simulate transport outcomes.
`SAY_NOW` can exercise an alert held during quiet hours. It does not model the
real gateway's daily budget or alert deduplication.
