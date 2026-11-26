"""C-29.7: a pre-start withdrawal is one timeline line, including tombstones."""
import pytest
from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def notices(tmp_path_factory):
    probe = compile_probe(tmp_path_factory.mktemp("queue-notice") / "probe",
                          ROOT / "tests/frontend/QueueTrayNoticeProbe.swift", "SUBFLEET_VIEW_TEST")
    return run_probe(probe)


def test_prestart_withdrawal_is_one_line_without_a_second_status_caption(notices):
    for width in [300, 480]:
        rows = {row["name"]: row for row in notices["rows"] if row["width"] == width}
        assert rows["short"]["height"] == rows["long"]["height"]
        assert rows["short"]["height"] < rows["notice"]["height"]
        assert rows["short"]["buttons"] == rows["long"]["buttons"] == 0
    assert notices["visible_windows"] == 0


def test_other_message_notices_keep_their_status(notices):
    for width in [300, 480]:
        rows = {row["name"]: row for row in notices["rows"] if row["width"] == width}
        assert rows["notice-status"]["height"] > rows["notice"]["height"]
