"""The API credit lanes' harness: `python -I -m subfleet.api_agent` (C-31.7).

One process per attempt, owned by the guardian like any other provider. It runs a
streaming Messages API loop against `https://api.anthropic.com` with two client
tools, `bash` and `str_replace_based_edit_tool`, and writes `stream.jsonl`,
`messages.jsonl` and `last.md` in the attempt folder. The adapter
(`subfleet/adapters/claude_api.py`) builds its launch and never spawns it; the two
meet only at the interface in `docs/decisions/2026-11-03-api-credit-lanes.md`.

Only `subfleet.api_agent.client` imports the `anthropic` SDK (the `api` extra), and
only after `__main__` has taken the key out of the environment.
"""
