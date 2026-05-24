"""Surface codex app-server item events as Hermes tool-progress callbacks.

On the ``codex_app_server`` runtime, the codex subprocess emits
``item/started`` / ``item/completed`` JSON-RPC notifications for each
shell command, file edit, MCP tool call, etc. This module bridges those
notifications to Hermes' existing tool-progress callbacks
(``agent.tool_progress_callback``, ``agent.tool_start_callback``,
``agent.tool_complete_callback``) so the kawaii spinner / TUI tool lines
and gateway tool-progress bubbles (Telegram, Discord, …) fire the same
way they do on the default ``chat_completions`` loop.

Without this bridge, the codex turn is a black box — the user sees
nothing until ``turn/completed``. With it, ``tool_executor.py``'s
existing display surface lights up unchanged on the codex path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# Codex item type → Hermes-side display name. Mirrors the projector's
# tool_call naming (see agent/transports/codex_event_projector.py) so any
# downstream handler that keys off the tool name recognises these.
_STATIC_TOOL_NAMES = {
    "commandExecution": "exec_command",
    "fileChange": "apply_patch",
}

_PREVIEW_MAX = 80


def _truncate(s: str, n: int = _PREVIEW_MAX) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def _oneline(s: str) -> str:
    return " ".join((s or "").split())


def _resolve_tool_name(item: dict) -> Optional[str]:
    """Translate a codex item to a Hermes-style tool name.

    Returns ``None`` for non-tool items (agentMessage, reasoning,
    userMessage, opaque types) so the display callback skips them.
    """
    item_type = item.get("type") or ""
    if item_type in _STATIC_TOOL_NAMES:
        return _STATIC_TOOL_NAMES[item_type]
    if item_type == "mcpToolCall":
        server = item.get("server") or "mcp"
        tool = item.get("tool") or "unknown"
        return f"mcp.{server}.{tool}"
    if item_type == "dynamicToolCall":
        return item.get("tool") or "dynamic_tool"
    if item_type == "webSearch":
        return "web_search"
    return None


def _build_preview(item: dict) -> Optional[str]:
    """Build a short preview string passed to ``tool_progress_callback``.

    Matches the spirit of ``agent.display.build_tool_preview`` for the
    chat_completions path: a one-line, truncated summary of the
    primary argument.
    """
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        cmd = item.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return _truncate(_oneline(str(cmd))) or None
    if item_type == "fileChange":
        changes = item.get("changes") or []
        if not changes:
            return None
        kinds: dict[str, int] = {}
        paths: list[str] = []
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            kind = (ch.get("kind") or {}).get("type") or "update"
            kinds[kind] = kinds.get(kind, 0) + 1
            p = ch.get("path") or ""
            if p:
                paths.append(p)
        counts = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        head = ", ".join(paths[:3])
        if len(paths) > 3:
            head += f", +{len(paths) - 3} more"
        return f"{counts}: {head}" if head else counts
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        args = item.get("arguments")
        if isinstance(args, dict) and args:
            try:
                return _truncate(_oneline(json.dumps(args, ensure_ascii=False)))
            except (TypeError, ValueError):
                return None
        return None
    if item_type == "webSearch":
        q = item.get("query") or ""
        return _truncate(_oneline(str(q))) or None
    return None


def _build_args(item: dict) -> dict:
    """Build the args dict handed to ``tool_start_callback`` /
    ``tool_progress_callback``.

    Mirrors the shape produced by ``codex_event_projector`` so any callback
    that introspects ``args`` sees the same fields as in the messages list.
    """
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        return {
            "command": item.get("command") or "",
            "cwd": item.get("cwd") or "",
        }
    if item_type == "fileChange":
        changes_summary: list[dict[str, str]] = []
        for ch in item.get("changes") or []:
            if not isinstance(ch, dict):
                continue
            kind = (ch.get("kind") or {}).get("type") or "update"
            path = ch.get("path") or ""
            changes_summary.append({"kind": kind, "path": path})
        return {"changes": changes_summary}
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        args = item.get("arguments")
        if not isinstance(args, dict):
            return {"arguments": args} if args is not None else {}
        return dict(args)
    if item_type == "webSearch":
        return {"query": item.get("query") or ""}
    return {}


def _is_completed_error(item: dict) -> bool:
    """Best-effort error detection from a completed codex item."""
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        code = item.get("exitCode")
        return code is not None and code != 0
    if item_type == "fileChange":
        status = (item.get("status") or "").lower()
        return status not in {"", "completed", "success", "applied", "ok"}
    if item_type == "mcpToolCall":
        return bool(item.get("error"))
    if item_type == "dynamicToolCall":
        return item.get("success") is False
    return False


def _build_result_summary(item: dict) -> str:
    """Short string handed to ``tool_complete_callback`` as the ``result``.

    Codex completed items can be large (full exec output, MCP responses).
    The projector already adds the full content to the messages list — for
    the callback we hand a one-line digest so any handler that logs it
    doesn't blow up its consumer with kilobytes per tool.
    """
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        code = item.get("exitCode")
        out = item.get("aggregatedOutput") or ""
        return f"exit={code} bytes={len(out)}"
    if item_type == "fileChange":
        n = len(item.get("changes") or [])
        return f"status={item.get('status') or 'unknown'} changes={n}"
    if item_type == "mcpToolCall":
        return "error" if item.get("error") else "ok"
    if item_type == "dynamicToolCall":
        return f"success={item.get('success')}"
    if item_type == "webSearch":
        results = item.get("results") or []
        try:
            return f"results={len(results)}"
        except TypeError:
            return ""
    return ""


def build_event_display_callback(agent) -> Callable[[dict], None]:
    """Return a callable for ``CodexAppServerSession(on_event=...)``.

    The returned callback translates codex ``item/started`` and
    ``item/completed`` notifications into ``agent.tool_progress_callback``
    + ``agent.tool_start_callback`` / ``tool_complete_callback`` calls,
    so codex-runtime turns drive the same kawaii spinner / Telegram
    tool-progress bubbles as default-runtime turns. Notifications for
    non-tool items (agentMessage, reasoning, userMessage) are ignored —
    those already land in the conversation via the projector.

    Exceptions raised by any downstream callback are swallowed and logged
    at DEBUG level — the codex polling loop must never crash on display
    plumbing.
    """
    # Per-item start state so we can compute ``duration=`` on completion.
    # Keyed by codex item id (uuid).
    started_at: dict[str, float] = {}
    started_args: dict[str, dict] = {}

    def _fire(callback_name: str, *args: Any, **kwargs: Any) -> None:
        cb = getattr(agent, callback_name, None)
        if cb is None:
            return
        try:
            cb(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive
            logger.debug("%s raised on codex event", callback_name, exc_info=True)

    def _on_event(note: dict) -> None:
        try:
            method = note.get("method") or ""
            if method not in {"item/started", "item/completed"}:
                return
            params = note.get("params") or {}
            item = params.get("item") or {}
            name = _resolve_tool_name(item)
            if not name:
                return
            item_id = item.get("id") or ""

            if method == "item/started":
                args = _build_args(item)
                preview = _build_preview(item)
                if item_id:
                    started_at[item_id] = time.monotonic()
                    started_args[item_id] = args
                _fire("tool_progress_callback", "tool.started", name, preview, args)
                _fire("tool_start_callback", item_id, name, args)
                return

            # item/completed
            start_ts = started_at.pop(item_id, None) if item_id else None
            args = (started_args.pop(item_id, None) if item_id else None) or _build_args(item)
            duration = (time.monotonic() - start_ts) if start_ts is not None else 0.0
            is_error = _is_completed_error(item)
            _fire(
                "tool_progress_callback",
                "tool.completed", name, None, None,
                duration=duration, is_error=is_error,
            )
            _fire(
                "tool_complete_callback",
                item_id, name, args, _build_result_summary(item),
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("codex event display callback raised", exc_info=True)

    return _on_event
