"""D-ADMIT-IDLE1: each admission pool must receive its own capacity news."""

import pytest

from tests.fake.test_idle_capacity_events import clock, held_fleet, release  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


@pytest.mark.parametrize("first,second", [("detached", "turn"), ("turn", "detached")])
def test_capacity_news_censuses_are_independent_for_each_pool(held_fleet, first, second):
    """One pool's successful census cannot acknowledge a release for the other."""
    service, _, closure, _ = held_fleet
    # First observation establishes each pool's baseline and brings nothing
    # forward. Both pools have to have observed the held lane before release.
    assert service._capacity_change_news(first) is False
    assert service._capacity_change_news(second) is False
    release(service, closure)
    assert service._capacity_change_news(first) is True
    assert service._capacity_change_news(second) is True
    assert service._capacity_change_news(first) is False
    assert service._capacity_change_news(second) is False


def test_a_pool_that_never_ran_establishes_its_own_first_census(held_fleet):
    service, _, closure, _ = held_fleet
    assert service._capacity_change_news("detached") is False
    release(service, closure)
    assert service._capacity_change_news("detached") is True
    # Startup recovery handles old waits; a pool with no prior census has no
    # observed condition to retire, even after the other pool saw a release.
    assert service._capacity_change_news("turn") is False
