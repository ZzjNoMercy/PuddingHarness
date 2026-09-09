"""Host-bound deep-research contract tests; no provider or legacy runtime."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.errors import GraphInterrupt


OVERLAY = Path(__file__).parents[1] / "overlays/backend/tools/deep_research_tool.py"


def load_overlay():
    spec = importlib.util.spec_from_file_location("harness_research_overlay", OVERLAY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_tool(name="read_file", metadata=None):
    return SimpleNamespace(name=name, metadata=metadata or {})


def setup_graph(monkeypatch, result=None, error=None):
    import deepagents.middleware.filesystem as filesystem_module
    import langchain.agents as agents_module

    captured = {}

    class FakeFilesystem:
        def __init__(self, **kwargs):
            captured["filesystem"] = kwargs

    class FakeAgent:
        def invoke(self, *args, **kwargs):
            captured["invoke"] = (args, kwargs)
            if error:
                raise error
            return result

        async def ainvoke(self, *args, **kwargs):
            captured["ainvoke"] = (args, kwargs)
            if error:
                raise error
            return result

    def create_agent(**kwargs):
        captured["create_agent"] = kwargs
        return FakeAgent()

    monkeypatch.setattr(filesystem_module, "FilesystemMiddleware", FakeFilesystem)
    monkeypatch.setattr(agents_module, "create_agent", create_agent)
    return captured


def bind(module, tool, **overrides):
    values = {
        "model": object(),
        "backend": object(),
        "tools": [read_tool("read_file"), read_tool("fetch_url")],
        "middleware_factory": lambda: ["parent-middleware"],
        "run_context": {"run_id": "r1"},
        "state_schema": type("ParentState", (), {}),
    }
    values.update(overrides)
    return module.bind_research_tool(tool, **values), values


def test_unbound_fails_closed_and_binding_preserves_host_refs():
    module = load_overlay()
    tool = module.create_deep_research_tool(Path("/tmp"))
    with pytest.raises(ToolException, match="host binding"):
        tool._run("q", "s")

    bound, values = bind(module, tool)
    host = bound._host_binding
    assert host.model is values["model"]
    assert host.backend is values["backend"]
    assert host.run_context == {"run_id": "r1"}
    assert host.state_schema is values["state_schema"]


def test_binding_rejects_write_terminal_and_unannotated_tools():
    module = load_overlay()
    tool = module.create_deep_research_tool(Path("/tmp"))
    for bad in ("terminal", "execute", "write_file"):
        with pytest.raises(ValueError, match="not explicitly read-only"):
            bind(module, tool, tools=[read_tool(bad)])
    with pytest.raises(ValueError, match="not explicitly read-only"):
        bind(module, tool, tools=[read_tool("custom_mcp")])
    with pytest.raises(ValueError, match="not explicitly read-only"):
        bind(module, tool, tools=[read_tool("custom_mcp", {"readOnlyHint": "false"})])
    with pytest.raises(ValueError, match="not explicitly read-only"):
        bind(module, tool, tools=[read_tool("custom_mcp", {"readOnlyHint": True, "destructiveHint": True})])

    bound, _ = bind(
        module,
        tool,
        tools=[read_tool("custom_mcp", {"readOnlyHint": True})],
    )
    assert [item.name for item in bound._host_binding.tools] == ["custom_mcp"]


def test_binding_isolated_from_factory_cached_tool():
    module = load_overlay()
    tool = module.create_deep_research_tool(Path("/tmp"))
    first, first_values = bind(module, tool, run_context={"workspace": "one"})
    second, second_values = bind(module, tool, run_context={"workspace": "two"})
    assert first is not second
    assert first is not tool and second is not tool
    assert first._host_binding.run_context == {"workspace": "one"}
    assert second._host_binding.run_context == {"workspace": "two"}
    assert first._host_binding.model is first_values["model"]
    assert second._host_binding.model is second_values["model"]
    with pytest.raises(ToolException, match="host binding"):
        tool._run("q", "s")


def test_create_agent_uses_same_model_explicit_tools_filesystem_and_hard_limit(monkeypatch):
    module = load_overlay()
    result = {
        "messages": [
            ToolMessage(content="file evidence", tool_call_id="1"),
            AIMessage(content="answer", response_metadata={"stop_reason": "stop"}),
        ]
    }
    captured = setup_graph(monkeypatch, result=result)
    tool = module.create_deep_research_tool(Path("/tmp"))
    bound, values = bind(module, tool, tools=[read_tool("read_file"), read_tool("fetch_url")])

    assert bound._run("q", "s") == "[deep_research result]\nanswer"
    create_kwargs = captured["create_agent"]
    assert create_kwargs["model"] is values["model"]
    assert [item.name for item in create_kwargs["tools"]] == ["fetch_url"]
    assert captured["filesystem"] == {"backend": values["backend"], "tools": ["read_file"]}
    assert create_kwargs["state_schema"] is values["state_schema"]
    assert create_kwargs["middleware"][1] == "parent-middleware"
    limiter = create_kwargs["middleware"][-1]
    assert limiter.run_limit == 10
    assert limiter.exit_behavior == "error"
    assert "terminal" not in [getattr(item, "name", "") for item in create_kwargs["tools"]]
    invoke_args, invoke_kwargs = captured["invoke"]
    assert invoke_kwargs["context"] == {"run_id": "r1"}


def test_real_child_graph_stops_on_eleventh_tool_call(tmp_path):
    from deepagents.backends import FilesystemBackend
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    class RepeatingModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    (tmp_path / "evidence.txt").write_text("evidence", encoding="utf-8")
    model = RepeatingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/evidence.txt"},
                        "id": str(index),
                    }
                ],
            )
            for index in range(12)
        ]
    )
    module = load_overlay()
    tool = module.create_deep_research_tool(tmp_path)
    tool, _ = bind(
        module,
        tool,
        model=model,
        backend=FilesystemBackend(tmp_path),
        tools=[],
        middleware_factory=lambda: [],
    )
    with pytest.raises(ToolException, match="Tool call limit reached"):
        tool._run("repeat", "/evidence.txt")
    assert model.i == 11


@pytest.mark.asyncio
async def test_async_path_forwards_parent_config_and_context(monkeypatch):
    module = load_overlay()
    result = {
        "messages": [
            ToolMessage(content="evidence", tool_call_id="1"),
            AIMessage(content="async answer", response_metadata={"finish_reason": "stop"}),
        ]
    }
    captured = setup_graph(monkeypatch, result=result)
    tool = module.create_deep_research_tool(Path("/tmp"))
    bound, _ = bind(module, tool, runnable_config={"tags": ["host"]})
    output = await bound._arun(
        "q", "s", config={"callbacks": ["cb"], "metadata": {"x": 1}}
    )
    assert output == "[deep_research result]\nasync answer"
    _, invoke_kwargs = captured["ainvoke"]
    assert invoke_kwargs["config"]["tags"] == ["host"]
    assert invoke_kwargs["config"]["callbacks"] == ["cb"]
    assert invoke_kwargs["config"]["metadata"] == {"x": 1}
    assert invoke_kwargs["context"] == {"run_id": "r1"}


@pytest.mark.asyncio
async def test_tool_call_formats_failure_as_error_tool_message():
    module = load_overlay()
    tool = module.create_deep_research_tool(Path("/tmp"))
    message = await tool.ainvoke(
        {
            "name": "deep_research",
            "args": {"query": "q", "scope": "s"},
            "id": "research-error",
            "type": "tool_call",
        }
    )
    assert isinstance(message, ToolMessage)
    assert message.status == "error"
    assert "host binding" in str(message.content)


def test_mcp_resource_error_string_is_not_successful_evidence():
    module = load_overlay()
    failed = {
        "messages": [
            ToolMessage(
                content="❌ MCP Resource read failed: timeout",
                tool_call_id="resource-1",
                name="read_resource",
            ),
            AIMessage(content="The resource was read successfully."),
        ]
    }
    with pytest.raises(module._ResearchFailure, match="MCP resource read failed"):
        module.DeepResearchTool._validate_result(failed)

    ordinary_marker = {
        "messages": [
            ToolMessage(
                content="Evidence text includes ❌ as a quoted character.",
                tool_call_id="file-1",
                name="read_file",
            ),
            AIMessage(content="The evidence is complete."),
        ]
    }
    assert module.DeepResearchTool._validate_result(ordinary_marker) == "The evidence is complete."


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"messages": []},
        {"messages": [AIMessage(content="no tool evidence")]},
        {"messages": [ToolMessage(content="ok", tool_call_id="1")]},
        {
            "messages": [
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content=""),
            ]
        },
        {
            "messages": [
                ToolMessage(content="bad", tool_call_id="1", status="error"),
                AIMessage(content="answer"),
            ]
        },
        {
            "messages": [
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="truncated", response_metadata={"stop_reason": "length"}),
            ]
        },
        {
            "stopReason": "error",
            "messages": [
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="failed final answer"),
            ]
        },
        {
            "messages": [
                ToolMessage(content="ok", tool_call_id="1"),
                AIMessage(content="still calling", tool_calls=[{"name": "read_file", "args": {}, "id": "2"}]),
            ]
        },
    ],
)
def test_invalid_completion_never_uses_success_prefix(monkeypatch, result):
    module = load_overlay()
    captured = setup_graph(monkeypatch, result=result)
    tool = module.create_deep_research_tool(Path("/tmp"))
    tool, _ = bind(module, tool)
    with pytest.raises(ToolException, match="deep_research error"):
        tool._run("q", "s")
    assert "create_agent" in captured
    assert "invoke" in captured


def test_interrupt_and_model_exception_are_errors(monkeypatch):
    module = load_overlay()
    captured = setup_graph(monkeypatch, result={"__interrupt__": []})
    tool = module.create_deep_research_tool(Path("/tmp"))
    tool, _ = bind(module, tool)
    with pytest.raises(ToolException, match="deep_research error"):
        tool._run("q", "s")

    captured = setup_graph(monkeypatch, error=RuntimeError("model failed"))
    with pytest.raises(ToolException, match="deep_research error"):
        tool._run("q", "s")

    setup_graph(monkeypatch, error=GraphInterrupt("approval"))
    with pytest.raises(GraphInterrupt):
        tool._run("q", "s")
