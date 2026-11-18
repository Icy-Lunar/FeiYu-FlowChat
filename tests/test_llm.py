import json

import httpx
import pytest

from app.llm import chat_completions_url, parse_completion_chunk, test_connection as run_connection_test
from app.models import ProviderConfig


def test_chat_completion_url() -> None:
    assert chat_completions_url("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
    assert chat_completions_url("https://api.example.com/v1/") == "https://api.example.com/v1/chat/completions"
    assert chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"


def test_parse_stream_chunk_with_reasoning_and_usage() -> None:
    events = parse_completion_chunk(
        {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "delta": {"content": "答案", "reasoning_content": "推理"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 10},
        }
    )
    assert {event["type"] for event in events} == {
        "delta",
        "reasoning_delta",
        "finish",
        "usage",
        "response_id",
    }


@pytest.mark.asyncio
async def test_connection_sends_small_billable_standard_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "provider-model",
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            },
        )

    config = ProviderConfig(
        provider_id="test",
        base_url="https://provider.example/v1",
        api_key="secret-key",
        model="provider-model",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_connection_test(config, client=client)

    assert captured["authorization"] == "Bearer secret-key"
    assert captured["body"] == {
        "model": "provider-model",
        "messages": [{"role": "user", "content": "Reply only with OK."}],
        "stream": False,
        "temperature": 0.7,
        "max_tokens": 16,
    }
    assert result["ok"] is True
    assert result["sample"] == "OK"
    assert result["usage"]["total_tokens"] == 9
