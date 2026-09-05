# Shadow-week routing diffs

One file per night, `<date>.md`, written by `tools/compare_decisions.py`
(`docs/migration.md`, shadow week step 4). Each file lists every input shape
where v2's routing decision differed from v1's, with an empty explanation column
that a human fills in: the point of the exercise is the one line saying *why*
they differed, and the script never guesses it.

```
SUBFLEET_HOME=~/.subfleet tools/compare_decisions.py --since 2026-09-06
```

The script is not part of the `subfleet` package and never ships with it.
