"""C-6.22: common spellings of one GitHub PR share its review history."""

import pytest

from subfleet.review_subject import normalize_subject


@pytest.mark.parametrize("subject", [
    "https://github.com/Owner/Repository/pull/314/changes",
    "https://github.com/Owner/Repository/pull/314/commits",
    "https://github.com/Owner/Repository/pull/314/checks",
    "https://github.com/Owner/Repository/pull/314/files/0123456789abcdef",
    "github.com/Owner/Repository/pull/314",
    "https://www.github.com/Owner/Repository/pull/314",
    "Owner/Repository#314",
])
def test_github_pr_spellings_and_repository_case_have_one_subject(subject):
    assert normalize_subject(subject) == "https://github.com/owner/repository/pull/314"
