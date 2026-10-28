"""#271 fix r3: the decision-2 mutant the brief names, and the deviation's, each against its killer.

Run with pytest from the repository root. As in fix r2's harness, production files are never
edited: each mutant replaces one `TurnRunner` method, compiled from its source with one change,
for the length of one test. A mutant counts as killed only when its named test fails with an
AssertionError, and each killer passes unmutated (the controls). The brief's two decision-1
mutants (the helper without the peek, a peek that reaps) are in `decision1_candidate.py`,
since decision 1 is not built.
"""

from contextlib import contextmanager
import inspect
import textwrap

import pytest

from subfleet.conversations import runner as runner_module
from tests.fake import test_turn_relay_exit_r3 as r3
from tests.fake import test_turn_relay_exit_recovery as recovery

SHORT_READ = ("and self.offset + len(self.partial) - position < READ_CHUNK):",
              "and not progressed):")
MUTANTS = {
    # The brief's: an entry size still unknown after the retries is read as zero.
    "unknown-backlog-read-as-zero": ("_stdout_size", ("    return None", "    return 0")),
    # The deviation's: only an empty read catches up an unknown entry size (the decision as written).
    "short-read-catch-up-removed": ("_drain_stdout", SHORT_READ),
}
KILLERS = {
    "unknown-backlog-read-as-zero": lambda r, patch:
        r3.test_every_entry_stat_failing_still_reads_the_whole_backlog_and_settles(r, patch, "proof"),
    "short-read-catch-up-removed": lambda r, patch:
        r3.test_an_unknown_entry_size_still_bounds_a_writer_that_never_pauses(r, patch),
}


def apply(patch, name):
    method, (before, after) = MUTANTS[name]
    source = textwrap.dedent(inspect.getsource(getattr(runner_module.TurnRunner, method)))
    assert source.count(before) == 1, (name, before)
    scope = dict(vars(runner_module))
    exec(compile(source.replace(before, after), f"<fix r3 mutant {name}>", "exec"), scope)
    patch.setattr(runner_module.TurnRunner, method, scope[method])


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_kills_the_mutant(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        apply(patch, name)
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            with pytest.raises(AssertionError):
                KILLERS[name](r, patch)


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_passes_unmutated(tmp_path, name):
    with pytest.MonkeyPatch.context() as patch:
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            KILLERS[name](r, patch)
