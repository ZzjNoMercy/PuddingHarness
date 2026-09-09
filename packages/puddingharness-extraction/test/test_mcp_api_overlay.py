"""Behavior checks for the target-only generic MCP API overlay."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


OVERLAY = Path(__file__).parents[1] / "overlays/backend/api/mcp.py"


def _load_overlay(tmp_path: Path, monkeypatch):
    module_name = f"fake_harness_mcp_api_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, OVERLAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_mcp_config_accepts_user_named_gbrain_and_preserves_opaque_credentials(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    raw = {
        "enabled": ["gbrain"],
        "servers": {
            "gbrain": {
                "transport": "streamable-http",
                "url": "https://mcp.example.test",
                "headers": {"Authorization": "${MCP_TOKEN}"},
                "env": {"MCP_MODE": "vault://users/local/credentials/mode"},
            }
        },
    }

    validated = module._validate_mcp_config(raw, {})

    assert validated == raw
    safe = module._safe_mcp_config(
        {
            "enabled": ["gbrain"],
            "servers": {
                "gbrain": {
                    "transport": "streamable-http",
                    "headers": {"Authorization": "literal-secret"},
                    "env": {"MCP_MODE": "${MCP_MODE}"},
                }
            },
        }
    )
    assert safe["servers"]["gbrain"]["headers"]["Authorization"] == "***"
    assert safe["servers"]["gbrain"]["env"]["MCP_MODE"] == "${MCP_MODE}"


def test_mcp_config_has_no_product_specific_gbrain_management(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)

    assert not hasattr(module, "_MCP_DISPLAY_NAMES")
    source = OVERLAY.read_text(encoding="utf-8")
    assert "gbrain_runtime_status" not in source
    assert "knowledge.gbrain" not in source

    try:
        module._validate_mcp_config({"enabled": ["gbrain", "gbrain"]}, {})
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate enabled names must remain invalid")
