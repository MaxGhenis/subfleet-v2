def pytest_collection_modifyitems(items):
    for item in items:
        item.user_properties.append(("nodeid", item.nodeid))


def pytest_runtest_logstart(nodeid):
    from pathlib import Path
    Path(__file__).with_name('current-test.txt').write_text(nodeid)
