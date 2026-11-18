import json

from app.storage import ArchiveStore


def test_archive_round_trip_and_openai_shape(tmp_path) -> None:
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="测试")
    archive.add_message("user", "问题")
    archive.add_message("assistant", "回答", reasoning_content="思考")
    store.save(archive)

    loaded = store.get(archive.id)
    assert loaded.title == "测试"
    assert [message.request_dict() for message in loaded.messages] == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    raw = json.loads((tmp_path / "archives" / f"{archive.id}.json").read_text(encoding="utf-8"))
    assert raw["format"] == "openai-chat-completions"
    assert raw["messages"][0] == {"role": "user", "content": "问题"}
    assert raw["message_metadata"][1]["reasoning_content"] == "思考"


def test_json_import_and_recoverable_delete(tmp_path) -> None:
    store = ArchiveStore(tmp_path / "archives")
    archive = store.import_openai(
        {"title": "旧对话", "messages": [{"role": "user", "content": "hello"}]},
        "fallback",
    )
    store.move_to_trash(archive.id)
    assert not (tmp_path / "archives" / f"{archive.id}.json").exists()
    assert list((tmp_path / "archives" / ".trash").glob(f"{archive.id}-*.json"))


def test_versions_restore_and_permanent_delete_purges_sidecars(tmp_path) -> None:
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="版本一")
    archive.add_message("user", "保留历史")
    archive.source = {"type": "deepseek_share", "raw_snapshot": store.save_raw(archive.id, {"raw": 1})}
    store.save(archive)
    archive.title = "版本二"
    store.save(archive)

    versions = store.list_versions(archive.id)
    assert versions
    restored = store.restore_version(archive.id, versions[-1]["name"])
    assert restored.id == archive.id

    store.move_to_trash(archive.id)
    trash = store.list_trash()[0]
    store.permanently_delete_trash(trash["trash_name"])
    assert not (tmp_path / "raw" / f"{archive.id}.json").exists()
    assert not (tmp_path / "versions" / archive.id).exists()


def test_restore_collision_writes_the_new_archive_id(tmp_path) -> None:
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="待恢复")
    store.move_to_trash(archive.id)
    replacement = archive.model_copy(deep=True)
    replacement.title = "占用原 ID"
    store.save(replacement, create_version=False)

    restored = store.restore_from_trash(store.list_trash()[0]["trash_name"])

    assert restored.id != archive.id
    assert store.get(restored.id).id == restored.id
    assert store.get(restored.id).title.endswith("（恢复）")
