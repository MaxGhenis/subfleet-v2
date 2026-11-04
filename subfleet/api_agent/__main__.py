"""Entry point: `python -I -m subfleet.api_agent <interface argv>` (C-31.7).

The key leaves `os.environ` here, before the harness imports anything that could
read it or start a process (C-31.12). First, the values of the key and of every
other variable the tools never see (`sandbox.secret_name`) are overwritten in the
environment block the kernel copied at exec (`exec_env`): unsetting a variable
leaves that copy readable to any process of the same user, from inside the tools'
sandbox too.
"""
import os
import sys

from subfleet.api_agent import exec_env
from subfleet.api_agent.sandbox import secret_name

exec_env.overwrite(secret_name)
_KEY = os.environ.pop("ANTHROPIC_API_KEY", None)

if __name__ == "__main__":
    from subfleet.api_agent.harness import main

    sys.exit(main(sys.argv[1:], _KEY))
