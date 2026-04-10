from typing import Any

from webapi.models.common import WebAPIModel


class JobCreateRequest(WebAPIModel):
    """Request body for ``POST /api/jobs``.

    Matches ``cron.jobs.create_job`` — see that function for field semantics.
    ``name`` is optional at the cron layer but required by the dashboard UX,
    so we enforce it here.
    """

    name: str
    schedule: str
    prompt: str = ""
    deliver: str = "local"
    skills: list[str] | None = None
    skill: str | None = None
    repeat: int | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    script: str | None = None


class JobUpdateRequest(WebAPIModel):
    """Request body for ``PATCH /api/jobs/{job_id}``.

    Only the fields listed here are accepted — any other keys are ignored
    to prevent clients from injecting arbitrary state onto persisted jobs.
    """

    name: str | None = None
    schedule: str | None = None
    prompt: str | None = None
    deliver: str | None = None
    skills: list[str] | None = None
    skill: str | None = None
    repeat: int | None = None
    enabled: bool | None = None


class JobResponse(WebAPIModel):
    """Wrapper around a single cron job dict.

    The job shape itself is opaque at the HTTP layer — it mirrors whatever
    ``cron.jobs`` returns, which includes fields like ``id``, ``name``,
    ``prompt``, ``schedule``, ``enabled``, ``state``, ``next_run_at``,
    ``last_run_at``, ``last_status``, etc. (see ``cron/jobs.py`` for the
    authoritative structure).
    """

    job: dict[str, Any]


class JobsListResponse(WebAPIModel):
    jobs: list[dict[str, Any]]


class JobDeleteResponse(WebAPIModel):
    ok: bool
    job_id: str
