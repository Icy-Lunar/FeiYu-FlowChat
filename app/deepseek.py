"""DeepSeek public-share URL validation, download, and lossless conversion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import ImportError
from .attachments import share_attachments
from .models import ConversationArchive, MessageRole, utc_now


_SHARE_ID = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")
_SHARE_API = "https://chat.deepseek.com/api/v0/share/content"


@dataclass(slots=True)
class ParsedShareMessage:
    """Normalized message plus the source fields needed for its sidecar."""

    role: MessageRole
    content: str
    reasoning: str | None
    raw: dict[str, Any]
    details: dict[str, Any]


def parse_share_url(url: str) -> tuple[str, str]:
    """Validate a public share URL and return its ID and canonical form."""
    value = url.strip().replace("\\/", "/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "chat.deepseek.com":
        raise ImportError("请输入 https://chat.deepseek.com/share/... 格式的 DeepSeek 分享链接")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "share" or not _SHARE_ID.fullmatch(parts[1]):
        raise ImportError("DeepSeek 分享链接格式不正确")
    canonical = f"https://chat.deepseek.com/share/{parts[1]}"
    return parts[1], canonical


def _timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return utc_now()


def _join_fragments(message: dict[str, Any], wanted: set[str]) -> str:
    values: list[str] = []
    for fragment in message.get("fragments") or []:
        if not isinstance(fragment, dict) or fragment.get("type") not in wanted:
            continue
        content = fragment.get("content")
        if isinstance(content, str) and content:
            values.append(content)
    return "\n\n".join(values).strip()


def parse_share_payload(payload: dict[str, Any], *, share_id: str, source_url: str) -> ConversationArchive:
    """Convert DeepSeek's share payload without discarding useful metadata."""
    if payload.get("code") not in (0, None):
        raise ImportError(str(payload.get("msg") or "DeepSeek 返回了错误"), status_code=502)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ImportError("DeepSeek 返回数据格式异常", status_code=502)
    if data.get("biz_code") != 0:
        biz_code = data.get("biz_code")
        if biz_code in {10002, 10003, 10004}:
            raise ImportError("该 DeepSeek 分享链接不存在、已失效或已被删除", status_code=404)
        raise ImportError(str(data.get("biz_msg") or f"DeepSeek 导入失败（{biz_code}）"), status_code=422)
    biz_data = data.get("biz_data")
    if not isinstance(biz_data, dict) or not isinstance(biz_data.get("messages"), list):
        raise ImportError("分享链接中没有可读取的对话", status_code=422)

    raw_messages: list[dict[str, Any]] = [item for item in biz_data["messages"] if isinstance(item, dict)]
    first_user = ""
    converted: list[ParsedShareMessage] = []

    for raw in raw_messages:
        raw_role = str(raw.get("role", "")).upper()
        if raw_role == "USER":
            role: MessageRole = "user"
            content = _join_fragments(raw, {"REQUEST", "RESPONSE"})
            reasoning = None
            first_user = first_user or content
        elif raw_role == "ASSISTANT":
            role = "assistant"
            content = _join_fragments(raw, {"RESPONSE"})
            reasoning = _join_fragments(raw, {"THINK"}) or None
            if not content:
                content = _join_fragments(raw, {"REQUEST"})
        elif raw_role == "SYSTEM":
            role = "system"
            content = _join_fragments(raw, {"REQUEST", "RESPONSE"})
            reasoning = None
        else:
            continue
        attachments = share_attachments(raw)
        if not content and not reasoning and not attachments:
            continue
        fragment_summary = [
            {
                key: fragment.get(key)
                for key in ("id", "type", "elapsed_secs", "stage_id", "references")
                if key in fragment
            }
            for fragment in raw.get("fragments") or []
            if isinstance(fragment, dict)
        ]
        details = {
            "source": "deepseek_share",
            "original_role": raw_role,
            "status": raw.get("status"),
            "thinking_enabled": raw.get("thinking_enabled"),
            "search_enabled": raw.get("search_enabled"),
            "accumulated_token_usage": raw.get("accumulated_token_usage"),
            "fragments": fragment_summary,
            "attachments": attachments,
        }
        converted.append(ParsedShareMessage(role, content, reasoning, raw, details))

    if not converted:
        raise ImportError("分享链接中没有可导入的消息", status_code=422)

    raw_title = str(biz_data.get("title") or "").strip()
    if not raw_title or raw_title.lower() == "shared conversation":
        raw_title = " ".join(first_user.split())[:38] or "DeepSeek 历史对话"
    archive = ConversationArchive(
        title=raw_title[:120],
        source={
            "type": "deepseek_share",
            "url": source_url,
            "share_id": share_id,
            "imported_at": utc_now(),
            "original_model_type": biz_data.get("model_type"),
        },
        model={"provider": "DeepSeek Web", "model": biz_data.get("model_type") or "unknown"},
    )
    for item in converted:
        archive.add_message(
            item.role,
            item.content,
            reasoning_content=item.reasoning,
            provider_message_id=item.raw.get("message_id"),
            created_at=_timestamp(item.raw.get("inserted_at")),
            details=item.details,
        )
    if archive.message_metadata:
        archive.created_at = archive.message_metadata[0].created_at
    archive.updated_at = utc_now()
    return archive

async def fetch_deepseek_share(url: str, *, client: httpx.AsyncClient | None = None) -> ConversationArchive:
    """Fetch one public share and attach its raw payload for local persistence.

    ``_raw_payload`` is deliberately temporary.  The import service removes it
    before writing the OpenAI-compatible archive and stores it in ``data/raw``.
    """
    share_id, canonical = parse_share_url(url)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)
    try:
        response = await http.get(
            _SHARE_API,
            params={"share_id": share_id},
            headers={
                "Accept": "application/json",
                "Origin": "https://chat.deepseek.com",
                "Referer": canonical,
                "User-Agent": "FeiYu-FlowChat/0.5 (+local archive importer)",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise ImportError("连接 DeepSeek 超时，请稍后重试", status_code=504) from exc
    except httpx.HTTPStatusError as exc:
        raise ImportError(f"DeepSeek 返回 HTTP {exc.response.status_code}", status_code=502) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise ImportError("无法读取 DeepSeek 分享内容，请检查网络或链接是否有效", status_code=502) from exc
    finally:
        if owns_client:
            await http.aclose()
    if not isinstance(payload, dict):
        raise ImportError("DeepSeek 返回数据格式异常", status_code=502)
    archive = parse_share_payload(payload, share_id=share_id, source_url=canonical)
    # Kept only in memory until the import route writes an immutable raw sidecar.
    archive.source["_raw_payload"] = payload
    return archive
