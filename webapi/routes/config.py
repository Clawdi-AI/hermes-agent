import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from hermes_cli.config import (
    get_env_value,
    load_config,
    save_config,
    save_env_value,
    remove_env_value,
)
from webapi.deps import get_config, get_runtime_agent_kwargs, get_runtime_model
from webapi.models.config import ConfigPatchResponse, ConfigResponse


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/config", tags=["config"])


# Top-level sections of ~/.hermes/config.yaml that dashboards may need to
# read/write. Each platform section has its own nested shape (see
# website/docs/user-guide/configuration.md) — we accept an opaque dict here
# and merge it into the existing config, so clients can ship whatever shape
# Hermes supports without requiring a webapi release every time a new field
# lands in the agent.
_MERGEABLE_SECTIONS = (
    # Messaging platforms
    "discord",
    "telegram",
    "slack",
    "whatsapp",
    "matrix",
    "mattermost",
    "signal",
    "sms",
    "email",
    "feishu",
    "dingtalk",
    "wecom",
    "bluebubbles",
    "homeassistant",
    "webhook",
    # Cross-cutting subsystems
    "security",
    "memory",
    "cron",
    "display",
    "toolsets",
    # Hermes stores MCP server definitions under `mcp_servers` (not `mcp`)
    # — see hermes_cli/mcp_config.py:8 + :81.
    "mcp_servers",
)


def _deep_merge(dst: dict, src: dict) -> dict:
    """Recursively merge ``src`` into ``dst``.

    Rules:
    - Dict values are merged recursively.
    - ``None`` values in ``src`` delete the key from ``dst`` (use this to
      clear a field rather than setting it to empty).
    - All other values overwrite the existing value.
    """
    for key, value in src.items():
        if value is None:
            dst.pop(key, None)
            continue
        existing = dst.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            dst[key] = _deep_merge(dict(existing), value)
        else:
            dst[key] = value
    return dst


class ConfigPatch(BaseModel):
    """Partial update for ``~/.hermes/config.yaml``.

    The top-level ``model``, ``provider``, ``base_url`` shortcuts are kept for
    backwards compatibility. Nested platform / subsystem sections can be
    patched by setting the corresponding field to a dict; that dict is
    deep-merged into the current config. Setting a nested key's value to
    ``null`` inside such a dict deletes it.
    """

    model: str | None = None
    provider: str | None = None
    base_url: str | None = None

    # Messaging platforms
    discord: dict[str, Any] | None = None
    telegram: dict[str, Any] | None = None
    slack: dict[str, Any] | None = None
    whatsapp: dict[str, Any] | None = None
    matrix: dict[str, Any] | None = None
    mattermost: dict[str, Any] | None = None
    signal: dict[str, Any] | None = None
    sms: dict[str, Any] | None = None
    email: dict[str, Any] | None = None
    feishu: dict[str, Any] | None = None
    dingtalk: dict[str, Any] | None = None
    wecom: dict[str, Any] | None = None
    bluebubbles: dict[str, Any] | None = None
    homeassistant: dict[str, Any] | None = None
    webhook: dict[str, Any] | None = None

    # Cross-cutting subsystems
    security: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    cron: dict[str, Any] | None = None
    display: dict[str, Any] | None = None
    toolsets: dict[str, Any] | None = None
    # Hermes stores MCP server definitions under `mcp_servers` (not `mcp`)
    # — see hermes_cli/mcp_config.py:8 + :81.
    mcp_servers: dict[str, Any] | None = None


def _build_credential_status() -> dict[str, dict[str, bool]]:
    """Check which platform credentials are set in .env (without exposing values)."""
    status: dict[str, dict[str, bool]] = {}
    for platform, cred_map in _CREDENTIAL_ENV_MAP.items():
        plat_status: dict[str, bool] = {}
        for field_name, env_var in cred_map.items():
            plat_status[field_name] = bool(get_env_value(env_var))
        status[platform] = plat_status
    return status


@router.get("", response_model=ConfigResponse)
async def get_web_config() -> ConfigResponse:
    runtime = get_runtime_agent_kwargs()
    raw_model = get_runtime_model()
    # Upstream now returns dict {'default': 'model', 'provider': 'x'} instead of str
    if isinstance(raw_model, dict):
        model_str = raw_model.get("default", raw_model.get("model", str(raw_model)))
    else:
        model_str = raw_model

    cfg = get_config()
    # Inject credential status into the config dict so the dashboard
    # can show configured/unconfigured state without exposing secrets.
    # This is merged into the ``config`` field (not a separate key) so
    # the existing ``hermesConfigToDeploymentConfig`` mapper in the
    # frontend picks it up transparently via ``cfg.telegram.bot_token``
    # being truthy/falsy.
    cred_status = await run_in_threadpool(_build_credential_status)
    enriched = dict(cfg)
    for platform, fields in cred_status.items():
        section = dict(enriched.get(platform, {})) if isinstance(enriched.get(platform), dict) else {}
        for field_name, is_set in fields.items():
            # Only inject if the field is NOT already in config.yaml
            # (env var is the source of truth for credentials)
            if field_name not in section:
                section[field_name] = is_set  # True/False, not the actual value
        enriched[platform] = section

    return ConfigResponse(
        model=model_str,
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        base_url=runtime.get("base_url"),
        config=enriched,
    )


# Mapping from config.yaml platform field names → .env var names.
# Hermes reads credentials from env vars (via ~/.hermes/.env), NOT from
# config.yaml. When a dashboard sends `{telegram: {bot_token: "xxx"}}`,
# we extract the credential fields, write them to .env, and only merge
# the remaining behaviour fields (require_mention, etc.) into config.yaml.
_CREDENTIAL_ENV_MAP: dict[str, dict[str, str]] = {
    "telegram": {
        "bot_token": "TELEGRAM_BOT_TOKEN",
        "allowed_usernames": "TELEGRAM_ALLOWED_USERS",
        "home_channel": "TELEGRAM_HOME_CHANNEL",
    },
    "discord": {
        "bot_token": "DISCORD_BOT_TOKEN",
        "allowed_usernames": "DISCORD_ALLOWED_USERS",
        "home_channel": "DISCORD_HOME_CHANNEL",
    },
    "slack": {
        "bot_token": "SLACK_BOT_TOKEN",
        "app_token": "SLACK_APP_TOKEN",
        "allowed_usernames": "SLACK_ALLOWED_USERS",
        "home_channel": "SLACK_HOME_CHANNEL",
    },
    "feishu": {
        "app_id": "FEISHU_APP_ID",
        "app_secret": "FEISHU_APP_SECRET",
        "allowed_open_ids": "FEISHU_ALLOWED_USERS",
        "home_channel": "FEISHU_HOME_CHANNEL",
    },
    "dingtalk": {
        "client_id": "DINGTALK_CLIENT_ID",
        "client_secret": "DINGTALK_CLIENT_SECRET",
    },
    "whatsapp": {
        "allowed_usernames": "WHATSAPP_ALLOWED_USERS",
    },
    "matrix": {
        "access_token": "MATRIX_ACCESS_TOKEN",
        "homeserver": "MATRIX_HOMESERVER",
        "user_id": "MATRIX_USER_ID",
        "allowed_usernames": "MATRIX_ALLOWED_USERS",
    },
}


def _extract_credentials(
    section: str, section_patch: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """Split a platform section patch into (yaml_fields, env_updates).

    Returns:
        yaml_fields: dict to deep-merge into config.yaml (behaviour only)
        env_updates: dict of {ENV_VAR: value_or_None} to write to .env
    """
    cred_map = _CREDENTIAL_ENV_MAP.get(section, {})
    yaml_fields: dict[str, Any] = {}
    env_updates: dict[str, str | None] = {}

    for key, value in section_patch.items():
        env_var = cred_map.get(key)
        if env_var is not None:
            # Credential field → route to .env
            if value is None:
                env_updates[env_var] = None  # delete
            elif isinstance(value, list):
                # Arrays (allowed_usernames, allowed_open_ids) → comma-joined
                env_updates[env_var] = ",".join(str(v) for v in value)
            else:
                env_updates[env_var] = str(value)
        else:
            # Behaviour field → keep in config.yaml
            yaml_fields[key] = value

    return yaml_fields, env_updates


def _apply_config_patch(patch: ConfigPatch) -> ConfigPatchResponse:
    """Pure-sync routine that loads, merges, and saves the YAML config.

    Credential fields (bot tokens, API keys, allowed user lists) are
    extracted from platform sections and written to ``~/.hermes/.env``
    via ``save_env_value()``. Only behaviour fields (require_mention,
    auto_thread, etc.) are deep-merged into config.yaml.

    Wrapped in ``run_in_threadpool`` by the route handler so the
    blocking disk IO doesn't stall the event loop.
    """
    config = load_config()

    # Top-level shortcuts (backwards compatible with older clients)
    if patch.model is not None:
        config["model"] = patch.model
    if patch.provider is not None:
        config["provider"] = patch.provider
    if patch.base_url is not None:
        if patch.base_url.strip():
            config["base_url"] = patch.base_url.strip()
        else:
            config.pop("base_url", None)  # empty string = remove it

    # Nested sections: deep-merge any that were supplied
    patch_dict = patch.model_dump(exclude_none=True)
    env_changed = False
    for section in _MERGEABLE_SECTIONS:
        section_patch = patch_dict.get(section)
        if section_patch is None:
            continue

        # Split credential fields → .env, behaviour fields → config.yaml
        yaml_fields, env_updates = _extract_credentials(section, section_patch)

        # Write credentials to .env
        for env_var, env_val in env_updates.items():
            if env_val is None:
                remove_env_value(env_var)
                os.environ.pop(env_var, None)
            else:
                save_env_value(env_var, env_val)
                os.environ[env_var] = env_val
            env_changed = True

        # Merge remaining behaviour fields into config.yaml
        if yaml_fields:
            existing = config.get(section)
            if not isinstance(existing, dict):
                existing = {}
            config[section] = _deep_merge(existing, yaml_fields)

    save_config(config)
    return ConfigPatchResponse(
        model=config.get("model"),
        provider=config.get("provider"),
        base_url=config.get("base_url"),
        merged_sections=[
            section for section in _MERGEABLE_SECTIONS if patch_dict.get(section) is not None
        ],
    )


@router.patch("", response_model=ConfigPatchResponse)
async def patch_web_config(patch: ConfigPatch) -> ConfigPatchResponse:
    """Patch ``~/.hermes/config.yaml`` with the provided fields.

    Top-level model/provider/base_url keys are set directly. Any
    platform or subsystem section (e.g. ``telegram``, ``discord``,
    ``security``) is deep-merged into the existing config — you only
    need to send the fields you want to change. Setting a nested value
    to ``null`` removes it; setting ``base_url`` to an empty string
    removes the top-level base URL override.
    """
    try:
        return await run_in_threadpool(_apply_config_patch, patch)
    except Exception:
        # Don't echo raw filesystem / YAML exception text to the
        # browser — paths and parser internals are operator-only.
        logger.exception("[webapi.config] patch_web_config failed")
        raise HTTPException(status_code=500, detail="Failed to update config")
