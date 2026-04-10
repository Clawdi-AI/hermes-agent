from typing import Any

from webapi.models.common import WebAPIModel


class ConfigResponse(WebAPIModel):
    model: str | dict[str, Any] | None = None
    provider: str | None = None
    api_mode: str | None = None
    base_url: str | None = None
    config: dict[str, Any]


class ConfigPatchResponse(WebAPIModel):
    """``PATCH /api/config`` ack. Returns the post-merge top-level shortcuts
    plus the list of nested sections the server actually deep-merged so
    the client can reconcile its optimistic UI.
    """

    ok: bool = True
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    merged_sections: list[str] = []
