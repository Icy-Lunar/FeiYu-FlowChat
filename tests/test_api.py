from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

import app.main as main
from app.storage import ArchiveStore
from app.models import ConversationArchive


def test_streaming_chat_is_archived(monkeypatch, tmp_path) -> None:
    test_store = ArchiveStore(tmp_path / "archives")
    archive = test_store.create(title="新对话")

    async def fake_stream_chat(*_args, **_kwargs) -> AsyncIterator[dict]:
        yield {"type": "response_id", "id": "chatcmpl-test"}
        yield {"type": "reasoning_delta", "content": "先思考"}
        yield {"type": "delta", "content": "继续回答"}
        yield {"type": "usage", "usage": {"total_tokens": 12}}
        yield {"type": "finish", "finish_reason": "stop"}

    async def fake_generate_title(*_args, **_kwargs) -> str:
        return "模型生成的标题"

    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(main, "generate_title", fake_generate_title)
    main.conversation_locks.clear()

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/conversations/{archive.id}/chat",
            json={
                "content": "继续吧",
                "provider": {
                    "provider_id": "test",
                    "base_url": "https://example.test/v1",
                    "model": "test-model",
                    "api_key": "secret-not-to-be-saved",
                },
            },
            headers={"X-FlowChat-Token": main.SESSION_TOKEN},
        )

    assert response.status_code == 200
    assert "event: delta" in response.text
    assert "event: done" in response.text
    saved = test_store.get(archive.id)
    assert [(message.role, message.content) for message in saved.messages] == [
        ("user", "继续吧"),
        ("assistant", "继续回答"),
    ]
    assert saved.message_metadata[1].reasoning_content == "先思考"
    assert saved.message_metadata[1].details["usage"]["total_tokens"] == 12
    assert saved.title == "模型生成的标题"
    assert saved.title_info["method"] == "model"
    assert "secret-not-to-be-saved" not in (tmp_path / "archives" / f"{archive.id}.json").read_text(encoding="utf-8")


def test_deepseek_import_saves_raw_snapshot_and_falls_back_without_model(monkeypatch, tmp_path) -> None:
    test_store = ArchiveStore(tmp_path / "archives")

    async def fake_fetch(_url: str) -> ConversationArchive:
        archive = ConversationArchive(
            title="来源页面标题",
            source={"type": "deepseek_share", "share_id": "abcdefgh", "_raw_payload": {"code": 0, "data": {"raw": True}}},
        )
        archive.add_message("user", "第一句用户消息用于模型不可用时回退")
        archive.add_message("assistant", "历史回答")
        return archive

    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main, "fetch_deepseek_share", fake_fetch)

    with TestClient(main.app) as client:
        response = client.post(
            "/api/import/deepseek",
            json={"url": "https://chat.deepseek.com/share/abcdefgh"},
            headers={"X-FlowChat-Token": main.SESSION_TOKEN},
        )

    assert response.status_code == 201
    conversation = response.json()["conversation"]
    assert conversation["title"] == "第一句用户消息用于模型不可用时回退"
    assert conversation["title_info"]["method"] == "fallback"
    assert (tmp_path / conversation["source"]["raw_snapshot"]["path"]).is_file()
    assert test_store.list_health_issues()["ok"] is True
