"""Only the ten mutations named by the r1 brief; each runs its named killer."""
from contextlib import contextmanager
import inspect
import textwrap

import pytest

from subfleet.conversations import runner as runner_module
from subfleet import relay as relay_module
from tests.fake import test_turn_relay_exit as first
from tests.fake import test_turn_relay_exit_recovery as review
from tests.fake import test_turn_relay_exit_properties as properties

NAMES = ["exit-bypass-removed", "exit-bypass-unconditional", "ignore-exit-argument", "pipe-alone-proves-exit",
         "replay-passes-false", "early-return-keeps-failed", "exit-evidence-dropped", "drain-bound-removed",
         "replay-io-guards-removed", "proved-exit-sets-failed"]

def mutate(patch, cls, name, transform, module):
    source = textwrap.dedent(inspect.getsource(getattr(cls, name)))
    changed = transform(source)
    assert changed != source
    scope = dict(vars(module))
    exec(compile(changed, '<named mutant ' + name + '>', 'exec'), scope)
    patch.setattr(cls, name, scope[name])


@pytest.mark.parametrize('name', NAMES)
def test_named_mutant_is_killed(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        cls = runner_module.TurnRunner
        if name == 'exit-bypass-removed':
            mutate(patch, cls, '_relay_lost', lambda s: s.replace('if child_exited or self._process_gone():', 'if False:'), runner_module)
        elif name == 'exit-bypass-unconditional':
            mutate(patch, cls, '_relay_lost', lambda s: s.replace('if child_exited or self._process_gone():', 'if True:'), runner_module)
        elif name == 'ignore-exit-argument':
            mutate(patch, cls, '_relay_lost', lambda s: s.replace('if child_exited or self._process_gone():', 'if self._process_gone():'), runner_module)
        elif name == 'pipe-alone-proves-exit':
            mutate(patch, relay_module.RelayServer, '_refusal', lambda s: s.replace('child is not None and child.poll() is not None', 'child is not None'), relay_module)
        elif name == 'replay-passes-false':
            mutate(patch, cls, '_handshake', lambda s: s.replace('child_exited=status is not None and status.get("child") == "exited"', 'child_exited=False'), runner_module)
        elif name in ('early-return-keeps-failed', 'proved-exit-sets-failed'):
            mutate(patch, cls, '_relay_lost', lambda s: s.replace('if child_exited or self._process_gone():', 'if child_exited or self._process_gone():\n        self.relay_failed = True'), runner_module)
        elif name == 'exit-evidence-dropped':
            mutate(patch, cls, '_relay_lost', lambda s: s.replace('self.exit_proved_at = self.clock()', 'pass  # exit proof forgotten'), runner_module)
        elif name == 'drain-bound-removed':
            mutate(patch, cls, '_run', lambda s: s.replace('>= EXIT_DRAIN_S', '>= float("inf")'), runner_module)
        elif name == 'replay-io-guards-removed':
            # Both guards cover replay-generated frames: remove this path's
            # I/O distinction while leaving P2 and title/steer checks intact.
            for method in ('_send_frames', '_transmit'):
                mutate(patch, cls, method, lambda s: s.replace('self.relay_io_ended', 'self.relay_failed'), runner_module)
        if name == 'drain-bound-removed':
            with pytest.raises(AssertionError):
                properties.test_proved_exit_drain_is_bounded_on_the_fake_clock.hypothesis.inner_test(reaches_eof=False, step=1.0)
        elif name in ('replay-passes-false', 'exit-evidence-dropped', 'replay-io-guards-removed'):
            with contextmanager(review.recovery.__wrapped__)(tmp_path, patch) as r:
                with pytest.raises(AssertionError):
                    if name == 'replay-passes-false':
                        review.test_replay_exit_status_prevents_a_relay_stop_before_receipt(r, patch)
                    elif name == 'exit-evidence-dropped':
                        review.test_proved_exit_without_a_receipt_reaches_eof(r, patch, 'reply')
                    else:
                        review.test_exit_refusal_does_not_permit_a_later_frame(r, patch)
        else:
            with contextmanager(first.refused_turn.__wrapped__)(tmp_path, patch) as make:
                with pytest.raises(AssertionError):
                    if name in ('exit-bypass-unconditional', 'pipe-alone-proves-exit'):
                        first.test_a_settings_refusal_while_the_child_lives_is_a_relay_failure(make, 'failed', False)
                    else:
                        first.test_a_settings_refusal_after_the_child_exits_keeps_its_end_reason(make, 'failed', 'reply')
