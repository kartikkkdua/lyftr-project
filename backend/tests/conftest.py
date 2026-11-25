import socket
import threading
import time
import uuid

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import KeySlot, Settings
from app.main import create_app

from . import fake_provider


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def provider_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(fake_provider.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True


def make_settings(tmp_path, provider_url, keys=("sk-good-1", "sk-good-2"), simulation=True, **overrides):
    values = dict(
        keys=[KeySlot(key_id=f"key_{i + 1}", api_key=k, model="openai/fake-model") for i, k in enumerate(keys)],
        database_url=f"sqlite:///{tmp_path}/app.db",
        allowed_origins=["http://localhost:3000"],
        enable_failure_simulation=simulation,
        cache_enabled=True,
        cache_type="disk",
        cache_dir=str(tmp_path / "cache"),
        cache_ttl_seconds=3600,
        default_system_prompt="You are a test assistant.",
        temperature=0.2,
        max_output_tokens=256,
        request_timeout_seconds=10,
        history_limit=40,
        api_base=provider_url,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_client(tmp_path, provider_url):
    clients = []

    def factory(**kwargs):
        client = TestClient(create_app(make_settings(tmp_path, provider_url, **kwargs)))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def sid():
    return lambda: f"s-{uuid.uuid4().hex[:8]}"
