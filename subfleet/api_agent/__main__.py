"""Entry point: `python -I -m subfleet.api_agent <interface argv>` (C-31.7).

The key arrives on the `--key-fd` pipe only (C-31.17): `harness.main` reads it once,
closes the descriptor and keeps it in memory. No environment variable is ever read for
it. Before anything else is imported, the values of every variable the tools never see
(`sandbox.secret_name`: every `ANTHROPIC_*` variable and every credential name) are
overwritten in the environment block the kernel copied at exec (`exec_env`, C-31.12):
unsetting a variable leaves that copy readable to any process of the same user, from
inside the tools' sandbox too. A launch never carries `ANTHROPIC_API_KEY` (C-31.2); a
stray one is overwritten here and removed by `main` like any other `ANTHROPIC_*`
variable, and never used.
"""
import sys

from subfleet.api_agent import exec_env
from subfleet.api_agent.sandbox import secret_name

exec_env.overwrite(secret_name)

if __name__ == "__main__":
    from subfleet.api_agent.harness import main

    sys.exit(main(sys.argv[1:]))
