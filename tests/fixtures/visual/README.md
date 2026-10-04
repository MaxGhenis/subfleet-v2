# Visual replay

`progress.json` is a frozen, sanitized fixture record in the daemon's public event format, assembled for this visual regression. It is not a capture from a live account. Replaying it never executes the recorded commands or contacts a daemon.

The live record contains 15 Bash calls, six thinking blocks (four empty), three agent prose messages, two failed calls, and one call still running. Descriptions survive in the public `summary` field. The snapshot and model probes replay this same file through the production `Timeline`; the finished variant appends completion at 3m 12s. The snapshot's last prose message also includes a code block and table to cover Markdown.

Dates are shifted together for rendered live elapsed labels. Event sequence, descriptions, thinking visibility and the finished duration remain unchanged.
