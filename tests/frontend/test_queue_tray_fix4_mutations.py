"""Each fix-4 mutation must fail its named, already passing regression."""
from pathlib import Path
import os

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.test_review_r219_fix4 import (
    fix4_pin,
    test_n1_n2_claim_requeue_defer_never_releases_a_tray_row as claim_regression,
    test_d1_a_deferred_timeline_send_never_goes_back as deferred_regression,
    test_n5_n5b_n6_watch_discovers_repairs_before_publishing as unknown_regression,
    test_n9_app_row_scroll_suspends_even_an_already_scheduled_pin as pin_regression,
)
from tools.check_queue_tray_fix4_mutations import MUTATIONS, compile_mutants

pytestmark = needs_swift


@pytest.fixture(scope='module')
def mutants(tmp_path_factory):
    supplied = os.environ.get('SUBFLEET_FIX4_MUTANTS')
    directory = Path(supplied) if supplied else tmp_path_factory.mktemp('fix4-mutants')
    if not supplied:
        compile_mutants(directory)
    assert all((directory / name / 'probe').is_file() for name in MUTATIONS)
    return directory


@pytest.mark.parametrize('mutation', list(MUTATIONS))
def test_each_mutation_fails_its_named_regression(mutation, mutants, core_probe, fix4_pin, tmp_path):
    mutant = mutants / mutation / 'probe'
    if mutation == 'claim_moves_tray':
        check = lambda probe, path: claim_regression(probe, 0)
    elif mutation == 'deferred_returns_to_tray':
        check = lambda probe, path: deferred_regression(probe)
    elif mutation == 'skip_unknown_fetch':
        check = lambda probe, path: unknown_regression(probe, path, 'leave')
    else:
        check = lambda probe, path: pin_regression(run_probe(probe), 'approval')
    if mutation == 'app_scroll_keeps_pin':
        pin_regression(fix4_pin, 'approval')
    else:
        check(core_probe, tmp_path / 'control')
    with pytest.raises(AssertionError):
        check(mutant, tmp_path / 'mutant')
    print(f'{mutation} killed by {check.__name__ if check.__name__ != "<lambda>" else {"claim_moves_tray": claim_regression.__name__, "deferred_returns_to_tray": deferred_regression.__name__, "skip_unknown_fetch": unknown_regression.__name__, "app_scroll_keeps_pin": pin_regression.__name__}[mutation]}')
