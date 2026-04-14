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


# ---------------------------------------------------------------------------
# Credential → env var mapping
# ---------------------------------------------------------------------------
# Hermes reads platform credentials from env vars (via ~/.hermes/.env), NOT
# from config.yaml. When a dashboard sends {telegram: {bot_token: "xxx"}},
# we extract credential fields, write them to .env via save_env_value(),
# and only deep-merge the remaining behaviour fields into config.yaml.
#
# The gateway's _apply_env_overrides() in gateway/config.py reads each
# platform's credentials exclusively from os.getenv / get_env_value.
# Config.yaml's platform sections only carry behaviour settings like
# require_mention, auto_thread, free_response_channels, etc.
#
# The `enabled` field is special: the gateway determines "enabled" solely
# by whether the credential env var is set (e.g. TELEGRAM_BOT_TOKEN).
# There is no config.yaml `enabled` flag the gateway respects. We drop
# `enabled` from the patch entirely — it would be dead config.
# ---------------------------------------------------------------------------

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
        "encrypt_key": "FEISHU_ENCRYPT_KEY",
        "verification_token": "FEISHU_VERIFICATION_TOKEN",
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
        "password": "MATRIX_PASSWORD",
        "device_id": "MATRIX_DEVICE_ID",
        "allowed_usernames": "MATRIX_ALLOWED_USERS",
        "home_room": "MATRIX_HOME_ROOM",
    },
    "mattermost": {
        "token": "MATTERMOST_TOKEN",
        "url": "MATTERMOST_URL",
        "allowed_usernames": "MATTERMOST_ALLOWED_USERS",
        "home_channel": "MATTERMOST_HOME_CHANNEL",
    },
    "signal": {
        "http_url": "SIGNAL_HTTP_URL",
        "account": "SIGNAL_ACCOUNT",
        "allowed_usernames": "SIGNAL_ALLOWED_USERS",
        "home_channel": "SIGNAL_HOME_CHANNEL",
    },
    "email": {
        "address": "EMAIL_ADDRESS",
        "password": "EMAIL_PASSWORD",
        "imap_host": "EMAIL_IMAP_HOST",
        "smtp_host": "EMAIL_SMTP_HOST",
        "allowed_usernames": "EMAIL_ALLOWED_USERS",
        "home_address": "EMAIL_HOME_ADDRESS",
    },
    "sms": {
        "account_sid": "TWILIO_ACCOUNT_SID",
        "auth_token": "TWILIO_AUTH_TOKEN",
        "phone_number": "TWILIO_PHONE_NUMBER",
        "allowed_usernames": "SMS_ALLOWED_USERS",
        "home_channel": "SMS_HOME_CHANNEL",
    },
    "wecom": {
        "bot_id": "WECOM_BOT_ID",
        "secret": "WECOM_SECRET",
        "websocket_url": "WECOM_WEBSOCKET_URL",
        "allowed_usernames": "WECOM_ALLOWED_USERS",
        "home_channel": "WECOM_HOME_CHANNEL",
    },
    "homeassistant": {
        "token": "HASS_TOKEN",
        "url": "HASS_URL",
        "allowed_usernames": "HASS_ALLOWED_USERS",
    },
    "webhook": {
        "secret": "WEBHOOK_SECRET",
    },
}

# Fields silently dropped from platform patches — the gateway ignores
# config.yaml `enabled`; platform enablement is determined solely by
# whether the credential env var is set.
_IGNORED_FIELDS = frozenset({"enabled"})


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


# Fields whose .env values are safe to expose as-is (not secrets).
# All other credential fields (tokens, keys) are returned as boolean
# true/false to avoid leaking secrets.
#
# _ARRAY_FIELDS: comma-separated in .env, returned as array to frontend
# _STRING_FIELDS: single string value, returned as-is
_ARRAY_CREDENTIAL_FIELDS: frozenset[str] = frozenset({
    "allowed_usernames",
    "allowed_open_ids",
})
_STRING_CREDENTIAL_FIELDS: frozenset[str] = frozenset({
    "home_channel",
    "home_room",
    "home_address",
    "url",
    "homeserver",
})


# ---------------------------------------------------------------------------
# Credential status for GET
# ---------------------------------------------------------------------------

def _build_credential_enrichment() -> dict[str, dict[str, Any]]:
    """Build credential info to inject into GET /api/config response.

    - Secret fields (tokens, keys): boolean true/false (never expose value)
    - List fields (allowed users, home channel): actual value as array/string
      so the frontend can display and re-edit them
    """
    result: dict[str, dict[str, Any]] = {}
    for platform, cred_map in _CREDENTIAL_ENV_MAP.items():
        plat_data: dict[str, Any] = {}
        for field_name, env_var in cred_map.items():
            raw = get_env_value(env_var)
            if field_name in _ARRAY_CREDENTIAL_FIELDS:
                # Comma-separated list → always return as array so
                # frontend Array.isArray() works consistently.
                if not raw:
                    plat_data[field_name] = None
                else:
                    plat_data[field_name] = [v.strip() for v in raw.split(",") if v.strip()]
            elif field_name in _STRING_CREDENTIAL_FIELDS:
                # Single string value — safe to expose as-is
                plat_data[field_name] = raw or None
            else:
                # Secret field — boolean only
                plat_data[field_name] = bool(raw)
        result[platform] = plat_data
    return result


@router.get("", response_model=ConfigResponse)
async def get_web_config() -> ConfigResponse:
    runtime = get_runtime_agent_kwargs()
    raw_model = get_runtime_model()
    if isinstance(raw_model, dict):
        model_str = raw_model.get("default", raw_model.get("model", str(raw_model)))
    else:
        model_str = raw_model

    cfg = get_config()
    # Inject credential status (true/false) into the response so the
    # dashboard can show configured/unconfigured state without exposing
    # secrets. Always uses the .env status as source of truth — even if
    # an old config.yaml had a plaintext credential, the boolean from
    # .env takes precedence.
    cred_data = await run_in_threadpool(_build_credential_enrichment)
    enriched = dict(cfg)
    for platform, fields in cred_data.items():
        section = dict(enriched.get(platform, {})) if isinstance(enriched.get(platform), dict) else {}
        for field_name, value in fields.items():
            # Always overwrite — .env is the source of truth for
            # credentials. Prevents leaking plaintext tokens that
            # might linger in an old config.yaml.
            section[field_name] = value
        enriched[platform] = section

    return ConfigResponse(
        model=model_str,
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        base_url=runtime.get("base_url"),
        config=enriched,
    )


# ---------------------------------------------------------------------------
# PATCH helpers
# ---------------------------------------------------------------------------

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
        if key in _IGNORED_FIELDS:
            continue
        env_var = cred_map.get(key)
        if env_var is not None:
            if value is None:
                env_updates[env_var] = None  # delete
            elif isinstance(value, list):
                env_updates[env_var] = ",".join(str(v) for v in value)
            else:
                env_updates[env_var] = str(value)
        else:
            yaml_fields[key] = value

    return yaml_fields, env_updates


def _apply_config_patch(patch: ConfigPatch) -> ConfigPatchResponse:
    """Apply a config patch: credentials → .env, behaviour → config.yaml.

    Only writes config.yaml when there are actual YAML changes (top-level
    model/provider/base_url or behaviour fields in platform sections).
    Credential-only patches skip the YAML write entirely and go straight
    to .env — this avoids triggering save_config's normalize + atomic
    write when the config dict hasn't actually changed.
    """
    yaml_dirty = False
    config = load_config()

    if patch.model is not None:
        config["model"] = patch.model
        yaml_dirty = True
    if patch.provider is not None:
        config["provider"] = patch.provider
        yaml_dirty = True
    if patch.base_url is not None:
        if patch.base_url.strip():
            config["base_url"] = patch.base_url.strip()
        else:
            config.pop("base_url", None)
        yaml_dirty = True

    patch_dict = patch.model_dump(exclude_none=True)
    pending_env: list[tuple[str, str | None]] = []

    for section in _MERGEABLE_SECTIONS:
        section_patch = patch_dict.get(section)
        if section_patch is None:
            continue

        yaml_fields, env_updates = _extract_credentials(section, section_patch)

        for env_var, env_val in env_updates.items():
            pending_env.append((env_var, env_val))

        if yaml_fields:
            existing = config.get(section)
            if not isinstance(existing, dict):
                existing = {}
            config[section] = _deep_merge(existing, yaml_fields)
            yaml_dirty = True

    # Step 1: save config.yaml only if there are actual YAML changes
    if yaml_dirty:
        save_config(config)

    # Step 2: write credentials to .env
    for env_var, env_val in pending_env:
        if env_val is None:
            remove_env_value(env_var)
            os.environ.pop(env_var, None)
        else:
            save_env_value(env_var, env_val)
            os.environ[env_var] = env_val

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

    Credential fields (bot_token, app_id, etc.) within platform sections
    are automatically routed to ``~/.hermes/.env`` instead of config.yaml,
    because the gateway reads credentials from env vars exclusively.
    """
    try:
        return await run_in_threadpool(_apply_config_patch, patch)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.exception("[webapi.config] patch_web_config failed")
        # Temporarily include traceback for debugging CVM 500 errors.
        # TODO: remove before production — only expose generic message.
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update config: {type(exc).__name__}: {exc}\n{tb}",
        )
