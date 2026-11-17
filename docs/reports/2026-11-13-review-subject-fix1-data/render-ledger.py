"""Render the append-only fix-round test ledger without running tests."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shlex


SCRATCH = Path('/private/tmp/sf-a2-fix1')
EVIDENCE = '2026-11-13-review-subject-fix1-data/test-commands.jsonl'
WRAPPER = ('TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)" '
           'UV_CACHE_DIR=/private/tmp/sf-a2-fix1/uv-cache '
           "uv run --no-sync --with 'pytest-xdist>=3.6' python ")
KNOWN_EARLY_ROOTS = (
    '/tmp/sf-lzm3a2wm', '/tmp/sf-_07k5_2c', '/tmp/sf-pyarwa1q',
    '/tmp/sf-ebps5x1o', '/tmp/sf-kkj7cda_', '/tmp/sf-_1pjv228',
    '/tmp/sf-0gxogwz4', '/tmp/sf-e8wrr61a', '/tmp/sf-towj8w04',
)
INTERPRETATIONS = {
    'pytest-writer-red-1': 'Fixture/setup error; not valid red evidence.',
    'pytest-schema-slots-red': 'Fixture files were not ready; not valid red evidence.',
    'pytest-cli-cap-green1': 'Positive daemon stubs omitted the new capability; fixtures corrected.',
    'pytest-schema-slots-green': 'Preservation assertion compared normalized QoS with raw NULL; assertion corrected.',
    'pytest-cli-family-extra-red1': 'Deliberate red against git-archived 5b06c1826bc0 CLI; four regressions reproduced.',
    'pytest-importers-1': 'Interrupted before any test ran; initial broad collection did not finish.',
    'pytest-importer-batch-00': 'Fixture location caused outside-git cases to enter admission/wait; a store-reader close timing assertion also failed. Interrupted cases and that assertion were rechecked with a permitted outside-git basetemp.',
    'pytest-importer-batch-01': 'A real resume validation regression was found: source inheritance discarded invalid requested review fields before the effective-task guard. Guard restored before inheritance; focused validation/resume recheck passed.',
    'pytest-resume-validation-recheck-01': 'All resume validation and continuation cases passed after restoring the effective-task guard.',
    'pytest-importer-batch-10': 'Interrupted for a late safety audit. Manual counts and elapsed progress span are qualified in the exact record; a later full guarded selection supersedes this attempt.',
    'pytest-importer-batch-11': 'Interrupted after V1 denied native guardian reads indefinitely. Taskpolicy/opaque-fixture refusals caused guard-baseline failures; manual counts and observed span are qualified in the exact record. V2 reruns the full selection.',
    'pytest-importer-batch-12': 'Full V2 guarded selection; six R5 fixture GraphQL launches were refused. A later exact synthetic Python boundary recheck passed all17 file cases. Earlier identical interrupted selections are preserved but excluded from aggregate counts.',
    'pytest-importer-extras-01': 'The installed-publish-hook self-test is refused by the strict command guard; this one environment failure remains reported without an unsafe exemption.',
    'pytest-final-contracts-01': 'The refusal invariant found two new CLI ProtocolError constructions missing reason metadata. Both now name refused-args; CLI330 and final447 rechecks passed.',
    'pytest-cli-final-reasons-01': 'All330 changed CLI-file cases passed after adding refusal reason metadata.',
    'pytest-r5-synthetic-graphql-01': 'Scratch callback qualification assumed a module name and did not activate; six guard refusals remained. Qualification changed to the exact resolved module file path.',
    'pytest-r5-synthetic-graphql-02': 'All17 R5 cases passed with the exact synthetic read-only GraphQL callback; no external gh command executed.',
    'pytest-final-contracts-02': 'Final required four-file check passed all447 cases after the remaining rechecks.',
}
COUNT_NAMES = {
    'passed': 'passed', 'failed': 'failed', 'error': 'errors', 'errors': 'errors',
    'skipped': 'skipped', 'xfailed': 'xfailed', 'xpassed': 'xpassed',
    'deselected': 'deselected', 'unfinished': 'unfinished',
}


def cell(value: str) -> str:
    return value.replace('|', '\\|').replace('\n', '<br>')


def invocation(row: dict) -> tuple[str, str, str]:
    args = list(row['args'])
    basetemp = ''
    for index, arg in enumerate(args):
        if arg.startswith('--basetemp='):
            basetemp = arg.partition('=')[2]
            del args[index]
            break
        if arg == '--basetemp' and index + 1 < len(args):
            basetemp = args[index + 1]
            del args[index:index + 2]
            break
    name = Path(basetemp).name
    if args[:2] == ['-p', 'sf_fix1_safety']:
        prefix = 'P4'
        del args[:2]  # run4.py injects this pair itself.
    else:
        prefix = 'PH' if name == 'pytest-cli-family-extra-red1' else 'P0'
        if args[:3] == ['-q', '-n', '0']:
            del args[:3]  # Included in the P0/PH definition below.
    return prefix + (' ' + shlex.join(args) if args else ''), basetemp, name


def purpose(row: dict, name: str) -> str:
    if name in INTERPRETATIONS:
        return INTERPRETATIONS[name]
    if '-red' in name:
        return 'Deliberate pre-fix red; regression reproduced.'
    if row.get('exit') == 0:
        return 'Passed.'
    if row.get('exit') in (2, 130):
        return 'Interrupted/collection failure; see linked exact record.'
    return 'Validation failure; see linked exact record.'


def result(row: dict) -> str:
    counts = row.get('counts', {})
    parts = [f"{counts[key]} {'error' if label == 'errors' and counts[key] == 1 else label}"
             for key, label in COUNT_NAMES.items() if counts.get(key)]
    for key, value in counts.items():
        if key and key not in COUNT_NAMES and value:
            parts.append(f'{value} {key}')
    unfinished = re.search(r'with\s*(\d+)\s*unfinished', row.get('note', ''))
    if unfinished and not counts.get('unfinished'):
        parts.append(f'{unfinished[1]} unfinished (record note)')
    return f"exit {row['exit']}: " + (', '.join(parts) if parts else '0 tests recorded')


def named_counts(counts: dict) -> str:
    parts = [f"{counts[key]} {'error' if label == 'errors' and counts[key] == 1 else label}"
             for key, label in COUNT_NAMES.items() if counts.get(key)]
    return ', '.join(parts) if parts else '0 recorded cases'


def case_counts(row: dict) -> Counter:
    counts = Counter({key: value for key, value in row.get('counts', {}).items()
                      if key in COUNT_NAMES and key != 'deselected'})
    unfinished = re.search(r'with\s*(\d+)\s*unfinished', row.get('note', ''))
    if unfinished and not counts.get('unfinished'):
        counts['unfinished'] = int(unfinished[1])
    return counts


def batch_index(row: dict) -> int | None:
    name = invocation(row)[2]
    match = re.fullmatch(r'pytest-importer-batch-(\d+)', name)
    return int(match[1]) if match else None


def selection(row: dict, batches: list[list[str]]) -> tuple[list[str], str]:
    if isinstance(row.get('selected_files'), list):
        return row['selected_files'], 'selected_files captured in the exact ledger record'
    index = batch_index(row)
    if index is not None and index < len(batches):
        return batches[index], f'importer-batches.json[{index}] snapshot'
    for argument in row['args']:
        if argument.startswith('@'):
            path = Path(argument[1:])
            if path.is_relative_to(SCRATCH) and path.is_file():
                return path.read_text().splitlines(), f'{path.name} response-file snapshot'
    return [], ''


def focused_full_files(row: dict) -> list[str]:
    # Whole-file successes can be summed only when their file sets are disjoint.
    # Filtered/node-specific reruns stay in the command table, outside this sum.
    if row.get('exit') != 0 or batch_index(row) is not None:
        return []
    if '-k' in row['args'] or '-m' in row['args']:
        return []
    paths = [value for value in row['args'] if value.startswith('tests/')]
    if not paths or any(not path.endswith('.py') or '::' in path for path in paths):
        return []
    return paths


def aggregate(rows: list[tuple[int, dict]], batches: list[list[str]]) -> dict:
    # A full later rerun can supersede an interrupted selection even if its
    # launcher uses a different batch number. Keep all attempts in the table.
    by_selection = {}
    for number, row in rows:
        index = batch_index(row)
        if index is not None:
            files, _ = selection(row, batches)
            key = tuple(sorted(set(files)))
            by_selection.setdefault(key, []).append((number, row))
    primary = []
    superseded = []
    for files, attempts in by_selection.items():
        completed = [attempt for attempt in attempts if attempt[1].get('exit') in (0, 1)]
        preferred = (completed or attempts)[-1]
        primary.append(preferred)
        superseded.extend({'ledger_line': number, 'by_ledger_line': preferred[0],
                           'reason': 'same full selection; prefer latest completed attempt'}
                          for number, _row in attempts if number != preferred[0])
    file_owners = {}
    overlaps = set()
    primary_counts = Counter()
    primary_rows = []
    for number, row in sorted(primary):
        index = batch_index(row)
        files, source = selection(row, batches)
        for path in files:
            if path in file_owners:
                overlaps.add(path)
            file_owners[path] = index
        primary_counts.update(case_counts(row))
        primary_rows.append({'batch': index, 'ledger_line': number, 'files': len(files),
                             'selection_source': source, 'counts': dict(case_counts(row))})
    focused_files = set()
    focused_counts = Counter()
    focused_rows = []
    for number, row in reversed(rows):
        paths = focused_full_files(row)
        if paths and not focused_files.intersection(paths):
            focused_files.update(paths)
            focused_counts.update(case_counts(row))
            focused_rows.append({'ledger_line': number, 'files': paths, 'counts': dict(case_counts(row))})
    return {
        'ledger_records': len(rows),
        'primary_batches': primary_rows,
        'superseded_primary_attempts': superseded,
        'primary_distinct_files': len(file_owners),
        'primary_counts': dict(primary_counts),
        'primary_file_overlaps': sorted(overlaps),
        'focused_disjoint_green_rows': sorted(focused_rows, key=lambda value: value['ledger_line']),
        'focused_distinct_files': len(focused_files),
        'focused_counts': dict(focused_counts),
        'scope_note': 'Primary and focused aggregates overlap and must not be added. Focused node/filter reruns are excluded; identical primary file selections prefer the latest completed attempt, preserving interrupted attempts only in the command table.',
    }


def guard_counts(path: Path) -> dict:
    groups = {}
    if not path.is_file():
        return groups
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        raw = Path(entry.get('basetemp', 'unassigned-earlier-records'))
        parts = raw.parts
        # Workers add /popen-gwN. Match the controller's unique pytest root.
        cut = next((index + 1 for index, part in enumerate(parts) if part.startswith('pytest')), len(parts))
        base = str(Path(*parts[:cut]))
        group = groups.setdefault(base, {})
        group[entry['nodeid']] = entry['reason']
    return {base: {'unique_nodes': len(nodes),
                   'reasons': dict(Counter(nodes.values())),
                   'nodes': sorted(nodes)} for base, nodes in groups.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, default=SCRATCH / 'ledger.jsonl')
    parser.add_argument('--output', type=Path, default=SCRATCH / 'ledger-table.md')
    parser.add_argument('--evidence-link', default=EVIDENCE)
    parser.add_argument('--batches', type=Path, default=SCRATCH / 'importer-batches.json')
    parser.add_argument('--selection-output', type=Path, default=SCRATCH / 'batch-selections.jsonl')
    parser.add_argument('--aggregate-output', type=Path, default=SCRATCH / 'ledger-aggregate.json')
    parser.add_argument('--summary-output', type=Path, default=SCRATCH / 'ledger-summary.md')
    options = parser.parse_args()
    rows = []
    for number, line in enumerate(options.ledger.read_text().splitlines(), 1):
        if line.strip():
            rows.append((number, json.loads(line)))
    batches = json.loads(options.batches.read_text()) if options.batches.is_file() else []
    selections = []
    selection_lines = {}
    for number, row in rows:
        files, source = selection(row, batches)
        if files:
            selections.append({'ledger_line': number, 'batch': batch_index(row),
                               'source': source, 'selected_files': files})
            selection_lines[number] = len(selections)
    options.selection_output.write_text(''.join(json.dumps(row) + '\n' for row in selections))
    summary = aggregate(rows, batches)
    summary['guard_environment_skips'] = guard_counts(SCRATCH / 'guard-skips.jsonl')
    options.aggregate_output.write_text(json.dumps(summary, indent=2) + '\n')
    summary_text = (
        f"Primary importer batches cover {summary['primary_distinct_files']} disjoint test files: "
        f"{named_counts(summary['primary_counts'])}. Identical selections use the latest completed attempt; focused reruns are excluded. "
        f"Separately, the latest nonoverlapping whole-file focused green selections contain "
        f"{named_counts(summary['focused_counts'])} across {summary['focused_distinct_files']} files. "
        'These scopes overlap and are not added together.'
    )
    if summary['primary_file_overlaps']:
        summary_text = 'Primary batch file overlap prevents a unique primary-case aggregate. ' + summary_text
    if summary['guard_environment_skips']:
        guard_summary = '; '.join(f"{Path(base).name}: {group['unique_nodes']} distinct guard environment skips"
                                  for base, group in summary['guard_environment_skips'].items())
        summary_text += ' Separately recorded guard refusals/exclusions (already included in pytest skips): ' + guard_summary + '.'
    options.summary_output.write_text(summary_text + '\n')
    selection_link = str(Path(options.evidence_link).parent / options.selection_output.name)
    output = [
        'Test commands and results (launcher elapsed time except explicitly marked manual observed spans; every basetemp is distinct). '
        f'Full exact pytest arguments and raw counts are in the [JSONL ledger]({options.evidence_link}). '
        'The empty raw-count key records setup/teardown phases and is omitted from test counts below.',
        '',
        summary_text,
        '',
        'Prefixes used below; append the displayed arguments and `--basetemp=<Basetemp>`:',
        '',
        '```sh',
        'P0 = ' + WRAPPER + '/private/tmp/sf-a2-fix1/run.py -q -n 0',
        'P4 = ' + WRAPPER + '/private/tmp/sf-a2-fix1/run4.py',
        'PH = ' + WRAPPER + '/private/tmp/sf-a2-fix1/run_historical_cli.py -q -n 0',
        '```',
        '',
        '`P4` injects `-p sf_fix1_safety`, which remains visible in the exact JSONL arguments. '
        '`PH` preloads the original CLI from `git archive 5b06c1826bc0 subfleet`; current product files were untouched.',
        '',
        f'Directory-style importer commands use the selected module list captured in the [selection sidecar]({selection_link}); '
        'they do not run every module beneath those directories.',
        '',
        '| Record | Command arguments | Basetemp | Selected modules | Result | Elapsed | Interpretation |',
        '| --- | --- | --- | --- | --- | ---: | --- |',
    ]
    basetemps = []
    for number, row in rows:
        command, basetemp, name = invocation(row)
        basetemps.append(basetemp)
        reference = f'[L{number}]({options.evidence_link}#L{number})'
        files, source = selection(row, batches)
        modules = f'[{len(files)} files]({selection_link}#L{selection_lines[number]})' if files else 'explicit arguments'
        elapsed = f"{row['seconds']:.2f} s"
        if 'observed progress span' in row.get('note', '') or 'controller progress span' in row.get('note', ''):
            elapsed += ' observed span'
        output.append('| ' + ' | '.join((
            reference, '`' + cell(command) + '`', '`' + cell(basetemp) + '`',
            modules, cell(result(row)), elapsed, cell(purpose(row, name)),
        )) + ' |')
    if len(basetemps) != len(set(basetemps)):
        output.extend(('', 'Duplicate basetemps are present in the source ledger; inspect the linked records.'))
    output.extend((
        '',
        'Early temporary-root deviation: CLI red/initial green runs started before the launcher intercepted '
        'explicit `dir=/tmp` allocations. Unit fixtures created and removed socket roots outside the job scratch '
        'using their original cleanup, without the requested `sf-a2-fix1` guard. These nine paths are known:',
        '',
        *('- `' + path + '`' for path in KNOWN_EARLY_ROOTS),
        '',
        'Those runs also used worktree `sfwork-*` fallbacks. Successful-case paths were not logged and were '
        'already deleted when interception was added, so a complete initial path inventory cannot be reconstructed. '
        'Later allocations were named/logged and guarded by the corrected launcher. This is a deviation, '
        'not a claim that the initial folder-recording requirement was met.',
        '',
    ))
    options.output.write_text('\n'.join(output))
    print(f'Rendered {len(rows)} commands to {options.output}; {len(set(basetemps))} distinct basetemps.')


if __name__ == '__main__':
    main()
