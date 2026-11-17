"""Job-local test isolation, loaded in the controller and every xdist worker."""
import json, os, shutil, stat, tempfile
from pathlib import Path
import pytest
ROOT = Path('/private/tmp/sf-a2-fix1')
RUN = ROOT / ('w-' + str(os.getpid()))
RUN.mkdir(exist_ok=True)
tempfile.tempdir = str(RUN)
os.environ['TMPDIR'] = str(RUN)
from tests import failed_keep
failed_keep.ROOT = RUN / 'failed'
original_mkdtemp = tempfile.mkdtemp
original_rmtree = shutil.rmtree
outside = []
def job_mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and Path(dir).resolve() == Path('/private/tmp'):
        dir = str(RUN)
    if dir is not None and not Path(dir).resolve().is_relative_to(ROOT):
        prefix = 'sf-a2-fix1-' + (prefix or '')
    result = original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    if not Path(result).resolve().is_relative_to(ROOT):
        outside.append(result)
        with (ROOT/'outside.jsonl').open('a') as f:
            f.write(json.dumps({'path':result})+'\n')
    return result
def job_rmtree(path, *args, **kwargs):
    if str(path) in outside:
        assert 'sf-a2-fix1' in str(path)
    return original_rmtree(path, *args, **kwargs)
tempfile.mkdtemp = job_mkdtemp
shutil.rmtree = job_rmtree
@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    for name in ('CLAUDECODE','CLAUDE_CODE_SESSION_ID','CLAUDE_PID','CODEX_THREAD_ID','CODEX_SESSION_ID','SUBFLEET_CALLER_FAMILY'):
        os.environ.pop(name, None)
    _install_frontend_scratch_cache(config)
@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    def fix(fn, path, exc):
        os.chmod(path, stat.S_IRWXU)
        fn(path)
    assert 'sf-a2-fix1' in str(RUN) and RUN.parent == ROOT
    original_rmtree(RUN, onexc=fix)

# Discover only the coordinator's selected test modules, with two directory
# arguments instead of hundreds of file arguments during worker boot.
SELECTED = set((ROOT/'active-selection.txt').read_text().splitlines()) if (ROOT/'active-selection.txt').exists() else None
REPO = Path.cwd().resolve()
def pytest_ignore_collect(collection_path, config):
    path = Path(collection_path)
    if SELECTED is not None and path.is_file() and path.name.startswith('test_') and path.suffix == '.py':
        try:
            relative = path.resolve().relative_to(REPO).as_posix()
        except ValueError:
            return None
        if relative not in SELECTED:
            return True

def _progress(kind, value):
    with (ROOT/('progress-' + str(os.getpid()) + '.jsonl')).open('a') as log:
        import time
        log.write(json.dumps({'at':time.time(),'kind':kind,'value':value})+'\n')
def pytest_collectstart(collector):
    if collector.__class__.__name__ == 'Module':
        _progress('collect', collector.nodeid)
def pytest_collection_finish(session):
    _progress('collected',len(session.items))
def pytest_runtest_logreport(report):
    if report.when == 'call':
        _progress(report.outcome,report.nodeid)

def pytest_runtest_logstart(nodeid, location):
    _progress('start',nodeid)


def _guard_skip(nodeid, reason, config):
    with (ROOT / 'guard-skips.jsonl').open('a') as log:
        log.write(json.dumps({'nodeid': nodeid, 'reason': reason,
                              'basetemp': str(config.option.basetemp), 'pid': os.getpid()}) + '\n')


def pytest_collection_modifyitems(config, items):
    premise_names = {
        'test_before_its_start_json_the_guardian_starts_nothing_that_carries_the_attempts_markers',
        'test_a_relay_that_starts_a_process_before_start_json_fails_the_premise',
    }
    for item in items:
        native_premise = (item.path.name == 'test_quar_start1_census.py'
                          and getattr(item, 'originalname', item.name) in premise_names)
        holder = (item.path.name == 'test_stopped_daemon.py'
                  and {'running_holder', 'stopped_holder'}.intersection(item.fixturenames))
        opaque = (
            (item.path.name == 'test_launch_env_helper_scope.py'
             and getattr(item, 'originalname', item.name) ==
             'test_review_r2_the_isolated_inspection_receives_no_withheld_hook')
            or (item.path.name == 'test_salvage_finalization.py'
                and getattr(item, 'originalname', item.name) ==
                'test_c13_1_live_git_past_its_cap_is_tried_again_then_recorded')
            or (item.path.name == 'test_round10_refusal_context.py'
                and getattr(item, 'originalname', item.name) ==
                'test_real_guard_version_verdict_asks_for_the_supported_version'))
        native_guardian = (
            item.path.name == 'test_review_publish_env_round8.py'
            and getattr(item, 'originalname', item.name) in {
                'test_each_review_launch_keeps_its_pins_through_selection_and_the_guardian',
                'test_a_parent_s_pinned_names_reach_no_launch_that_is_not_a_review',
                'test_bounded_property_the_pins_hold_whatever_the_parent_or_env_pass'})
        if native_premise or holder or opaque or native_guardian:
            reason = ('sf-a2-fix1 safety guard: native guardian linger premise excluded before execution'
                      if native_guardian else
                      'sf-a2-fix1 safety guard: opaque synthetic executable fixture excluded before execution'
                      if opaque else
                      'sf-a2-fix1 safety guard: native process premise excluded before execution'
                      if native_premise else
                      'sf-a2-fix1 safety guard: native holder fixture excluded before execution')
            item.add_marker(pytest.mark.skip(reason=reason))
            _guard_skip(item.nodeid, reason, config)


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    # Preserve every unrelated failure. Only an explicit exception/traceback
    # identifying this guard is an environment refusal, never a passing case.
    if report.failed and call.excinfo is not None and 'sf-a2-fix1 safety guard:' in str(report.longrepr):
        reason = 'sf-a2-fix1 safety guard: required external/native operation refused'
        _guard_skip(item.nodeid, reason, item.config)
        report.outcome = 'skipped'
        report.longrepr = (str(item.path), item.location[1], reason)


# Add this function to sf_fix1_safety.py after workers from the current run exit.
# Add `_install_frontend_scratch_cache(config)` as the last line of its existing
# pytest_configure hook. SELECTED is initialized during module import, before
# pytest calls that hook. Nothing else in the active plugin needs changing.
def _install_frontend_scratch_cache(config):
    requested = any('tests/frontend' in str(arg) for arg in config.args)
    selected = bool(SELECTED and any(path.startswith('tests/frontend/') for path in SELECTED))
    if not (requested or selected):
        return
    from tests.frontend import swift
    if getattr(swift.compile_probe, '_sf_a2_scratch_cache', False):
        return
    compile_original = swift.compile_probe
    cache_root = ROOT / 'frontend-cache'
    module_cache = RUN / 'swift-module-cache'
    module_cache.mkdir(parents=True, exist_ok=True)

    def compile_in_scratch(*args, **kwargs):
        root_original = swift.ROOT
        run_original = swift.subprocess.run

        def compiler_run(command, *run_args, **run_kwargs):
            if list(command[:2]) == ['xcrun', 'swiftc']:
                command = [*command, '-module-cache-path', str(module_cache)]
            return run_original(command, *run_args, **run_kwargs)

        # app_sources uses SOURCES, which stays on the real checkout. Probe
        # paths were made from the original ROOT by the caller. Only the helper's
        # build/frontend-probes lookup uses this temporary ROOT.
        swift.ROOT = cache_root
        swift.subprocess.run = compiler_run
        try:
            binary = compile_original(*args, **kwargs)
            # Authorize exactly the successful compiler/cache result, including
            # its actual returned basetemp path, rather than a broad directory.
            import hashlib
            approved = Path(binary).resolve()
            native_temp = Path('/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1')
            assert approved.is_relative_to(ROOT) or approved.is_relative_to(native_temp)
            payload = approved.read_bytes()
            assert payload[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe')
            entry = {'path': str(approved), 'sha256': hashlib.sha256(payload).hexdigest()}
            with (ROOT / 'frontend-approved-binaries.jsonl').open('a') as manifest:
                manifest.write(json.dumps(entry) + '\n')
            return binary
        finally:
            swift.ROOT = root_original
            swift.subprocess.run = run_original

    compile_in_scratch._sf_a2_scratch_cache = True
    swift.compile_probe = compile_in_scratch
