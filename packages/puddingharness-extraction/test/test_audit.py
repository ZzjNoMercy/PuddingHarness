import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("harness_audit", Path(__file__).parents[1] / "audit.py")
audit_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_module)


def put(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def test_scan_covers_files_outside_old_mixed_list_and_applies_overlay(tmp_path):
    put(tmp_path, "backend/knowledge/__init__.py", "")
    put(tmp_path, "backend/graph/session.py", "from knowledge import service\nanalytics_model_id = None\n")
    put(tmp_path, "backend/app.py", "from graph import session\n")
    result = audit_module.audit(tmp_path)
    assert result["status"] == "blocked"
    assert any(f["kind"] == "business_protocol_symbol" for f in result["findings"])
    assert any(f["kind"] == "forbidden_domain_import" for f in result["findings"])
    overlay = tmp_path / "overlays"
    put(overlay, "backend/graph/session.py", "session_id = None\n")
    result = audit_module.audit(tmp_path, overlay)
    assert result["status"] == "python_static_clean"
    assert result["full_repository_verified"] is False
    assert "knowledge" in (tmp_path / "backend/graph/session.py").read_text()


def test_excluded_relative_import_and_unknown_dynamic_import_are_blocked(tmp_path):
    put(tmp_path, "backend/graph/middlewares/tool_intent_router.py", "")
    put(tmp_path, "backend/graph/middlewares/__init__.py", "from . import tool_intent_router\nimportlib.import_module(name)\n")
    result = audit_module.audit(tmp_path)
    assert {f["kind"] for f in result["findings"]} >= {"excluded_module_import", "dynamic_import_review"}


def test_overlay_cannot_reintroduce_excluded_domain(tmp_path):
    overlay = tmp_path / "overlays"
    put(overlay, "backend/knowledge/new.py", "")
    with pytest.raises(ValueError, match="excluded"):
        audit_module.audit(tmp_path, overlay)


def test_reviewed_overlay_refuses_source_drift(tmp_path):
    import hashlib
    import json

    put(tmp_path, "backend/app.py", "original = True\n")
    overlay = tmp_path / "overlays"
    put(overlay, "backend/app.py", "target = True\n")
    manifest = tmp_path / "provenance.json"
    manifest.write_text(json.dumps({"format": "puddingharness-cleanup-overlay/v1", "applied_to_source": False,
        "files": [{"target_path": "backend/app.py",
                   "source_sha256": hashlib.sha256((tmp_path / "backend/app.py").read_bytes()).hexdigest(),
                   "overlay_sha256": hashlib.sha256((overlay / "backend/app.py").read_bytes()).hexdigest()}]}))
    audit_module.verify_overlay_provenance(tmp_path, overlay, manifest)
    (tmp_path / "backend/app.py").write_text("new_user_change = True\n")
    with pytest.raises(ValueError, match="source changed"):
        audit_module.verify_overlay_provenance(tmp_path, overlay, manifest)


def test_generic_browser_connector_is_not_misclassified_as_knowledge(tmp_path):
    put(tmp_path, "backend/connectors/kimi_webbridge/adapter.py", "class Browser: pass\n")
    put(tmp_path, "backend/api/connectors.py", "from connectors.kimi_webbridge.adapter import Browser\n")
    result = audit_module.audit(tmp_path)
    assert {row["path"] for row in result["selected"]} == {
        "backend/connectors/kimi_webbridge/adapter.py", "backend/api/connectors.py"}
    assert result["findings"] == []


def test_business_keyword_argument_cannot_pass_static_gate(tmp_path):
    put(tmp_path, "backend/session.py", "create_session(analytics_model_id=1)\n")
    result = audit_module.audit(tmp_path)
    assert result["status"] == "blocked"
    assert result["findings"] == [{"path": "backend/session.py", "line": 1,
        "kind": "business_protocol_symbol", "target": "analytics_model_id"}]


def test_business_tool_category_is_detected_without_banning_workspace_names(tmp_path):
    put(tmp_path, "backend/research.py", 'get_tools_by_categories("knowledge", {"core"})\n'
        'get_tools_by_categories(root, categories={"knowledge"})\n')
    result = audit_module.audit(tmp_path)
    assert result["findings"] == [{"path": "backend/research.py", "line": 2,
        "kind": "business_tool_category", "target": "knowledge"}]


def test_removed_export_is_detected_even_when_module_is_retained(tmp_path):
    put(tmp_path, 'backend/config.py', 'def get_agent_config(): return {}\ndef get_business_config(): return {}\n')
    put(tmp_path, 'backend/api/agent.py', 'from config import get_agent_config, get_business_config\n')
    overlay = tmp_path / 'overlays'
    put(overlay, 'backend/config.py', 'def get_agent_config(): return {}\n')
    result = audit_module.audit(tmp_path, overlay)
    assert result['findings'] == [{'path': 'backend/api/agent.py', 'line': 1,
        'kind': 'missing_local_export', 'target': 'config.get_business_config'}]


def test_local_exports_include_aliases_and_submodules_not_function_locals(tmp_path):
    put(tmp_path, 'backend/adapter.py', 'from external import Client as Alias\nclass Service: pass\n'
        'def function():\n    hidden = 1\n')
    put(tmp_path, 'backend/package/__init__.py', '')
    put(tmp_path, 'backend/package/child.py', '')
    put(tmp_path, 'backend/main.py', 'from adapter import Alias, Service, hidden\nfrom package import child\n')
    result = audit_module.audit(tmp_path)
    assert result['findings'] == [{'path': 'backend/main.py', 'line': 1,
        'kind': 'missing_local_export', 'target': 'adapter.hidden'}]


def test_dynamic_export_is_not_falsely_reported_missing(tmp_path):
    put(tmp_path, 'backend/dynamic.py', 'def __getattr__(name): return 1\n')
    put(tmp_path, 'backend/main.py', 'from dynamic import computed\n')
    result = audit_module.audit(tmp_path)
    assert not any(f['kind'] == 'missing_local_export' for f in result['findings'])


def test_legacy_environment_cannot_hide_behind_generic_paths_module(tmp_path):
    put(tmp_path, 'backend/worker.py', 'home = os.getenv("PUDDINGCLAW_HOME")\n')
    result = audit_module.audit(tmp_path)
    assert result['findings'] == [{'path': 'backend/worker.py', 'line': 1,
        'kind': 'legacy_runtime_environment', 'target': 'PUDDINGCLAW_HOME'}]


def test_platform_skills_and_catalog_are_excluded_without_removing_generic_skills(tmp_path):
    for path in ('backend/catalog_migration.py', 'backend/graph/database_evidence.py',
                 'backend/graph/database_schema_evidence.py',
                 'backend/skills/build-semantic-dimension/scripts/build.py',
                 'backend/skills/database-analysis/scripts/query.py',
                 'backend/skills/github-monitor/scripts/store_kb.py',
                 'backend/skills/hv-analysis/scripts/report.py'):
        put(tmp_path, path, '')
    report = audit_module.audit(tmp_path)
    assert {row['path'] for row in report['selected']} == {
        'backend/skills/github-monitor/scripts/store_kb.py',
        'backend/skills/hv-analysis/scripts/report.py'}
    assert len(report['excluded']) == 5


def test_skill_local_scripts_imports_use_the_skill_startup_root_and_missing_modules_fail(tmp_path):
    put(tmp_path, 'backend/skills/example/scripts/__init__.py', '')
    put(tmp_path, 'backend/skills/example/scripts/helpers.py', 'def answer(): return 42\n')
    put(tmp_path, 'backend/skills/example/test_skill.py',
        'from scripts.helpers import answer\nassert answer() == 42\n')
    report = audit_module.audit(tmp_path)
    assert report['findings'] == []

    put(tmp_path, 'backend/skills/example/test_missing.py',
        'from scripts.missing import answer\n')
    report = audit_module.audit(tmp_path)
    assert {(finding['line'], finding['kind'], finding['target']) for finding in report['findings']} == {
        (1, 'unresolved_local_import', 'scripts.missing'),
    }


def test_real_skill_script_aliases_are_resolved_without_global_scripts_root():
    report = audit_module.audit(Path(__file__).parents[3])
    unresolved = [finding for finding in report['findings'] if finding['kind'] == 'unresolved_local_import']
    assert not any(finding['path'].startswith('backend/skills/') for finding in unresolved)
