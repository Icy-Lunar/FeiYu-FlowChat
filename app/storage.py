"""Crash-safe local persistence for active archives and recovery sidecars."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ConversationNotFound, ImportError
from .models import ConversationArchive, ConversationSummary, MessageMetadata, OpenAIMessage, utc_now


_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


def _atomic_json(path: Path, payload: Any) -> None:
    """Write JSON durably, then atomically replace the destination file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.stem}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as temp:
            json.dump(payload, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
            temp_name = temp.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class ArchiveStore:
    """Atomic archives with revisions, raw snapshots, trash and health checks."""

    def __init__(self, archive_dir: Path) -> None:
        self.archive_dir = Path(archive_dir)
        self.data_dir = self.archive_dir.parent
        self.trash_dir = self.archive_dir / ".trash"
        self.raw_dir = self.data_dir / "raw"
        self.versions_dir = self.data_dir / "versions"
        self.attachments_dir = self.data_dir / "attachments"
        for directory in (self.archive_dir, self.trash_dir, self.raw_dir, self.versions_dir, self.attachments_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, conversation_id: str) -> Path:
        if not _ID_PATTERN.fullmatch(conversation_id):
            raise ConversationNotFound()
        return self.archive_dir / f"{conversation_id}.json"

    def _snapshot_existing(self, path: Path, conversation_id: str) -> None:
        if not path.is_file():
            return
        revision_dir = self.versions_dir / conversation_id
        revision_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(path, revision_dir / f"{stamp}.json")
        for stale in sorted(revision_dir.glob("*.json"), reverse=True)[50:]:
            stale.unlink(missing_ok=True)

    def save(self, archive: ConversationArchive, *, create_version: bool = True) -> ConversationArchive:
        archive.updated_at = utc_now()
        path = self._path(archive.id)
        with self._lock:
            if create_version:
                self._snapshot_existing(path, archive.id)
            _atomic_json(path, archive.model_dump(exclude_none=True))
        return archive

    def create(self, *, title: str = "新对话", source: dict[str, Any] | None = None) -> ConversationArchive:
        archive = ConversationArchive(title=title, source=source or {"type": "local"})
        return self.save(archive, create_version=False)

    def get(self, conversation_id: str) -> ConversationArchive:
        path = self._path(conversation_id)
        with self._lock:
            if not path.is_file():
                raise ConversationNotFound()
            try:
                return ConversationArchive.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ImportError(f"归档文件损坏或无法读取：{path.name}") from exc

    def list(self, *, query: str = "", source_type: str | None = None, starred: bool | None = None,
             folder: str | None = None, tags: list[str] | None = None) -> list[ConversationSummary]:
        summaries: list[ConversationSummary] = []
        with self._lock:
            paths = list(self.archive_dir.glob("*.json"))
        needle = query.casefold().strip()
        wanted_tags = {tag.casefold() for tag in tags or []}
        for path in paths:
            try:
                archive = ConversationArchive.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if source_type and archive.source.get("type") != source_type:
                continue
            if starred is not None and archive.starred != starred:
                continue
            if folder is not None and (archive.folder or "") != folder:
                continue
            if wanted_tags and not wanted_tags.issubset({tag.casefold() for tag in archive.tags}):
                continue
            if needle:
                haystack = "\n".join([archive.title, archive.folder or "", *archive.tags,
                                       *(m.text_content() for m in archive.messages)]).casefold()
                if needle not in haystack:
                    continue
            preview = next((" ".join(m.text_content().split())[:100] for m in reversed(archive.messages)
                            if m.text_content().strip()), "")
            summaries.append(ConversationSummary(
                id=archive.id, title=archive.title, created_at=archive.created_at, updated_at=archive.updated_at,
                message_count=len(archive.messages), source_type=str(archive.source.get("type", "local")),
                preview=preview, tags=archive.tags, folder=archive.folder, starred=archive.starred,
                pinned=archive.pinned, title_method=str(archive.title_info.get("method", "fallback")),
            ))
        summaries.sort(key=lambda item: (item.pinned, item.updated_at), reverse=True)
        return summaries

    def update(self, conversation_id: str, **changes: Any) -> ConversationArchive:
        archive = self.get(conversation_id)
        for key in ("title", "tags", "folder", "starred", "pinned"):
            if key in changes and changes[key] is not None:
                setattr(archive, key, changes[key])
        if changes.get("title") is not None:
            archive.title_info = {"method": "manual", "updated_at": utc_now()}
        return self.save(archive)

    def move_to_trash(self, conversation_id: str) -> None:
        source = self._path(conversation_id)
        with self._lock:
            if not source.is_file():
                raise ConversationNotFound()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = self.trash_dir / f"{conversation_id}-{timestamp}.json"
            os.replace(source, target)
            # os.replace preserves the old mtime.  Retention must be based on
            # deletion time rather than the archive's last conversation turn.
            target.touch()

    def list_trash(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.trash_dir.glob("*.json"), reverse=True):
            try:
                archive = ConversationArchive.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append({"trash_name": path.name, "id": archive.id, "title": archive.title,
                          "message_count": len(archive.messages),
                          "deleted_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()})
        return items

    def restore_from_trash(self, trash_name: str) -> ConversationArchive:
        if Path(trash_name).name != trash_name or not trash_name.endswith(".json"):
            raise ImportError("回收站文件名无效")
        source = self.trash_dir / trash_name
        if not source.is_file():
            raise ConversationNotFound()
        archive = ConversationArchive.model_validate_json(source.read_text(encoding="utf-8"))
        target = self._path(archive.id)
        if target.exists():
            old_id = archive.id
            archive.id = str(uuid4())
            archive.title = f"{archive.title}（恢复）"
            target = self._path(archive.id)
            snapshot = archive.source.get("raw_snapshot")
            if isinstance(snapshot, dict):
                old_raw = self.data_dir / str(snapshot.get("path", ""))
                if old_raw.is_file():
                    new_raw = self.raw_dir / f"{archive.id}.json"
                    shutil.copy2(old_raw, new_raw)
                    archive.source["raw_snapshot"] = {
                        "path": f"raw/{new_raw.name}",
                        "sha256": hashlib.sha256(new_raw.read_bytes()).hexdigest(),
                        "saved_at": utc_now(),
                    }
                else:
                    archive.source.pop("raw_snapshot", None)
            archive.source["restored_from"] = old_id
            _atomic_json(target, archive.model_dump(exclude_none=True))
            source.unlink()
        else:
            os.replace(source, target)
        return archive

    def permanently_delete_trash(self, trash_name: str) -> None:
        if Path(trash_name).name != trash_name or not trash_name.endswith(".json"):
            raise ImportError("回收站文件名无效")
        path = self.trash_dir / trash_name
        if not path.is_file():
            raise ConversationNotFound()
        try:
            conversation_id = ConversationArchive.model_validate_json(path.read_text(encoding="utf-8")).id
        except (OSError, ValueError):
            possible_id = path.name[:36]
            conversation_id = possible_id if _ID_PATTERN.fullmatch(possible_id) else ""
        path.unlink()
        if conversation_id:
            (self.raw_dir / f"{conversation_id}.json").unlink(missing_ok=True)
            shutil.rmtree(self.versions_dir / conversation_id, ignore_errors=True)
            attachment_root = self.attachments_dir / conversation_id
            if attachment_root.is_dir():
                shutil.rmtree(attachment_root, ignore_errors=True)
            elif attachment_root.is_file():
                attachment_root.unlink(missing_ok=True)

    def cleanup_trash(self, days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = 0
        for path in self.trash_dir.glob("*.json"):
            if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def list_versions(self, conversation_id: str) -> list[dict[str, Any]]:
        self._path(conversation_id)
        items: list[dict[str, Any]] = []
        for path in sorted((self.versions_dir / conversation_id).glob("*.json"), reverse=True):
            try:
                archive = ConversationArchive.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append({"name": path.name, "updated_at": archive.updated_at, "title": archive.title,
                          "message_count": len(archive.messages)})
        return items

    def restore_version(self, conversation_id: str, version_name: str) -> ConversationArchive:
        if Path(version_name).name != version_name or not version_name.endswith(".json"):
            raise ImportError("版本文件名无效")
        version = self.versions_dir / conversation_id / version_name
        if not version.is_file():
            raise ConversationNotFound()
        archive = ConversationArchive.model_validate_json(version.read_text(encoding="utf-8"))
        archive.id = conversation_id
        archive.source["restored_version"] = version_name
        return self.save(archive)

    def save_raw(self, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._path(conversation_id)
        path = self.raw_dir / f"{conversation_id}.json"
        _atomic_json(path, payload)
        return {"path": f"raw/{path.name}", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "saved_at": utc_now()}

    def find_by_share_id(self, share_id: str) -> ConversationArchive | None:
        """Find an imported share in one disk pass.

        This path is used for duplicate detection.  Reading the summaries first
        used to deserialize every transcript twice, which became noticeable for
        large archives and batch imports.
        """
        with self._lock:
            paths = list(self.archive_dir.glob("*.json"))
        for path in paths:
            try:
                archive = ConversationArchive.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if archive.source.get("share_id") == share_id:
                return archive
        return None

    def list_health_issues(self) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        healthy = 0
        for path in self.archive_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                archive = ConversationArchive.model_validate(raw)
            except (OSError, ValueError) as exc:
                issues.append({"file": path.name, "type": "corrupt", "message": str(exc)[:240]})
                continue
            if len(raw.get("messages", [])) != len(raw.get("message_metadata", [])):
                issues.append({"file": path.name, "type": "metadata_mismatch", "message": "消息与元数据数量不一致"})
            snapshot = archive.source.get("raw_snapshot")
            if archive.source.get("type") == "deepseek_share" and not snapshot:
                issues.append({"file": path.name, "type": "missing_raw", "message": "缺少 DeepSeek 原始响应快照"})
            elif isinstance(snapshot, dict):
                raw_path = self.data_dir / str(snapshot.get("path", ""))
                if not raw_path.is_file():
                    issues.append({"file": path.name, "type": "missing_raw", "message": "原始响应文件不存在"})
                elif snapshot.get("sha256") and hashlib.sha256(raw_path.read_bytes()).hexdigest() != snapshot["sha256"]:
                    issues.append({"file": path.name, "type": "checksum", "message": "原始响应校验失败"})
            healthy += 1
        return {"ok": not issues, "healthy_archives": healthy, "issues": issues, "checked_at": utc_now()}

    def repair(self) -> dict[str, Any]:
        repaired: list[str] = []
        failed: list[str] = []
        for path in self.archive_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                archive = ConversationArchive.model_validate(raw)
                if len(raw.get("messages", [])) != len(raw.get("message_metadata", [])):
                    self.save(archive)
                    repaired.append(path.name)
            except (OSError, ValueError):
                restored = False
                for revision in sorted((self.versions_dir / path.stem).glob("*.json"), reverse=True):
                    try:
                        archive = ConversationArchive.model_validate_json(revision.read_text(encoding="utf-8"))
                        recovery_dir = self.versions_dir / path.stem
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                        shutil.copy2(path, recovery_dir / f"corrupt-{stamp}.json")
                        self.save(archive, create_version=False)
                        repaired.append(path.name)
                        restored = True
                        break
                    except (OSError, ValueError):
                        continue
                if not restored:
                    failed.append(path.name)
        return {"repaired": repaired, "failed": failed, "health": self.list_health_issues()}

    def import_openai(self, data: dict[str, Any], fallback_title: str) -> ConversationArchive:
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ImportError("JSON 中必须包含非空 messages 数组")
        archive = ConversationArchive(
            title=str(data.get("title") or fallback_title)[:120],
            source={"type": "json_import", "imported_at": utc_now(),
                    "original_format": data.get("format", "openai-compatible")},
            model=data.get("model") if isinstance(data.get("model"), dict) else {},
            tags=data.get("tags") if isinstance(data.get("tags"), list) else [],
            folder=data.get("folder") if isinstance(data.get("folder"), str) else None,
        )
        raw_metadata = data.get("message_metadata")
        for index, item in enumerate(raw_messages):
            if not isinstance(item, dict):
                raise ImportError(f"messages[{index}] 必须是对象")
            try:
                archive.messages.append(OpenAIMessage.model_validate(item))
            except ValueError as exc:
                raise ImportError(f"messages[{index}] 不符合 OpenAI 消息格式") from exc
            metadata_item = raw_metadata[index] if isinstance(raw_metadata, list) and index < len(raw_metadata) else {}
            try:
                archive.message_metadata.append(MessageMetadata.model_validate(metadata_item or {}))
            except ValueError:
                archive.message_metadata.append(MessageMetadata(details={"invalid_original_metadata": True}))
        return self.save(archive, create_version=False)
