import uuid

from app.gateway import parse_simulation_header


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


def unique(text="hello"):
    return f"{text} {uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------ health
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_health_without_keys_is_503(make_client):
    client = make_client(keys=())
    r = client.get("/health")
    assert r.status_code == 503
    assert "detail" in r.json()


# ------------------------------------------------------------------ chat contract
def test_chat_live_inference_contract(client, sid):
    s = sid()
    r = chat(client, s, unique("What is the capital of France?"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == s
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "openai/fake-model"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] >= 0
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


def test_cache_hit_on_same_first_message_in_new_session(client, sid):
    msg = unique("Tell me a fact")
    live = chat(client, sid(), msg).json()
    cached = chat(client, sid(), msg).json()

    assert live["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert cached["metadata"]["resolution_source"] == "LLM_CACHE"
    assert cached["reply"] == live["reply"]
    assert cached["metadata"]["key_id"] is None
    assert cached["metadata"]["fallback_attempts"] == 0
    assert cached["metadata"]["input_tokens"] == live["metadata"]["input_tokens"]
    assert cached["metadata"]["output_tokens"] == live["metadata"]["output_tokens"]
    assert cached["metadata"]["latency_ms"] < live["metadata"]["latency_ms"]


def test_different_message_is_never_cached(client, sid):
    chat(client, sid(), unique("first"))
    r = chat(client, sid(), unique("second")).json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_key_includes_history(client, sid):
    """The same text as a second turn has a different message list, so it is not a cache hit."""
    msg = unique("repeat me")
    chat(client, sid(), msg)
    s = sid()
    chat(client, s, unique("opening line"))
    r = chat(client, s, msg).json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_tokens_are_per_request_not_cumulative(client, sid):
    a = chat(client, sid(), "alpha beta gamma " + uuid.uuid4().hex[:6]).json()
    b = chat(client, sid(), "delta epsilon zeta " + uuid.uuid4().hex[:6]).json()
    assert a["metadata"]["input_tokens"] == b["metadata"]["input_tokens"]


# ------------------------------------------------------------------ multi-turn + history
def test_multi_turn_history_is_sent_and_stored(client, sid):
    s = sid()
    first = chat(client, s, unique("My name is Kartik")).json()
    second = chat(client, s, unique("What is my name?")).json()
    # fake provider reports how many messages it got: system + user  ->  system + 3 history/new
    assert first["reply"].startswith("[2 msgs]")
    assert second["reply"].startswith("[4 msgs]")
    assert second["metadata"]["input_tokens"] > first["metadata"]["input_tokens"]

    r = client.get(f"/sessions/{s}/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert body["messages"][1]["content"] == first["reply"]
    assert "metadata" not in body["messages"][0]
    meta = body["messages"][1]["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["input_tokens"] == first["metadata"]["input_tokens"]
    assert meta["output_tokens"] == first["metadata"]["output_tokens"]
    assert all(m["role"] != "system" for m in body["messages"])


def test_sessions_are_isolated(client, sid):
    a, b = sid(), sid()
    chat(client, a, unique("a1"))
    chat(client, a, unique("a2"))
    r = chat(client, b, unique("b1")).json()
    assert r["reply"].startswith("[2 msgs]")
    assert len(client.get(f"/sessions/{b}/messages").json()["messages"]) == 2


def test_history_survives_app_restart(make_client, sid):
    s = sid()
    chat(make_client(), s, unique("persist me"))
    fresh = make_client()  # same sqlite file
    assert len(fresh.get(f"/sessions/{s}/messages").json()["messages"]) == 2


def test_unknown_session_is_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_system_prompt_override_is_used_and_remembered(client, sid):
    s = sid()
    r1 = chat(client, s, unique("hi"), system_prompt="You are a pirate.").json()
    r2 = chat(client, s, unique("again")).json()  # no prompt sent: session keeps the pirate prompt
    assert r1["reply"].startswith("Arr!")
    assert r2["reply"].startswith("Arr!")
    other = chat(client, sid(), unique("hi")).json()
    assert not other["reply"].startswith("Arr!")


# ------------------------------------------------------------------ failover (simulated)
def test_simulated_429_on_key1_falls_back_to_key2(client, sid):
    r = chat(client, sid(), unique(), headers={"X-Simulate-Failure": "key_1=429"})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"].startswith("[2 msgs]")  # a real answer, not an error in a 200


def test_simulated_500_on_key1_falls_back_to_key2(client, sid):
    r = chat(client, sid(), unique(), headers={"X-Simulate-Failure": "key_1=500"})
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_2"


def test_simulation_bypasses_cache_read(client, sid):
    msg = unique("cached?")
    chat(client, sid(), msg)
    r = chat(client, sid(), msg, headers={"X-Simulate-Failure": "key_1=502"}).json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert r["metadata"]["key_id"] == "key_2"


def test_all_keys_fail_returns_503_and_nothing_stored(client, sid):
    s = sid()
    chat(client, s, unique("keep me"))
    r = chat(client, s, unique("lost"), headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert len(client.get(f"/sessions/{s}/messages").json()["messages"]) == 2


def test_all_keys_fail_on_new_session_leaves_no_session(client, sid):
    s = sid()
    r = chat(client, s, unique(), headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_simulation_header_ignored_when_disabled(make_client, sid):
    client = make_client(simulation=False)
    r = chat(client, sid(), unique(), headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_1"


# ------------------------------------------------------------------ failover (real provider errors)
def test_real_429_from_provider_fails_over(make_client, sid):
    client = make_client(keys=("sk-fail429", "sk-good"))
    meta = chat(client, sid(), unique()).json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1


def test_real_5xx_chain_fails_over_to_third_key(make_client, sid):
    client = make_client(keys=("sk-fail500", "sk-fail503", "sk-good"))
    meta = chat(client, sid(), unique()).json()["metadata"]
    assert meta["key_id"] == "key_3" and meta["fallback_attempts"] == 2


def test_real_all_keys_down_is_503(make_client, sid):
    client = make_client(keys=("sk-fail429", "sk-fail500"))
    r = chat(client, sid(), unique())
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_unreachable_provider_is_503_not_500(make_client, sid):
    client = make_client(api_base="http://127.0.0.1:1/v1", request_timeout_seconds=2)
    r = chat(client, sid(), unique())
    assert r.status_code == 503


# ------------------------------------------------------------------ validation
def test_validation_errors_are_422_json(client, sid):
    cases = [
        {"session_id": sid(), "message": ""},
        {"session_id": sid(), "message": "   \n\t "},
        {"session_id": sid(), "message": "x" * 4001},
        {"session_id": "", "message": "hi"},
        {"session_id": "x" * 129, "message": "hi"},
        {"message": "hi"},
        {"session_id": sid()},
        {"session_id": 123, "message": "hi"},
    ]
    for payload in cases:
        r = client.post("/chat", json=payload)
        assert r.status_code == 422, payload
        assert "detail" in r.json()


def test_message_of_4000_chars_after_trim_is_accepted(client, sid):
    r = chat(client, sid(), "  " + "y" * 3990 + uuid.uuid4().hex[:10] + "  ")
    assert r.status_code == 200


def test_malformed_json_is_422(client):
    r = client.post("/chat", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_failed_validation_is_not_stored(client, sid):
    s = sid()
    client.post("/chat", json={"session_id": s, "message": ""})
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_api_key_never_leaks(make_client, sid):
    client = make_client(keys=("sk-fail429-SECRET", "sk-good-SECRET"))
    r = chat(client, sid(), unique())
    assert "SECRET" not in r.text


def test_cors_allows_frontend_origin_and_simulation_header(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


# ------------------------------------------------------------------ header parsing
def test_parse_simulation_header():
    ids = ["key_1", "key_2"]
    assert parse_simulation_header("key_1=429", ids) == {"key_1": 429}
    assert parse_simulation_header("key_1=429, key_2=500", ids) == {"key_1": 429, "key_2": 500}
    assert parse_simulation_header("*=503", ids) == {"key_1": 503, "key_2": 503}
    assert parse_simulation_header("key_9=429,key_1=418,garbage", ids) == {}
    assert parse_simulation_header(None, ids) == {}
