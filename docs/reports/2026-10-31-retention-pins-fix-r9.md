# Retention dependency pins: fix round r9

Started 2026-10-31 09:56:57 UTC at `236c80883ce3`. Work is confined to the assigned workspace. The requested macOS scratch folder was denied; `/private/tmp/sf-fix272r9` is the fallback. Durable evidence is in the adjacent data folder.

The red review hold fails all four owner-retention assertions. Before editing, the Data-volume probe executed plain and linked checkouts through both `git_storage` and `record`, at base, with the three-site trial, and with start spelling. Plain records are unchanged in all three. The linked checkout gains its canonical `.git` file with the three-site trial; spelling start restores the exact base record. `record` forwards the supplied folder without spelling it; daemon submission and legacy discovery also forward workdir to it before separately spelling the workdir. The one-line fix belongs in `_Aliases.locations` before its ancestor comparison.

Step 1 canonicalizes successful physical files and scan targets, and start before location comparisons. Physical link entries retain canonical parent plus entry name. Only review r9's strict decorator is removed; its bodies are unchanged. Further evidence and final accounting follow in the text commit.
