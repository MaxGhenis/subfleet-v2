# Guard startup scheduling, 2026-09-20

Several Codex attempts stopped before provider launch because the local app-server did not answer `hooks/list` within the guard deadline. A metadata-only replay against the same lane home, executable, and workdir verified the installed hook hash, pinned Codex 0.153.3, and runtime hook trust. No model turn was requested.

The installed daemon used launchd `ProcessType=Background`. The local macOS `launchd.plist(5)` manual describes Background as work not directly requested by the user, with resource restrictions intended to protect interactive work. Standard uses the default light CPU/I/O limits. Subfleet's user-requested dispatch belongs in Standard; it does not need unrestricted Interactive scheduling.

Controlled metadata-only checks on this Mac produced:

| Invocation | Elapsed | Result |
|---|---:|---|
| Direct process | 0.32 s | Guard trusted |
| Direct process with plist environment | 0.44 s | Guard trusted |
| Temporary launchd job, Background | 17.03 s | hooks/list timeout |
| Temporary launchd job, Standard | 10.65 s | Guard trusted |
| Temporary launchd job, Background, later warm run | 3.67 s | Guard trusted |

These observations reproduce an intermittent startup timeout and support removing unnecessary Background restrictions. They do not isolate scheduling from filesystem-cache state or prove every timeout has that cause. Elapsed time includes the version check and teardown; the hooks/list deadline itself remains ten seconds. Temporary diagnostic jobs were removed afterward.

The installer now emits Standard. Timeout refusals still return code 7 without a launch override, but their advice identifies local scheduling/load and unverified trust rather than asserting that guard files or the pinned version must be restored. Hash, version, hook identity, enabled/trusted status, and runtime response checks remain mandatory.
