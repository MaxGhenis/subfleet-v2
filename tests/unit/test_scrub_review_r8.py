"""C-23.14 (#239 review r8, at be4f090b6): inputs where the head shows a `SECRET7` value that
both references (473fe2941, `c2e080e44`) and release be0c2a311 hide. These are the review's
findings, minimized from its generators and pinned red: every case here fails at
be4f090b6 by design, and a fix round turns them green.

`scrub_473fe2941` stands for both references, as in `test_scrub_review_r2` to `_r7`; on
every input below release gives the references' output.

- P2-1 (new at 95420ecaf, `p34h`): a flag on the line after a swallowed header name, whose
  name is a key whose value on the next line is shorter than the run the flag rule takes
  there (`:=,v`, `==,v`: the key takes `=`). `p34h` hands the flag to the key, which takes
  `=`; in the two line modes the references' folded line gives the flag `:=,v` whole.
- P2-2 (since at least 1ad7da99c): an item at `p34h`'s second site that does not end its
  line. A `Bearer` the `Bearer` rule reads no token after, or a flag whose own value stops
  short of the line's end, keeps the union from joining the lines, so the outer header no
  longer takes the rest of the line, as it does in both references.
- P2-3 (since at least 1ad7da99c): `_header_redaction` leaves a flag's name to the key rule
  when the key's value on the next line is shorter than the flag's folded run (`:=;v`, or
  an empty quoted value `''v`); the references' folded line gives the flag the whole run.
- P2-4 (since at least 1ad7da99c): an inline header after an empty quoted value, whose
  value is a lone key past its line (`Authorization:""Cookie:apikey` and `=r v`), and a
  `Bearer` with a token mid-line at the second site."""

from __future__ import annotations

import pytest

from tests.unit.test_scrub_review_r2 import _new_leaks
from tests.unit.test_scrub_review_r6 import BOTH

R8 = {
    # P2-1: line modes
    "p2-1-plain-outer-short-key-value": "secret:'cookie:\nx --privatekey\n:=,SECRET7c5",
    "p2-1-double-equals": 'token:"Authorization:\nx --token\n==,SECRET7c5',
    # P2-2: excerpt, whole and `clean`
    "p2-2-tokenless-bearer": 'Authorization:"Authorization\n:Bearer =r SECRET7c5',
    "p2-2-short-bearer-run": 'Authorization:"Authorization\n:Bearer SECRET7',
    "p2-2-mid-line-flag": 'Authorization:"Authorization\n:--password \\"=r SECRET7c5',
    # P2-3: line modes
    "p2-3-header-flag-short-key-value": "Authorization:x --token\n:=;SECRET7c5",
    "p2-3-header-flag-empty-quoted-value": "cookie:x --token\n=''SECRET7c5",
    # P2-4: excerpt, whole and `clean`
    "p2-4-empty-quote-then-inline-header": 'Authorization:""Cookie:apikey\n=r SECRET7c5',
    "p2-4-mid-line-bearer-token": "Cookie:'secret\n=Bearer Seytoker SECRET7c5",
}


@pytest.mark.parametrize("text", R8.values(), ids=R8.keys())
def test_review_r8_s_inputs_show_nothing_both_references_hide(text):
    assert _new_leaks(text, BOTH) == {}

