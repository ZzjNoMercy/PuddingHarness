"""Behavior checks for the target-only capability/profile overlays."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_capabilities_overlay_reports_only_harness_owned_services(monkeypatch) -> None:
    module = _load(ROOT / "overlays/backend/capabilities.py", "target_capabilities")

    async def fake_database(_url):
        return module.CapabilityStatus(True, details={"scope": "core"})

    async def fake_docker():
        return module.CapabilityStatus(False, reason="docker unavailable")

    monkeypatch.setattr(module, "_check_postgres", fake_database)
    monkeypatch.setattr(module, "_check_docker", fake_docker)
    monkeypatch.setattr(
        module,
        "_check_cli",
        lambda: module.CapabilityStatus(False, reason="cli unavailable"),
    )

    result = asyncio.run(module.detect_capabilities(force=True)).to_dict()

    assert set(result) == {"core_database", "database", "docker", "cli"}
    assert result["core_database"]["details"]["scope"] == "core"
    assert result["docker"]["available"] is False


def test_sync_database_probe_does_not_turn_connection_errors_into_healthy(
    monkeypatch,
) -> None:
    module = _load(ROOT / "overlays/backend/capabilities.py", "target_capabilities_sync")
    monkeypatch.setattr(module, "_resolve_database_url", lambda _url: "postgresql://db.example/catalog")

    def refused(_target: str) -> None:
        raise ConnectionRefusedError("no PostgreSQL listener")

    monkeypatch.setattr(module, "_sync_select_one", refused)

    result = module._check_postgres_sync(None)

    assert result.available is False
    assert "no PostgreSQL listener" in (result.reason or "")
    assert result.details == {"mode": "postgresql", "scope": "core", "verified": False}


def test_sync_sqlite_probe_requires_a_real_openable_catalog(tmp_path) -> None:
    module = _load(ROOT / "overlays/backend/capabilities.py", "target_capabilities_sqlite")
    missing_file = tmp_path / "catalog.sqlite3"

    result = module._check_postgres_sync(f"sqlite+aiosqlite:///{missing_file}")

    assert result.available is False
    assert result.details == {"mode": "sqlite", "scope": "core", "verified": False}
    assert not missing_file.exists()


def test_sqlite_probe_accepts_readable_db_and_rejects_corruption(tmp_path) -> None:
    module = _load(ROOT / "overlays/backend/capabilities.py", "target_capabilities_sqlite_files")
    readable = tmp_path / "catalog ?#.sqlite3"
    corrupted = tmp_path / "corrupted.sqlite3"
    import sqlite3

    with sqlite3.connect(readable) as connection:
        connection.execute("create table catalog_marker (id integer primary key)")
    corrupted.write_bytes(b"not a sqlite database")

    readable_result = module._check_postgres_sync(f"sqlite+aiosqlite:///{quote(str(readable), safe='/:')}")
    corrupted_result = module._check_postgres_sync(f"sqlite+aiosqlite:///{quote(str(corrupted), safe='/:')}")

    assert readable_result.available is True
    assert readable_result.details == {"mode": "sqlite", "scope": "core"}
    assert corrupted_result.available is False
    assert corrupted_result.details["mode"] == "sqlite"

    async_readable = asyncio.run(module._check_postgres(f"sqlite+aiosqlite:///{quote(str(readable), safe='/:')}"))
    async_missing_path = tmp_path / "async-missing.db"
    async_missing = asyncio.run(module._check_postgres(f"sqlite+aiosqlite:///{quote(str(async_missing_path), safe='/:')}"))
    assert async_readable.available is True
    assert async_missing.available is False


def test_runtime_profile_preserves_frontend_shape_without_business_switches() -> None:
    module = _load(ROOT / "overlays/backend/api/runtime_profile.py", "target_runtime_profile")
    profile = asyncio.run(module.get_runtime_profile())

    assert profile["schema_version"] == 1
    assert profile["profile"] == "harness"
    assert profile["extensions"] == {
        "knowledge": False,
        "analytics": False,
        "headless_worker": True,
    }


def test_target_overlays_have_no_business_imports() -> None:
    business_roots = {"knowledge", "analytics", "vanna", "extensions"}
    for relative in (
        "overlays/backend/capabilities.py",
        "overlays/backend/api/capabilities.py",
        "overlays/backend/api/runtime_profile.py",
    ):
        tree = ast.parse((ROOT / relative).read_text())
        imports = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports |= {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imports & business_roots, (relative, imports & business_roots)
