"""Verified full-data backups and conflict-aware restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .errors import ImportError
from .models import BackupSettings, ConversationArchive, utc_now
from .storage import ArchiveStore, _atomic_json


class BackupManager:
    """Create and restore portable ZIP packages without API credentials.

    The manifest is verified before any destination file is changed.  API keys
    live in the OS credential vault, so ``provider_profiles.json`` is safe to
    include and requires users to re-enter keys on a different computer.
    """
    def __init__(self, data_dir: Path, settings_path: Path, store: ArchiveStore) -> None:
        self.data_dir = Path(data_dir)
        self.settings_path = Path(settings_path)
        self.store = store
        self.default_dir = self.data_dir / "backups"
        self.default_dir.mkdir(parents=True, exist_ok=True)

    def get_settings(self) -> BackupSettings:
        if not self.settings_path.is_file():
            return BackupSettings()
        try:
            return BackupSettings.model_validate_json(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return BackupSettings()

    def save_settings(self, settings: BackupSettings) -> BackupSettings:
        _atomic_json(self.settings_path, settings.model_dump(exclude_none=True))
        return settings

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _included_files(self) -> list[tuple[Path, str]]:
        result: list[tuple[Path, str]] = []
        for folder in ("archives", "raw", "versions", "attachments"):
            root = self.data_dir / folder
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file() and not path.name.endswith(".tmp"):
                    result.append((path, path.relative_to(self.data_dir).as_posix()))
        for name in ("provider_profiles.json", "settings.json"):
            path = self.data_dir / name
            if path.is_file():
                result.append((path, name))
        return result

    def create(self, destination: Path | None = None) -> Path:
        """Create a complete, checksummed backup using an atomic final rename."""
        target_dir = Path(destination) if destination else self.default_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = target_dir / f"feiyu-flowchat-backup-{stamp}.zip"
        files = self._included_files()
        manifest = {
            "format": "feiyu-flowchat-backup",
            "version": 1,
            "display_name": "肥鱼 FlowChat",
            "created_at": utc_now(),
            "files": [{"path": arcname, "size": path.stat().st_size, "sha256": self._hash(path)}
                      for path, arcname in files],
        }
        fd, temp_name = tempfile.mkstemp(prefix=".flowchat-backup-", suffix=".zip", dir=target_dir)
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for path, arcname in files:
                    archive.write(path, arcname)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    def apply_retention(self, directory: Path, keep: int) -> int:
        removed = 0
        for path in sorted(Path(directory).glob("feiyu-flowchat-backup-*.zip"), reverse=True)[keep:]:
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def automatic_backup(self) -> Path | None:
        settings = self.get_settings()
        if not settings.auto_enabled or not settings.destination:
            return None
        target = self.create(Path(settings.destination).expanduser())
        self.apply_retention(target.parent, settings.retention_count)
        settings.last_backup_at = utc_now()
        settings.last_backup_path = str(target)
        settings.last_error = None
        self.save_settings(settings)
        self.store.cleanup_trash(settings.trash_retention_days)
        return target

    @staticmethod
    def _safe_member(name: str) -> bool:
        path = PurePosixPath(name)
        return not path.is_absolute() and ".." not in path.parts and path.parts and path.parts[0] in {
            "archives", "raw", "versions", "attachments", "provider_profiles.json", "settings.json", "manifest.json"
        }

    @staticmethod
    def _write_bytes_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def restore(self, backup_path: Path, conflict: str = "skip") -> dict[str, Any]:
        """Validate an entire package, then merge it using ``conflict`` policy."""
        imported = skipped = replaced = copied = 0
        with zipfile.ZipFile(backup_path) as archive:
            if any(not self._safe_member(item.filename) for item in archive.infolist()):
                raise ImportError("备份包包含不安全路径")
            if sum(item.file_size for item in archive.infolist()) > 2 * 1024 * 1024 * 1024:
                raise ImportError("备份解压后超过 2 GB 限制")
            if archive.testzip() is not None:
                raise ImportError("备份 ZIP 的 CRC 校验失败")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, ValueError) as exc:
                raise ImportError("缺少有效的 manifest.json") from exc
            if manifest.get("format") != "feiyu-flowchat-backup" or manifest.get("version") != 1:
                raise ImportError("备份格式或版本不受支持")
            manifest_files = manifest.get("files")
            if not isinstance(manifest_files, list):
                raise ImportError("备份清单缺少文件列表")
            expected: dict[str, dict[str, Any]] = {}
            for item in manifest_files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ImportError("备份清单包含无效条目")
                name = item["path"]
                if name in expected or not self._safe_member(name) or name == "manifest.json":
                    raise ImportError("备份清单包含重复或不安全路径")
                expected[name] = item
            zip_files = [item.filename for item in archive.infolist() if not item.is_dir()]
            if len(zip_files) != len(set(zip_files)):
                raise ImportError("备份 ZIP 包含重复文件名")
            unexpected = set(zip_files) - set(expected) - {"manifest.json"}
            if unexpected:
                raise ImportError("备份 ZIP 包含清单之外的文件")
            for name, item in expected.items():
                try:
                    content = archive.read(name)
                except KeyError as exc:
                    raise ImportError(f"备份缺少文件：{name}") from exc
                if len(content) != item.get("size"):
                    raise ImportError(f"备份大小校验失败：{name}")
                if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                    raise ImportError(f"备份校验失败：{name}")

            id_map: dict[str, str] = {}
            for name in sorted(expected):
                parts = PurePosixPath(name).parts
                if len(parts) != 2 or parts[0] != "archives" or not name.endswith(".json"):
                    continue
                try:
                    restored = ConversationArchive.model_validate_json(archive.read(name))
                except ValueError:
                    skipped += 1
                    continue
                source_id = restored.id
                exists = (self.store.archive_dir / f"{restored.id}.json").exists()
                if exists and conflict == "skip":
                    skipped += 1
                    continue
                if exists and conflict == "copy":
                    restored.id = str(uuid4())
                    restored.title = f"{restored.title}（备份副本）"
                    restored.source["copied_from"] = source_id

                snapshot = restored.source.get("raw_snapshot")
                if isinstance(snapshot, dict):
                    raw_name = str(snapshot.get("path", ""))
                    if raw_name in expected and raw_name.startswith("raw/"):
                        raw_content = archive.read(raw_name)
                        raw_target = self.data_dir / "raw" / f"{restored.id}.json"
                        if raw_target.exists() and raw_target.read_bytes() != raw_content:
                            revision_dir = self.data_dir / "versions" / restored.id
                            revision_dir.mkdir(parents=True, exist_ok=True)
                            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                            shutil.copy2(raw_target, revision_dir / f"raw-before-restore-{stamp}.json")
                        self._write_bytes_atomic(raw_target, raw_content)
                        restored.source["raw_snapshot"] = {
                            "path": f"raw/{raw_target.name}",
                            "sha256": hashlib.sha256(raw_content).hexdigest(),
                            "saved_at": utc_now(),
                        }
                    else:
                        restored.source.pop("raw_snapshot", None)

                self.store.save(restored, create_version=exists)
                id_map[source_id] = restored.id
                if exists and conflict == "copy":
                    copied += 1
                else:
                    replaced += int(exists)
                    imported += int(not exists)

            # Restore version history and attachment trees for every archive
            # that was actually imported.  A copied archive gets rewritten IDs.
            for name in sorted(expected):
                parts = PurePosixPath(name).parts
                if len(parts) < 3 or parts[0] not in {"versions", "attachments"}:
                    continue
                destination_id = id_map.get(parts[1])
                if not destination_id:
                    continue
                target = self.data_dir / parts[0] / destination_id / Path(*parts[2:])
                content = archive.read(name)
                if parts[0] == "versions" and destination_id != parts[1] and name.endswith(".json"):
                    try:
                        version_archive = ConversationArchive.model_validate_json(content)
                        version_archive.id = destination_id
                        content = (json.dumps(version_archive.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    except ValueError:
                        pass
                if target.exists() and conflict != "replace":
                    continue
                self._write_bytes_atomic(target, content)

            # The recycle bin is part of a full backup.  It remains a recycle
            # bin after restore and never becomes an active conversation.
            for name in sorted(expected):
                parts = PurePosixPath(name).parts
                if len(parts) != 3 or parts[:2] != ("archives", ".trash") or not name.endswith(".json"):
                    continue
                target = self.store.trash_dir / parts[2]
                if target.exists() and conflict != "replace":
                    continue
                self._write_bytes_atomic(target, archive.read(name))

            # Profiles intentionally contain no API keys.  Restore these
            # small configuration files only when absent, or on explicit
            # replace, so a normal merge cannot silently change local setup.
            for name in ("provider_profiles.json", "settings.json"):
                if name not in expected:
                    continue
                target = self.data_dir / name
                if target.exists() and conflict != "replace":
                    continue
                try:
                    json.loads(archive.read(name))
                except ValueError as exc:
                    raise ImportError(f"备份中的 {name} 不是有效 JSON") from exc
                self._write_bytes_atomic(target, archive.read(name))
        return {"imported": imported, "replaced": replaced, "copied": copied, "skipped": skipped,
                "health": self.store.list_health_issues()}
