import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from hermes_state import SessionDB
from webapi.deps import WEB_SOURCE, ensure_session_title, get_session_db, new_session_id
from webapi.models.sessions import (
    ForkSessionResponse,
    MessageListResponse,
    MessageRecord,
    SearchSessionsResponse,
    SessionCreateRequest,
    SessionDeleteResponse,
    SessionDetailResponse,
    SessionListResponse,
    SessionPatchRequest,
    SessionRecord,
)


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# All ``SessionDB`` methods are synchronous SQLite calls. Calling them
# directly from an ``async def`` handler would block the event loop for
# the duration of the query — fine for in-memory test fixtures but
# problematic in production where the SQLite file lives on a network
# volume and a single ``COUNT(*)`` over a large session table can take
# tens of milliseconds. Every route in this module funnels DB work
# through ``run_in_threadpool`` so the loop stays responsive.


def _coerce_session(row: dict) -> SessionRecord:
    return SessionRecord.model_validate(row)


def _coerce_message(row: dict) -> MessageRecord:
    return MessageRecord.model_validate(row)


@router.get("/search", response_model=SearchSessionsResponse)
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session_db: Annotated[SessionDB, Depends(get_session_db)] = None,
) -> SearchSessionsResponse:
    results = await run_in_threadpool(
        session_db.search_messages,
        q,
        source_filter=["web", "webapi", "cli", "telegram", "discord", "whatsapp", "slack"],
        limit=limit,
        offset=offset,
    )
    return SearchSessionsResponse(query=q, count=len(results), results=results)


def _create_session_sync(
    *,
    session_db: SessionDB,
    payload: SessionCreateRequest,
) -> dict[str, Any]:
    """Run create + optional title-set + read-back in one threadpool hop.

    Bundling these into a single call (instead of three separate
    ``run_in_threadpool`` round-trips) keeps the operation atomic from
    the SessionDB lock's perspective and avoids three event-loop
    re-entries for what is logically one transaction.
    """
    session_id = payload.id or new_session_id()
    title = ensure_session_title(session_db, payload.title)
    session_db.create_session(
        session_id=session_id,
        source=payload.source or WEB_SOURCE,
        model=payload.model,
        model_config=payload.session_model_config,
        system_prompt=payload.system_prompt,
        user_id=payload.user_id,
        parent_session_id=payload.parent_session_id,
    )
    if title:
        session_db.set_session_title(session_id, title)
    return session_db.get_session(session_id)


@router.post("", response_model=SessionDetailResponse, status_code=201)
async def create_session(
    payload: SessionCreateRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    # ``_create_session_sync`` calls ``set_session_title`` internally,
    # which raises ``ValueError`` on a title collision (another
    # session already owns that title). Surface it as a 400 — same
    # contract as ``patch_session``.
    try:
        session = await run_in_threadpool(
            _create_session_sync, session_db=session_db, payload=payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionDetailResponse(session=_coerce_session(session))


def _list_sessions_sync(
    *,
    session_db: SessionDB,
    source: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    sessions = session_db.list_sessions_rich(source=source, limit=limit, offset=offset)
    total = session_db.session_count(source=source)
    return sessions, total


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    session_db: Annotated[SessionDB, Depends(get_session_db)] = None,
) -> SessionListResponse:
    sessions, total = await run_in_threadpool(
        _list_sessions_sync,
        session_db=session_db,
        source=source,
        limit=limit,
        offset=offset,
    )
    return SessionListResponse(items=[_coerce_session(item) for item in sessions], total=total)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    session = await run_in_threadpool(session_db.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return SessionDetailResponse(session=_coerce_session(session))


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def get_session_messages(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
    limit: int = Query(
        default=0,
        ge=0,
        le=1000,
        description="Max messages to return. 0 = all (legacy behavior).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Skip this many messages from the BEGINNING of the session history.",
    ),
    tail: bool = Query(
        default=False,
        description="When true, return the last `limit` messages instead of the first. "
                    "Equivalent to `offset = total - limit`. Useful for chat UIs that load "
                    "the most recent N messages on first render.",
    ),
) -> MessageListResponse:
    """Return messages for a session with optional pagination.

    By default (limit=0) the whole transcript is returned in chronological
    order — matches the legacy behavior. When `limit` is set:

    - `offset` skips messages from the beginning (default 0).
    - `tail=true` returns the LAST `limit` messages regardless of offset.

    `total` in the response is always the full session message count so
    the client can render correct "X of N" indicators and know when it
    has reached the head.
    """
    # Bundle exists-check + count + page-load into a single threadpool
    # hop so the three SQLite reads run back-to-back without three
    # event-loop re-entries.
    def _load() -> tuple[bool, int, list[dict[str, Any]]]:
        exists = session_db.get_session(session_id) is not None
        if not exists:
            return False, 0, []
        total_count = session_db.count_messages(session_id)
        if limit == 0:
            # Legacy behavior: unbounded load. Only used by callers that
            # explicitly want the full transcript (e.g. cron job runners,
            # export flows). Chat UIs should always set a limit.
            rows = session_db.get_messages(session_id)
        else:
            rows = session_db.get_messages_page(
                session_id,
                limit=limit,
                offset=offset,
                tail=tail,
            )
        return True, total_count, rows

    exists, total, selected = await run_in_threadpool(_load)
    if not exists:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return MessageListResponse(
        items=[_coerce_message(item) for item in selected],
        total=total,
    )


def _patch_session_sync(
    *,
    session_db: SessionDB,
    session_id: str,
    payload: SessionPatchRequest,
) -> dict[str, Any] | None:
    """Apply the PATCH atomically inside one threadpool hop.

    Returns ``None`` if the session doesn't exist. ``ValueError`` from
    ``set_session_title`` propagates so the caller can map it to a 400.
    """
    session = session_db.get_session(session_id)
    if not session:
        return None
    if payload.title is not None:
        session_db.set_session_title(session_id, payload.title)
    if payload.system_prompt is not None:
        session_db.update_system_prompt(session_id, payload.system_prompt)
    if payload.end_reason is not None:
        session_db.end_session(session_id, payload.end_reason)
    return session_db.get_session(session_id)


@router.patch("/{session_id}", response_model=SessionDetailResponse)
async def patch_session(
    session_id: str,
    payload: SessionPatchRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDetailResponse:
    try:
        updated = await run_in_threadpool(
            _patch_session_sync,
            session_db=session_db,
            session_id=session_id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return SessionDetailResponse(session=_coerce_session(updated))


@router.delete("/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> SessionDeleteResponse:
    try:
        deleted = await run_in_threadpool(session_db.delete_session, session_id)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session '{session_id}' cannot be deleted because it has dependent forked sessions"
            ),
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return SessionDeleteResponse(session_id=session_id)


# Maximum attempts to allocate a unique fork title. Each attempt is
# an independent ``get_next_title_in_lineage`` read plus a
# ``set_session_title`` write that races the unique-title constraint;
# collisions are rare even at a few concurrent forks, and 5 retries
# effectively guarantees success. After 5 we give up and raise a
# ``RuntimeError`` so the server returns 500 — the client can't
# resolve this by retrying (they didn't supply the title).
_FORK_TITLE_MAX_RETRIES = 5


def _fork_session_sync(
    *,
    session_db: SessionDB,
    session_id: str,
) -> dict[str, Any] | None:
    """Atomically fork a session inside a single threadpool hop.

    For large sessions the loop over ``original["messages"]`` can be
    O(thousands) of synchronous SQLite writes — running this from an
    ``async def`` directly would monopolize the event loop for the
    entire fork. Returns ``None`` if the source session is missing.

    Title allocation race
    ─────────────────────
    ``get_next_title_in_lineage`` reads the current max suffix under
    a brief lock and releases it before the caller commits with
    ``set_session_title``. Two concurrent forks of the same parent
    can therefore pick identical titles — the second ``set`` will
    hit the unique-title constraint and raise ``ValueError``. Retry
    the allocate+commit inside ``_FORK_TITLE_MAX_RETRIES`` attempts
    because the title is SERVER-generated, not client-supplied: the
    caller has no way to resolve this by retrying on their end.
    """
    original = session_db.export_session(session_id)
    if not original:
        return None

    fork_id = new_session_id()
    model_config = original.get("model_config")
    if isinstance(model_config, str) and model_config:
        try:
            model_config = json.loads(model_config)
        except json.JSONDecodeError:
            model_config = None

    session_db.create_session(
        session_id=fork_id,
        source=original.get("source") or WEB_SOURCE,
        model=original.get("model"),
        model_config=model_config,
        system_prompt=original.get("system_prompt"),
        user_id=original.get("user_id"),
        parent_session_id=session_id,
    )

    # Retry loop for the generate+commit race (see docstring).
    base_title = original.get("title") or "New Chat"
    last_err: Exception | None = None
    for _ in range(_FORK_TITLE_MAX_RETRIES):
        fork_title = session_db.get_next_title_in_lineage(base_title)
        try:
            session_db.set_session_title(fork_id, fork_title)
            break
        except ValueError as exc:
            last_err = exc
            continue
    else:
        # Exhausted retries — under sustained concurrent-fork
        # contention. The session row already exists but has no
        # title; leave it so an operator can inspect the state, and
        # surface a 500 to the client.
        raise RuntimeError(
            f"could not allocate unique fork title after "
            f"{_FORK_TITLE_MAX_RETRIES} attempts: {last_err}"
        )

    for message in original.get("messages", []):
        session_db.append_message(
            session_id=fork_id,
            role=message.get("role"),
            content=message.get("content"),
            tool_name=message.get("tool_name"),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
            finish_reason=message.get("finish_reason"),
        )

    return session_db.get_session(fork_id)


@router.post("/{session_id}/fork", response_model=ForkSessionResponse)
async def fork_session(
    session_id: str,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> ForkSessionResponse:
    # Title allocation collisions are retried inside
    # ``_fork_session_sync``; if we reach this catch, all retries
    # were exhausted and the unrecoverable RuntimeError propagates
    # to the global 500 handler. The client cannot resolve this by
    # retrying on their end — that's why it's not a 409.
    forked = await run_in_threadpool(
        _fork_session_sync, session_db=session_db, session_id=session_id
    )
    if forked is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return ForkSessionResponse(session=_coerce_session(forked), forked_from=session_id)
