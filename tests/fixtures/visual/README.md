# Visual replay

`progress.json` is a frozen, sanitized fixture record in the daemon's public event format, assembled for this visual regression. It is not a capture from a live account. Replaying it never executes the recorded commands or contacts a daemon.

The live record contains 15 Bash calls, six thinking blocks (four empty), three agent prose messages, two failed calls, and one call still running. Descriptions survive in the public `summary` field. The snapshot and model probes replay this same file through the production `Timeline`; the finished variant appends completion at 3m 12s. The snapshot's last prose message also includes a code block and table to cover Markdown.

Dates are shifted together for rendered live elapsed labels. Event sequence, descriptions, thinking visibility and the finished duration remain unchanged.

`approvals.json` adds sanitized requests matching the shapes retained by the Claude and Codex turn drivers: Codex command execution, file-change and additional permissions; Claude Bash, Write and AskUserQuestion; and a legacy flat command. It includes grant roots, blocked/file paths, filesystem/network permissions, execpolicy/network amendments, a future grant field and a masked command. These are synthetic requests, and their commands are never executed. Rendered-text assertions inspect the real card and sheet after `approval.get` loads each request. Those assertions use foreground Tesseract OCR (`tesseract` on PATH); they skip explicitly when that optional native verification dependency is absent.

The renderer also covers Codex command progress, a text-only response, a model-mismatch failure without an error event, a stopped turn, a withdrawn turn and completion before Stop took effect. Every scene is captured in both modes, using a fixed 2880×1800 bitmap independent of the monitor's backing scale. The refused-folder fixture advertises `workspace.check.v1` and must settle before capture. All storage is under the supplied fixture root, and every backing window remains unshown.

The snapshot test instruments only AppKit's natural bitmap factory to exercise 1× and 2× capture on any display. It retains the real renderer and checks the saved dimensions. The refused-folder check uses 1× so a scale failure cannot obscure a missing-capability failure. The grant fixtures also cover an additional command and a literal key containing dots and brackets; their field identities and values must survive flattening.
