from contextlib import contextmanager
import importlib.util
import inspect
from pathlib import Path
import textwrap
import pytest
from tests.fake import test_turn_relay_exit_properties as properties
from tests.fake import test_turn_relay_exit_recovery as recovery
from tests.fake import test_turn_relay_exit_review as review
from tests.fake import test_turn_relay_exit_r3 as r3
from subfleet.conversations.runner import TurnRunner
from subfleet import relay as relay_module

path = Path.cwd() / 'docs/reports/2026-10-28-relay-exit-r2/named_mutants.py'
spec = importlib.util.spec_from_file_location('r2_named_mutants', path)
named = importlib.util.module_from_spec(spec)
spec.loader.exec_module(named)

DEVIATIONS = {
    'receipt_after_the_drain_bound': lambda r,p: review.test_a_receipt_after_the_drain_bound_does_not_extend_it(r,p),
    'restart_in_the_active_drain': lambda r,p: review.test_restart_in_the_active_drain_rearms_from_available_proof(r,p),
    'drain_is_bounded[at-bound]': lambda r,p: properties.test_proved_exit_drain_is_bounded_on_the_fake_clock(reaches_eof=False),
    'drain-entry[receipt]': lambda r,p: review.test_every_exit_drain_entry_arms_the_bound(r,p,'receipt'),
    'drain-entry[owner-ended]': lambda r,p: review.test_every_exit_drain_entry_arms_the_bound(r,p,'owner-ended'),
}

@pytest.mark.parametrize('name', DEVIATIONS)
def test_deviation_sensitivity(name, tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        named.apply(patch, 'clock-started-at-entry')
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            try:
                DEVIATIONS[name](r,patch)
            except AssertionError:
                print('DEVIATION', name, 'KILLED')
            else:
                print('DEVIATION', name, 'SURVIVED')

@pytest.mark.parametrize('retry', [True, False], ids=['candidate-retry', 'retry-removed'])
def test_transient_stat_failure_candidate_and_mutant(tmp_path, retry):
    original = TurnRunner._stdout_size
    def retry_once(self):
        try:
            return original(self)
        except OSError:
            return original(self)
    with pytest.MonkeyPatch.context() as patch:
        if retry:
            patch.setattr(TurnRunner, '_stdout_size', retry_once)
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            if retry:
                r3.test_a_transient_entry_stat_failure_still_settles_the_exited_turn(r, patch, 'owner-ended')
            else:
                with pytest.raises(AssertionError):
                    r3.test_a_transient_entry_stat_failure_still_settles_the_exited_turn(r, patch, 'owner-ended')


def apply_kernel_peek_candidate(patch):
    source = textwrap.dedent(inspect.getsource(relay_module.RelayServer._refusal))
    before = 'exited = child is not None and child.poll() is not None'
    after = before + '''
    if child is not None and not exited and hasattr(child, 'pid'):
        observed = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        exited = observed is not None
'''
    assert source.count(before) == 1
    scope = dict(vars(relay_module))
    exec(compile(source.replace(before, after), '<review candidate: kernel exit peek>', 'exec'), scope)
    patch.setattr(relay_module.RelayServer, '_refusal', scope['_refusal'])


@pytest.mark.parametrize('peek', [True, False], ids=['candidate-kernel-peek', 'peek-removed'])
def test_kernel_exit_proof_candidate_and_mutant(tmp_path, peek):
    with pytest.MonkeyPatch.context() as patch:
        if peek:
            apply_kernel_peek_candidate(patch)
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            if peek:
                r3.test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(r, patch)
            else:
                with pytest.raises(AssertionError, match='relay-failed'):
                    r3.test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(r, patch)
