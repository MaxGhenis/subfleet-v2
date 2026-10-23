"""Provider approval frames shared by wall tests, without property decorators."""
from dataclasses import replace

from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.codex_turn import CodexTurn


def approval_reply(runner, provider, decision):
    """Build the provider's actual reply, with an active turn and no queued steers."""
    runner.spec = replace(runner.spec, provider=provider)
    if provider == "claude":
        runner.driver = ClaudeTurn(runner.spec, read_bytes=lambda image: b"")
        runner.driver.phase = "sent"
        runner.driver.pending["wall-approval"] = {"tool_name": "Bash", "tool_use_id": "tool-1"}
        step = runner.driver.respond("wall-approval", decision, answers={"Branch?": "dev"})
    else:
        runner.driver = CodexTurn(runner.spec)
        runner.driver.thread_id, runner.driver.turn_id, runner.driver.phase = "thread-1", "turn-1", "running"
        method = "item/permissions/requestApproval" if decision == "allow-turn" else "item/commandExecution/requestApproval"
        runner.driver.pending["wall-approval"] = (method, {"permissions": {"network": {"enabled": True}}})
        step = runner.driver.respond("wall-approval", decision)
    [frame] = step.frames
    return frame

