"""Target-only contracts for the detached Headless transport.

These tests intentionally load only syntax-level API pieces and a registry
isolated resolver.  Importing the source application would reintroduce the
business modules this overlay is meant to exclude.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).parents[3]
HEADLESS = ROOT / "packages/puddingharness-extraction/overlays/backend/api/headless.py"
RESOLVER = ROOT / "packages/puddingharness-extraction/overlays/backend/graph/headless_resolver.py"
LIFECYCLE = ROOT / "packages/puddingharness-extraction/overlays/backend/headless_session_lifecycle.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(path: Path, name: str, namespace: dict[str, object]) -> type:
    node = next(node for node in _tree(path).body if isinstance(node, ast.ClassDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]  # type: ignore[return-value]


def _function(path: Path, name: str, namespace: dict[str, object]) -> object:
    node = next(
        node
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def test_headless_overlay_has_no_business_selector_or_capability_contract():
    text = HEADLESS.read_text(encoding="utf-8")
    forbidden = (
        "analytics_model_id",
        "analytics_model_match",
        "analytics_model_snapshot",
        "AnalyticsModelRoute",
        "get_analytics_model_registry",
        "_route_analytics_model",
        "_model_binding",
        "data.query",
        "data.analysis",
        "data.nl2sql",
        "knowledge.query",
        "database_sql_revision",
        "dimension_build_rule",
        "logical_dataset_rule",
    )
    assert not [token for token in forbidden if token in text]
    assert '"capabilities": ["agent.run", "workspace.files", "mcp"]' in text
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("analytics")
        for node in _tree(HEADLESS).body
    )


def test_headless_request_rejects_removed_selector_fields():
    namespace = {
        "BaseModel": BaseModel,
        "ConfigDict": ConfigDict,
        "Field": Field,
        "model_validator": model_validator,
        "Any": Any,
    }
    request = _class(HEADLESS, "HeadlessRunRequest", namespace)
    request.model_rebuild(_types_namespace={"Any": Any})
    assert request(message="hello").message == "hello"
    with pytest.raises(Exception):
        request.model_validate({"message": "hello", "analytics_model_id": "legacy"})


def test_start_execution_passes_only_generic_manager_protocol():
    calls: list[dict[str, object]] = []

    class Manager:
        def astream(self, **kwargs):
            calls.append(kwargs)

            async def events():
                if False:
                    yield None

            return events()

    class Execution:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            return None

    namespace = {
        "Any": object,
        "deepagents_agent_manager": Manager(),
        "_HeadlessExecution": Execution,
        "_headless_executions": {},
        "_headless_executions_lock": __import__("threading").RLock(),
        "_prune_headless_executions": lambda: None,
    }
    start = _function(HEADLESS, "_start_headless_execution", namespace)
    request = SimpleNamespace(message="hello")
    execution = asyncio.run(
        start(
            request=request,
            session_id="session",
            project_id="project",
            approval_mode="smart",
            authority={"profile": "restricted", "directories": [], "network_origins": []},
            request_received_at=123.0,
        )
    )
    assert isinstance(execution, Execution)
    assert len(calls) == 1
    assert "analytics_model_id" not in calls[0]
    assert "analytics_model_snapshot" not in calls[0]
    assert calls[0]["interaction_mode"] == "external"
    assert calls[0]["authority_profile"] == "restricted"


def test_consume_run_waits_for_generic_execution_boundary():
    state = {"waited": False, "cancelled": False}

    class Execution:
        async def wait_for_boundary(self):
            state["waited"] = True

        async def cancel(self):
            state["cancelled"] = True

        def response(self):
            return {"status": "completed", "outcome": "completed"}

    async def start(**kwargs):
        assert "analytics_model_id" not in kwargs
        assert "analytics_model_snapshot" not in kwargs
        return Execution()

    namespace = {"Any": Any, "_start_headless_execution": start}
    consume = _function(HEADLESS, "_consume_run", namespace)
    result = asyncio.run(
        consume(
            request=SimpleNamespace(message="hello"),
            session_id="session",
            project_id="project",
            approval_mode="smart",
            authority={"profile": "restricted"},
            request_received_at=123.0,
        )
    )
    assert result == {"status": "completed", "outcome": "completed"}
    assert state == {"waited": True, "cancelled": False}


def test_execution_responses_do_not_reintroduce_removed_selector_payloads():
    namespace = {
        "Any": object,
        "asyncio": asyncio,
        "json": __import__("json"),
        "secrets": __import__("secrets"),
        "time": __import__("time"),
        "_HEADLESS_EVENT_HISTORY_LIMIT": 32,
        "_HEADLESS_SUBSCRIBER_QUEUE_LIMIT": 8,
        "_needs_input": lambda *_args: None,
        "_headless_artifacts": lambda *_args: [],
        "session_manager": SimpleNamespace(update_metadata=lambda *_args, **_kwargs: None),
    }
    execution_cls = _class(HEADLESS, "_HeadlessExecution", namespace)
    execution = execution_cls(
        stream=SimpleNamespace(),
        session_id="session",
        project_id="project",
        approval_mode="smart",
    )
    execution.pending_inputs = {"p": {"request_id": "p", "type": "permission_request"}}
    pending = execution.response()
    assert pending["status"] == "needs_input"
    assert not {"analytics_model_id", "analytics_model_snapshot"}.intersection(pending)

    execution.pending_inputs = {}
    execution.cancelled = True
    cancelled = execution.response()
    assert cancelled["status"] == "cancelled"
    assert not {"analytics_model_id", "analytics_model_snapshot"}.intersection(cancelled)

    execution.cancelled = False
    execution.done = True
    execution.outcome = {"status": "completed", "outcome": "completed"}
    final = execution.response()
    assert final["status"] == "completed"
    assert not {"analytics_model_id", "analytics_model_snapshot"}.intersection(final)


def _load_resolver(monkeypatch):
    class Registry:
        def __init__(self, name):
            self.name = name
            self.calls = []

        def resolve(self, request_id, decision):
            self.calls.append(("resolve", request_id, decision))
            return dict(decision)

        def cancel(self, request_id, reason):
            self.calls.append(("cancel", request_id, reason))
            return {"action": "cancel", "reason": reason}

    registries = {name: Registry(name) for name in ("kernel", "permission", "skill_plan", "skill_secret", "user_input")}
    modules = {
        "graph.kernel_fallback_resume": ("kernel_fallback_resume_registry", registries["kernel"]),
        "graph.permission_resume": ("permission_resume_registry", registries["permission"]),
        "graph.skill_plan_resume": ("skill_plan_resume_registry", registries["skill_plan"]),
        "graph.skill_secret_resume": ("skill_secret_resume_registry", registries["skill_secret"]),
        "graph.user_input_resume": ("user_input_resume_registry", registries["user_input"]),
    }
    for module_name, (attr, registry) in modules.items():
        module = types.ModuleType(module_name)
        setattr(module, attr, registry)
        monkeypatch.setitem(sys.modules, module_name, module)

    spec = importlib.util.spec_from_file_location("target_headless_resolver", RESOLVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, registries


def test_resolver_keeps_generic_auto_decisions_and_rejects_removed_interrupts(monkeypatch):
    resolver_module, registries = _load_resolver(monkeypatch)
    context = {"interaction_mode": "auto"}
    resolver = resolver_module.HeadlessInterruptResolver(context=context)

    permission = resolver.resolve("permission_request", {"id": "p1", "tool_name": "execute"})
    assert permission["type"] == "reject"
    assert registries["permission"].calls

    user = resolver.resolve("user_input_request", {"id": "u1", "allow_agent_decide": True})
    assert user["action"] == "agent_decide"
    assert context["_headless_interrupt_summary"]["total"] == 2

    skill = resolver.resolve("skill_plan_confirmation_request", {"id": "s1"})
    assert skill["action"] == "cancel"
    assert "skill" in skill["reason"]

    with pytest.raises(ValueError, match="Unsupported headless interrupt"):
        resolver.resolve("database_sql_revision_request", {"id": "legacy"})


def test_resolver_external_user_input_can_remain_consumer_driven(monkeypatch):
    resolver_module, _ = _load_resolver(monkeypatch)
    context = {"interaction_mode": "external"}
    resolver = resolver_module.HeadlessInterruptResolver(context=context)
    decision = resolver._decision("user_input_request", {"allow_agent_decide": False})
    assert decision["action"] == "cancel"
    assert decision["reason"] == "headless_user_input_required"


def test_lifecycle_overlay_excludes_business_resume_registries():
    text = LIFECYCLE.read_text(encoding="utf-8")
    assert not any(
        token in text
        for token in (
            "database_sql_revision",
            "dimension_build_rule",
            "logical_dataset_rule",
            "analytics",
            "knowledge",
        )
    )

    spec = importlib.util.spec_from_file_location("target_headless_session_lifecycle", LIFECYCLE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    from graph.kernel_fallback_resume import kernel_fallback_resume_registry
    from graph.skill_secret_resume import skill_secret_resume_registry

    assert kernel_fallback_resume_registry in module._RESUME_REGISTRIES
    assert skill_secret_resume_registry in module._RESUME_REGISTRIES

    class PendingRegistry:
        def __init__(self, pending):
            self.pending = pending

        def has_pending_session(self, session_id):
            return session_id in self.pending

    pending = PendingRegistry({"pending"})
    assert module.headless_session_has_pending_resume("pending", [pending])
    assert not module.headless_session_has_pending_resume("idle", [pending])

    # Skill-secret/kernel registries have no has_pending_session() helper;
    # their request ledger must still protect an active Session from cleanup.
    ledger_only = SimpleNamespace(_requests={"r": {"session_id": "ledger", "status": "pending"}})
    assert module.headless_session_has_pending_resume("ledger", [ledger_only])

    class Manager:
        is_initialized = True

        def __init__(self):
            self.deleted = []

        def list_sessions(self):
            return [
                {"id": "idle"},
                {"id": "pending"},
                {"id": "ledger"},
                {"id": "protected"},
            ]

        def delete_session_if_idle_headless_before(self, session_id, **kwargs):
            self.deleted.append((session_id, kwargs))
            return True

    manager = Manager()
    deleted = module.cleanup_stale_headless_sessions(
        manager=manager,
        now=100.0,
        ttl_seconds=10.0,
        protected_session_ids={"protected"},
        resume_registries=[pending, ledger_only],
    )
    assert deleted == ["idle"]
    assert manager.deleted[0][1]["cutoff"] == 90.0
    assert manager.deleted[0][1]["terminal_run_statuses"] == module.TERMINAL_RUN_STATUSES


def test_target_loader_imports_lifecycle_without_excluded_modules():
    loader = ROOT / "packages/puddingharness-extraction/test/target_runtime_loader.py"
    code = f"""
import importlib.util
from pathlib import Path
loader = Path({str(loader)!r})
spec = importlib.util.spec_from_file_location('target_runtime_loader', loader)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import headless_session_lifecycle
assert 'overlays/backend/headless_session_lifecycle.py' in headless_session_lifecycle.__file__
print(headless_session_lifecycle.__file__)
"""
    env = {**os.environ, "PYTHONPATH": "backend"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "overlays/backend/headless_session_lifecycle.py" in result.stdout
