"""Regression tests for the target backend MCP blob decoding path."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TARGET_PYTHON = REPOSITORY_ROOT / "backend/.venv/bin/python"


def run_target(script: str) -> str:
    installed = os.environ.get("HARNESS_TEST_INSTALLED") == "1"
    target_python = Path(sys.executable) if installed else TARGET_PYTHON
    assert target_python.is_file(), f"target runtime missing: {TARGET_PYTHON}"
    environment = os.environ.copy()
    if installed:
        environment.pop("PYTHONPATH", None)
    else:
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    completed = subprocess.run(
        [str(target_python), "-c", textwrap.dedent(script)],
        cwd=tempfile.gettempdir() if installed else REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout


def test_target_decodes_utf8_text_blob() -> None:
    encoded = base64.b64encode("中文 resource".encode("utf-8")).decode("ascii")
    run_target(
        f"""
        from tools.read_resource_tool import ReadResourceTool
        payload = {{"contents": [{{"blob": {encoded!r}, "mimeType": "text/plain; charset=utf-8"}}]}}
        assert ReadResourceTool._resource_text(payload) == "中文 resource"
        """
    )


def test_target_rejects_invalid_base64_and_utf8() -> None:
    invalid_utf8 = base64.b64encode(b"\xff\xfe").decode("ascii")
    run_target(
        f"""
        from langchain_core.tools import ToolException
        from tools.read_resource_tool import ReadResourceTool

        for blob in ("not-base64!", {invalid_utf8!r}):
            try:
                ReadResourceTool._resource_text({{"contents": [{{"blob": blob, "mimeType": "text/plain"}}]}})
            except ToolException as exc:
                assert "invalid encoded text" in str(exc), exc
            else:
                raise AssertionError("invalid MCP text blob was accepted")
        """
    )


def test_target_rejects_text_blob_over_limit_before_decode() -> None:
    encoded = base64.b64encode(b"a" * 10).decode("ascii")
    run_target(
        f"""
        from langchain_core.tools import ToolException
        import tools.read_resource_tool as module

        module.MAX_MCP_RESOURCE_TEXT_BYTES = 4
        try:
            module.ReadResourceTool._resource_text({{"contents": [{{"blob": {encoded!r}, "mimeType": "text/plain"}}]}})
        except ToolException as exc:
            assert "exceeds the local size limit" in str(exc), exc
        else:
            raise AssertionError("oversized MCP text blob was accepted")
        """
    )


def test_target_preserves_binary_blob_base64() -> None:
    encoded = base64.b64encode(b"\x00\xff\x10").decode("ascii")
    run_target(
        f"""
        from tools.read_resource_tool import ReadResourceTool
        payload = {{"contents": [{{"blob": {encoded!r}, "mimeType": "application/octet-stream"}}]}}
        assert ReadResourceTool._resource_text(payload) == {encoded!r}
        """
    )
