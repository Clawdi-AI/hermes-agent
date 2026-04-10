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
    prompt: str | None = None
    deliver: str | None = None
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


class JobRepeat(WebAPIModel):
    """Repeat counter for a cron job.

    ``times`` is the target number of runs (``None`` = infinite); ``completed``
    is how many have fired so far.
    """

    times: int | None = None
    completed: int = 0


class JobSchedule(WebAPIModel):
    """Parsed schedule produced by ``cron.jobs.parse_schedule``.

    The dict is a tagged union keyed by ``kind``:
    - ``interval``   → ``{"minutes": int, "display": str}``
    - ``cron``       → ``{"expr": str, "display": str}``
    - ``once``       → ``{"run_at": iso8601, "display": str}``

    We model the union loosely (all fields optional) so the JSON schema
    stays easy to consume from TypeScript without per-kind branches.
    """

    kind: str
    display: str | None = None
    minutes: int | None = None
    expr: str | None = None
    run_at: str | None = None


class JobModel(WebAPIModel):
    """Runtime cron job record as persisted by ``cron.jobs`` and returned
    by ``list_jobs``/``get_job``/``create_job``/``update_job``.

    Field set mirrors the dict literal in ``cron.jobs.create_job`` plus the
    derived ``next_run_at``/``last_run_at`` fields updated by the scheduler.
    Every field except ``id`` is allowed to be absent so legacy jobs on
    disk (missing newly added fields) still deserialize cleanly.
    """

    id: str
    name: str | None = None
    prompt: str | None = None
    skills: list[str] | None = None
    skill: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    script: str | None = None
    schedule: JobSchedule | None = None
    schedule_display: str | None = None
    repeat: JobRepeat | None = None
    enabled: bool | None = None
    state: str | None = None
    paused_at: str | None = None
    paused_reason: str | None = None
    created_at: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    deliver: str | None = None
    origin: dict[str, Any] | None = None


class JobResponse(WebAPIModel):
    """Single-job wrapper returned by create/get/update/pause/resume/run."""

    job: JobModel


class JobsListResponse(WebAPIModel):
    jobs: list[JobModel]


class JobDeleteResponse(WebAPIModel):
    ok: bool
    job_id: str
