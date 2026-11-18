"""Validated data contracts shared by storage, services, and HTTP routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


MessageRole = Literal["system", "user", "assistant", "tool"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OpenAIMessage(BaseModel):
    """A message directly usable in an OpenAI Chat Completions request."""

    role: MessageRole
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content")
    @classmethod
    def validate_parts(cls, value: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """Accept portable Chat Completions parts; reject malformed imports."""
        if isinstance(value, list):
            for part in value:
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    continue
                if kind == "image_url" and isinstance(part.get("image_url"), dict) and isinstance(part["image_url"].get("url"), str):
                    continue
                if kind == "file" and isinstance(part.get("file"), dict) and isinstance(part["file"].get("file_data"), str):
                    continue
                raise ValueError("仅支持 text、image_url 和含 file_data 的 file 内容块")
        return value

    def text_content(self) -> str:
        """Text projection used by search, titles, and text-only providers."""
        if isinstance(self.content, str):
            return self.content
        return "\n\n".join(part["text"] for part in self.content if part.get("type") == "text")

    def request_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            data["name"] = self.name
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        return data


class MessageMetadata(BaseModel):
    """Lossless sidecar fields, excluded from standard message serialization.

    Explicitly supported provider extensions may be replayed by the chat route
    to the same endpoint only; all other local metadata stays on this device.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = Field(default_factory=utc_now)
    reasoning_content: str | None = None
    provider_message_id: str | int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ConversationArchive(BaseModel):
    """OpenAI-compatible transcript plus lossless sidecar metadata."""

    format: Literal["openai-chat-completions"] = "openai-chat-completions"
    version: str = "1.2"
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = "新对话"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    source: dict[str, Any] = Field(default_factory=lambda: {"type": "local"})
    model: dict[str, Any] = Field(default_factory=dict)
    title_info: dict[str, Any] = Field(default_factory=lambda: {"method": "fallback"})
    tags: list[str] = Field(default_factory=list)
    folder: str | None = None
    starred: bool = False
    pinned: bool = False
    messages: list[OpenAIMessage] = Field(default_factory=list)
    message_metadata: list[MessageMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def align_metadata(self) -> "ConversationArchive":
        """Keep the sidecar array positionally aligned with ``messages``.

        Older or third-party JSON files may omit metadata.  Repairing alignment
        here gives every downstream caller one simple invariant to rely on.
        """
        if len(self.message_metadata) > len(self.messages):
            self.message_metadata = self.message_metadata[: len(self.messages)]
        while len(self.message_metadata) < len(self.messages):
            self.message_metadata.append(MessageMetadata())
        return self

    def add_message(
        self,
        role: MessageRole,
        content: str | list[dict[str, Any]],
        *,
        reasoning_content: str | None = None,
        provider_message_id: str | int | None = None,
        created_at: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> tuple[OpenAIMessage, MessageMetadata]:
        message = OpenAIMessage(role=role, content=content)
        metadata = MessageMetadata(
            created_at=created_at or utc_now(),
            reasoning_content=reasoning_content,
            provider_message_id=provider_message_id,
            details=details or {},
        )
        self.messages.append(message)
        self.message_metadata.append(metadata)
        self.updated_at = utc_now()
        return message, metadata


class ConversationSummary(BaseModel):
    """Small list-view projection; full transcripts stay out of list responses."""

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    source_type: str
    preview: str
    tags: list[str] = Field(default_factory=list)
    folder: str | None = None
    starred: bool = False
    pinned: bool = False
    title_method: str = "fallback"


class DeepSeekImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    profile_id: str | None = None
    duplicate_action: Literal["skip", "copy"] = "skip"


class DeepSeekBatchImportRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    profile_id: str | None = None
    duplicate_action: Literal["skip", "copy"] = "skip"


class JsonImportRequest(BaseModel):
    archive: dict[str, Any]
    fallback_title: str = "导入的对话"
    profile_id: str | None = None


class ConversationCreateRequest(BaseModel):
    title: str = Field(default="新对话", max_length=120)


class ConversationUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    tags: list[str] | None = None
    folder: str | None = Field(default=None, max_length=120)
    starred: bool | None = None
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("标题不能为空")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(" ".join(item.split())[:40] for item in value if item.strip()))[:20]


class BatchConversationRequest(BaseModel):
    """Validated payload for bulk recycle-bin and organization operations."""

    ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["trash", "organize"]
    tags: list[str] | None = None
    folder: str | None = Field(default=None, max_length=120)
    starred: bool | None = None
    pinned: bool | None = None

    @field_validator("ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not cleaned:
            raise ValueError("至少需要一个有效的对话 ID")
        return cleaned

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(" ".join(item.split())[:40] for item in value if item.strip()))[:20]


class ProviderConfig(BaseModel):
    """One OpenAI-compatible endpoint configuration.

    ``api_key`` is accepted at request time but removed before a profile is
    serialized.  See :mod:`app.profiles` for credential-vault handling.
    """
    provider_id: str = "custom"
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field(default="", max_length=4000)
    model: str = Field(min_length=1, max_length=300)
    temperature: float | None = Field(default=0.7, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    system_prompt: str = Field(default="", max_length=100_000)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120, ge=5, le=600)
    attachment_mode: Literal["text", "images", "files"] = "text"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        return value

    @field_validator("extra_headers")
    @classmethod
    def block_unsafe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        blocked = {"host", "content-length", "transfer-encoding"}
        if any(key.lower() in blocked for key in value):
            raise ValueError("自定义请求头包含不安全字段")
        return value


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    provider: ProviderConfig | None = None
    profile_id: str | None = None
    context_mode: Literal["all", "recent"] = "all"
    recent_messages: int = Field(default=20, ge=2, le=200)
    # None preserves provider defaults (including after automatic fallback).
    thinking: bool | None = None
    web_search: bool | None = None

    @model_validator(mode="after")
    def require_provider(self) -> "ChatRequest":
        if not self.provider and not self.profile_id:
            raise ValueError("必须提供模型配置或配置档案")
        return self

    @field_validator("content")
    @classmethod
    def non_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class ConnectionTestRequest(BaseModel):
    provider: ProviderConfig


class RenderContentRequest(BaseModel):
    """Bounded batch of message bodies for local Markdown/LaTeX rendering."""

    contents: list[Annotated[str, Field(max_length=1_000_000)]] = Field(min_length=1, max_length=100)


class ProviderProfileUpsert(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    provider: ProviderConfig


class BackupSettings(BaseModel):
    """User schedule plus server-owned last-run status fields."""

    auto_enabled: bool = False
    destination: str = ""
    interval_hours: int = Field(default=24, ge=1, le=720)
    retention_count: int = Field(default=14, ge=1, le=365)
    trash_retention_days: int = Field(default=30, ge=1, le=3650)
    last_backup_at: str | None = None
    last_backup_path: str | None = None
    last_error: str | None = None


class RestoreRequest(BaseModel):
    conflict: Literal["skip", "replace", "copy"] = "skip"
