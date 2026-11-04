"""The fake daemon (`tests.fake.run_daemon`) with the real API credit lane adapter, its
harness pointed at a local fake Messages API, for the C-31.17 containment checks
(`tests/fake/test_credit_key_containment.py`).

Two options of its own, taken out of argv before `run_daemon` parses the rest:

- `--credit-api-url URL`: the fake API's loopback URL. The real `ClaudeApiAdapter`
  builds every credit launch; this runner's subclass only adds the URL to its
  `env_add` as `SUBFLEET_API_AGENT_TEST_URL`, which the harness honors only in a pytest
  session's process tree (C-31.11). The runner refuses to start outside a live pytest
  session or with any URL but `http://127.0.0.1:<port>`, so no request leaves the host.
- `--self-test-dump DIR`: each publish hook self-test (`review_publish.hook_self_test`)
  starts its process as before; before the payload is written to the hook's stdin, the
  raw `KERN_PROCARGS2` block (exec argv and environment) of that process and of each of
  its descendants is written to DIR, for the test to read. `/bin/sh` is a platform
  binary, whose block macOS 26 returns without its environment, so the dump waits (up
  to 20 s) for the hook itself, a Python process whose block has it, to be running.

It never outlives the test process that started it (`run_daemon.ParentWatch`).
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import subprocess
import sys
import time
import types
from pathlib import Path

TEST_URL_ENV = "SUBFLEET_API_AGENT_TEST_URL"


def _take(flag: str) -> str | None:
    if flag not in sys.argv:
        return None
    index = sys.argv.index(flag)
    value = sys.argv[index + 1]
    del sys.argv[index:index + 2]
    return value


def pointed(url: str):
    """The real credit adapter, whose launches name the fake API (C-31.11)."""
    from subfleet.adapters.claude_api import ClaudeApiAdapter

    def point(launch):
        return None if launch is None else dataclasses.replace(
            launch, env_add={**launch.env_add, TEST_URL_ENV: url})

    class PointedCreditAdapter(ClaudeApiAdapter):
        def build_launch(self, *args, **kwargs):
            return point(super().build_launch(*args, **kwargs))

        def resume_launch(self, *args, **kwargs):
            return point(super().resume_launch(*args, **kwargs))

    return PointedCreditAdapter


def dump_self_tests(directory: Path) -> None:
    """Write each self-test process's exec block to `directory` while it waits on stdin."""
    from subfleet import review_publish
    from tests.fake import proc_blocks
    numbers = itertools.count(1)
    real = subprocess

    def run(argv, *, input=None, env=None, cwd=None, capture_output=False, text=False, timeout=None, **_):
        process = real.Popen(argv, stdin=real.PIPE, stdout=real.PIPE, stderr=real.PIPE, env=env, cwd=cwd,
                             text=text)
        number = next(numbers)
        try:
            deadline = time.monotonic() + 20
            while True:
                found = {pid: proc_blocks.block(pid) for pid in [process.pid, *proc_blocks.descendants(process.pid)]}
                if any(data and proc_blocks.environment(data) for data in found.values()) \
                        or time.monotonic() > deadline or process.poll() is not None:
                    break
                time.sleep(0.01)
            for pid, data in found.items():
                if data is not None:
                    (directory / f"self-test-{number}-{pid}.bin").write_bytes(data)
            out, err = process.communicate(input, timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        return real.CompletedProcess(argv, process.returncode, out, err)

    review_publish.subprocess = types.SimpleNamespace(**{**vars(real), "run": run})


def main() -> int:
    url = _take("--credit-api-url")
    dump = _take("--self-test-dump")
    from subfleet import home_paths
    from subfleet.adapters.registry import register
    if not home_paths.under_pytest():
        raise SystemExit("run_credit_daemon: refusing to run outside a live pytest session")
    if url is not None:
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", url):
            raise SystemExit("run_credit_daemon: --credit-api-url must be http://127.0.0.1:<port>")
        register("claude-api", pointed(url))
    if dump is not None:
        dump_self_tests(Path(dump))
    from tests.fake import run_daemon
    return run_daemon.main()


if __name__ == "__main__":
    raise SystemExit(main())
