"""Independent review r5 checks, using only task-owned fixture trees."""
import errno
import os
from pathlib import Path
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import retention_fs as rfs
from tests.unit import test_retention_descriptor_prepared as prepared
from tests.unit.test_retention_descriptor_ownership import _failure, _IN_FLIGHT, _CLOSE_OUTCOMES
from tests.unit.test_retention_walk_descriptors import unchanged_descriptors


@pytest.mark.parametrize('phase', ['construction', 'entry'])
@pytest.mark.parametrize('kind', ['memory', 'keyboard'])
def test_direct_owner_setup_failure(tmp_path, monkeypatch, phase, kind):
    path = tmp_path / 'f'
    path.write_bytes(b'scratch')
    injected = MemoryError('independent setup') if kind == 'memory' else KeyboardInterrupt('independent setup')
    opened = []
    original_open = os.open
    def open_(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def fail(*args, **kwargs):
        raise injected
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'open', open_)
            patch.setattr(rfs._cleanup, '__init__' if phase == 'construction' else '__enter__', fail)
            with pytest.raises(type(injected)) as caught:
                rfs.read_regular(path)
        assert caught.value is injected
        assert opened == []
    finally:
        for fd in opened:
            os.close(fd)

@settings(max_examples=150, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(in_flight=st.sampled_from(_IN_FLIGHT),
       outcomes=st.lists(st.sampled_from(_CLOSE_OUTCOMES), min_size=1, max_size=5))
def test_empty_and_live_slot_precedence(tmp_path, in_flight, outcomes):
    ambient = (_failure('exception') if in_flight == 'handled_exception' else
               _failure('keyboard') if in_flight == 'handled_keyboard' else None)
    primary = None if in_flight in ('none', 'handled_exception', 'handled_keyboard') else _failure(in_flight)
    failures = [_failure(outcome) for outcome in outcomes]
    live, attempted, remaining = [], [], set()
    def close(fd):
        attempted.append(fd)
        assert fd in remaining
        os.close(fd)
        remaining.remove(fd)
        error = failures[live.index(fd)]
        if error is not None:
            raise error
    def operation():
        with rfs._cleanup([None, *live, None], close=close):
            if primary is not None:
                raise primary
    try:
        for _ in outcomes:
            fd = os.open(tmp_path / 'owned', os.O_CREAT | os.O_RDWR, 0o600)
            live.append(fd)
            remaining.add(fd)
        caught = None
        try:
            if ambient is None:
                operation()
            else:
                try:
                    raise ambient
                except BaseException:
                    operation()
        except BaseException as exc:
            caught = exc
        first = next((e for e in failures if e is not None), None)
        cancel = next((e for e in failures if e is not None and not isinstance(e, (Exception, GeneratorExit))), None)
        expected = first if primary is None else cancel if isinstance(primary, (Exception, GeneratorExit)) and cancel is not None else primary
        assert caught is expected
        assert attempted == live and not remaining
        if caught is not None:
            assert caught.__context__ is (ambient if primary is None else primary if caught is not primary else None)
            assert caught.__cause__ is None
    finally:
        for fd in remaining:
            os.close(fd)

@pytest.mark.parametrize('argument', ['(fd,)', '(self.fd,)', '(os.open("x", 0),)',
                                     '[fd]', '[self.fd]', '[os.open("x", 0)]',
                                     'fd', 'self.fd', 'os.open("x", 0)'])
def test_ast_guard_rejects_acquired_arguments(monkeypatch, argument):
    original = Path.read_text
    def read(path, *args, **kwargs):
        source = original(path, *args, **kwargs)
        if path == Path(rfs.__file__):
            source = source.replace('with _cleanup([None]) as owner:', 'with _cleanup(' + argument + ') as owner:', 1)
        return source
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', read)
        with pytest.raises(AssertionError):
            prepared.test_no_cleanup_receives_an_acquired_descriptor()

@pytest.mark.parametrize('site', ['walk-stack', 'helper-return', 'second-slot', 'set-aside', 'set-aside-fd', 'reclaim'])
@pytest.mark.parametrize('kind', ['os', 'keyboard'])
def test_slot_handoffs_close_once_with_reused_number(tmp_path, monkeypatch, site, kind):
    root = tmp_path / 'tree'
    (root / 'a' / 'b').mkdir(parents=True)
    (root / 'a' / 'b' / 'marker').write_bytes(b'scratch')
    sentinel_path = tmp_path / 'stranger'
    sentinel_path.write_bytes(b'other owner')
    borrowed = os.open(root, rfs.O_DIR)
    original_open, original_close = os.open, os.close
    opened, attempted, remaining, strangers = [], [], set(), set()
    live, attempted_generations, unexpected = {}, [], []
    injected = OSError(errno.EIO, 'handoff close') if kind == 'os' else KeyboardInterrupt('handoff close')
    def open_(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        live[fd] = len(opened)
        opened.append(fd)
        remaining.add(fd)
        return fd
    def close(fd):
        attempted.append(fd)
        generation = live.pop(fd, None)
        if generation is None:
            unexpected.append(fd)
        else:
            attempted_generations.append(generation)
        original_close(fd)
        remaining.discard(fd)
        if not strangers:
            spare = original_open(sentinel_path, rfs.O_FILE)
            if spare != fd:
                os.dup2(spare, fd, inheritable=False)
                original_close(spare)
            strangers.add(fd)
            raise injected
    reclaim = rfs.Reclaim({'': {'sig': rfs.signature(root.stat())},
                           'a': {'sig': rfs.signature((root/'a').stat())}}, tmp_path/'conflicts', 'label')
    if site == 'walk-stack':
        invoke = lambda: rfs.tree_bytes(root)
    elif site == 'helper-return':
        invoke = lambda: rfs.RegenerableWalk(borrowed, lambda _: True)._whole(rfs._Rule('venv', '.'), 'a/b')
    elif site == 'second-slot':
        invoke = lambda: rfs._marker(borrowed, 'a/b', (root/'a'/'b').stat(), 'marker')
    elif site == 'set-aside':
        invoke = lambda: reclaim._set_aside(root, 'a', 'a', 'new')
    elif site == 'set-aside-fd':
        invoke = lambda: reclaim._set_aside_fd(borrowed, 'a', 'a', 'new')
    else:
        invoke = lambda: reclaim.run(root)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'open', open_)
            patch.setattr(os, 'close', close)
            caught = None
            try:
                invoke()
            except BaseException as exc:
                caught = exc
        assert strangers and not remaining
        assert sorted(attempted_generations) == list(range(len(opened)))
        assert not live
        assert not unexpected, "close retried after ownership ended"
        assert borrowed not in attempted
        assert set(attempted) == set(opened)
        for fd in strangers:
            assert os.pread(fd, 64, 0) == b'other owner', 'a stale owner closed the reused descriptor'
        # Some documented helper paths handle OSError, but cancellation must escape.
        if kind == 'keyboard':
            assert caught is injected
        elif caught is not None:
            assert caught is injected
        os.fstat(borrowed)
    finally:
        for fd in remaining | strangers:
            try:
                original_close(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
        original_close(borrowed)

@pytest.mark.parametrize('site', ['builder', 'survey'])
@pytest.mark.parametrize('kind', ['memory', 'keyboard'])
def test_regenerable_constructor_keeps_root_owned(tmp_path, monkeypatch, site, kind):
    from collections import defaultdict
    from subfleet import retention_archive as archive, retention_survey as survey
    injected = MemoryError('regen construction') if kind == 'memory' else KeyboardInterrupt('regen construction')
    root = tmp_path / 'root'
    root.mkdir()
    opened, attempted = [], []
    original_open, original_close = os.open, os.close
    def open_(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def close(fd):
        attempted.append(fd)
        original_close(fd)
        raise OSError(errno.EIO, 'cleanup of root')
    def fail(*args, **kwargs):
        raise injected
    builder = object.__new__(archive._Builder)
    builder.j, builder._tick = {}, lambda: None
    ignored = SimpleNamespace(clear=lambda _: True)
    with unchanged_descriptors(), monkeypatch.context() as patch:
        patch.setattr(os, 'open', open_)
        patch.setattr(os, 'close', close)
        patch.setattr(rfs, 'RegenerableWalk', fail)
        with pytest.raises(type(injected)) as caught:
            if site == 'builder':
                builder._walk('worktree', root, {}, None, -1, defaultdict(int), ignored)
            else:
                survey._sample_walk_once(root, None, ignored, None, 0, set())
        assert caught.value is injected
        assert len(opened) == 1 and attempted == opened
