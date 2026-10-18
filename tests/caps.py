"""The count caps in force until 2026-09-27, for tests of capped admission.

C-6.4 made every concurrency cap null by default that day (Max: "uncap
everything and instead use prioritization"). A positive whole number still sets
one, so the mechanics a cap drives (C-6.9's hold-back and kept slot, `no-slot`,
`fleet-full`) are still the contract whenever a policy sets it. Tests of those
mechanics pin these values; tests of the default use the shipped policy as is.
The parent cap was a hidden 1; tests of it set it themselves.
"""

from __future__ import annotations

CAPS_UNTIL_2026_09_27 = {"max_active_attempts": 4, "max_in_flight_per_lane": 2, "max_in_flight_unmeasured": 1}


def capped(policy: dict) -> dict:
    """The policy with the caps of before 2026-09-27 set, in place; returned for chaining."""
    policy["caps"] = {**policy["caps"], **CAPS_UNTIL_2026_09_27}
    return policy
