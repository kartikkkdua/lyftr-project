"""A tiny OpenAI-compatible provider used by the tests (and for keyless local demos).

Behaviour is chosen by the API key so tests can make individual keys fail for real:
  * key containing "fail429" -> HTTP 429 (rate limit / quota)
  * key containing "fail500" -> HTTP 500
  * key containing "fail503" -> HTTP 503
  * anything else            -> 200 with a deterministic reply

The reply reports how many messages it received, so multi-turn context can be asserted.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
DELAY = float(os.getenv("FAKE_PROVIDER_DELAY", "0.15"))
calls: list[dict] = []


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def completions(request: Request):
    auth = request.headers.get("authorization", "")
    body = await request.json()
    calls.append({"auth": auth, "body": body})
    for status in (429, 500, 503):
        if f"fail{status}" in auth:
            return JSONResponse({"error": {"message": f"fake {status}", "code": status}}, status_code=status)
    await asyncio.sleep(DELAY)
    messages = body["messages"]
    last = messages[-1]["content"]
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    reply = f"[{len(messages)} msgs] you said: {last}"
    if "pirate" in system.lower():
        reply = "Arr! " + reply
    prompt_tokens = sum(len(m["content"].split()) + 4 for m in messages)
    return {
        "id": f"fake-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(reply.split()),
            "total_tokens": prompt_tokens + len(reply.split()),
        },
    }
