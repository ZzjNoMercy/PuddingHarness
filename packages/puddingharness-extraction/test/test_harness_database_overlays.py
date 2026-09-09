"""Standalone Harness database initialization and migration gates."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
LOADER = ROOT / "packages/puddingharness-extraction/test/target_runtime_loader.py"


def run_target(
    code: str,
    tmp_path: Path,
    *,
    harness_home_name: str = "harness",
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PUDDINGHARNESS_HOME": str(tmp_path / harness_home_name),
        "PUDDINGCLAW_HOME": str(tmp_path / "legacy"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "backend",
    }
    script = f"import runpy; runpy.run_path({str(LOADER)!r})\n{code}"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    return result


def assert_target_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_harness_database_init_is_independent_and_idempotent(tmp_path):
    result = run_target(
        """
import asyncio, os
from pathlib import Path
from sqlalchemy import text
import db
import capabilities
import runtime_control
import schema_migrations

async def main():
    assert {
        'knowledge_bases', 'knowledge_documents', 'knowledge_source_connections',
        'knowledge_source_items', 'knowledge_sync_runs', 'knowledge_import_jobs',
        'knowledge_import_events', 'knowledge_database_sources', 'knowledge_table_assets',
        'read_later_items', 'feishu_app_credentials', 'feishu_user_grants',
        'feishu_oauth_sessions', 'analytics_query_results',
        'semantic_dimension_build_jobs', 'semantic_dimension_build_events', 'task_notifications',
    } <= schema_migrations._DISALLOWED_TABLES
    assert str(Path(os.environ['PUDDINGHARNESS_HOME']) / 'db' / 'catalog.sqlite3') in db.get_database_url()
    assert capabilities._resolve_database_url() == db.get_database_url()
    assert await db.init_database() is True
    engine = db.get_engine()
    async with engine.connect() as conn:
        names = set((await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars())
        assert names == {'worker_access_logs', 'core_runtime_control', 'harness_schema_migrations'}
        versions = (await conn.execute(text("SELECT product, version FROM harness_schema_migrations ORDER BY version"))).all()
        assert versions == [('puddingharness', 1), ('puddingharness', 2)]
        async with db.get_sessionmaker()() as session:
            assert await runtime_control.queue_running_counts(session) == {}
            acquired = await runtime_control.acquire_maintenance(session, owner='test-owner')
            assert acquired['write_mode'] == 'draining'
            entered = await runtime_control.enter_maintenance(session, owner='test-owner')
            assert entered['write_mode'] == 'maintenance'
            released = await runtime_control.release_maintenance(session, owner='test-owner')
            assert released['write_mode'] == 'normal'
    await db.close_database()
    assert await db.init_database() is True
    assert db.get_database_status()['schema_version'] == 2
    await db.close_database()

asyncio.run(main())
""",
        tmp_path,
    )
    assert_target_ok(result)


def test_default_sqlite_path_escapes_url_syntax(tmp_path):
    result = run_target(
        """
import asyncio, os
from pathlib import Path
import db

async def main():
    path = Path(os.environ['PUDDINGHARNESS_HOME']) / 'db' / 'catalog.sqlite3'
    assert '%20' in db.get_database_url() and '%23' in db.get_database_url()
    assert await db.init_database() is True
    assert path.exists()
    await db.close_database()

asyncio.run(main())
""",
        tmp_path,
        harness_home_name="harness ?#",
    )
    assert_target_ok(result)


def test_mixed_legacy_database_is_rejected_without_creating_harness_schema(tmp_path):
    result = run_target(
        """
import asyncio, os, sqlite3
from pathlib import Path
import db

path = Path(os.environ['PUDDINGHARNESS_HOME']) / 'db' / 'catalog.sqlite3'
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as conn:
    conn.execute('CREATE TABLE knowledge_bases (id VARCHAR(64) PRIMARY KEY)')
    conn.commit()
before = path.read_bytes()

async def main():
    assert await db.init_database() is False
    assert 'excluded tables' in (db.get_database_status()['last_error'] or '')
    assert db._engine is None
    await db.close_database()

asyncio.run(main())
with sqlite3.connect(path) as conn:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert tables == {'knowledge_bases'}
assert path.read_bytes() == before
""",
        tmp_path,
    )
    assert_target_ok(result)


def test_version_history_requires_minimum_harness_schema(tmp_path):
    result = run_target(
        """
import asyncio, os, sqlite3
from pathlib import Path
import db

root = Path(os.environ['PUDDINGHARNESS_HOME'])
paths = {
    'missing_worker': root / 'missing-worker.sqlite3',
    'missing_runtime': root / 'missing-runtime.sqlite3',
    'wrong_worker_shape': root / 'wrong-worker-shape.sqlite3',
    'wrong_history_shape': root / 'wrong-history-shape.sqlite3',
    'empty_history_runtime': root / 'empty-history-runtime.sqlite3',
}

def create(path, *, worker=False, runtime=False, versions=(1, 2)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE harness_schema_migrations (product TEXT NOT NULL, version INTEGER NOT NULL, description TEXT NOT NULL, applied_at TEXT NOT NULL, PRIMARY KEY(product, version))')
        for version in versions:
            conn.execute('INSERT INTO harness_schema_migrations VALUES (?, ?, ?, ?)', ('puddingharness', version, 'test', 'now'))
        if worker:
            conn.execute('CREATE TABLE worker_access_logs (id VARCHAR(64) PRIMARY KEY, key_id VARCHAR(120) NOT NULL, key_name VARCHAR(120) NOT NULL, query TEXT NOT NULL, created_at DATETIME NOT NULL)')
        if runtime:
            conn.execute('CREATE TABLE core_runtime_control (id INTEGER PRIMARY KEY CHECK (id = 1), write_mode VARCHAR(20) NOT NULL, maintenance_owner VARCHAR(120), lease_expires_at DATETIME, generation INTEGER NOT NULL, reason TEXT NOT NULL, updated_at DATETIME)')

create(paths['missing_worker'], runtime=True)
create(paths['missing_runtime'], worker=True)
create(paths['wrong_worker_shape'], runtime=True)
with sqlite3.connect(paths['wrong_worker_shape']) as conn:
    conn.execute('CREATE TABLE worker_access_logs (id VARCHAR(64) PRIMARY KEY)')
with sqlite3.connect(paths['wrong_history_shape']) as conn:
    conn.execute('CREATE TABLE harness_schema_migrations (product TEXT, version INTEGER)')
    conn.execute("INSERT INTO harness_schema_migrations VALUES ('puddingharness', 2)")
create(paths['empty_history_runtime'], runtime=True, versions=())

async def main():
    current = {'path': None}
    db.get_database_url = lambda: f"sqlite+aiosqlite:///{current['path']}"
    expected = {
        'missing_worker': 'version 1 requires worker_access_logs',
        'missing_runtime': 'version 2 requires core_runtime_control',
        'wrong_worker_shape': 'missing required columns',
        'wrong_history_shape': 'missing required columns',
        'empty_history_runtime': 'without Harness schema history',
    }
    for name, path in paths.items():
        current['path'] = path
        before = path.read_bytes()
        assert await db.init_database() is False
        assert expected[name] in (db.get_database_status()['last_error'] or '')
        assert db._engine is None
        await db.close_database()
        assert path.read_bytes() == before

asyncio.run(main())
""",
        tmp_path,
    )
    assert_target_ok(result)


def test_literal_percent_path_preflight_matches_writable_engine(tmp_path):
    result = run_target(
        """
import asyncio, os, sqlite3
from pathlib import Path
import db

path = Path(os.environ['PUDDINGHARNESS_HOME']) / 'literal%2Flegacy.sqlite3'
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as conn:
    conn.execute('CREATE TABLE knowledge_bases (id VARCHAR(64) PRIMARY KEY)')
    conn.commit()
before = path.read_bytes()
db.get_database_url = lambda: f"sqlite+aiosqlite:///{path}"

async def main():
    assert await db.init_database() is False
    assert 'excluded tables' in (db.get_database_status()['last_error'] or '')
    assert db._engine is None
    await db.close_database()

asyncio.run(main())
assert path.read_bytes() == before
""",
        tmp_path,
    )
    assert_target_ok(result)


def test_future_version_is_rejected_and_failed_migration_rolls_back(tmp_path):
    result = run_target(
        """
import asyncio, os, sqlite3
from pathlib import Path
import db
import schema_migrations

async def main():
    original = schema_migrations.MIGRATIONS
    def fail_after_ddl(conn):
        schema_migrations._migrate_v2_runtime_control(conn)
        raise RuntimeError('injected migration failure')
    schema_migrations.MIGRATIONS = [
        original[0],
        (2, original[1][1], fail_after_ddl),
    ]
    assert await db.init_database() is False
    await db.close_database()
    path = Path(os.environ['PUDDINGHARNESS_HOME']) / 'db' / 'catalog.sqlite3'
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(), tables

    schema_migrations.MIGRATIONS = original
    assert await db.init_database() is True
    await db.close_database()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("INSERT INTO harness_schema_migrations VALUES (?, ?, ?, ?)",
                     ('other-product', 1, 'foreign', 'now'))
        conn.commit()
    foreign_before = path.read_bytes()
    assert await db.init_database() is False
    assert 'another product' in (db.get_database_status()['last_error'] or '')
    assert db._engine is None
    await db.close_database()
    assert path.read_bytes() == foreign_before
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM harness_schema_migrations WHERE product = ?", ('other-product',))
        conn.execute("INSERT INTO harness_schema_migrations VALUES (?, ?, ?, ?)",
                     ('puddingharness', 99, 'future', 'now'))
        conn.commit()
    future_before = path.read_bytes()
    assert await db.init_database() is False
    assert 'future schema versions' in (db.get_database_status()['last_error'] or '')
    assert db._engine is None
    await db.close_database()
    assert path.read_bytes() == future_before

asyncio.run(main())
""",
        tmp_path,
    )
    assert_target_ok(result)
