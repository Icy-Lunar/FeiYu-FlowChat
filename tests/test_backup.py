import json
import zipfile

import pytest

from app.backup import BackupManager
from app.errors import ImportError as FlowImportError
from app.storage import ArchiveStore


def _prepared_backup(tmp_path):
    data_dir = tmp_path / "source"
    store = ArchiveStore(data_dir / "archives")
    archive = store.create(title="模型生成的项目标题", source={"type": "deepseek_share", "share_id": "abcdefgh"})
    archive.add_message("user", "这不是用来直接当标题的第一句话")
    archive.add_message("assistant", "已整理完成")
    snapshot = store.save_raw(archive.id, {"code": 0, "data": {"complete": True}})
    archive.source["raw_snapshot"] = snapshot
    store.save(archive)
    archive.tags = ["重要"]
    store.save(archive)
    (data_dir / "provider_profiles.json").write_text(
        json.dumps({"version": 1, "profiles": [{"id": "profile-1", "name": "测试", "provider": {"model": "m"}}]}),
        encoding="utf-8",
    )
    manager = BackupManager(data_dir, data_dir / "settings.json", store)
    return manager.create(data_dir / "exports"), archive


def test_full_backup_restore_preserves_raw_versions_and_profiles(tmp_path) -> None:
    backup, original = _prepared_backup(tmp_path)
    with zipfile.ZipFile(backup) as package:
        manifest = json.loads(package.read("manifest.json"))
    assert manifest["display_name"] == "肥鱼 FlowChat"
    assert all(item["sha256"] for item in manifest["files"])

    target_dir = tmp_path / "target"
    target_store = ArchiveStore(target_dir / "archives")
    target = BackupManager(target_dir, target_dir / "settings.json", target_store)
    result = target.restore(backup)

    assert result["imported"] == 1
    restored = target_store.get(original.id)
    assert restored.title == "模型生成的项目标题"
    assert (target_dir / restored.source["raw_snapshot"]["path"]).is_file()
    assert target_store.list_versions(original.id)
    assert (target_dir / "provider_profiles.json").is_file()
    assert target_store.list_health_issues()["ok"] is True


def test_restore_copy_gets_new_id_and_own_raw_snapshot(tmp_path) -> None:
    backup, original = _prepared_backup(tmp_path)
    source_dir = tmp_path / "source"
    store = ArchiveStore(source_dir / "archives")
    manager = BackupManager(source_dir, source_dir / "settings.json", store)

    result = manager.restore(backup, conflict="copy")
    copies = [item for item in store.list() if item.id != original.id]

    assert result["copied"] == 1
    assert len(copies) == 1
    copied = store.get(copies[0].id)
    assert copied.source["copied_from"] == original.id
    assert copied.source["raw_snapshot"]["path"] == f"raw/{copied.id}.json"
    assert store.list_health_issues()["ok"] is True


def test_restore_rejects_tampered_manifest_content(tmp_path) -> None:
    backup, _ = _prepared_backup(tmp_path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename.startswith("archives/") and "/.trash/" not in item.filename:
                content += b"tampered"
            target.writestr(item, content)

    data_dir = tmp_path / "empty"
    manager = BackupManager(data_dir, data_dir / "settings.json", ArchiveStore(data_dir / "archives"))
    with pytest.raises(FlowImportError, match="校验失败"):
        manager.restore(tampered)
