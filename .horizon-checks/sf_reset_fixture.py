"""Replay the failed fixture with distinct, controlled weekly reset clocks."""
def pytest_runtest_call(item):
    if 'test_c11_8_held_told_once_blocking_nobody_then_failed_rc_3' not in item.nodeid:
        return
    from subfleet.daemon import after
    service, _ = item.funcargs['fleet']
    with service.store.transaction('diagnostic.reset-order') as tx:
        for lane, seconds in [('claude-9',3600),('claude-7',7200)]:
            tx.execute("UPDATE readings SET resets_at=? WHERE lane_id=? AND window='seven_day'",
                       (after(seconds),lane))
