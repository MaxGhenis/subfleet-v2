"""Entry point: `python -I -m subfleet.api_agent <interface argv>` (C-31.7).

The key leaves `os.environ` here, before the harness imports anything that could
read it or start a process (C-31.12).
"""
import os
import sys

_KEY = os.environ.pop("ANTHROPIC_API_KEY", None)

if __name__ == "__main__":
    from subfleet.api_agent.harness import main

    sys.exit(main(sys.argv[1:], _KEY))
