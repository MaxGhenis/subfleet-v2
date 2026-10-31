"""P2-6: production owners are entered before descriptor acquisition at every site."""
from __future__ import annotations

import ast
from collections import defaultdict
import dis
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from subfleet import attempt_tmp, retention_archive as archive, retention_fs as rfs
from subfleet import retention_git as git, retention_survey as survey
from tests.unit.test_retention_descriptor_review import raise_at
from tests.unit.test_retention_walk_descriptors import unchanged_descriptors


MODULES = {m.__name__.rpartition('.')[2]: m for m in (rfs, attempt_tmp, git, survey, archive)}
# The ordinal distinguishes separate owners in one function, including both
# slots of nested directory/file sites and both late-admin walks.
SITES = [
    ('retention_fs', 'still_archived', 0),
    ('retention_fs', 'tree_bytes', 0),
    ('retention_fs', 'clone_or_copy', 0),
    ('retention_fs', '_package_or_scope', 0),
    ('retention_fs', '_package_or_scope', 1),
    ('retention_fs', '_layout', 0),
    ('retention_fs', 'bytecode', 0),
    ('retention_fs', 'installed_files', 0),
    ('retention_fs', 'installed_files', 1),
    ('retention_fs', 'record_complete', 0),
    ('retention_fs', '_site_packages', 0),
    ('retention_fs', '_site_packages', 1),
    ('retention_fs', 'RegenerableWalk._rule_for', 0),
    ('retention_fs', 'RegenerableWalk._start', 0),
    ('retention_fs', 'RegenerableWalk._whole', 0),
    ('retention_fs', '_small', 0),
    ('retention_fs', '_marker', 0),
    ('retention_fs', '_marker', 1),
    ('retention_fs', 'sync_path', 0),
    ('retention_fs', 'write_atomic', 0),
    ('retention_fs', 'read_regular', 0),
    ('retention_fs', 'remove_own_tree', 0),
    ('retention_fs', '_remove_children', 0),
    ('retention_fs', 'Reclaim.run', 0),
    ('retention_fs', 'Reclaim._one', 0),
    ('retention_fs', 'Reclaim._set_aside_fd', 0),
    ('retention_fs', 'Reclaim._set_aside', 0),
    ('attempt_tmp', 'AttemptTemp.size', 0),
    ('retention_git', 'admin_objects', 0),
    ('retention_survey', '_walk_sizes', 0),
    ('retention_survey', '_sample_walk_once', 0),
    ('retention_archive', 'Retirement._final_check', 0),
    ('retention_archive', 'Retirement._late_admin', 0),
    ('retention_archive', 'Retirement._late_admin', 1),
    ('retention_archive', '_Builder._walk', 0),
]


def definition(module, name):
    node = ast.parse(Path(module.__file__).read_text())
    value = module
    for part in name.split('.'):
        node = next(n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == part)
        value = getattr(value, part)
    return node, value


def cleanup_call(node):
    return (isinstance(node, ast.Call)
            and getattr(node.func, 'attr', getattr(node.func, 'id', '')) == '_cleanup')


def slot_owners(node):
    return sorted((n for n in ast.walk(node) if isinstance(n, ast.With)
                   and cleanup_call(n.items[0].context_expr)
                   and isinstance(n.items[0].context_expr.args[0], ast.List)), key=lambda n: n.lineno)


# These are resource collections, never an already-acquired descriptor. Both
# collections are empty when their owner is constructed and entered.
COLLECTION_EXCEPTIONS = {
    ('retention_fs', 'walk'): 'lazy stack view entered before the first child acquisition; borrowed root excluded',
    ('retention_fs', 'RegenerableWalk._in_repository'): 'empty list entered before descent and registration',
}
# An unstarted walk generator has acquired no descriptor. Its owner is entered
# before iteration starts. The root descriptor already has its own slot.
ITERATOR_EXCEPTIONS = {
    ('retention_fs', 'tree_bytes'), ('retention_fs', 'record_complete'),
    ('attempt_tmp', 'AttemptTemp.size'), ('retention_git', 'admin_objects'),
    ('retention_survey', '_walk_sizes'), ('retention_survey', '_sample_walk_once'),
    ('retention_archive', 'Retirement._final_check'),
    ('retention_archive', 'Retirement._late_admin'), ('retention_archive', '_Builder._walk'),
}


def test_no_cleanup_receives_an_acquired_descriptor():
    seen, collections, iterators = [], set(), set()
    for module_name, module in MODULES.items():
        def visit(node, name=''):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                name = f'{name}.{node.name}'.lstrip('.')
            for child in ast.iter_child_nodes(node):
                if cleanup_call(child):
                    key = module_name, name
                    argument = child.args[0]
                    if isinstance(argument, ast.List):
                        assert len(argument.elts) == 1 and isinstance(argument.elts[0], ast.Constant)
                        assert argument.elts[0].value is None
                        seen.append(key)
                    elif isinstance(argument, ast.Tuple):
                        assert key in ITERATOR_EXCEPTIONS, key
                        assert len(argument.elts) == 1 and isinstance(argument.elts[0], ast.Name)
                        assert argument.elts[0].id == 'iterator'
                        assert len(child.keywords) == 1 and child.keywords[0].arg == 'close'
                        iterators.add(key)
                    else:
                        assert key in COLLECTION_EXCEPTIONS, key
                        assert isinstance(argument, ast.GeneratorExp) if name == 'walk' else (
                            isinstance(argument, ast.Name) and argument.id == 'opened')
                        collections.add(key)
                visit(child, name)
        visit(ast.parse(Path(module.__file__).read_text()))
    assert sorted(seen) == sorted((m, f) for m, f, _ in SITES)
    assert collections == set(COLLECTION_EXCEPTIONS)
    assert iterators == ITERATOR_EXCEPTIONS


@pytest.fixture
def operations(tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    package = root / '@scope' / 'pkg'
    package.mkdir(parents=True)
    file = package / 'package.json'
    file.write_bytes(b'{}')
    cache = root / '__pycache__'
    cache.mkdir()
    (root / 'm.py').write_bytes(b'')
    pyc = cache / 'm.pyc'
    pyc.write_bytes(b'xx\r\n')
    dist = root / 'lib' / 'python3.14' / 'site-packages' / 'x.dist-info'
    dist.mkdir(parents=True)
    (dist / 'INSTALLER').write_bytes(b'uv')
    (dist / 'RECORD').write_bytes(b'')
    allocation = attempt_tmp.AttemptTemp(tmp_path / 'store')
    record = allocation.create('dummy')
    borrowed = [os.open(p, rfs.O_DIR) for p in (root, package, cache)]
    fd, package_fd, cache_fd = borrowed
    source = os.open(file, rfs.O_FILE)
    borrowed.append(source)
    regen = rfs.RegenerableWalk(fd, lambda _: True)
    reclaim = rfs.Reclaim({'': {'sig': rfs.signature(root.stat())},
                           '@scope': {'sig': rfs.signature((root / '@scope').stat())}},
                          tmp_path / 'conflicts', 'label')
    entry = {'sig': dict(rfs.signature(file.stat()), nlink=2), 'sha256': hashlib.sha256(b'{}').hexdigest()}
    entry['sig']['ctime'] -= 1
    retirement = object.__new__(archive.Retirement)
    retirement.journal = {}
    retirement.ctx = SimpleNamespace(check=lambda: None)
    retirement.manifest = lambda: {'trees': {'worktree': {'entries': []}}}
    retirement.trees = lambda: [('worktree', root)]
    builder = object.__new__(archive._Builder)
    builder.j, builder._tick = {}, lambda: None
    monkeypatch.setattr(rfs, 'FORCE_COPY', True)
    monkeypatch.setattr(rfs, 'free_bytes', lambda _: rfs.LOW_SPACE_FLOOR + 1024)
    calls = {
        'still_archived': lambda: rfs.still_archived(entry, file.stat(), package_fd, file.name),
        'tree_bytes': lambda: rfs.tree_bytes(root),
        'clone_or_copy': lambda: rfs.clone_or_copy(source, fd, 'copy'),
        '_package_or_scope': lambda: rfs._package_or_scope(fd, '@scope', (root / '@scope').stat()),
        '_layout': lambda: rfs._layout(fd, '@scope', (root / '@scope').stat(), rfs._NODE_MODULES),
        'bytecode': lambda: rfs.bytecode(cache_fd, 'm.pyc', pyc.stat()),
        'installed_files': lambda: rfs.installed_files(fd),
        'record_complete': lambda: rfs.record_complete(package_fd, {'x/package.json': ('digest', 2)}, 'x/RECORD'),
        '_site_packages': lambda: rfs._site_packages(fd),
        'RegenerableWalk._rule_for': lambda: regen._rule_for('venv', '@scope', fd, '@scope', (root / '@scope').stat()),
        'RegenerableWalk._start': lambda: regen._start('@scope', {}, rfs._Rule('tox', '@scope'),
                                                       (root / '@scope').stat(), fd, '@scope'),
        'RegenerableWalk._whole': lambda: regen._whole(rfs._Rule('venv', '.'), '@scope'),
        '_small': lambda: rfs._small(package_fd, file.name, 100),
        '_marker': lambda: rfs._marker(fd, '@scope', (root / '@scope').stat(), 'marker'),
        'sync_path': lambda: rfs.sync_path(file),
        'write_atomic': lambda: rfs.write_atomic(tmp_path / 'new', b'scratch'),
        'read_regular': lambda: rfs.read_regular(file),
        'remove_own_tree': lambda: rfs.remove_own_tree(root),
        '_remove_children': lambda: rfs._remove_children(fd),
        'Reclaim.run': lambda: reclaim.run(root),
        'Reclaim._one': lambda: reclaim._one(fd, '', '@scope'),
        'Reclaim._set_aside_fd': lambda: reclaim._set_aside_fd(fd, '@scope', '@scope', 'new'),
        'Reclaim._set_aside': lambda: reclaim._set_aside(root, '@scope', '@scope', 'new'),
        'AttemptTemp.size': lambda: allocation.size(record),
        'admin_objects': lambda: git.admin_objects(root, 'sha1'),
        '_walk_sizes': lambda: survey._walk_sizes(root, None),
        '_sample_walk_once': lambda: survey._sample_walk_once(root, None, None, None, 0, set()),
        'Retirement._final_check': lambda: retirement._final_check(),
        'Retirement._late_admin': lambda: retirement._late_admin({'entries': []}, root),
        '_Builder._walk': lambda: builder._walk('worktree', root, {}, None, -1, defaultdict(int)),
    }
    # A quiet first walk reaches late_admin's second root without git commands.
    def empty_walk(*args, **kwargs):
        yield from ()
    def invoke(name, ordinal):
        if name == 'Retirement._late_admin' and ordinal == 1:
            with monkeypatch.context() as patch:
                patch.setattr(rfs, 'walk', empty_walk)
                calls[name]()
        else:
            calls[name]()
    try:
        yield invoke
    finally:
        for handle in reversed(borrowed):
            os.close(handle)


@pytest.mark.parametrize('module_name,name,ordinal', SITES, ids=['.'.join((m, f, str(i))) for m, f, i in SITES])
@pytest.mark.parametrize('phase', ['construction', 'entry'])
@pytest.mark.parametrize('fault', ['memory', 'keyboard'])
def test_owner_setup_failure_at_every_converted_site(operations, monkeypatch, module_name, name, ordinal, phase, fault):
    node, operation = definition(MODULES[module_name], name)
    line = slot_owners(node)[ordinal].lineno
    method_name = '__init__' if phase == 'construction' else '__enter__'
    method = getattr(rfs._cleanup, method_name)
    offset = next(i.offset for i in dis.get_instructions(method) if i.opname != 'RESUME')
    injected = MemoryError('owner setup') if fault == 'memory' else KeyboardInterrupt('owner setup')
    fired, opened, attempted = [], [], []
    original_open, original_close = os.open, os.close

    def setup(self, *args, **kwargs):
        caller = sys._getframe(1)
        if caller.f_code is operation.__code__ and caller.f_lineno == line:
            with raise_at(method.__code__, offset, injected) as events:
                try:
                    return method(self, *args, **kwargs)
                finally:
                    fired.extend(events)
        return method(self, *args, **kwargs)

    def open_(*args, **kwargs):
        result = original_open(*args, **kwargs)
        opened.append(result)
        return result

    def close(handle):
        attempted.append(handle)
        original_close(handle)

    with unchanged_descriptors(), monkeypatch.context() as patch:
        patch.setattr(rfs._cleanup, method_name, setup)
        patch.setattr(os, 'open', open_)
        patch.setattr(os, 'close', close)
        with pytest.raises(type(injected)) as caught:
            operations(name, ordinal)
        assert fired and caught.value is injected
        # Ancestor descriptors may have been acquired, and must each close once.
        assert sorted(opened) == sorted(attempted)


def test_empty_slots_are_never_closed_and_live_slots_close_once(tmp_path, monkeypatch):
    fd = os.open(tmp_path, rfs.O_DIR)
    attempted = []
    original_close = os.close
    def close(handle):
        attempted.append(handle)
        original_close(handle)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, 'close', close)
            rfs._close_all([None, fd, None], None)
        assert attempted == [fd]
    finally:
        if fd not in attempted:
            original_close(fd)


def test_marker_closes_file_before_directory(tmp_path, monkeypatch):
    directory = tmp_path / 'd'
    directory.mkdir()
    (directory / 'marker').write_bytes(b'scratch')
    parent = os.open(tmp_path, rfs.O_DIR)
    opened, attempted = [], []
    original_open, original_close = os.open, os.close
    def open_(*args, **kwargs):
        handle = original_open(*args, **kwargs)
        opened.append(handle)
        return handle
    def close(handle):
        attempted.append(handle)
        original_close(handle)
    try:
        with unchanged_descriptors(parent), monkeypatch.context() as patch:
            patch.setattr(os, 'open', open_)
            patch.setattr(os, 'close', close)
            assert rfs._marker(parent, 'd', directory.stat(), 'marker') == b'scratch'
        assert len(opened) == 2 and attempted == opened[::-1]
    finally:
        original_close(parent)
