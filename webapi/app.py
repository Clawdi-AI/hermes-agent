import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from webapi.auth import verify_bearer_token
from webapi.errors import register_error_handlers
from webapi.routes.chat import router as chat_router
from webapi.routes.config import router as config_router
from webapi.routes.health import router as health_router
from webapi.routes.jobs import router as jobs_router
from webapi.routes.memory import router as memory_router
from webapi.routes.models import router as models_router
from webapi.routes.sessions import router as sessions_router
from webapi.routes.skills import router as skills_router

# In production webapi is only reached via the agent-image controller
# at 127.0.0.1:19000 (hermes-workspace Node UI) or externally through
# the controller's `/_hermes/*` proxy (which enforces its own CORS).
# The default list just unblocks local dev against `bun run dev` on
# :3000. Anything else should be set explicitly via HERMES_CORS_ORIGINS.
_DEFAULT_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


def _get_cors_origins() -> list[str]:
    extra = os.environ.get("HERMES_CORS_ORIGINS", "").strip()
    if extra:
        return [o.strip() for o in extra.split(",") if o.strip()]
    return list(_DEFAULT_ORIGINS)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hermes Web API",
        version="0.1.0",
    )

    # CORS:
    # - credentials=False because webapi uses a bearer header, never a
    #   cookie. `allow_credentials=True` + `allow_origins=["*"]` is an
    #   invalid combination that browsers reject on preflight, and we
    #   don't need cookie-scoped credentials anyway.
    # - headers enumerated explicitly: "Authorization" (bearer) and
    #   "Content-Type" (JSON body). Wildcard headers with credentials
    #   is also spec-invalid.
    # - methods enumerated to the set the routes actually handle.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    register_error_handlers(app)

    # Health is unauthenticated so load balancers / probes can reach it.
    app.include_router(health_router)

    # Everything else is gated by HERMES_API_TOKEN (no-op if env var unset).
    protected = [Depends(verify_bearer_token)]
    app.include_router(models_router, dependencies=protected)
    app.include_router(sessions_router, dependencies=protected)
    app.include_router(chat_router, dependencies=protected)
    app.include_router(memory_router, dependencies=protected)
    app.include_router(skills_router, dependencies=protected)
    app.include_router(config_router, dependencies=protected)
    app.include_router(jobs_router, dependencies=protected)

    return app


app = create_app()
