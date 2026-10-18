"""Keep each foreground pytest hold below the shared-lock budget."""
import signal
import time


def pytest_sessionstart(session):
    # Default SIGALRM terminates the process; the lock is released on exit.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    remaining = signal.alarm(0)
    signal.alarm(min(remaining or 18 * 60, 18 * 60))
    session._review_startup_seconds = 18 * 60 - remaining if remaining else 0
    session._review_started = time.monotonic()
    print("REVIEW HOLD: 18-minute hard alarm, SQLite FULL unchanged")


def pytest_sessionfinish(session, exitstatus):
    signal.alarm(0)
    elapsed = session._review_startup_seconds + time.monotonic() - session._review_started
    print(f"REVIEW HOLD SECONDS (including entrypoint startup): {elapsed:.3f}; exit {exitstatus}")
