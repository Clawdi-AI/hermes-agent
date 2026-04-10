from webapi.models.common import WebAPIModel


class HealthResponse(WebAPIModel):
    """Public ``GET /health`` payload — consumed by k8s probes + load balancers."""

    status: str = "ok"
    platform: str = "hermes-agent"
    service: str = "webapi"
