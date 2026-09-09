"""Static composition checks; runtime startup remains a separate required gate."""
import ast
from pathlib import Path


def test_target_app_retains_generic_routes_and_removes_business_lifecycle():
    path = Path(__file__).parents[1] / "overlays/backend/app.py"
    tree = ast.parse(path.read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert modules >= {"api.agent", "api.sessions", "api.headless", "api.mcp", "api.evaluation", "api.projects", "api.connectors"}
    assert not modules.intersection({"api.chat", "graph.agent", "extensions", "db_maintenance", "api.knowledge", "api.analytics", "runtime_identity.migration"})
    assert not any(m and m.split('.')[0] in {"knowledge", "analytics", "vanna", "knowledge_platform"} for m in modules)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names >= {"backend_lease", "evaluation_worker_manager", "session_manager", "deepagents_agent_manager"}
    assert not names.intersection({"knowledge_catalog_watcher", "semantic_dimension_build_worker_manager", "query_result_cleanup_manager", "extension_enabled"})


def test_target_lifespan_uses_owned_database_and_cleanup():
    path = Path(__file__).parents[1] / "overlays/backend/app.py"
    tree = ast.parse(path.read_text())
    lifecycle = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    assert any(isinstance(n, ast.Name) and n.id == "asynccontextmanager" for n in lifecycle.decorator_list)
    awaited = {n.value.func.id for n in ast.walk(lifecycle) if isinstance(n, ast.Await)
               and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)}
    assert {"init_database", "close_database"} <= awaited
