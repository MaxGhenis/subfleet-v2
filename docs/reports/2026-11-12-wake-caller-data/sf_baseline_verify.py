from pathlib import Path
import sys

sys.path.insert(0, '/private/tmp/sf-wc-b1/baseline')
import subfleet
assert Path('/private/tmp/sf-wc-b1/baseline') in Path(subfleet.__file__).resolve().parents


def pytest_sessionstart(session):
    import subfleet.daemon
    path = Path(subfleet.daemon.__file__).resolve()
    expected = Path('/private/tmp/sf-wc-b1/baseline').resolve()
    assert expected in path.parents, path
    Path('/private/tmp/sf-wc-b1/baseline-import.txt').write_text(str(path) + '\n')
