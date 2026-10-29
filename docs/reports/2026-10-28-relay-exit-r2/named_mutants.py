"""#271 fix r2: the five mutants its brief names, each against its named killer.

Run with pytest from the repository root. Production files are never edited: each
mutant replaces one `TurnRunner` method, compiled from its source with one change,
for the length of one test. A mutant counts as killed only when its named test
fails with an AssertionError, and each killer passes unmutated (the controls).
`test_report_whether_the_generated_property_kills_the_mutant` prints whether the
widened property catches it too (shrinking off: a kill stops at its first example).
"""

from contextlib import contextmanager
import inspect
import textwrap

from hypothesis import Phase, settings
import pytest

from subfleet.conversations import runner as runner_module
from tests.fake import test_turn_relay_exit_properties as properties
from tests.fake import test_turn_relay_exit_recovery as recovery
from tests.fake import test_turn_relay_exit_review as review

ENTRY = "self.drain_entry_size = self._stdout_size()"
CAUGHT_UP = "if self.drain_caught_up_at is None and self.offset + len(self.partial) >= self.drain_entry_size:"
MUTANTS = {
    # Review r2's three reverts of a candidate fix.
    "status-proof-forgotten-on-a-closed-restart": ("_handshake", (
        'if status.get("child") == "exited" and self.exit_proved_at is None:', "if False:")),
    "receipt-arm-forgotten": ("_drain_stdout", (
        CAUGHT_UP, CAUGHT_UP.replace("if ", "if (self.exit_proved_at is not None or self.ended) and ", 1))),
    "owner-ended-arm-forgotten": ("_drain_stdout", (
        CAUGHT_UP, CAUGHT_UP.replace(
            "if ", 'if (self.exit_proved_at is not None or (self.adir / "exit.json").exists()) and ', 1))),
    # The brief's two more.
    "clock-started-at-entry": ("_drain_stdout", (ENTRY, ENTRY + "\n    self.drain_caught_up_at = self.clock()")),
    "entry-size-unrecorded-on-a-rebuilt-runner": ("_drain_stdout", (
        ENTRY, "self.drain_entry_size = (self._stdout_size() if not (self.replayed_message or self.ended)\n"
               "                         else self.offset + len(self.partial))")),
}
KILLERS = {
    "status-proof-forgotten-on-a-closed-restart":
        lambda r, patch: review.test_restart_recovers_exit_without_a_receipt(r, patch, "closed"),
    "receipt-arm-forgotten": lambda r, patch: review.test_every_exit_drain_entry_arms_the_bound(r, patch, "receipt"),
    "owner-ended-arm-forgotten":
        lambda r, patch: review.test_every_exit_drain_entry_arms_the_bound(r, patch, "owner-ended"),
    "clock-started-at-entry": lambda r, patch: review.test_a_backlog_on_disk_at_the_proof_is_read_in_full(r, patch),
    "entry-size-unrecorded-on-a-rebuilt-runner":
        lambda r, patch: review.test_a_rebuilt_runner_reads_its_whole_backlog_before_the_bound(r, patch),
}


def apply(patch, name):
    method, (before, after) = MUTANTS[name]
    source = textwrap.dedent(inspect.getsource(getattr(runner_module.TurnRunner, method)))
    assert source.count(before) == 1, (name, before)
    scope = dict(vars(runner_module))
    exec(compile(source.replace(before, after), f"<fix r2 mutant {name}>", "exec"), scope)
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


@pytest.mark.parametrize("name", list(MUTANTS))
def test_report_whether_the_generated_property_kills_the_mutant(name):
    """Informational: prints whether the widened property catches the mutant. The
    named killers above are the kill record."""
    settings.register_profile("fix-r2-mutants", phases=[Phase.explicit, Phase.generate])
    previous = settings.get_current_profile_name()
    settings.load_profile("fix-r2-mutants")
    try:
        with pytest.MonkeyPatch.context() as patch:
            apply(patch, name)
            try:
                properties.test_loss_interleavings_keep_one_outcome_and_all_stdout()
            except AssertionError as exc:
                where = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
                print(f"PROPERTY {name}: killed ({where[:160]})")
            else:
                print(f"PROPERTY {name}: not killed")
    finally:
        settings.load_profile(previous)
