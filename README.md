# Lyftr LLM Chatbot: key failover and response caching

A multi-turn chatbot with a **React** frontend and a **FastAPI + LiteLLM** backend. History is stored in **SQLite**. Requests fail over across a pool of API keys, LiteLLM's built-in cache serves repeated prompts, and every reply shows its own source, model, latency and token usage.

```
React (Vite, nginx)  ──HTTP/JSON──▶  FastAPI  ──▶  LLMGateway ──▶ LiteLLM (cache ➜ key_1 ➜ key_2 ➜ …) ──▶ provider
                                        │
                                        └──▶ SQLite (/data/app.db): sessions + messages
```

---

## 1. Live URLs

| Tier | URL |
|---|---|
| Frontend | `https://<your-frontend>.vercel.app` ← _fill in after deploy_ |
| Backend  | `https://<your-backend>.onrender.com` ← _fill in after deploy_ (`/health`) |
| Repo     | `https://github.com/<you>/lyftr-chatbot` |
| Demo video | `<link>` |

The deployed backend runs with `ENABLE_FAILURE_SIMULATION=true`.

---

## 2. Run locally

### With Docker (recommended)

```bash
cp .env.example .env        # then put at least 2 real keys in LLM_API_KEYS
make up                     # builds and starts both services
```

* Frontend: http://localhost:3000
* Backend: http://localhost:8000 (`/health`, `/chat`, `/sessions/{id}/messages`)
* `make down` stops the stack. `make logs` follows the logs. `make clean` also deletes the `/data` volume.

`make up` creates `.env` from `.env.example` if it is missing. SQLite (`/data/app.db`) and the LiteLLM disk cache (`/data/llm_cache`) live on the named volume `chat-data`, so both survive container restarts.

### Without Docker

```bash
# backend (Python 3.11+)
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export LLM_API_KEYS=key-a,key-b DATABASE_URL=sqlite:///./data/app.db \
       CACHE_DIR=./data/llm_cache ALLOWED_ORIGINS=http://localhost:3000 \
       ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --reload --port 8000

# frontend (Node 18+), in another terminal
cd frontend
npm install
VITE_API_BASE_URL=http://localhost:8000 npm run dev     # http://localhost:3000
```

### Tests

```bash
make test        # or: cd backend && pytest -q
```

The 29 tests run against a small **fake OpenAI-compatible provider** (`backend/tests/fake_provider.py`) started on a local port. Real HTTP calls therefore go through LiteLLM, its cache, and its exception mapping, and no real keys are needed. Keys named `…fail429…` or `…fail500…` make that fake provider really return those statuses, so failover is tested with real provider errors as well as with `X-Simulate-Failure`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEYS` | — | Comma-separated key pool (≥2). Ids `key_1`, `key_2`, … in order |
| `DATABASE_URL` | `sqlite:////data/app.db` (Docker) | SQLite location |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins |
| `ENABLE_FAILURE_SIMULATION` | `false` (`true` in compose) | Turns on the `X-Simulate-Failure` hook |
| `LLM_MODEL` | `gemini/gemini-3.5-flash-lite` | LiteLLM model for every key |
| `LLM_MODELS` | empty | Optional per-key models aligned with the keys, e.g. `gemini/gemini-3.5-flash-lite,groq/llama-3.1-8b-instant` |
| `LLM_API_BASE` | empty | Optional OpenAI-compatible base URL |
| `LLM_TEMPERATURE` / `LLM_MAX_OUTPUT_TOKENS` / `LLM_TIMEOUT_SECONDS` | `0.2` / `1024` / `30` | Sampling settings and the per-key timeout |
| `DEFAULT_SYSTEM_PROMPT` | `You are a helpful, concise assistant.` | Used when a session has no prompt of its own |
| `HISTORY_LIMIT` | `40` | Newest stored messages sent back as context |
| `CACHE_ENABLED` / `CACHE_TYPE` / `CACHE_DIR` / `CACHE_TTL_SECONDS` | `true` / `disk` / `/data/llm_cache` / `86400` | LiteLLM cache settings |
| `PUBLIC_API_BASE_URL` (compose) → `API_BASE_URL` (frontend container) | `http://localhost:8000` | Backend URL as the browser sees it |

---

## 3. Design decisions

### Key fallback

* `backend/app/gateway.py` → `LLMGateway.complete()` walks the key pool **in order**. For each key it calls `litellm.acompletion(model, messages, api_key=…, num_retries=0, caching=True)`.
* Any provider failure moves on to the next key: `RateLimitError` (429 and quota/`RESOURCE_EXHAUSTED`), 5xx (`InternalServerError`, `ServiceUnavailableError`, `APIError` 502), timeouts, connection errors, auth errors, and an empty reply. The failure is logged with the key **id** only, never the key.
* LiteLLM's own retries are switched off (`num_retries=0`). A retry on a rate-limited key would only add latency, so the next key is tried at once.
* `fallback_attempts` is the number of keys that failed before the one that answered. If every key fails, the API returns **503 `{"detail": "all providers unavailable"}`** and nothing is written to history.
* **Why an explicit loop rather than LiteLLM's Router fallbacks?** The contract needs the exact `key_id` that answered and an exact count of failed attempts. It also needs per-request fault injection on a chosen key. An ordered loop over LiteLLM calls gives all three deterministically and stays easy to read. LiteLLM still does all the provider work: the provider adapters, mapping every provider's errors onto typed exceptions, token usage, and the cache.
* **`X-Simulate-Failure`** (`key_1=429`, `key_1=429,key_2=500`, `*=429`; statuses 429/500/502/503) is honoured only when `ENABLE_FAILURE_SIMULATION=true`. A listed key raises **the same LiteLLM exception** a real provider would, so it takes exactly the same failover path. Malformed pairs, unknown key ids and unsupported statuses are ignored. A simulated request **skips the cache read**, so failover is really exercised and `key_id` is real rather than `null` from a cache hit. The response is still written to the cache.

### Caching

* LiteLLM's built-in cache (`litellm.cache = Cache(type="disk", …)`) is stored on the `/data` volume, so it survives restarts. `CACHE_TYPE=local` switches it to in-memory.
* LiteLLM builds the cache key from **model + the full message list (system prompt + history + new message) + sampling params**. The API key is not part of it, so a reply produced by `key_1` is a hit for `key_2` too. This means:
  * The same first message in a new session → `LLM_CACHE` the second time.
  * A different message, a different system prompt, or the same text later in a conversation (different history) → a miss, so `LIVE_INFERENCE`.
* A hit is detected through LiteLLM's `response._hidden_params["cache_hit"]`. A hit returns the identical reply and the **usage stored with the cached response**, with `key_id: null` and `fallback_attempts: 0`. Its latency is a few ms, compared with hundreds of ms for a live call.

### SQLite session storage

* Tables are created on startup (`backend/app/db.py`): `messages` (the required schema plus an index on `session_id, id`) and `sessions` (id, the session's system prompt, timestamps).
* One `/chat` turn is stored **atomically** after the LLM succeeds: the user message, the assistant message and its metadata JSON go in one transaction. A 422 or 503 therefore never leaves a half-written turn, and a session whose first request failed does not exist (its history returns 404).
* The client sends only the new message. The server rebuilds the context from SQLite (`system + last HISTORY_LIMIT messages + new message`).
* A per-session `asyncio.Lock` stops two in-flight messages in the same chat from interleaving. Different sessions still run concurrently.
* The connection uses WAL mode and is shared behind a lock, which is simple and plenty for this workload. `DATABASE_URL` points at the Docker volume (`sqlite:////data/app.db`).
* `system_prompt` sent with `/chat` overrides the default for that session and is remembered for later turns.

### Errors and security

* Every error is JSON with `detail`: 422 for validation and malformed JSON (inputs are not echoed back), 503 when every provider is unavailable, 404 for an unknown session, and a catch-all 500 `{"detail": "internal server error"}`. No stack traces or HTML are returned.
* Keys come only from the environment. Only `key_1`/`key_2`… ever appear in responses or logs, and a test asserts that keys don't leak. `.env` is git-ignored.
* CORS allows only `ALLOWED_ORIGINS`. Containers run as non-root users, and both Dockerfiles are multi-stage: Python venv → slim runtime, and Node build → nginx static.

---

## 4. Verification

Set `B=http://localhost:8000` (or the deployed backend URL).

**Health**
```bash
curl $B/health
# {"status":"ok","keys_configured":2,"cache_enabled":true}
```

**Cache hit**: send the same first message in two new sessions:
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-one","message":"What is the capital of France?"}'
# metadata.resolution_source = LIVE_INFERENCE, key_id = key_1, latency ~ hundreds of ms
curl -s -X POST $B/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"s-two","message":"What is the capital of France?"}'
# metadata.resolution_source = LLM_CACHE, same reply, same tokens, key_id = null, latency ~ few ms
```

**Failover**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-fo","message":"Say hi"}'          # 200, key_id=key_2, fallback_attempts=1
curl -s -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=500' \
  -d '{"session_id":"s-fo","message":"Say hi again"}'    # 200, key_id=key_2
curl -s -i -X POST $B/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-fo","message":"Nope"}'            # 503 {"detail":"all providers unavailable"}
curl -s $B/sessions/s-fo/messages                        # 4 messages: the 503 turn is not stored
```

**Validation and 404**
```bash
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{"session_id":"s","message":"   "}'  # 422
curl -s -X POST $B/chat -H 'Content-Type: application/json' -d '{bad json'                            # 422
curl -s $B/sessions/unknown/messages                                                                  # 404
```

**Per-message token usage and multi-turn**: in a session ask "My name is Sam", then "What is my name?". The second reply answers from history. Its `input_tokens` grows because the history is part of *that* request's prompt. `output_tokens` is just that reply. Two equal-length first messages in two new sessions report the same `input_tokens`.

**In the UI**: every assistant bubble shows `LIVE_INFERENCE`/`LLM_CACHE`, the model, latency (`812 ms`), input and output tokens, the answering key, and a "↪ N failover" badge when keys failed. **Settings** has the system-prompt field and a *Simulate failure* dropdown that sends `X-Simulate-Failure` with each message, so failover and the 503 message can be shown without curl. Reloading the page starts a new session. Reload, then resend the first question to see `LLM_CACHE`.

UI `data-testid`s: `chat-input`, `send-button`, `assistant-message`, `meta-source`, `meta-model`, `meta-latency`, `meta-input-tokens`, `meta-output-tokens`, `system-prompt-input` (plus `meta-key-id`, `meta-fallback-attempts`, `user-message`, `error-message`).

---

## 5. Deployment

* **Backend → Render** (Docker): New → Blueprint → this repo (uses `render.yaml`), or a Web Service with root `backend/`. Set `LLM_API_KEYS`, `ALLOWED_ORIGINS=https://<frontend-domain>` and `ENABLE_FAILURE_SIMULATION=true`. Render's free tier has no persistent disk, so add a disk at `/data` on a paid plan for durable history. Railway or Fly.io with a volume at `/data` work the same way.
* **Frontend → Vercel**: import the repo with root directory `frontend/` (Vite preset). Set `VITE_API_BASE_URL=https://<backend-domain>` and deploy. `vercel.json` adds the SPA rewrites.

---

## 6. Setup Used

* Editor: _<your editor, e.g. VS Code>_
* AI tools: Claude Code. It was used to scaffold the code, write tests, and verify the stack end to end.

---

## 7. Bonus features

* **Failure-simulation control in the UI**: a dropdown that sends `X-Simulate-Failure`, so failover can be shown live in the browser.
* **Disk cache on the volume**: the LiteLLM cache survives restarts, not only SQLite.
* **Per-key models (`LLM_MODELS`)**: the key pool can span providers and models (e.g. Gemini → Groq) for real provider diversity.
* **Atomic turn writes and per-session locks**: no partial history, no interleaved concurrent turns.
* **Backend health pill, retry button on errors, a new-chat button, light and dark themes.**
* **Runtime frontend config**: one frontend Docker image works for any backend URL (`API_BASE_URL` is written to `config.js` at container start).
* **Fake provider for tests**: real HTTP and LiteLLM code paths with no keys or cost.
