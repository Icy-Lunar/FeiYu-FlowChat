"""All provider behavior is simulated locally; these tests incur no API fees."""

import json

import httpx
import pytest

import app.llm as llm
import app.main as main
from app.chat_modes import apply_modes, default_mode_config, mode_capabilities, normalize_modes
from app.errors import ProviderError
from app.models import OpenAIMessage, ProviderConfig
from app.storage import ArchiveStore
from fastapi.testclient import TestClient


def config(provider="deepseek", model="deepseek-flash", **kwargs):
    return ProviderConfig(provider_id=provider, base_url="https://local-test.invalid/v1", model=model, **kwargs)


@pytest.mark.parametrize("provider,model,expected", [
    ("deepseek", "deepseek-flash", {"thinking": {"type": "enabled"}, "tools": [{"type": "deepseek_web_search"}]}),
    ("dashscope", "qwen-plus", {"enable_thinking": True, "enable_search": True}),
    ("siliconflow", "Qwen/Qwen3-8B", {"enable_thinking": True}),
    ("openai", "gpt-5.1", {"reasoning_effort": "medium"}),
    ("openrouter", "openai/gpt-5.2", {"reasoning": {"enabled": True}, "tools": [{"type": "openrouter:web_search"}]}),
    ("ollama", "qwen3:8b", {"reasoning_effort": "medium"}),
])
def test_provider_mappings_and_immutable_profile(provider, model, expected):
    original = config(provider, model, extra_body={"seed": 42})
    modes, _ = normalize_modes(original, {"thinking": True, "web_search": True})
    selected = apply_modes(original, modes)
    body = llm._body(selected, [OpenAIMessage(role="user", content="hello")], stream=True)
    for key, value in expected.items():
        assert body[key] == value
    assert body["seed"] == 42
    assert original.extra_body == {"seed": 42}
    if provider == "openai":
        assert "temperature" not in body


def test_off_clears_conflicts_without_erasing_custom_tools():
    tool = {"type": "function", "function": {"name": "example"}}
    original = config(extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high",
                                  "tools": [tool, {"type": "deepseek_web_search"}], "seed": 7})
    selected = apply_modes(original, {"thinking": False, "web_search": False})
    assert selected.extra_body == {"thinking": {"type": "disabled"}, "tools": [tool], "seed": 7}
    selected.extra_body["tools"][0]["function"]["name"] = "changed"
    assert original.extra_body["tools"][0]["function"]["name"] == "example"
    fallback = default_mode_config(original)
    assert fallback.extra_body == {"tools": [tool], "seed": 7}


@pytest.mark.parametrize("provider,model", [("custom", "anything"), ("openai", "gpt-4o-mini"), ("ollama", "gpt-oss:20b"), ("siliconflow", "Qwen/Qwen3-8B-Instruct-2507")])
def test_unknown_or_fixed_models_keep_defaults(provider, model):
    cfg = config(provider, model)
    assert not mode_capabilities(cfg)["thinking"]["supported"]
    effective, unavailable = normalize_modes(cfg, {"thinking": True, "web_search": True})
    assert effective == {"thinking": None, "web_search": None}
    assert unavailable == ["思考", "联网搜索"]


def mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_rejection_retries_once_without_modes(monkeypatch):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": "enable_search is not supported", "param": "enable_search"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "默认回复"}}]})
    mock_http(monkeypatch, handler)
    cfg = config("dashscope", "qwen-plus", extra_body={"seed": 3, "enable_thinking": True, "enable_search": True})
    messages = [OpenAIMessage(role="user", content="问题")]
    events = [e async for e in llm.stream_chat(cfg, messages, modes={"thinking": True, "web_search": True})]
    assert len(bodies) == 2
    assert bodies[0]["enable_search"] is True
    assert "enable_search" not in bodies[1] and "enable_thinking" not in bodies[1]
    assert bodies[1]["seed"] == 3 and bodies[1]["messages"] == bodies[0]["messages"]
    assert any(e.get("fallback") and e["modes"] == {"thinking": None, "web_search": None} for e in events)
    assert events[-1] == {"type": "delta", "content": "默认回复"}
    assert cfg.extra_body["enable_search"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status,message", [(401, "thinking not supported"), (402, "web_search insufficient credits"), (429, "web_search rate limit"), (500, "thinking unavailable"), (400, "messages exceeds context length"), (404, "model not found")])
async def test_unrelated_errors_are_not_retried(monkeypatch, status, message):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": message}})
    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError):
        _ = [e async for e in llm.stream_chat(config(), [], modes={"thinking": True, "web_search": True})]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_second_failure_stops_retry_loop(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(422, json={"error": {"message": "thinking is not supported"}})
    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError):
        _ = [e async for e in llm.stream_chat(config(), [], modes={"thinking": True})]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_chunk", [
    {"choices": [{"delta": {"content": "部分回答"}}]},
    {"choices": [{"delta": {"reasoning_content": "思考开始"}}]},
    {"choices": [{"server_tool_event": {"type": "call_started", "step": 0}}]},
    {"usage": {"total_tokens": 1}},
])
async def test_never_replay_after_output_or_tool_execution(monkeypatch, first_chunk):
    calls = []
    def handler(request):
        calls.append(request)
        error = {"error": {"code": "unsupported_parameter", "message": "thinking not supported"}}
        sse = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in [first_chunk, error])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError):
        _ = [e async for e in llm.stream_chat(config(), [], modes={"thinking": True})]
    assert len(calls) == 1


def test_api_fallback_saves_one_turn_and_mode_notice(monkeypatch, tmp_path):
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="测试回退")
    archive.title_info = {"method": "manual"}
    store.save(archive)
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": "deepseek_web_search is not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "已恢复"}}]})
    mock_http(monkeypatch, handler)
    monkeypatch.setattr(main, "store", store)
    main.conversation_locks.clear()
    with TestClient(main.app) as client:
        response = client.post(f"/api/conversations/{archive.id}/chat", headers={"X-FlowChat-Token": main.SESSION_TOKEN},
                               json={"content": "继续", "provider": config().model_dump(), "thinking": True, "web_search": True})
    assert "event: mode_status" in response.text and "event: done" in response.text
    saved = store.get(archive.id)
    assert len(saved.messages) == 2 and len(bodies) == 2
    assert saved.model["modes"] == {"thinking": None, "web_search": None}
    assert "默认模式" in saved.message_metadata[1].details["mode_notice"]


def test_web_context_and_citations_survive_archive_and_next_turn(monkeypatch, tmp_path):
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="资料查阅")
    archive.title_info = {"method": "manual"}
    store.save(archive)
    tool_calls = [{"index": 0, "calls": [{"type": "deepseek_web_search", "status": "completed", "signature": "opaque", "sources": [{"url": "https://example.com", "title": "参考"}]}]}]
    annotations = [{"type": "url_citation", "url_citation": {"url": "https://example.com", "title": "参考"}}]
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "带引用的回答", "reasoning_content": "最终思考", "server_tool_calls": tool_calls, "annotations": annotations}}]})
    mock_http(monkeypatch, handler)
    monkeypatch.setattr(main, "store", store)
    main.conversation_locks.clear()
    with TestClient(main.app) as client:
        payload = {"content": "查资料", "provider": config().model_dump(), "web_search": True, "thinking": True}
        for _ in range(2):
            response = client.post(f"/api/conversations/{archive.id}/chat", headers={"X-FlowChat-Token": main.SESSION_TOKEN}, json=payload)
            assert "event: done" in response.text
    saved = store.get(archive.id)
    extensions = saved.message_metadata[1].details["provider_extensions"]
    assert extensions["server_tool_calls"] == tool_calls and extensions["annotations"] == annotations
    assert bodies[1]["messages"][1]["server_tool_calls"] == tool_calls
    assert bodies[1]["messages"][1]["reasoning_content"] == "最终思考"
    other = llm._body(config("openai", "gpt-4o-mini"), bodies[1]["messages"], stream=True)
    assert "server_tool_calls" not in other["messages"][1]
    assert "reasoning_content" not in other["messages"][1]


@pytest.mark.asyncio
async def test_unknown_modes_fall_back_before_single_request(monkeypatch):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "普通回复"}}]})
    mock_http(monkeypatch, handler)
    events = [e async for e in llm.stream_chat(config("custom", "unknown"), [], modes={"thinking": True, "web_search": True})]
    assert len(bodies) == 1
    assert events[0]["fallback"] is True
    assert events[0]["modes"] == {"thinking": None, "web_search": None}
    assert "thinking" not in bodies[0] and "tools" not in bodies[0]


@pytest.mark.asyncio
async def test_title_does_not_inherit_web_or_reasoning_settings(monkeypatch):
    from app.models import ConversationArchive
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "模型生成标题"}}]})
    mock_http(monkeypatch, handler)
    archive = ConversationArchive()
    archive.add_message("user", "请查资料")
    cfg = config(extra_body={"thinking": {"type": "enabled"}, "tools": [{"type": "deepseek_web_search"}]})
    assert await llm.generate_title(cfg, archive.messages) == "模型生成标题"
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert "tools" not in bodies[0]


def test_partial_error_is_saved_without_retry(monkeypatch, tmp_path):
    store = ArchiveStore(tmp_path / "archives")
    archive = store.create(title="部分回复")
    calls = []
    def handler(request):
        calls.append(request)
        chunks = [{"choices": [{"delta": {"content": "已经收到的文字"}}]},
                  {"error": {"code": "unsupported_parameter", "message": "thinking not supported"}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks))
    mock_http(monkeypatch, handler)
    monkeypatch.setattr(main, "store", store)
    main.conversation_locks.clear()
    with TestClient(main.app) as client:
        response = client.post(f"/api/conversations/{archive.id}/chat", headers={"X-FlowChat-Token": main.SESSION_TOKEN},
                               json={"content": "请继续", "provider": config().model_dump(), "thinking": True})
    assert "event: error" in response.text and len(calls) == 1
    saved = store.get(archive.id)
    assert saved.messages[-1].content == "已经收到的文字"
    assert saved.message_metadata[-1].details["status"] == "failed"
