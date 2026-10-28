"""Round 12: quiet setup may normalize a valid landing, never a distant clip.

The full view replays r10's valid 24 pt callback followed by a 650 pt clip.
The smaller AppKit diagnostic executes the checked-in barrier on an isolated clip.
"""
import json
import os
import platform
import subprocess

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe
from tests.frontend.test_review_r219_fix8_mutations import mutant_source
from tests.frontend.test_review_r219_fix13_view import (
    assert_landed_once, assert_landing_follows, assert_natural_presentation,
    assert_quiet_landing_order, own_send_landings,
)

pytestmark = needs_swift


@pytest.fixture(scope="module")
def setup_probe(tmp_path_factory):
    source = (ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift").read_text()
    old = '            self.trace.append(entry.merging(["at": self.at(), "position": self.position()]) { _, new in new })'
    assert source.count(old) == 1
    source = source.replace(old, '''            if entry["event"] as? String == "own_send_landed" {
                self.clipTo(fromEnd: 24)
            }
''' + old)
    old = '        let landedAt = landed()?["at"] as? Double ?? 0'
    assert source.count(old) == 1
    source = source.replace(old, old + '''
        ProbeWaits.failureFile = root.appendingPathComponent("probe-failure.json")
        ProbeWaits.context = { ["steps": record.steps, "trace": record.trace] }
        if let text = ProcessInfo.processInfo.environment["R254_SETUP_GAP"], let gap = Double(text) {
            record.clipTo(fromEnd: gap)
            record.step("before_zero", target: target)
        }
''')
    # Keep this deterministic rejection diagnostic short; the checked-in scene
    # retains its existing readiness budget. Both use the same correction loop.
    old = 'await waitReady("send.zero_end", view:'
    assert source.count(old) == 1
    source = source.replace(old, 'await waitReady("send.zero_end", seconds: 1, view:')
    probe = mutant_source("r12_setup_gap", "Probe.swift", source)
    return compile_probe(tmp_path_factory.mktemp("setup-gap") / "probe", probe,
                         "SUBFLEET_VIEW_TEST")


def setup_run(binary, root, gap):
    env = {key: value for key, value in os.environ.items() if not key.startswith("SUBFLEET_")}
    env["R254_SETUP_GAP"] = str(gap)
    load = os.getloadavg()[0]
    run = subprocess.run([str(binary), str(root), "fix12-quiet-regrow-natural"],
                         env=env, capture_output=True, text=True, timeout=240)
    if run.returncode == 0:
        result = json.loads(run.stdout)
    else:
        result = json.loads((root / "probe-failure.json").read_text())
    write_json(root / "result.json", result)
    context = result.get("context", result)
    steps = {step["step"]: step for step in context["steps"]}
    print(json.dumps({"load": load, "input_gap": gap, "returncode": run.returncode,
                      "before_zero": steps["before_zero"]["position"]["from_end"],
                      "quiet": steps.get("landed", {}).get("position", {}).get("from_end"),
                      "reply": steps.get("reply", {}).get("position", {}).get("from_end"),
                      "barrier": result.get("state", {})}))
    return run, result


def accept_setup(run, result):
    assert run.returncode == 0, run.stderr
    steps = {step["step"]: step for step in result["steps"]}
    assert result["visible_windows"] == 0, result
    [callback] = own_send_landings(result, steps)
    assert callback["position"]["from_end"] == 24, result
    assert_natural_presentation(result, steps)
    assert_landed_once(result, steps)
    assert_quiet_landing_order(result, steps, "quiet-regrow-natural")
    return steps


def test_valid_callback_24_pt_reaches_quiet_zero_and_reply_assertion(setup_probe, tmp_path):
    run, result = setup_run(setup_probe, tmp_path, 24)
    steps = accept_setup(run, result)
    assert steps["before_zero"]["position"]["from_end"] == 24, result
    assert_landing_follows(result, steps)


def test_quiet_setup_rejects_a_clip_650_pt_behind_after_valid_callback(setup_probe, tmp_path):
    run, result = setup_run(setup_probe, tmp_path, 650)
    with pytest.raises(AssertionError, match="send.zero_end"):
        accept_setup(run, result)
    assert run.returncode == 70 and result["wait"] == "send.zero_end", result
    assert result["state"]["zero_end_first_gap"] == 650, result
    # Lazy measurement can move the end while readiness waits. The fixture must
    # leave the gap outside its correction tolerance, rather than hold it at 650.
    assert abs(result["state"]["zero_end_gap"]) > result["state"]["zero_end_correction_tolerance"], result
    assert result["state"]["zero_end_corrected_gaps"] == [], result
    context = result["context"]
    steps = {step["step"]: step for step in context["steps"]}
    [callback] = own_send_landings(context, steps)
    assert callback["position"]["from_end"] == 24, result
    assert steps["before_zero"]["position"]["from_end"] == 650, result
    assert "landed" not in steps and "regrowing" not in steps, result


@pytest.fixture(scope="module")
def isolated_barrier(tmp_path_factory):
    text = (ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift").read_text()
    quiet_move = "@MainActor final class QuietMove {" + text.split(
        "@MainActor final class QuietMove {", 1)[1].split("\n// P2-3:", 1)[0]
    barrier = "        // The product may validly" + text.split(
        "        // The product may validly", 1)[1].split('        if landing == "quiet-regrow-late"', 1)[0]
    root = tmp_path_factory.mktemp("isolated-barrier")
    source = root / "Barrier.swift"
    source.write_text('''import AppKit
enum Theme { enum space { static let column: CGFloat = 24 } }
final class Document: NSView { override var isFlipped: Bool { true } }
@MainActor enum Wait { static var failure: [String: Any]? }
@MainActor final class Record {
    let scroll: NSScrollView
    var steps: [[String: Any]] = []
    var trace: [[String: Any]] = []
    var corrections = 0
    init(_ scroll: NSScrollView) { self.scroll = scroll }
    func at() -> Double { Date().timeIntervalSinceReferenceDate }
    func position() -> [String: Double] {
        ["from_end": Double(scroll.documentView!.frame.height - scroll.contentView.bounds.maxY)]
    }
    func clipTo(fromEnd: CGFloat) {
        corrections += 1
        scroll.contentView.scroll(to: NSPoint(x: 0, y: scroll.documentView!.frame.height - scroll.contentView.bounds.height - fromEnd))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    func step(_ name: String, target: String, _ extra: [String: Any]) {
        steps.append(extra.merging(["step": name, "position": position()]) { _, new in new })
    }
}
@MainActor func waitReady(_ name: String, view: NSView, state: () -> [String: Any], _ ready: () -> Bool) async {
    // Same readiness loop with a short diagnostic budget; report expiry to Python.
    let end = Date().addingTimeInterval(0.5)
    while !ready() {
        if Date() >= end { Wait.failure = ["wait": name, "state": state()]; return }
        view.layoutSubtreeIfNeeded()
        try? await Task.sleep(nanoseconds: 5_000_000)
    }
}
''' + quiet_move + '''
@main struct Main {
    @MainActor static func main() async {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        var out: [[String: Any]] = []
        for gap: CGFloat in [24, 650] {
            Wait.failure = nil
            let window = NSWindow(contentRect: NSRect(x: 40, y: 40, width: 300, height: 300), styleMask: [.titled], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 300, height: 300))
            let document = Document(frame: NSRect(x: 0, y: 0, width: 300, height: 2000))
            scroll.documentView = document
            window.contentView = scroll
            window.orderFront(nil)
            document.postsFrameChangedNotifications = true
            scroll.contentView.postsBoundsChangedNotifications = true
            let record = Record(scroll)
            record.clipTo(fromEnd: gap)
            let before = record.position()["from_end"]!
            record.corrections = 0
            let target = "setup-only"
            func landed() -> [String: Any]? { ["position": ["from_end": 24]] }
            struct Scene {
                let host: NSView
                func readiness() -> [String: Any] { [:] }
            }
            let scene = Scene(host: scroll)
''' + barrier + '''
            out.append(["before": before, "quiet": record.position()["from_end"]!, "corrections": record.corrections,
                        "accepted": record.steps.last?["step"] as? String == "landed", "steps": record.steps,
                        "trace": record.trace, "failure": Wait.failure ?? [:]])
            window.close()
        }
        let result: [String: Any] = ["inputs": out, "visible_windows": NSApp.windows.filter(\\.isVisible).count]
        print(String(data: try! JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    }
}
''')
    binary = root / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-target",
                            f"{platform.machine()}-apple-macos14.0", str(source), "-o", str(binary)],
                           capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr
    result = run_probe(binary, timeout=30)
    write_json(root / "result.json", result)
    print("isolated barrier: " + json.dumps({"load": os.getloadavg()[0], **result}))
    assert result["visible_windows"] == 0, result
    return result


def test_extracted_barrier_accepts_valid_24_pt_offset(isolated_barrier):
    run = isolated_barrier["inputs"][0]
    assert run["before"] == 24 and run["quiet"] == 0 and run["accepted"], run


def test_extracted_barrier_rejects_a_distant_650_pt_clip(isolated_barrier):
    run = isolated_barrier["inputs"][1]
    assert run["before"] == 650, run
    with pytest.raises(AssertionError, match="send.zero_end"):
        assert run["accepted"], json.dumps(run["failure"])
    assert run["quiet"] == 650 and run["corrections"] == 0, run
    assert run["failure"]["state"]["zero_end_first_gap"] == 650, run
    assert run["failure"]["state"]["zero_end_corrected_gaps"] == [], run
