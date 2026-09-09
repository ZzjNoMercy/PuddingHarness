"""Target-only environment namespace and credential isolation checks."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).parents[1]
OVERLAY = PACKAGE / "overlays/backend"
LOADER = Path(__file__).with_name("target_runtime_loader.py")

_MIGRATED_NAMES = {
    "PUDDINGCLAW_HEADLESS_SESSION_CLEANUP_INTERVAL_S",
    "PUDDINGCLAW_TIMEOUT_S",
    "PUDDINGCLAW_PROJECTS_ROOT",
    "PUDDINGCLAW_HEADLESS_APPROVAL_MODE",
    "PUDDINGCLAW_CLI_PACKAGE_DIR",
    "PUDDINGCLAW_CLI_INSTALL_POLICY",
    "PUDDINGCLAW_ENV",
    "PUDDINGCLAW_ENVIRONMENT",
    "PUDDINGCLAW_CLI_INSTALL_TIMEOUT_S",
    "PUDDINGCLAW_CATALOG_CHECKPOINT_INTERVAL_SECONDS",
    "PUDDINGCLAW_DISABLE_CATALOG_MAINTENANCE",
    "PUDDINGCLAW_EVALUATION_RUNTIME_ROOT",
    "PUDDINGCLAW_HEADLESS_ALLOWED_DIRECTORIES",
    "PUDDINGCLAW_HEADLESS_ALLOWED_NETWORK_ORIGINS",
    "PUDDINGCLAW_HEADLESS_AUTHORITY_PROFILE",
    "PUDDINGCLAW_HEADLESS_SESSION_TTL_HOURS",
    "PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER",
    "PUDDINGCLAW_INITIAL_PROVIDER",
    "PUDDINGCLAW_INITIAL_PROVIDER_API_KEY",
    "PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER_API_KEY",
    "PUDDINGCLAW_INITIAL_PROVIDER_BOOTSTRAP_ID",
    "PUDDINGCLAW_LARK_CLI_PATH",
    "PUDDINGCLAW_LARK_NATIVE_CREDENTIAL_DIR",
    "PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER",
    "PUDDINGCLAW_MASTER_KEY",
    "PUDDINGCLAW_HTTPS_PROXY",
}


def run_target(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        PUDDINGHARNESS_HOME=str(tmp_path / "harness"),
        PUDDINGCLAW_HOME=str(tmp_path / "legacy"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    for name in _MIGRATED_NAMES:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(LOADER)!r})\n{code}"],
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )


def test_target_overlays_do_not_consume_legacy_runtime_environment_names():
    paths = (
        "api/headless.py",
        "cli_runtime.py",
        "db_maintenance.py",
        "graph/deepagents_manager.py",
        "graph/headless_resolver.py",
        "headless_session_lifecycle.py",
        "provider_registry.py",
        "runtime_identity/host_lark_cli.py",
        "runtime_identity/profiles.py",
        "tools/fetch_url_tool.py",
    )
    for relative in paths:
        source = (OVERLAY / relative).read_text(encoding="utf-8")
        for name in _MIGRATED_NAMES:
            assert name not in source, f"{relative} still contains {name}"


def test_harness_environment_wins_and_legacy_environment_is_ignored(tmp_path):
    result = run_target(
        r'''
import json, os
from pathlib import Path

# Both namespaces are present: only the Harness namespace may affect behavior.
os.environ['PUDDINGCLAW_INITIAL_PROVIDER'] = json.dumps({
    'status': 'configured', 'id': 'legacy-provider', 'name': 'Legacy',
    'base_url': 'https://legacy.invalid/v1', 'model': 'legacy-model'})
os.environ['PUDDINGHARNESS_INITIAL_PROVIDER'] = json.dumps({
    'status': 'configured', 'id': 'harness-provider', 'name': 'Harness',
    'base_url': 'https://harness.invalid/v1', 'model': 'harness-model'})
os.environ['PUDDINGCLAW_INITIAL_PROVIDER_API_KEY'] = 'legacy-key'
os.environ['PUDDINGHARNESS_INITIAL_PROVIDER_API_KEY'] = 'harness-key'
os.environ['PUDDINGCLAW_CLI_INSTALL_POLICY'] = 'auto'
os.environ['PUDDINGHARNESS_CLI_INSTALL_POLICY'] = 'never'
os.environ['PUDDINGCLAW_HEADLESS_SESSION_TTL_HOURS'] = '1'
os.environ['PUDDINGHARNESS_HEADLESS_SESSION_TTL_HOURS'] = '2'
os.environ['PUDDINGCLAW_HEADLESS_ALLOWED_DIRECTORIES'] = '/legacy'
os.environ['PUDDINGHARNESS_HEADLESS_ALLOWED_DIRECTORIES'] = '/harness'
os.environ['PUDDINGCLAW_HEADLESS_AUTHORITY_PROFILE'] = 'legacy'
os.environ['PUDDINGHARNESS_HEADLESS_AUTHORITY_PROFILE'] = 'network'
os.environ['PUDDINGCLAW_LARK_CLI_PATH'] = '/legacy/lark-cli'
os.environ['PUDDINGHARNESS_LARK_CLI_PATH'] = '/harness/lark-cli'
os.environ['PUDDINGCLAW_LARK_NATIVE_CREDENTIAL_DIR'] = '/legacy/lark-state'
os.environ['PUDDINGHARNESS_LARK_NATIVE_CREDENTIAL_DIR'] = '/harness/lark-state'
os.environ['PUDDINGCLAW_HTTPS_PROXY'] = 'http://legacy.invalid:1'
os.environ['PUDDINGHARNESS_HTTPS_PROXY'] = 'http://harness.invalid:2'

from cli_runtime import _requested_policy
import sys, types
for name in (
    'graph.database_sql_revision_resume', 'graph.dimension_build_resume',
    'graph.kernel_fallback_resume', 'graph.logical_dataset_resume',
    'graph.permission_resume', 'graph.skill_plan_resume',
    'graph.skill_secret_resume', 'graph.user_input_resume',
):
    sys.modules[name] = types.SimpleNamespace(
        database_sql_revision_resume_registry={}, dimension_build_resume_registry={},
        kernel_fallback_resume_registry={}, logical_dataset_resume_registry={},
        permission_resume_registry={}, skill_plan_resume_registry={},
        skill_secret_resume_registry={}, user_input_resume_registry={},
    )
from graph.headless_resolver import headless_authority_from_environment
from headless_session_lifecycle import headless_session_ttl_seconds
from provider_registry import _default_registry
from runtime_identity.host_lark_cli import HostLarkCliRuntime
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.profiles import CredentialAuthorityLockedError, MasterKeyProvider
from tools.fetch_url_tool import _configured_https_proxy_url

assert _requested_policy() == ('never', True)
assert headless_session_ttl_seconds() == 7200
assert headless_authority_from_environment() == {
    'profile': 'network', 'directories': ['/harness'], 'network_origins': []}
registry = _default_registry()
assert registry['bindings']['agent'].startswith('harness-provider:')
assert 'legacy-provider' not in {item['id'] for item in registry['providers']}
assert HostLarkCliRuntime._candidate_paths()[0] == Path('/harness/lark-cli')
assert HostLarkCliRuntime._native_credential_dir() == Path('/harness/lark-state')
assert _configured_https_proxy_url() == 'http://harness.invalid:2'

legacy_only = Path(os.environ['PUDDINGHARNESS_HOME']) / 'legacy-only'
legacy_only.mkdir(parents=True)
os.environ.pop('PUDDINGHARNESS_CREDENTIAL_KEY_PROVIDER', None)
os.environ['PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER'] = 'environment'
os.environ.pop('PUDDINGHARNESS_MASTER_KEY', None)
os.environ['PUDDINGCLAW_MASTER_KEY'] = 'aa' * 32
try:
    legacy_authority = MasterKeyProvider(PuddingClawPaths(legacy_only), 'owner').authority()
except CredentialAuthorityLockedError as exc:
    # On macOS the ignored legacy setting leaves the platform Keychain as the
    # authority; an unavailable Keychain must fail closed rather than use it.
    assert os.uname().sysname == 'Darwin' and 'macOS Keychain' in str(exc)
else:
    assert legacy_authority.provider != 'environment'

target_only = Path(os.environ['PUDDINGHARNESS_HOME']) / 'target-only'
target_only.mkdir(parents=True)
os.environ['PUDDINGHARNESS_CREDENTIAL_KEY_PROVIDER'] = 'environment'
os.environ['PUDDINGHARNESS_MASTER_KEY'] = 'bb' * 32
authority = MasterKeyProvider(PuddingClawPaths(target_only), 'owner').authority()
assert authority.provider == 'environment' and authority.key == bytes.fromhex('bb' * 32)
''',
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_target_evaluation_subprocess_uses_harness_runtime_root():
    runner = (OVERLAY / "evaluation/runner.py").read_text(encoding="utf-8")
    worker = (OVERLAY / "evaluation/worker_manager.py").read_text(encoding="utf-8")
    assert 'os.environ["PUDDINGHARNESS_EVALUATION_RUNTIME_ROOT"]' in runner
    assert "PUDDINGCLAW_EVALUATION_RUNTIME_ROOT" not in runner
    assert '"PUDDINGHARNESS_EVALUATION_DB"' in worker
    assert '"PUDDINGHARNESS_EVALUATION_SETTINGS"' in worker
    assert "PUDDINGCLAW_EVALUATION_RUNTIME_ROOT" not in worker
