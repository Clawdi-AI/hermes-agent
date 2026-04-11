import json
import logging
import threading
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from hermes_state import SessionDB
from run_agent import AIAgent
from webapi.deps import create_agent, get_session_db, get_session_or_404, get_runtime_model
from webapi.models.chat import ChatRequest, ChatResponse
from webapi.sse import SSEEmitter, SSEStream

logger = logging.getLogger(__name__)



router = APIRouter(prefix="/api/sessions", tags=["chat"])


def _read_attachment_field(attachment: Any, *keys: str) -> str:
    if not isinstance(attachment, dict):
        return ""
    for key in keys:
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_user_content(payload: ChatRequest) -> tuple[str | list[dict[str, Any]], str]:
    text = payload.message or ""
    attachments = payload.attachments or []
    image_parts: list[dict[str, Any]] = []

    for attachment in attachments:
        if hasattr(attachment, "model_dump"):
            raw = attachment.model_dump(exclude_none=True)
        elif isinstance(attachment, dict):
            raw = dict(attachment)
        else:
            continue

        mime = _read_attachment_field(raw, "contentType", "mimeType", "mediaType")
        if not mime.startswith("image/"):
            continue

        content = _read_attachment_field(raw, "content", "base64", "data")
        if not content:
            continue

        image_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{content}"},
            }
        )

    if not image_parts:
        return text, payload.persist_user_message or text

    content_parts: list[dict[str, Any]] = []
    if text.strip():
        content_parts.append({"type": "text", "text": text})
    content_parts.extend(image_parts)
    if not content_parts:
        content_parts.append({"type": "text", "text": ""})

    persist_text = payload.persist_user_message or text
    return content_parts, persist_text


def _tool_map(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for index, tool_call in enumerate(message.get("tool_calls") or []):
            tool_id = tool_call.get("id")
            if not tool_id:
                continue
            fn = tool_call.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
            except json.JSONDecodeError:
                parsed_args = raw_args
            mapping[tool_id] = {
                "tool_name": fn.get("name") or message.get("tool_name") or f"tool_{index + 1}",
                "args": parsed_args,
            }
    return mapping


def _result_preview(content: Any, limit: int = 4000) -> str:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return text[:limit] + ("..." if len(text) > limit else "")


def _tool_result_failed(content: Any) -> bool:
    """Return True iff a tool-role message is a structured failure.

    Hermes tools are *inconsistent* about the failure envelope they
    emit. Observed shapes (after auditing ``tools/*.py``):

    - ``{"success": False, "error": "..."}`` — ``memory_tool``,
      ``skills_tool``, parts of ``web_tools``.
    - ``{"error": "..."}`` **without** a ``success`` field —
      ``delegate_tool``, ``file_tools``, ``tools/registry.py``'s
      exception wrapper, several ``web_tools`` branches.
    - Plain text — almost every other code path, including grep/test
      output that happens to contain the word "error".

    Classifying plain text as failure (the original implementation's
    substring match) is wrong because it mislabels legitimate output.
    Classifying only ``success: false`` as failure (the first fix
    pass) is wrong the other way — it misses the very common
    ``{"error": "..."}`` shape and renders real failures as green
    cards.

    The correct contract:

    1. If the content isn't a JSON object, treat it as success
       (plain-text output is the common case and is never a failure).
    2. If it IS a JSON object:
       - explicit ``success: false`` → failure
       - explicit ``success: true`` → success
       - no ``success`` field but an ``error`` key with a truthy value
         → failure (the unstructured-error-envelope shape)
       - otherwise → success

    This catches every tool in the tools/ directory's failure paths
    while still tolerating the large set of tools that legitimately
    put the word "error" in a success payload (log summaries, test
    results, search hits, etc).
    """
    if not isinstance(content, str):
        return False
    text = content.strip()
    if not text or text[0] != "{":
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("success") is False:
        return True
    if parsed.get("success") is True:
        return False
    # No explicit success field. An `error` key with a non-empty value
    # is the idiomatic "this failed" shape in tools that didn't adopt
    # the full envelope.
    err = parsed.get("error")
    if isinstance(err, str) and err.strip():
        return True
    if isinstance(err, dict) and err:
        return True
    return False


def _emit_post_run_events(emitter: SSEEmitter, stream: SSEStream, result: dict[str, Any], assistant_message_id: str) -> None:
    messages = result.get("messages") or []
    tools = _tool_map(messages)

    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_id = message.get("tool_call_id")
        tool_meta = tools.get(tool_id, {})
        tool_name = tool_meta.get("tool_name") or message.get("tool_name") or "unknown"
        payload = {
            "tool_call_id": tool_id,
            "tool_name": tool_name,
            "args": tool_meta.get("args"),
            "result_preview": _result_preview(message.get("content")),
        }
        content = message.get("content") or ""
        failed = _tool_result_failed(content)
        stream.put(emitter.event("tool.failed" if failed else "tool.completed", **payload))

        if not failed and tool_name == "memory":
            try:
                parsed = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            stream.put(
                emitter.event(
                    "memory.updated",
                    tool_name=tool_name,
                    target=parsed.get("target"),
                    entry_count=parsed.get("entry_count"),
                    message=parsed.get("message"),
                )
            )

        if not failed and "skill" in tool_name:
            stream.put(
                emitter.event(
                    "skill.loaded",
                    tool_name=tool_name,
                    name=(tool_meta.get("args") or {}).get("name"),
                )
            )

        if not failed:
            artifact_paths: list[str] = []
            parsed = None
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, dict):
                for key in ("path", "file_path", "output_path"):
                    value = parsed.get(key)
                    if isinstance(value, str):
                        artifact_paths.append(value)
                files = parsed.get("files")
                if isinstance(files, list):
                    artifact_paths.extend(str(item) for item in files if isinstance(item, (str, int, float)))
            for path in artifact_paths:
                stream.put(
                    emitter.event(
                        "artifact.created",
                        tool_name=tool_name,
                        path=path,
                    )
                )

    stream.put(
        emitter.event(
            "assistant.completed",
            message_id=assistant_message_id,
            content=result.get("final_response") or "",
            completed=result.get("completed", False),
            partial=result.get("partial", False),
            interrupted=result.get("interrupted", False),
        )
    )
    stream.put(
        emitter.event(
            "run.completed",
            message_id=assistant_message_id,
            completed=result.get("completed", False),
            partial=result.get("partial", False),
            interrupted=result.get("interrupted", False),
            api_calls=result.get("api_calls"),
        )
    )


def _run_chat(
    *,
    session_id: str,
    payload: ChatRequest,
    session_db: SessionDB,
) -> dict[str, Any]:
    get_session_or_404(session_id, session_db)
    history = session_db.get_messages_as_conversation(session_id)
    user_content, persist_text = _build_user_content(payload)
    agent = create_agent(
        session_id=session_id,
        session_db=session_db,
        model=payload.model,
        ephemeral_system_prompt=payload.system_message,
        enabled_toolsets=payload.enabled_toolsets,
        disabled_toolsets=payload.disabled_toolsets,
        skip_context_files=bool(payload.skip_context_files),
        skip_memory=bool(payload.skip_memory),
    )
    return agent.run_conversation(
        user_content,
        conversation_history=history,
        persist_user_message=persist_text,
    )


@router.post("/{session_id}/chat", response_model=ChatResponse)
async def chat(
    session_id: str,
    payload: ChatRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> ChatResponse:
    result = await run_in_threadpool(_run_chat, session_id=session_id, payload=payload, session_db=session_db)
    if result.get("error") and not result.get("final_response"):
        # Log the raw provider/tool error for the operator but return a
        # stable opaque message — agent.run_conversation can surface
        # provider API keys, file paths, or stack-trace fragments here.
        logger.error(
            "[webapi.chat] non-streaming chat failed for session %s: %s",
            session_id,
            result.get("error"),
        )
        raise HTTPException(status_code=500, detail="Chat agent failed")
    return ChatResponse(
        session_id=session_id,
        run_id=f"run_{uuid.uuid4().hex}",
        model=payload.model or get_runtime_model(),
        final_response=result.get("final_response"),
        completed=result.get("completed", False),
        partial=result.get("partial", False),
        interrupted=result.get("interrupted", False),
        api_calls=result.get("api_calls", 0),
        messages=result.get("messages", []),
        last_reasoning=result.get("last_reasoning"),
        response_previewed=result.get("response_previewed", False),
    )


@router.post(
    "/{session_id}/chat/stream",
    # FastAPI's default schema generator infers a JSON response from
    # the ``StreamingResponse`` return annotation, which is wrong: this
    # endpoint serves Server-Sent Events (``text/event-stream``).
    # Declare the real content type so the generated OpenAPI schema
    # matches reality and downstream typed clients (the Clawdi Hermes
    # adapter uses ``openapi-typescript``) don't try to JSON-parse a
    # streaming response. We don't pin a JSON schema for the body
    # because the SSE event envelopes are documented separately on
    # the TypeScript side.
    responses={
        200: {
            "description": "Server-Sent Events stream of run/tool/message frames.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def chat_stream(
    session_id: str,
    payload: ChatRequest,
    session_db: Annotated[SessionDB, Depends(get_session_db)],
) -> StreamingResponse:
    session = get_session_or_404(session_id, session_db)
    user_content, persist_text = _build_user_content(payload)
    run_id = f"run_{uuid.uuid4().hex}"
    assistant_message_id = f"msg_asst_{uuid.uuid4().hex}"
    stream = SSEStream()
    emitter = SSEEmitter(session_id=session_id, run_id=run_id)

    stream.put(
        emitter.event(
            "session.created",
            title=session.get("title") or "New Chat",
            cwd=None,
            model=payload.model or session.get("model") or get_runtime_model(),
        )
    )
    stream.put(
        emitter.event(
            "run.started",
            user_message={
                "id": f"msg_user_{uuid.uuid4().hex}",
                "role": "user",
                "content": persist_text,
            },
        )
    )
    stream.put(
        emitter.event(
            "message.started",
            message={"id": assistant_message_id, "role": "assistant"},
        )
    )

    # Shared state between the worker thread and the async generator
    # that feeds StreamingResponse. The generator catches client
    # disconnect (`GeneratorExit` raised when the client aborts the
    # SSE connection) and signals the worker to stop burning tokens
    # on a stream nobody is reading.
    #
    # Two signals are needed because `create_agent` can take non-
    # trivial time (load skills, read memory, etc):
    #
    #   * `cancelled`: set by the generator on disconnect. The worker
    #     checks it right after agent creation so we can bail out
    #     BEFORE `run_conversation` burns any tokens if the user
    #     already gave up during startup. Without this flag there's a
    #     race window where the worker starts a non-interruptible
    #     agent turn even though the interrupt call from the
    #     generator ran against an `agent_ref[0]` that was still None.
    #   * `agent_ref`: the live agent instance, published by the
    #     worker once `create_agent` returns. The generator calls
    #     `agent.interrupt()` on it if the worker is already running
    #     when disconnect happens. Combined with `cancelled`, every
    #     disconnect either stops before `run_conversation` starts
    #     or interrupts it mid-flight.
    cancelled = threading.Event()
    agent_ref: list[AIAgent | None] = [None]

    def worker() -> None:
        try:
            history = session_db.get_messages_as_conversation(session_id)

            def stream_callback(delta: str) -> None:
                if delta:
                    stream.put(
                        emitter.event(
                            "assistant.delta",
                            message_id=assistant_message_id,
                            delta=delta,
                        )
                    )

            def tool_progress_callback(tool_name: str, preview: str, args: dict[str, Any] | None = None) -> None:
                # `_thinking` is a pseudo-tool that carries the reasoning
                # stream — surface it as a `tool.progress` event so the
                # client can render it as a running "thinking" bubble.
                if tool_name == "_thinking":
                    stream.put(
                        emitter.event(
                            "tool.progress",
                            message_id=assistant_message_id,
                            delta=preview,
                        )
                    )
                    return
                # For real tools we only emit `tool.pending` here — the
                # authoritative `tool.started` with the tool_call_id is
                # emitted from `tool_start_callback` below, so ordering
                # matches: pending (no id) → started (with id) → completed
                # (with id). See chat-adapter.ts for the matching logic.
                stream.put(
                    emitter.event(
                        "tool.pending",
                        tool_name=tool_name,
                        preview=preview,
                        args=args,
                    )
                )

            def tool_start_callback(
                tool_call_id: str | None,
                tool_name: str,
                args: dict[str, Any] | None = None,
            ) -> None:
                # Emitted once per real tool invocation, carrying the
                # assistant-provided ``tool_call_id`` so the TS client
                # can match started/completed pairs by id rather than
                # ordinal. ``_thinking`` is handled by the progress
                # callback and never reaches this path.
                stream.put(
                    emitter.event(
                        "tool.started",
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        args=args,
                    )
                )

            # Fast-path bail: the client can disconnect while we're
            # loading session history. No point building an agent
            # we'll never run.
            if cancelled.is_set():
                return

            agent = create_agent(
                session_id=session_id,
                session_db=session_db,
                model=payload.model,
                ephemeral_system_prompt=payload.system_message,
                enabled_toolsets=payload.enabled_toolsets,
                disabled_toolsets=payload.disabled_toolsets,
                skip_context_files=payload.skip_context_files,
                skip_memory=payload.skip_memory,
                stream_callback=stream_callback,
                tool_progress_callback=tool_progress_callback,
                tool_start_callback=tool_start_callback,
            )
            agent_ref[0] = agent
            # Close the race between "agent created" and
            # "run_conversation about to start": if the generator's
            # GeneratorExit ran during `create_agent` it called
            # `_interrupt_agent(agent_ref[0])` when the ref was
            # still None. Re-check so we honor the disconnect before
            # burning any inference tokens.
            if cancelled.is_set():
                agent.interrupt("SSE client disconnected during startup")
                return

            result = agent.run_conversation(
                user_content,
                conversation_history=history,
                stream_callback=stream_callback,
                persist_user_message=persist_text,
            )
            _emit_post_run_events(emitter, stream, result, assistant_message_id)
        except Exception:
            # Same rationale as the non-streaming path: never push
            # ``str(exc)`` over the wire. The worker exception is
            # almost always provider/tool internals (API keys in
            # rate-limit text, file paths from skills, etc).
            logger.exception(
                "[webapi.chat] streaming worker failed for session %s",
                session_id,
            )
            stream.put(emitter.event("error", message="Chat agent failed"))
        finally:
            stream.put(emitter.event("done"))
            stream.close()

    threading.Thread(target=worker, daemon=True).start()

    async def event_stream():
        """Relay queued frames to StreamingResponse.

        Wrapping the blocking SSEStream in an async generator lets us
        catch client disconnect (GeneratorExit raised when
        ``StreamingResponse`` closes the iterator) and signal the worker
        thread's agent via ``agent.interrupt()``. Mirrors the upstream
        aiohttp pattern at ``gateway/platforms/api_server.py:1080-1100``.
        """
        try:
            async for chunk in stream.aiter():
                yield chunk
        except GeneratorExit:
            # Client disconnected before the worker finished. Set
            # `cancelled` FIRST so the worker bails out of startup
            # if it hasn't yet spawned the agent, then interrupt
            # the agent if it's already running. The worker's
            # post-create_agent re-check closes the race where
            # `agent_ref[0]` was still None when this code ran.
            cancelled.set()
            _interrupt_agent(agent_ref[0], "SSE client disconnected")
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _interrupt_agent(agent: AIAgent | None, reason: str) -> None:
    """Call ``agent.interrupt(reason)`` if the worker has already
    published an instance. ``AIAgent.interrupt`` just sets an internal
    flag and does not raise, so no try/except is needed.
    """
    if agent is not None:
        agent.interrupt(reason)
