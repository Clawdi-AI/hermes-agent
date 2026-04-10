import importlib
from pathlib import Path
import sys
import types


def _load_deps_module():
    module_name = "_webapi_deps_under_test"
    sys.modules.pop(module_name, None)
    deps_path = Path(__file__).resolve().parents[2] / "webapi" / "deps.py"
    spec = importlib.util.spec_from_file_location(module_name, deps_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_kwargs_import_survives_missing_gateway_model_resolver(monkeypatch):
    sentinel = {
        "provider": "openai-codex",
        "api_key": "fake-key",
        "base_url": "https://proxy.example/v1",
        "default_headers": {"x-api-key": "proxy-key"},
    }
    fake_gateway_run = types.ModuleType("gateway.run")
    fake_gateway_run._resolve_runtime_agent_kwargs = lambda: sentinel
    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi.HTTPException = type("HTTPException", (Exception,), {})

    monkeypatch.setitem(sys.modules, "gateway.run", fake_gateway_run)
    monkeypatch.setitem(sys.modules, "fastapi", fake_fastapi)

    deps = _load_deps_module()

    try:
        assert deps.get_runtime_agent_kwargs() is sentinel
    finally:
        sys.modules.pop("_webapi_deps_under_test", None)
