"""Compile four fix-4 mutants in the foreground, outside the pytest lock.

The mutation tests call each named regression with these binaries. Source
copies live in scratch; this script never edits the checkout's Swift sources.
"""
from pathlib import Path
import os
import platform
import subprocess
import sys

from tests.frontend.swift import ROOT, app_sources

CORE = [ROOT / 'tests/frontend' / name for name in (
    'CoreProbe.swift', 'CoreProbeScenarios.swift', 'CoreProbeLive.swift', 'CoreProbeSearch.swift',
    'CoreProbeQueue.swift', 'CoreProbeQueueEngine.swift', 'CoreProbeSuggestions.swift')]
MUTATIONS = {
    'claim_moves_tray': ('Timeline.swift',
        '''                || (turn.messageState == .waiting && !turn.hasBoundJob && (turn.stateReason == "dispatching"
                    || turn.stateReason?.hasPrefix("external-writer:") == true
                    || turn.stateReason?.hasPrefix("deferred:") == true))''', ''),
    'deferred_returns_to_tray': ('Timeline.swift',
        'else if turn.queuePlacement == nil {',
        'else if turn.queuePlacement == nil || turn.stateReason?.hasPrefix("deferred:") == true {'),
    'skip_unknown_fetch': ('ConversationStore.swift',
        'receipts: try receipts(for: wanted)', 'receipts: []'),
    'app_scroll_keeps_pin': ('ConversationOpeningPin.swift',
        '''            guard let self, note.object as? String == self.conversationID else { return }
            self.personScrolled = true''',
        '''            guard let self, note.object as? String == self.conversationID else { return }
            _ = self'''),
}


def compile_mutants(directory: Path, names: list[str] | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    for name, (filename, old, new) in MUTATIONS.items():
        if names is not None and name not in names:
            continue
        target = ROOT / 'app/Sources' / filename
        source = target.read_text()
        assert source.count(old) == 1, (name, source.count(old))
        variant = directory / name
        variant.mkdir(exist_ok=True)
        altered = variant / filename
        altered.write_text(source.replace(old, new))
        sources = [altered if path == target else path for path in app_sources()]
        view = name == 'app_scroll_keeps_pin'
        probes = [ROOT / 'tests/frontend/ReviewR219Fix4PinProbe.swift'] if view else CORE
        flags = ['-D', 'SUBFLEET_VIEW_TEST' if view else 'SUBFLEET_MODEL_TEST']
        if os.environ.get('SUBFLEET_SWIFT_NESTED_SANDBOX') == 'off':
            flags += ['-Xfrontend', '-disable-sandbox']
        command = ['xcrun', 'swiftc', '-whole-module-optimization', *flags, '-parse-as-library',
                   '-target', f'{platform.machine()}-apple-macos14.0', *map(str, sources),
                   *map(str, probes), '-o', str(variant / 'probe-building')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=1100)
        assert result.returncode == 0, result.stderr[-12000:]
        (variant / 'probe-building').replace(variant / 'probe')
        print(f'Compiled {name}', flush=True)


if __name__ == '__main__':
    compile_mutants(Path(sys.argv[1]), sys.argv[2:] or None)
