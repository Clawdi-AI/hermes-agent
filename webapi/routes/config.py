from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hermes_cli.config import load_config, save_config
from webapi.deps import get_config, get_runtime_agent_kwargs, get_runtime_model
from webapi.models.config import ConfigPatchResponse, ConfigResponse


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


@router.get("", response_model=ConfigResponse)
async def get_web_config() -> ConfigResponse:
    runtime = get_runtime_agent_kwargs()
    raw_model = get_runtime_model()
    # Upstream now returns dict {'default': 'model', 'provider': 'x'} instead of str
    if isinstance(raw_model, dict):
        model_str = raw_model.get("default", raw_model.get("model", str(raw_model)))
    else:
        model_str = raw_model
    return ConfigResponse(
        model=model_str,
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        base_url=runtime.get("base_url"),
        config=get_config(),
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
        for section in _MERGEABLE_SECTIONS:
            section_patch = patch_dict.get(section)
            if section_patch is None:
                continue
            existing = config.get(section)
            if not isinstance(existing, dict):
                existing = {}
            config[section] = _deep_merge(existing, section_patch)

        save_config(config)
        return ConfigPatchResponse(
            model=config.get("model"),
            provider=config.get("provider"),
            base_url=config.get("base_url"),
            merged_sections=[
                section for section in _MERGEABLE_SECTIONS if patch_dict.get(section) is not None
            ],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
