from typing import Any

from pydantic import Field

from webapi.models.common import WebAPIModel


class SessionRecord(WebAPIModel):
    id: str
    source: str
    user_id: str | None = None
    model: str | None = None
    session_model_config: Any | None = Field(default=None, alias="model_config")
    system_prompt: str | None = None
    parent_session_id: str | None = None
    started_at: float
    ended_at: float | None = None
    end_reason: str | None = None
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    title: str | None = None
    preview: str | None = None
    last_active: float | None = None


class MessageRecord(WebAPIModel):
    id: int
    session_id: str
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: Any | None = None
    tool_name: str | None = None
    timestamp: float
    token_count: int | None = None
    finish_reason: str | None = None


class SessionCreateRequest(WebAPIModel):
    id: str | None = None
    source: str | None = None
    model: str | None = None
    session_model_config: dict[str, Any] | None = Field(default=None, alias="model_config")
    system_prompt: str | None = None
    user_id: str | None = None
    parent_session_id: str | None = None
    title: str | None = None


class SessionPatchRequest(WebAPIModel):
    title: str | None = None
    system_prompt: str | None = None
    end_reason: str | None = None


class SessionListResponse(WebAPIModel):
    items: list[SessionRecord]
    total: int


class SessionDetailResponse(WebAPIModel):
    session: SessionRecord


class MessageListResponse(WebAPIModel):
    items: list[MessageRecord]
    total: int


class SearchSessionContext(WebAPIModel):
    role: str
    content: str


class SearchSessionMatch(WebAPIModel):
    """One FTS5 hit from ``GET /api/sessions/search``. Shape matches the SQL
    projection in ``hermes_state.SessionDB.search_messages``.
    """

    id: int
    session_id: str
    role: str
    snippet: str
    timestamp: float
    tool_name: str | None = None
    source: str | None = None
    model: str | None = None
    session_started: float | None = None
    context: list[SearchSessionContext] = []


class SearchSessionsResponse(WebAPIModel):
    query: str
    count: int
    results: list[SearchSessionMatch]


class ForkSessionResponse(WebAPIModel):
    session: SessionRecord
    forked_from: str = Field(..., alias="forked_from")


class SessionDeleteResponse(WebAPIModel):
    ok: bool = True
    session_id: str
