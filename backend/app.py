"""PuddingHarness API composition; target-only cleanup of the legacy entry point."""
import asyncio
import os
from builtins import BaseExceptionGroup
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
load_dotenv()
# Hold admission before importing any business singleton or opening Home stores.
from harness.installation_guard import admit_backend_process
_installation_guard = admit_backend_process()
from importlib.metadata import version as distribution_version
BACKEND_VERSION = distribution_version("puddingharness-backend")
BASE_DIR = Path(__file__).resolve().parent

def _exception_leaf_summary(exc: BaseException) -> str:
    """Expose useful TaskGroup leaf errors without dumping tracebacks."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [_exception_leaf_summary(item) for item in exc.exceptions]
        return '; '.join(dict.fromkeys((part for part in parts if part)))
    detail = ' '.join(str(exc).split())
    return f'{type(exc).__name__}: {detail}' if detail else type(exc).__name__

async def _warm_mcp_discovery(*, max_attempts: int=2, retry_delay_seconds: float=0.25) -> None:
    """Prime MCP metadata while keeping startup failures non-fatal.

    A stdio MCP server can close its first cold-start handshake while its
    runtime is still settling.  Retry that transient once before surfacing a
    warning; discovery failures are not cached, so the retry is a clean spawn.
    """
    enabled_mcp: list[str] = []
    try:
        import config
        from mcp_clients import load_filtered_mcp_tools
        from mcp_clients.servers import effective_mcp_server_names
        mcp_config = config.load_config().get('mcp', {})
        enabled_mcp = effective_mcp_server_names(mcp_config.get('enabled', []))
        if not enabled_mcp:
            return
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                tools = await load_filtered_mcp_tools(enabled_mcp)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(max(0.0, retry_delay_seconds))
            else:
                retry_note = ' after one cold-start retry' if attempt > 1 else ''
                print(f'🔌 MCP discovery warmed{retry_note}: {len(tools)} filtered tools')
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        cause = _exception_leaf_summary(exc)
        print(f'⚠️ MCP discovery warm-up did not complete; backend startup will continue and first use will retry. Cause: {cause}')

async def _install_cli_runtime_in_background() -> None:
    """Install the optional CLI after the backend has become ready."""
    from cli_runtime import ensure_cli_runtime
    try:
        status = await asyncio.to_thread(ensure_cli_runtime, BASE_DIR)
        if status.get('installed'):
            print(f"🧩 Worker CLI ready: {status.get('command')} v{status.get('version')} ({status.get('path')})")
        else:
            print(f"⚠️ Worker CLI remains unavailable; backend is still usable. {status.get('install_message') or 'install it separately when needed.'}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f'⚠️ Worker CLI background setup failed; backend will continue: {exc}')

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: scan skills, initialize agent, build memory index."""
    import traceback
    print('🚀 Initializing PuddingHarness backend...')
    from backend_lease import BackendInstanceLease
    from cli_runtime import detect_cli_runtime
    from db import close_database, get_database_status, init_database
    from evaluation.worker_manager import evaluation_worker_manager
    from graph.attachment_store import attachment_store
    from graph.deepagents_manager import deepagents_agent_manager
    from graph.session_manager import session_manager
    from projects.registry import project_registry
    from runtime_identity.paths import PuddingClawPaths
    from tools.skills_scanner import scan_skills
    user_paths = PuddingClawPaths.from_environment()
    user_paths.ensure_layout()
    backend_lease = BackendInstanceLease()
    lease_acquired = backend_lease.acquire(user_paths.state())
    if not lease_acquired:
        raise RuntimeError(f'Harness Home is already owned by another backend: {backend_lease.diagnostic}')
    try:
        if not await init_database():
            raise RuntimeError(f"Harness database initialization failed: {get_database_status().get('last_error')}")
        scan_skills(BASE_DIR, user_root=user_paths.user_skills(), snapshot_path=user_paths.skill_management() / 'SKILLS_SNAPSHOT.md')
        project_registry.initialize(user_paths.root)
        attachment_store.initialize(user_paths.root)
        session_manager.initialize(sessions_dir=user_paths.sessions())
        cli_status = detect_cli_runtime(BASE_DIR)
        if not cli_status.get('installed'):
            print(f"ℹ️ Worker CLI not ready yet; backend startup will continue. Policy={cli_status.get('install_policy')}; an optional background setup may install it.")
            if cli_status.get('install_policy') in {'auto', 'prompt'}:
                app.state.cli_runtime_install_task = asyncio.create_task(_install_cli_runtime_in_background(), name='puddingharness-cli-runtime-setup')
        await _warm_mcp_discovery()
        try:
            deepagents_agent_manager.initialize(BASE_DIR, user_root=user_paths.root)
            recovered_reviews = await deepagents_agent_manager.recover_pending_run_reviews()
            if recovered_reviews:
                print(f'🔁 Recovered {len(recovered_reviews)} pending ordinary Run review(s)')
        except Exception as e:
            print(f'⚠️ DeepAgents initialization failed: {e}')
            traceback.print_exc()
            print('ℹ️ Server will continue running, but /api/agent requires DeepAgents runtime.')
        await evaluation_worker_manager.start_pending()
        print('✅ PuddingHarness backend ready')
        yield
    finally:
        cli_task = getattr(app.state, 'cli_runtime_install_task', None)
        if cli_task is not None and not cli_task.done():
            cli_task.cancel()
            await asyncio.gather(cli_task, return_exceptions=True)
        try:
            await evaluation_worker_manager.stop()
        finally:
            try:
                await close_database()
            finally:
                backend_lease.release()
app = FastAPI(title='PuddingHarness', version=BACKEND_VERSION, lifespan=lifespan)
cors_origins = [origin.strip() for origin in os.getenv('CORS_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000').split(',') if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
from runtime_control import MaintenanceModeError

@app.exception_handler(MaintenanceModeError)
async def maintenance_mode_exception_handler(request, exc: MaintenanceModeError):
    """Map drain/maintenance write rejections to a uniform 503 + Retry-After."""
    return JSONResponse(status_code=503, headers={'Retry-After': str(exc.retry_after)}, content={'detail': str(exc), 'write_mode': exc.write_mode, 'retry_after': exc.retry_after})
from api.connectors import router as connectors_router
from api.agent import router as agent_router
from api.attachments import router as attachments_router
from api.capabilities import router as capabilities_router
from api.compress import router as compress_router
from api.config_api import router as config_router
from api.eval_api import router as eval_router
from api.evaluation import router as evaluation_router
from api.files import router as files_router
from api.maintenance import router as maintenance_router
from api.mcp import router as mcp_router
from api.permissions import router as permissions_router
from api.projects import router as projects_router
from api.runtime_profile import router as runtime_profile_router
from api.sessions import router as sessions_router
from api.skill_plans import router as skill_plans_router
from api.skill_secret_requests import router as skill_secret_requests_router
from api.skills_api import router as skills_api_router
from api.stats_api import router as stats_router
from api.tokens import router as tokens_router
from api.toolchains import router as toolchains_router
from api.user_input_requests import router as user_input_requests_router
from api.kernel_fallback_requests import router as kernel_fallback_requests_router
from api.web_search_config import router as web_search_config_router
app.include_router(connectors_router, prefix='/api')
app.include_router(agent_router, prefix='/api')
app.include_router(skills_api_router, prefix='/api')
app.include_router(files_router, prefix='/api')
app.include_router(sessions_router, prefix='/api')
app.include_router(tokens_router, prefix='/api')
app.include_router(compress_router, prefix='/api')
app.include_router(config_router, prefix='/api')
app.include_router(eval_router, prefix='/api')
app.include_router(evaluation_router, prefix='/api')
app.include_router(stats_router, prefix='/api')
app.include_router(mcp_router, prefix='/api')
app.include_router(maintenance_router, prefix='/api')
app.include_router(capabilities_router, prefix='/api')
app.include_router(runtime_profile_router, prefix='/api')
app.include_router(projects_router, prefix='/api')
app.include_router(permissions_router, prefix='/api')
app.include_router(skill_plans_router, prefix='/api')
app.include_router(skill_secret_requests_router, prefix='/api')
app.include_router(attachments_router, prefix='/api')
app.include_router(user_input_requests_router, prefix='/api')
app.include_router(kernel_fallback_requests_router, prefix='/api')
app.include_router(toolchains_router, prefix='/api')
app.include_router(web_search_config_router, prefix='/api')
from api.headless import router as headless_router
from api.headless import headless_activity_router
app.include_router(headless_router, prefix='/api')
app.include_router(headless_activity_router, prefix='/api')

@app.get('/')
async def root():
    return {'name': 'PuddingHarness', 'version': BACKEND_VERSION, 'status': 'running',
            'role': 'backend', 'instance_id': os.environ.get('PUDDINGHARNESS_INSTANCE_ID')}
