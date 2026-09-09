"""Authority regression: removing a business mount must remove its implicit grant."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

OVERLAYS = Path(__file__).parents[1] / 'overlays/backend/graph'


def load(name, monkeypatch):
    spec = importlib.util.spec_from_file_location('target_' + name, OVERLAYS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('path', ['/knowledge/a.png', '/semantic-assets/a.md', '/sql-guardrails/a.yml', '/analytics-models/a.md'])
def test_removed_mounts_have_no_implicit_authority(path, monkeypatch, tmp_path):
    module = load('virtual_paths', monkeypatch)
    assert not module.is_virtual_path(path)
    assert module.classify_path_authority(path, workspace_root=tmp_path).authority == module.PathAuthority.EXTERNAL


def test_generic_mounts_and_workspace_escape_stay_enforced(monkeypatch, tmp_path):
    module = load('virtual_paths', monkeypatch)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'escape').symlink_to(tmp_path)
    cases = {'/workspace/a.txt': 'workspace', '/scratch/a.txt': 'scratch',
             '/skills/a.md': 'managed', '/large_tool_results/a.txt': 'managed',
             '/workspace/../outside': 'escape', '/workspace/escape/outside': 'escape'}
    for path, authority in cases.items():
        assert module.classify_path_authority(path, workspace_root=workspace).authority.value == authority


def test_only_generic_attachment_root_remains_implicitly_managed(monkeypatch, tmp_path):
    attachments = tmp_path / 'attachments'
    attachments.mkdir()
    knowledge = tmp_path / 'knowledge'
    knowledge.mkdir()
    (attachments / 'escape').symlink_to(knowledge)
    stub = ModuleType('graph.attachment_store')
    stub.attachment_store = SimpleNamespace(root_dir=attachments)
    monkeypatch.setitem(sys.modules, 'graph.attachment_store', stub)
    monkeypatch.setitem(sys.modules, 'knowledge.paths', None)
    module = load('managed_paths', monkeypatch)
    assert module.is_managed_resource_path(attachments / 'image.png', tmp_path)
    assert not module.is_managed_resource_path(knowledge / 'image.png', tmp_path)
    assert not module.is_managed_resource_path(attachments / 'escape/image.png', tmp_path)


def identity_overlay(monkeypatch):
    path = OVERLAYS.parent / 'runtime_identity/paths.py'
    spec = importlib.util.spec_from_file_location('target_identity_paths', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_harness_home_never_falls_back_to_legacy_home(monkeypatch, tmp_path):
    module = identity_overlay(monkeypatch)
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'sentinel').write_text('original')
    monkeypatch.setenv('PUDDINGCLAW_HOME', str(legacy))
    monkeypatch.setenv('PUDDINGCLAW_OWNER_USER_ID', 'legacy-owner')
    monkeypatch.delenv('PUDDINGHARNESS_HOME', raising=False)
    monkeypatch.delenv('PUDDINGHARNESS_OWNER_USER_ID', raising=False)
    monkeypatch.setattr(module.Path, 'home', classmethod(lambda cls: tmp_path))
    paths = module.PuddingClawPaths.from_environment()
    assert paths.root == tmp_path / '.puddingharness'
    assert module.trusted_owner_user_id() == 'local'
    paths.ensure_layout()
    assert paths.sessions().is_dir()
    assert paths.agent_workspaces().is_dir()
    assert paths.config().is_dir()
    assert sorted(p.name for p in legacy.iterdir()) == ['sentinel']
    for business in ('knowledge', 'definitions/semantic-assets', 'definitions/analytics-models',
                     'definitions/sql-guardrails', 'data/query-results'):
        assert not (paths.root / business).exists()
    assert not hasattr(paths, 'knowledge')


def test_harness_explicit_home_and_owner_are_validated(monkeypatch, tmp_path):
    module = identity_overlay(monkeypatch)
    monkeypatch.setenv('PUDDINGHARNESS_HOME', str(tmp_path / 'target'))
    monkeypatch.setenv('PUDDINGHARNESS_OWNER_USER_ID', 'target-owner')
    assert module.PuddingClawPaths.from_environment().root == tmp_path / 'target'
    assert module.trusted_owner_user_id() == 'target-owner'
    monkeypatch.setenv('PUDDINGHARNESS_HOME', 'relative')
    with pytest.raises(ValueError, match='absolute'):
        module.PuddingClawPaths.from_environment()
    monkeypatch.setenv('PUDDINGHARNESS_OWNER_USER_ID', '../legacy')
    with pytest.raises(ValueError):
        module.trusted_owner_user_id()
