"""Detect and optionally install the PuddingHarness Worker CLI.

The backend is the server and must remain usable when the client CLI is not
installed.  This module therefore treats the CLI as an optional local tool:
detect it during startup, install it only under an explicit policy, and expose
the result to health/doctor callers without ever failing backend startup.
"""

from __future__ import annotations

from harness.installation_guard import inherited_guard_fds
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

CLI_COMMAND = "puddingharness"
CLI_VERSION = "0.1.19"
MIN_NODE_MAJOR = 20
INSTALL_POLICIES = frozenset({"auto", "prompt", "never"})
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

_last_status: dict[str, Any] | None = None
_install_thread_lock = threading.Lock()
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback uses the process lock.
    fcntl = None


def cli_package_dir(base_dir: Path) -> Path:
    configured = str(os.getenv("PUDDINGHARNESS_CLI_PACKAGE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (base_dir.parent / "packages" / "puddingclaw-deploy-cli").resolve()


def _parse_version(value: str) -> tuple[int, ...] | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", text)
    if not match:
        return None
    return tuple(int(item) for item in match.groups())


def _version_from_output(value: str) -> str | None:
    parsed = _parse_version(value)
    if parsed is None:
        return None
    return ".".join(str(item) for item in parsed)


def _run(
    args: Sequence[str],
    *,
    timeout: float,
    runner: CommandRunner = subprocess.run,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(
            list(args),
            pass_fds=inherited_guard_fds(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _tool_status(command: str | None, *, version_args: Sequence[str], runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return {"available": False, "path": None, "version": None}
    result = _run([command, *version_args], timeout=5.0, runner=runner)
    output = ""
    if result is not None:
        output = str(result.stdout or result.stderr or "").strip()
    version = _version_from_output(output)
    return {
        "available": result is not None and result.returncode == 0,
        "path": command,
        "version": version,
    }


def _cli_status(command: str | None, *, runner: CommandRunner) -> dict[str, Any]:
    if not command:
        return {"available": False, "path": None, "version": None}
    result = _run([command, "version", "--json"], timeout=5.0, runner=runner)
    # A successful executable is not proof of the installed product. The
    # version command is a structured identity/protocol handshake.
    output = str(result.stdout or "").strip() if result is not None else ""
    version: str | None = None
    identity_verified = False
    try:
        payload = json.loads(output)
        if isinstance(payload, dict):
            version = _version_from_output(str(payload.get("cli_version") or ""))
            identity_verified = (
                payload.get("cli") == CLI_COMMAND
                and payload.get("agent_id") == CLI_COMMAND
                and payload.get("schema_version") == "1"
                and payload.get("protocol_version") == "1"
            )
    except (TypeError, ValueError):
        pass
    return {
        "available": result is not None and result.returncode == 0 and identity_verified and version is not None,
        "path": command,
        "version": version,
        "identity_verified": identity_verified,
    }


def _requested_policy() -> tuple[str, bool]:
    configured = str(os.getenv("PUDDINGHARNESS_CLI_INSTALL_POLICY") or "").strip().lower()
    if configured:
        return (configured if configured in INSTALL_POLICIES else "never"), True

    # The repository is currently developed with the CLI package alongside the
    # backend. A packaged/open-source deployment can identify itself as
    # production and receive an interactive prompt instead of an implicit
    # global npm install.
    environment = str(
        os.getenv("PUDDINGHARNESS_ENV") or os.getenv("PUDDINGHARNESS_ENVIRONMENT") or ""
    ).strip().lower()
    if environment in {"production", "prod", "staging"}:
        return "prompt", False

    # Automated tests must never mutate the developer's global npm prefix.
    if os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return "never", False
    return "auto", False


def detect_cli_runtime(
    base_dir: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Perform a read-only CLI detection and cache the result."""

    global _last_status
    policy, policy_explicit = _requested_policy()
    result = _status(
        base_dir=base_dir,
        runner=runner,
        policy=policy,
        policy_explicit=policy_explicit,
    )
    _last_status = result
    return result


def _status(
    *,
    base_dir: Path,
    runner: CommandRunner,
    policy: str,
    policy_explicit: bool,
    install_attempted: bool = False,
    install_succeeded: bool = False,
    install_message: str | None = None,
) -> dict[str, Any]:
    command = shutil.which(CLI_COMMAND)
    node = _tool_status(shutil.which("node"), version_args=("--version",), runner=runner)
    npm = _tool_status(shutil.which("npm"), version_args=("--version",), runner=runner)
    cli = _cli_status(command, runner=runner)
    if cli["version"] and cli["version"] != CLI_VERSION:
        cli["version_mismatch"] = True
    cli["required_version"] = CLI_VERSION
    node_version = _parse_version(str(node.get("version") or ""))
    node["supported"] = bool(node_version and node_version[0] >= MIN_NODE_MAJOR)
    return {
        "command": CLI_COMMAND,
        "installed": bool(cli["available"] and cli.get("version") == CLI_VERSION),
        "identity_verified": bool(cli.get("identity_verified")),
        "path": cli.get("path"),
        "version": cli.get("version"),
        "required_version": CLI_VERSION,
        "version_mismatch": bool(cli.get("version_mismatch")),
        "node": node,
        "npm": npm,
        "package_dir": str(cli_package_dir(base_dir)),
        "install_policy": policy,
        "install_policy_explicit": policy_explicit,
        "install_attempted": install_attempted,
        "install_succeeded": install_succeeded,
        "install_message": install_message,
    }


def _prompt_for_install() -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input("PuddingHarness CLI 未安装，是否现在安装？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes", "是", "好"}


def _validate_package_dir(package_dir: Path) -> str | None:
    manifest_path = package_dir / "package.json"
    if manifest_path.is_symlink() or (package_dir / "src").is_symlink():
        return "CLI package metadata or source directory is symlinked"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"CLI package manifest is unavailable: {manifest_path}"
    if not isinstance(payload, dict):
        return "CLI package manifest is invalid"
    if payload.get("name") != "@puddingai/puddingharness":
        return "CLI package name does not match @puddingai/puddingharness"
    if payload.get("version") != CLI_VERSION:
        return f"CLI package version does not match {CLI_VERSION}"
    package_bin = payload.get("bin")
    if package_bin != {CLI_COMMAND: "src/cli.js"}:
        return "CLI package manifest does not expose the expected puddingharness binary"
    if (package_dir / "src" / "cli.js").is_symlink() or not (package_dir / "src" / "cli.js").is_file():
        return "CLI package entrypoint src/cli.js is missing"
    return None


def _acquire_file_lock(base_dir: Path):
    if fcntl is None:
        return None
    from runtime_identity.paths import PuddingClawPaths

    # An installed module lives under site-packages, never under a writable
    # checkout. Operational state belongs to the explicit Harness Home.
    lock_path = PuddingClawPaths.from_environment().data() / ".puddingharness-cli-install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return handle


def _release_file_lock(handle) -> None:
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def ensure_cli_runtime(
    base_dir: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    """Detect the CLI and optionally install it without blocking backend startup."""

    global _last_status
    policy, policy_explicit = _requested_policy()
    initial = detect_cli_runtime(base_dir, runner=runner)
    if initial["installed"]:
        _last_status = initial
        return initial

    should_install = policy == "auto" or (policy == "prompt" and _prompt_for_install())
    if not should_install:
        initial["install_message"] = (
            "CLI not installed; set PUDDINGHARNESS_CLI_INSTALL_POLICY=auto or run "
            "npm install -g ./packages/puddingclaw-deploy-cli"
        )
        _last_status = initial
        return initial

    package_dir = cli_package_dir(base_dir)
    node_supported = bool(initial.get("node", {}).get("supported"))
    npm_available = bool(initial.get("npm", {}).get("available"))
    if not package_dir.is_dir():
        initial["install_message"] = f"CLI package directory not found: {package_dir}"
        _last_status = initial
        return initial
    package_error = _validate_package_dir(package_dir)
    if package_error:
        initial["install_message"] = package_error
        _last_status = initial
        return initial
    if not node_supported or not npm_available:
        initial["install_message"] = "Node.js >= 20 and npm are required for CLI installation"
        _last_status = initial
        return initial

    if not _install_thread_lock.acquire(blocking=False):
        initial["install_message"] = "Another backend worker is already installing the CLI"
        _last_status = initial
        return initial
    try:
        file_lock = _acquire_file_lock(base_dir)
    except OSError:
        _install_thread_lock.release()
        initial["install_message"] = "CLI installation lock is unavailable"
        _last_status = initial
        return initial
    if fcntl is not None and file_lock is None:
        _install_thread_lock.release()
        initial["install_message"] = "Another backend process is already installing the CLI"
        _last_status = initial
        return initial
    try:
        install_result = _run(
            [
                str(shutil.which("npm") or "npm"),
                "install",
                "--global",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                str(package_dir),
            ],
            timeout=max(10.0, float(os.getenv("PUDDINGHARNESS_CLI_INSTALL_TIMEOUT_S", "120"))),
            runner=runner,
        )
    finally:
        _release_file_lock(file_lock)
        _install_thread_lock.release()
    succeeded = bool(install_result is not None and install_result.returncode == 0)
    message = "CLI installed" if succeeded else "CLI installation failed"
    if install_result is not None and not succeeded:
        detail = str(install_result.stderr or install_result.stdout or "").strip()
        if detail:
            message = f"{message}: {detail[-500:]}"
    final = _status(
        base_dir=base_dir,
        runner=runner,
        policy=policy,
        policy_explicit=policy_explicit,
        install_attempted=True,
        install_succeeded=succeeded,
        install_message=message,
    )
    final["install_succeeded"] = bool(succeeded and final["installed"])
    if not final["installed"] and succeeded:
        final["install_message"] = (
            "CLI installation completed, but the command is not discoverable or has an incompatible version"
        )
    _last_status = final
    return final


def current_cli_runtime_status(base_dir: Path) -> dict[str, Any]:
    """Return startup status, falling back to a read-only detection."""

    if _last_status is not None:
        return dict(_last_status)
    policy, policy_explicit = _requested_policy()
    return _status(
        base_dir=base_dir,
        runner=subprocess.run,
        policy=policy,
        policy_explicit=policy_explicit,
    )
