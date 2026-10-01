"""The sessions fixture's real guardian with synthetic process identity reads.

These tests exercise provider launches, durable receipts and session leases,
not kernel process identity. The parent observes exact guardian Popen handles;
the fake providers neither escape their group nor outlive their guardian.
Kernel identity and escaped-process containment have separate process tests.
"""

import os

from subfleet import guardian

BOOT = "fixture-boot"
START = "fixture-start"


def own_start(pid):
    assert pid == os.getpid(), "the guardian only asks for its own identity"
    return START


def main():
    guardian.boot_id = lambda: BOOT
    guardian.proc_start = own_start
    return guardian.main()


if __name__ == "__main__":
    raise SystemExit(main())
