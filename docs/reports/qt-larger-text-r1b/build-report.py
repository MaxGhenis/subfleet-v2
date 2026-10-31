"""Write the review report and full proposed body from earned evidence."""
from collections import Counter, defaultdict
import json
from pathlib import Path

from summarize import HERE, ROOT, OUT

evidence = json.loads(OUT.read_text())
final_names = {
    'fixed-pin-forced', 'fixed-resizes', 'fix10-whole', 'fix8-view',
    'round7-default', 'round7-larger', 'round10-quiet', 'round10-growth',
    'round10-window', 'repeat-larger', 'guards-quiet',
}
selected = [r for r in evidence['runs'] if r['name'] in final_names]
assert {r['name'] for r in selected} == final_names
assert all(r['end'] for r in selected), 'required selection unfinished'
assert all(r['end'] for r in evidence['runs']), 'diagnostic or baseline unfinished'
assert all(evidence[k] for k in ['navigation', 'two_reveal_input', 'attempt2_comparison'])
files = defaultdict(Counter)
for run in selected:
    for file, counts in run['counts'].items():
        files[file].update(counts)
total = sum(c['passed'] for c in files.values())
expected_passes = {
    'tests/frontend/test_review_pr284_pin.py': 21,
    'tests/frontend/test_review_pr284_resizes.py': 8,
    'tests/frontend/test_review_r219_fix10_view.py': 60,
    'tests/frontend/test_review_r219_fix8_mutations.py': 12,
    'tests/frontend/test_review_r219_fix8_view.py': 6,
    'tests/fake/test_state_contract.py': 37,
    'tests/unit/test_test_module_names.py': 1,
    'tests/unit/test_invariants_index.py': 25,
}
assert {f: c['passed'] for f, c in files.items()} == expected_passes, files
assert files['tests/frontend/test_review_pr284_pin.py']['failed'] == 1
assert files['tests/frontend/test_review_pr284_pin.py']['xfailed'] == 1
assert sum(c['failed'] for c in files.values()) == 1
assert not any(c.get('error') or c.get('skipped') for c in files.values())
baseline = defaultdict(Counter)
for r in evidence['runs']:
    if r['name'].startswith('baseline'):
        for file, counts in r['counts'].items():
            baseline[file].update(counts)
assert baseline['tests/frontend/test_review_pr284_pin.py'] == Counter(passed=9, failed=6)
assert baseline['tests/frontend/test_review_pr284_resizes.py'] == Counter(passed=3)
checkpoint = evidence['resource_checkpoint_bytes']
assert checkpoint['scratch'] < 1_000_000_000
resource_note = (f"The final checkpoint records scratch at {checkpoint['scratch'] / 2**20:.1f} MiB "
                 f"and this job's frontend cache at {checkpoint['workspace_frontend_cache'] / 2**20:.1f} MiB.")
cleanup_note = ('Guarded cleanup removed scratch and this job\'s generated frontend cache and bytecode. '
                'Every managed tool command was awaited to completion.'
                if evidence.get('cleanup', {}).get('complete') else 'Cleanup is pending.')
repeat = next(r for r in selected if r['name'] == 'repeat-larger')
counts = Counter(i['nodeid'] for i in repeat['items'] if i['outcome'] == 'passed')
assert len(repeat['collection']) == 20 and len(counts) == 2 and set(counts.values()) == {10}
for name, expected in [('repeat-larger', 20), ('round7-larger', 3)]:
    native = [item for item in evidence['native_results']
              if item['path'].startswith(f'cases/{name}/') and 'layout' in item]
    assert len(native) == expected, (name, len(native))
    assert all(item['layout']['text_scale'] == 1.728
               and item['layout']['window_height'] == 760 for item in native), native
steps = {s['step']: s for s in evidence['two_reveal_input']['steps']}
assert steps['landed']['reveals'] == 2
at = steps['shrinking']['at']
old_releases = [t for t in evidence['two_reveal_input']['trace']
                if t['at'] >= at and 'suspended' in t and not t['suspended'] and t['event'] != 'move']
state = False
real = []
for t in evidence['two_reveal_input']['trace']:
    if 'suspended' not in t:
        continue
    if state and not t['suspended'] and t['at'] >= at:
        real.append(t)
    state = t['suspended']
assert len(real) == 1
body = (HERE / 'pr-body-draft.md').read_text()
body = body.replace(
    '- Fix-round validation is being rerun at inherited head `98924e35c1fd`; final counts will be recorded here. The saved #282 attempt-2 pending capture is checked through the old marker-based and new geometry-based order helpers and both behavioral versions.',
    f'- Fresh fix-round r1b validation: {total} passing repository test invocations and three passing diagnostics. The larger-text shrink cases ran ten times each after a 20-item collection-count check. With `--runxfail`, only the listed quiet-landing counterexample fails; normally it is a strict expected failure. All repaired cases pass.\n'
    '- The saved #282 attempt-2 pending capture fails the old marker-based order helper, passes the new geometry-based helper because it measures a clamp while the target remains pending, and passes both original and corrected behavioral helpers.')
body = body.replace(':**\n-', ':**\n\n-')
assert 'is being rerun' not in body and 'final counts will be recorded' not in body
(HERE / 'pr-body-draft.md').write_text(body)
run_rows = []
for r in sorted(evidence['runs'], key=lambda r: r['start']['start']):
    summary = '; '.join(Path(f).name + ': ' + ', '.join(f'{n} {k}' for k, n in c.items())
                        for f, c in r['counts'].items()) or 'no completed cases'
    if r['end'].get('interrupted'):
        summary += '; interrupted compile'
    run_rows.append(f"| {r['start']['start'][11:19]} | {r['start']['load'][0]:.2f} | `{r['name']}`: {summary} | {r['end']['seconds']:.2f} |")
file_rows = [f"| `{f}` | {c['passed']} | " + (', '.join(f'{n} {k}' for k, n in c.items() if k != 'passed') or 'none') + ' |'
             for f, c in sorted(files.items())]
outside = [r['name'] for r in evidence['runs'] if r['outside_lock']]
lock = ('All runs used the suite wrapper; none ran outside the lock.' if not outside else
        'After 30 minutes without admission, only the owned waiters were stopped and these selections ran directly: '
        + ', '.join(outside) + '. The other runs used the suite wrapper.')
section = f'''## Review r1 and fix round r1

Fix round r1b starts from `98924e35c1fd`, the byte-for-byte recovery of round r1's
code and tests after the reboot. Every result below was earned again. The
production pin, regular probe scenes and inherited tests required no further change.

1. A changed `conversationID` clears both `outstanding` and `landedRequest`.
   Assigning the same ID preserves them. Immediate and already queued inputs in
   both document orientations produce zero old-request callbacks after changing
   IDs; same-ID controls still deliver callbacks. The identity-reset mutant is
   rejected. Both identity strict marks were removed by the inherited fix.
2. One private `distanceFromEnd` helper supplies all four distance calculations
   without changing the formula. Both orientations retain 25 pt acceptance and
   26 pt rejection controls.
3. The existing scene permits up to three reveal attempts. The assertion accepts
   one through three and still requires the landed order and release behavior.
   This preserves the existing scene. A fresh native input forces two permitted
   attempts: `reveals=2` passes, while the former exact-one check rejects that
   value. The saved replay passes for 1, 2 and 3; zero reveals is rejected.
4. `releases_after` carries suspension state across the cutoff and counts only
   true-to-false transitions, including transitions recorded on movement events.
   Repeated resumed confirmations and entries without suspension data add none.
   The fresh two-reveal trace changes the old count of {len(old_releases)} entries
   to one real release, on `{real[0]['event']}`. All dependent default/larger
   shrink controls and all six round-7 mutation executions pass; no mutant
   survives the corrected count. Only the release-count strict mark was removed.
5. **Listed difference retained:** quiet-landing cases lack an independent
   growth probe. Their reply-only rule accepts a measured missed 600 pt growth
   followed by a compensating shrink to zero. Its scene and strict mark remain.
   With `--runxfail`, this is an intentional assertion failure; the separate
   normal run reports a strict expected failure. These outcomes are counted
   separately below.
6. The saved #282 attempt-2 fixture exactly matches the supplied log's JSON.
   Executing it rejects the old marker-based order helper and passes the current
   geometry-based helper: a zero-distance reveal check occurs during the measured
   clamp while the target remains outstanding. Both original and corrected
   suspension/behavior assertions pass. A replay claiming landing before the
   shrink remains rejected.
7. The report and full proposed body use the qualified current-watch invariant,
   name pin teardown, add conversation change to watch cancellation, and send
   the next review to r2. No hub decision was changed.

### Suspension input and navigation

Clearing the pin's request does not call a resume callback on the old owner.
The app mounts `ConversationView(...).id(conversation_id)`, so navigation disposes
that owner's state and pin. The executed input leaves A with a pending reveal
and suspension enabled, then crosses that same identity boundary to B. The pin
is replaced, B's recorded states are resumed, catch-up jumps, and B finishes
{evidence['navigation']['b_position']['from_end']} pt from the end, within the existing 30 pt control. B inherits no
suspension, so no additional repair or new navigation route was added.

### Fresh red and green validation

The baseline is `26ee5d6f2`; final product/test sources are inherited
`98924e35c1fd`. Every hold contains one serial pytest and a 19-minute boundary.
The first hold began within one hour of this job's 02:34:34 UTC start.

Final selections: **{total} passing repository test invocations**, plus **three
passing diagnostics**. Baseline results are separate. The quiet-landing forced
failure and normal strict expected failure are also separate.
Ten baseline source/fixture files match their `26ee5d6f2` Git blobs, and the
inherited product and test sources remain unchanged from `98924e35c1fd`.

Baseline `--runxfail`: **9 passed / 6 intended failures** in the pin file and
**3 passed** in the resize file. The cold baseline reached its 19-minute boundary
with six passing controls and two identity failures; its continuation ran only
unfinished cases. An initial diagnostic compile was stopped before producing
a native binary so its docs-path collection could explicitly load the repository's
home-isolation guard; the corrected hold passed all three diagnostics. Two queued
controllers were restarted while correcting the direct-run handoff, with their
original wait times preserved. Another queued controller was split into two
independent named-mutation sequences, and its larger-text environment was aligned
with the test's existing scale-only layout before admission. Those restarts also
retain the original wait time. These setup attempts add no passing checks.

| File | Final passing invocations | Other outcomes |
| --- | ---: | --- |
{chr(10).join(file_rows)}

All starts below are UTC on 2026-10-31; loads use `os.getloadavg()` at pytest
start because the wrapper's `sysctl` display is denied.

| Start | Load | Run and counts per file | Seconds |
| --- | ---: | --- | ---: |
{chr(10).join(run_rows)}

Repeated file collection, with a narrow selector, collected exactly 20 items:
the pending and landed `[larger_text]` cases ten times each. Both collected and
passing per-node counts are checked; repeated node IDs were not used. The
whole fix10 file additionally covers both layouts, the person-scroll control
and the 12-schedule deterministic Hypothesis property.

| Mutant | Executions | Result |
| --- | ---: | --- |
| `marker_releases_pending_reveal` | 2, default/larger | rejected by pending suspension rule |
| `confirm_by_distance_only` | 2, default/larger | rejected by pending suspension rule |
| `reveal_never_lands` | 2, default/larger | rejected by landed release rule |
| `quiet_landing_ends_following` | 3 named scenes | rejected; separate listed reply-only counterexample remains |
| `growth_repins_dropped` | 2 named checks | rejected for measured growth before compensation |
| `quiet_window_0ms` | 1 | rejected by settled-position rule |

All six named mutants are rejected. The independent reset, person-scroll
cancellation and dropped-landed-watch mutants are also rejected by the review
files. Mutation setup/order checks remain outside their expected assertion
failure blocks.

{lock} Largest reported child RSS was
{evidence['resource']['max_reported_child_rss_bytes'] / 1e9:.3f} GB, below 8 GB.
{resource_note} {cleanup_note}
Darwin scratch creation was denied, so `/private/tmp/sf-fix284r1b` held the
venv, uv cache, compiler cache and per-hold TMPDIR. Shared Git metadata denied
the index lock. Commits use `.git-local` in this worktree on
`fix/qt-larger-text-217`; each is delivered immediately in the verified
`docs/reports/qt-larger-text-217-r1b.bundle`, based on `98924e35c1fd`.
The assigned checkout's shared HEAD remains at that inherited commit.

The [evidence JSON](2026-10-30-qt-larger-text-r1b-evidence.json) retains source
hashes, collection counts, each test result, actual native inputs and reduced
native outputs. The [full body draft](qt-larger-text-r1b/pr-body-draft.md),
runner and diagnostic sources are retained in the worktree.

Not run: prohibited `tests/frontend/test_core_live.py`, a fresh CI matrix,
Python 3.12, or a CI macOS image. CI owns the live test's person-only daemon
gate. No requested local selection remains unrun. No fetch, merge, push,
GitHub call, live store, real CLI/provider, sub-agent or host-loading process
was used.

### Full proposed PR body for review r2

```markdown
{body.rstrip()}
```
'''
report = ROOT / 'docs/reports/2026-10-30-qt-larger-text.md'
original = report.read_text().split('## Review r1 and fix round r1')[0].rstrip()
report.write_text(original + '\n\n' + section)
evidence['final_selection_counts_per_file'] = dict(files)
evidence['final_repository_passes'] = total
evidence['two_reveal_release_counts'] = {'old': len(old_releases), 'real': len(real)}
evidence['invariant'] = ('While the same request and conversation remain current and its watch has not been cancelled, '
                         'a shrink exposing the end releases nothing before the target is presented. '
                         'After landing, it confirms the end independently of a marker remount.')
evidence['decisions'] = {
    '1': {'built': 'Changed identity clears pending and landed requests; same identity retains them; queued checks read current requests.',
          'suspension': 'The app identity boundary replaces the owner and pin. Executed A pending+suspended input leaves B resumed with normal catch-up jumps; no further repair.'},
    '2': {'built': 'All four distance calculations use one private helper; 25/26 pt controls cover both orientations.'},
    '3': {'choice': 'Accept one through three attempts while still requiring landing and release behavior; preserve the existing scene.',
          'native_reveals': 2, 'old_exact_one': 'rejected', 'zero_reveal_replay': 'rejected'},
    '4': {'built': 'Count suspended-to-resumed transitions and carry state across the cutoff.',
          'old_trace_count': len(old_releases), 'real_releases': 1, 'transition_dependent_mutant_survivors': 0},
    '5': {'listed_difference': 'Quiet-landing reply-only coverage accepts measured missed growth of 600 pt followed by compensation to zero.',
          'strict_mark_retained': True, 'forced_outcome': 'failed', 'normal_outcome': 'xfailed', 'scene_changed': False},
    '6': evidence['attempt2_comparison'],
    '7': {'watch_ends': ['timeline scroll input', 'another reveal', 'matching abandonment', 'own send', 'conversation change', 'pin teardown'],
          'next': 'review r2', 'pr_body': body},
}
evidence['hub_decision_deviations'] = []
evidence['not_run'] = ['tests/frontend/test_core_live.py (prohibited on a lane)',
                       'fresh CI matrix', 'Python 3.12', 'CI macOS image']
(ROOT / 'docs/reports/2026-10-30-qt-larger-text-r1b-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
print('Report and full body written:', total, 'repository passes')
