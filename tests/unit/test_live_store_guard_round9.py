"""D-ST1 r9: the wrapper check fails closed, reads env as env does, and names its scope.

Executed probes launch for real and expect the guard to refuse before anything
starts. A guard that approves runs only `sw_vers`, and the failure prints what
the child printed: the escape is shown with `sw_vers`, never with the reader.
Synthetic audit events start no program. The reader is named only as a string.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import credentials
from tests.keychain_standin import SAFETY_HITS

PROPERTIES = settings(max_examples=int(os.environ.get("SUBFLEET_EXEC_GUARD_EXAMPLES", "150")),
                      deadline=None, database=None, derandomize=True,
                      suppress_health_check=[HealthCheck.function_scoped_fixture,
                                             HealthCheck.filter_too_much])
SYSTEM = "/usr/bin:/bin"
# The round-9 review's wrapper inputs, each run with PATH /usr/bin:/bin.
NEW_WRAPPERS = {
    "ksh": ["/bin/ksh", "-c", "sw_vers"],
    "csh": ["/bin/csh", "-c", "sw_vers"],
    "tcsh": ["/bin/tcsh", "-c", "sw_vers"],
    "time": ["/usr/bin/time", "sw_vers"],
    "script": ["/usr/bin/script", "-q", "/dev/null", "sw_vers"],
    "caffeinate": ["/usr/bin/caffeinate", "sw_vers"],
    "lockf": ["/usr/bin/lockf", "{scratch}/lock", "sw_vers"],
    "arch": ["/usr/bin/arch", "-arm64", "sw_vers"],
}


def recorder() -> str:
    """The session's recording `security` directory, first on the session PATH."""
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert Path(first, "security").is_file() and first.endswith("keychain-bin"), first
    return first


def refused(argv, **kwargs) -> None:
    """Launch `argv`; the guard must refuse it with the reader's code-7 refusal."""
    hits = len(SAFETY_HITS)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                stdin=subprocess.DEVNULL, **kwargs)
    except credentials.KeychainExecRefused as error:
        assert (error.code, error.reason) == (7, "keychain-test-guard")
        assert SAFETY_HITS[hits:] == []
        return
    pytest.fail(f"approved {argv!r}; the child printed {(result.stdout + result.stderr).strip()[:160]!r}")


def audit(argv, env=None, cwd=None, program=None) -> None:
    """A synthetic Popen audit event: the guard judges it, and nothing starts."""
    sys.audit("subprocess.Popen", program or argv[0], argv, cwd, env)


def refuses(argv, env=None, cwd=None) -> None:
    with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain"):
        audit(argv, env, cwd)


@contextlib.contextmanager
def deleted_working_directory(scratch: Path):
    """Run the body from a working directory that no longer exists."""
    before = os.getcwd()
    gone = scratch / "gone"
    gone.mkdir()
    os.chdir(gone)
    gone.rmdir()
    try:
        yield
    finally:
        os.chdir(before)


# P3-1: a wrapper launch whose check cannot finish is refused.

@pytest.mark.parametrize("launch", ["subprocess", "os.system"])
def test_p3_1_deleted_working_directory_is_refused(tmp_path, monkeypatch, launch):
    """getcwd ran for every entry and raised, and the guard approved the shell."""
    with deleted_working_directory(tmp_path):
        if launch == "subprocess":
            refused(["/bin/sh", "-c", "command -v sw_vers"], env={"PATH": SYSTEM})
        else:
            monkeypatch.setenv("PATH", SYSTEM)
            with pytest.raises(credentials.KeychainExecRefused):
                status = os.system("sw_vers >/dev/null")
                pytest.fail(f"os.system approved; sw_vers exited {status}")


def test_p3_1_absolute_entries_never_read_the_working_directory(tmp_path):
    """Only a relative entry needs getcwd; the session's absolute PATH does not."""
    with deleted_working_directory(tmp_path):
        result = subprocess.run(["/bin/sh", "-c", "true"], capture_output=True, timeout=60)
    assert result.returncode == 0


@pytest.mark.parametrize("case", ["relative-entry-after-deleted-cwd", "unbalanced-split-quote",
                                  "malformed-recorder"])
def test_p3_1_a_check_that_cannot_finish_is_refused(case, tmp_path):
    """Each check raises at head, and must refuse rather than approve."""
    if case == "relative-entry-after-deleted-cwd":
        # The relative entry finds nothing in a deleted directory; /usr/bin is next.
        with deleted_working_directory(tmp_path):
            refused(["/bin/sh", "-c", "sw_vers"], env={"PATH": "bin:" + SYSTEM})
    elif case == "unbalanced-split-quote":
        refuses(["/usr/bin/env", "-S", 'PATH=/usr/bin sw_vers "', "fixture-ref"], dict(os.environ))
    else:
        guard = credentials._exec_guard([], recorder_dirs=(0,))
        with pytest.raises(credentials.KeychainExecRefused):
            guard("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "true"], None, {"PATH": SYSTEM}))


def test_p3_1_text_and_bytes_path_keys_are_both_judged():
    """Popen passes both keys; the shell kept the bytes one and ran /usr/bin/sw_vers."""
    session = os.environ["PATH"]
    refused(["/bin/sh", "-c", "command -v sw_vers"], env={"PATH": session, b"PATH": SYSTEM.encode()})
    refused(["/bin/sh", "-c", "command -v sw_vers"], env={"PATH": SYSTEM, b"PATH": session.encode()})


def test_p3_1_both_path_keys_safe_stay_approved():
    session = os.environ["PATH"]
    result = subprocess.run(["/bin/sh", "-c", "true"], capture_output=True, timeout=60,
                            env={"PATH": session, b"PATH": session.encode()})
    assert result.returncode == 0


@pytest.mark.parametrize("stop", ["#", "\\c"])
def test_p3_1_unbalanced_quote_after_env_stop_is_refused(stop):
    """env stops reading at `#` or `\\c`; shlex raised on the quote after it."""
    refused(["/usr/bin/env", "-S", f'PATH=/usr/bin sw_vers {stop}"'])


# P3-2: env's options as macOS env's getopt ("-0C:iP:S:u:v") reads them.

@pytest.mark.parametrize("arguments", [
    ["-vu", "PATH", "sw_vers"], ["-vuPATH", "sw_vers"], ["-vP", "/usr/bin", "sw_vers"],
    ["-vS", "PATH=/usr/bin sw_vers"], ["-v-", "sw_vers"], ["--i", "sw_vers"],
], ids=lambda arguments: " ".join(arguments))
def test_p3_2_grouped_env_options_are_walked(arguments):
    """A -u, -P, -S or clear inside a group of short options resets the child's PATH.

    `-v-` and `--i` clear the environment: getopt reads "-" as env's clear option.
    """
    refused(["/usr/bin/env", *arguments])


@pytest.mark.parametrize("arguments", [
    ["-vSPATH=/usr/bin sw_vers"], ["-0vuPATH", "sw_vers"], ["-vC/", "PATH=usr/bin", "sw_vers"],
    ["-S", "-vS PATH=/usr/bin sw_vers"],
], ids=lambda arguments: " ".join(arguments))
def test_p3_2_attached_values_and_nested_split_strings_are_walked(arguments):
    refuses(["/usr/bin/env", *arguments], dict(os.environ))


@pytest.mark.parametrize("split", ["PATH=${SYS} sw_vers", "PATH=/usr/bin\\_sw_vers",
                                   "PATH=/usr/bin\vsw_vers", "PATH=/usr/bin\fsw_vers"],
                         ids=["substitution", "escaped-separator", "vertical-tab", "form-feed"])
def test_p3_2_env_split_string_env_would_rewrite_is_refused(split):
    """env substitutes ${SYS}, reads \\_ as a space, and splits on \\v and \\f; shlex does not."""
    refused(["/usr/bin/env", "-S", split], env={**os.environ, "SYS": "/usr/bin"})


def test_p3_2_env_alternate_path_and_child_path_are_both_judged():
    """-P finds the utility; the child still searches the PATH env passes on."""
    refused(["/usr/bin/env", "-P", recorder() + ":/bin", "/bin/sh", "-c", "sw_vers"], env={"PATH": SYSTEM})


def test_p3_2_env_chdir_is_the_base_of_relative_entries():
    """env -C / makes PATH=usr/bin mean /usr/bin."""
    refused(["/usr/bin/env", "-C", "/", "PATH=usr/bin", "sw_vers"])


def test_p3_2_options_after_an_assignment_are_the_utility():
    """getopt stops at the first operand, so a later -i is the program env runs."""
    audit(["/usr/bin/env", "A=1", "-i", "fixture-ref"], dict(os.environ))


@pytest.mark.parametrize("options", [
    ["-v"], ["-0v"], ["-vv"], ["-u", "HOME"], ["-uHOME"], ["-vuHOME"], ["-C", "/"], ["-vC/"],
    ["-i", "PATH={recorder}"], ["-", "PATH={recorder}:/usr/bin"], ["-S", "PATH={recorder} A=1"],
    ["-vS", "-u HOME"], ["--"], ["-P", "{recorder}"],
], ids=lambda options: " ".join(options))
def test_p3_2_env_options_that_keep_the_recorder_first_stay_approved(options):
    argv = ["/usr/bin/env", *(option.format(recorder=recorder()) for option in options), "fixture-ref"]
    audit(argv, dict(os.environ))


@pytest.mark.parametrize("arguments", [
    ["-x", "fixture-ref"], ["--unset=PATH", "fixture-ref"], ["--ignore-environment", "fixture-ref"],
    ["--split-string", "PATH=/usr/bin", "fixture-ref"], ["-vu"],
], ids=lambda arguments: " ".join(arguments))
def test_p3_2_env_options_env_does_not_have_are_refused(arguments):
    """env rejects an unknown option or a missing value; the check refuses rather than guess."""
    refuses(["/usr/bin/env", *arguments], dict(os.environ))


# P3-3: spellings of a system directory.

@pytest.mark.parametrize("path, program", [("//usr/bin", "sw_vers"), ("//bin", "ls")])
def test_p3_3_double_slash_system_directory_is_refused(path, program):
    """normpath keeps a double leading slash; the child resolved //usr/bin/sw_vers."""
    refused(["/bin/sh", "-c", f"command -v {program}"], env={"PATH": path})


def test_p3_3_tilde_entry_is_refused():
    """bash expands ~ in PATH: with HOME=/usr, ~/bin is /usr/bin."""
    refused(["/bin/bash", "-c", "sw_vers"], env={"HOME": "/usr", "PATH": "~/bin"})


# P3-4: the named wrappers.

@pytest.mark.parametrize("name", NEW_WRAPPERS)
def test_p3_4_named_wrappers_with_system_path_are_refused(name, tmp_path):
    """Each runs its operand through PATH; each ran /usr/bin/sw_vers when approved."""
    argv = [part.format(scratch=tmp_path) for part in NEW_WRAPPERS[name]]
    refused(argv, env={"PATH": SYSTEM})


@pytest.mark.parametrize("options", [["-c"], ["-d", "PATH"], ["-e", "PATH=/usr/bin"],
                                     ["-e", "PATH=" + "{recorder}", "-c"]])
def test_p3_4_arch_environment_options_are_judged(options):
    """arch -c, -d and -e change the environment its program runs with."""
    argv = ["/usr/bin/arch", "-arm64", *(option.format(recorder=recorder()) for option in options),
            "/bin/sh", "-c", "sw_vers"]
    refused(argv)


@pytest.mark.parametrize("name", sorted(credentials._DELEGATING_PROGRAMS))
def test_p3_4_wrappers_that_keep_the_recorder_first_stay_approved(name):
    program = "/usr/bin/" + name
    audit([program, "fixture-ref"], dict(os.environ))
    audit([program, "fixture-ref"], {"PATH": recorder() + ":" + SYSTEM})


def test_p3_4_arch_restoring_the_recorder_after_clearing_stays_approved():
    audit(["/usr/bin/arch", "-arm64", "-c", "-e", "PATH=" + recorder(), "/bin/sh", "-c", "true"],
          dict(os.environ))


def named_programs(text: str) -> set[str]:
    found = re.search(r"launch of ((?:[a-z]+, )+[a-z]+),? or ([a-z]+) whose", " ".join(text.split()))
    assert found, "the checked programs are not listed"
    return {*found.group(1).split(", "), found.group(2)}


def test_p3_4_docstring_and_contract_name_exactly_the_checked_programs():
    contract = (Path(__file__).parents[2] / "docs/acceptance-contract.md").read_text()
    clause = next(line for line in contract.splitlines() if line.startswith("- **C-20.6**"))
    docstring = credentials.install_keychain_exec_guard.__doc__
    for text in (clause, docstring):
        assert named_programs(text) == set(credentials._DELEGATING_PROGRAMS)
        flat = " ".join(text.split())
        for phrase in ("other PATH-searching programs", "find -exec", "path aliases", "login shell",
                       "inside the shell", "cannot finish"):
            assert phrase in flat, phrase


# The property: no env spelling or system-directory spelling gets past the recorder.

def letter(character):
    # APFS folds case, and the long s (U+017F) case-folds to "s".
    return st.sampled_from([character, character.upper(), *(["ſ"] if character == "s" else [])])


def detours(directory):
    """Real directories (not symlinks) inside `directory`: `<name>/..` returns to it."""
    names = ["usr", "bin", "sbin", "lib", "libexec", "share", "System", "Library"]
    return [name for name in names if os.path.isdir(os.path.join(directory, name))
            and not os.path.islink(os.path.join(directory, name))]


@st.composite
def system_spellings(draw):
    """A spelling of a real system directory: extra slashes, `.` and `..`, case."""
    directory = draw(st.sampled_from([d for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
                                      if os.path.isdir(d) and not os.path.islink(d)]))
    out, here = "/" * draw(st.integers(1, 3)), "/"
    parts = directory.strip("/").split("/")
    for level, name in enumerate(parts):
        for _ in range(draw(st.integers(0, 2))):
            options = ["dot", *(["detour"] if detours(here) else [])]
            if draw(st.sampled_from(options)) == "dot":
                out += "." + "/" * draw(st.integers(1, 2))
            else:
                out += draw(st.sampled_from(detours(here))) + "/.." + "/" * draw(st.integers(1, 2))
        out += "".join(draw(letter(character)) for character in name)
        here = os.path.join(here, name)
        if level < len(parts) - 1:
            out += "/" * draw(st.integers(1, 3))
    return out + draw(st.sampled_from(["", "/", "//", "/."]))


def harmless_options(draw) -> list[str]:
    """Options that leave PATH alone: -0 and -v flags, -u of another name, -C /."""
    out = []
    for _ in range(draw(st.integers(0, 2))):
        kind = draw(st.sampled_from(["flags", "unset", "chdir"]))
        flags = "".join(draw(st.lists(st.sampled_from("0v"), min_size=1, max_size=3)))
        if kind == "flags":
            out.append("-" + flags)
        else:
            flag, value = ("u", "HOME") if kind == "unset" else ("C", "/")
            out += ([f"-{flags}{flag}{value}"] if draw(st.booleans()) else [f"-{flags}{flag}", value])
    return out


@st.composite
def env_reaching_system(draw):
    """env arguments that make the utility or its child search `spelling` first."""
    spelling = draw(system_spellings())
    flags = "".join(draw(st.lists(st.sampled_from("0v"), max_size=2)))
    space = draw(st.sampled_from(list(" \t\n\v\f\r")))
    attached = draw(st.booleans())

    def option(flag, value):
        return [f"-{flags}{flag}{value}"] if attached else [f"-{flags}{flag}", value]

    kind = draw(st.sampled_from(["clear", "dash", "unset", "altpath", "split", "split-unset", "assign"]))
    if kind == "clear":
        reset = [f"-{flags}i" + "".join(draw(st.lists(st.sampled_from("0v"), max_size=2)))]
    elif kind == "dash":
        reset = [f"-{flags}-"] if flags else ["-"]
    elif kind == "unset":
        reset = option("u", "PATH")
    elif kind == "altpath":
        reset = option("P", f"{spelling}:{recorder()}")
    elif kind == "split":
        reset = option("S", f"PATH={spelling}{space}A=1")
    elif kind == "split-unset":
        reset = option("S", f"-u{space}PATH")
    else:
        reset = [f"PATH={spelling}:{recorder()}"]
    before = harmless_options(draw)
    return ["/usr/bin/env", *before, *reset, "sw_vers"]


@PROPERTIES
@given(argv=env_reaching_system())
def test_property_env_spellings_that_search_a_system_directory_first_are_refused(argv):
    refuses(argv, dict(os.environ))


@PROPERTIES
@given(name=st.sampled_from(sorted(credentials._DELEGATING_PROGRAMS)), spelling=system_spellings(),
       absent=st.booleans())
def test_property_wrapper_paths_that_reach_a_system_directory_first_are_refused(name, spelling, absent):
    env = {} if absent else {"PATH": f"{spelling}:{recorder()}"}
    refuses(["/usr/bin/" + name, "fixture-ref"], env)


@PROPERTIES
@given(argv=st.composite(lambda draw: ["/usr/bin/env", *harmless_options(draw), "fixture-ref"])(),
       spelling=system_spellings())
def test_property_env_spellings_that_keep_the_recorder_first_stay_approved(argv, spelling):
    audit(argv, dict(os.environ))
    audit([*argv[:-1], f"PATH={recorder()}:{spelling}", argv[-1]], dict(os.environ))
