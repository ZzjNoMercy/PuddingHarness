"""Target Evaluation protocol boundary tests.

These tests load the effective target through the same finder used by the package
suite.  They exercise generic Candidate construction, legacy read projection,
and the runner/worker/API call boundary without mutating source artifacts.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

PACKAGE = Path(__file__).parents[1]
ROOT = PACKAGE / "overlays/backend"
LOADER = Path(__file__).with_name("target_runtime_loader.py")


def run_target(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        PUDDINGHARNESS_HOME=str(tmp_path / "harness"),
        PUDDINGCLAW_HOME=str(tmp_path / "legacy"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(LOADER)!r})\n{code}"],
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )


def test_new_candidate_and_experiment_are_protocol_2_without_retired_selector(tmp_path):
    result = run_target(
        """
from pydantic import ValidationError
import json
from evaluation.candidate import CandidateRequest
from evaluation.contracts import PROTOCOL_VERSION, ExperimentCandidate, EvalExperiment
assert PROTOCOL_VERSION == '2.0'
request = CandidateRequest.model_validate({'name': 'generic-agent'})
from api.evaluation import CreateExperimentRequest
api_request = CreateExperimentRequest.model_validate({
    'name': 'run', 'dataset_id': 'dataset', 'dataset_version': 1,
    'candidate_request': {'name': 'generic-agent'},
})
assert api_request.candidate_request.name == 'generic-agent'
try:
    CreateExperimentRequest.model_validate({
        'name': 'run', 'dataset_id': 'dataset', 'dataset_version': 1,
        'candidate_request': {'name': 'generic-agent', 'analytics_model_id': 'retired'},
    })
except ValidationError:
    pass
else:
    raise AssertionError('new Evaluation API silently accepted a retired selector')
assert request.model_dump() == {
    'name': 'generic-agent', 'llm_model_id': None, 'thinking_level': None,
    'credential_name': None, 'project_id': None, 'tool_allowlist': [], 'config': {},
}
try:
    CandidateRequest.model_validate({'name': 'generic-agent', 'analytics_model_id': 'retired'})
except ValidationError:
    pass
else:
    raise AssertionError('new CandidateRequest silently accepted a retired selector')
import types, sys
sys.modules['config'] = types.SimpleNamespace(
    get_fallback_llm_config=lambda **kwargs: {'provider': 'fake', 'model': kwargs.get('model_id_override')}
)
from pathlib import Path
from evaluation.candidate import resolve_candidate
resolved = resolve_candidate(Path('.'), request)
assert resolved.protocol_version == '2.0'
assert 'analytics_model_id' not in resolved.model_dump()
assert 'analytics_model_id' not in resolved.config
candidate = ExperimentCandidate(name='generic-agent')
assert candidate.protocol_version == '2.0'
assert 'analytics_model_id' not in candidate.model_dump()
experiment = EvalExperiment(name='run', dataset_id='dataset', dataset_version=1,
    dataset_version_id='version', dataset_content_hash='hash', candidate=candidate)
assert experiment.protocol_version == '2.0'
assert 'analytics_model_id' not in json.dumps(experiment.model_dump(mode='json'))
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_protocol_1_artifact_projects_in_memory_and_keeps_source_bytes(tmp_path):
    result = run_target(
        """
from evaluation.contracts import EvalExperiment, HistoricalEvaluationArtifact
raw = {
    'protocol_version': '1.0', 'experiment_id': 'old-exp', 'name': 'legacy',
    'dataset_id': 'dataset', 'dataset_version': 1, 'dataset_version_id': 'version',
    'dataset_content_hash': 'hash',
    'candidate': {'protocol_version': '1.0', 'candidate_id': 'old-candidate',
                  'name': 'legacy-agent', 'analytics_model_id': 'old-selector',
                  'config': {'analytics_model_id': 'old-selector', 'generic': True}},
}
original = dict(raw)
encoded = __import__('json').dumps(raw, sort_keys=True)
projected = EvalExperiment.model_validate(raw)
assert projected.protocol_version == '2.0'
assert projected.historical_read_only is True
assert projected.candidate.protocol_version == '2.0'
assert projected.candidate.candidate_id == 'old-candidate'
assert 'analytics_model_id' not in projected.candidate.model_dump()
assert projected.candidate.config == {'generic': True}
assert raw == original
assert __import__('json').dumps(raw, sort_keys=True) == encoded
import asyncio
from evaluation.runner import EvaluationRunner
class Repo:
    def get_experiment(self, _experiment_id):
        return projected
    def update_experiment(self, *args, **kwargs):
        raise AssertionError('historical read attempted a write')
runner = EvaluationRunner.__new__(EvaluationRunner)
runner.repository = Repo()
for operation in (runner.run, runner.retry_projection, runner.run_official_verifier_replay,
                  runner.run_swebench_missing_case_resume):
    try:
        asyncio.run(operation('old-exp'))
    except ValueError as exc:
        assert 'read-only' in str(exc)
    else:
        raise AssertionError('historical artifact entered a mutable Evaluation path')
opaque = HistoricalEvaluationArtifact(protocol_version='1.0', payload=raw)
assert opaque.project()['candidate']['candidate_id'] == 'old-candidate'
assert raw['candidate']['analytics_model_id'] == 'old-selector'
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_target_schema_is_new_and_has_no_retired_selector():
    schema = json.loads((ROOT / "evaluation/schemas/protocol-2.0.json").read_text())
    assert schema["protocol_version"] == "2.0"
    encoded = json.dumps(schema, ensure_ascii=False)
    assert "analytics_model_id" not in encoded
    assert "knowledge" not in encoded.lower()
    assert "vanna" not in encoded.lower()


def test_candidate_runner_worker_and_api_have_no_selector_forwarding():
    candidate = ast.parse((ROOT / "evaluation/candidate.py").read_text())
    runner = ast.parse((ROOT / "evaluation/runner.py").read_text())
    worker = ast.parse((ROOT / "evaluation/worker_manager.py").read_text())
    api = ast.parse((ROOT / "api/evaluation.py").read_text())

    for tree in (candidate, runner, worker, api):
        assert not any(
            isinstance(node, ast.keyword) and node.arg == "analytics_model_id"
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == "analytics_model_id"
            for node in ast.walk(tree)
        )
    # The runner still forwards all target DeepAgents generic controls.
    call = next(
        node for node in ast.walk(runner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "astream"
    )
    names = {keyword.arg for keyword in call.keywords}
    assert {
        "message", "session_id", "project_id", "llm_model_id", "thinking_level",
        "credential_name", "callbacks_override", "evaluation_tool_allowlist",
        "disable_mcp", "evaluation_builtin_tool_allowlist", "evaluation_required_toolset",
        "evaluation_workspace_backend",
    } <= names
    assert "analytics_model_id" not in names
    assert "historical_read_only" in (ROOT / "evaluation/runner.py").read_text()

    # The new API request still owns the complete generic candidate closure.
    request = next(
        node for node in api.body
        if isinstance(node, ast.ClassDef) and node.name == "CreateExperimentRequest"
    )
    fields = {
        node.target.id
        for node in request.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert {"name", "dataset_id", "dataset_version", "candidate_request", "profile_id", "execution"} <= fields
    assert "analytics_model_id" not in fields


def test_worker_restart_refresh_uses_target_home_environment():
    source = (ROOT / "evaluation/worker_manager.py").read_text()
    assert "PUDDINGHARNESS_EVALUATION_DB" in source
    assert "PUDDINGHARNESS_EVALUATION_SETTINGS" in source
    assert "PUDDINGHARNESS_HOME" in source
    assert "PUDDINGCLAW_EVALUATION" not in source


def test_target_evaluation_env_and_repository_keep_legacy_rows_read_only(tmp_path):
    result = run_target(
        """
import json, os, sqlite3
from pathlib import Path
from evaluation.contracts import EvaluationResult
from evaluation.repository import EvaluationRepository, ConflictError
from evaluation.settings import EvaluationSettingsStore

db_path = Path(os.environ['PUDDINGHARNESS_HOME']) / 'evaluation.sqlite3'
os.environ['PUDDINGHARNESS_EVALUATION_DB'] = str(db_path)
os.environ['PUDDINGHARNESS_EVALUATION_SETTINGS'] = str(Path(os.environ['PUDDINGHARNESS_HOME']) / 'evaluation.json')
repo = EvaluationRepository()
assert repo.db_path == db_path
assert EvaluationSettingsStore().path == Path(os.environ['PUDDINGHARNESS_EVALUATION_SETTINGS'])
raw = {
    'protocol_version': '1.0', 'experiment_id': 'legacy-exp', 'name': 'legacy',
    'dataset_id': 'dataset', 'dataset_version': 1, 'dataset_version_id': 'version',
    'dataset_content_hash': 'hash', 'candidate': {'protocol_version': '1.0',
    'candidate_id': 'legacy-candidate', 'name': 'legacy-agent',
    'analytics_model_id': 'retired'}, 'status': 'completed',
}
encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
with sqlite3.connect(db_path) as connection:
    connection.execute(
        'INSERT INTO eval_experiments VALUES (?, ?, ?, ?, ?)',
        ('legacy-exp', encoded, 'completed', 'created', 'updated'))
    connection.execute(
        'INSERT INTO eval_case_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ('legacy-attempt', 'legacy-exp', 'legacy-case', 0, 'running', None, None, 'created', 'updated'))
    connection.commit()
projected = repo.get_experiment('legacy-exp')
assert projected.historical_read_only is True
# Simulate a model_dump/revalidate round trip: the internal marker is gone,
# so repository mutation must consult the raw row instead.
revalidated = type(projected).model_validate(projected.model_dump())
assert revalidated.historical_read_only is False
for operation in (
    lambda: repo.update_experiment(revalidated),
    lambda: repo.delete_experiment('legacy-exp'),
    lambda: repo.cancel_running_attempts('legacy-exp', 'cancel'),
    lambda: repo.create_attempt('legacy-exp', 'new-case', 1),
    lambda: repo.update_attempt_status('legacy-attempt', 'failed'),
    lambda: repo.update_attempt_run('legacy-attempt', {'response': 'mutated'}),
    lambda: repo.finish_attempt('legacy-attempt', status='failed'),
    lambda: repo.save_result('legacy-exp', 'legacy-attempt', EvaluationResult(
        evaluator_id='test', evaluator_version='1', dimension='task_completion',
        outcome='pass', score=1, passed=True, reason='mutated')),
):
    try:
        operation()
    except ConflictError as exc:
        assert 'read-only' in str(exc)
    else:
        raise AssertionError('historical repository row was mutated')
with repo._connect() as connection:
    assert connection.execute(
        'SELECT status, run_envelope_json FROM eval_case_attempts WHERE attempt_id=?', ('legacy-attempt',)
    ).fetchone()['status'] == 'running'
with sqlite3.connect(db_path) as connection:
    assert connection.execute(
        'SELECT payload_json FROM eval_experiments WHERE experiment_id=?', ('legacy-exp',)
    ).fetchone()[0] == encoded
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_evaluation_producer_consumers_use_harness_environment_names():
    runtime = ROOT / "evaluation"
    for name in ("repository.py", "settings.py", "official_swebench.py", "swebench_agent_backend.py"):
        source = (runtime / name).read_text()
        assert "PUDDINGCLAW_" not in source
        assert "PUDDINGHARNESS_" in source
