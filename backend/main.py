"""FastAPI — pairing, upload, bot relay.

Бэкенд НЕ видит код авторизации Telegram и НЕ читает сессию пользователя.
Он только:
  1. выдаёт одноразовый pairing-код фронту,
  2. валидирует код у локального агента и выдаёт ему HMAC-токен,
  3. принимает chunked-upload готового ZIP от агента,
  4. проверяет sha256,
  5. отправляет ZIP в Telegram-бота владельца.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from bot_sender import cleanup_archive, send_archive_to_owner, sha256_file
from config import settings
from pairing import store
from schemas import (
    AgentClaim,
    AgentClaimOk,
    AgentError,
    AgentProgressReport,
    PairingCreated,
    PairingStatus,
    UploadFinalize,
    UploadFinalizeOk,
    UploadInit,
    UploadInitOk,
)
from uploader_receiver import uploads

logger = logging.getLogger("tde")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Telegram Data Extractor — Relay", version="2.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": "2.0.0"}


@app.on_event("startup")
async def _startup() -> None:
    async def _janitor() -> None:
        while True:
            await asyncio.sleep(120)
            await store.prune()
    asyncio.create_task(_janitor())


# ======================== BROWSER ========================

@app.post("/api/pairing/new", response_model=PairingCreated)
@limiter.limit("20/minute")
async def pairing_new(request: Request) -> PairingCreated:
    p = await store.create()
    return PairingCreated(
        pairing_code=p.pairing_code,
        polling_token=p.polling_token,
        expires_in=settings.pairing_ttl_seconds,
    )


@app.get("/api/pairing/status", response_model=PairingStatus)
async def pairing_status(polling_token: str) -> PairingStatus:
    try:
        p = store.get_by_poll(polling_token)
    except PermissionError:
        raise HTTPException(401, "invalid_polling_token")
    return PairingStatus(
        state=p.state,  # type: ignore[arg-type]
        progress=p.progress,
        stage=p.stage,
        delivered=p.delivered,
        error=p.error,
        agent_info=p.agent_info,
    )


# ======================== AGENT ========================

@app.post("/api/agent/claim", response_model=AgentClaimOk)
@limiter.limit("30/minute")
async def agent_claim(request: Request, body: AgentClaim) -> AgentClaimOk:
    try:
        p = await store.claim(body.pairing_code, body.agent_info)
    except KeyError:
        raise HTTPException(404, "pairing_not_found")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    assert p.agent_token is not None
    return AgentClaimOk(
        agent_token=p.agent_token,
        pairing_id=p.pairing_id,
        expires_in=settings.agent_token_ttl_seconds,
    )


@app.post("/api/agent/progress")
async def agent_progress(body: AgentProgressReport) -> dict:
    try:
        await store.update_progress(body.agent_token, body.state, body.progress, body.stage)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    return {"ok": True}


@app.post("/api/agent/error")
async def agent_error(body: AgentError) -> dict:
    try:
        await store.mark_error(body.agent_token, body.error)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    return {"ok": True}


# ---- upload ----

@app.post("/api/agent/upload/init", response_model=UploadInitOk)
async def upload_init(body: UploadInit) -> UploadInitOk:
    try:
        p = await store.get_by_agent(body.agent_token)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    try:
        s = await uploads.init(p.pairing_id, body.filename, body.total_size, body.sha256)
    except ValueError as e:
        raise HTTPException(413, str(e))
    p.upload_id = s.upload_id
    p.upload_filename = s.filename
    p.upload_total_size = s.total_size
    p.upload_sha256 = s.sha256_expected
    return UploadInitOk(upload_id=s.upload_id, chunk_size=settings.chunk_max_bytes)


@app.post("/api/agent/upload/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Header(..., alias="X-Upload-Id"),
    agent_token: str = Header(..., alias="X-Agent-Token"),
) -> dict:
    # авторизация агента на каждый чанк
    try:
        p = await store.get_by_agent(agent_token)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    try:
        s = uploads.get(upload_id)
    except KeyError:
        raise HTTPException(404, "upload_not_found")
    if s.pairing_id != p.pairing_id:
        raise HTTPException(403, "upload_belongs_to_other_pairing")

    data = await request.body()
    if len(data) > settings.chunk_max_bytes:
        raise HTTPException(413, "chunk_too_large")
    try:
        received = await uploads.append_chunk(upload_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"received": received}


@app.post("/api/agent/upload/finalize", response_model=UploadFinalizeOk)
async def upload_finalize(body: UploadFinalize) -> UploadFinalizeOk:
    try:
        p = await store.get_by_agent(body.agent_token)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    try:
        s = uploads.get(body.upload_id)
    except KeyError:
        raise HTTPException(404, "upload_not_found")
    if s.pairing_id != p.pairing_id:
        raise HTTPException(403, "upload_belongs_to_other_pairing")

    try:
        ok, received = await uploads.finalize(body.upload_id)
    except ValueError as e:
        await uploads.drop(body.upload_id)
        raise HTTPException(400, str(e))

    if not ok:
        await uploads.drop(body.upload_id)
        await store.mark_error(body.agent_token, "sha256_mismatch")
        raise HTTPException(400, "sha256_mismatch")

    # перемещаем в архивы
    final = settings.work_dir / "archives" / f"{p.pairing_id}__{s.filename}"
    final.parent.mkdir(parents=True, exist_ok=True)
    s.tmp_path.rename(final)
    p.upload_path = str(final)
    p.archive_bytes = received

    # запускаем доставку в бота в фоне
    asyncio.create_task(_deliver_to_bot(p.polling_token, body.agent_token, final))

    return UploadFinalizeOk(verified=True, bytes=received)


async def _deliver_to_bot(polling_token: str, agent_token: str, zip_path: Path) -> None:
    try:
        # контрольная сверка перед отправкой
        got = sha256_file(zip_path)
        p = store.get_by_poll(polling_token)
        if p.upload_sha256 and got != p.upload_sha256:
            await store.mark_error(agent_token, "sha256_mismatch_before_send")
            return

        await store.update_progress(agent_token, "forwarding", 50, "передача в Telegram-бот")

        async def progress(pct: int, stage: str) -> None:
            # 50..100
            total = 50 + int(pct * 0.5)
            await store.update_progress(agent_token, "forwarding", total, stage)

        caption = f"Telegram Desktop Export · {zip_path.name}"
        await send_archive_to_owner(zip_path, caption=caption, progress=progress)
        await store.mark_delivered(agent_token)
    except Exception as e:
        logger.exception("delivery failed")
        try:
            await store.mark_error(agent_token, f"delivery_failed: {type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        cleanup_archive(zip_path)


# ---- глобальные ошибки ----

@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.backend_host, port=settings.backend_port, reload=False)
