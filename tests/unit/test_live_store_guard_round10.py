"""D-ST1 r10 (#226 review r1): direct launches fail closed, and a wrapper is judged by what it runs.

Executed probes launch for real and expect the guard to refuse before anything
starts. A guard that approves runs only `sw_vers` or a registered recording
stand-in, and the failure prints what the child printed: escapes are shown with
`sw_vers` or the stand-in, never with the reader. Synthetic audit events start
no program. The reader is named only as a string.

Tests take only plain arguments (a scratch directory, a case name), so the
round-10 mutation check can call them outside pytest.
"""
from __future__ import annotations

import contextlib
import functools
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import credentials, guardian, retention_qos
from tests import keychain_standin
from tests.keychain_standin import SAFETY_HITS, StandIn

PROPERTIES = settings(max_examples=int(os.environ.get("SUBFLEET_EXEC_GUARD_EXAMPLES", "150")),
                      deadline=None, database=None, derandomize=True,
                      suppress_health_check=[HealthCheck.function_scoped_fixture,
                                             HealthCheck.filter_too_much, HealthCheck.too_slow])
SYSTEM = "/usr/bin:/bin"
READER = "/usr/bin/security"
QUERY = ["find-generic-password", "-s", "probe-ref", "-w"]
ORDERS = ["text-first", "bytes-first"]


def recorder() -> str:
    """The session's recording `security` directory, first on the session PATH."""
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert Path(first, "security").is_file() and first.endswith("keychain-bin"), first
    return first


def both_keys(order: str, value: str) -> dict:
    """A text and a bytes PATH key with one value, in either order."""
    keys = [("PATH", value), (b"PATH", value.encode())]
    return dict(keys if order == "text-first" else keys[::-1])


@contextlib.contextmanager
def registered(scratch: Path):
    """A recording stand-in that the production guard refuses as it would the reader."""
    found = StandIn(Path(scratch) / "guarded")
    before = credentials._STANDIN_KEYCHAIN_BINARIES
    credentials._STANDIN_KEYCHAIN_BINARIES = (str(found.path),)
    try:
        yield found
    finally:
        credentials._STANDIN_KEYCHAIN_BINARIES = before


def refused(argv, *, input=None, **kwargs) -> None:
    """Launch `argv`; the guard must refuse it with the reader's code-7 refusal."""
    hits = len(SAFETY_HITS)
    stdin = {} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, input=input,
                                **stdin, **kwargs)
    except credentials.KeychainExecRefused as error:
        assert (error.code, error.reason) == (7, "keychain-test-guard")
        assert SAFETY_HITS[hits:] == []
        return
    pytest.fail(f"approved {argv!r}; the child printed {(result.stdout + result.stderr).strip()[:160]!r}")


def approved(argv, **kwargs) -> None:
    """Launch `argv`; the guard must let it run, and it must exit 0."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                            **kwargs)
    assert result.returncode == 0, (argv, result.stdout + result.stderr)


def audit(argv, env=None, cwd=None, program=None) -> None:
    """A synthetic Popen audit event: the guard judges it, and nothing starts."""
    sys.audit("subprocess.Popen", program or argv[0], argv, cwd, env)


def refuses(argv, env=None, cwd=None, program=None) -> None:
    hits = len(SAFETY_HITS)
    with pytest.raises(credentials.KeychainExecRefused, match="refusing personal keychain") as caught:
        audit(argv, env, cwd, program)
    assert (caught.value.code, caught.value.reason) == (7, "keychain-test-guard")
    assert SAFETY_HITS[hits:] == []


# P2: a direct launch searches PATH only for a bare name, and fails closed.

@pytest.mark.parametrize("order", ORDERS)
def test_p2_standin_by_path_with_text_and_bytes_path_keys_is_refused(tmp_path, order):
    """get_exec_path raised on both keys for a path Popen never searches, and the
    guard approved: the registered stand-in ran."""
    with registered(tmp_path) as found:
        refused([str(found.path), *QUERY], env=both_keys(order, os.environ["PATH"]))
        assert found.count() == 0


@pytest.mark.parametrize("order", ORDERS)
def test_p2_synthetic_reader_event_with_both_path_keys_is_refused(order):
    """Neither the guard nor the safety net raised for this event."""
    refuses([READER, *QUERY], both_keys(order, os.environ["PATH"]))


@pytest.mark.parametrize("order", ORDERS)
def test_p2_safety_net_judges_a_reader_path_with_both_path_keys(order):
    """The safety net's own check must name the reader, independently of the guard."""
    details = (READER, [READER, *QUERY], None, both_keys(order, os.environ["PATH"]))
    assert keychain_standin.refusal("subprocess.Popen", details) == READER


def test_p2_path_object_path_value_gives_the_code_7_refusal(tmp_path):
    """The hook raised AttributeError, which stopped the launch only by accident."""
    with registered(tmp_path) as found:
        refused([str(found.path), *QUERY], env={"PATH": found.path.parent})
        assert found.count() == 0
    refuses([READER, *QUERY], {"PATH": Path(recorder())})


def test_p2_a_bare_name_whose_search_cannot_finish_is_refused(tmp_path):
    """Popen raises too, unless a later hook changes env first: refuse, not approve."""
    with registered(tmp_path) as found:
        (found.path.parent / "sf-r10-reader").symlink_to(found.path)
        directory = str(found.path.parent)
        for env in (both_keys("text-first", directory), both_keys("bytes-first", directory),
                    {"PATH": Path(directory)}, {b"PATH": Path(directory)}, {"PATH": 7}):
            refuses(["sf-r10-reader", *QUERY], env)
        assert found.count() == 0


def test_p2_a_later_hook_cannot_finish_an_unfinished_search_for_the_reader(tmp_path):
    """A later audit hook drops the bytes key, so Popen's own search would succeed."""
    with registered(tmp_path) as found:
        (found.path.parent / "sf-r10-reader").symlink_to(found.path)
        env = both_keys("text-first", str(found.path.parent))
        active = [True]

        def drop_bytes_key(event, details):
            if active[0] and event == "subprocess.Popen" and details[3] is env:
                env.pop(b"PATH", None)

        sys.addaudithook(drop_bytes_key)
        try:
            refused(["sf-r10-reader", *QUERY], env=env)
        finally:
            active[0] = False
        assert found.count() == 0


@pytest.mark.parametrize("order", ORDERS)
def test_p2_a_program_by_path_with_both_path_keys_stays_approved(order):
    """Popen never searches for a path, so two PATH keys are no reason to refuse it."""
    approved(["/usr/bin/true"], env=both_keys(order, os.environ["PATH"]))
    assert keychain_standin.refusal("subprocess.Popen", ("/usr/bin/true", ["/usr/bin/true"], None,
                                                         both_keys(order, os.environ["PATH"]))) is None


def test_p2_safety_net_refuses_a_check_that_cannot_finish():
    """A bare name with both keys, or a PATH that is not text: the net cannot finish, so it refuses."""
    for env in (both_keys("text-first", recorder()), {"PATH": Path(recorder())}, {"PATH": 7}):
        found = keychain_standin.refusal("subprocess.Popen", ("sf-r10-reader", ["sf-r10-reader"], None, env))
        assert found is not None and "check cannot finish" in found, (env, found)


def test_p2_an_inactive_guard_judges_nothing():
    """With no live session the guard returns before any check. At 7916d50d2 it
    computed targets first, and a path-object PATH raised AttributeError out of
    an inactive guard."""
    guard = credentials._exec_guard([])
    for env in ({"PATH": Path("/usr/bin")}, both_keys("text-first", SYSTEM), {"PATH": 7}):
        for argv in ([READER, *QUERY], ["sf-r10-reader"], ["/usr/bin/nice", "env", "-i", "sw_vers"]):
            assert guard("subprocess.Popen", (argv[0], argv, None, env), _active=lambda pins: False) is None


# P3: a listed wrapper is judged by the command it runs.

# The review's nested inputs, each run with the session PATH (recorder first).
NESTED = {
    "nice env -i": ["nice", "env", "-i", "sw_vers"],
    "env /usr/bin/env -i": ["env", "/usr/bin/env", "-i", "sw_vers"],
    "nohup env -u PATH": ["nohup", "env", "-u", "PATH", "sw_vers"],
    "taskpolicy -c utility env -i": [guardian.TASKPOLICY, "-c", "utility", "env", "-i", "sw_vers"],
    "time env -i": ["/usr/bin/time", "env", "-i", "sw_vers"],
    "caffeinate env -i": ["caffeinate", "env", "-i", "sw_vers"],
    "arch -arm64 env -i": ["arch", "-arm64", "env", "-i", "sw_vers"],
    "nice env PATH=/usr/bin": ["nice", "env", "PATH=/usr/bin", "sw_vers"],
}
# The review's command-word wrappers, plus stdbuf, lockf and script, before the stand-in's path.
COMMAND_WORD = {
    "nice": ["nice"], "env": ["env"], "nohup": ["nohup"],
    "taskpolicy -c utility": [guardian.TASKPOLICY, "-c", "utility"],
    "time": ["/usr/bin/time"], "caffeinate": ["caffeinate"], "arch -arm64": ["arch", "-arm64"],
    "stdbuf -o0": ["stdbuf", "-o0"], "lockf": ["lockf", "{scratch}/lock"],
    "script": ["script", "-q", "/dev/null"],
}


@pytest.mark.parametrize("name", NESTED)
def test_p3_nested_wrappers_are_judged_with_the_environment_the_outer_leaves(name):
    """Each ran /usr/bin/sw_vers: the outer wrapper kept the recorder, the inner one reset PATH."""
    refused(NESTED[name])


@pytest.mark.parametrize("name", COMMAND_WORD)
def test_p3_a_wrapper_running_the_reader_by_path_is_refused(name, tmp_path):
    """Each ran the registered stand-in, which a direct launch would have refused."""
    with registered(tmp_path) as found:
        wrapper = [part.format(scratch=tmp_path) for part in COMMAND_WORD[name]]
        refused([*wrapper, str(found.path), *QUERY])
        assert found.count() == 0


def test_p3_xargs_running_the_reader_by_path_is_refused(tmp_path):
    with registered(tmp_path) as found:
        refused(["xargs", "-0", str(found.path), *QUERY], input="probe")
        assert found.count() == 0


def test_p3_a_relative_command_word_is_judged_from_the_working_directory(tmp_path):
    with registered(tmp_path) as found:
        refused(["/usr/bin/nice", "./security", *QUERY], cwd=found.path.parent)
        refused(["/usr/bin/env", "-C", str(found.path.parent), "/usr/bin/nice", "./security"])
        assert found.count() == 0


def test_p3_script_without_a_command_is_judged_by_its_shell(tmp_path):
    """script runs `$SHELL -i`, by path, when it is given no command."""
    with registered(tmp_path) as found:
        refused(["script", "-q", "/dev/null"], env={**os.environ, "SHELL": str(found.path)})
        refused(["script", "-q", "/dev/null"], env={**os.environ, "SHELL": "./security"},
                cwd=found.path.parent)
        assert found.count() == 0


def test_p3_script_shell_set_by_an_outer_wrapper_is_judged(tmp_path):
    with registered(tmp_path) as found:
        refused(["/usr/bin/env", f"SHELL={found.path}", "/usr/bin/script", "-q", "/dev/null"])
        refused(["/usr/bin/arch", "-e", f"SHELL={found.path}", "/usr/bin/script", "-q", "/dev/null"])
        assert found.count() == 0


def test_p3_stdbuf_joins_the_checked_wrappers():
    """stdbuf runs its command through PATH, as nice does: it ran /usr/bin/sw_vers."""
    refused(["/usr/bin/stdbuf", "-o0", "sw_vers"], env={"PATH": SYSTEM})


def test_p3_nesting_is_judged_to_a_fixed_depth():
    """A chain of listed wrappers deeper than the guard follows is refused."""
    chain = ["/usr/bin/nice"] * credentials._WRAPPER_DEPTH
    audit([*chain, "/usr/bin/true"], dict(os.environ))
    refuses([*chain[:-1], "/usr/bin/env", "-i", "sw_vers"], dict(os.environ))
    refuses([*chain, "/usr/bin/nice", "/usr/bin/true"], dict(os.environ))


@pytest.mark.parametrize("argv", [
    ["/usr/bin/nice", "-x", "/usr/bin/true"], ["/usr/sbin/taskpolicy", "-c"],
    ["/usr/bin/lockf", "-t"], ["/usr/bin/nohup", "-n", "1", "/usr/bin/true"],
], ids=" ".join)
def test_p3_a_wrapper_option_its_getopt_rejects_is_refused(argv):
    """The wrapper would not run; the check refuses rather than guess at its command."""
    refuses(argv, dict(os.environ))


# No over-refusal: production's taskpolicy argv, and ordinary wrapped commands.

def test_p3_production_taskpolicy_argv_stays_approved(monkeypatch):
    """retention_qos and the guardian wrap a path or a bare name in `taskpolicy -c <clamp>`."""
    commands = [["/usr/bin/true"], ["true"], ["/bin/sh", "-c", "true"], ["git", "--version"],
                ["/usr/bin/env", "A=1", "/usr/bin/true"]]
    for clamp in retention_qos.CLAMPS:
        monkeypatch.setenv(retention_qos.QOS_ENV, clamp)
        for command in commands:
            argv = retention_qos.argv(command)
            assert argv[:3] == [guardian.TASKPOLICY, "-c", clamp], argv
            audit(argv, None)
            audit(argv, dict(os.environ))
    approved(retention_qos.argv(["/usr/bin/true"]))
    approved(guardian.provider_argv(["/usr/bin/true"], guardian.PROVIDER_QOS))
    approved(guardian.provider_argv(["true"], guardian.PROVIDER_QOS))


ORDINARY = [
    ["nice", "-n", "5", "/usr/bin/true"], ["nice", "-5", "/usr/bin/true"], ["nice", "--", "true"],
    ["nohup", "--", "/usr/bin/true"], ["/usr/bin/time", "-p", "/usr/bin/true"],
    ["caffeinate", "-i", "/usr/bin/true"], ["arch", "-arm64", "/usr/bin/true"],
    ["stdbuf", "-o0", "/usr/bin/true"], ["lockf", "-k", "{scratch}/lock", "/usr/bin/true"],
    ["script", "-q", "/dev/null", "/usr/bin/true"], ["script", "-q", "/dev/null"],
    ["nice", "env", "PATH={recorder}:/usr/bin", "/bin/sh", "-c", "true"],
    ["env", "/usr/bin/nice", "/usr/bin/true"], ["/usr/bin/nice", "/bin/sh", "-c", "true"],
    [guardian.TASKPOLICY, "-c", "utility", "/usr/bin/nice", "-n", "1", "/usr/bin/true"],
]


@pytest.mark.parametrize("index", range(len(ORDINARY)), ids=lambda index: " ".join(ORDINARY[index]))
def test_p3_wrappers_running_ordinary_commands_stay_approved(index, tmp_path):
    argv = [part.format(scratch=tmp_path, recorder=recorder()) for part in ORDINARY[index]]
    # These cases run the wrapper. macOS 26 ships /usr/bin/stdbuf; CI's macOS 15 runners have
    # none, and without it there is nothing to run.
    if os.sep not in argv[0] and shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} is not installed on this host")
    approved(argv, env={**os.environ, "SHELL": "/bin/sh"})


def test_p3_xargs_running_an_ordinary_command_stays_approved():
    result = subprocess.run(["xargs", "-0", "/usr/bin/true"], input="probe", capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# The contract and the docstring state the rule.

def test_contract_and_docstring_state_the_command_word_and_nesting_rule():
    contract = (Path(__file__).parents[2] / "docs/acceptance-contract.md").read_text()
    clause = " ".join(next(line for line in contract.splitlines() if line.startswith("- **C-20.6**")).split())
    docstring = " ".join(credentials.install_keychain_exec_guard.__doc__.split())
    for text in (clause, docstring):
        for phrase in ("only for a bare name", "the command it runs", "registered stand-in",
                       "the outer one leaves", f"{credentials._WRAPPER_DEPTH} listed wrappers",
                       "$SHELL -i", "xargs reads from its input", "find -exec"):
            assert phrase in text, phrase
    assert "nothing in this repository launches" in clause


def out_of_scope(text: str, marker: str) -> list[str]:
    """The items of the list after `marker` and its colon: split at semicolons
    outside parentheses, up to the full stop that ends the sentence."""
    flat = " ".join(text.split())
    start = flat.index(marker)
    rest = flat[flat.index(": ", start) + 2:]
    items, item, depth = [], "", 0
    for index, character in enumerate(rest):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0 and (character == ";" or character == "." and rest[index + 1:index + 2] in ("", " ")):
            items.append(item.strip())
            item = ""
            if character == ".":
                break
            continue
        item += character
    items[-1] = items[-1].removeprefix("and ")
    return items


def test_r2_p3_docstring_and_contract_list_the_same_out_of_scope_items():
    # #226 review r2's P3: the docstring missed the shell's script operand, and
    # put every symlink to the reader out of scope, which the guard refuses but
    # for a wrapper's bare command found through PATH.
    contract = (Path(__file__).parents[2] / "docs/acceptance-contract.md").read_text()
    clause = next(line for line in contract.splitlines() if line.startswith("- **C-20.6**"))
    documented = out_of_scope(clause, "Out of scope, requiring isolated inputs")
    docstring = out_of_scope(credentials.install_keychain_exec_guard.__doc__, "Outside C-20.6 (D-ST1)")
    assert len(documented) > 2 and all(documented), documented
    assert docstring == documented
    for items in (documented, docstring):
        assert "a shell's script operand" in items
        aliases = next(item for item in items if item.startswith("path aliases"))
        assert "a symlink to the reader that a wrapper's bare command finds through PATH" in aliases, aliases


# The property: env shapes over direct and wrapped launches.

GETOPT = {  # Each wrapper's option string, as its macOS binary holds it.
    "xargs": "0E:I:J:L:n:oP:pR:S:s:rtx", "nice": "n:", "nohup": "",
    "taskpolicy": "xXbBd:g:c:t:l:p:asS:m:j:P:", "time": "ahlo:p", "script": "adeFkpqrT:t:",
    "caffeinate": "mdhisut:w:", "lockf": "knst:w", "stdbuf": "e:i:o:",
}
FILES = {"script": ["/dev/null"], "lockf": ["/dev/null"]}
KEYS = ["PATH", b"PATH", Path("PATH")]
ALIAS = "sf-r10-reader"


@pytest.fixture(scope="module")
def property_standin(tmp_path_factory):
    """One stand-in for every example, with a bare alias beside it."""
    found = StandIn(tmp_path_factory.mktemp("round10-property") / "guarded")
    (found.path.parent / ALIAS).symlink_to(found.path)
    return found


@st.composite
def getopt_options(draw, name):
    options = GETOPT[name]
    flags = [c for i, c in enumerate(options) if c != ":" and options[i + 1:i + 2] != ":"]
    valued = [c for i, c in enumerate(options) if c != ":" and options[i + 1:i + 2] == ":"]
    out = []
    if name == "nice" and draw(st.booleans()):
        out.append(draw(st.sampled_from(["-5", "--5", "-10"])))  # nice's obsolete -<number>
    for _ in range(draw(st.integers(0, 2))):
        kinds = [kind for kind, pool in (("flags", flags), ("value", valued)) if pool]
        if not kinds:
            break
        prefix = "".join(draw(st.lists(st.sampled_from(flags), max_size=2))) if flags else ""
        if draw(st.sampled_from(kinds)) == "flags":
            out.append("-" + prefix + draw(st.sampled_from(flags)))
        else:
            flag, value = draw(st.sampled_from(valued)), draw(st.sampled_from(["1", "utility", "/dev/null"]))
            out += [f"-{prefix}{flag}{value}"] if draw(st.booleans()) else [f"-{prefix}{flag}", value]
    if draw(st.booleans()):
        out.append("--")
    return [*out, *FILES.get(name, [])]


@st.composite
def wrapper_prefixes(draw):
    """A listed wrapper and options that keep its child's PATH: (argv prefix)."""
    name = draw(st.sampled_from(sorted([*GETOPT, "env", "arch"])))
    path = guardian.TASKPOLICY if name == "taskpolicy" else "/usr/bin/" + name
    if name == "env":
        options = draw(st.lists(st.sampled_from(["-v", "-0", "-uHOME", "A=1", "-C/"]), max_size=3))
        options.sort(key=lambda option: "=" in option)  # Assignments follow the options.
    elif name == "arch":
        options = draw(st.lists(st.sampled_from([["-arm64"], ["-arch", "arm64"], ["-e", "A=1"],
                                                 ["-d", "HOME"]]), max_size=2))
        options = [part for option in options for part in option]
    else:
        options = draw(getopt_options(name))
    return [path, *options]


def path_values():
    """A PATH value's (kind, form): text, bytes or a path object, or not text at all."""
    spelled = st.tuples(st.sampled_from(["session", "system", "alias"]),
                        st.sampled_from(["text", "bytes", "path"]))
    return spelled | st.just(("int", "int"))


def materialize(kind_form, alias_directory):
    """(kind, value): the session PATH, /usr/bin:/bin, or only the alias's directory."""
    kind, form = kind_form
    if kind == "int":
        return kind, 7
    text = {"session": os.environ["PATH"], "system": SYSTEM, "alias": alias_directory}[kind]
    return kind, {"text": text, "bytes": text.encode(), "path": Path(text)}[form]


@st.composite
def launches(draw, standin):
    alias_directory = str(standin.path.parent)
    entries = draw(st.lists(st.tuples(st.sampled_from(range(len(KEYS))), path_values()),
                            unique_by=lambda entry: entry[0], max_size=3))
    env = None if draw(st.booleans()) and not entries else {
        KEYS[key]: materialize(value, alias_directory)[1] for key, value in entries}
    kinds = [materialize(value, alias_directory)[0] for _key, value in entries]
    shape = draw(st.sampled_from(["direct", "wrapped"]))
    if shape == "direct":
        program = draw(st.sampled_from(["reader", "standin", "relative", "alias", "bare-security",
                                        "control", "bare-control"]))
        cwd = None
        if program == "relative":
            cwd = draw(st.sampled_from([alias_directory, alias_directory.encode(), Path(alias_directory)]))
        name = {"reader": READER, "standin": str(standin.path), "relative": "./security", "alias": ALIAS,
                "bare-security": "security", "control": "/usr/bin/true", "bare-control": "true"}[program]
        event = draw(st.sampled_from(["subprocess.Popen", "os.exec"])) if os.path.dirname(name) else \
            "subprocess.Popen"
        if event == "os.exec":
            details = (name, [name, *QUERY], env)
        else:
            details = (name, [name, *QUERY], cwd, env)
        return event, details, ("direct", program, kinds)
    prefix = draw(wrapper_prefixes())
    command = draw(st.sampled_from(["reader", "nested-reset", "nested-reader", "control"]))
    words = {"reader": [str(standin.path), *QUERY],
             "nested-reset": draw(st.sampled_from([["/usr/bin/env", "-i", "sw_vers"], ["env", "-u", "PATH", "sw_vers"],
                                                   ["/usr/bin/nice", "env", "-i", "sw_vers"],
                                                   ["arch", "-c", "sw_vers"], ["env", "PATH=/usr/bin", "sw_vers"]])),
             "nested-reader": ["/usr/bin/nice", str(standin.path)],
             "control": draw(st.sampled_from([["/usr/bin/true"], ["true"], ["/bin/sh", "-c", "true"],
                                              ["/usr/bin/env", "A=1", "/usr/bin/true"]]))}[command]
    argv = [*prefix, *words]
    return "subprocess.Popen", (argv[0], argv, None, env), ("wrapped", command, kinds)


def expected_refusal(event, details, shape, standin) -> bool:
    """The documented rule, computed independently of the guard."""
    kind, case, kinds = shape
    env = details[-1]
    if kind == "wrapped":
        # Every generated key is a PATH key. No key means the wrapper's default,
        # /usr/bin:/bin; no env means the session PATH, recorder first.
        if "int" in kinds:
            return True  # A PATH that is not text: the check cannot finish.
        system_first = env is not None and (not env or "system" in kinds)
        return system_first or case != "control"
    name = details[0]
    if case == "bare-security":
        return True
    if os.path.dirname(name):
        base = os.fsdecode(details[2]) if event == "subprocess.Popen" and details[2] is not None else ""
        target = os.path.realpath(os.path.join(base, name))
        return target in (READER, os.path.realpath(standin.path))
    try:
        search = os.get_exec_path(env)
    except Exception:
        return True  # Popen's own search raises; a check that cannot finish refuses.
    found = shutil.which(name, path=os.pathsep.join(search))
    return found is not None and os.path.realpath(found) == os.path.realpath(standin.path)


def tracked(helper, raised):
    @functools.wraps(helper)
    def call(*args, **kwargs):
        try:
            return helper(*args, **kwargs)
        except BaseException as error:
            raised.append((helper.__name__, error))
            raise
    return call


@PROPERTIES
@given(data=st.data())
def test_property_no_launch_that_could_reach_the_reader_and_no_unfinished_check_is_approved(
        property_standin, data):
    event, details, shape = data.draw(launches(property_standin))
    expected = expected_refusal(event, details, shape, property_standin)
    before = credentials._STANDIN_KEYCHAIN_BINARIES
    credentials._STANDIN_KEYCHAIN_BINARIES = (str(property_standin.path),)
    hits = len(SAFETY_HITS)
    try:
        # The whole installed stack: the production guard, then the safety net.
        try:
            sys.audit(event, *details)
            outcome = None
        except credentials.KeychainExecRefused as error:
            outcome = (error.code, error.reason)
        assert SAFETY_HITS[hits:] == [], "the safety net caught what the guard approved"
        assert outcome == ((7, "keychain-test-guard") if expected else None), (shape, details)
        # A fresh guard with every helper instrumented: no check that raises approves.
        raised = []
        unsafe = functools.partial(
            credentials._unsafe_delegation, _paths=tracked(credentials._launch_paths, raised),
            _searches=tracked(credentials._delegated_searches, raised),
            _first=tracked(credentials._searches_system_first, raised),
            _command=tracked(credentials._wrapper_command, raised),
            _matches=tracked(credentials._is_keychain_binary, raised))
        guard = credentials._exec_guard([], recorder_dirs=(recorder(),))
        try:
            guard(event, details, _targets=tracked(credentials._launch_targets, raised), _unsafe=unsafe,
                  _matches=tracked(credentials._is_keychain_binary, raised))
            instrumented = None
        except credentials.KeychainExecRefused as error:
            instrumented = (error.code, error.reason)
        if raised:
            assert instrumented == (7, "keychain-test-guard"), (raised, details)
        assert instrumented == outcome, (raised, details)
    finally:
        credentials._STANDIN_KEYCHAIN_BINARIES = before
    assert property_standin.count() == 0
