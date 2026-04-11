import os

import uvicorn


# The Clawdi agent-image controller (TypeScript reverse proxy) hard-codes
# 8643 as the upstream port for ``/_hermes/*``, and the supervisor config
# pins ``HERMES_WEBAPI_PORT=8643`` inside the pod. The previous fallback
# to 8642 only ever caused local-dev confusion when the env var wasn't
# set — running the bare ``python -m webapi`` would bind 8642 while a
# co-located controller probed 8643 and got 404s. Single source of truth.
DEFAULT_PORT = 8643


def main() -> None:
    host = os.getenv("HERMES_WEBAPI_HOST", "127.0.0.1")
    port = int(os.getenv("HERMES_WEBAPI_PORT") or DEFAULT_PORT)
    print(f"Starting Hermes WebAPI on {host}:{port}")
    uvicorn.run("webapi.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
