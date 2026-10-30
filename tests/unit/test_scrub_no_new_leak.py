"""C-23.14 (D-SCR2 fix round 1, #239 review r1): no mode of the head's scrubber shows
a `SECRET7` value that 473fe2941 (this branch's base, `scrub_473fe2941`) or release/217
(`scrub_release217`) hides; a second scrub shows nothing the first hid (round 2: it may
remove more, review r2's P3-3, so this is monotone, not idempotence); and an excerpt cut
at any line break `_neutral_break` allows scrubs exactly as the whole text does there.

Two seeded generators feed it. The builder's (`_fuzz_text` in `test_scrub_key_order`,
seeds 225 and 2026) and a reconstruction of #239 review r1's, whose own code was not
kept: it is built from the shapes that review executed (an unclosed quote around a
header's name, a `Bearer` or flag ending a line, trailing spaces, carriage returns,
a flag glued to another's value, header names on consecutive lines, YAML pairs, a
partial `Bearer` cut), at the seeds the review named (4101, 4102, 9001) and two of
this round's (239, 2392); its counts are its own. A bounded Hypothesis property draws
from the same pieces."""

from __future__ import annotations

import random
import re

from hypothesis import given, strategies as st

from subfleet import refusals
from subfleet.conversations import suggestions
from subfleet.sessions import handoff
from tests.unit import scrub_473fe2941 as base473
from tests.unit import scrub_release217 as release217
from tests.unit.test_scrub_key_order import ALNUM, CI, SECRET, _fuzz_text, _neutral_breaks, _revealed

PREFIX = "request submit refused (ValueError): "
TREES = {"head": handoff, "473fe2941": base473, "release/217": release217}

# --- #239 review r1's generator, reconstructed -----------------------------------

KEYS = ("api_key", "token", "secret", "password", "passwd", "client_secret", "access_token", "auth-token",
        "db_password", "SERVICE_PASSWORD", "credentials")
HEADER_NAMES = ("Authorization", "Cookie", "Set-Cookie", "Proxy-Authorization", "X-Authorization",
                "proxy_authorization", "session_cookie", "cookie")
FLAGS = ("--password", "--passwd", "--token", "--auth-token", "--client-secret", "--api-key")
#: A value's opening quote: none, one that closes later or never, and escaped ones.
OPENERS = ("", "", "'", '"', "\\'", '\\"')
SEPARATORS = (":", "=", ": ", " = ", ":\n", ":\n  ", ":\r", "\n: ", "=\n")
HEADER_COLONS = (": ", ":", ":\n", ":\n  ", " : ", "\t: ", ":\r\n  ")
BEARER_WORDS = ("Bearer", "BEARER", "bearer")
#: Values; `{s}` is a fresh secret, `{h}` a header name, `{f}` a flag, `{k}` a key.
VALUES = ("{s}", "x", "2", "Basic {s}", "Bearer {s}", "a=1; b={s}", "{h}:", "{f}", "xyz Bearer", "xyz {f}",
          "x {f}  ", "{s}{f}", "x{f} {s}", "{k}:", "{k}=", "xyz {k}:", "Bearer", "1{f}\t{s}")
FILLER = ("ok", "x", "2", "rest", "Bearer", "--password", "--verbose", "curl -H", "sent", "====", "'", '"',
          ":authority: example.com", "abc1pi_key", "credentials", "token_type: Bearer", "headers:", "\\",
          "[REDACTED]", "a.b")
JOINS = (" ", "\n", "\n  ", "", "\r\n", "\r", "  \n", "\t", "\n\n", "\\\n  ")
#: What a secret may end in: nothing, mostly, or a credential word (#231 review r1).
ENDINGS = ("", "", "", "", "token", "_token", "secret", "Passwd", "-key")


def _secret(rng: random.Random, index: int) -> str:
    """At least nine characters with a letter and a digit, so that every secret reads
    as a token (`_token_like`); the documented converse needs a shorter one."""
    return "SECRET7" + "".join(rng.choice(ALNUM) for _ in range(rng.randint(2, 7))) + str(index) + rng.choice(ENDINGS)


_SLOT_RE = re.compile(r"\{([shfk])\}")


def _fill(template: str, rng: random.Random, secrets: list[str]) -> str:
    def slot(match: re.Match) -> str:
        if match.group(1) == "s":
            secrets.append(_secret(rng, len(secrets)))
            return secrets[-1]
        return rng.choice({"h": HEADER_NAMES, "f": FLAGS, "k": KEYS}[match.group(1)])
    return _SLOT_RE.sub(slot, template)


def _part(rng: random.Random, secrets: list[str]) -> str:
    draw = rng.random()
    if draw < 0.3:
        template = rng.choice(KEYS) + rng.choice(SEPARATORS) + rng.choice(OPENERS) + rng.choice(VALUES)
    elif draw < 0.5:
        template = rng.choice(HEADER_NAMES) + rng.choice(HEADER_COLONS) + rng.choice(OPENERS) + rng.choice(VALUES)
    elif draw < 0.6:
        template = rng.choice(BEARER_WORDS) + rng.choice((" ", "\n", "\n  ", "\r\n")) + rng.choice(
            ("{s}", "{k}: {s}", "{s}_token: x", "{f} {s}", "abc1pi_key{h}:{f} x", "x.y.z{f}"))
    elif draw < 0.7:
        template = "{f}" + rng.choice((" ", "\t", "  ", "\n", " \\\n  ")) + rng.choice(("{s}", "x{f} {s}", "{s}{f}"))
    else:
        template = rng.choice(FILLER)
    return _fill(template, rng, secrets)


def _review_text(rng: random.Random) -> tuple[str, list[str]]:
    """Two to seven parts joined by spaces, line breaks (LF, CRLF, CR, blank lines,
    trailing spaces, a shell continuation) or nothing."""
    secrets: list[str] = []
    parts = [_part(rng, secrets) for _ in range(rng.randint(2, 7))]
    return parts[0] + "".join(rng.choice(JOINS) + part for part in parts[1:]), secrets


# --- the checks -------------------------------------------------------------------

def _fold(scrubber, text: str) -> str:
    """daemon.log's line without its 2,000-character bound (`suggestions._scrub_line`)."""
    return suggestions._scrub_line(text)[0] if scrubber is handoff else scrubber.scrubbed_line(text)


def _outputs(scrubber, text: str) -> dict[str, str]:
    return {"excerpt": scrubber.scrub_secrets(text)[0], "whole": scrubber.scrub_secrets(text, whole=True)[0],
            "clean": scrubber.clean(text, 4000)[0], "daemon.log line": _fold(scrubber, text),
            "daemon.log": _fold(scrubber, PREFIX + text)}


def _check(text: str, secrets) -> dict[str, list]:
    """What breaks an invariant for one text: the head's new leaks (reference, mode,
    secret), the modes whose second scrub shows what the first hid, and the cuts whose
    excerpts disagree with the whole text's scrub."""
    found = {name: _outputs(scrubber, text) for name, scrubber in TREES.items()}
    shown = {name: {(mode, secret) for mode, out in modes.items() for secret in secrets if secret in out}
             for name, modes in found.items()}
    leaks = sorted((name, mode, secret) for name in ("473fe2941", "release/217")
                   for mode, secret in shown["head"] - shown[name])
    head = found["head"]
    unstable = [mode for mode, again in (("excerpt", handoff.scrub_secrets(head["excerpt"])[0]),
                                         ("whole", handoff.scrub_secrets(head["whole"], whole=True)[0]),
                                         ("daemon.log line", refusals.scrubbed_line(head["daemon.log line"])))
                if _revealed(head[mode], again)]
    whole = handoff._scrub(text, False)[0]
    splits = [cut for cut in _neutral_breaks(text)
              if handoff._scrub(text[:cut], False)[0] + handoff._scrub(text[cut:], False)[0] != whole]
    return {"leaks": leaks, "unstable": unstable, "splits": splits}


def _sweep(generator, seed: int, count: int) -> dict[str, list]:
    rng, failures = random.Random(seed), {"leaks": [], "unstable": [], "splits": []}
    for _ in range(count):
        text, secrets = generator(rng)
        for kind, found in _check(text, set(secrets)).items():
            if found:
                failures[kind].append((text, found))
    return failures


def test_the_builders_fuzz_shows_nothing_a_reference_hides():
    """Seeds 225 and 2026, 2,000 texts each (#239's own differential runs 4,000 of
    seed 225 against release/217 in three modes)."""
    for seed in (225, 2026):
        assert _sweep(_fuzz_text, seed, 2000) == {"leaks": [], "unstable": [], "splits": []}, seed


def test_the_reviews_generator_shows_nothing_a_reference_hides():
    """#239 review r1's seeds (4101, 4102, 9001) and this round's (239, 2392), 1,200
    texts each. At `d3a19518b` these seeds gave head-only leaks (an unclosed quote around
    a header name swallowing a line-ending `Bearer`, review P3-1), idempotence failures
    (a header name a partial `Bearer` cut exposes, P3-4) and split disagreements (P3-3)."""
    for seed in (4101, 4102, 9001, 239, 2392):
        assert _sweep(_review_text, seed, 1200) == {"leaks": [], "unstable": [], "splits": []}, seed


@st.composite
def _drawn_texts(draw):
    seed = draw(st.integers(min_value=0, max_value=2**32 - 1))
    return _review_text(random.Random(seed))


@CI
@given(drawn=_drawn_texts())
def test_no_drawn_text_shows_what_a_reference_hides(drawn):
    """The same pieces, bounded (the suite's ci profile: 200 examples, derandomized)."""
    text, secrets = drawn
    assert _check(text, set(secrets)) == {"leaks": [], "unstable": [], "splits": []}, text


def test_the_generator_draws_every_shape_the_review_found():
    """The reconstruction reaches the review's shapes, so a zero above is not an empty sweep."""
    rng, texts = random.Random(4101), []
    for _ in range(1200):
        texts.append(_review_text(rng)[0])
    shapes = {
        "unclosed quote around a header name": lambda t: any(q + h in t for q in ("'", '"') for h in HEADER_NAMES),
        "line-ending Bearer": lambda t: "Bearer\n" in t or "Bearer\r" in t,
        "flag then trailing spaces and a break": lambda t: any(f + "  \n" in t for f in FLAGS),
        "carriage return": lambda t: "\r" in t and "\r\n" not in t,
        "flag glued to a value": lambda t: any("x" + f + " " in t or "1" + f + "\t" in t for f in FLAGS),
        "header name on the next line": lambda t: any(":\n" + h + ":" in t or ":\n  " + h + ":" in t
                                                      for h in HEADER_NAMES),
        "partial Bearer cut": lambda t: "abc1pi_key" in t and "Bearer" in t,
        "shell continuation": lambda t: "\\\n" in t,
    }
    assert {name: sum(map(test, texts)) for name, test in shapes.items() if sum(map(test, texts)) < 5} == {}
