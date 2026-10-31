| Single-site reversion | Result | First failing generated input, before shrinking | Assertion |
|---|---|---|---|
| r9-text-canonicalization-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-alternates-canonicalization-dropped | Detected | `{"named": true, "position": "alternates-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-scan-canonicalization-dropped | Detected | `{"named": false, "position": "commondir-file", "spelling": "data-volume"}` | `assert os.path.join(folders.canonical(os.path.dirname(place)), os.path.basename(place)) == place, place` |
| r9-start-spelling-dropped | Detected | `{"named": false, "position": "gitfile-target", "spelling": "data-volume"}` | `assert str(checkout / ".git") not in recorded.get("git", []), recorded` |
