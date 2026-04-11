"""FastAPI routes for cron job management.

Mirrors the aiohttp cron routes in ``gateway/platforms/api_server.py``
(ported for the webapi module). The underlying storage and scheduler live
in ``cron/jobs.py`` and ``cron/scheduler.py``; this module is just a thin
HTTP layer over those functions with input validation.

If the ``cron`` module cannot be imported (optional install), every route
returns HTTP 501 via ``_check_jobs_available``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Query, status

logger = logging.getLogger(__name__)

from webapi.models.jobs import (
    JobCreateRequest,
    JobDeleteResponse,
    JobResponse,
    JobsListResponse,
    JobUpdateRequest,
)


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Cron module availability (mirrors api_server.py:1013 pattern)
# ---------------------------------------------------------------------------

_CRON_AVAILABLE = False
try:
    from cron.jobs import (
        create_job as _cron_create,
        get_job as _cron_get,
        list_jobs as _cron_list,
        parse_schedule as _cron_parse_schedule,
        pause_job as _cron_pause,
        remove_job as _cron_remove,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
        update_job as _cron_update,
    )

    _CRON_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _cron_list = None  # type: ignore[assignment]
    _cron_get = None  # type: ignore[assignment]
    _cron_create = None  # type: ignore[assignment]
    _cron_update = None  # type: ignore[assignment]
    _cron_remove = None  # type: ignore[assignment]
    _cron_pause = None  # type: ignore[assignment]
    _cron_resume = None  # type: ignore[assignment]
    _cron_trigger = None  # type: ignore[assignment]
    _cron_parse_schedule = None  # type: ignore[assignment]


_JOB_ID_RE = re.compile(r"[a-f0-9]{12}")
_MAX_NAME_LENGTH = 200
_MAX_PROMPT_LENGTH = 5000


def _check_available() -> None:
    if not _CRON_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Cron module not available",
        )


def _internal_error(operation: str, exc: Exception) -> NoReturn:
    """Log the real exception (with stack trace) and raise a generic
    500 to the client.

    The previous implementation raised ``HTTPException(500, str(exc))``
    which echoes raw exception messages — file system paths, SQL errors,
    stack-trace fragments, tempfile names — back to the browser. This
    helper keeps the detail on the server logs where it belongs and
    gives the client a stable, operation-scoped message.
    """
    logger.exception("[webapi.jobs] %s failed", operation)
    raise HTTPException(
        status_code=500,
        detail=f"Internal error during {operation}",
    ) from exc


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid job ID format",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=JobsListResponse)
async def list_jobs(
    include_disabled: bool = Query(False),
) -> JobsListResponse:
    _check_available()
    try:
        jobs = _cron_list(include_disabled=include_disabled)  # type: ignore[misc]
        return JobsListResponse(jobs=jobs)
    except Exception as exc:
        _internal_error("list_jobs", exc)


@router.post("", response_model=JobResponse)
async def create_job(payload: JobCreateRequest) -> JobResponse:
    _check_available()

    name = payload.name.strip()
    schedule = payload.schedule.strip()
    prompt = payload.prompt or ""

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(name) > _MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Name must be ≤ {_MAX_NAME_LENGTH} characters",
        )
    if not schedule:
        raise HTTPException(status_code=400, detail="Schedule is required")
    if len(prompt) > _MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt must be ≤ {_MAX_PROMPT_LENGTH} characters",
        )
    if payload.repeat is not None and payload.repeat < 1:
        raise HTTPException(status_code=400, detail="Repeat must be a positive integer")

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "schedule": schedule,
        "name": name,
        "deliver": payload.deliver,
    }
    if payload.skills:
        kwargs["skills"] = payload.skills
    if payload.skill:
        kwargs["skill"] = payload.skill
    if payload.repeat is not None:
        kwargs["repeat"] = payload.repeat
    if payload.model is not None:
        kwargs["model"] = payload.model
    if payload.provider is not None:
        kwargs["provider"] = payload.provider
    if payload.base_url is not None:
        kwargs["base_url"] = payload.base_url
    if payload.script is not None:
        kwargs["script"] = payload.script

    try:
        job = _cron_create(**kwargs)  # type: ignore[misc]
        return JobResponse(job=job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _internal_error("create_job", exc)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    _check_available()
    _validate_job_id(job_id)
    try:
        job = _cron_get(job_id)  # type: ignore[misc]
    except Exception as exc:
        _internal_error("get_job", exc)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(job=job)


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(job_id: str, payload: JobUpdateRequest) -> JobResponse:
    _check_available()
    _validate_job_id(job_id)

    sanitized = payload.model_dump(exclude_none=True)
    if not sanitized:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    if "name" in sanitized and len(sanitized["name"]) > _MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Name must be ≤ {_MAX_NAME_LENGTH} characters",
        )
    if "prompt" in sanitized and len(sanitized["prompt"]) > _MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt must be ≤ {_MAX_PROMPT_LENGTH} characters",
        )

    # `cron.jobs.update_job` expects ``updates["schedule"]`` to be an
    # already-parsed dict (its merge path calls `.get("display", ...)`
    # on it). The wire-level contract is a string though, matching
    # `JobCreateRequest.schedule`, so we parse it here before handing
    # off. ``parse_schedule`` raises ValueError on invalid input which
    # we surface as a 400.
    if "schedule" in sanitized and isinstance(sanitized["schedule"], str):
        try:
            sanitized["schedule"] = _cron_parse_schedule(  # type: ignore[misc]
                sanitized["schedule"].strip()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        job = _cron_update(job_id, sanitized)  # type: ignore[misc]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _internal_error("update_job", exc)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(job=job)


@router.delete("/{job_id}", response_model=JobDeleteResponse)
async def delete_job(job_id: str) -> JobDeleteResponse:
    _check_available()
    _validate_job_id(job_id)
    try:
        success = _cron_remove(job_id)  # type: ignore[misc]
    except Exception as exc:
        _internal_error("delete_job", exc)
    if not success:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobDeleteResponse(ok=True, job_id=job_id)


@router.post("/{job_id}/pause", response_model=JobResponse)
async def pause_job(job_id: str) -> JobResponse:
    _check_available()
    _validate_job_id(job_id)
    try:
        job = _cron_pause(job_id)  # type: ignore[misc]
    except Exception as exc:
        _internal_error("pause_job", exc)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(job=job)


@router.post("/{job_id}/resume", response_model=JobResponse)
async def resume_job(job_id: str) -> JobResponse:
    _check_available()
    _validate_job_id(job_id)
    try:
        job = _cron_resume(job_id)  # type: ignore[misc]
    except Exception as exc:
        _internal_error("resume_job", exc)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(job=job)


@router.post("/{job_id}/run", response_model=JobResponse)
async def run_job_now(job_id: str) -> JobResponse:
    """Trigger immediate execution of a job, regardless of schedule."""
    _check_available()
    _validate_job_id(job_id)
    try:
        job = _cron_trigger(job_id)  # type: ignore[misc]
    except Exception as exc:
        _internal_error("run_job_now", exc)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(job=job)
