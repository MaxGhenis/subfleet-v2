| Reverted | Location property /55 | Focused /40 | First failing generated input or strategy boundary |
|---|---|---|---|
| decision1 | Detected | Pass | location: `{"chain": 1, "entry": "HEAD", "named": false, "position": "gitfile-target", "relative": false}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| decision2 | Detected | Pass | location: `{"chain": 2, "entry": "HEAD", "named": false, "position": "gitfile-pointer", "relative": true}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| decision3 | Detected | Pass | location: `{"chain": 1, "entry": "HEAD", "named": false, "position": "git-entry", "relative": false}` Focused strategy has no metadata-only owner or entry selector (NAMED_OWN_INPUTS); it records the owner’s object storage directly. |
| r7-f1 | Detected | Detected | location: `{"chain": 1, "entry": "refs", "named": true, "position": "git-entry", "relative": true}` focused: `{"common": "absent", "own": "plain", "parent": "unrelated-commondir", "slot": 162}` |
