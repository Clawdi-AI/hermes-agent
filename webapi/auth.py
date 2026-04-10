"""Bearer token authentication for the webapi module.

The middleware is controlled by the ``HERMES_API_TOKEN`` environment variable:

- If unset or empty, authentication is disabled (dev/localhost default).
- If set, every incoming request must carry ``Authorization: Bearer <token>``
  where the token matches ``HERMES_API_TOKEN`` exactly.

This is intentionally minimal — it's designed for deployments where Hermes
sits behind a public HTTPS endpoint and the operator wants a single shared
secret per-instance. For per-user multi-tenant isolation, each user should
get their own Hermes instance with its own ``HERMES_API_TOKEN`` value.

The ``/health`` route is intentionally NOT protected so load balancers and
uptime probes can reach it without credentials.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def _expected_token() -> str:
    return os.getenv("HERMES_API_TOKEN", "").strip()


async def verify_bearer_token(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency that validates a bearer token header.

    Raises 401 when a token is configured and the request does not present
    a matching ``Authorization: Bearer <token>`` header. Uses
    ``hmac.compare_digest`` to avoid timing side channels.
    """
    expected = _expected_token()
    if not expected:
        # Auth disabled — noop.
        return

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, provided = authorization.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization scheme; expected Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
