"""Run target integration via the guarded loader or a standalone installed Python.

HARNESS_TEST_PYTHON selects an independent installation: child processes run
outside the checkout without PYTHONPATH or the development import loader.
"""
import os
import subprocess
import sys
from pathlib import Path

LOADER = Path(__file__).with_name('target_runtime_loader.py')


def run_target(code, tmp_path):
    env = dict(os.environ)
    env.update(PUDDINGHARNESS_HOME=str(tmp_path / 'harness'),
               PUDDINGCLAW_HOME=str(tmp_path / 'legacy'),
               PYTHONDONTWRITEBYTECODE='1')
    installed_python = os.getenv('HARNESS_TEST_PYTHON')
    if installed_python:
        env.pop('PYTHONPATH', None)
        bootstrap = ''
    else:
        bootstrap = f'import runpy; runpy.run_path({str(LOADER)!r})\n'
    result = subprocess.run([installed_python or sys.executable, '-c', bootstrap + code],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=45)
    if result.returncode:
        output = result.stdout + result.stderr
        log = tmp_path / 'target-runtime-failure.log'
        log.write_text(output)
        raise AssertionError(f'Target runtime failed; full log: {log}\n{output[-12000:]}')


def test_generic_toolsets_and_verification_activation_boundary(tmp_path):
    run_target('''
from tools.toolsets import agent_custom_tool_names, validate_toolset_names, validate_tool_control_descriptors
from harness.verification_activations import verification_packs_for_tool, build_verification_activations, _PROPORTIONAL_MUTATION_TOOLS
from langchain_core.messages import ToolMessage
names = agent_custom_tool_names()
assert {'read_resource', 'request_skill_runtime', 'browser', 'prepare_skill_install'} <= names
assert not names.intersection({'database_sql_execute','read_later_save_url','gbrain_query','llm_wiki_query'})
assert validate_toolset_names(['database_analysis','knowledge_analysis']) == ['database_analysis','knowledge_analysis']
assert validate_tool_control_descriptors() == []
assert not {'apply_logical_dataset_rule','publish_semantic_dimension_build'} & _PROPORTIONAL_MUTATION_TOOLS
assert verification_packs_for_tool('database_sql_execute') == []
assert verification_packs_for_tool('llamaindex_knowledge_query') == []
assert verification_packs_for_tool('terminal', {'command': 'python analyze.csv'}) == []
assert verification_packs_for_tool('terminal', {'command': 'pytest test_app.py'}) == ['code']
assert verification_packs_for_tool('write_file', {'file_path': '/workspace/app.py'}) == ['code']
assert verification_packs_for_tool('web_search') == ['web_research']
activations = build_verification_activations(run_id='run-1',query_id='query-1',tool_call_id='call-1',
    tool_name='database_sql_execute',args={},result=ToolMessage(content='result_id: old-result',tool_call_id='call-1'))
assert activations == []
''', tmp_path)


def test_real_deepagents_graph_reads_workspace_and_persists_generic_run(tmp_path):
    run_target('''
import asyncio, inspect, json, os
from pathlib import Path
from unittest.mock import patch
from graph import deepagents_manager as m
from graph.session_manager import session_manager
from projects.registry import project_registry
from harness.workspace_backends import SpawnWorkspaceBackend
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
root = Path(os.environ['PUDDINGHARNESS_HOME'])
root.mkdir(parents=True)
session_manager.initialize(root / 'sessions')
project_registry.initialize(root / 'projects')
session_manager.create_session('integration-session')
runtime = m.DeepAgentsAgentManager()
runtime.initialize(root / 'app', user_root=root)
workspace = project_registry.ensure_unscoped_workspace('integration-session')
(workspace / 'hello.txt').write_text('TARGET_WORKSPACE_EVIDENCE')
backend = SpawnWorkspaceBackend(root_dir=workspace)
composite = runtime._build_backend(workspace, session_id='integration-session',
    query_id='probe', workspace_backend_override=backend)
assert set(composite.managed_host_path_aliases) == {'/skills','/large_tool_results'}
assert {x['virtual_path'] for x in runtime._filesystem_inventory(workspace)['mounts']} == {'/workspace/','/skills/'}
assert not any((root / 'definitions' / name).exists() for name in ('analytics-models','semantic-assets','sql-guardrails'))
assert not (root / 'knowledge').exists()
assert 'analytics_model_id' not in inspect.signature(runtime.astream).parameters
assert 'analytics_model_snapshot' not in inspect.signature(runtime.astream).parameters
from types import SimpleNamespace
accepted = runtime._extract_hitl_interrupts({'__interrupt__': [SimpleNamespace(
    id='interrupt-1', value={'type':'user_input_request','request':{'id':'input-1'}})]})
assert accepted[0][0] == 'user_input_request'
try:
    runtime._extract_hitl_interrupts({'__interrupt__': [SimpleNamespace(
        id='old', value={'type':'database_sql_revision_request','request':{'id':'old'}})]})
except RuntimeError as exc:
    assert 'Unsupported HITL interrupt type' in str(exc)
else:
    raise AssertionError('Removed business interrupt remained executable')
class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, *args, **kwargs):
        if self.i == 1:
            assert any(isinstance(msg, ToolMessage) and 'TARGET_WORKSPACE_EVIDENCE' in str(msg.content)
                       for msg in messages), 'Real read_file result did not reach the model'
        return super()._generate(messages, *args, **kwargs)
def model_factory(**kwargs):
    return ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'read_file','args':{'file_path':'/workspace/hello.txt'},'id':'read1'}]),
        AIMessage(content='Verified TARGET_WORKSPACE_EVIDENCE.')])
async def no_title(*args):
    pass
async def run():
    with patch.object(m, 'ModelClientChatModel', model_factory), patch.object(m, '_generate_title', no_title):
        return [event async for event in runtime.astream(message='Read hello.txt',
            session_id='integration-session',user_id='test-user',evaluation_workspace_backend=backend)]
events = asyncio.run(run())
assert not any(e['event'] == 'error' for e in events), events
result = json.loads(next(e['data'] for e in events if e['event'] == 'done'))
assert result['run_outcome'] == 'completed', result
assert 'TARGET_WORKSPACE_EVIDENCE' in result['content']
run = session_manager.get_run_state('integration-session', result['run_id'])
assert run['status'] == 'completed'
assert 'analytics_model_id' not in run
assert result['usage_summary']['tool_calls'] >= 1
# Generic secret/runtime tools must remain bound by the host, despite loader
# factories requiring per-Run context rather than a global registration call.
trace = session_manager._read_trace_file('integration-session')
serialized = json.dumps(trace)
assert 'request_skill_runtime' in serialized and 'request_skill_secret' in serialized
''', tmp_path)


def test_headless_transport_waits_for_input_and_records_owned_activity(tmp_path):
    run_target('''
import asyncio, json, os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from api.headless import _HeadlessExecution, HeadlessRunRequest
from graph.session_manager import session_manager
from harness.database_models import Base
from headless_activity import HeadlessActivityLogStore
root = Path(os.environ['PUDDINGHARNESS_HOME'])
root.mkdir(parents=True)
session_manager.initialize(root / 'sessions')
session_manager.create_session('headless-test')
assert HeadlessRunRequest(message='hello').message == 'hello'
async def check():
    engine = create_async_engine('sqlite+aiosqlite:///' + str(root / 'headless.sqlite3'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        assert set(Base.metadata.tables) == {'worker_access_logs'}
        store = HeadlessActivityLogStore(async_sessionmaker(engine,expire_on_commit=False))
        written = await store.record(source_id='test',source_name='Test caller',query='literal 100% query')
        found = await store.list(source_name='Test caller',query='100%')
        assert found['total'] == 1 and found['items'][0]['id'] == written['id']
        assert (await store.list(query='missing'))['total'] == 0
    finally:
        await engine.dispose()
    resume = asyncio.Event()
    async def stream():
        yield {'event':'user_input_required','data':json.dumps({'id':'input-1','prompt':'Choose',
               'run_id':'run-1','query_id':'query-1'})}
        await resume.wait()
        yield {'event':'user_input_resolved','data':json.dumps({'request_id':'input-1'})}
        yield {'event':'run_outcome','data':json.dumps({'run_id':'run-1','outcome':'completed'})}
        yield {'event':'done','data':json.dumps({'content':'finished'})}
    execution = _HeadlessExecution(stream=stream(),session_id='headless-test',project_id='project-1',approval_mode='smart')
    execution.start()
    await execution.wait_for_boundary()
    paused = execution.response()
    assert paused['status'] == 'needs_input' and not execution.done
    assert execution.task and not execution.task.done()
    assert session_manager.get_metadata('headless-test')['headless_pending_input']['request_ids'] == ['input-1']
    assert 'analytics_model_id' not in paused
    revision = execution.revision
    resume.set()
    await execution.wait_for_boundary(after_revision=revision)
    assert execution.response()['status'] == 'completed'
    assert not execution.pending_inputs
    assert session_manager.get_metadata('headless-test')['headless_pending_input']['status'] == 'completed'
asyncio.run(check())
''', tmp_path)


def test_complete_app_lifespan_and_http_session_boundary(tmp_path):
    run_target('''
import os
os.environ['PUDDINGHARNESS_CLI_INSTALL_POLICY'] = 'never'
from pathlib import Path
from fastapi.testclient import TestClient
from app import app
import db
from backend_lease import BackendInstanceLease
from runtime_identity.paths import PuddingClawPaths
from unittest.mock import patch
from graph import deepagents_manager as manager_module
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
import config
config.save_config({'harness': {'terminal': {'execution_mode':'spawn'}}})
class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
async def no_title(*args):
    pass
with patch.object(manager_module, 'ModelClientChatModel', lambda **kw: ScriptedModel(
        responses=[AIMessage(content='HTTP Harness complete.')])), \
     patch.object(manager_module, '_generate_title', no_title), TestClient(app) as client:
    assert client.get('/').json()['name'] == 'PuddingHarness'
    status = db.get_database_status()
    assert status['healthy'] and status['schema_version'] >= 1
    session = client.post('/api/sessions', json={'runtime_mode':'agent'})
    assert session.status_code == 200, session.text
    sid = session.json()['id']
    response = client.post('/api/agent',json={'message':'Say hello','session_id':sid,'stream':False})
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'completed' and response.json()['reply'] == 'HTTP Harness complete.'
    assert client.get('/api/sessions').status_code == 200
    assert client.post('/api/sessions',json={'runtime_mode':'chat'}).status_code == 422
    assert client.post('/api/sessions',json={'analytics_model_id':'old'}).status_code == 422
    assert client.post('/api/chat',json={}).status_code == 404
    assert client.get('/api/worker/models').status_code == 404
assert db._engine is None and db._sessionmaker is None
lease = BackendInstanceLease()
assert lease.acquire(PuddingClawPaths.from_environment().state())
lease.release()
assert not Path(os.environ['PUDDINGCLAW_HOME']).exists()
assert (Path(os.environ['PUDDINGHARNESS_HOME']) / 'db/catalog.sqlite3').is_file()
assert (Path(os.environ['PUDDINGHARNESS_HOME']) / 'sessions' / (sid + '.json')).is_file()
''', tmp_path)


def test_app_releases_lease_when_database_startup_fails(tmp_path):
    run_target('''
import os, sqlite3
from pathlib import Path
os.environ['PUDDINGHARNESS_CLI_INSTALL_POLICY'] = 'never'
from fastapi.testclient import TestClient
from app import app
import db
from backend_lease import BackendInstanceLease
from runtime_identity.paths import PuddingClawPaths
paths = PuddingClawPaths.from_environment()
paths.databases().mkdir(parents=True)
file = paths.databases() / 'catalog.sqlite3'
with sqlite3.connect(file) as connection:
    connection.execute('CREATE TABLE core_schema_migrations (version INTEGER)')
    connection.execute('INSERT INTO core_schema_migrations VALUES (123)')
try:
    with TestClient(app):
        raise AssertionError('Mixed catalog was accepted at startup')
except RuntimeError as exc:
    assert 'Harness database initialization failed' in str(exc), exc
assert db._engine is None and db._sessionmaker is None
lease = BackendInstanceLease()
assert lease.acquire(paths.state())
lease.release()
with sqlite3.connect(file) as connection:
    assert connection.execute('SELECT version FROM core_schema_migrations').fetchone() == (123,)
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('core_schema_migrations',)]
''', tmp_path)


def test_tool_policy_keeps_generic_controls_without_business_name_privileges(tmp_path):
    run_target('''
from types import SimpleNamespace
from unittest.mock import patch
import harness.tool_execution as module
pipeline = module.ToolExecutionPipeline(known_tools={'read_file','external_query'},
    mcp_tool_names={'external_query'},backend_mode='spawn')
assert {'read_file','read_resource','validate_artifact_contract','task'} <= pipeline.DECLARED_ALLOW_TOOLS
assert not {'database_sql_execute','llamaindex_knowledge_query','apply_logical_dataset_rule'} & pipeline.DECLARED_ALLOW_TOOLS
assert not hasattr(pipeline,'SEMANTIC_COMMIT_TOOLS')
pipeline._context = lambda request: {'session_id':'session-1','run_id':'run-1'}
run = {'execution_mode':'delta_repair','delta_repair_kind':'presentation_only'}
with patch.object(module.session_manager,'get_run_state',return_value=run), \
     patch.object(module.session_manager,'reserve_delta_repair_tool_call',return_value={'allowed':True}):
    external = SimpleNamespace(tool_call={'name':'external_query','id':'call-1'})
    denied = pipeline._delta_repair_denial(external)
    assert denied is not None and denied.status == 'error'
    local = SimpleNamespace(tool_call={'name':'read_file','id':'call-2'})
    assert pipeline._delta_repair_denial(local) is None
    run['execution_mode'] = 'native'
    assert pipeline._delta_repair_denial(external) is None
''', tmp_path)


def test_duplicate_app_cannot_initialize_database_or_start_workers(tmp_path):
    run_target('''
import os
os.environ['PUDDINGHARNESS_CLI_INSTALL_POLICY'] = 'never'
from fastapi.testclient import TestClient
from app import app
import db
from backend_lease import BackendInstanceLease
from runtime_identity.paths import PuddingClawPaths
paths = PuddingClawPaths.from_environment()
owner = BackendInstanceLease()
assert owner.acquire(paths.state())
try:
    try:
        with TestClient(app):
            raise AssertionError('Duplicate app started')
    except RuntimeError as exc:
        assert 'already owned' in str(exc), exc
    assert db._engine is None
    assert not (paths.databases() / 'catalog.sqlite3').exists()
    contender = BackendInstanceLease()
    assert not contender.acquire(paths.state())
finally:
    owner.release()
assert contender.acquire(paths.state())
contender.release()
''', tmp_path)


def test_research_inherits_parent_workspace_provider_and_run_policy(tmp_path):
    run_target('''
import asyncio, json, os
from pathlib import Path
from unittest.mock import patch
from graph import deepagents_manager as m
from graph.session_manager import session_manager
from projects.registry import project_registry
from harness.workspace_backends import SpawnWorkspaceBackend
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
root = Path(os.environ['PUDDINGHARNESS_HOME'])
root.mkdir(parents=True)
session_manager.initialize(root / 'sessions')
project_registry.initialize(root / 'projects')
session_manager.create_session('research-session')
runtime = m.DeepAgentsAgentManager()
runtime.initialize(root / 'app', user_root=root)
workspace = project_registry.ensure_unscoped_workspace('research-session')
(workspace / 'evidence.txt').write_text('PARENT_RESEARCH_WORKSPACE_EVIDENCE')
backend = SpawnWorkspaceBackend(root_dir=workspace)
class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, *args, **kwargs):
        if self.i == 2:
            assert any(isinstance(msg, ToolMessage) and 'PARENT_RESEARCH_WORKSPACE_EVIDENCE' in str(msg.content)
                       for msg in messages), 'Research did not read the parent workspace'
        if self.i == 3:
            assert any(isinstance(msg, ToolMessage) and '[deep_research result]' in str(msg.content)
                       and 'PARENT_RESEARCH_WORKSPACE_EVIDENCE' in str(msg.content)
                       for msg in messages), 'Parent did not receive successful research evidence'
        return super()._generate(messages, *args, **kwargs)
def model_factory(**kwargs):
    return ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'deep_research','args':{'query':'Read evidence','scope':'/workspace/evidence.txt'},'id':'research1'}]),
        AIMessage(content='',tool_calls=[{'name':'read_file','args':{'file_path':'/workspace/evidence.txt'},'id':'read1'}]),
        AIMessage(content='Evidence from /workspace/evidence.txt: PARENT_RESEARCH_WORKSPACE_EVIDENCE'),
        AIMessage(content='Verified parent-scoped research.')])
async def no_title(*args):
    pass
async def run():
    with patch.object(m, 'ModelClientChatModel', model_factory), patch.object(m, '_generate_title', no_title):
        return [event async for event in runtime.astream(message='Research evidence.txt',
            session_id='research-session',user_id='test-user',evaluation_workspace_backend=backend)]
events = asyncio.run(run())
assert not any(e['event'] == 'error' for e in events), events
result = json.loads(next(e['data'] for e in events if e['event'] == 'done'))
assert result['run_outcome'] == 'completed', result
assert 'Verified parent-scoped research' in result['content'], result
assert not Path(os.environ['PUDDINGCLAW_HOME']).exists()
''', tmp_path)


def test_research_mcp_read_hint_cannot_bypass_parent_hitl(tmp_path):
    run_target('''
import asyncio, json, os
from pathlib import Path
from unittest.mock import patch
import config, mcp_clients
from graph import deepagents_manager as m
from graph.session_manager import session_manager
from graph.permission_resume import permission_resume_registry
from projects.registry import project_registry
from harness.workspace_backends import SpawnWorkspaceBackend
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
root = Path(os.environ['PUDDINGHARNESS_HOME'])
root.mkdir(parents=True)
config.save_config({'mcp': {'enabled':['fixture']}})
session_manager.initialize(root / 'sessions')
project_registry.initialize(root / 'projects')
session_manager.create_session('research-hitl')
runtime = m.DeepAgentsAgentManager()
runtime.initialize(root / 'app', user_root=root)
workspace = project_registry.ensure_unscoped_workspace('research-hitl')
backend = SpawnWorkspaceBackend(root_dir=workspace)
called = []
async def read_external():
    called.append(True)
    return 'MCP must not execute before approval'
mcp_tool = StructuredTool.from_function(coroutine=read_external,name='fixture_query',
    description='Read external evidence',metadata={'readOnlyHint':True,'destructiveHint':False})
async def tools(_names):
    return [mcp_tool]
class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, *args, **kwargs):
        if self.i == 3:
            assert any(isinstance(msg, ToolMessage) and msg.status == 'error' and
                       '[deep_research error]' in str(msg.content) for msg in messages)
        return super()._generate(messages, *args, **kwargs)
def model_factory(**kwargs):
    return ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'deep_research','args':{'query':'Query external source','scope':'fixture'},'id':'research1'}]),
        AIMessage(content='',tool_calls=[{'name':'fixture_query','args':{},'id':'external1'}]),
        AIMessage(content='The requested evidence was denied.'),
        AIMessage(content='Research was denied; no external evidence was read.')])
async def no_title(*args):
    pass
async def run():
    paused = False
    events = []
    with patch.object(m, 'ModelClientChatModel', model_factory), patch.object(m, '_generate_title', no_title), \
         patch.object(mcp_clients, 'load_filtered_mcp_tools', tools):
        async for event in runtime.astream(message='Research fixture', session_id='research-hitl',
            user_id='test-user',evaluation_workspace_backend=backend):
            events.append(event)
            if event['event'] == 'permission_required':
                request = json.loads(event['data'])
                assert request['tool_name'] == 'fixture_query'
                assert not called
                assert session_manager.get_run_state('research-hitl',request['run_id'])['status'] == 'waiting_hitl'
                paused = True
                assert permission_resume_registry.resolve(request['id'], {'type':'reject'})
    assert paused, [(e['event'], e['data'][:300]) for e in events if e['event'] in {'done','error','permission_required'}]
    assert not called
    assert not any(event['event'] == 'error' for event in events), [event for event in events if event['event'] == 'error']
    final = json.loads(next(event['data'] for event in events if event['event'] == 'done'))
    assert 'Research was denied' in final['content'], final
asyncio.run(run())
''', tmp_path)


def test_mcp_resource_policy_binds_server_and_complete_uri(tmp_path):
    run_target('''
from types import SimpleNamespace
from harness.tool_execution import ToolExecutionPipeline, PolicyDecision
pipeline = ToolExecutionPipeline(known_tools={'read_resource'},backend_mode='spawn')
def request(uri, server='fixture'):
    return SimpleNamespace(tool_call={'name':'read_resource','id':'resource1','args':{
        'resource':uri,'mcp_server':server,'offset':0,'limit':50}},runtime=None)
first = request('knowledge://evidence/' + 'x' * 5000 + 'first')
second = request('knowledge://evidence/' + 'x' * 5000 + 'second')
assert pipeline._preflight(first).decision == PolicyDecision.ASK
assert 'network_access' in pipeline._required_capabilities(first)
assert pipeline._action_preview(first) == pipeline._action_preview(second)
assert pipeline._permission_fingerprint_command(first,pipeline._action_preview(first)) != pipeline._permission_fingerprint_command(second,pipeline._action_preview(second))
other = request(first.tool_call['args']['resource'],'another-server')
assert pipeline._permission_fingerprint_command(first,'') != pipeline._permission_fingerprint_command(other,'')
local = SimpleNamespace(tool_call={'name':'read_resource','args':{'resource':'att_local'}},runtime=None)
assert pipeline._preflight(local).decision == PolicyDecision.ALLOW
''', tmp_path)


def test_mcp_resource_agent_waits_for_approval_before_read(tmp_path):
    run_target('''
import asyncio, json, os
from pathlib import Path
from unittest.mock import patch
from graph import deepagents_manager as m
from graph.session_manager import session_manager
from graph.permission_resume import permission_resume_registry
from projects.registry import project_registry
from harness.workspace_backends import SpawnWorkspaceBackend
from tools import read_resource_tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
root = Path(os.environ['PUDDINGHARNESS_HOME'])
root.mkdir(parents=True)
session_manager.initialize(root / 'sessions')
project_registry.initialize(root / 'projects')
session_manager.create_session('resource-hitl')
runtime = m.DeepAgentsAgentManager()
runtime.initialize(root / 'app', user_root=root)
workspace = project_registry.ensure_unscoped_workspace('resource-hitl')
backend = SpawnWorkspaceBackend(root_dir=workspace)
called = []
async def reader(*args, **kwargs):
    called.append((args, kwargs))
    return {'contents':[{'uri':'knowledge://evidence/one','text':'APPROVED_RESOURCE_EVIDENCE'}]}
def bind(tool, names):
    return tool.model_copy(update={'resource_reader':reader})
class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, messages, *args, **kwargs):
        if self.i == 1:
            assert any(isinstance(msg, ToolMessage) and 'APPROVED_RESOURCE_EVIDENCE' in str(msg.content) for msg in messages), repr(messages)
        return super()._generate(messages, *args, **kwargs)
def model_factory(**kwargs):
    return ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'read_resource','args':{'resource':'knowledge://evidence/one','mcp_server':'fixture'},'id':'resource1'}]),
        AIMessage(content='Approved evidence read.')])
async def no_title(*args):
    pass
async def run():
    paused = False
    events = []
    with patch.object(m, 'ModelClientChatModel', model_factory), patch.object(m, '_generate_title', no_title), patch.object(read_resource_tool, 'bind_mcp_resource_reader', bind):
        async for event in runtime.astream(message='Read the MCP resource', session_id='resource-hitl', user_id='test-user',evaluation_workspace_backend=backend):
            events.append(event)
            if event['event'] == 'permission_required':
                request = json.loads(event['data'])
                assert request['tool_name'] == 'read_resource'
                assert not called
                assert session_manager.get_run_state('resource-hitl',request['run_id'])['status'] == 'waiting_hitl'
                paused = True
                from api.permissions import grant_tool_action_permission, ToolActionGrantRequest
                await grant_tool_action_permission('resource-hitl', ToolActionGrantRequest(permission_request_id=request['id'], scope='once'))
    assert paused
    assert len(called) == 1, called
    assert not any(event['event'] == 'error' for event in events), [event for event in events if event['event'] == 'error']
    final = json.loads(next(event['data'] for event in events if event['event'] == 'done'))
    assert 'Approved evidence read' in final['content'], final
asyncio.run(run())
''', tmp_path)
