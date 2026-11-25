"""FastAPI app: GET /health, POST /chat, GET /sessions/{session_id}/messages."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .db import Database
from .gateway import AllProvidersUnavailable, LLMGateway, parse_simulation_header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app")

MAX_MESSAGE_CHARS = 4000


# ---------------------------------------------------------------- schemas
class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    message: str
    system_prompt: Optional[str] = Field(default=None, max_length=8000)

    @field_validator("session_id")
    @classmethod
    def _session_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def _message_length(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        if len(value) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message must be at most {MAX_MESSAGE_CHARS} characters")
        return value

    @field_validator("system_prompt")
    @classmethod
    def _blank_prompt_is_none(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ChatMetadata(BaseModel):
    resolution_source: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    key_id: Optional[str]
    fallback_attempts: int


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    metadata: ChatMetadata


# ---------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_path)
        app.state.gateway = LLMGateway(settings)
        logger.info(
            "started: %d key(s) %s, cache=%s (%s), failure_simulation=%s, db=%s",
            len(settings.keys),
            [f"{k.key_id}:{k.model}" for k in settings.keys],
            app.state.gateway.cache_enabled,
            settings.cache_type,
            settings.enable_failure_simulation,
            settings.database_path,
        )
        yield
        app.state.db.close()

    app = FastAPI(title="Lyftr LLM Chatbot", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # One lock per session so two concurrent messages in the same chat cannot interleave.
    session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------ error handlers
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errors = []
        for err in exc.errors():
            err = dict(err)
            err.pop("ctx", None)  # may hold exception objects that are not JSON-serialisable
            err.pop("input", None)  # do not echo (possibly huge) inputs back
            errors.append(err)
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # ------------------------------------------------------------ routes
    @app.get("/health")
    async def health(request: Request):
        db: Database = request.app.state.db
        gateway: LLMGateway = request.app.state.gateway
        keys = len(gateway.keys)
        if not db.ping():
            raise HTTPException(status_code=503, detail="database unavailable")
        if keys == 0:
            raise HTTPException(status_code=503, detail="no API keys configured")
        return {"status": "ok", "keys_configured": keys, "cache_enabled": gateway.cache_enabled}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest,
        request: Request,
        x_simulate_failure: Optional[str] = Header(default=None),
    ):
        started = time.perf_counter()
        db: Database = request.app.state.db
        gateway: LLMGateway = request.app.state.gateway

        simulate: dict[str, int] = {}
        if settings.enable_failure_simulation and x_simulate_failure:
            simulate = parse_simulation_header(x_simulate_failure, gateway.key_ids)
            logger.info("X-Simulate-Failure active for this request: %s", simulate)

        async with session_locks[body.session_id]:
            session = await asyncio.to_thread(db.get_session, body.session_id)
            stored_prompt = session["system_prompt"] if session else None
            system_prompt = body.system_prompt or stored_prompt or settings.default_system_prompt

            history = await asyncio.to_thread(
                db.get_history, body.session_id, settings.history_limit
            )
            messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
            messages += [{"role": m["role"], "content": m["content"]} for m in history]
            messages.append({"role": "user", "content": body.message})

            try:
                result = await gateway.complete(messages, simulate=simulate)
            except AllProvidersUnavailable as exc:
                logger.error("all providers unavailable: %s", exc.failures)
                raise HTTPException(status_code=503, detail="all providers unavailable")

            metadata: dict[str, Any] = {
                "resolution_source": "LLM_CACHE" if result.cache_hit else "LIVE_INFERENCE",
                "model": result.model,
                "latency_ms": int(round((time.perf_counter() - started) * 1000)),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "key_id": result.key_id,
                "fallback_attempts": result.fallback_attempts,
            }
            await asyncio.to_thread(
                db.save_exchange,
                body.session_id,
                body.system_prompt,
                body.message,
                result.reply,
                metadata,
            )

        logger.info(
            "chat session=%s source=%s key=%s fallbacks=%d tokens=%d/%d latency=%dms",
            body.session_id,
            metadata["resolution_source"],
            metadata["key_id"],
            metadata["fallback_attempts"],
            metadata["input_tokens"],
            metadata["output_tokens"],
            metadata["latency_ms"],
        )
        return {"session_id": body.session_id, "reply": result.reply, "metadata": metadata}

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str, request: Request):
        db: Database = request.app.state.db
        history = await asyncio.to_thread(db.get_history, session_id)
        if not history:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": history}

    return app


app = create_app()
