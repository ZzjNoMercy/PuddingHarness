from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
OVERLAY = ROOT / "packages/puddingharness-extraction/overlays/backend/harness/kernel_sandbox.py"
LOADER = ROOT / "packages/puddingharness-extraction/test/target_runtime_loader.py"


def test_probe_markers_keep_legacy_wire_values_without_legacy_runtime_literals():
    text = OVERLAY.read_text(encoding="utf-8")
    assert '"PUDDINGCLAW_SEATBELT_DENY_PROBE"' not in text
    assert '"PUDDINGCLAW_BWRAP_DENY_PROBE"' not in text
    assert '"PUDDINGHARNESS_SEATBELT_DENY_PROBE"' in text
    assert '"PUDDINGHARNESS_BWRAP_DENY_PROBE"' in text


def test_target_probe_failure_is_fail_closed_and_retries_after_failure_ttl(tmp_path):
    code = f"""
import harness.kernel_sandbox as module

now = [100.0]
calls = []
module.time.monotonic = lambda: now[0]
module.MacOSSeatbeltRunner._probe_cache = None
module.MacOSSeatbeltRunner._probe_once = classmethod(lambda cls: calls.append(now[0]) or (False, "deny probe failed"))

assert module.MacOSSeatbeltRunner.probe() == (False, "deny probe failed")
assert module.MacOSSeatbeltRunner.probe() == (False, "deny probe failed")
assert calls == [100.0]
now[0] += module.MacOSSeatbeltRunner._PROBE_FAILURE_TTL_SECONDS + 1
assert module.MacOSSeatbeltRunner.probe() == (False, "deny probe failed")
assert calls == [100.0, now[0]]
"""
    env = {
        **os.environ,
        "PUDDINGHARNESS_HOME": str(tmp_path / "harness"),
        "PUDDINGCLAW_HOME": str(tmp_path / "legacy"),
        "PYTHONPATH": "backend",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(LOADER)!r})\n{code}"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_target_probe_uses_target_marker_and_rejects_a_leaked_marker(tmp_path):
    code = f"""
from pathlib import Path
from types import SimpleNamespace
import sys
import harness.kernel_sandbox as module

class FakeSeatbelt(module.MacOSSeatbeltRunner):
    executable = Path(sys.executable)
    captured_marker = None
    def __init__(self, profile):
        self.profile = profile
    def execute(self, command):
        if command == "true":
            return SimpleNamespace(exit_code=0, output="")
        if command.startswith("cat "):
            type(self).captured_marker = Path(command[4:]).read_text()
            return SimpleNamespace(exit_code=1, output="permission denied")
        return SimpleNamespace(exit_code=0, output="")

module.sys.platform = "darwin"
result = FakeSeatbelt._probe_once()
assert result == (True, "macOS Seatbelt allow/deny probe passed")
assert FakeSeatbelt.captured_marker == "PUDDINGHARNESS_SEATBELT_DENY_PROBE"

class LeakingSeatbelt(FakeSeatbelt):
    def execute(self, command):
        if command.startswith("cat "):
            type(self).captured_marker = Path(command[4:]).read_text()
            return SimpleNamespace(exit_code=0, output=type(self).captured_marker)
        return super().execute(command)

assert LeakingSeatbelt._probe_once() == (False, "kernel deny probe did not enforce the filesystem boundary")
"""
    env = {
        **os.environ,
        "PUDDINGHARNESS_HOME": str(tmp_path / "harness"),
        "PUDDINGCLAW_HOME": str(tmp_path / "legacy"),
        "PYTHONPATH": "backend",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(LOADER)!r})\n{code}"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
