"""Target-only delegation control checks with the real target protocol models."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

ROOT = Path(__file__).parents[1]
OVERLAY_ROOT = ROOT / "overlays/backend"


class _NoPersistence:
    is_initialized = False


def _load_target(monkeypatch):
    """Load target models and middleware, never the legacy middleware module."""
    legacy_spec = importlib.util.spec_from_file_location(
        "harness.legacy_artifacts", OVERLAY_ROOT / "harness/legacy_artifacts.py"
    )
    assert legacy_spec and legacy_spec.loader
    legacy = importlib.util.module_from_spec(legacy_spec)
    monkeypatch.setitem(sys.modules, "harness.legacy_artifacts", legacy)
    legacy_spec.loader.exec_module(legacy)

    models_spec = importlib.util.spec_from_file_location(
        "harness.models", OVERLAY_ROOT / "harness/models.py"
    )
    assert models_spec and models_spec.loader
    models = importlib.util.module_from_spec(models_spec)
    monkeypatch.setitem(sys.modules, "harness.models", models)
    models_spec.loader.exec_module(models)

    manager_module = ModuleType("graph.session_manager")
    manager_module.session_manager = _NoPersistence()
    monkeypatch.setitem(sys.modules, "graph.session_manager", manager_module)

    middleware_name = "graph.middlewares.delegation_control"
    middleware_spec = importlib.util.spec_from_file_location(
        middleware_name, OVERLAY_ROOT / "graph/middlewares/delegation_control.py"
    )
    assert middleware_spec and middleware_spec.loader
    middleware = importlib.util.module_from_spec(middleware_spec)
    monkeypatch.setitem(sys.modules, middleware_name, middleware)
    middleware_spec.loader.exec_module(middleware)
    return middleware, models


def _request(events: list[dict]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "task",
            "id": "task-call-target",
            "args": {"description": "review workspace changes", "subagent_type": "general-purpose"},
        },
        tool=None,
        state={"todos": [{"id": "todo-1", "status": "in_progress"}]},
        runtime=SimpleNamespace(context={}, stream_writer=events.append),
    )


@pytest.mark.asyncio
async def test_generic_delegation_uses_target_models_and_returns_envelope(monkeypatch):
    middleware_module, models = _load_target(monkeypatch)
    assert models.DelegationContract.model_fields["expected_output_schema"].default == "DelegationResultEnvelope/v1"
    events: list[dict] = []
    middleware = middleware_module.DelegationControlMiddleware()

    async def handler(_request):
        return ToolMessage(content="workspace review complete", tool_call_id="task-call-target")

    result = await middleware.awrap_tool_call(_request(events), handler)
    assert isinstance(result, ToolMessage)
    envelope = models.DelegationResultEnvelope.model_validate_json(str(result.content))
    assert envelope.status == "completed"
    assert envelope.summary == "workspace review complete"
    assert "sql_generation_ids" not in envelope.model_dump()
    assert envelope.validation_receipt_ids == []
    assert {event["type"] for event in events} >= {"subagent_started", "context_mounted", "subagent_completed"}


@pytest.mark.asyncio
async def test_graph_interrupt_is_propagated_and_emits_waiting_event(monkeypatch):
    middleware_module, _ = _load_target(monkeypatch)
    events: list[dict] = []
    middleware = middleware_module.DelegationControlMiddleware()

    async def handler(_request):
        raise GraphInterrupt(
            [Interrupt(value={"type": "permission_request"}, id="interrupt-target")]
        )

    with pytest.raises(GraphInterrupt):
        await middleware.awrap_tool_call(_request(events), handler)
    assert any(event["type"] == "subagent_waiting_for_permission" for event in events)


@pytest.mark.asyncio
async def test_timeout_and_nested_model_tool_budgets_remain_generic(monkeypatch):
    middleware_module, models = _load_target(monkeypatch)
    middleware = middleware_module.DelegationControlMiddleware(
        limits=models.DelegationLimits(wall_clock_seconds=1, idle_seconds=1, model_calls=1, tool_calls=1)
    )
    events: list[dict] = []
    request = _request(events)
    contract = middleware._contract(request)
    contract = contract.model_copy(
        update={"limits": models.DelegationLimits(wall_clock_seconds=1, idle_seconds=1, model_calls=1, tool_calls=1)}
    )
    active = middleware_module._ActiveDelegation(contract=contract, last_activity_at=0.0)

    async def never_finishes():
        await asyncio.Event().wait()

    with pytest.raises(middleware_module._DelegationLimitExceeded, match="idle_limit"):
        await middleware_module.DelegationControlMiddleware._run_bounded(never_finishes(), active)

    progress = middleware_module.SubagentProgressMiddleware()
    token = middleware_module._ACTIVE_DELEGATION.set(active)
    try:
        model_request = SimpleNamespace(runtime=SimpleNamespace(stream_writer=None))

        async def model_handler(_request):
            return SimpleNamespace()

        await progress.awrap_model_call(model_request, model_handler)
        with pytest.raises(middleware_module._DelegationLimitExceeded, match="model_call_limit"):
            await progress.awrap_model_call(model_request, model_handler)
    finally:
        middleware_module._ACTIVE_DELEGATION.reset(token)

    plain = middleware._derive_limits(objective="ordinary work", todo_count=1)
    business_word = middleware._derive_limits(objective="database sql 查询", todo_count=1)
    assert plain == business_word


def test_overlay_has_no_business_selector_or_sql_registry_path():
    text = (OVERLAY_ROOT / "graph/middlewares/delegation_control.py").read_text()
    for forbidden in (
        "analytics_model_id",
        "selected_analytics_model",
        "database_sql_revision_resume",
        "DatabaseEvidenceBatch",
        "sql-gen-",
        "sql-validation-",
    ):
        assert forbidden not in text


def test_generic_template_work_keeps_proportional_budget(monkeypatch):
    module, _models = _load_target(monkeypatch)
    middleware = module.DelegationControlMiddleware()
    ordinary = middleware._derive_limits(objective="ordinary work", todo_count=1)
    artifact = middleware._derive_limits(objective="fill template slots", todo_count=1)
    assert artifact.tool_calls > ordinary.tool_calls
    assert artifact.wall_clock_seconds > ordinary.wall_clock_seconds
