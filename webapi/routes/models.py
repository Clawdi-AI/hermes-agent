import time
from typing import Optional

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from webapi.deps import get_runtime_model
from webapi.models.models import (
    AvailableModel,
    AvailableModelsResponse,
    OpenAIModelInfo,
    OpenAIModelsResponse,
)

router = APIRouter()


@router.get("/v1/models", response_model=OpenAIModelsResponse)
async def list_models() -> OpenAIModelsResponse:
    runtime_model = get_runtime_model()
    now = int(time.time())
    return OpenAIModelsResponse(
        data=[
            OpenAIModelInfo(
                id="hermes-agent",
                created=now,
                owned_by="hermes",
                root="hermes-agent",
                parent=None,
                runtime_model=runtime_model,
            ),
            OpenAIModelInfo(
                id=runtime_model,
                created=now,
                owned_by="runtime",
                root=runtime_model,
                parent="hermes-agent",
            ),
        ],
    )


def _resolve_models(effective_provider: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Synchronous helper that hits the provider catalog.

    ``hermes_cli.models`` performs blocking HTTP calls (``urllib.request.urlopen``)
    against provider model-list endpoints with a multi-second timeout.
    Calling that from an ``async def`` route directly would stall the
    entire FastAPI worker for the duration of the slowest probe — one
    misbehaving provider could pin every request. Wrap in a threadpool.
    """
    from hermes_cli.models import (
        curated_models_for_provider,
        list_available_providers,
    )

    return (
        curated_models_for_provider(effective_provider),
        list_available_providers(),
    )


@router.get("/api/available-models", response_model=AvailableModelsResponse)
async def available_models(
    provider: Optional[str] = Query(None),
) -> AvailableModelsResponse:
    """Return available models for a provider.

    Uses the same resolution as ``hermes setup``: live API query first,
    then static catalog fallback. If ``provider`` is omitted, uses the
    currently configured provider.
    """
    effective_provider = provider
    if not effective_provider:
        from webapi.deps import get_runtime_agent_kwargs
        runtime = get_runtime_agent_kwargs()
        effective_provider = runtime.get("provider", "anthropic")

    models, providers = await run_in_threadpool(_resolve_models, effective_provider)

    return AvailableModelsResponse(
        provider=effective_provider,
        models=[AvailableModel(id=m[0], description=m[1]) for m in models],
        providers=providers,
    )
