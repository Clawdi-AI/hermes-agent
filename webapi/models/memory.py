from webapi.models.common import WebAPIModel


class MemoryPostRequest(WebAPIModel):
    target: str
    content: str


class MemoryPatchRequest(WebAPIModel):
    target: str
    old_text: str
    content: str


class MemoryDeleteRequest(WebAPIModel):
    target: str
    old_text: str


class MemoryTarget(WebAPIModel):
    """Single memory-target snapshot (either the shared ``memory`` or
    per-user ``user`` target).
    """

    target: str
    entries: list[str]
    usage: str
    entry_count: int


class MemoryMutationResponse(WebAPIModel):
    success: bool
    target: str
    entries: list[str]
    usage: str
    entry_count: int
    message: str | None = None


class MemoryReadResponse(WebAPIModel):
    targets: list[MemoryTarget]
