#!/usr/bin/env python3
"""Stage an independent PuddingHarness backend from its repository source.

The source checkout is an input only.  The output is a flat Python application
root (``app.py``, ``api/``, ``graph/`` ...) and never imports files from the
checkout at runtime.  An output directory must be absent or empty so a stale
artifact cannot silently survive a re-run.

Static audit findings are recorded in the manifest because the extraction is
being performed in phases.  ``--require-clean-audit`` turns those findings
into a hard packaging gate; without it, the resulting artifact is explicitly
marked non-releaseable in ``.stage-manifest.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = PACKAGE_ROOT / "audit.py"
MANIFEST_NAME = ".stage-manifest.json"
SKILL_PREFIX = "backend/skills/"
_CACHE_PARTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "cache", "node_modules", "dist", "build", "coverage"})
_SECRET_PARTS = frozenset({"secrets", "credentials", "private", ".git"})
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".mobileprovision", ".provisionprofile"})
EXCLUDED_SKILL_NAMES = frozenset(
    {
        "knowledge-search",
        "llm-wiki",
        "semantic-steward",
        "database-analysis",
        "table-analysis",
        "build-semantic-dimension",
        "build-logical-dataset",
        "sql-guardrail-designer",
    }
)

# These are product-neutral runtime resources.  Knowledge/Analytics schema
# packs, platform migration fixtures, and source tests are intentionally not
# copied.  protocol-2.0 is target-only and therefore has no source fallback.
RESOURCE_FILES = (
    "backend/evaluation/schemas/protocol-1.0.json",
    "backend/evaluation/schemas/protocol-2.0.json",
    "backend/evaluation/examples/general-agent-core.bundle.json",
    "backend/harness/docker/Dockerfile",
    "backend/harness/docker/validate-html-report-e2e.mjs",
    "backend/prompts/tool_guides/managed-lark-autonomy.md",
    "backend/prompts/tool_guides/webbridge-browser.md",
)

TARGET_IDENTITY = """# IDENTITY — PuddingHarness

- **Name**: PuddingHarness
- **Version**: 0.1.0
- **Role**: a local Agent, MCP, evaluation, and runtime service
- **Language**: follow the user's language
"""


def _load_audit():
    spec = importlib.util.spec_from_file_location("puddingharness_extraction_audit", AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load audit module: {AUDIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(repo: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def _absolute_lexical(path: Path) -> Path:
    """Make an absolute path without resolving symlinks."""
    import os

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _symlink_component(path: Path) -> Path | None:
    current = path
    while True:
        if current.is_symlink():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    link = _symlink_component(path)
    if link is not None:
        raise ValueError(f"{label} contains a symlink component: {link}")


def _ensure_empty_output(output: Path) -> None:
    _assert_no_symlink_components(output, label="output")
    if output.exists() and not output.is_dir():
        raise ValueError(f"output exists and is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)


def _safe_copy(source: Path, destination: Path, *, label: str) -> dict[str, Any]:
    _assert_no_symlink_components(source, label=label)
    _assert_no_symlink_components(destination.parent, label="destination")
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{label} must be a regular file: {source}")
    if destination.exists() and destination.is_symlink():
        raise ValueError(f"destination is a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {"sha256": _sha256(destination), "bytes": destination.stat().st_size}


def _select_file(source_root: Path, relative: str) -> tuple[Path | None, str]:
    """Read the independent repository source; historical overlays have no authority."""
    source = source_root / relative
    if source.exists():
        return source, "source"
    return None, "missing"


def _is_forbidden_resource(relative: str) -> bool:
    path = Path(relative)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (
        name.startswith(".env")
        or name == ".npmrc"
        or any(token in name for token in ("secret", "credential", "password", "token"))
        or name.endswith(tuple(_SECRET_SUFFIXES))
        or any(part in _CACHE_PARTS or part in _SECRET_PARTS for part in parts)
        or name.endswith((".pyc", ".pyo", ".log"))
    )


def _render_pyproject(stage: Path) -> str:
    template = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    entries = sorted(
        child.name
        for child in stage.iterdir()
        if child.name not in {"pyproject.toml", MANIFEST_NAME} and child.name != "__pycache__"
    )
    block = "[tool.hatch.build.targets.wheel]\nonly-include = [\n" + "".join(
        f'    "{entry}",\n' for entry in entries
    ) + "]\n"
    pattern = re.compile(r"\[tool\.hatch\.build\.targets\.wheel\]\nonly-include = \[.*?\n\]\n", re.S)
    rendered, count = pattern.subn(block, template, count=1)
    if count != 1:
        raise ValueError("packaging template has no replaceable Hatch include block")
    return rendered


def _copy_prompts(repo: Path, stage: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    source_soul = repo / "backend/prompts/SOUL.md"
    for relative, content in (("prompts/IDENTITY.md", TARGET_IDENTITY),):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        records.append(
            {
                "path": relative,
                "sha256": _sha256(destination),
                "bytes": destination.stat().st_size,
                "kind": "generated-target-prompt",
            }
        )
    destination = stage / "prompts/SOUL.md"
    record = _safe_copy(source_soul, destination, label="backend/prompts/SOUL.md")
    record.update({"path": "prompts/SOUL.md", "source_path": "backend/prompts/SOUL.md", "kind": "source"})
    records.append(record)
    return records


def _skill_name(relative: str) -> str | None:
    if not relative.startswith(SKILL_PREFIX):
        return None
    remainder = relative.removeprefix(SKILL_PREFIX)
    return remainder.split("/", 1)[0] if remainder else None


def _is_excluded_skill(relative: str) -> bool:
    return _skill_name(relative) in EXCLUDED_SKILL_NAMES


def _copy_generic_skill_resources(repo: Path, output: Path) -> list[dict[str, Any]]:
    """Copy non-Python files for selected generic Skills, never business Skills."""
    records: list[dict[str, Any]] = []
    skills_root = repo / SKILL_PREFIX
    if not skills_root.is_dir():
        return records
    for path in sorted(skills_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(repo).as_posix()
        if _is_excluded_skill(relative) or path.suffix == ".py" or _is_forbidden_resource(relative):
            continue
        destination = output / relative.removeprefix("backend/")
        record = _safe_copy(path, destination, label=relative)
        record.update({"path": destination.relative_to(output).as_posix(), "source_path": relative, "kind": "skill-resource"})
        records.append(record)
    return records


def _copy_launcher(output: Path) -> dict[str, Any]:
    destination = output / "puddingharness_cli.py"
    destination.write_text('''"""Console entry point for the standalone PuddingHarness API."""

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app:app",
        host=os.getenv("PUDDINGHARNESS_HOST", "127.0.0.1"),
        port=int(os.getenv("PUDDINGHARNESS_PORT", "8888")),
    )
''', encoding="utf-8")
    return {"path": "puddingharness_cli.py", "sha256": _sha256(destination), "bytes": destination.stat().st_size, "kind": "generated-target-launcher"}


def _copy_generic_tool_guide_manifest(output: Path) -> dict[str, Any]:
    destination = output / "prompts" / "tool_guides" / "manifest.yaml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        """version: 1
guides:
  - id: managed-lark-autonomy
    file: managed-lark-autonomy.md
    skill_prefixes: [lark-]
  - id: webbridge-browser
    file: webbridge-browser.md
    tools: [browser]
""",
        encoding="utf-8",
    )
    return {"path": "prompts/tool_guides/manifest.yaml", "sha256": _sha256(destination), "bytes": destination.stat().st_size, "kind": "generated-target-resource"}


def _excluded_skill_inventory(repo: Path) -> list[str]:
    root = repo / SKILL_PREFIX
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(repo).as_posix()
        for path in root.rglob("*")
        if path.is_file() and _is_excluded_skill(path.relative_to(repo).as_posix())
    )


def _verify_selected_hashes(repo: Path, report: dict[str, Any]) -> None:
    """Detect source drift after the audit snapshot was produced."""
    for row in report["selected"]:
        relative = str(row["path"])
        source, _kind = _select_file(repo, relative)
        if source is None:
            raise ValueError(f"selected target file disappeared after audit: {relative}")
        actual = _sha256(source)
        if actual != row["sha256"]:
            raise ValueError(f"selected target changed after audit: {relative}")


def _verify_source_parity(repo: Path, report: dict[str, Any]) -> None:
    """Require the independent product to stage its tested source implementation.

    Compare source with the audit digest so changes during staging fail closed.
    """
    for row in report["selected"]:
        relative = row["path"]
        source = repo / relative
        _assert_no_symlink_components(source, label="runtime source")
        if not source.is_file() or _sha256(source) != row["sha256"]:
            raise ValueError(f"runtime source differs from effective target: {relative}")


def stage_backend(
    repo: Path,
    output: Path,
    *,
    require_clean_audit: bool = False,
) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    output = _absolute_lexical(output)
    if not (repo / "backend").is_dir():
        raise ValueError(f"repository has no backend directory: {repo}")
    _assert_no_symlink_components(output, label="output")
    output_resolved = output.resolve(strict=False)
    if output_resolved == repo or repo in output_resolved.parents:
        raise ValueError("output must not be the source checkout or a child of it")
    audit_module = _load_audit()
    report = audit_module.audit(repo)
    _verify_source_parity(repo, report)
    _ensure_empty_output(output)
    if require_clean_audit and report["findings"]:
        raise ValueError(
            f"effective target audit is {report['status']} with {len(report['findings'])} finding(s); "
            "resolve them or omit --require-clean-audit for a review artifact"
        )

    staged_python: list[dict[str, Any]] = []
    skipped_skills: list[str] = []
    for row in report["selected"]:
        relative = str(row["path"])
        if not relative.startswith("backend/") or not relative.endswith(".py"):
            continue
        if _is_excluded_skill(relative):
            raise ValueError(f"audit selected an excluded Skill file: {relative}")
        source, kind = _select_file(repo, relative)
        if source is None:
            raise ValueError(f"selected target file is missing from source: {relative}")
        target_relative = relative.removeprefix("backend/")
        record = _safe_copy(source, output / target_relative, label=relative)
        if record["sha256"] != row["sha256"]:
            raise ValueError(f"selected target changed during copy: {relative}")
        record.update({"path": target_relative, "source_path": relative, "kind": kind})
        staged_python.append(record)

    resources: list[dict[str, Any]] = []
    for relative in RESOURCE_FILES:
        source, kind = _select_file(repo, relative)
        if source is None:
            raise ValueError(f"required target resource is missing: {relative}")
        target_relative = relative.removeprefix("backend/")
        record = _safe_copy(source, output / target_relative, label=relative)
        record.update({"path": target_relative, "source_path": relative, "kind": kind})
        resources.append(record)
    resources.extend(_copy_prompts(repo, output))
    resources.extend(_copy_generic_skill_resources(repo, output))
    resources.append(_copy_generic_tool_guide_manifest(output))
    resources.append(_copy_launcher(output))
    _verify_selected_hashes(repo, report)
    _verify_source_parity(repo, report)

    pyproject = _render_pyproject(output)
    pyproject_path = output / "pyproject.toml"
    pyproject_path.write_text(pyproject, encoding="utf-8")
    pyproject_record = {
        "path": "pyproject.toml",
        "sha256": _sha256(pyproject_path),
        "bytes": pyproject_path.stat().st_size,
    }
    lock_record: dict[str, Any] | None = None
    lock_source = PACKAGE_ROOT / "uv.lock"
    if lock_source.is_file():
        lock_path = output / "uv.lock"
        lock_record = _safe_copy(lock_source, lock_path, label="uv.lock")
        lock_record["path"] = "uv.lock"
        lock_record["source_path"] = "packages/puddingharness-extraction/uv.lock"

    manifest: dict[str, Any] = {
        "format": "puddingharness-independent-backend-stage/v1",
        # A static audit is only one extraction gate. Production activation,
        # migration, dependency, frontend, and release checks remain required.
        "releaseable": False,
        "audit_status": report["status"],
        "python_static_status": report["status"],
        "audit_finding_count": len(report["findings"]),
        "audit_findings": report["findings"],
        "source_revision": _git_revision(repo),
        "python": {
            "selected_count": len(report["selected"]),
            "staged_count": len(staged_python),
            "skipped_skill_count": len(skipped_skills),
            "skipped_skills": skipped_skills,
            "excluded_skill_files": _excluded_skill_inventory(repo),
            "files": staged_python,
        },
        "resources": resources,
        "packaging": {"pyproject": pyproject_record, "uv_lock": lock_record},
        "applied_overlays": [],
        "runtime_authority": "independent_repository_source",
        "excluded_domains": [
            "backend/knowledge/",
            "backend/knowledge_platform/",
            "backend/knowledge_contracts/",
            "backend/analytics/",
            "backend/vanna/",
            "backend/tools/database/",
            "backend/skills/{knowledge-search,llm-wiki,semantic-steward,database-analysis,table-analysis,build-semantic-dimension,build-logical-dataset,sql-guardrail-designer}/",
        ],
        "verification": {
            "source_checkout_runtime_path": False,
            "clean_dependency_install": False,
            "http_agent_smoke": "not_run by staging step",
        },
    }
    _verify_selected_hashes(repo, report)
    _verify_source_parity(repo, report)
    for record in resources:
        if record.get("kind") == "source":
            source = repo / record["source_path"]
            _assert_no_symlink_components(source, label="resource source")
            if not source.is_file() or _sha256(source) != record["sha256"]:
                raise ValueError(f"resource source changed before manifest: {record['source_path']}")
    for record in [*staged_python, *resources, pyproject_record, *([lock_record] if lock_record else [])]:
        target = output / record["path"]
        _assert_no_symlink_components(target, label="staged file")
        if not target.is_file() or _sha256(target) != record["sha256"]:
            raise ValueError(f"staged file changed before manifest: {record['path']}")
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        "--source-root",
        dest="repo",
        type=Path,
        default=PACKAGE_ROOT.parents[1],
        help="independent PuddingHarness source checkout",
    )
    parser.add_argument("--output", type=Path, required=True, help="absent or empty independent stage directory")
    parser.add_argument(
        "--require-clean-audit",
        action="store_true",
        help="fail instead of producing a review artifact when audit.selected has findings",
    )
    args = parser.parse_args(argv)
    try:
        result = stage_backend(args.repo, args.output, require_clean_audit=args.require_clean_audit)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "stage": str(Path(args.output).expanduser().resolve()),
                "releaseable": result["releaseable"],
                "audit_status": result["audit_status"],
                "audit_findings": result["audit_finding_count"],
                "staged_python": result["python"]["staged_count"],
                "skipped_skills": result["python"]["skipped_skill_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
