"""FastAPI composition root and HTTP/SSE transport layer.

Business logic lives in focused modules (storage, backup, DeepSeek conversion,
profiles, and LLM transport).  This file owns request validation, lifecycle
tasks, local-browser protection, and conversion to JSON or SSE responses.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .backup import BackupManager
from .attachments import SAFE_IMAGES, decode_part, preserve_attachments
from .chat_modes import mode_capabilities
from .config import (
    APP_NAME, APP_VERSION, ARCHIVE_DIR, BACKUP_DIR, DATA_DIR, PROFILES_PATH, SETTINGS_PATH,
    STATIC_DIR, ensure_data_dirs,
)
from .deepseek import fetch_deepseek_share, parse_share_url
from .errors import FlowChatError, ImportError, ProviderError
from .llm import PROVIDER_PRESETS, generate_title, list_provider_models, stream_chat, test_connection
from .models import (
    BackupSettings, BatchConversationRequest, ChatRequest, ConnectionTestRequest, ConversationArchive,
    ConversationCreateRequest,
    ConversationUpdateRequest, DeepSeekBatchImportRequest, DeepSeekImportRequest, JsonImportRequest,
    OpenAIMessage, ProviderConfig, ProviderProfileUpsert, RenderContentRequest, utc_now,
)
from .profiles import ProviderProfileStore
from .rendering import render_messages
from .storage import ArchiveStore


ensure_data_dirs()
store = ArchiveStore(ARCHIVE_DIR)
profile_store = ProviderProfileStore(PROFILES_PATH)
backup_manager = BackupManager(DATA_DIR, SETTINGS_PATH, store)
conversation_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
SESSION_TOKEN = secrets.token_urlsafe(32)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle and local-browser security
# ---------------------------------------------------------------------------


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _automatic_backup_loop() -> None:
    """Run inexpensive maintenance and record failures for the settings UI."""
    while True:
        try:
            settings = backup_manager.get_settings()
            last = _parse_time(settings.last_backup_at)
            due = not last or datetime.now(timezone.utc) - last >= timedelta(hours=settings.interval_hours)
            if settings.auto_enabled and settings.destination and due:
                await asyncio.to_thread(backup_manager.automatic_backup)
            await asyncio.to_thread(store.cleanup_trash, settings.trash_retention_days)
        except Exception as exc:
            logger.exception("Background backup/retention maintenance failed")
            try:
                settings = backup_manager.get_settings()
                settings.last_error = f"{utc_now()} · {exc.__class__.__name__}: {str(exc)[:240]}"
                backup_manager.save_settings(settings)
            except Exception:
                logger.exception("Could not persist the background-maintenance error")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_data_dirs()
    task = asyncio.create_task(_automatic_backup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])


@app.middleware("http")
async def protect_local_mutations(request: Request, call_next):
    """Reject cross-origin writes and requests not issued by this UI session."""
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-flowchat-token", "")
        if not hmac.compare_digest(supplied, SESSION_TOKEN):
            return JSONResponse(status_code=403, content={"error": {"code": "local_token_required", "message": "本地会话令牌无效，请刷新页面"}})
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).hostname or "").lower() not in {"localhost", "127.0.0.1"}:
            return JSONResponse(status_code=403, content={"error": {"code": "origin_rejected", "message": "拒绝非本地页面调用"}})
    return await call_next(request)


@app.exception_handler(FlowChatError)
async def flowchat_error_handler(_: Request, exc: FlowChatError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})


# ---------------------------------------------------------------------------
# Provider profiles and connection checks
# ---------------------------------------------------------------------------


@app.get("/api/session")
async def local_session() -> dict[str, Any]:
    return {"token": SESSION_TOKEN, "name": APP_NAME, "version": APP_VERSION}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "name": APP_NAME, "version": APP_VERSION}


@app.get("/api/providers")
async def providers() -> dict[str, Any]:
    return {"providers": PROVIDER_PRESETS}


@app.post("/api/render")
async def render_content(body: RenderContentRequest) -> dict[str, list[str]]:
    """Render message presentation without changing archived source text."""
    html = await asyncio.to_thread(render_messages, body.contents)
    return {"html": html}


@app.get("/api/provider-profiles")
async def list_provider_profiles() -> dict[str, Any]:
    profiles = profile_store.list()
    for profile in profiles:
        profile["capabilities"] = mode_capabilities(ProviderConfig.model_validate(profile["provider"]))
    return {"profiles": profiles}


@app.post("/api/provider-profiles")
async def save_provider_profile(body: ProviderProfileUpsert) -> dict[str, Any]:
    return {"profile": profile_store.save(body)}


@app.delete("/api/provider-profiles/keys")
async def clear_provider_keys() -> dict[str, Any]:
    return {"cleared": profile_store.clear_all_keys()}


@app.delete("/api/provider-profiles/{profile_id}", status_code=204)
async def delete_provider_profile(profile_id: str) -> Response:
    profile_store.delete(profile_id)
    return Response(status_code=204)


def _resolve_provider(provider: ProviderConfig | None, profile_id: str | None) -> ProviderConfig:
    if profile_id:
        return profile_store.resolve(profile_id)
    if provider:
        return provider
    raise ProviderError("未配置模型服务", status_code=422)


@app.post("/api/providers/test")
async def provider_test(body: ConnectionTestRequest) -> dict[str, Any]:
    return await test_connection(body.provider)


@app.post("/api/provider-profiles/{profile_id}/test")
async def profile_test(profile_id: str) -> dict[str, Any]:
    return await test_connection(profile_store.resolve(profile_id))


@app.get("/api/provider-profiles/{profile_id}/models")
async def provider_models(profile_id: str) -> dict[str, Any]:
    return {"models": await list_provider_models(profile_store.resolve(profile_id))}


@app.get("/api/conversations")
async def list_conversations(
    q: str = "", source: str | None = None, starred: bool | None = None,
    folder: str | None = None, tags: list[str] = Query(default=[]),
) -> dict[str, Any]:
    return {"conversations": [item.model_dump() for item in store.list(
        query=q, source_type=source, starred=starred, folder=folder, tags=tags,
    )]}


# ---------------------------------------------------------------------------
# Active conversations and revisions
# ---------------------------------------------------------------------------


@app.post("/api/conversations", status_code=201)
async def create_conversation(body: ConversationCreateRequest) -> dict[str, Any]:
    archive = store.create(title=body.title.strip() or "新对话")
    return {"conversation": archive.model_dump(exclude_none=True)}


@app.post("/api/conversations/batch")
async def batch_conversations(body: BatchConversationRequest) -> dict[str, Any]:
    """Apply one validated operation to at most 200 unique conversations."""
    changes = body.model_dump(
        exclude={"ids", "action"},
        exclude_unset=True,
    )
    updated = 0
    for conversation_id in body.ids:
        if body.action == "trash":
            store.move_to_trash(conversation_id)
        else:
            store.update(conversation_id, **changes)
        updated += 1
    return {"updated": updated}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict[str, Any]:
    return {"conversation": store.get(conversation_id).model_dump(exclude_none=True)}


@app.patch("/api/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdateRequest) -> dict[str, Any]:
    archive = store.update(conversation_id, **body.model_dump(exclude_unset=True))
    return {"conversation": archive.model_dump(exclude_none=True)}


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str) -> Response:
    store.move_to_trash(conversation_id)
    return Response(status_code=204)


@app.get("/api/conversations/{conversation_id}/export")
async def export_conversation(conversation_id: str) -> FileResponse:
    archive = store.get(conversation_id)
    safe_title = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", archive.title).strip("-")[:60] or "conversation"
    return FileResponse(store.archive_dir / f"{archive.id}.json", media_type="application/json; charset=utf-8",
                        filename=f"{safe_title}-{archive.id[:8]}.json")


@app.get("/api/conversations/{conversation_id}/versions")
async def conversation_versions(conversation_id: str) -> dict[str, Any]:
    return {"versions": store.list_versions(conversation_id)}


@app.get("/api/conversations/{conversation_id}/attachments/{message_index}/{part_index}")
async def get_attachment(conversation_id: str, message_index: int, part_index: int, download: bool = False) -> Response:
    """Serve only embedded archive parts; never act as a remote URL proxy."""
    archive = store.get(conversation_id)
    try:
        if message_index < 0 or part_index < 0:
            raise ValueError()
        content = archive.messages[message_index].content
        if not isinstance(content, list):
            raise ValueError()
        part = content[part_index]
        mime, data = decode_part(part)
    except (IndexError, ValueError, TypeError) as exc:
        raise ImportError("附件未保存在本机或内容无效", status_code=404) from exc
    name = (part.get("file") or {}).get("filename", "image." + {"image/jpeg": "jpg"}.get(mime, mime.split("/")[-1]))
    # Only raster images are inline. PDFs/HTML/office files are downloaded to
    # prevent untrusted documents from executing under the localhost origin.
    disposition = "inline" if mime in SAFE_IMAGES and not download else "attachment"
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(str(name), safe='')}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox",
    })


@app.post("/api/conversations/{conversation_id}/versions/{version_name}/restore")
async def restore_conversation_version(conversation_id: str, version_name: str) -> dict[str, Any]:
    return {"conversation": store.restore_version(conversation_id, version_name).model_dump(exclude_none=True)}


def _fallback_title(archive: ConversationArchive) -> str:
    """Return the required last-resort title from the first user message."""
    first = next((message.text_content() for message in archive.messages if message.role == "user" and message.text_content().strip()), "")
    return " ".join(first.split())[:38] or "未命名对话"


async def _set_generated_title(archive: ConversationArchive, config: ProviderConfig | None) -> None:
    """Generate a title, falling back without ever failing the main workflow."""
    archive.title = _fallback_title(archive)
    archive.title_info = {"method": "fallback", "updated_at": utc_now()}
    if not config:
        return
    try:
        archive.title = await generate_title(config, archive.messages)
        archive.title_info = {"method": "model", "model": config.model, "provider": config.provider_id,
                              "generated_at": utc_now()}
    except FlowChatError as exc:
        archive.title_info = {"method": "fallback", "reason": exc.message, "updated_at": utc_now()}
    except Exception as exc:
        # Title generation is deliberately best-effort: an otherwise valid
        # import or completed reply must never be lost because this extra
        # model request is unavailable or returns a vendor-specific shape.
        archive.title_info = {"method": "fallback", "reason": exc.__class__.__name__, "updated_at": utc_now()}
        logger.exception("Unexpected title generation failure for archive %s", archive.id)


async def _import_deepseek(body: DeepSeekImportRequest) -> tuple[ConversationArchive, bool]:
    """Import one share, persist its raw snapshot, and assign a safe title."""
    share_id, _ = parse_share_url(body.url)
    existing = store.find_by_share_id(share_id)
    if existing and body.duplicate_action == "skip":
        return existing, True
    archive = await fetch_deepseek_share(body.url)
    raw_payload = archive.source.pop("_raw_payload", None)
    await preserve_attachments(archive)
    config = profile_store.resolve(body.profile_id) if body.profile_id else None
    await _set_generated_title(archive, config)
    if existing and body.duplicate_action == "copy":
        archive.title = f"{archive.title}（副本）"[:120]
        archive.title_info["duplicate_copy"] = True
    if isinstance(raw_payload, dict):
        archive.source["raw_snapshot"] = store.save_raw(archive.id, raw_payload)
    store.save(archive, create_version=False)
    return archive, False


# ---------------------------------------------------------------------------
# Imports, recycle bin, health checks, and full backups
# ---------------------------------------------------------------------------


@app.post("/api/import/deepseek", status_code=201)
async def import_deepseek(body: DeepSeekImportRequest) -> dict[str, Any]:
    archive, duplicate = await _import_deepseek(body)
    return {"conversation": archive.model_dump(exclude_none=True), "duplicate": duplicate}


@app.post("/api/import/deepseek/batch")
async def import_deepseek_batch(body: DeepSeekBatchImportRequest) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for url in list(dict.fromkeys(item.strip() for item in body.urls if item.strip())):
        try:
            archive, duplicate = await _import_deepseek(DeepSeekImportRequest(
                url=url, profile_id=body.profile_id, duplicate_action=body.duplicate_action,
            ))
            results.append({"url": url, "ok": True, "duplicate": duplicate, "id": archive.id, "title": archive.title})
        except FlowChatError as exc:
            results.append({"url": url, "ok": False, "error": exc.message})
    return {"results": results, "success": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
            "duplicates": sum(1 for item in results if item.get("duplicate"))}


@app.post("/api/import/json", status_code=201)
async def import_json(body: JsonImportRequest) -> dict[str, Any]:
    archive = store.import_openai(body.archive, body.fallback_title)
    config = profile_store.resolve(body.profile_id) if body.profile_id else None
    await _set_generated_title(archive, config)
    store.save(archive)
    return {"conversation": archive.model_dump(exclude_none=True)}


@app.get("/api/trash")
async def list_trash() -> dict[str, Any]:
    return {"items": store.list_trash()}


@app.post("/api/trash/{trash_name}/restore")
async def restore_trash(trash_name: str) -> dict[str, Any]:
    return {"conversation": store.restore_from_trash(trash_name).model_dump(exclude_none=True)}


@app.delete("/api/trash/{trash_name}", status_code=204)
async def permanently_delete_trash(trash_name: str) -> Response:
    store.permanently_delete_trash(trash_name)
    return Response(status_code=204)


@app.get("/api/data/health")
async def data_health() -> dict[str, Any]:
    return store.list_health_issues()


@app.post("/api/data/repair")
async def repair_data() -> dict[str, Any]:
    return store.repair()


@app.get("/api/backups/settings")
async def get_backup_settings() -> dict[str, Any]:
    return {"settings": backup_manager.get_settings().model_dump(exclude_none=True)}


@app.put("/api/backups/settings")
async def update_backup_settings(body: BackupSettings) -> dict[str, Any]:
    # Runtime status is server-owned; editing the schedule must not erase a
    # success path or an error the user still needs to notice.
    settings = backup_manager.get_settings()
    for field in ("auto_enabled", "destination", "interval_hours", "retention_count", "trash_retention_days"):
        setattr(settings, field, getattr(body, field))
    return {"settings": backup_manager.save_settings(settings).model_dump(exclude_none=True)}


@app.post("/api/backups/create")
async def create_backup() -> dict[str, Any]:
    path = await asyncio.to_thread(backup_manager.create, BACKUP_DIR)
    return {"name": path.name, "size": path.stat().st_size, "download_url": f"/api/backups/{path.name}",
            "created_at": utc_now()}


@app.get("/api/backups/{backup_name}")
async def download_backup(backup_name: str) -> FileResponse:
    if Path(backup_name).name != backup_name or not backup_name.endswith(".zip"):
        raise ImportError("备份文件名无效")
    path = BACKUP_DIR / backup_name
    if not path.is_file():
        raise ImportError("找不到备份文件", status_code=404)
    return FileResponse(path, media_type="application/zip", filename=backup_name)


@app.post("/api/backups/restore")
async def restore_backup(request: Request, conflict: str = "skip") -> dict[str, Any]:
    if conflict not in {"skip", "replace", "copy"}:
        raise ImportError("冲突策略无效")
    content = await request.body()
    if not content or len(content) > 500 * 1024 * 1024:
        raise ImportError("备份文件为空或超过 500 MB")
    fd, temp_name = tempfile.mkstemp(prefix="flowchat-restore-", suffix=".zip", dir=BACKUP_DIR)
    os.close(fd)
    try:
        Path(temp_name).write_bytes(content)
        return await asyncio.to_thread(backup_manager.restore, Path(temp_name), conflict)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _sse(event: str, data: dict[str, Any]) -> str:
    """Encode one Server-Sent Events frame without ASCII-escaping Chinese."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/api/conversations/{conversation_id}/chat")
async def chat(conversation_id: str, body: ChatRequest) -> StreamingResponse:
    """Append a user turn and stream/persist the provider response.

    A per-conversation lock prevents two browser submissions from interleaving
    writes to the same JSON archive.  Other conversations remain concurrent.
    """
    store.get(conversation_id)
    config = _resolve_provider(body.provider, body.profile_id)

    async def generate() -> AsyncIterator[str]:
        async with conversation_locks[conversation_id]:
            archive = store.get(conversation_id)
            content = body.content.strip()
            if not archive.messages and archive.title_info.get("method") not in {"model", "manual"}:
                archive.title = " ".join(content.split())[:38] or "新对话"
                archive.title_info = {"method": "fallback", "updated_at": utc_now()}
            archive.model = {"provider": config.provider_id, "base_url": config.base_url, "model": config.model,
                             "temperature": config.temperature, "max_tokens": config.max_tokens,
                             "profile_id": body.profile_id, "last_used_at": utc_now()}
            user_message, user_meta = archive.add_message("user", content, details={"source": "local"})
            store.save(archive)
            yield _sse("accepted", {"message": user_message.model_dump(exclude_none=True),
                                    "metadata": user_meta.model_dump(exclude_none=True)})

            # Extension context is endpoint-specific, so never forward it to
            # another model or a different gateway when the profile changes.
            history = []
            for message, meta in zip(archive.messages, archive.message_metadata):
                item = message.request_dict()
                details = meta.details
                same_endpoint = (details.get("provider") == config.provider_id and details.get("model") == config.model
                                 and details.get("base_url") == config.base_url)
                if message.role == "assistant" and same_endpoint:
                    extensions = details.get("provider_extensions") or {}
                    if not isinstance(extensions, dict):
                        extensions = {}
                    for key in ("server_tool_calls", "reasoning_details", "reasoning_content"):
                        if key in extensions:
                            item[key] = extensions[key]
                    if config.provider_id == "deepseek":
                        item.setdefault("reasoning_content", meta.reasoning_content or "")
                history.append(item)
            if body.context_mode == "recent":
                history = history[-body.recent_messages:]
            request_messages = history
            if config.system_prompt.strip():
                request_messages.insert(0, {"role": "system", "content": config.system_prompt.strip()})
            answer_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict[str, Any] = {}
            response_id: str | None = None
            finish_reason: str | None = None
            mode_fields = {"thinking", "web_search"}
            modes = {name: getattr(body, name) for name in mode_fields} if body.model_fields_set & mode_fields else None
            effective_modes = modes
            fallback_notice = ""
            extensions: dict[str, Any] = {}
            last_step_reasoning: list[str] = []
            assistant_recorded = False

            def save_partial(status: str) -> None:
                """A failed stream must not discard text already shown to the user."""
                if assistant_recorded:
                    # Cancellation can also arrive during the auxiliary title
                    # request, after the completed assistant was appended.
                    store.save(archive)
                    return
                partial, reasoning = "".join(answer_parts), "".join(reasoning_parts) or None
                if partial or reasoning:
                    archive.add_message("assistant", partial, reasoning_content=reasoning,
                                        provider_message_id=response_id,
                                        details={"source": "api", "status": status, "model": config.model,
                                                 "modes": effective_modes, "mode_notice": fallback_notice,
                                                 "provider_extensions": extensions})
                    store.save(archive)
            try:
                async for event in stream_chat(config, request_messages, modes=modes):
                    event_type = event.get("type")
                    if event_type == "delta":
                        answer_parts.append(str(event.get("content", "")))
                        yield _sse("delta", {"content": event.get("content", "")})
                    elif event_type == "reasoning_delta":
                        reasoning_parts.append(str(event.get("content", "")))
                        last_step_reasoning.append(str(event.get("content", "")))
                        yield _sse("reasoning_delta", {"content": event.get("content", "")})
                    elif event_type == "mode_status":
                        effective_modes = event["modes"]
                        if event.get("fallback"):
                            fallback_notice = event["message"]
                            response_id = None
                        archive.model.update({"modes": effective_modes, "mode_notice": fallback_notice})
                        store.save(archive)
                        yield _sse("mode_status", event)
                    elif event_type == "provider_metadata":
                        for key, value in event["metadata"].items():
                            if key == "server_tool_calls":
                                extensions[key] = value  # DeepSeek sends a complete final snapshot.
                            elif key == "reasoning_details":
                                # OpenRouter may split one indexed reasoning item across chunks.
                                target = extensions.setdefault(key, [])
                                for part in value:
                                    if not isinstance(part, dict):
                                        continue
                                    existing = next((p for p in target if "index" in part and p.get("index") == part["index"]), None)
                                    if existing is None:
                                        target.append(dict(part))
                                    else:
                                        for field, fragment in part.items():
                                            existing[field] = (existing.get(field, "") + fragment) if field in {"text", "data", "signature"} and isinstance(fragment, str) else fragment
                            else:
                                extensions.setdefault(key, []).extend(value)
                    elif event_type == "tool_activity":
                        activity = event["event"]
                        if activity.get("type") == "step_reasoning_done":
                            last_step_reasoning.clear()
                        yield _sse("tool_activity", {"message": "正在联网检索…"})
                    elif event_type == "usage":
                        usage = event.get("usage") or {}
                    elif event_type == "response_id":
                        response_id = event.get("id")
                    elif event_type == "finish":
                        finish_reason = str(event.get("finish_reason") or "") or None
                answer = "".join(answer_parts)
                reasoning = "".join(reasoning_parts) or None
                if not answer and not reasoning:
                    raise ProviderError("模型返回了空响应")
                if config.provider_id == "deepseek" and extensions.get("server_tool_calls"):
                    extensions["reasoning_content"] = "".join(last_step_reasoning)
                assistant, assistant_meta = archive.add_message(
                    "assistant", answer, reasoning_content=reasoning, provider_message_id=response_id,
                    details={"source": "api", "provider": config.provider_id, "model": config.model,
                             "base_url": config.base_url, "modes": effective_modes,
                             "mode_notice": fallback_notice, "provider_extensions": extensions,
                             "finish_reason": finish_reason, "usage": usage,
                             "context_mode": body.context_mode, "context_messages": len(request_messages)},
                )
                assistant_recorded = True
                if archive.title_info.get("method") not in {"model", "manual"}:
                    await _set_generated_title(archive, config)
                store.save(archive)
                yield _sse("done", {"message": assistant.model_dump(exclude_none=True),
                                    "metadata": assistant_meta.model_dump(exclude_none=True),
                                    "conversation": {"id": archive.id, "title": archive.title,
                                                     "title_info": archive.title_info,
                                                     "updated_at": archive.updated_at}})
            except asyncio.CancelledError:
                save_partial("interrupted")
                raise
            except FlowChatError as exc:
                save_partial("failed")
                yield _sse("error", {"code": exc.code, "message": exc.message})
            except Exception:
                save_partial("failed")
                logger.exception("Unexpected chat failure for archive %s", conversation_id)
                yield _sse("error", {"code": "unexpected_error", "message": "生成回复时发生未预期错误"})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(STATIC_DIR) / "index.html")
