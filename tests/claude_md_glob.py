"""Claude Code 2.1.318's `claudeMdExcludes` matcher, for the patterns Subfleet writes (D-LANE-STARTUP1).

Claude Code builds the matcher in `v8t()`:

    function v8t(){let e=gt().claudeMdExcludes;if(!e||e.length===0)return()=>!1;
      let n=FWo(de(),e).filter((s)=>s.length>0);if(n.length===0)return()=>!1;
      let r=p8t.default(n,{dot:!0});
      return(s,h)=>(h==="User"||h==="Project"||h==="Local")&&r(s.replaceAll("\\\\","/"))}

and `FWo` adds a real-path spelling of each absolute pattern:

    function FWo(e,n){let r=n.map((s)=>s.replaceAll("\\\\","/"));for(let s of r){
      if(!s.startsWith("/"))continue;let h=s.search(/[*?{[]/),y=h===-1?s:s.slice(0,h),b=Cx(y);
      try{let w=No(e,b),{resolvedPath:H}=w;if(DN(H)||s2(b,w))continue;
      let z=H.replaceAll("\\\\","/");if(z!==b){let Y=z+s.slice(b.length);r.push(Y)}}catch{}}return r}

`p8t` is picomatch; an array of patterns matches when any one does. This module is
that matcher for the subset of picomatch syntax `job_startup_settings` writes, and it
refuses any other syntax (`Unsupported`) rather than guess:

* a leading `!`, which picomatch compiles to `^(?!^(?:BODY)$).*$`;
* literal characters, picomatch's regex-special ones escaped;
* picomatch's `test`: an empty input is no match, an input equal to the glob's text
  is a match;
* one-character classes `[c]` for the characters `glob_literal` brackets, and the
  ranges `[,-,]` and `["-"]`: classes whose content holds a regex metacharacter,
  which picomatch compiles to the class alone (`[!]`, whose content holds none, it
  compiles to `(?:\\[!\\]|[!])`, so that and its kind are refused here); a raw `"`
  is refused, since picomatch's parser drops it and quotes what follows;
* `{a,b}` alternatives, compiled to a group, nested to any depth and with empty
  alternatives (`{,/b{,/c}}`, review r2 of #326), as picomatch nests them
  (`(|\\/b(|\\/c))`);
* a final `/**`, compiled with `dot: true` to
  `(?:\\/(?!\\.{1,2}(?:\\/|$))(?:(?:(?!(?:^|\\/)\\.{1,2}(?:\\/|$)).)*?)|$)`.

JavaScript's `.` matches no line terminator (`\\n`, `\\r`, U+2028, U+2029), and its
`$` (without the `m` flag) only the end of input; both are kept here. The regex
shapes were printed by the bundle's own picomatch (`picomatch.makeRe`), and
`tests/fixtures/lane_startup/picomatch-2.1.318.json` records the bundle's verdicts
over generated paths, which `tests/unit/test_lane_startup_glob.py` holds this module to.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable

#: The characters `glob_literal` writes as `[c]`; each is a regex metacharacter.
CLASS_CHARS = frozenset("*?[]{}()|+.")
#: The characters it writes as the one-character range `[c-c]`.
RANGE_CHARS = frozenset(',"')
#: JavaScript's `.` without the `s` flag.
ANY = "[^\n\r  ]"
GLOBSTAR = rf"(?:/(?!\.{{1,2}}(?:/|\Z))(?:(?:(?!(?:\A|/)\.{{1,2}}(?:/|\Z)){ANY})*?)|\Z)"


class Unsupported(ValueError):
    """A pattern outside the subset this matcher states exactly."""


def js_dirname(path: str) -> str:
    """Node's `path.posix.dirname`, byte for byte (`Cx` in the bundle)."""
    if not path:
        return "."
    has_root = path[0] == "/"
    end, matched_slash = -1, True
    for index in range(len(path) - 1, 0, -1):
        if path[index] == "/":
            if not matched_slash:
                end = index
                break
        else:
            matched_slash = False
    if end == -1:
        return "/" if has_root else "."
    if has_root and end == 1:
        return "//"
    return path[:end]


def real_or_same(path: str) -> str:
    """`No(fs, path).resolvedPath` for a local path: `realpathSync`, else the path."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError:
        return path


def fwo(patterns: Iterable[str], resolve: Callable[[str], str] = real_or_same) -> list[str]:
    """`FWo`: backslashes to `/`, then a real-path spelling of each absolute pattern's
    static folder, appended (and itself walked, as `for…of` walks a growing array)."""
    out = [pattern.replace("\\", "/") for pattern in patterns]
    index = 0
    while index < len(out):
        pattern = out[index]
        index += 1
        if not pattern.startswith("/"):
            continue
        glob = re.search(r"[*?{\[]", pattern)
        folder = js_dirname(pattern if glob is None else pattern[:glob.start()])
        resolved = resolve(folder).replace("\\", "/")
        if resolved != folder:
            out.append(resolved + pattern[len(folder):])
    return out


def _body(text: str) -> str:
    out: list[str] = []
    braces: list[list[list[str]]] = []      # open braces: their alternatives so far, innermost last
    index = 0
    while index < len(text):
        current = braces[-1][-1] if braces else out
        char = text[index]
        if text.startswith("/**", index) and index + 3 == len(text) and not braces:
            current.append(GLOBSTAR)
            index += 3
            continue
        if (char == "[" and index + 4 < len(text) and text[index + 1] in RANGE_CHARS
                and text[index + 2] == "-" and text[index + 3] == text[index + 1] and text[index + 4] == "]"):
            current.append(re.escape(text[index + 1]))
            index += 5
            continue
        if char == "[":
            if index + 2 < len(text) and text[index + 2] == "]" and text[index + 1] in CLASS_CHARS:
                current.append(re.escape(text[index + 1]))
                index += 3
                continue
            raise Unsupported(f"class at {index} of {text!r}")
        if char == "{":
            braces.append([[]])
            index += 1
            continue
        if char == "," and braces:
            braces[-1].append([])
            index += 1
            continue
        if char == "}" and braces:
            alternatives = braces.pop()
            if len(alternatives) < 2:
                raise Unsupported(f"a brace with one alternative in {text!r}")
            (braces[-1][-1] if braces else out).append(
                "(?:" + "|".join("".join(item) for item in alternatives) + ")")
            index += 1
            continue
        if char in '*?()|+{},]"' or (char == "." and braces):
            # Inside braces picomatch reads two raw dots as a range (`{a..b}`).
            raise Unsupported(f"{char!r} at {index} of {text!r}")
        current.append(re.escape(char))
        index += 1
    if braces:
        raise Unsupported(f"unclosed brace in {text!r}")
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """picomatch's regex for one pattern of the subset, as a Python regex."""
    if pattern.startswith("!"):
        if pattern.startswith("!("):
            raise Unsupported(f"extglob in {pattern!r}")
        return re.compile(rf"\A(?!\A(?:{_body(pattern[1:])})\Z){ANY}*\Z")
    return re.compile(rf"\A(?:{_body(pattern)})\Z")


def matcher(patterns: Iterable[str], resolve: Callable[[str], str] = real_or_same) -> Callable[[str], bool]:
    """`v8t()` for a User, Project or Local file: whether `path` is excluded."""
    expanded = [pattern for pattern in fwo(patterns, resolve) if pattern]
    if not expanded:
        return lambda _path: False
    compiled = [(pattern, compile_pattern(pattern)) for pattern in expanded]

    def test(path: str) -> bool:
        # picomatch's `test`: an empty input never matches, and an input equal to
        # the glob's own text matches before any regex is tried.
        path = path.replace("\\", "/")
        return path != "" and any(path == pattern or regex.match(path) for pattern, regex in compiled)
    return test


def excluded(patterns: Iterable[str], path: str, resolve: Callable[[str], str] = real_or_same) -> bool:
    return matcher(patterns, resolve)(path)


def expand(pattern: str) -> list[str]:
    """The brace-free patterns `pattern`'s alternatives name, in order, with every
    class and range kept as written: `/a{,/b{,/c}}/X` gives `/a/X`, `/a/b/X` and
    `/a/b/c/X`. Only for the syntax `compile_pattern` accepts."""
    def sequence(index: int, inside: bool) -> tuple[list[str], int]:
        out = [""]
        while index < len(pattern):
            char = pattern[index]
            if inside and char in ",}":
                return out, index
            if char == "[":                 # `[c]`, `[]]`, `[c-c]`: the `]` that closes it
                close = pattern.index("]", index + 2) + 1
                out = [item + pattern[index:close] for item in out]
                index = close
            elif char == "{":
                choices: list[str] = []
                index += 1
                while True:
                    part, index = sequence(index, True)
                    choices += part
                    index += 1
                    if pattern[index - 1] == "}":
                        break
                out = [item + choice for item in out for choice in choices]
            else:
                out = [item + char for item in out]
                index += 1
        if inside:
            raise Unsupported(f"unclosed brace in {pattern!r}")
        return out, index
    return sequence(0, False)[0]


def unglob(pattern: str) -> str:
    """The one path a pattern without wildcards names (`glob_literal` undone), with a
    final `/**` kept as written."""
    out, index = [], 0
    while index < len(pattern):
        if (pattern[index] == "[" and index + 4 < len(pattern) and pattern[index + 1] in RANGE_CHARS
                and pattern[index + 2] == "-" and pattern[index + 3] == pattern[index + 1]
                and pattern[index + 4] == "]"):
            out.append(pattern[index + 1])
            index += 5
        elif pattern[index] == "[" and index + 2 < len(pattern) and pattern[index + 2] == "]":
            out.append(pattern[index + 1])
            index += 3
        else:
            out.append(pattern[index])
            index += 1
    return "".join(out)
