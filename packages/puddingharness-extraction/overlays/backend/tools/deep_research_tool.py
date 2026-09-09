"""Host-bound deep research tool.

The target harness must not discover a second, process-global tool/model graph
for research.  A host binds the already selected model, backend, tools and
middleware before exposing this tool to an agent.  An unbound tool therefore
fails closed instead of silently starting a legacy runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, ToolException
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)


RESEARCH_SUBAGENT_SYSTEM_PROMPT = """你是一个受宿主授权的研究子 agent。

你只能使用宿主传入的只读工具和 FilesystemMiddleware 的 read_file，收集
query 与 scope 所需的证据，然后给出简洁、结构化的结论。不要写文件、执行
命令、安装依赖、修改状态、调用 deep_research 自身，或自行发现其它工具。
每个事实都应尽量引用文件路径/行号、URL 或工具返回的具体证据。
"""


_SUBAGENT_TOOL_CALL_LIMIT = 10
_SUMMARY_MAX_CHARS = 500

# FilesystemMiddleware's execute/terminal-like tools are deliberately excluded.
# Names outside this set are accepted only when their host-provided metadata says
# that they are read-only.  This keeps MCP extensibility without trusting an
# arbitrary imported tool merely because it was handed to the binding.
_FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "terminal",
        "execute",
        "rawterminal",
        "write_file",
        "edit_file",
        "delete",
        "install_packages",
        "deep_research",
        "task_manager",
        "request_skill_runtime",
        "request_skill_secret",
    }
)
_EXPLICIT_READ_ONLY_NAMES = frozenset(
    {"read_file", "fetch_url", "read_resource", "read_evidence", "web_search"}
)


class DeepResearchInput(BaseModel):
    query: str = Field(description="要研究的具体问题")
    scope: str = Field(description="研究范围，可包含文件路径、目录或 URL")


class _ResearchFailure(RuntimeError):
    """A result that must never be presented as a successful research result."""


class ResearchHostBinding:
    """Immutable references supplied by the owning agent host."""

    __slots__ = (
        "model",
        "backend",
        "tools",
        "middleware_factory",
        "run_context",
        "runnable_config",
        "state_schema",
    )

    def __init__(
        self,
        *,
        model: Any,
        backend: Any,
        tools: Sequence[BaseTool],
        middleware_factory: Callable[[], Sequence[Any]],
        run_context: Any = None,
        runnable_config: Mapping[str, Any] | None = None,
        state_schema: type[Any] | None = None,
    ) -> None:
        self.model = model
        self.backend = backend
        self.tools = tuple(tools)
        self.middleware_factory = middleware_factory
        self.run_context = run_context
        self.runnable_config = dict(runnable_config or {})
        self.state_schema = state_schema


class DeepResearchTool(BaseTool):
    """A deep-research capability that is usable only after host binding."""

    name: str = "deep_research"
    description: str = (
        "Use the host-bound read-only research sub-agent for multi-file or web "
        "evidence gathering. It returns a short evidence-backed summary."
    )
    args_schema: type[BaseModel] = DeepResearchInput
    risk_level: str = "safe"
    # Let LangChain format ToolException as ToolMessage(status="error") so the
    # parent agent can explain the failed/blocked research call and continue.
    handle_tool_error: bool = True
    # Kept for the discovery factory's compatibility signature. It is never used
    # to resolve tools, models, workspaces, or provider configuration.
    base_dir: str = ""
    _host_binding: ResearchHostBinding | None = PrivateAttr(default=None)

    def _bind(self, binding: ResearchHostBinding) -> "DeepResearchTool":
        self._host_binding = binding
        return self

    def _run(
        self, query: str, scope: str, config: RunnableConfig = None, **_: Any
    ) -> str:
        try:
            host = self._require_host()
            result = self._invoke_subagent(query, scope, host, config=config)
            summary = self._validate_result(result)
            return f"[deep_research result]\n{summary[:_SUMMARY_MAX_CHARS]}"
        except GraphInterrupt:
            raise
        except ToolException:
            raise
        except Exception as exc:
            logger.warning("[deep_research] failed: %s: %s", type(exc).__name__, exc)
            raise ToolException(
                f"[deep_research error] {type(exc).__name__}: {exc}"
            ) from exc

    async def _arun(
        self, query: str, scope: str, config: RunnableConfig = None, **_: Any
    ) -> str:
        try:
            host = self._require_host()
            result = await self._ainvoke_subagent(query, scope, host, config=config)
            summary = self._validate_result(result)
            return f"[deep_research result]\n{summary[:_SUMMARY_MAX_CHARS]}"
        except GraphInterrupt:
            raise
        except ToolException:
            raise
        except Exception as exc:
            logger.warning("[deep_research] failed: %s: %s", type(exc).__name__, exc)
            raise ToolException(
                f"[deep_research error] {type(exc).__name__}: {exc}"
            ) from exc

    def _require_host(self) -> ResearchHostBinding:
        if self._host_binding is None:
            raise _ResearchFailure(
                "deep_research requires an explicit host binding "
                "(model, backend, tools, middleware_factory)"
            )
        return self._host_binding

    @staticmethod
    def _merge_config(
        host: ResearchHostBinding, config: Any = None
    ) -> dict[str, Any]:
        merged = dict(host.runnable_config)
        if config is not None:
            if isinstance(config, Mapping):
                merged.update(config)
            else:
                raise _ResearchFailure("parent RunnableConfig must be a mapping")
        # The child must remain bounded even when a parent supplies a larger
        # recursion limit.  ToolCallLimitMiddleware is the authoritative tool
        # budget; this graph limit only prevents pathological model loops.
        merged["recursion_limit"] = min(int(merged.get("recursion_limit", 100)), 100)
        return merged

    @staticmethod
    def _prompt(query: str, scope: str) -> str:
        return f"研究 query: {query}\n\n研究 scope: {scope}\n\n请只使用宿主授权的只读工具给出证据支持的结论。"

    def _make_agent(self, host: ResearchHostBinding) -> Any:
        from deepagents.middleware.filesystem import FilesystemMiddleware
        from langchain.agents import create_agent
        from langchain.agents.middleware import ToolCallLimitMiddleware

        # read_file is native to this middleware.  execute/write/edit/delete are
        # never requested, so no raw terminal capability is exposed.
        filesystem = FilesystemMiddleware(backend=host.backend, tools=["read_file"])
        host_middleware = list(host.middleware_factory())
        middleware = [filesystem, *host_middleware]
        middleware.append(
            ToolCallLimitMiddleware(
                run_limit=_SUBAGENT_TOOL_CALL_LIMIT,
                exit_behavior="error",
            )
        )
        kwargs: dict[str, Any] = {
            "model": host.model,
            "tools": list(host.tools),
            "system_prompt": RESEARCH_SUBAGENT_SYSTEM_PROMPT,
            "middleware": middleware,
        }
        if host.state_schema is not None:
            kwargs["state_schema"] = host.state_schema
        return create_agent(**kwargs)

    def _invoke_subagent(
        self,
        query: str,
        scope: str,
        host: ResearchHostBinding,
        *,
        config: Any = None,
    ) -> Mapping[str, Any]:
        from langchain_core.messages import HumanMessage

        agent = self._make_agent(host)
        return agent.invoke(
            {"messages": [HumanMessage(content=self._prompt(query, scope))]},
            config=self._merge_config(host, config),
            context=host.run_context,
        )

    async def _ainvoke_subagent(
        self,
        query: str,
        scope: str,
        host: ResearchHostBinding,
        *,
        config: Any = None,
    ) -> Mapping[str, Any]:
        from langchain_core.messages import HumanMessage

        agent = self._make_agent(host)
        return await agent.ainvoke(
            {"messages": [HumanMessage(content=self._prompt(query, scope))]},
            config=self._merge_config(host, config),
            context=host.run_context,
        )

    @classmethod
    def _validate_result(cls, result: Any) -> str:
        from langchain_core.messages import AIMessage, ToolMessage

        if not isinstance(result, Mapping):
            raise _ResearchFailure("sub-agent returned no state")
        if "__interrupt__" in result:
            raise _ResearchFailure("sub-agent interrupted before completion")
        messages = result.get("messages")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
            raise _ResearchFailure("sub-agent returned no messages")

        evidence = False
        for message in messages:
            if isinstance(message, ToolMessage) or getattr(message, "type", None) == "tool":
                status = getattr(message, "status", None)
                metadata = getattr(message, "response_metadata", {}) or {}
                additional = getattr(message, "additional_kwargs", {}) or {}
                if status == "error" or metadata.get("error") or additional.get("error"):
                    raise _ResearchFailure("sub-agent tool returned an error")
                # read_resource currently reports adapter failures as ordinary
                # strings.  Treat only its stable error marker as failure; a
                # normal evidence body containing the ❌ character remains valid.
                tool_content = getattr(message, "content", "")
                if isinstance(tool_content, list):
                    tool_content = "".join(
                        block.get("text", "") if isinstance(block, Mapping) else str(block)
                        for block in tool_content
                    )
                if str(tool_content).lstrip().startswith("❌ MCP Resource"):
                    raise _ResearchFailure("sub-agent MCP resource read failed")
                evidence = True
        if not evidence:
            raise _ResearchFailure("sub-agent produced no successful tool evidence")

        final = messages[-1]
        if not isinstance(final, AIMessage) and getattr(final, "type", None) != "ai":
            raise _ResearchFailure("sub-agent did not finish with an AI message")
        if getattr(final, "tool_calls", None):
            raise _ResearchFailure("sub-agent final AI message still requests a tool")

        metadata = getattr(final, "response_metadata", {}) or {}
        additional = getattr(final, "additional_kwargs", {}) or {}
        stop_values = [
            metadata.get(key)
            for key in ("stop_reason", "stopReason", "finish_reason", "finishReason")
        ] + [
            additional.get(key)
            for key in ("stop_reason", "stopReason", "finish_reason", "finishReason")
        ] + [
            result.get(key)
            for key in ("stop_reason", "stopReason", "finish_reason", "finishReason")
        ]
        bad_stops = {"length", "max_tokens", "error", "content_filter", "cancelled", "canceled"}
        if any(str(value).lower() in bad_stops for value in stop_values if value is not None):
            raise _ResearchFailure("sub-agent stopped without a complete answer")

        content = getattr(final, "content", "")
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, Mapping) else str(block)
                for block in content
            )
        text = str(content).strip()
        if not text:
            raise _ResearchFailure("sub-agent returned an empty final answer")
        return text


def _tool_metadata(tool: Any) -> Mapping[str, Any]:
    metadata = getattr(tool, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _is_read_only_tool(tool: Any) -> bool:
    name = str(getattr(tool, "name", ""))
    metadata = _tool_metadata(tool)
    if metadata.get("destructiveHint") is True or metadata.get("destructive_hint") is True:
        return False
    if name in _EXPLICIT_READ_ONLY_NAMES:
        return True
    return (
        metadata.get("readOnlyHint") is True
        or metadata.get("read_only_hint") is True
    )


def bind_research_tool(
    tool: DeepResearchTool,
    *,
    model: Any,
    backend: Any,
    tools: Sequence[BaseTool],
    middleware_factory: Callable[[], Sequence[Any]],
    run_context: Any = None,
    runnable_config: Mapping[str, Any] | None = None,
    state_schema: type[Any] | None = None,
) -> DeepResearchTool:
    """Bind research to the owning agent's already selected runtime objects."""

    if not isinstance(tool, DeepResearchTool):
        raise TypeError("tool must be a DeepResearchTool")
    if model is None or backend is None:
        raise ValueError("model and backend are required for research binding")
    if not callable(middleware_factory):
        raise TypeError("middleware_factory must be callable")

    bound: list[BaseTool] = []
    seen: set[str] = set()
    for candidate in tools:
        name = str(getattr(candidate, "name", ""))
        if not name or name in seen:
            continue
        if name in _FORBIDDEN_TOOL_NAMES or not _is_read_only_tool(candidate):
            raise ValueError(f"research tool is not explicitly read-only: {name or '<unnamed>'}")
        # FilesystemMiddleware owns the native read_file tool.  Passing another
        # implementation would create an ambiguous graph tool name.
        if name != "read_file":
            bound.append(candidate)
        seen.add(name)

    # Factory discovery may cache the returned BaseTool.  Never mutate that
    # shared instance: every host/run receives its own binding snapshot.
    bound_tool = tool.model_copy(deep=False)
    return bound_tool._bind(
        ResearchHostBinding(
            model=model,
            backend=backend,
            tools=bound,
            middleware_factory=middleware_factory,
            run_context=run_context,
            runnable_config=runnable_config,
            state_schema=state_schema,
        )
    )


def create_deep_research_tool(base_dir: Path) -> DeepResearchTool:
    """Factory entry point; discovery does not imply runtime binding."""

    tool = DeepResearchTool()
    tool.base_dir = str(base_dir)
    return tool
