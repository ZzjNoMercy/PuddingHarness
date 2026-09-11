"""Audit the complete proposed Harness Python tree before history extraction.

This is an executable target selection and dependency check, not an extraction
or a release assertion. Legacy source files are never edited or deleted.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

# Whole-domain exclusions required by specification sections 11.8 and 11.18.
EXCLUDED_PREFIXES = (
    "backend/knowledge/", "backend/knowledge_platform/", "backend/knowledge_contracts/",
    "backend/analytics/", "backend/vanna/", "backend/tools/database/",
    # Platform query/admin Skills are separately installed bundles (spec 11.17).
    *("backend/skills/" + name + "/" for name in (
        "knowledge-search", "llm-wiki", "semantic-steward", "database-analysis",
        "table-analysis", "build-semantic-dimension", "build-logical-dataset",
        "sql-guardrail-designer",
    )),
    "backend/scripts/", "backend/dist/", "backend/.venv/", "backend/logs/", "backend/tests/",
)
EXCLUDED_FILES = frozenset({
    "backend/gbrain_runtime.py", "backend/extensions.py",
    "backend/catalog_migration.py", "backend/graph/database_evidence.py",
    "backend/graph/database_schema_evidence.py",
    "backend/api/chat.py", "backend/graph/agent.py",
    # Source-product layout migrators copy mixed business state implicitly.
    # Target upgrade must use the explicit, versioned cross-product importer.
    "backend/runtime_identity/migration.py",
    "backend/api/knowledge.py", "backend/api/analytics.py", "backend/api/knowledge_sources.py",
    "backend/api/llm_wiki.py", "backend/api/read_later.py", "backend/api/feishu_connector.py",
    "backend/api/brain_schema.py", "backend/api/dimension_build_rules.py",
    "backend/api/logical_dataset_rules.py", "backend/api/database_sql_revisions.py",
    "backend/graph/middlewares/tool_intent_router.py", "backend/graph/middlewares/semantic_assets.py",
    "backend/graph/middlewares/analysis_templates.py", "backend/graph/dimension_build_resume.py",
    "backend/graph/logical_dataset_resume.py", "backend/graph/database_sql_revision_resume.py", "backend/harness/analytics_invariants.py",
    "backend/llm/embed_client.py", "backend/llm/embedding_limits.py",
    "backend/llm/multimodal_embedding.py", "backend/llm/rerank_client.py",
    *('backend/tools/' + name + '.py' for name in (
        'feishu_bitable_tools', 'llm_wiki_tools', 'mineru_tool', 'read_later_tool', 'search_knowledge_tool',
        'database_knowledge_tool', 'inspect_dimension_build_input_tool', 'logical_dataset_tools',
        'request_dimension_build_rule_tool', 'request_logical_dataset_rule_tool',
        'semantic_dimension_build_tool', 'semantic_steward_tool', 'pandas_knowledge_tool',
    )),
})
BUSINESS_ROOTS = frozenset({"knowledge", "knowledge_platform", "knowledge_contracts", "analytics", "vanna"})
BUSINESS_SYMBOLS = frozenset({
    "analytics_model_id", "analytics_model_context", "ToolIntentRouterMiddleware",
    "SemanticAssetsMiddleware", "AnalysisTemplatesMiddleware", "semantic_steward",
    "llamaindex_knowledge_query",
})


def module_name(path: str) -> str:
    name = path.removeprefix("backend/").removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


def skill_root(path: str) -> str | None:
    """Return the owning Skill directory for a backend/skills file."""
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "backend" and parts[1] == "skills":
        return "/".join(parts[:3])
    return None


def resolve_skill_local_import(path: str, target: str, skill_aliases: dict[str, set[str]], target_modules: set[str]) -> tuple[str | None, bool]:
    """Resolve imports such as ``from scripts.utils import ...`` within one Skill.

    Skill scripts are launched with their Skill directory on ``sys.path`` (the
    bundled scripts and their tests do this explicitly). Those imports are not
    backend package imports and must be resolved against the owning Skill root.
    The boolean reports whether the target used a known Skill-local alias; a
    missing module under such an alias remains an audit finding.
    """
    root = skill_root(path)
    if root is None or not target or target.startswith("<"):
        return None, False
    aliases = skill_aliases.get(root, set())
    pieces = target.split(".")
    if not pieces or pieces[0] not in aliases:
        return None, False
    for length in range(len(pieces), 0, -1):
        # ``scripts.missing`` is a module path, not the ``scripts`` package
        # with a missing export. Package-prefix fallback is only valid when
        # the import has already named a concrete module plus an imported
        # symbol, such as ``scripts.utils.parse_skill_md``.
        if length == 1 and len(pieces) > 1:
            continue
        candidate = f"{root}/{'/'.join(pieces[:length])}"
        for suffix in (".py", "/__init__.py"):
            canonical = module_name(candidate + suffix)
            if canonical in target_modules:
                return canonical, True
    return None, True


def _module_exports(tree: ast.Module) -> tuple[set[str], bool]:
    """Collect explicit module bindings without treating function locals as exports.

    Star reexports and module __getattr__ are deliberately unknown; runtime
    verification is still required for those modules.
    """
    class Bindings(ast.NodeVisitor):
        def __init__(self):
            self.names = set()
            self.dynamic = False

        def visit_FunctionDef(self, node):
            self.names.add(node.name)
            self.dynamic |= node.name == "__getattr__"

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.names.add(node.name)

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                self.names.add(node.id)

        def visit_Import(self, node):
            self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

        def visit_ImportFrom(self, node):
            self.dynamic |= any(alias.name == "*" for alias in node.names)
            self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

    bindings = Bindings()
    bindings.visit(tree)
    return bindings.names, bindings.dynamic


def _imports(tree, module, is_package, include_imported_names=True):
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                if node.level > len(parts):
                    yield node.lineno, "<invalid-relative-import>"
                    continue
                base = ".".join(parts[:len(parts) - node.level + 1])
                target = ".".join(filter(None, (base, node.module)))
            else:
                target = node.module or ""
            yield node.lineno, target
            for alias in node.names:
                if include_imported_names is True or (include_imported_names == "relative" and node.level):
                    if alias.name != "*":
                        yield node.lineno, f"{target}.{alias.name}"
        elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            called = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if called in {"import_module", "__import__"}:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    yield node.lineno, node.args[0].value
                else:
                    yield node.lineno, "<dynamic-import-review>"


def verify_overlay_provenance(repo: Path, overlays: Path, manifest: Path) -> None:
    value = json.loads(manifest.read_text())
    if value.get("format") != "puddingharness-cleanup-overlay/v1" or value.get("applied_to_source") is not False:
        raise ValueError("overlay provenance format is invalid")
    expected = {}
    for row in value["files"]:
        name = row["target_path"]
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts or name in expected:
            raise ValueError("overlay provenance path is invalid")
        expected[name] = row
    actual = {p.relative_to(overlays).as_posix(): p for p in overlays.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    if set(actual) != set(expected):
        raise ValueError("overlay file set changed after review")
    for name, path in actual.items():
        source = repo / name
        if path.is_symlink() or source.is_symlink():
            raise ValueError("overlay or source is a symlink")
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
        if source_digest != expected[name]["source_sha256"]:
            raise ValueError("overlay source changed after review: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]["overlay_sha256"]:
            raise ValueError("overlay changed after review: " + name)


# This disposition is code-reviewed alongside the audited implementation. It is
# not a signature or release authorization. Only this exact module's bytes and
# this single finding are covered; every other detector remains blocking.
_COMPATIBILITY_REVIEW = {
    "path": "backend/harness/legacy_artifacts.py",
    "sha256": "68f5e6e27e18df6a6556752410dcae9b4f2b59cfd4a30e7528e22fdb78dd1c60",
    "kind": "business_protocol_symbol",
    "target": "analytics_model_id",
    "line": 13,
    "reason": "Retired selector is only declared for rejection and read-only legacy projection; opaque payloads are preserved.",
    "review_type": "automated_adversarial_boundary_review",
    "evidence": "docs/knowledge-platform/harness-compatibility-audit-review.md",
}


def _disposition_findings(findings: list[dict], contents: dict[str, bytes], *, historical: bool):
    blocking, reviewed = [], []
    review = _COMPATIBILITY_REVIEW
    exact_source = (not historical and review["path"] in contents and
                    hashlib.sha256(contents[review["path"]]).hexdigest() == review["sha256"])
    for finding in findings:
        if exact_source and all(finding.get(key) == review[key] for key in ("path", "kind", "target", "line")):
            reviewed.append({**finding, "disposition": "reviewed_compatibility_boundary",
                             "review": dict(review)})
        else:
            blocking.append(finding)
    return blocking, reviewed


def audit(repo: Path, overlays: Path | None = None) -> dict:
    repo = repo.resolve()
    originals = {p.relative_to(repo).as_posix(): p for p in (repo / "backend").rglob("*.py")
                 if not any(part in {"__pycache__", ".venv", "dist", "logs"} for part in p.relative_to(repo).parts)}
    selected = {name: path for name, path in originals.items()
                if name not in EXCLUDED_FILES and not name.startswith(EXCLUDED_PREFIXES)}
    excluded = sorted(set(originals) - set(selected))
    effective = dict(selected)
    overlay_names = set()
    if overlays and overlays.exists():
        for path in overlays.rglob("*.py"):
            name = path.relative_to(overlays).as_posix()
            if name in EXCLUDED_FILES or name.startswith(EXCLUDED_PREFIXES):
                raise ValueError("overlay attempts to restore an excluded domain")
            if name.startswith("backend/"):
                effective[name] = path
                overlay_names.add(name)
    original_modules = {module_name(p) for p in originals}
    target_modules = {module_name(p) for p in effective}
    local_roots = {m.split(".")[0] for m in original_modules}
    skill_aliases: dict[str, set[str]] = {}
    for name in effective:
        owner = skill_root(name)
        if owner is None:
            continue
        relative = name[len(owner) + 1:]
        if relative:
            skill_aliases.setdefault(owner, set()).add(relative.split("/", 1)[0].removesuffix(".py"))
    parsed = {}
    exports = {}
    contents = {}
    for name, path in effective.items():
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("source or overlay contains a symlink")
        try:
            contents[name] = path.read_bytes()
            parsed[name] = ast.parse(contents[name], filename=name)
            exports[module_name(name)] = _module_exports(parsed[name])
        except (SyntaxError, UnicodeError):
            pass
    findings = []
    rows = []
    for name, path in sorted(effective.items()):
        content = contents[name]
        rows.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "overlay": name in overlay_names})
        tree = parsed.get(name)
        if tree is None:
            findings.append({"path": name, "line": 1, "kind": "invalid_python", "target": "parse"})
            continue
        seen = set()
        for line, target in _imports(tree, module_name(name), name.endswith("/__init__.py"), include_imported_names="relative"):
            resolved_target, is_skill_local = resolve_skill_local_import(name, target, skill_aliases, target_modules)
            if is_skill_local and resolved_target is None:
                kind = "unresolved_local_import"
                if (line, kind, target) not in seen:
                    findings.append({"path": name, "line": line, "kind": kind, "target": target})
                    seen.add((line, kind, target))
                continue
            checked_target = resolved_target or target
            root = checked_target.split(".")[0]
            kind = None
            if target.startswith("<"):
                kind = "dynamic_import_review" if "dynamic" in target else "invalid_relative_import"
            elif root in BUSINESS_ROOTS:
                kind = "forbidden_domain_import"
            elif checked_target in original_modules and checked_target not in target_modules:
                kind = "excluded_module_import"
            elif root in local_roots and checked_target not in original_modules and not any(
                checked_target.startswith(module + ".") or module.startswith(checked_target + ".") for module in target_modules
            ):
                kind = "unresolved_local_import"
            if kind and (line, kind, target) not in seen:
                findings.append({"path": name, "line": line, "kind": kind, "target": target})
                seen.add((line, kind, target))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = list(_imports(ast.Module(body=[node], type_ignores=[]), module_name(name), name.endswith("/__init__.py")))
                target_module = imported[0][1]
                resolved_target, is_skill_local = resolve_skill_local_import(name, target_module, skill_aliases, target_modules)
                if resolved_target is not None:
                    target_module = resolved_target
                known = exports.get(target_module)
                if known is not None and not known[1]:
                    for alias in node.names:
                        qualified = f"{target_module}.{alias.name}"
                        if alias.name != "*" and alias.name not in known[0] and qualified not in target_modules:
                            findings.append({"path": name, "line": node.lineno,
                                "kind": "missing_local_export", "target": qualified})
            if isinstance(node, ast.alias):
                for imported_symbol in {node.name, node.asname}:
                    if imported_symbol in BUSINESS_SYMBOLS and (node.lineno, imported_symbol) not in seen:
                        findings.append({"path": name, "line": node.lineno,
                                         "kind": "business_protocol_symbol", "target": imported_symbol})
                        seen.add((node.lineno, imported_symbol))
            symbol = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute)
                      else node.arg if isinstance(node, (ast.arg, ast.keyword)) else node.value if isinstance(node, ast.Constant) else None)
            if (isinstance(node, ast.Constant) and isinstance(symbol, str)
                    and symbol.startswith("PUDDINGCLAW_") and symbol.replace("_", "").isalnum()):
                findings.append({"path": name, "line": node.lineno,
                    "kind": "legacy_runtime_environment", "target": symbol})
            if isinstance(symbol, str) and symbol in BUSINESS_SYMBOLS and (node.lineno, symbol) not in seen:
                findings.append({"path": name, "line": node.lineno, "kind": "business_protocol_symbol", "target": symbol})
                seen.add((node.lineno, symbol))
            if isinstance(node, ast.Call):
                called = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                if called == "get_tools_by_categories":
                    # Examine category arguments only: a workspace named knowledge is legal.
                    categories = list(node.args[1:]) + [kw.value for kw in node.keywords if kw.arg == "categories"]
                    for argument in categories:
                        for value in ast.walk(argument):
                            if isinstance(value, ast.Constant) and value.value in ("knowledge", "analytics"):
                                findings.append({"path": name, "line": value.lineno,
                                    "kind": "business_tool_category", "target": value.value})
    findings.sort(key=lambda row: (row["path"], row["line"], row["kind"], row["target"]))
    blocking, reviewed = _disposition_findings(findings, contents, historical=overlays is not None)
    status = "blocked" if blocking else "python_static_reviewed" if reviewed else "python_static_clean"
    return {"format": "puddingharness-target-python-audit/v1", "status": status,
            "full_repository_verified": False, "production_activation_allowed": False,
            "scope": "all proposed Python runtime files; excludes tests; frontend/build/runtime tests remain required",
            "selected": rows, "excluded": excluded, "findings": findings,
            "blocking_findings": blocking, "reviewed_findings": reviewed,
            "source_digest": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-extraction", action="store_true",
                        help="audit historical overlays with their immutable extraction provenance")
    args = parser.parse_args()
    overlay_root = None
    if args.legacy_extraction:
        overlay_root = Path(__file__).parent / "overlays"
        verify_overlay_provenance(args.repo, overlay_root, Path(__file__).parent / "overlay-provenance.json")
    result = audit(args.repo, overlay_root)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"status": result["status"], "selected": len(result["selected"]),
                      "excluded": len(result["excluded"]), "findings": len(result["findings"]),
                      "blocking_findings": len(result["blocking_findings"]),
                      "reviewed_findings": len(result["reviewed_findings"])}))
