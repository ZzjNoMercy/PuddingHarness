"""Generic PuddingHarness configuration boundary.

This target overlay owns user settings for the Harness runtime.  PuddingClaw's
Knowledge, RAG, Vanna, and Analytics configuration remains in the legacy
source module and is intentionally absent here.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILE: Path | None = None
DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO = 0.8
_LEGACY_TERMINAL_EXECUTION_KEYS = frozenset(
    {"sandbox_mode", "docker_enabled", "on_unavailable"}
)
_REPLACE_DICT_PATHS = frozenset({("subagents",), ("mcp", "servers")})


class UnsupportedTerminalExecutionConfig(ValueError):
    """Raised when a config still uses the removed execution-mode contract."""


_DEFAULT_CONFIG: dict[str, Any] = {
    "database": {
        "provider": "sqlite",
        "source": "local_file",
        "host": "127.0.0.1",
        "port": 5432,
        "database": "puddingharness",
        "username": "puddingharness",
        "password": "",
        "url": "",
    },
    "compression": {
        "trigger_count": 20,
        "max_history_messages": 100,
        "deepagents": {
            "summarization": {
                "enabled": True,
                "model_id": "",
                "trigger_tokens": 272000,
                "keep_tokens": 64000,
            },
            "tool_context": {
                "enabled": True,
                "immediate_compaction_enabled": False,
                "single_tool_trigger_tokens": 8000,
                "background_min_result_tokens": 1000,
                "retain_tool_context_tokens": 32000,
                "batch_size": 6,
                "max_concurrency": 4,
                "job_timeout_seconds": 120,
                "max_candidates_per_job": 48,
            },
        },
        "middleware": {
            "enabled": True,
            "tool_clear": {"keep_recent": 10, "min_summary_length": 500},
            "summarization": {
                "enabled": True,
                "trigger_tokens": 200000,
                "keep_messages": 10,
                "use_chinese_prompt": True,
            },
            "trim": {"max_tokens": 12000, "keep_last": 10},
            "compaction": {
                "enabled": True,
                "trigger_tokens": 500000,
                "keep_recent": 8,
                "compact_budget_tokens": 120000,
            },
        },
    },
    "cache": {
        "enabled": True,
        "cache_boundary": {"enabled": True},
        "tail_trim": {
            "enabled": True,
            "max_tokens": 200000,
            "head_keep": 2,
            "keep_recent": 30,
        },
        "middle_trim": {
            "enabled": True,
            "max_tokens": 200000,
            "head_keep": 2,
            "keep_recent": 30,
            "summary_budget_chars": 60000,
        },
    },
    "subagents": {
        "image_analyzer": {
            "enabled": True,
            "model": "qwen:qwen3.7",
            "description": "Analyze image inputs and answer questions about them.",
            "route_trigger": "image_input",
            "tools": {"mode": "inherit"},
            "skills": {"mode": "inherit", "paths": []},
            "system_prompt": (
                "You are an image analysis specialist. When given an image, describe its contents "
                "in detail and answer any questions about it. Return your findings as concise, "
                "structured text."
            ),
        },
    },
    "harness": {
        "prompt_cache": {
            "trace_part_diagnostics": True,
            "ordered_system_sections": True,
            "tail_routing_message": True,
            "deterministic_session_projection": True,
            "stable_tool_schema": False,
        },
        "model_call_limit": {
            "enabled": True,
            "run_limit": 50,
            "thread_limit": None,
            "exit_behavior": "end",
        },
        "model_resilience": {
            "transport_retry": {
                "enabled": True,
                "max_attempts": 2,
                "initial_delay_seconds": 0.25,
                "max_delay_seconds": 2.0,
            },
            "terminal_response": {"enabled": True, "max_recovery_attempts": 1},
        },
        "completion": {
            "run_review": {
                "policy": "off",
                "model": "",
                "environment_profile": "none",
                "manual_enabled": True,
            },
            "rubric": {
                "enabled": False,
                "model": "deepseek-v4-flash",
                "max_iterations": 3,
                "max_stagnant_repairs": 2,
                "environment_profile": "none",
                "custom_rules_enabled": False,
                "custom_rules": [],
            },
        },
        "goals": {
            "enabled": True,
            "activation": "explicit_user_only",
            "default_enabled": False,
            "auto_promote_from_run": False,
            "max_rounds": 8,
        },
        "terminal": {
            "execution_mode": "spawn",
            "external_directory_writable_enabled": False,
            "default_timeout_seconds": 120,
            "docker": {
                "connection": "",
                "context": "",
                "probe_timeout_seconds": 5,
                "image": "puddingclaw/sandbox:python3.12-node22-chromium-v5",
                "cpu_limit": "2",
                "memory_limit_mb": 4096,
                "pids_limit": 256,
                "network_enabled": False,
                "dependency_setup_enabled": False,
                "dependency_setup_opt_in_version": 1,
                "lifecycle": "project",
                "idle_stop_minutes": 30,
            },
        },
    },
    "write_middleware": {
        "enabled": True,
        "task_state": {
            "enabled": True,
            "todo_path": "workspace/TODO.md",
            "triggers": ["帮我", "待办", "记得", "提醒", "任务", "需要做"],
        },
    },
    "mcp": {"enabled": [], "servers": {}},
}


def _deep_merge(base: dict, override: dict, *, _path: tuple[str, ...] = ()) -> dict:
    """Deep merge user settings into defaults without sharing mutable values."""

    result = copy.deepcopy(base)
    for key, value in override.items():
        path = (*_path, str(key))
        if path in _REPLACE_DICT_PATHS and isinstance(value, dict):
            result[key] = copy.deepcopy(value)
        elif key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value, _path=path)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _deepagents_summary_input_tokens(config: dict[str, Any]) -> int:
    del config
    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("agent")
    try:
        context_window = max(1, int(resolved.get("context_window", 1000000)))
    except (TypeError, ValueError):
        context_window = 1000000
    return max(1, int(context_window * DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO))


def _validate_mcp_document(value: Any) -> None:
    """Validate only generic MCP container shape; names remain user-owned."""

    if not isinstance(value, dict):
        raise ValueError("mcp settings must be an object")
    enabled = value.get("enabled", [])
    if not isinstance(enabled, list) or any(
        not isinstance(name, str) or not name.strip() for name in enabled
    ):
        raise ValueError("mcp.enabled must be an array of server names")
    servers = value.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.servers must be an object")


def _validate_config_document(value: Any) -> dict[str, Any]:
    """Validate the Harness sparse override without business compatibility."""

    if not isinstance(value, dict):
        raise ValueError("PuddingHarness settings must be a JSON object")
    data = copy.deepcopy(value)
    schema_version = data.pop("schema_version", 1)
    if schema_version != 1:
        raise ValueError(f"Unsupported config schema_version: {schema_version!r}")
    unknown = set(data).difference(_DEFAULT_CONFIG)
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))
    compression = data.get("compression")
    if isinstance(compression, dict) and "ratio" in compression:
        raise ValueError("Retired setting is not supported: compression.ratio")
    database = data.get("database")
    if isinstance(database, dict) and database.get("password"):
        raise ValueError("database.password must be stored through Credential Vault")
    if "mcp" in data:
        _validate_mcp_document(data["mcp"])
    harness = data.get("harness")
    completion = harness.get("completion") if isinstance(harness, dict) else None
    run_review = completion.get("run_review") if isinstance(completion, dict) else None
    if isinstance(run_review, dict) and run_review.get("policy") not in {None, "off", "shadow"}:
        raise ValueError(
            "harness.completion.run_review.policy must be off or shadow; "
            "blocking_one_shot is request-scoped"
        )
    terminal = harness.get("terminal") if isinstance(harness, dict) else None
    if isinstance(terminal, dict):
        legacy_keys = sorted(_LEGACY_TERMINAL_EXECUTION_KEYS.intersection(terminal))
        if legacy_keys:
            raise UnsupportedTerminalExecutionConfig(
                "harness.terminal uses removed execution fields: "
                + ", ".join(legacy_keys)
                + "; choose execution_mode=spawn or kernel"
            )
    _strip_empty_inherited_overrides(data)
    return data


def _strip_empty_inherited_overrides(config: dict[str, Any]) -> bool:
    harness = config.get("harness")
    completion = harness.get("completion") if isinstance(harness, dict) else None
    rubric = completion.get("rubric") if isinstance(completion, dict) else None
    if not isinstance(rubric, dict) or not isinstance(rubric.get("model"), str):
        return False
    if rubric["model"].strip():
        return False
    rubric.pop("model", None)
    if not rubric:
        completion.pop("rubric", None)
    if not completion:
        harness.pop("completion", None)
    if not harness:
        config.pop("harness", None)
    return True


def _config_path() -> Path:
    if CONFIG_FILE is not None:
        return Path(CONFIG_FILE)
    from harness.installation_guard import bound_installation_home
    return bound_installation_home() / "config.json"


def load_config() -> dict[str, Any]:
    """Load Harness defaults plus sparse user overrides."""

    config_path = _config_path()
    if not config_path.exists():
        return copy.deepcopy(_DEFAULT_CONFIG)
    try:
        data = _validate_config_document(
            json.loads(config_path.read_text(encoding="utf-8"))
        )
        terminal = data.get("harness", {}).get("terminal") if isinstance(data, dict) else None
        if isinstance(terminal, dict):
            legacy_keys = sorted(_LEGACY_TERMINAL_EXECUTION_KEYS.intersection(terminal))
            if legacy_keys:
                raise UnsupportedTerminalExecutionConfig(
                    "harness.terminal uses removed execution fields: "
                    + ", ".join(legacy_keys)
                    + "; choose execution_mode=spawn or kernel"
                )
        return _deep_merge(_DEFAULT_CONFIG, data)
    except UnsupportedTerminalExecutionConfig:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("[config] invalid settings file %s: %s", config_path, exc)
        raise ValueError(f"Invalid PuddingHarness settings file: {config_path}") from exc
    except Exception:
        logger.exception("[config] failed to load settings from %s", config_path)
        raise


def _config_overrides(value: Any, defaults: Any, *, _path: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(defaults, dict):
        return value
    result: dict[str, Any] = {}
    for key, current in value.items():
        if key == "schema_version":
            continue
        if key not in defaults:
            result[key] = copy.deepcopy(current)
            continue
        default = defaults[key]
        path = (*_path, str(key))
        if path in _REPLACE_DICT_PATHS:
            if current != default:
                result[key] = copy.deepcopy(current)
        elif isinstance(current, dict) and isinstance(default, dict):
            nested = _config_overrides(current, default, _path=path)
            if nested:
                result[key] = nested
        elif isinstance(current, float) and isinstance(default, (int, float)):
            if not math.isclose(current, float(default), rel_tol=1e-12, abs_tol=1e-12):
                result[key] = current
        elif current != default:
            result[key] = current
    return result


def _strip_database_credentials(config: dict[str, Any]) -> bool:
    database = config.get("database")
    if not isinstance(database, dict) or not database.get("password"):
        return False
    from provider_registry import LocalCredentialStore

    reference = LocalCredentialStore().put("database-config", str(database["password"]))
    database["password_ref"] = reference
    database["password"] = ""
    return True


def save_config(config: dict[str, Any]) -> None:
    """Persist only generic Harness settings atomically."""

    # Validate before touching the filesystem or Vault. The writable API may
    # accept a new password, but the on-disk document must contain a reference.
    candidate = copy.deepcopy(config)
    database_candidate = candidate.get("database") if isinstance(candidate, dict) else None
    if isinstance(database_candidate, dict):
        database_candidate.pop("password", None)
    _validate_config_document(candidate)
    target = _config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    persisted = copy.deepcopy(config)
    _strip_empty_inherited_overrides(persisted)
    _strip_database_credentials(persisted)
    database = persisted.get("database")
    if isinstance(database, dict) and database.get("password_ref") and database.get("password") == "":
        database.pop("password", None)
    payload = {"schema_version": 1, **_config_overrides(persisted, _DEFAULT_CONFIG)}
    fd, raw_path = tempfile.mkstemp(prefix=".config.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw_path, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def get_middleware_config() -> dict[str, Any]:
    config = load_config()
    return config.get("compression", {}).get("middleware", copy.deepcopy(_DEFAULT_CONFIG["compression"]["middleware"]))


def get_cache_config() -> dict[str, Any]:
    return load_config().get("cache", copy.deepcopy(_DEFAULT_CONFIG["cache"]))


def get_compress_trigger_count() -> int:
    return int(load_config().get("compression", {}).get("trigger_count", 20))


def get_fallback_llm_config(
    *,
    thinking_enabled_override: bool | None = None,
    binding: str = "agent",
    model_id_override: str | None = None,
    thinking_level: str | None = None,
    credential_name: str | None = None,
) -> dict[str, Any]:
    """Resolve the generic Agent model through Provider Registry."""

    from llm.thinking_mapping import map_thinking_request
    from provider_registry import get_provider_registry

    registry = get_provider_registry()
    resolved = (
        registry.resolve_model(model_id_override, credential_name=credential_name)
        if model_id_override
        else registry.resolve_binding(binding, credential_name=credential_name)
    )
    if thinking_enabled_override is False:
        provider = str(resolved.get("provider_id") or "").strip().lower()
        model = str(resolved.get("name") or "").strip().lower()
        mapped_thinking = {
            "thinking_enabled": False,
            "thinking_level": None,
            "reasoning_effort": None,
            "extra_body": (
                {"thinking": {"type": "disabled"}}
                if provider == "deepseek" and model.startswith("deepseek-v4-")
                else None
            ),
        }
    else:
        mapped_thinking = map_thinking_request(resolved.get("thinking_profile", {}), thinking_level)
    return {
        "provider": resolved.get("provider_id", "deepseek"),
        "model": resolved.get("name") or "deepseek-chat",
        "api_key": resolved.get("api_key", ""),
        "base_url": resolved.get("base_url", "https://api.deepseek.com"),
        "protocol": resolved.get("protocol", "deepseek"),
        "model_id": resolved.get("id", ""),
        "credential_name": resolved.get("credential_name", "default"),
        "temperature": float(resolved.get("temperature", 0.7)),
        "max_tokens": int(resolved.get("max_tokens", 4096)),
        "context_window": int(resolved.get("context_window", 1000000)),
        **mapped_thinking,
    }


def _raw_database_overrides() -> dict[str, Any]:
    path = _config_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    database = data.get("database") if isinstance(data, dict) else None
    return database if isinstance(database, dict) else {}


def get_database_config(*, resolve_password: bool = True) -> dict[str, Any]:
    """Read generic Core database settings and resolve its credential."""

    from urllib.parse import quote, unquote, urlparse

    database = load_config().get("database", {})
    raw_database = _raw_database_overrides()
    env_url = os.getenv("PUDDINGHARNESS_DATABASE_URL", "").strip()
    env_mode = os.getenv("PUDDINGHARNESS_DATABASE_MODE", "").strip().lower()
    env_source = os.getenv("PUDDINGHARNESS_DATABASE_SOURCE", "").strip().lower()
    env_provider = os.getenv("PUDDINGHARNESS_DATABASE_PROVIDER", "").strip().lower()
    configured_url = str(database.get("url", "") or "").strip()
    host = str(database.get("host", "127.0.0.1") or "127.0.0.1").strip()
    port = int(database.get("port") or 5432)
    db_name = str(database.get("database", "puddingharness") or "puddingharness").strip()
    username = str(database.get("username", "puddingharness") or "puddingharness").strip()
    provider = str(raw_database.get("provider", "") or "").strip().lower()
    source = str(raw_database.get("source", "") or "").strip().lower()
    legacy_mode = str(raw_database.get("mode", "") or "").strip().lower()
    if provider not in {"sqlite", "postgresql"}:
        if legacy_mode == "sqlite":
            provider, source = "sqlite", source or "local_file"
        elif legacy_mode in {"bundled", "external"}:
            provider, source = "postgresql", source or legacy_mode
        else:
            provider = str(database.get("provider", "sqlite") or "sqlite").strip().lower()
            if provider not in {"sqlite", "postgresql"}:
                provider = "sqlite"
            source = source or str(database.get("source", "") or "").strip().lower()
    if not source:
        source = "local_file" if provider == "sqlite" else "external"

    from provider_registry import LocalCredentialStore

    raw_password = str(database.get("password") or "")
    password_ref = str(database.get("password_ref") or "")
    if resolve_password and raw_password and not password_ref:
        password_ref = LocalCredentialStore().put("database-config", raw_password)
    credential_status = (
        LocalCredentialStore().inspect(password_ref)
        if password_ref
        else {
            "credential_configured": bool(raw_password),
            "credential_readable": True,
            "credential_error": "",
        }
    )
    password = (
        LocalCredentialStore().get(password_ref)
        if resolve_password and password_ref
        else raw_password if resolve_password else ""
    )
    if resolve_password and not password:
        password = os.getenv("PUDDINGHARNESS_DATABASE_PASSWORD", "")
    if env_mode == "sqlite" or env_provider == "sqlite":
        provider, source = "sqlite", env_source or "local_file"
    elif env_url:
        parsed = urlparse(env_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        host = parsed.hostname or host
        port = int(parsed.port or port)
        db_name = unquote(parsed.path.lstrip("/")) or db_name
        username = unquote(parsed.username or username)
        provider = "postgresql"
        source = "external" if env_source == "external" else (env_source or "bundled")
    elif env_mode in {"postgresql", "bundled", "external"} or env_provider == "postgresql":
        provider = "postgresql"
        if env_mode in {"bundled", "external"}:
            source = env_mode
        elif env_source:
            source = env_source
    assembled_url = ""
    if provider == "postgresql":
        assembled_url = f"postgresql+asyncpg://{quote(username)}:{quote(password)}@{host}:{port}/{quote(db_name)}"
    environment_override = bool(env_url or env_mode or env_provider)
    mode = "sqlite" if provider == "sqlite" else "external" if source == "external" else "bundled"
    return {
        "provider": provider,
        "source": source,
        "mode": mode,
        "catalog_path": str(_config_path().parent / "db" / "catalog.sqlite3") if provider == "sqlite" else "",
        "host": host,
        "port": port,
        "database": db_name,
        "username": username,
        "password": password,
        "password_ref": password_ref,
        "password_configured": bool(credential_status.get("credential_configured") or raw_password or os.getenv("PUDDINGHARNESS_DATABASE_PASSWORD") or env_url),
        "password_readable": bool(credential_status.get("credential_readable", True)),
        "password_error": str(credential_status.get("credential_error") or ""),
        "url": "" if provider == "sqlite" else env_url or configured_url or assembled_url,
        "configured_url": configured_url,
        "configured_by": "environment" if environment_override else "config.json" if raw_database else "default",
        "environment_override": environment_override,
    }


_SUBAGENT_RESERVED_KEYS = {"enabled", "items"}


def _subagent_items_for_display(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if not raw:
        return []
    if "items" in raw:
        raise ValueError("subagents.items is not supported; use a keyed subagents object")
    items: list[dict[str, Any]] = []
    for key, value in raw.items():
        if key in _SUBAGENT_RESERVED_KEYS or not isinstance(value, dict):
            continue
        item = dict(value)
        item["name"] = str(item.get("name") or key)
        item.setdefault("enabled", False)
        item.setdefault("description", "Analyze image inputs and answer questions about them.")
        item.setdefault("model", "")
        item.setdefault("system_prompt", "")
        item.setdefault("route_trigger", "image_input")
        item.setdefault("tools", {"mode": "inherit"})
        item.setdefault("skills", {"mode": "inherit", "paths": []})
        items.append(item)
    return items


def _subagent_config_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"subagent_{index + 1}").strip() or f"subagent_{index + 1}"
        stored = dict(item)
        stored.pop("name", None)
        result[name] = stored
    return result


def _normalize_subagent_config(raw: dict[str, Any]) -> dict[str, Any]:
    return {"items": _subagent_items_for_display(raw)}


def get_settings_for_display() -> dict[str, Any]:
    """Return only generic Harness settings and masked Core DB metadata."""

    from provider_registry import get_provider_registry

    config = load_config()
    database = dict(get_database_config(resolve_password=False))
    database["password"] = ""
    database["url"] = _redact_database_url(str(database.get("url") or ""))
    return {
        "provider_registry": get_provider_registry().display(),
        "database": {**config.get("database", {}), **database},
        "compression": config.get("compression", {}),
        "harness": config.get("harness", {}),
        "subagents": _normalize_subagent_config(config.get("subagents", {})),
    }


def _redact_database_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    authority, suffix = rest.split("@", 1)
    if ":" in authority:
        return f"{scheme}://{authority.split(':', 1)[0]}:***@{suffix}"
    return value


def _update_mcp_config(config: dict[str, Any], update: Any) -> None:
    if not isinstance(update, dict):
        raise ValueError("mcp settings must be an object")
    current = config.setdefault("mcp", {})
    if not isinstance(current, dict):
        current = {}
        config["mcp"] = current
    if "enabled" in update:
        enabled = update["enabled"]
        if not isinstance(enabled, list) or any(not isinstance(name, str) or not name.strip() for name in enabled):
            raise ValueError("mcp.enabled must be an array of server names")
        current["enabled"] = list(dict.fromkeys(enabled))
    if "servers" in update:
        servers = update["servers"]
        if not isinstance(servers, dict):
            raise ValueError("mcp.servers must be an object")
        current["servers"] = copy.deepcopy(servers)
    for key, value in update.items():
        if key not in {"enabled", "servers"}:
            current[key] = copy.deepcopy(value)


def update_settings(updates: dict[str, Any]) -> dict[str, Any] | None:
    """Apply sparse updates for generic Harness settings."""

    if not isinstance(updates, dict):
        raise ValueError("settings update must be an object")
    removed = {"knowledge", "rag", "vanna", "analytics", "tool_intent_router"}.intersection(updates)
    if removed:
        raise ValueError("Business settings are owned outside PuddingHarness: " + ", ".join(sorted(removed)))
    config = load_config()
    previous_provider: str | None = None
    if "database" in updates:
        database_update = updates["database"]
        if not isinstance(database_update, dict):
            raise ValueError("database settings must be an object")
        database = config.setdefault("database", {})
        if not isinstance(database, dict):
            database = {}
            config["database"] = database
        previous_provider = get_database_config(resolve_password=False).get("provider")
        requested_provider = ""
        requested_source = ""
        legacy_mode = str(database_update.get("mode") or "").strip().lower()
        if legacy_mode == "sqlite":
            requested_provider, requested_source = "sqlite", "local_file"
        elif legacy_mode == "external":
            requested_provider, requested_source = "postgresql", "external"
        elif legacy_mode:
            requested_provider, requested_source = "postgresql", "bundled"
        if "provider" in database_update:
            requested_provider = str(database_update.get("provider") or "").strip().lower()
            if requested_provider not in {"sqlite", "postgresql"}:
                raise ValueError("database.provider must be sqlite or postgresql")
        if "source" in database_update:
            requested_source = str(database_update.get("source") or "").strip().lower()
        if requested_provider or requested_source or legacy_mode:
            requested_provider = requested_provider or ("sqlite" if requested_source == "local_file" else "postgresql")
            requested_source = requested_source or ("local_file" if requested_provider == "sqlite" else "external")
            database.pop("mode", None)
            database["provider"] = requested_provider
            database["source"] = requested_source
        for key in ("host", "database", "username", "url"):
            if key in database_update:
                database[key] = str(database_update.get(key) or "").strip()
        if "password" in database_update:
            password = str(database_update.get("password") or "").strip()
            if password:
                from provider_registry import LocalCredentialStore

                database["password_ref"] = LocalCredentialStore().put("database-config", password)
                database["password"] = ""
        if "port" in database_update:
            try:
                database["port"] = int(database_update.get("port") or 5432)
            except (TypeError, ValueError):
                database["port"] = 5432
    if "compression" in updates:
        compression_update = updates["compression"]
        if not isinstance(compression_update, dict):
            raise ValueError("compression settings must be an object")
        compression = config.setdefault("compression", {})
        if "trigger_count" in compression_update:
            compression["trigger_count"] = compression_update["trigger_count"]
        if "deepagents" in compression_update:
            deepagents = compression_update["deepagents"]
            if not isinstance(deepagents, dict):
                raise ValueError("compression.deepagents must be an object")
            summary_update = deepagents.get("summarization")
            if isinstance(summary_update, dict):
                summary_update = dict(summary_update)
                summary_update.pop("summary_input_tokens", None)
                if "model_id" in summary_update:
                    model_id = summary_update["model_id"]
                    if model_id is not None and not isinstance(model_id, str):
                        raise ValueError("summary model ID must be a string")
                    summary_update["model_id"] = str(model_id or "").strip()[:512]
                if "trigger_tokens" in summary_update:
                    trigger = int(summary_update["trigger_tokens"])
                    if not 10000 <= trigger <= 1000000:
                        raise ValueError("summary trigger must be between 10,000 and 1,000,000 tokens")
                    summary_update["trigger_tokens"] = trigger
                if "keep_tokens" in summary_update:
                    keep_tokens = int(summary_update["keep_tokens"])
                    effective_trigger = int(
                        summary_update.get(
                            "trigger_tokens",
                            config["compression"].get("deepagents", {})
                            .get("summarization", {})
                            .get("trigger_tokens", 272000),
                        )
                    )
                    if not 1000 <= keep_tokens < effective_trigger:
                        raise ValueError("summary keep budget must be between 1,000 tokens and the trigger")
                    summary_update["keep_tokens"] = keep_tokens
                deepagents = dict(deepagents)
                deepagents["summarization"] = summary_update
            tool_context = deepagents.get("tool_context")
            if isinstance(tool_context, dict):
                current_tool_context = config["compression"].get("deepagents", {}).get("tool_context", {})
                merged_tool_context = _deep_merge(current_tool_context, tool_context)
                single = int(merged_tool_context.get("single_tool_trigger_tokens", 8000))
                background = int(merged_tool_context.get("background_min_result_tokens", 1000))
                retain_tokens = int(merged_tool_context.get("retain_tool_context_tokens", 32000))
                if not 1000 <= single <= 20000:
                    raise ValueError("single tool threshold must be between 1,000 and 20,000 tokens")
                if not 100 <= background <= 100000:
                    raise ValueError("background tool result threshold must be between 100 and 100,000 tokens")
                if not 1000 <= retain_tokens <= 500000:
                    raise ValueError("tool context retention must be between 1,000 and 500,000 tokens")
                deepagents = dict(deepagents)
                deepagents["tool_context"] = dict(tool_context)
            config["compression"]["deepagents"] = _deep_merge(
                config["compression"].get("deepagents", {}), deepagents
            )
            config["compression"]["deepagents"].get("summarization", {}).pop("summary_input_tokens", None)
        if "middleware" in compression_update:
            middleware = compression_update["middleware"]
            if not isinstance(middleware, dict):
                raise ValueError("compression.middleware must be an object")
            config["compression"]["middleware"] = _deep_merge(
                config["compression"].get("middleware", {}), middleware
            )
    if "write_middleware" in updates:
        if not isinstance(updates["write_middleware"], dict):
            raise ValueError("write_middleware settings must be an object")
        config["write_middleware"] = _deep_merge(config.get("write_middleware", {}), updates["write_middleware"])
    if "harness" in updates:
        config["harness"] = _deep_merge(config.get("harness", {}), _normalize_harness_update(updates["harness"]))
    if "subagents" in updates:
        subagents = updates["subagents"]
        if not isinstance(subagents, dict):
            raise ValueError("subagents settings must be an object")
        config["subagents"] = _subagent_config_from_items(_subagent_items_for_display(subagents))
    if "mcp" in updates:
        _update_mcp_config(config, updates["mcp"])
    save_config(config)
    if previous_provider is not None:
        requested = config.get("database", {}).get("provider")
        if previous_provider == "postgresql" and requested == "sqlite":
            return {"requires_migration": True, "migration_warning": "Switched from PostgreSQL to SQLite; the new catalog starts empty."}
    return None


def _normalize_harness_update(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("harness settings must be an object")
    result = copy.deepcopy(value)
    prompt_cache = result.get("prompt_cache")
    if prompt_cache is not None:
        if not isinstance(prompt_cache, dict):
            raise ValueError("harness.prompt_cache must be an object")
        for key in (
            "trace_part_diagnostics",
            "ordered_system_sections",
            "tail_routing_message",
            "deterministic_session_projection",
            "stable_tool_schema",
        ):
            if key in prompt_cache and not isinstance(prompt_cache[key], bool):
                raise ValueError(f"harness.prompt_cache.{key} must be a boolean")
    model_resilience = result.get("model_resilience")
    if model_resilience is not None:
        if not isinstance(model_resilience, dict):
            raise ValueError("harness.model_resilience must be an object")
        transport_retry = model_resilience.get("transport_retry")
        if transport_retry is not None:
            if not isinstance(transport_retry, dict):
                raise ValueError("harness.model_resilience.transport_retry must be an object")
            if "enabled" in transport_retry and not isinstance(transport_retry["enabled"], bool):
                raise ValueError("harness.model_resilience.transport_retry.enabled must be a boolean")
            max_attempts = transport_retry.get("max_attempts", 2)
            if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 5:
                raise ValueError("harness.model_resilience.transport_retry.max_attempts must be in [1, 5]")
            for key, default, maximum in (("initial_delay_seconds", 0.25, 10.0), ("max_delay_seconds", 2.0, 60.0)):
                item = transport_retry.get(key, default)
                if not isinstance(item, (int, float)) or isinstance(item, bool) or not 0 <= float(item) <= maximum:
                    raise ValueError(f"harness.model_resilience.transport_retry.{key} is out of range")
                transport_retry[key] = float(item)
            if transport_retry["max_delay_seconds"] < transport_retry["initial_delay_seconds"]:
                raise ValueError("transport retry max delay must be >= initial delay")
        terminal_response = model_resilience.get("terminal_response")
        if terminal_response is not None:
            if not isinstance(terminal_response, dict):
                raise ValueError("harness.model_resilience.terminal_response must be an object")
            if "enabled" in terminal_response and not isinstance(terminal_response["enabled"], bool):
                raise ValueError("harness.model_resilience.terminal_response.enabled must be a boolean")
            attempts = terminal_response.get("max_recovery_attempts", 1)
            if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 3:
                raise ValueError("harness.model_resilience.terminal_response.max_recovery_attempts must be in [0, 3]")
    goals = result.get("goals")
    if goals is not None:
        if not isinstance(goals, dict):
            raise ValueError("harness.goals must be an object")
        goals.update({"activation": "explicit_user_only", "default_enabled": False, "auto_promote_from_run": False})
        max_rounds = goals.get("max_rounds", 8)
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= 100:
            raise ValueError("harness.goals.max_rounds must be in [1, 100]")
    completion = result.get("completion")
    if completion is not None:
        if not isinstance(completion, dict):
            raise ValueError("harness.completion must be an object")
        run_review = completion.get("run_review")
        if run_review is not None:
            if not isinstance(run_review, dict):
                raise ValueError("harness.completion.run_review must be an object")
            policy = str(run_review.get("policy") or "off")
            if policy not in {"off", "shadow"}:
                raise ValueError("harness.completion.run_review.policy must be off or shadow")
            run_review["policy"] = policy
            model = run_review.get("model", "")
            if not isinstance(model, str) or len(model.strip()) > 200:
                raise ValueError("harness.completion.run_review.model must be a short string")
            run_review["model"] = model.strip()
            profile = str(run_review.get("environment_profile") or "none")
            if profile not in {"none", "deterministic_only", "independent_evidence_review", "environment_verified"}:
                raise ValueError("harness.completion.run_review.environment_profile is invalid")
            run_review["environment_profile"] = profile
            run_review["manual_enabled"] = bool(run_review.get("manual_enabled", True))
        rubric = completion.get("rubric")
        if rubric is not None:
            if not isinstance(rubric, dict):
                raise ValueError("harness.completion.rubric must be an object")
            for key, default, minimum, maximum in (("max_iterations", 2, 1, 20), ("max_stagnant_repairs", 2, 1, 20)):
                item = rubric.get(key, default)
                if not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum:
                    raise ValueError(f"harness.completion.rubric.{key} is out of range")
            model = rubric.get("model", "")
            if not isinstance(model, str) or len(model.strip()) > 200:
                raise ValueError("harness.completion.rubric.model must be a short string")
            rubric["model"] = model.strip()
            profile = str(rubric.get("environment_profile") or "none")
            if profile not in {"none", "deterministic_only", "independent_evidence_review", "environment_verified"}:
                raise ValueError("harness.completion.rubric.environment_profile is invalid")
            rubric["environment_profile"] = profile
            rules = rubric.get("custom_rules", [])
            if not isinstance(rules, list) or len(rules) > 50:
                raise ValueError("harness.completion.rubric.custom_rules must contain at most 50 rules")
            normalized_rules = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"custom_rules[{index}] must be an object")
                statement = str(rule.get("statement") or "").strip()
                verifier = str(rule.get("verifier") or "llm_grader")
                if not statement or len(statement) > 1000 or verifier != "llm_grader":
                    raise ValueError(f"custom_rules[{index}] is invalid")
                normalized_rules.append({
                    "id": str(rule.get("id") or f"custom_{index + 1}")[:100],
                    "enabled": bool(rule.get("enabled", True)),
                    "statement": statement,
                    "required": bool(rule.get("required", True)),
                    "verifier": verifier,
                })
            rubric["custom_rules"] = normalized_rules
    terminal = result.get("terminal")
    if terminal is not None:
        if not isinstance(terminal, dict):
            raise ValueError("harness.terminal must be an object")
        legacy_keys = sorted(_LEGACY_TERMINAL_EXECUTION_KEYS.intersection(terminal))
        if legacy_keys:
            raise UnsupportedTerminalExecutionConfig(
                "harness.terminal uses removed execution fields: " + ", ".join(legacy_keys)
            )
        if terminal.get("execution_mode", "spawn") not in {"spawn", "kernel"}:
            raise ValueError("harness.terminal.execution_mode must be spawn or kernel")
        timeout = terminal.get("default_timeout_seconds", 120)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ValueError("harness.terminal.default_timeout_seconds must be in [1, 3600]")
        docker = terminal.get("docker")
        if docker is not None:
            if not isinstance(docker, dict):
                raise ValueError("harness.terminal.docker must be an object")
            docker["lifecycle"] = "project"
            for key, minimum, maximum in (
                ("probe_timeout_seconds", 1, 30),
                ("memory_limit_mb", 128, 131072),
                ("pids_limit", 16, 4096),
                ("idle_stop_minutes", 1, 10080),
            ):
                item = docker.get(key)
                if item is not None and (
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or not minimum <= item <= maximum
                ):
                    raise ValueError(f"harness.terminal.docker.{key} is out of range")
            image = str(docker.get("image") or "").strip()
            if not image:
                raise ValueError("harness.terminal.docker.image cannot be empty")
            docker["image"] = image
            if "dependency_setup_enabled" in docker:
                docker["dependency_setup_enabled"] = bool(docker["dependency_setup_enabled"])
                docker["dependency_setup_opt_in_version"] = 1
    return result


def get_write_middleware_config() -> dict[str, Any]:
    return load_config().get("write_middleware", copy.deepcopy(_DEFAULT_CONFIG["write_middleware"]))


def get_max_history_messages() -> int:
    return int(load_config().get("compression", {}).get("max_history_messages", 100))


def get_context_window() -> int:
    return int(get_fallback_llm_config().get("context_window", 1000000))


def get_compaction_trigger_tokens() -> int:
    return int(load_config().get("compression", {}).get("middleware", {}).get("compaction", {}).get("trigger_tokens", 500000))


def get_deepagents_summarization_config() -> dict[str, Any]:
    result = dict(load_config().get("compression", {}).get("deepagents", {}).get("summarization", {}))
    if not result:
        result = copy.deepcopy(_DEFAULT_CONFIG["compression"]["deepagents"]["summarization"])
    result["summary_input_tokens"] = _deepagents_summary_input_tokens(result)
    return result


def get_deepagents_tool_context_config() -> dict[str, Any]:
    result = load_config().get("compression", {}).get("deepagents", {}).get("tool_context", {})
    return dict(result or _DEFAULT_CONFIG["compression"]["deepagents"]["tool_context"])
