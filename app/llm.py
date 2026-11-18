"""Small, provider-neutral OpenAI Chat Completions client.

The module intentionally depends on HTTP shapes instead of vendor SDKs so one
saved profile can target cloud providers, gateways, or a local Ollama server.
"""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .errors import ProviderError
from .chat_modes import apply_modes, default_mode_config, is_mode_rejection, normalize_modes
from .models import OpenAIMessage, ProviderConfig


PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "hint": "DeepSeek 官方 OpenAI 兼容接口",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "OpenAI Chat Completions",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "hint": "SiliconCloud OpenAI 兼容接口",
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint": "百炼按量付费 OpenAI 兼容接口",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "hint": "OpenRouter 多厂商路由",
    },
    {
        "id": "ollama",
        "name": "Ollama（本机）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3",
        "hint": "无需 API Key；需先启动 Ollama",
    },
    {
        "id": "custom",
        "name": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "hint": "填写任意兼容 /chat/completions 的服务",
    },
]


def chat_completions_url(base_url: str) -> str:
    """Accept either a version root or an explicit chat-completions URL."""
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _headers(config: ProviderConfig) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    headers.update(config.extra_headers)
    return headers


def _body(
    config: ProviderConfig,
    messages: list[OpenAIMessage] | list[dict[str, Any]],
    *,
    stream: bool,
) -> dict[str, Any]:
    serialized = [item.request_dict() if isinstance(item, OpenAIMessage) else item for item in messages]
    # Saved archives always keep every part. The selected endpoint's capability
    # only affects the outgoing copy, never the transcript on disk.
    serialized = deepcopy(serialized)
    # Provider extensions stay in archive sidecars and are replayed only to
    # their original endpoint. Reasoning is required by DeepSeek with tools.
    for item in serialized:
        if config.provider_id != "deepseek" or not config.extra_body.get("tools"):
            item.pop("server_tool_calls", None)
            if config.provider_id != "openrouter":
                item.pop("reasoning_content", None)
        if config.provider_id != "openrouter":
            item.pop("reasoning_details", None)
    for item in serialized:
        if not isinstance(item.get("content"), list):
            continue
        allowed = {"text"}
        if item.get("role") == "user" and config.attachment_mode in {"images", "files"}:
            allowed.add("image_url")
        if item.get("role") == "user" and config.attachment_mode == "files":
            allowed.add("file")
        parts = [part for part in item["content"] if part.get("type") in allowed]
        if config.attachment_mode == "text" or item.get("role") != "user":
            item["content"] = "\n\n".join(p["text"] for p in parts if p.get("type") == "text") or "[附件消息：没有可用的识别文字]"
        else:
            item["content"] = parts or [{"type": "text", "text": "[附件消息]"}]
            if config.provider_id == "deepseek":
                # DeepSeek's documented file part is flat; OpenAI's is nested.
                # Adapt only the request so exports keep the OpenAI shape.
                item["content"] = [{"type": "file", **part["file"]} if part.get("type") == "file" else part for part in item["content"]]
    body: dict[str, Any] = deepcopy(config.extra_body)
    body.update({"model": config.model, "messages": serialized, "stream": stream})
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.max_tokens is not None:
        body["max_tokens"] = config.max_tokens
    if config.provider_id == "openai" and config.model.startswith("gpt-5"):
        if "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        if body.get("reasoning_effort", "none") != "none":
            for key in ("temperature", "top_p", "logprobs", "top_logprobs"):
                body.pop(key, None)
    return body


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def parse_completion_chunk(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize streaming and non-streaming provider chunks into UI events."""
    events: list[dict[str, Any]] = []
    if chunk.get("error"):
        error = chunk["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ProviderError(f"模型服务返回错误：{message}",
                            upstream_code=str(error.get("code", "")) if isinstance(error, dict) else None,
                            upstream_param=str(error.get("param", "")) if isinstance(error, dict) else None)

    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if not delta and isinstance(choice.get("message"), dict):
            delta = choice["message"]
        content = _text(delta.get("content"))
        reasoning = _text(delta.get("reasoning_content") or delta.get("reasoning"))
        if content:
            events.append({"type": "delta", "content": content})
        if reasoning:
            events.append({"type": "reasoning_delta", "content": reasoning})
        extensions = {key: delta[key] for key in ("server_tool_calls", "annotations", "reasoning_details") if isinstance(delta.get(key), list)}
        if extensions:
            events.append({"type": "provider_metadata", "metadata": extensions})
        if isinstance(choice.get("server_tool_event"), dict):
            events.append({"type": "tool_activity", "event": choice["server_tool_event"]})
        if choice.get("finish_reason"):
            events.append({"type": "finish", "finish_reason": choice["finish_reason"]})

    if isinstance(chunk.get("usage"), dict):
        events.append({"type": "usage", "usage": chunk["usage"]})
    if isinstance(chunk.get("id"), str):
        events.append({"type": "response_id", "id": chunk["id"]})
    return events


async def _read_error(response: httpx.Response) -> str:
    raw = (await response.aread())[:16_000]
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            detail = data.get("error") or data.get("message") or data.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code")
            if detail:
                return str(detail)[:1000]
    except (ValueError, UnicodeDecodeError):
        pass
    return raw.decode("utf-8", errors="replace")[:1000] or "无错误详情"


async def stream_chat(
    config: ProviderConfig,
    messages: list[OpenAIMessage] | list[dict[str, Any]],
    *, modes: dict[str, bool | None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply UI modes and retry once, only for a pre-output mode rejection.

    Keeping retry inside the transport avoids appending the user message twice.
    None values in an explicit modes object select parameter-free defaults;
    omitting the object preserves legacy callers' advanced request settings.
    """
    requested = modes or {}
    effective, unavailable = normalize_modes(config, requested)
    base = default_mode_config(config) if modes is not None else config
    selected = apply_modes(base, effective)
    if modes is not None:
        yield {"type": "mode_status", "modes": effective, "fallback": bool(unavailable),
               "message": f"{'、'.join(unavailable)}未适配此模型，已使用默认模式。" if unavailable else ""}
    started = False
    try:
        async for event in _stream_chat_once(selected, messages):
            if event["type"] in {"delta", "reasoning_delta", "tool_activity", "provider_metadata", "usage", "finish"}:
                started = True
            yield event
    except ProviderError as exc:
        if started or not any(value is not None for value in effective.values()) or not is_mode_rejection(exc, effective):
            raise
        # The retry is outside the try block: a second failure is final.
        yield {"type": "mode_status", "modes": {"thinking": None, "web_search": None}, "fallback": True,
               "message": "服务商不支持本次思考或联网设置，已回到默认模式并重试一次。默认模式是否思考由模型决定。"}
        async for event in _stream_chat_once(default_mode_config(config), messages):
            yield event


async def _stream_chat_once(
    config: ProviderConfig,
    messages: list[OpenAIMessage] | list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield normalized events from an OpenAI-compatible streaming response."""
    timeout = httpx.Timeout(config.timeout_seconds, connect=min(30.0, config.timeout_seconds))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                chat_completions_url(config.base_url),
                headers=_headers(config),
                json=_body(config, messages, stream=True),
            ) as response:
                if response.status_code >= 400:
                    detail = await _read_error(response)
                    # Keep structured error fields for safe fallback decisions;
                    # the UI receives only a short message, never credentials.
                    try:
                        payload = response.json()
                        error = payload.get("error", {}) if isinstance(payload, dict) else {}
                        error = error if isinstance(error, dict) else {}
                    except ValueError:
                        error = {}
                    raise ProviderError(f"模型服务返回 HTTP {response.status_code}：{detail}",
                                        upstream_status=response.status_code,
                                        upstream_code=str(error.get("code", "")),
                                        upstream_param=str(error.get("param", "")))

                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    raw = await response.aread()
                    try:
                        payload = json.loads(raw)
                    except ValueError as exc:
                        raise ProviderError("模型服务返回了无法解析的 JSON") from exc
                    for event in parse_completion_chunk(payload):
                        yield event
                    return

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(chunk, dict):
                        for event in parse_completion_chunk(chunk):
                            yield event
    except httpx.TimeoutException as exc:
        raise ProviderError("模型请求超时，请检查接口地址或增大超时时间", status_code=504) from exc
    except httpx.RequestError as exc:
        raise ProviderError(f"无法连接模型服务：{exc.__class__.__name__}") from exc


async def test_connection(
    config: ProviderConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Send one tiny, non-streaming Chat Completions request.

    This deliberately performs a real billable provider call. It never writes
    the prompt, response, or credential to a conversation archive.
    """
    test_config = config.model_copy(deep=True)
    test_config.max_tokens = min(config.max_tokens or 16, 16)
    body = _body(
        test_config,
        [{"role": "user", "content": "Reply only with OK."}],
        stream=False,
    )
    timeout = httpx.Timeout(config.timeout_seconds, connect=min(30.0, config.timeout_seconds))
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        response = await http.post(
            chat_completions_url(config.base_url),
            headers=_headers(config),
            json=body,
            timeout=timeout,
        )
        if response.status_code >= 400:
            detail = await _read_error(response)
            raise ProviderError(f"连接失败（HTTP {response.status_code}）：{detail}")
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list):
            raise ProviderError("接口可访问，但响应不是 OpenAI Chat Completions 格式")
        sample = ""
        if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
            sample = _text(choices[0]["message"].get("content"))
        return {
            "ok": True,
            "message": "连接成功，服务商已接受标准 Chat Completions 请求",
            "sample": sample,
            "model": payload.get("model", config.model),
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        }
    except httpx.TimeoutException as exc:
        raise ProviderError("连接测试超时", status_code=504) from exc
    except httpx.RequestError as exc:
        raise ProviderError(f"无法连接模型服务：{exc.__class__.__name__}") from exc
    except ValueError as exc:
        raise ProviderError("接口返回了无法解析的 JSON") from exc
    finally:
        if owns_client:
            await http.aclose()


async def generate_title(config: ProviderConfig, messages: list[OpenAIMessage]) -> str:
    """Generate a concise archive title with the same requested model."""
    transcript = "\n".join(f"{item.role}: {item.text_content()[:1200]}" for item in messages[:12])[:8000]
    # Naming a transcript never needs the user's web tools. Do not inherit
    # optional chat mode failures or trigger a separate search for a title.
    title_modes, _ = normalize_modes(config, {"thinking": False})
    title_config = apply_modes(default_mode_config(config), title_modes)
    title_config.temperature = 0.2
    title_config.max_tokens = min(config.max_tokens or 32, 32)
    body = _body(
        title_config,
        [
            {"role": "system", "content": "为对话生成一个准确、具体的中文标题。不得直接复制首句，不超过20个汉字，只输出标题。"},
            {"role": "user", "content": transcript},
        ],
        stream=False,
    )
    timeout = httpx.Timeout(config.timeout_seconds, connect=min(30.0, config.timeout_seconds))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(chat_completions_url(config.base_url), headers=_headers(config), json=body)
        if response.status_code >= 400:
            raise ProviderError(f"标题模型返回 HTTP {response.status_code}")
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("标题模型响应格式不兼容")
        message = choices[0].get("message")
        title = _text(message.get("content")) if isinstance(message, dict) else ""
        title = title.strip().strip("#*`\"'“”《》 ").replace("\n", " ")
        title = " ".join(title.split())[:40]
        if not title:
            raise ProviderError("标题模型返回空标题")
        return title
    except (httpx.RequestError, httpx.TimeoutException, ValueError) as exc:
        raise ProviderError("无法使用请求模型生成标题") from exc


async def list_provider_models(config: ProviderConfig) -> list[str]:
    """Return model IDs from the optional OpenAI-compatible ``/models`` API."""
    base = config.base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = f"{base}/models"
    timeout = httpx.Timeout(min(config.timeout_seconds, 30), connect=min(config.timeout_seconds, 15))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers=_headers(config))
        if response.status_code >= 400:
            detail = await _read_error(response)
            raise ProviderError(f"模型列表返回 HTTP {response.status_code}：{detail}")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ProviderError("该服务商未返回 OpenAI 兼容的模型列表")
        return sorted({str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")})
    except httpx.RequestError as exc:
        raise ProviderError("无法读取模型列表") from exc
