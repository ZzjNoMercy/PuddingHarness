"""Regression checks for the cleaned target backend, without source overlays."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"


def _run_target_probe() -> dict[str, object]:
    code = r'''
import json
from pydantic import ValidationError
from api.config_api import DatabaseConnectionRequest, SettingsUpdateRequest
from graph.middlewares import harness_todos

result = {
    "settings_fields": sorted(SettingsUpdateRequest.model_fields),
    "database_defaults": DatabaseConnectionRequest().model_dump(),
    "analytics_settings_rejected": False,
    "analytics_completion": harness_todos._control_plane_completion_contract(
        "查询数据库指标",
        {"verification_contract": {"verification_packs": ["analytics"]}},
    ),
    "code_completion": harness_todos._control_plane_completion_contract(
        "运行 pytest 测试",
        {"verification_contract": {"verification_packs": ["code"]}},
    ),
    "artifact_completion": harness_todos._control_plane_completion_contract(
        "生成报告文件",
        {"verification_contract": {"verification_packs": ["artifact"]}},
    ),
    "query_tools": sorted(harness_todos._QUERY_RESULT_TOOLS),
}
try:
    SettingsUpdateRequest.model_validate({"analytics": {}})
except ValidationError:
    result["analytics_settings_rejected"] = True
print(json.dumps(result, sort_keys=True))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(BACKEND)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_target_config_rejects_business_settings_and_preserves_generic_todos() -> None:
    result = _run_target_probe()

    assert result["settings_fields"] == ["compression", "database", "harness", "subagents"]
    assert result["analytics_settings_rejected"] is True
    assert result["database_defaults"]["database"] == "puddingharness"
    assert result["database_defaults"]["username"] == "puddingharness"
    assert result["analytics_completion"] is None
    assert result["code_completion"] == "validation_receipt"
    assert result["artifact_completion"] == "artifact_receipt"
    assert result["query_tools"] == ["execute", "python_repl"]
