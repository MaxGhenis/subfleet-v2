"""Count execution independently of the property's own coverage counter."""
from collections import Counter
import json
from pathlib import Path
from tests.fake import test_turn_relay_exit_properties as p
from subfleet.relay import RelayServer
from subfleet.conversations.runner import TurnRunner

def test_independent_property_execution_counter(monkeypatch):
    counts = Counter()
    refused, constructed, backlog, receipts = [], [], [], []
    play, apply, init, drain = p._play, RelayServer.apply, TurnRunner.__init__, TurnRunner._drain_stdout
    write_text = Path.write_text
    def write_spy(self, data, *args, **kwargs):
        if self.name == 'exit.json':
            receipts.append(json.loads(data).get('rc'))
        return write_text(self, data, *args, **kwargs)
    def apply_spy(self, frame):
        answer = apply(self, frame)
        if answer.get('error') in ('failed', 'closed'):
            refused.append(answer['error'])
        return answer
    def init_spy(self, *args, **kwargs):
        init(self, *args, **kwargs)
        constructed.append(self)
    def drain_spy(self):
        if (self.adir / 'stdout').stat().st_size > self.offset + len(self.partial):
            backlog.append(self)
        return drain(self)
    def play_spy(schedule, seen):
        refused.clear(); constructed.clear(); backlog.clear(); receipts.clear()
        play(schedule, seen)
        counts['examples'] += 1
        counts['restarts'] += len(constructed) > 1
        counts['backlog'] += bool(backlog)
        counts['actual-refusals=' + str(min(len(refused), 2))] += 1
        counts['guardian-receipt-published'] += 0 in receipts
        counts['synthetic-containment-receipt'] += -15 in receipts
        counts['refusals-scheduled-but-none-reached-relay'] += bool(
            not refused and any(event[0] == 'refusal' for event in schedule.events))
    monkeypatch.setattr(RelayServer, 'apply', apply_spy)
    monkeypatch.setattr(TurnRunner, '__init__', init_spy)
    monkeypatch.setattr(TurnRunner, '_drain_stdout', drain_spy)
    monkeypatch.setattr(p, '_play', play_spy)
    monkeypatch.setattr(Path, 'write_text', write_spy)
    p.test_loss_interleavings_keep_one_outcome_and_all_stdout()
    print('INDEPENDENT', json.dumps(dict(counts), sort_keys=True))
    assert counts['examples'] == 154
    assert counts['restarts'] and counts['backlog'] and counts['actual-refusals=1']
