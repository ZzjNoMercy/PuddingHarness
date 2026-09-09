"""Real JSON persistence tests for the target Session/Run compatibility boundary."""
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).parents[1] / 'overlays/backend'


def load(name, relative, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    compatibility = load('harness.legacy_artifacts', 'harness/legacy_artifacts.py', monkeypatch)
    models = load('harness.models', 'harness/models.py', monkeypatch)
    module = load('target_session_manager', 'graph/session_manager.py', monkeypatch)
    manager = module.SessionManager()
    manager.initialize(sessions_dir=tmp_path / 'sessions')
    return manager, models, compatibility


def run_data():
    return {'run_id': 'run-1', 'query_id': 'query-1', 'session_id': 'session-1',
            'objective': 'read workspace', 'status': 'preparing'}


def test_new_run_and_delegation_reject_retired_selectors(runtime):
    _, models, _ = runtime
    assert models.RunRecord.model_validate(run_data()).run_id == 'run-1'
    with pytest.raises(ValidationError, match='Retired control fields'):
        models.RunRecord.model_validate({**run_data(), 'analytics_model_id': 'old'})
    delegation = {'subagent_run_id': 'child', 'parent_run_id': 'parent',
                  'parent_tool_call_id': 'call', 'session_id': 'session-1',
                  'subagent_type': 'research', 'objective': 'read files'}
    assert models.DelegationContract.model_validate(delegation).objective == 'read files'
    with pytest.raises(ValidationError, match='Retired control fields'):
        models.DelegationContract.model_validate({**delegation, 'selected_analytics_model': 'old'})


def test_historical_read_is_pure_and_preserves_opaque_message_content(runtime):
    manager, _, compatibility = runtime
    old = {'title': 'Legacy', 'messages': [{'role': 'assistant', 'content': {
        'analytics_model_id': 'ordinary external tool payload'}}], 'analytics_model_id': 'old',
        'harness': {'runs': {'run-1': {**run_data(), 'analytics_model_id': 'old',
            'task_profile': {'available_context_refs': ['analytics_model:old', 'resource://external']}}}},
        'traces': {'query-1': {'query_id': 'query-1', 'steps': []}}, 'latest_query_id': 'query-1'}
    original = deepcopy(old)
    path = manager._session_path('session-1')
    path.write_text(json.dumps(old))
    before = {p.relative_to(path.parent): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    result = manager._read_file('session-1')
    assert 'analytics_model_id' not in result
    assert 'analytics_model_id' not in result['harness']['runs']['run-1']
    assert result['messages'] == original['messages']
    assert result['harness']['runs']['run-1']['task_profile']['available_context_refs'] == ['resource://external']
    assert manager._read_trace_file('session-1')['traces'] == original['traces']
    assert 'analytics_model_id' not in manager.list_sessions()[0]
    after = {p.relative_to(path.parent): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    assert after == before
    compatibility.project_legacy_session(old)
    assert old == original


def test_update_old_run_does_not_copy_retired_field_back_to_new_snapshot(runtime):
    manager, _, _ = runtime
    path = manager._session_path('session-1')
    path.write_text(json.dumps({'title': 'Legacy', 'messages': [], 'analytics_model_id': 'old',
        'harness': {'runs': {'run-1': {**run_data(), 'analytics_model_id': 'old'}}}}))
    saved = manager.upsert_run_state('session-1', {**run_data(), 'status': 'running'})
    assert saved['status'] == 'running'
    assert 'analytics_model_id' not in saved
    persisted = json.loads(path.read_text())
    assert 'analytics_model_id' not in persisted
    assert 'analytics_model_id' not in persisted['harness']['runs']['run-1']


def test_new_session_metadata_rejects_retired_field(runtime):
    manager, _, _ = runtime
    with pytest.raises(ValueError, match='Retired control fields'):
        manager.create_session('session-1', metadata={'analytics_model_id': 'old'})
    assert not manager._session_path('session-1').exists()
    manager.create_session('session-1', metadata={'runtime_mode': 'agent'})
    with pytest.raises(ValueError, match='Unsupported Session metadata'):
        manager.update_metadata('session-1', {'analytics_model_id': 'old'})


@pytest.mark.parametrize('file,class_name,payload', [
    ('agent.py', 'AgentRequest', {'message': 'read workspace'}),
    ('sessions.py', 'SessionCreateRequest', {'runtime_mode': 'agent'}),
])
def test_new_api_requests_reject_removed_field_without_changing_generic_validation(
    runtime, monkeypatch, file, class_name, payload,
):
    import ast
    import re
    from types import ModuleType
    from typing import Any, Literal
    from pydantic import BaseModel, Field, model_validator

    _, _, compatibility = runtime
    tree = ast.parse((ROOT / 'api' / file).read_text())
    request = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    module = ModuleType('target_request_' + class_name)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    module.__dict__.update(Any=Any, Literal=Literal, BaseModel=BaseModel, Field=Field,
                           model_validator=model_validator, re=re,
                           reject_legacy_selectors=compatibility.reject_legacy_selectors)
    exec(compile(ast.Module(body=[request], type_ignores=[]), file, 'exec'), module.__dict__)
    model = getattr(module, class_name)
    assert 'analytics_model_id' not in model.model_validate(payload).model_dump()
    with pytest.raises(ValidationError, match='Retired control fields'):
        model.model_validate({**payload, 'analytics_model_id': 'old'})
    if class_name == 'SessionCreateRequest':
        with pytest.raises(ValidationError):
            model.model_validate({'runtime_mode': 'chat'})
    if class_name == 'AgentRequest':
        with pytest.raises(ValidationError, match='goal_id requires'):
            model.model_validate({**payload, 'goal_id': 'goal-1', 'goal_mode': False})
    assert not any(isinstance(n, ast.keyword) and n.arg == 'analytics_model_id' for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and '/analytics-model' in n.value for n in ast.walk(tree))


def test_harness_get_does_not_migrate_expired_leases_or_create_lock(runtime):
    import ast
    import asyncio
    from starlette.concurrency import run_in_threadpool

    manager, _, _ = runtime
    path = manager._session_path('session-1')
    data = {'title': 'Legacy', 'messages': [], 'harness': {},
            'external_artifact_leases': {'lease-1': {'lease_id': 'lease-1', 'status': 'prepared', 'expires_at': 1}}}
    path.write_text(json.dumps(data))
    before = {p.relative_to(path.parent): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    tree = ast.parse((ROOT / 'api/sessions.py').read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_session_harness_state')
    handler.decorator_list = []
    namespace = {'session_manager': manager, 'run_in_threadpool': run_in_threadpool}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), 'target_sessions_handler', 'exec'), namespace)
    result = asyncio.run(namespace['get_session_harness_state']('session-1'))
    assert result['legacy_external_lease_audit']['migrated_lease_ids'] == []
    assert result['legacy_external_lease_audit']['active_lease_count'] == 1
    after = {p.relative_to(path.parent): p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    assert after == before
    migrated = manager.audit_legacy_external_leases('session-1', migrate=True)
    assert migrated['migrated_lease_ids'] == ['lease-1']
    assert json.loads(path.read_text())['external_artifact_leases']['lease-1']['status'] == 'abandoned'


def test_corrupt_sidecar_falls_back_without_rewriting_either_artifact(runtime):
    manager, _, _ = runtime
    path = manager._session_path('session-1')
    trace = {'query_id': 'query-1', 'steps': [{'tool': 'read_file'}]}
    path.write_text(json.dumps({'title': 'Legacy', 'messages': [], 'trace': trace}))
    sidecar = manager._trace_path('session-1')
    sidecar.write_text('{corrupt')
    before = (path.read_bytes(), sidecar.read_bytes())
    assert manager._read_trace_file('session-1')['traces']['query-1'] == trace
    assert (path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.parametrize('invalid', ['a#b', '../ab', '', 'ab/child', 'a' * 129])
def test_invalid_session_ids_cannot_alias_another_session(runtime, invalid):
    manager, _, _ = runtime
    path = manager._session_path('ab')
    path.write_text(json.dumps({'messages': [{'role': 'user', 'content': 'private'}]}))
    original = path.read_bytes()
    for operation in (manager.get_raw_messages, manager.delete_session, manager._read_trace_file):
        with pytest.raises(ValueError, match='Invalid Session id'):
            operation(invalid)
    assert path.read_bytes() == original


def load_coordinators(monkeypatch):
    for relative in ('task_profiles', 'rubric_compiler', 'deterministic_checks'):
        load(f'harness.{relative}', f'harness/{relative}.py', monkeypatch)
    return load('target_coordinators', 'harness/coordinators.py', monkeypatch)


def test_coordinator_creates_generic_run_through_target_contracts(runtime, monkeypatch):
    manager, models, _ = runtime
    coordinator = load_coordinators(monkeypatch).HarnessRunCoordinator(manager)
    manager.create_session('session-1')
    run, goal = coordinator.start_run(
        session_id='session-1', query_id='query-1', objective='修改 Python 代码并验证',
        goal_mode=False, run_review_policy=models.RunReviewPolicy.OFF,
    )
    assert goal is None
    assert run.verification_contract is not None
    assert 'code' in run.task_profile.intents
    assert 'analytics_model_id' not in manager.get_run_state('session-1', run.run_id)
    with pytest.raises(TypeError, match='analytics_model_id'):
        coordinator.start_run(session_id='session-1', query_id='query-2', objective='x',
                              goal_mode=False, analytics_model_id='retired')


def test_goal_revision_recompiles_target_contract_and_rejects_stale_revision(runtime, monkeypatch):
    manager, models, _ = runtime
    coordinator = load_coordinators(monkeypatch).HarnessRunCoordinator(manager)
    manager.create_session('session-1')
    run, goal = coordinator.start_run(
        session_id='session-1', query_id='query-goal', objective='修改 Python 代码',
        goal_mode=True, completion_policy=models.GoalCompletionPolicy.RUBRIC,
    )
    assert goal is not None
    assert run.requires_goal_verification
    revised = coordinator.goals.update_objective(
        'session-1', goal.goal_id, objective='修改 Python 代码并添加测试',
        expected_revision=goal.objective_revision,
    )
    assert revised.objective_revision == goal.objective_revision + 1
    assert revised.goal_contract is not None
    with pytest.raises(models.HarnessStateError, match='revision conflict'):
        coordinator.goals.update_objective('session-1', goal.goal_id, objective='跳过测试',
                                          expected_revision=goal.objective_revision)
    assert manager.get_goal_state('session-1', goal.goal_id)['objective'] == revised.objective


@pytest.mark.parametrize('verifier', ['llm_grader', 'analytics'])
def test_settlement_does_not_activate_legacy_analytics_evidence(runtime, monkeypatch, verifier):
    _, models, _ = runtime
    module = load_coordinators(monkeypatch)
    contract = models.RunVerificationContract(
        contract_id='contract-1', task_type='generic', rubric='Verify consistency',
        criteria=[models.VerificationCriterion(id='metric_consistency', statement='Consistent',
                  source='user', verifier=verifier)],
    )
    report = module.CompletionVerificationCoordinator.report_from_final_state(
        run_id='run-1', contract=contract,
        final_state={'_rubric_status': 'satisfied', '_rubric_evaluations': [
            {'criteria': [{'name': 'metric_consistency', 'passed': True}]}],
            'verification_activations': [{'pack': 'analytics', 'status': 'succeeded',
                'evidence_refs': [{'type': 'analytics_result', 'id': 'legacy'}]}]},
    )
    assert report.evaluations[0].evidence == []
    if verifier == 'analytics':
        assert report.evaluations[0].passed is False
        assert report.status != models.VerificationStatus.SATISFIED
        assert 'Retired verifier' in report.evaluations[0].gap
    else:
        assert report.evaluations[0].passed is True


def test_target_cannot_write_business_ledgers_but_preserves_historical_payload(runtime):
    manager, _, _ = runtime
    old = {'title': 'Legacy', 'messages': [], 'harness': {
        'sql_generation_ledger': {'g1': {'sql': 'SELECT 1'}},
        'sql_validation_receipts': {'v1': {'sql_sha256': 'immutable'}}}}
    path = manager._session_path('session-1')
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    assert manager._read_file('session-1')['harness'] == old['harness']
    for name in ('record_sql_generation', 'record_sql_submission', 'record_database_evidence',
                 'record_database_schema_evidence', 'record_database_path_event',
                 'record_sql_validation_receipt', 'record_sql_execution_attestation',
                 'session_references_result_id', 'result_owner_tool_call'):
        assert not hasattr(manager, name)
    assert path.read_bytes() == before


def test_sql_looking_external_tool_output_remains_generic_session_evidence(runtime):
    manager, _, _ = runtime
    output = 'result_id: result-123 validation_receipt_id: external-123'
    for tool in ('', 'database_sql_execute', 'external_mcp_query'):
        ref = manager._tool_context_raw_ref('session-1', 'tool-1', output, 'hash',
                                           tool_name=tool, source_query_id='query-1')
        assert ref['kind'] == 'session_tool_call'
        assert ref['output_complete'] is True
        assert 'result_id' not in ref


def test_business_handoff_fields_rejected_new_and_projected_readonly(runtime):
    manager, models, _ = runtime
    envelope = {'subagent_run_id': 'child', 'status': 'completed',
                'evidence_refs': ['external:1'], 'validation_receipt_ids': ['generic-check']}
    handoff = {'source_run_id': 'run-1', 'terminal_status': 'completed', 'objective': 'read'}
    with pytest.raises(ValidationError, match='Retired control fields'):
        models.DelegationResultEnvelope.model_validate({**envelope, 'sql_generation_ids': ['g1']})
    with pytest.raises(ValidationError, match='Retired control fields'):
        models.RunHandoffSummary.model_validate({**handoff, 'sql_generation_refs': [{'id':'g1'}]})
    old_run = {**run_data(), 'delegation_results': [{**envelope, 'sql_generation_ids': ['g1']}],
               'handoff_summary': {**handoff, 'sql_generation_refs': [{'id':'g1'}]}}
    path = manager._session_path('session-1')
    path.write_text(json.dumps({'messages': [], 'harness': {'runs': {'run-1': old_run}}}))
    before = path.read_bytes()
    projected = manager.get_run_state('session-1', 'run-1')
    run = models.RunRecord.model_validate(projected)
    assert run.delegation_results[0].validation_receipt_ids == ['generic-check']
    assert run.delegation_results[0].evidence_refs == ['external:1']
    assert 'sql_generation_refs' not in run.handoff_summary.model_dump()
    assert path.read_bytes() == before
