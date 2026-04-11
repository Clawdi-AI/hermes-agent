"""End-to-end smoke tests for the webapi module.

See ``conftest.py`` in this directory for the stubs that let webapi
boot without the real hermes runtime. Each test builds a fresh
``TestClient`` so auth middleware state (HERMES_API_TOKEN env var)
is isolated per test.

Covers every route touched by the dashboard-support branch.

Run with::

    .venv/bin/python -m pytest -o addopts= tests/webapi/test_smoke.py -v
"""

from __future__ import annotations

import importlib
import os
import sys


def _build_client(*, token: str | None = None):
    """Fresh TestClient with the given HERMES_API_TOKEN env var."""
    from fastapi.testclient import TestClient

    if token is None:
        os.environ.pop("HERMES_API_TOKEN", None)
    else:
        os.environ["HERMES_API_TOKEN"] = token

    # Force a fresh import so the auth middleware picks up the env var.
    for key in list(sys.modules.keys()):
        if key.startswith("webapi"):
            del sys.modules[key]

    webapi_app = importlib.import_module("webapi.app")
    return TestClient(webapi_app.app)


# ─────────────────────────────────────────────────────────────────────
# Public routes
# ─────────────────────────────────────────────────────────────────────


def test_health_unauthenticated():
    client = _build_client(token=None)
    assert client.get("/health").status_code == 200


def test_health_public_even_when_auth_enabled():
    client = _build_client(token="secret")
    assert client.get("/health").status_code == 200


# ─────────────────────────────────────────────────────────────────────
# Read routes with auth disabled
# ─────────────────────────────────────────────────────────────────────


def test_get_routes_work_without_auth():
    client = _build_client(token=None)
    for path in (
        "/v1/models",
        "/api/sessions",
        "/api/memory",
        "/api/config",
        "/api/skills",
        "/api/skills/categories",
        "/api/jobs",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


# ─────────────────────────────────────────────────────────────────────
# Config PATCH — nested platform sections (commit e4a9766)
# ─────────────────────────────────────────────────────────────────────


def test_config_patch_telegram_section():
    client = _build_client(token=None)
    resp = client.patch(
        "/api/config",
        json={"telegram": {"bot_token": "TEST", "enabled": True}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "telegram" in body["merged_sections"]


def test_config_patch_mcp_servers_section():
    """Covers the mcp_servers naming fix (commit 9ec016e)."""
    client = _build_client(token=None)
    resp = client.patch(
        "/api/config",
        json={
            "mcp_servers": {
                "composio": {
                    "url": "https://api.clawdi.ai/composio/mcp",
                    "headers": {
                        "Authorization": "Bearer ${COMPOSIO_MCP_TOKEN}",
                    },
                }
            }
        },
    )
    assert resp.status_code == 200
    assert "mcp_servers" in resp.json()["merged_sections"]


def test_config_patch_multiple_sections_at_once():
    client = _build_client(token=None)
    resp = client.patch(
        "/api/config",
        json={
            "telegram": {"bot_token": "t"},
            "discord": {"bot_token": "d"},
            "security": {"redact_secrets": True},
        },
    )
    assert resp.status_code == 200
    merged = resp.json()["merged_sections"]
    assert "telegram" in merged
    assert "discord" in merged
    assert "security" in merged


# ─────────────────────────────────────────────────────────────────────
# Session lifecycle + message pagination (commit 2432dc0)
# ─────────────────────────────────────────────────────────────────────


def test_session_create_get_messages():
    client = _build_client(token=None)
    created = client.post("/api/sessions", json={"title": "Smoke test"})
    assert created.status_code == 201
    sid = created.json()["session"]["id"]
    assert client.get(f"/api/sessions/{sid}").status_code == 200


def test_session_messages_pagination():
    client = _build_client(token=None)
    created = client.post("/api/sessions", json={"title": "Test"})
    sid = created.json()["session"]["id"]

    # Legacy default — returns all
    assert client.get(f"/api/sessions/{sid}/messages").status_code == 200
    # Limit only
    resp = client.get(f"/api/sessions/{sid}/messages?limit=5")
    assert resp.status_code == 200
    assert "items" in resp.json()
    # Tail mode
    assert (
        client.get(f"/api/sessions/{sid}/messages?limit=5&tail=true").status_code
        == 200
    )
    # Offset
    assert client.get(f"/api/sessions/{sid}/messages?offset=10").status_code == 200


def test_session_search_fts5():
    client = _build_client(token=None)
    client.post("/api/sessions", json={"title": "Test"})
    resp = client.get("/api/sessions/search?q=hello")
    assert resp.status_code == 200
    assert "results" in resp.json()


def test_session_fork():
    client = _build_client(token=None)
    created = client.post("/api/sessions", json={"title": "Original"})
    sid = created.json()["session"]["id"]
    forked = client.post(f"/api/sessions/{sid}/fork")
    assert forked.status_code == 200
    body = forked.json()
    assert "session" in body
    assert body["forked_from"] == sid


# ─────────────────────────────────────────────────────────────────────
# Cron job routes (commit 2e2d915)
# ─────────────────────────────────────────────────────────────────────


def test_cron_create_valid_job():
    client = _build_client(token=None)
    resp = client.post(
        "/api/jobs",
        json={
            "name": "daily summary",
            "schedule": "0 9 * * *",
            "prompt": "summarize yesterday",
        },
    )
    assert resp.status_code == 200
    assert "job" in resp.json()


def test_cron_create_missing_required_field_rejected():
    client = _build_client(token=None)
    # Missing `name` — pydantic should reject with 422
    resp = client.post("/api/jobs", json={"schedule": "0 9 * * *"})
    assert resp.status_code == 422


def test_cron_create_empty_name_rejected():
    client = _build_client(token=None)
    # Whitespace-only name → our custom 400
    resp = client.post(
        "/api/jobs",
        json={"name": "   ", "schedule": "0 9 * * *", "prompt": "hi"},
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────
# Memory CRUD with non-standard DELETE-with-body
# ─────────────────────────────────────────────────────────────────────


def test_memory_post():
    client = _build_client(token=None)
    resp = client.post(
        "/api/memory", json={"target": "memory", "content": "remember this"}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_memory_patch():
    client = _build_client(token=None)
    resp = client.patch(
        "/api/memory",
        json={
            "target": "memory",
            "old_text": "remember this",
            "content": "updated",
        },
    )
    assert resp.status_code == 200


def test_memory_delete_with_json_body():
    """The fork's DELETE /api/memory reads a JSON body. Non-standard REST
    but required by the webapi/routes/memory.py:74 handler."""
    client = _build_client(token=None)
    resp = client.request(
        "DELETE",
        "/api/memory",
        json={"target": "memory", "old_text": "remember this"},
    )
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────
# Auth middleware (commit 934b2dd)
# ─────────────────────────────────────────────────────────────────────


def test_auth_enabled_no_bearer_rejected():
    client = _build_client(token="secret")
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/memory").status_code == 401


def test_auth_enabled_wrong_bearer_rejected():
    client = _build_client(token="secret")
    wrong = client.get(
        "/api/sessions", headers={"Authorization": "Bearer wrong"}
    )
    assert wrong.status_code == 401

    # Missing scheme
    no_scheme = client.get("/api/sessions", headers={"Authorization": "secret"})
    assert no_scheme.status_code == 401


def test_auth_enabled_correct_bearer_accepted():
    client = _build_client(token="secret")
    resp = client.get(
        "/api/sessions", headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200


def test_auth_enabled_rejects_every_protected_route():
    """Every protected router must return 401 when a bearer is required
    but missing — a guard against a future router being added without
    the ``Depends(verify_bearer_token)`` annotation in app.py.
    """
    client = _build_client(token="secret")
    protected = [
        ("GET", "/v1/models"),
        ("GET", "/api/sessions"),
        ("GET", "/api/memory"),
        ("GET", "/api/config"),
        ("GET", "/api/skills"),
        ("GET", "/api/skills/categories"),
        ("GET", "/api/jobs"),
    ]
    for method, path in protected:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}, expected 401"


# ─────────────────────────────────────────────────────────────────────
# Tool success/failure classification (chat.py::_tool_result_failed)
# ─────────────────────────────────────────────────────────────────────


def test_tool_result_failed_classification():
    """Regression tests for the tool success/failure classifier.

    Covers both the original substring-match false positives and the
    second-pass oversight where tools that emit plain ``{"error":
    "..."}`` (without a ``success`` field) were treated as success.
    """
    from webapi.routes.chat import _tool_result_failed

    # --- structured envelope with explicit success ---
    # success:false → failed
    assert _tool_result_failed('{"success": false, "error": "nope"}') is True
    # success:true with an "error" key inside a payload → completed
    assert _tool_result_failed(
        '{"success": true, "result": {"error_count": 3}}'
    ) is False

    # --- idiomatic {"error": "..."} without success field ---
    # delegate_tool / file_tools / registry convention
    assert _tool_result_failed('{"error": "file not found"}') is True
    assert _tool_result_failed('{"error": "permission denied", "code": 13}') is True
    # Nested-dict error (some tools return structured error objects)
    assert _tool_result_failed(
        '{"error": {"kind": "timeout", "after_ms": 5000}}'
    ) is True
    # Empty/null error value → NOT a failure (tool said "no error")
    assert _tool_result_failed('{"error": ""}') is False
    assert _tool_result_failed('{"error": null}') is False

    # --- plain text output (common case) ---
    # Containing the word "error" → completed (false positive avoided)
    assert _tool_result_failed("grep: /etc/hosts: No such file or error") is False
    assert _tool_result_failed("Test results: 0 failed, 42 passed") is False
    assert _tool_result_failed("found 3 errors in the log") is False
    assert _tool_result_failed("Error: this looks scary but isn't JSON") is False

    # --- edge cases ---
    assert _tool_result_failed("") is False
    assert _tool_result_failed("   ") is False
    assert _tool_result_failed(None) is False
    assert _tool_result_failed(42) is False
    assert _tool_result_failed({"error": "not a string"}) is False  # non-string
    # Malformed JSON starting with { → treat as plain text, success
    assert _tool_result_failed("{not valid json") is False
    # JSON array → success (only a dict envelope can signal failure)
    assert _tool_result_failed('[{"success": false}]') is False
    # JSON with neither success nor error → success (empty payload,
    # "result" keys, etc.)
    assert _tool_result_failed('{"result": "ok"}') is False
    assert _tool_result_failed("{}") is False


# ─────────────────────────────────────────────────────────────────────
# Cron job full lifecycle (pause/resume/run/delete)
# ─────────────────────────────────────────────────────────────────────


def test_cron_job_pause_resume_run_delete():
    """Walk a fresh job through every mutation endpoint and confirm
    each returns 200 with a JobResponse shape.
    """
    client = _build_client(token=None)
    created = client.post(
        "/api/jobs",
        json={
            "name": "lifecycle job",
            "schedule": "0 9 * * *",
            "prompt": "hi",
        },
    )
    assert created.status_code == 200
    job_id = created.json()["job"]["id"]

    # Pause
    paused = client.post(f"/api/jobs/{job_id}/pause")
    assert paused.status_code == 200
    assert "job" in paused.json()

    # Resume
    resumed = client.post(f"/api/jobs/{job_id}/resume")
    assert resumed.status_code == 200

    # Run immediately
    ran = client.post(f"/api/jobs/{job_id}/run")
    assert ran.status_code == 200

    # Update with the newly-added model/provider/base_url/script fields
    updated = client.patch(
        f"/api/jobs/{job_id}",
        json={
            "model": "claude-sonnet-4-5",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "script": "/tmp/context.py",
        },
    )
    assert updated.status_code == 200

    # Delete
    deleted = client.delete(f"/api/jobs/{job_id}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True


def test_cron_invalid_job_id_rejected():
    """Job IDs must be 12 hex chars — anything else is a client error
    before hitting the cron subsystem.
    """
    client = _build_client(token=None)
    # Too short — not matched by the {12 hex} regex
    assert client.get("/api/jobs/abc").status_code == 400
    # Right length but non-hex chars
    assert client.get("/api/jobs/zzzzzzzzzzzz").status_code == 400
    # Right length with mixed case (regex only allows lowercase hex)
    assert client.get("/api/jobs/ABCDEF012345").status_code == 400


# ─────────────────────────────────────────────────────────────────────
# Pagination edge cases (SQL LIMIT/OFFSET pushdown)
# ─────────────────────────────────────────────────────────────────────


def test_messages_pagination_bounds():
    """Exercise the limit/offset/tail query params against the fake
    session DB. Real SQL pushdown is covered by the hermes_state unit
    suite; this is a smoke test that the route plumbing works.
    """
    client = _build_client(token=None)
    sid = client.post("/api/sessions", json={"title": "p"}).json()["session"]["id"]

    # limit=0 → legacy unbounded path
    body = client.get(f"/api/sessions/{sid}/messages?limit=0").json()
    assert "items" in body
    assert body["total"] == 0  # fake db has no messages

    # limit=1 offset=0 → pushes to get_messages_page(limit=1, offset=0)
    body = client.get(f"/api/sessions/{sid}/messages?limit=1").json()
    assert body["items"] == []

    # tail=true → get_messages_page(limit=N, tail=True)
    body = client.get(f"/api/sessions/{sid}/messages?limit=10&tail=true").json()
    assert body["items"] == []

    # offset without limit → server still honors limit=0 (legacy),
    # offset ignored
    body = client.get(f"/api/sessions/{sid}/messages?offset=100").json()
    assert body["items"] == []

    # limit too large → 422 from the Query(le=1000) guard
    assert (
        client.get(f"/api/sessions/{sid}/messages?limit=9999").status_code == 422
    )


# ─────────────────────────────────────────────────────────────────────
# JobUpdateRequest new fields (model/provider/base_url/script)
# ─────────────────────────────────────────────────────────────────────


def test_job_update_parses_schedule_string():
    """Regression: cron.jobs.update_job expects `updates["schedule"]`
    to be an already-parsed dict, not the raw wire-level string. The
    route must parse it before passing through, otherwise patching
    the schedule crashes with AttributeError.
    """
    client = _build_client(token=None)
    created = client.post(
        "/api/jobs",
        json={"name": "sched-test", "schedule": "0 9 * * *", "prompt": "hi"},
    )
    assert created.status_code == 200
    job_id = created.json()["job"]["id"]

    # PATCH with a new schedule string — should succeed. Before the
    # fix this raised 500 "AttributeError: 'str' object has no
    # attribute 'get'".
    updated = client.patch(
        f"/api/jobs/{job_id}", json={"schedule": "*/5 * * * *"}
    )
    assert updated.status_code == 200, (
        f"schedule PATCH returned {updated.status_code}: {updated.text}"
    )

    # Invalid schedule → 400 (not 500)
    bad = client.patch(f"/api/jobs/{job_id}", json={"schedule": ""})
    assert bad.status_code in (400, 422), (
        f"empty schedule should be rejected, got {bad.status_code}"
    )


def test_job_update_accepts_all_create_fields():
    """Mirror of JobCreateRequest (minus origin). Guards against a
    future drift where JobCreateRequest grows a field that
    JobUpdateRequest forgets to mirror.
    """
    from webapi.models.jobs import JobCreateRequest, JobUpdateRequest

    create_fields = set(JobCreateRequest.model_fields.keys())
    update_fields = set(JobUpdateRequest.model_fields.keys())
    # Update must at minimum support every create field, plus `enabled`.
    missing = create_fields - update_fields
    assert missing == set(), (
        f"JobUpdateRequest is missing fields present in JobCreateRequest: {missing}"
    )
    assert "enabled" in update_fields


# ─────────────────────────────────────────────────────────────────────
# Exception sanitization (round 6 #4 #7 #14)
# ─────────────────────────────────────────────────────────────────────
#
# Every error path that crosses the network boundary must NEVER reflect
# raw ``str(exc)`` content. Provider/tool exception messages routinely
# carry secrets (API keys in 401 bodies), file system paths, SQL
# fragments, and stack-trace fragments. The sanitization fix replaces
# them with stable opaque messages and logs the real exception
# server-side. These tests pin that contract.


def _build_unsafe_client(*, token: str | None = None):
    """Like ``_build_client`` but with ``raise_server_exceptions=False``.

    Default Starlette ``TestClient`` re-raises any exception that
    bubbles out of a route, which short-circuits the registered
    ``Exception`` handler so we can't observe what the *client* would
    receive. The exception sanitization tests need the handler-applied
    response, so they bypass the default and let Starlette translate
    exceptions into JSONResponses just like a real network client
    would see.
    """
    from fastapi.testclient import TestClient

    if token is None:
        os.environ.pop("HERMES_API_TOKEN", None)
    else:
        os.environ["HERMES_API_TOKEN"] = token

    for key in list(sys.modules.keys()):
        if key.startswith("webapi"):
            del sys.modules[key]

    webapi_app = importlib.import_module("webapi.app")
    return TestClient(webapi_app.app, raise_server_exceptions=False)


def test_unhandled_exception_handler_does_not_leak_raw_message():
    """Round 6 #7 — webapi/errors.py:32 used to return ``str(exc)`` to
    the browser. Inject a route that raises with a sentinel string in
    the exception message and assert it does NOT appear in the
    response body."""
    secret_marker = "API_KEY=sk-leaked-1234567890"

    client = _build_unsafe_client(token=None)
    # Add a one-shot route to the live app whose handler always raises
    # with the sentinel marker in the message. This goes through the
    # global Exception handler we hardened. ``webapi.app`` exposes
    # the FastAPI instance as the module attribute ``app``.
    from webapi.app import app as fastapi_app

    @fastapi_app.get("/__test/leak")
    async def _leak_route() -> dict:
        raise RuntimeError(f"sensitive: {secret_marker}")

    resp = client.get("/__test/leak")
    assert resp.status_code == 500
    body = resp.json()
    assert body == {
        "error": {"message": "Internal server error", "type": "internal_error"}
    }, f"raw message leaked: {body}"
    assert secret_marker not in resp.text


def test_config_patch_failure_does_not_leak_filesystem_path(monkeypatch):
    """Round 6 #14 — config.py used to return ``str(exc)`` from a
    bare ``except``. Force ``save_config`` to raise with a path-like
    sentinel and confirm the response is the stable opaque message."""
    sentinel = "/private/tmp/hermes/config.yaml.bak.99999"

    def _explode(_cfg):
        raise OSError(f"can't write {sentinel}: Permission denied")

    client = _build_unsafe_client(token=None)

    # ``webapi.routes.config`` does ``from hermes_cli.config import
    # save_config`` at import time, so patching ``sys.modules`` after
    # the fact has no effect — the route module already holds its own
    # reference. Patch on the route module instead.
    from webapi.routes import config as config_route

    monkeypatch.setattr(config_route, "save_config", _explode)

    resp = client.patch("/api/config", json={"model": "claude-sonnet"})
    assert resp.status_code == 500
    body = resp.json()
    # The route-level ``except`` raises ``HTTPException(500, "Failed to
    # update config")``, which the registered HTTPException handler
    # catches before the global Exception handler. Either way the raw
    # OSError text must not appear in the response.
    assert body["error"]["message"] == "Failed to update config"
    assert sentinel not in resp.text


def test_chat_failure_does_not_leak_provider_error(monkeypatch):
    """Round 6 #4 — webapi/routes/chat.py used to ``HTTPException(500,
    detail=result["error"])``. Force ``_run_chat`` to return a result
    dict with a sentinel error string and confirm the response body
    doesn't include it."""
    sentinel = "anthropic 401 invalid api key sk-ant-leak-xyz"

    client = _build_unsafe_client(token=None)
    sid = client.post("/api/sessions", json={"title": "leak-test"}).json()[
        "session"
    ]["id"]

    from webapi.routes import chat as chat_module

    async def _fake_threadpool(*args, **kwargs):
        return {"error": sentinel, "final_response": None}

    # Patch ``run_in_threadpool`` only inside the chat module to skip
    # the threadpool hop and return our canned dict directly.
    monkeypatch.setattr(chat_module, "run_in_threadpool", _fake_threadpool)

    resp = client.post(f"/api/sessions/{sid}/chat", json={"message": "hi"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["message"] == "Chat agent failed"
    assert sentinel not in resp.text


# ─────────────────────────────────────────────────────────────────────
# SSE bounded buffer (round 5 #9)
# ─────────────────────────────────────────────────────────────────────


def test_sse_stream_drops_frames_when_buffer_full():
    """The bounded ``SSEStream`` queue MUST drop new frames once full
    rather than blocking the worker thread or growing unbounded.
    Verifies the dropped-frame counter increments and ``close`` still
    terminates the consumer iterator even with a saturated buffer.
    """
    from webapi.sse import SSEEmitter, SSEStream

    stream = SSEStream(max_frames=4)
    emitter = SSEEmitter(session_id="s1", run_id="r1")

    # Fill the buffer past capacity. The first 4 frames go in, the
    # next 6 are dropped (and the worker thread keeps running, never
    # blocked on a bounded ``put`` — that's the whole point of using
    # ``put_nowait`` instead of a blocking sentinel).
    for i in range(10):
        stream.put(emitter.event("test", index=i))

    assert stream.dropped_frames == 6

    # Closing while the buffer is full must NOT block (no sentinel
    # push). The consumer iterator races ``queue.get(timeout=...)``
    # against the close flag and terminates as soon as the queue
    # drains AND ``close()`` has been called.
    stream.close()

    consumed = list(stream)
    assert len(consumed) == 4

    # After close, further puts are no-ops (don't crash, don't increase
    # the dropped-frame counter — the worker thread is well-behaved
    # and may legitimately try to push trailing frames after the
    # generator already saw GeneratorExit).
    stream.put(emitter.event("test", index=99))
    assert stream.dropped_frames == 6


# ─────────────────────────────────────────────────────────────────────
# OpenAPI schema reflects SSE response (round 4 #5)
# ─────────────────────────────────────────────────────────────────────


def test_chat_stream_route_advertises_sse_in_openapi():
    """The ``/chat/stream`` route returns ``text/event-stream`` not
    ``application/json``. The generated OpenAPI schema must reflect
    that so ``openapi-typescript`` consumers don't try to JSON-parse a
    streaming response.
    """
    client = _build_client(token=None)
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    stream_path = "/api/sessions/{session_id}/chat/stream"
    assert stream_path in paths, f"missing route in schema: {stream_path}"
    op = paths[stream_path]["post"]
    content = op["responses"]["200"]["content"]
    assert "text/event-stream" in content, (
        f"chat stream route should advertise text/event-stream, got {content}"
    )


# ─────────────────────────────────────────────────────────────────────
# __main__ default port (round 4 #6)
# ─────────────────────────────────────────────────────────────────────


def test_main_default_port_matches_controller():
    """The controller (Clawdi side) hard-codes 8643 as the upstream
    port for ``/_hermes/*``. ``webapi/__main__.py`` must use the same
    default so local-dev binds the port the controller probes.
    """
    from webapi import __main__ as webapi_main

    assert webapi_main.DEFAULT_PORT == 8643


# ─────────────────────────────────────────────────────────────────────
# Session title ValueError propagation (round 7 regression)
# ─────────────────────────────────────────────────────────────────────
#
# After wrapping SessionDB calls in `run_in_threadpool` (round 6),
# `ValueError` raised inside `set_session_title` (title collision)
# and similar no longer automatically surfaces as a 400 — the route
# handler must catch it explicitly. These tests pin the contract.


def test_create_session_title_collision_returns_400(monkeypatch):
    """create_session bundles ensure_session_title + create_session +
    set_session_title inside one threadpool hop. If ``set_session_title``
    raises ``ValueError`` the route must translate to 400, not 500.
    """
    client = _build_unsafe_client(token=None)
    # First session takes the title.
    first = client.post("/api/sessions", json={"title": "unique-title-1"})
    assert first.status_code == 201

    # Force the second call to raise ValueError on title-set.
    from webapi.routes import sessions as sessions_route

    original = sessions_route._create_session_sync

    def _collide(*, session_db, payload):
        raise ValueError(f"Title '{payload.title}' is already in use")

    monkeypatch.setattr(sessions_route, "_create_session_sync", _collide)

    conflict = client.post("/api/sessions", json={"title": "unique-title-1"})
    assert conflict.status_code == 400, conflict.text
    assert "already in use" in conflict.json()["error"]["message"]

    # Sanity: put it back and the route works again.
    monkeypatch.setattr(sessions_route, "_create_session_sync", original)


def test_fork_session_title_collision_retries_successfully(monkeypatch):
    """Happy path for the retry loop: the first ``set_session_title``
    raises ValueError, the second succeeds. The route must return
    200 and the transient collision must be invisible to the client.
    """
    client = _build_unsafe_client(token=None)
    source = client.post("/api/sessions", json={"title": "parent"})
    sid = source.json()["session"]["id"]

    import sys
    import webapi.routes.sessions as sessions_route

    # Patch ``set_session_title`` on the fake SessionDB to fail on the
    # FIRST call and succeed on subsequent calls, simulating a
    # transient race against another concurrent fork.
    fake_db_cls = sys.modules["hermes_state"].SessionDB
    original_set = fake_db_cls.set_session_title
    call_count = {"n": 0}

    def _flaky_set(self, session_id, title):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError(f"Title '{title}' is already in use")
        return original_set(self, session_id, title)

    monkeypatch.setattr(fake_db_cls, "set_session_title", _flaky_set)

    resp = client.post(f"/api/sessions/{sid}/fork")
    assert resp.status_code == 200, resp.text
    assert call_count["n"] >= 2  # proves retry fired


def test_fork_session_title_exhausted_retries_returns_500(monkeypatch):
    """Unhappy path: set_session_title raises ValueError every time.
    After ``_FORK_TITLE_MAX_RETRIES`` attempts the route must surface
    a 500 (not a 409) because the title is server-generated — the
    client cannot resolve this by retrying.
    """
    client = _build_unsafe_client(token=None)
    source = client.post("/api/sessions", json={"title": "parent"})
    sid = source.json()["session"]["id"]

    import sys
    fake_db_cls = sys.modules["hermes_state"].SessionDB

    def _always_collide(self, session_id, title):
        raise ValueError(f"Title '{title}' is already in use")

    monkeypatch.setattr(fake_db_cls, "set_session_title", _always_collide)

    resp = client.post(f"/api/sessions/{sid}/fork")
    assert resp.status_code == 500, resp.text
    # Opaque error from the global handler — do NOT leak the raw
    # collision text to the browser.
    assert resp.json()["error"]["message"] == "Internal server error"
