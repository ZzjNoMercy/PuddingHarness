import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness.installation_manifest import (
    MANIFEST_FORMAT, discover_installation, prepare_installation, validate_manifest,
)
from harness.migration_orchestrator import prepare_migration
from harness.session_import import digest, encoded
from harness.source_snapshot import VerifiedSourceSnapshot
from test_harness_migration_orchestrator import _python_wrapper
from test_source_snapshot_admission import fixture_snapshot

DOMAINS = ('session_harness', 'knowledge_catalog', 'connector_jobs')


def _staging(tmp_path):
    root = fixture_snapshot(tmp_path)
    python = _python_wrapper(tmp_path)
    stage = tmp_path / 'stage'
    result = prepare_migration(root / 'payload', b'{}', python, stage, source_home_snapshot=root)
    receipt = tmp_path / 'receipt.json'
    receipt.write_bytes(json.dumps(result['knowledge_receipt']).encode())
    receipt.chmod(0o600)
    return root, stage, receipt


@pytest.fixture(scope='module')
def staged(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('installation-manifest')
    return _staging(tmp)


def _manifest(path):
    return json.loads(path.read_bytes())


def _rewrite(path, value):
    path.write_bytes(json.dumps(value).encode())


def test_discover_then_prepare_reaches_prepared_and_replays(staged, tmp_path):
    root, stage, receipt = staged
    output = tmp_path / 'manifest' / 'manifest.json'
    discovered = discover_installation(root, output)
    assert discovered['state'] == 'DISCOVERED' and discovered['idempotent'] is False
    assert discovered['activation_allowed'] is False
    assert discovered['installation_cutover_performed'] is False
    manifest = _manifest(output)
    assert manifest['format'] == MANIFEST_FORMAT and manifest['state'] == 'DISCOVERED'
    assert manifest['active_writers'] == {domain: 'puddingclaw' for domain in DOMAINS}
    assert manifest['rollback_window_open'] is True
    assert manifest['rollback_strategy'] == 'no_write_until_finalized'
    assert manifest['credential_rebinds'] == [] and manifest['id_resource_mappings'] == []
    assert manifest['object_summaries'][0]['domain'] == 'session_harness'
    assert manifest['object_summaries'][0]['object_count'] == 1
    assert manifest['staging_namespace'] is None and 'started_at' not in manifest
    validate_manifest(manifest)
    prepared = prepare_installation(root, stage, receipt, output)
    assert prepared['state'] == 'PREPARED' and prepared['idempotent'] is False
    assert prepared['manifest_digest'] != discovered['manifest_digest']
    assert prepared['activation_allowed'] is False and prepared['installation_prepared'] is False
    assert prepared['writer_fence_verified'] is False and prepared['credential_rebind_required'] is True
    manifest = _manifest(output)
    checkpoint = json.loads((stage / 'checkpoint.json').read_bytes())
    assert manifest['state'] == 'PREPARED'
    assert manifest['checkpoint']['orchestrator_plan_digest'] == checkpoint['plan_digest']
    assert manifest['checkpoint']['orchestrator_harness_plan_digest'] == checkpoint['harness_plan_digest']
    assert manifest['checkpoint']['orchestrator_knowledge_receipt_digest'] == checkpoint['receipt_digest']
    assert manifest['targets'] == {'puddingharness': 'puddingharness-backend@0.1.0',
                                   'puddingknowledge': 'puddingknowledge-local@0.1.0'}
    assert manifest['staging_namespace'] == digest(str(stage).encode())
    assert manifest['snapshot_digest'] == prepared['snapshot_digest']
    assert manifest['active_installation_revision'] is None and manifest['completed_at'] is None
    assert isinstance(manifest['started_at'], str) and manifest['started_at']
    validate_manifest(manifest)
    before = output.read_bytes()
    again = prepare_installation(root, stage, receipt, output)
    assert again['idempotent'] is True and again['state'] == 'PREPARED'
    assert again['manifest_digest'] == prepared['manifest_digest']
    assert output.read_bytes() == before


def test_discover_binds_snapshot_commitment_and_retries_byte_stably(staged, tmp_path):
    root, _, _ = staged
    output = tmp_path / 'm' / 'manifest.json'
    first = discover_installation(root, output)
    before = output.read_bytes()
    second = discover_installation(root, output)
    assert first['idempotent'] is False and second['idempotent'] is True
    assert first['manifest_digest'] == second['manifest_digest']
    assert output.read_bytes() == before
    with VerifiedSourceSnapshot(root) as snapshot:
        commitment = snapshot.commitment
    manifest = _manifest(output)
    assert manifest['source'] == {'installation_id': commitment['plan_sha256'],
                                  'schema_revision': 'puddingclaw-source-home-snapshot/v1',
                                  'catalog_revision': 'puddingclaw-source-home-snapshot/v1'}
    assert manifest['snapshot_digest'] == digest(encoded(commitment))
    assert manifest['checkpoint'] == {'source_snapshot_format': commitment['format'],
                                      'source_snapshot_plan_sha256': commitment['plan_sha256'],
                                      'source_snapshot_manifest_sha256': commitment['manifest_sha256']}
    assert manifest['object_summaries'][0]['source_digest'].startswith('sha256:')
    assert manifest['object_summaries'][0]['source_digest'] != digest(encoded({}))


def test_declared_credential_slots_stay_pending_across_prepare(staged, tmp_path):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    slots = [{'slot': 'claw-provider-key', 'source_ref_digest': 'sha256:' + 'a' * 64,
              'target_ref': 'credential://platform/embedding', 'status': 'pending'}]
    discover_installation(root, output, credential_rebinds=slots)
    assert _manifest(output)['credential_rebinds'] == slots
    prepare_installation(root, stage, receipt, output)
    assert _manifest(output)['credential_rebinds'] == slots
    with pytest.raises(ValueError, match='pending'):
        discover_installation(root, tmp_path / 'm2' / 'manifest.json',
                              credential_rebinds=[{**slots[0], 'status': 'rebound'}])
    with pytest.raises(ValueError, match='repeats'):
        discover_installation(root, tmp_path / 'm3' / 'manifest.json', credential_rebinds=slots * 2)


@pytest.mark.parametrize('change', [
    'unknown_key', 'missing_required', 'format', 'snapshot_digest_pattern', 'domain_enum',
    'object_count_bool', 'duplicate_domain', 'state_enum', 'writer_enum', 'strategy_enum',
    'window_type', 'mapping_before_cutover', 'writer_before_cutover', 'bad_resource_uri',
])
def test_manifest_structure_or_increment_violations_are_rejected(staged, tmp_path, change):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    manifest = _manifest(output)
    if change == 'unknown_key': manifest['mystery'] = 'x'
    if change == 'missing_required': del manifest['source']
    if change == 'format': manifest['format'] = 'agent-knowledge-platform-installation-migration/v2'
    if change == 'snapshot_digest_pattern': manifest['snapshot_digest'] = 'sha256:' + 'A' * 64
    if change == 'domain_enum': manifest['object_summaries'][0]['domain'] = 'wiki'
    if change == 'object_count_bool': manifest['object_summaries'][0]['object_count'] = True
    if change == 'duplicate_domain': manifest['object_summaries'].append(dict(manifest['object_summaries'][0]))
    if change == 'state_enum': manifest['state'] = 'discovered'
    if change == 'writer_enum': manifest['active_writers']['session_harness'] = 'claw'
    if change == 'strategy_enum': manifest['rollback_strategy'] = 'delete_source'
    if change == 'window_type': manifest['rollback_window_open'] = 1
    if change == 'mapping_before_cutover':
        manifest['id_resource_mappings'] = [{'source_id': 's', 'resource_uri': 'harness://sessions/s'}]
    if change == 'writer_before_cutover': manifest['active_writers']['session_harness'] = 'puddingharness'
    if change == 'bad_resource_uri':
        manifest['id_resource_mappings'] = [{'source_id': 's', 'resource_uri': 'file:///etc/passwd'}]
    _rewrite(output, manifest)
    with pytest.raises(ValueError):
        prepare_installation(root, stage, receipt, output)
    with pytest.raises(ValueError):
        discover_installation(root, output)


@pytest.mark.parametrize('change', [
    'plan_binding', 'checkpoint_state', 'checkpoint_digest', 'home_manifest_state',
    'home_payload', 'knowledge_artifact', 'receipt_request_digest',
    'receipt_snapshot_identity', 'source_session_file', 'missing_request',
])
def test_orchestrator_staging_drift_is_rejected(tmp_path, change):
    root, stage, receipt = _staging(tmp_path)
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    before = output.read_bytes()
    if change == 'plan_binding':
        path = stage / 'plan.json'
        plan = json.loads(path.read_bytes())
        plan['request_digest'] = 'sha256:' + '0' * 64
        _rewrite(path, plan)
    if change == 'checkpoint_state':
        path = stage / 'checkpoint.json'
        checkpoint = json.loads(path.read_bytes())
        checkpoint['state'] = 'harness_verified'
        _rewrite(path, checkpoint)
    if change == 'checkpoint_digest':
        path = stage / 'checkpoint.json'
        checkpoint = json.loads(path.read_bytes())
        checkpoint['receipt_digest'] = 'sha256:' + '0' * 64
        _rewrite(path, checkpoint)
    if change == 'home_manifest_state':
        path = stage / 'harness' / 'manifest.json'
        home = json.loads(path.read_bytes())
        home['state'] = 'copying'
        _rewrite(path, home)
    if change == 'home_payload':
        (stage / 'harness' / 'payload' / 'sessions' / 's.json').write_bytes(b'changed')
    if change == 'knowledge_artifact':
        (stage / 'knowledge' / 'catalog.json').write_bytes(b'changed')
    if change == 'receipt_request_digest':
        value = json.loads(receipt.read_bytes())
        value['request_digest'] = 'sha256:' + '0' * 64
        _rewrite(receipt, value)
    if change == 'receipt_snapshot_identity':
        value = json.loads(receipt.read_bytes())
        value['source_snapshot_identity'] = 'sha256:' + '0' * 64
        _rewrite(receipt, value)
    if change == 'source_session_file':
        (root / 'payload' / 'sessions' / 's.json').write_bytes(b'changed')
    if change == 'missing_request':
        (stage / 'knowledge-request.json').unlink()
    with pytest.raises((ValueError, OSError)):
        prepare_installation(root, stage, receipt, output)
    assert output.read_bytes() == before


def test_unfinished_orchestrator_checkpoint_is_rejected(tmp_path):
    root = fixture_snapshot(tmp_path)
    python = _python_wrapper(tmp_path)
    stage = tmp_path / 'stage'
    def stop(state):
        if state == 'harness_verified':
            raise RuntimeError('stop before Knowledge delegation')
    with pytest.raises(RuntimeError):
        prepare_migration(root / 'payload', b'{}', python, stage, source_home_snapshot=root,
                          _after_checkpoint=stop)
    receipt = tmp_path / 'receipt.json'
    receipt.write_bytes(b'{}')
    receipt.chmod(0o600)
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    with pytest.raises(ValueError, match='verified inactive partial'):
        prepare_installation(root, stage, receipt, output)
    assert _manifest(output)['state'] == 'DISCOVERED'


def test_prepare_requires_a_discovered_manifest(staged, tmp_path):
    root, stage, receipt = staged
    with pytest.raises(ValueError, match='discover first'):
        prepare_installation(root, stage, receipt, tmp_path / 'm' / 'manifest.json')
    assert not (tmp_path / 'm').exists()


def test_crash_after_prepared_commit_resumes_idempotently(staged, tmp_path):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    def crash(state):
        if state == 'PREPARED':
            raise RuntimeError('crash after commit')
    with pytest.raises(RuntimeError):
        prepare_installation(root, stage, receipt, output, _after_checkpoint=crash)
    assert _manifest(output)['state'] == 'PREPARED'
    result = prepare_installation(root, stage, receipt, output)
    assert result['state'] == 'PREPARED' and result['idempotent'] is True


def test_failed_prepare_leaves_discovered_manifest_unchanged(staged, tmp_path):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    before = output.read_bytes()
    bad = tmp_path / 'bad-receipt.json'
    bad.write_bytes(b'{"format":"wrong"}')
    bad.chmod(0o600)
    with pytest.raises(ValueError):
        prepare_installation(root, stage, bad, output)
    assert output.read_bytes() == before
    assert prepare_installation(root, stage, receipt, output)['state'] == 'PREPARED'


@pytest.mark.parametrize('change', [
    'downgrade_with_residue', 'checkpoint_digest', 'object_count', 'snapshot_digest', 'targets',
    'started_at_type', 'active_writer', 'staging_namespace',
])
def test_prepared_manifest_tampering_is_rejected(staged, tmp_path, change):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    prepare_installation(root, stage, receipt, output)
    manifest = _manifest(output)
    if change == 'downgrade_with_residue':
        del manifest['started_at']
        manifest['state'] = 'DISCOVERED'
        manifest['staging_namespace'] = None
    if change == 'checkpoint_digest':
        manifest['checkpoint']['orchestrator_plan_digest'] = 'sha256:' + '0' * 64
    if change == 'object_count':
        manifest['object_summaries'][0]['object_count'] += 1
    if change == 'snapshot_digest':
        manifest['snapshot_digest'] = 'sha256:' + '0' * 64
    if change == 'targets':
        manifest['targets']['puddingknowledge'] = 'puddingknowledge-local@9.9.9'
    if change == 'started_at_type':
        manifest['started_at'] = 123
    if change == 'active_writer':
        manifest['active_writers']['knowledge_catalog'] = 'puddingknowledge'
    if change == 'staging_namespace':
        manifest['staging_namespace'] = digest(b'elsewhere')
    _rewrite(output, manifest)
    with pytest.raises(ValueError):
        prepare_installation(root, stage, receipt, output)


def test_replay_from_exact_discovered_skeleton_reexecutes_deterministically(staged, tmp_path):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    skeleton = output.read_bytes()
    prepare_installation(root, stage, receipt, output)
    committed = _manifest(output)
    output.write_bytes(skeleton)
    result = prepare_installation(root, stage, receipt, output)
    assert result['state'] == 'PREPARED' and result['idempotent'] is False
    replayed = _manifest(output)
    without_time = lambda value: {key: value[key] for key in value if key != 'started_at'}
    assert without_time(replayed) == without_time(committed)
    assert replayed['checkpoint'] == committed['checkpoint']
    assert prepare_installation(root, stage, receipt, output)['idempotent'] is True


@pytest.mark.parametrize('state', ['CUTOVER', 'ROLLED_BACK', 'FINALIZED'])
def test_completed_states_are_never_reopened(staged, tmp_path, state):
    root, stage, receipt = staged
    output = tmp_path / 'm' / 'manifest.json'
    discover_installation(root, output)
    manifest = _manifest(output)
    manifest['state'] = state
    _rewrite(output, manifest)
    with pytest.raises(ValueError, match='advanced'):
        prepare_installation(root, stage, receipt, output)
    with pytest.raises(ValueError):
        discover_installation(root, output)


def test_manifest_directory_hygiene_and_recovery(staged, tmp_path):
    root, stage, receipt = staged
    directory = tmp_path / 'm'
    output = directory / 'manifest.json'
    discover_installation(root, output)
    stale = directory / '.manifest.json.tmp-0123456789abcdef'
    stale.write_bytes(b'{')
    stale.chmod(0o600)
    assert prepare_installation(root, stage, receipt, output)['state'] == 'PREPARED'
    assert not stale.exists()
    (directory / 'foreign').write_bytes(b'x')
    with pytest.raises(ValueError, match='Unknown'):
        prepare_installation(root, stage, receipt, output)
    with pytest.raises(ValueError, match='filename'):
        discover_installation(root, tmp_path / 'm2' / '.hidden')


def test_roots_must_be_disjoint_and_envelope_is_required(staged, tmp_path):
    root, stage, receipt = staged
    with pytest.raises(ValueError, match='disjoint'):
        discover_installation(root, root / 'payload' / 'manifest.json')
    with pytest.raises(ValueError, match='disjoint'):
        prepare_installation(root, stage, receipt, stage / 'manifest.json')
    with pytest.raises((ValueError, OSError)):
        discover_installation(root / 'payload', tmp_path / 'm' / 'manifest.json')
    assert not (tmp_path / 'm').exists()
    output = tmp_path / 'm2' / 'manifest.json'
    discover_installation(root, output)
    with pytest.raises((ValueError, OSError)):
        prepare_installation(root, stage / 'harness', receipt, output)


def test_cli_discover_prepare_and_fail_closed_error(tmp_path):
    root, stage, receipt = _staging(tmp_path)
    output = tmp_path / 'm' / 'manifest.json'
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])
    def run(*arguments):
        return subprocess.run([sys.executable, '-m', 'harness.installation_manifest', *arguments],
                              env=env, cwd='/private/tmp', capture_output=True, text=True, timeout=30)
    bad = run('discover', '--source-snapshot', str(root / 'payload'), '--output', str(output))
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report['error_code'] == 'installation_manifest_rejected'
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    value = run('discover', '--source-snapshot', str(root), '--output', str(output),
                '--credential-rebind', 'slot-a|' + 'sha256:' + 'b' * 64 + '|credential://platform/x')
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['state'] == 'DISCOVERED' and report['idempotent'] is False
    assert report['activation_allowed'] is False and report['installation_prepared'] is False
    assert str(root) not in value.stdout and str(stage) not in value.stdout
    value = run('prepare', '--source-snapshot', str(root), '--orchestrator-staging', str(stage),
                '--knowledge-receipt', str(receipt), '--output', str(output))
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['state'] == 'PREPARED' and report['installation_cutover_performed'] is False
    assert report['rollback_completed'] is False and report['credential_rebind_required'] is True
    assert str(root) not in value.stdout and str(stage) not in value.stdout
    manifest = _manifest(output)
    assert manifest['credential_rebinds'] == [{'slot': 'slot-a', 'source_ref_digest': 'sha256:' + 'b' * 64,
                                               'target_ref': 'credential://platform/x', 'status': 'pending'}]
    retry = run('prepare', '--source-snapshot', str(root), '--orchestrator-staging', str(stage),
                '--knowledge-receipt', str(receipt), '--output', str(output))
    assert retry.returncode == 0 and json.loads(retry.stdout)['idempotent'] is True
