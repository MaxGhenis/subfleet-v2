# PR #192 review round 1 fixes

Base: `428e5dc530c9`. Work is confined to the assigned detached worktree;
no live store, network remote, or sub-agent is used.

Status: implementation and regression verification in progress.

Required changes:

- P1: compare every whole-store backup table's count and content digest with
  the planning snapshot before any DELETE.
- P2: refuse missing schema-7 tables without writer initialization or repair.
- P2: compare the complete post-delete event contents after VACUUM, including
  new prune audit payload bytes.
- Document that later retention archives contain no prune provenance marker.

Regression results at the base and fixed head: pending.
Tests: none executed yet. The initial base probe attempt could not acquire the
shared lock (`lockf -t 30`, exit 75), before pytest started; its TMPDIR was
cleaned. Locked dependencies are installed offline from cached wheels.
An isolated, unchanged base is being preserved in the workspace for later
before/after verification. All runs use one foreground pytest process per lock
hold with a unique task TMPDIR and Python cleanup.

The sandbox rejected writing the shared Git index. Commits therefore use
workspace-local `.git-local` metadata and a local `feat/prune-decisions-217`
branch rooted at the assigned base; shared metadata remains untouched.

Unrun: full repository suite and any live-store dry run/apply.
