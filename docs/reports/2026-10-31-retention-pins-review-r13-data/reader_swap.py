"""reader_swap.py <label> <dependencies snapshot> <pytest args...>: run tests with an older reader exec'd into
subfleet.dependencies, in-process, to replay a finding's red row."""
import importlib
from pathlib import Path
import sys

import pytest

label, snapshot = sys.argv[1:3]
module = importlib.import_module("subfleet.dependencies")
exec(compile(Path(snapshot).read_text(), label + ":subfleet/dependencies.py", "exec"), module.__dict__)
sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "--tb=line", "-rfE", *sys.argv[3:]]))
