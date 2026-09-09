"""PuddingHarness extraction overlay for the local tool factory.

This file is copied to ``backend/tools/__init__.py`` in the future target
repository.  It deliberately does not scan the package directory.  A module
and its factory must be reviewed and added to ``GENERIC_TOOL_FACTORIES``
before it can be imported by this factory.

The legacy PuddingClaw factory remains unchanged.  This overlay is a target
repository preparation artifact, not a repository extraction or release.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Final

from langchain_core.tools import BaseTool


# Explicit module -> factory registration.  The registry is the discovery
# boundary: files that happen to be present in the package are not tools.
# Keep the values explicit so an imported ``create_*`` helper can never be
# selected accidentally.
GENERIC_TOOL_FACTORIES: Final[dict[str, str]] = {
    "browser_tool": "create_browser_tool",
    "create_skill_version_tool": "create_skill_version_tool",
    "deep_research_tool": "create_deep_research_tool",
    "fetch_url_tool": "create_fetch_url_tool",
    "python_repl_tool": "create_python_repl_tool",
    "read_evidence_tool": "create_read_evidence_tool",
    "read_external_file_tool": "create_read_external_file_tool",
    "read_file_tool": "create_read_file_tool",
    "read_resource_tool": "create_read_resource_tool",
    "request_skill_runtime_tool": "create_request_skill_runtime_tool",
    "request_skill_secret_tool": "create_request_skill_secret_tool",
    "request_user_input_tool": "create_request_user_input_tool",
    "skill_inspection_tool": "create_skill_inspection_tool",
    "skill_management_tool": "create_skill_management_tools",
    "task_manager_tool": "create_task_manager_tool",
    "terminal_tool": "create_terminal_tool",
    "update_goal_tool": "create_update_goal_tool",
    "update_memory_tool": "create_update_memory_tool",
    "web_search_tool": "create_web_search_tool",
    "write_file_tool": "create_write_file_tool",
}


# Category membership is also explicit.  Unknown categories are ignored;
# this keeps the compatibility API while preventing a caller from turning a
# business label into package-wide discovery.
TOOL_CATEGORIES: Final[dict[str, tuple[str, ...]]] = {
    "core": (
        "read_evidence_tool",
        "read_external_file_tool",
        "read_file_tool",
        "read_resource_tool",
        "task_manager_tool",
        "terminal_tool",
        "update_goal_tool",
        "update_memory_tool",
        "write_file_tool",
    ),
    "skill": (
        "create_skill_version_tool",
        "request_skill_runtime_tool",
        "request_skill_secret_tool",
        "request_user_input_tool",
        "skill_inspection_tool",
        "skill_management_tool",
    ),
    "web": (
        "browser_tool",
        "fetch_url_tool",
        "web_search_tool",
    ),
    "research": ("deep_research_tool",),
    "code_exec": ("python_repl_tool",),
}


# Module-level instances retain the legacy cache behavior.  The base directory
# is part of the key because file and skill tools bind to it at construction.
_tool_instance_cache: dict[tuple[str, str], list[BaseTool]] = {}


def _factory_parameters(factory: object) -> tuple[list[inspect.Parameter], list[inspect.Parameter]]:
    """Return (all parameters, required parameters) for a registered factory."""

    parameters = list(inspect.signature(factory).parameters.values())
    required = [
        parameter
        for parameter in parameters
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    return parameters, required


def _invoke_registered_factory(factory: object, base_dir: Path) -> object:
    """Call only the small set of constructor shapes used by this overlay.

    Factories that need runtime-injected dependencies are intentionally skipped
    here and remain attached by the runtime that owns those dependencies.  In
    particular, this function never guesses arguments for an arbitrary
    ``create_*`` function.
    """

    parameters, required = _factory_parameters(factory)
    base_parameter = next(
        (
            parameter
            for parameter in parameters
            if parameter.name in {"base_dir", "dir", "path", "_base_dir"}
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ),
        None,
    )
    unsupported_required = [parameter for parameter in required if parameter is not base_parameter]
    if not unsupported_required and base_parameter is not None:
        if base_parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            return factory(base_dir)  # type: ignore[operator]
        return factory(**{base_parameter.name: base_dir})  # type: ignore[operator]

    if not required:
        return factory()  # type: ignore[operator]

    # The registered function may have additional optional parameters, but a
    # required injected dependency (runner, paths, session identity, ...) is
    # not available at this compatibility boundary.
    raise TypeError(
        f"registered factory requires unsupported parameters: "
        f"{[parameter.name for parameter in parameters]}"
    )


def _load_tool_module(module_name: str, base_dir: Path) -> list[BaseTool]:
    """Load one reviewed module and invoke its reviewed factory, if possible."""

    factory_name = GENERIC_TOOL_FACTORIES.get(module_name)
    if factory_name is None:
        return []

    cache_key = (module_name, str(base_dir))
    if cache_key in _tool_instance_cache:
        return _tool_instance_cache[cache_key]

    try:
        module = importlib.import_module(f".{module_name}", package=__package__)
        factory = getattr(module, factory_name, None)
        if not callable(factory) or getattr(factory, "__module__", None) != module.__name__:
            print(f"[tools] Warning: rejected unowned factory {module_name}.{factory_name}")
            return []

        result = _invoke_registered_factory(factory, base_dir)
        tools = result if isinstance(result, list) else [result]
        _tool_instance_cache[cache_key] = tools
        return tools
    except Exception as exc:
        print(f"[tools] Warning: failed to load {module_name}: {exc}")
        return []


def _report_loaded(tools: list[BaseTool], *, prefix: str) -> None:
    safe = sum(1 for tool in tools if getattr(tool, "risk_level", "safe") == "safe")
    moderate = sum(1 for tool in tools if getattr(tool, "risk_level", "") == "moderate")
    dangerous = sum(1 for tool in tools if getattr(tool, "risk_level", "") == "dangerous")
    print(f"[tools] {prefix}: {len(tools)} tools (safe={safe}, moderate={moderate}, dangerous={dangerous})")


def get_all_tools(base_dir: Path) -> list[BaseTool]:
    """Return all reviewed generic tools in deterministic module order."""

    tools: list[BaseTool] = []
    for module_name in sorted(GENERIC_TOOL_FACTORIES):
        tools.extend(_load_tool_module(module_name, base_dir))
    _report_loaded(tools, prefix="Loaded")
    return tools


def get_tools_by_categories(base_dir: Path, categories: set[str]) -> list[BaseTool]:
    """Load reviewed tools for categories, always including ``core``.

    The function keeps the legacy call signature.  Category names are looked
    up only in ``TOOL_CATEGORIES``; there is no fallback to filesystem scans.
    """

    active_categories = set(categories) | {"core"}
    module_names: set[str] = set()
    for category in active_categories:
        module_names.update(TOOL_CATEGORIES.get(category, ()))

    tools: list[BaseTool] = []
    for module_name in sorted(module_names):
        tools.extend(_load_tool_module(module_name, base_dir))
    _report_loaded(tools, prefix=f"Dynamic load categories={sorted(active_categories)}")
    return tools


__all__ = [
    "GENERIC_TOOL_FACTORIES",
    "TOOL_CATEGORIES",
    "get_all_tools",
    "get_tools_by_categories",
]
