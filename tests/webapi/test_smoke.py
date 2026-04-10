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
