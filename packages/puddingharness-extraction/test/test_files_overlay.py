import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import HTTPException
from runtime_identity.paths import PuddingClawPaths


@pytest.fixture
def files_overlay(monkeypatch, tmp_path):
    scanner = ModuleType('tools.skills_scanner')
    scanner.scan_skill_registry = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, 'tools.skills_scanner', scanner)
    target = Path(__file__).parents[1] / 'overlays/backend/api/files.py'
    spec = importlib.util.spec_from_file_location('harness_files_overlay', target)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.PuddingClawPaths, 'from_environment', classmethod(lambda cls: PuddingClawPaths(tmp_path)))
    return module


@pytest.mark.parametrize('path', ['knowledge/file.md', 'semantic-assets/a.yml', 'sql-guardrails/a.yml', 'analytics-models/a.yml'])
def test_business_virtual_roots_are_unavailable(files_overlay, path):
    with pytest.raises(HTTPException) as caught:
        files_overlay._validate_path(path)
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_generic_workspace_roundtrip_and_escape_rejection(files_overlay):
    request = files_overlay.FileSaveRequest(path='workspace/note.md', content='generic workspace')
    assert (await files_overlay.save_file(request))['status'] == 'saved'
    assert (await files_overlay.read_file(request.path))['content'] == request.content
    with pytest.raises(HTTPException) as caught:
        files_overlay._validate_path('workspace/../../secret')
    assert caught.value.status_code == 403
    assert (await files_overlay.list_skills()) == {'skills': []}
