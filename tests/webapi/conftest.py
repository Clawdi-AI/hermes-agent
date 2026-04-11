"""Fixtures that stub hermes runtime modules before webapi imports.

This conftest installs in-memory fakes for every heavy internal module
that ``webapi`` depends on (hermes_state.SessionDB, run_agent.AIAgent,
tools.memory_tool.MemoryStore, cron.jobs, gateway.run,
tools.skills_tool, hermes_cli.config). The stubs run at module import
time so pytest's collection phase can load ``test_smoke.py`` without
triggering the real import chain (which pulls in `openai`, `tools.*`,
and other heavy dependencies not present in the test venv).

Important: this file must run BEFORE ``webapi.app`` is ever imported
in this test session. pytest honors that because conftest.py files in
the target directory are loaded before test modules during collection.
"""

from __future__ import annotations

import json
import sys
import types

# ─────────────────────────────────────────────────────────────────────
# In-memory fakes
# ─────────────────────────────────────────────────────────────────────

_sessions_store: dict[str, dict] = {}
_messages_store: dict[str, list] = {}


class _FakeSessionDB:
    def sanitize_title(self, t):
        return t or None

    def get_next_title_in_lineage(self, t):
        return "New chat"

    def create_session(
        self,
        session_id,
        source,
        model=None,
        model_config=None,
        system_prompt=None,
        user_id=None,
        parent_session_id=None,
    ):
        _sessions_store[session_id] = {
            "id": session_id,
            "source": source,
            "user_id": user_id,
            "model": model,
            "model_config": model_config,
            "system_prompt": system_prompt,
            "parent_session_id": parent_session_id,
            "started_at": 1700000000.0,
            "ended_at": None,
            "end_reason": None,
            "message_count": 0,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "title": None,
            "preview": None,
            "last_active": 1700000000.0,
        }
        _messages_store[session_id] = []
        return session_id

    def get_session(self, sid):
        return _sessions_store.get(sid)

    def list_sessions_rich(self, source=None, limit=50, offset=0):
        return list(_sessions_store.values())[offset : offset + limit]

    def session_count(self, source=None):
        return len(_sessions_store)

    def get_messages(self, sid):
        return _messages_store.get(sid, [])

    def count_messages(self, sid):
        return len(_messages_store.get(sid, []))

    def get_messages_page(self, sid, *, limit, offset=0, tail=False):
        msgs = _messages_store.get(sid, [])
        if tail:
            start = max(0, len(msgs) - limit)
            return msgs[start : start + limit]
        return msgs[offset : offset + limit]

    def get_messages_as_conversation(self, sid):
        return _messages_store.get(sid, [])

    def set_session_title(self, sid, title):
        if sid in _sessions_store:
            _sessions_store[sid]["title"] = title

    def update_system_prompt(self, sid, prompt):
        pass

    def end_session(self, sid, reason):
        pass

    def delete_session(self, sid):
        return _sessions_store.pop(sid, None) is not None

    def search_messages(self, q, source_filter=None, limit=20, offset=0):
        # Fake row shape matches the SQL projection in
        # hermes_state.SessionDB.search_messages (id, session_id, role,
        # snippet, timestamp, tool_name, source, model, session_started,
        # context) so the typed SearchSessionsResponse pydantic model
        # validates against it.
        return [
            {
                "id": 1,
                "session_id": sid,
                "role": "user",
                "snippet": f"match for {q}",
                "timestamp": 0.0,
                "tool_name": None,
                "source": "web",
                "model": None,
                "session_started": 0.0,
                "context": [],
            }
            for sid in _sessions_store
        ]

    def export_session(self, sid):
        s = _sessions_store.get(sid)
        if not s:
            return None
        return {**s, "messages": _messages_store.get(sid, [])}

    def append_message(self, **kw):
        pass


class _FakeMemoryStore:
    memory_entries = ["fact"]
    user_entries = ["prefs"]

    def load_from_disk(self):
        pass

    def _success_response(self, target):
        return {"usage": "1/500 entries"}

    def add(self, target, content):
        return {
            "success": True,
            "target": target,
            "entries": ["new"],
            "usage": "1",
            "entry_count": 1,
        }

    def replace(self, *args, **kwargs):
        return {
            "success": True,
            "target": "memory",
            "entries": [],
            "usage": "",
            "entry_count": 0,
        }

    def remove(self, *args, **kwargs):
        return {
            "success": True,
            "target": "memory",
            "entries": [],
            "usage": "",
            "entry_count": 0,
        }


def _install_stubs() -> None:
    """Install fake modules so webapi.app can boot without hermes core."""
    for mod_name in (
        "hermes_state",
        "run_agent",
        "hermes_cli",
        "hermes_cli.config",
        "tools",
        "tools.memory_tool",
        "tools.skills_tool",
        "cron",
        "cron.jobs",
        "gateway",
        "gateway.run",
    ):
        if mod_name in sys.modules and not getattr(
            sys.modules[mod_name], "__webapi_stub__", False
        ):
            # Already imported for real — wipe it so stubs take over.
            del sys.modules[mod_name]
        if mod_name not in sys.modules:
            mod = types.ModuleType(mod_name)
            mod.__webapi_stub__ = True  # type: ignore[attr-defined]
            sys.modules[mod_name] = mod

    sys.modules["hermes_state"].SessionDB = _FakeSessionDB  # type: ignore[attr-defined]
    sys.modules["run_agent"].AIAgent = type("AIAgent", (), {})  # type: ignore[attr-defined]
    sys.modules["tools.memory_tool"].MemoryStore = _FakeMemoryStore  # type: ignore[attr-defined]
    sys.modules["hermes_cli.config"].load_config = lambda: {  # type: ignore[attr-defined]
        "model": "stub",
        "provider": "stub",
    }
    sys.modules["hermes_cli.config"].save_config = lambda c: None  # type: ignore[attr-defined]
    sys.modules["tools.skills_tool"].skill_view = lambda **k: json.dumps(  # type: ignore[attr-defined]
        {"success": True, "content": "# skill"}
    )
    sys.modules["tools.skills_tool"].skills_categories = lambda: json.dumps(  # type: ignore[attr-defined]
        {"success": True, "categories": [{"name": "a", "skill_count": 1}]}
    )
    sys.modules["tools.skills_tool"].skills_list = lambda **k: json.dumps(  # type: ignore[attr-defined]
        {
            "success": True,
            "skills": [{"name": "s", "category": "a", "description": "d"}],
            "categories": ["a"],
            "count": 1,
        }
    )

    # Fake cron.jobs backed by an in-memory dict so pause/resume/run/
    # delete can round-trip through the route handlers end-to-end.
    _jobs_store: dict[str, dict] = {}

    def _fake_create_job(**k):
        job_id = "abcdef012345"
        job = {
            "id": job_id,
            "name": k.get("name"),
            "prompt": k.get("prompt"),
            "schedule": {"kind": "cron", "display": k.get("schedule")},
            "schedule_display": k.get("schedule"),
            "enabled": True,
            "state": "scheduled",
        }
        _jobs_store[job_id] = job
        return job

    def _fake_update_job(job_id, updates):
        job = _jobs_store.get(job_id)
        if job is None:
            return None
        job.update(updates)
        return job

    def _fake_pause_job(job_id):
        return _fake_update_job(
            job_id, {"state": "paused", "enabled": False}
        )

    def _fake_resume_job(job_id):
        return _fake_update_job(
            job_id, {"state": "scheduled", "enabled": True}
        )

    def _fake_trigger_job(job_id):
        return _fake_update_job(job_id, {"last_run_at": "2026-01-01T00:00:00"})

    def _fake_remove_job(job_id):
        return _jobs_store.pop(job_id, None) is not None

    cj = sys.modules["cron.jobs"]
    cj.list_jobs = lambda include_disabled=False: list(_jobs_store.values())  # type: ignore[attr-defined]
    cj.get_job = lambda job_id: _jobs_store.get(job_id)  # type: ignore[attr-defined]
    cj.create_job = _fake_create_job  # type: ignore[attr-defined]
    cj.update_job = _fake_update_job  # type: ignore[attr-defined]
    cj.remove_job = _fake_remove_job  # type: ignore[attr-defined]
    cj.pause_job = _fake_pause_job  # type: ignore[attr-defined]
    cj.resume_job = _fake_resume_job  # type: ignore[attr-defined]
    cj.trigger_job = _fake_trigger_job  # type: ignore[attr-defined]
    cj._jobs_store = _jobs_store  # type: ignore[attr-defined]

    sys.modules["gateway.run"]._resolve_model = lambda: "stub"  # type: ignore[attr-defined]
    sys.modules["gateway.run"]._resolve_runtime_agent_kwargs = lambda: {  # type: ignore[attr-defined]
        "provider": "stub"
    }


# Install stubs at conftest import time so test_smoke.py can import
# webapi.app immediately.
_install_stubs()


# Override the repo-wide _isolate_hermes_home fixture so our in-memory
# stubs don't get wiped by its HERMES_HOME manipulation.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _keep_stubs_installed():
    """Re-install stubs before each test in case prior test hooks wiped them."""
    _install_stubs()
    # Reset the in-memory session stores so tests don't leak state.
    _sessions_store.clear()
    _messages_store.clear()
    yield
