from webapi.models.common import WebAPIModel


class OpenAIModelInfo(WebAPIModel):
    """OpenAI-compatible model-catalog entry returned by ``GET /v1/models``."""

    id: str
    object: str = "model"
    created: int
    owned_by: str
    permission: list[dict] = []
    root: str
    parent: str | None = None
    runtime_model: str | None = None


class OpenAIModelsResponse(WebAPIModel):
    object: str = "list"
    data: list[OpenAIModelInfo]


class AvailableModel(WebAPIModel):
    id: str
    description: str | None = None


class AvailableModelsResponse(WebAPIModel):
    """``GET /api/available-models`` — curated model list for the current
    (or explicitly-requested) provider, plus the list of all available
    providers for dashboard dropdowns.
    """

    provider: str
    models: list[AvailableModel]
    providers: list[str]
