"""Unit tests for the future PuddingHarness tools overlay.

These tests load the overlay as an isolated package and create temporary fake
modules.  They therefore exercise the discovery boundary without importing
the legacy ``backend/tools`` package or constructing production tools.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from langchain_core.tools import BaseTool


OVERLAY = Path(__file__).parents[1] / "overlays/backend/tools/__init__.py"
TEST_MODULE = __name__


def _load_overlay(tmp_path: Path, monkeypatch):
    package_name = f"fake_harness_tools_{tmp_path.name.replace('-', '_')}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    spec = importlib.util.spec_from_file_location(
        package_name,
        OVERLAY,
        submodule_search_locations=[str(package_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, module)
    spec.loader.exec_module(module)
    return module, package_dir


class _FakeTool(BaseTool):
    name: str = "fake_generic"
    description: str = "fake generic tool"

    def _run(self) -> str:
        return "ok"


class _BoundTool(BaseTool):
    name: str = "bound_generic"
    description: str = "fake tool with an observable base directory"
    base_dir: str = ""

    def _run(self) -> str:
        return self.base_dir


def _write_module(package_dir: Path, module_name: str, source: str) -> None:
    (package_dir / f"{module_name}.py").write_text(source, encoding="utf-8")


def test_factory_uses_explicit_allowlist_and_keeps_core_compatibility(tmp_path, monkeypatch) -> None:
    overlay, package_dir = _load_overlay(tmp_path, monkeypatch)
    _write_module(
        package_dir,
        "read_file_tool",
        f"from {TEST_MODULE} import _FakeTool\n"
        "def create_read_file_tool(base_dir):\n"
        "    return _FakeTool()\n",
    )
    _write_module(
        package_dir,
        "zombie_tool",
        "raise AssertionError('unreviewed module was imported')\n",
    )

    tools = overlay.get_all_tools(tmp_path)

    assert [tool.name for tool in tools] == ["fake_generic"]
    assert "zombie_tool" not in overlay.GENERIC_TOOL_FACTORIES


def test_business_modules_and_unknown_categories_are_not_discovered(tmp_path, monkeypatch) -> None:
    overlay, package_dir = _load_overlay(tmp_path, monkeypatch)
    marker = tmp_path / "business-imported"
    _write_module(
        package_dir,
        "search_knowledge_tool",
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def create_search_knowledge_tool():\n"
        "    raise AssertionError('business module was imported')\n",
    )
    _write_module(
        package_dir,
        "read_file_tool",
        f"from {TEST_MODULE} import _FakeTool\n"
        "def create_read_file_tool(base_dir):\n"
        "    return _FakeTool()\n",
    )

    tools = overlay.get_tools_by_categories(tmp_path, {"knowledge", "unreviewed"})

    assert [tool.name for tool in tools] == ["fake_generic"]
    assert not marker.exists()
    assert "knowledge" not in overlay.TOOL_CATEGORIES
    assert "analytics" not in overlay.TOOL_CATEGORIES


def test_imported_create_function_is_rejected(tmp_path, monkeypatch) -> None:
    overlay, package_dir = _load_overlay(tmp_path, monkeypatch)
    _write_module(
        package_dir,
        "foreign_factory",
        f"from {TEST_MODULE} import _FakeTool\n"
        "def create_foreign_tool():\n"
        "    return _FakeTool()\n",
    )
    _write_module(
        package_dir,
        "read_file_tool",
        "from .foreign_factory import create_foreign_tool as create_read_file_tool\n",
    )

    assert overlay.get_all_tools(tmp_path) == []


def test_unmounted_execute_skill_factory_is_not_in_target_allowlist(tmp_path, monkeypatch) -> None:
    overlay, package_dir = _load_overlay(tmp_path, monkeypatch)
    _write_module(
        package_dir,
        "skill_inspection_tool",
        f"from {TEST_MODULE} import _FakeTool\n"
        "def create_skill_inspection_tool(base_dir=None):\n"
        "    return _FakeTool()\n",
    )
    # DeepAgents has no runtime attach point for execute_skill; it remains in
    # the legacy source tree but is excluded from the target factory.
    assert "execute_skill_tool" not in overlay.GENERIC_TOOL_FACTORIES
    assert [tool.name for tool in overlay.get_tools_by_categories(tmp_path, {"skill"})] == ["fake_generic"]


def test_optional_base_dir_parameter_is_bound_to_the_callers_workspace(tmp_path, monkeypatch) -> None:
    overlay, package_dir = _load_overlay(tmp_path, monkeypatch)
    _write_module(
        package_dir,
        "update_memory_tool",
        f"from {TEST_MODULE} import _BoundTool\n"
        "def create_update_memory_tool(_base_dir=None):\n"
        "    return _BoundTool(base_dir=str(_base_dir))\n",
    )

    tools = overlay.get_tools_by_categories(tmp_path, {"core"})

    assert [tool.name for tool in tools] == ["bound_generic"]
    assert tools[0].base_dir == str(tmp_path)
