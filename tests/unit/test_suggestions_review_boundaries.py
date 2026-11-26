"""Additional Unicode and secret-family checks for the round-1 fixes."""

from __future__ import annotations

import pytest

from subfleet.conversations import suggestions
from subfleet.conversations.store import ConversationError
from tests.unit.test_suggestion_secret_properties import CUTS, kept, recognized
from tests.unit.test_suggestions import AGENT, people, suggest  # noqa: F401
from tests.unit.test_conversation_service import svc  # noqa: F401


SECRETS = tuple(prefix + "A" * 40 for prefix in (
    "sk-ant-api03-", "sk-proj-", "sk-", "ghp_", "gho_", "github_pat_", "glpat-",
    "xoxb-", "npm_", "hf_", "sk_live_", "rk_test_",
)) + ("AKIA" + "A" * 16, "AIza" + "A" * 20, "eyJ" + "A" * 8 + "." + "B" * 8 + "." + "C" * 8,
      "Bearer abcdefgh", "https://agent:abcdefgh@example.test/repo", "api_key=abcdefgh")


@pytest.mark.parametrize("where", sorted(CUTS))
def test_r2b_recognized_secrets_straddling_unicode_cuts_are_removed(where):
    limit = CUTS[where]
    for secret in SECRETS:
        assert recognized(secret)
        for offset in range(17):
            # A title/summary is admitted only within its input bound. Other
            # fields can straddle the cut both before and after scrubbing.
            prefix = max(0, limit - len(secret) - 1 - offset) if where in ("title", "tldr") else limit - 8 - offset
            text = "界" * prefix + " " + secret
            if where not in ("title", "tldr"):
                text += " 右 " * 20
            out = kept(where, text)
            assert secret not in out and not recognized(out), (where, offset, out[-70:])
            assert len(out) <= limit


def test_r2b_secret_shaped_jobs_surrounded_by_unicode_are_rejected(people):
    for secret in SECRETS:
        with pytest.raises(ConversationError) as caught:
            suggest(people, calling_job="界 " + secret + " 右")
        assert caught.value.reason == "bad-calling-job" and caught.value.code == 2
        assert secret not in str(caught.value)
    assert people.store.query("SELECT * FROM suggestions") == []


def test_r2b_brief_admission_counts_utf8_bytes():
    accepted = "🧪" * (suggestions.BRIEF_MAX_BYTES // 4)
    assert suggestions.normalize("Title", "Summary", accepted)[0]["brief"] == accepted
    with pytest.raises(ConversationError) as caught:
        suggestions.normalize("Title", "Summary", accepted + "🧪")
    assert caught.value.reason == "bad-brief"
