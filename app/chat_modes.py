"""Documented Chat Completions mode adapters (see docs/API_MODES.md).

Capability checks are deliberately conservative and never probe a paid API.
The UI and request builder share this registry; unknown models keep their
provider defaults. A mode is tri-state: True / False override the provider,
None leaves it alone. No adapter changes the selected model or endpoint.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ProviderError
from .models import ProviderConfig


MODE_NAMES = {"thinking": "思考", "web_search": "联网搜索"}
WEB_TOOLS = {"deepseek_web_search", "deepseek_read_url", "openrouter:web_search", "openrouter:web_fetch"}


def _hybrid_qwen(model: str) -> bool:
    # Dated Instruct/Thinking variants are separate, fixed-mode models.
    return bool(re.fullmatch(r"(?:qwen/)?qwen3-(?:0\.6|1\.7|4|8|14|32)b|(?:qwen/)?qwen3-(?:30b-a3b|235b-a22b)", model))


def mode_capabilities(config: ProviderConfig) -> dict[str, dict[str, Any]]:
    """Return display-safe support information, without credentials or I/O."""
    provider, model = config.provider_id, config.model.lower()
    thinking = search = False
    thinking_reason = "该模型尚无已适配的思考开关，使用服务商默认模式"
    search_reason = "该接口或模型尚无已适配的联网搜索，使用默认模式"
    if provider == "deepseek":
        thinking = model in {"deepseek-flash", "deepseek-v4-pro"}
        search = model == "deepseek-flash"
        search_reason = "DeepSeek 官方联网工具目前适配 deepseek-flash"
    elif provider == "dashscope":
        thinking = model in {"qwen-plus", "qwen-turbo", "qwen-flash"} or _hybrid_qwen(model)
        search = model in {"qwen-plus", "qwen-turbo", "qwen-flash", "qwen-max"}
    elif provider == "siliconflow":
        model = model.removeprefix("pro/")
        thinking = _hybrid_qwen(model) or model == "deepseek-ai/deepseek-v3.2"
    elif provider == "openai":
        thinking = bool(re.fullmatch(r"gpt-5\.[12](?:-\d{4}-\d{2}-\d{2})?", model))
        search_reason = "当前使用 Chat Completions；OpenAI 搜索专用模型不支持在同一模型内开关联网"
    elif provider == "openrouter":
        # Server tools require tool calling. Keep this allowlist explicit;
        # new/fixed-reasoning variants need their own documentation review.
        thinking = model in {"deepseek/deepseek-v3.2", "openai/gpt-5.1", "openai/gpt-5.2"} or _hybrid_qwen(model)
        search = thinking or model in {"deepseek/deepseek-chat", "openai/gpt-4o", "openai/gpt-4o-mini", "openai/gpt-4.1", "openai/gpt-4.1-mini"}
    elif provider == "ollama":
        thinking = model.split(":", 1)[0] == "qwen3"
        search_reason = "Ollama 的兼容聊天接口没有内置联网搜索"
    return {
        "thinking": {"supported": thinking, "reason": "启用模型思考，可能增加耗时及 token 费用" if thinking else thinking_reason},
        "web_search": {"supported": search, "reason": "允许模型联网检索，可能产生额外搜索费用；是否检索由模型决定" if search else search_reason},
    }


def normalize_modes(config: ProviderConfig, modes: dict[str, bool | None]) -> tuple[dict[str, bool | None], list[str]]:
    capabilities = mode_capabilities(config)
    effective = {name: modes.get(name) for name in MODE_NAMES}
    unavailable = []
    for name, value in effective.items():
        if value is not None and not capabilities[name]["supported"]:
            effective[name] = None
            if value:
                unavailable.append(MODE_NAMES[name])
    return effective, unavailable


def _strip_search(body: dict[str, Any]) -> None:
    """Remove only owned web tools; leave custom function tools untouched."""
    if isinstance(body.get("tools"), list):
        body["tools"] = [tool for tool in body["tools"] if not isinstance(tool, dict) or tool.get("type") not in WEB_TOOLS]
        if not body["tools"]:
            body.pop("tools")
            body.pop("tool_choice", None)
    if isinstance(body.get("plugins"), list):
        body["plugins"] = [p for p in body["plugins"] if not isinstance(p, dict) or p.get("id") != "web"]
        if not body["plugins"]:
            body.pop("plugins")
    for key in ("enable_search", "search_options", "web_search_options"):
        body.pop(key, None)


def default_mode_config(config: ProviderConfig) -> ProviderConfig:
    """Parameter-free fallback, keeping credentials, model and other settings.

    Used only after a mode incompatibility, and for auxiliary title requests.
    Removing a control means provider default, not a guarantee of no reasoning
    (some models always think). Saved profile JSON is never modified.
    """
    result = config.model_copy(deep=True)
    for key in ("thinking", "enable_thinking", "thinking_budget", "reasoning", "reasoning_effort"):
        result.extra_body.pop(key, None)
    _strip_search(result.extra_body)
    return result


def apply_modes(config: ProviderConfig, modes: dict[str, bool | None]) -> ProviderConfig:
    """Apply normalized per-turn overrides to a private configuration copy."""
    result = config.model_copy(deep=True)
    body, provider = result.extra_body, result.provider_id
    thinking, search = modes.get("thinking"), modes.get("web_search")
    if thinking is not None:
        for key in ("thinking", "enable_thinking", "thinking_budget", "reasoning", "reasoning_effort"):
            body.pop(key, None)
        if provider == "deepseek":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif provider in {"dashscope", "siliconflow"}:
            body["enable_thinking"] = thinking
        elif provider == "openrouter":
            body["reasoning"] = {"enabled": thinking}
        elif provider in {"openai", "ollama"}:
            body["reasoning_effort"] = "medium" if thinking else "none"
    if search is not None:
        _strip_search(body)
        if provider == "dashscope":
            body["enable_search"] = search
        elif provider in {"deepseek", "openrouter"}:
            if search:
                tool_type = "deepseek_web_search" if provider == "deepseek" else "openrouter:web_search"
                body.setdefault("tools", []).append({"type": tool_type})
                # A saved `none` or forced function would defeat the UI switch.
                body["tool_choice"] = "auto"
            if provider == "openrouter":
                # Disable the deprecated account-level web plugin as well.
                body.setdefault("plugins", []).append({"id": "web", "enabled": False})
    return result


def is_mode_rejection(error: ProviderError, modes: dict[str, bool | None] | None = None) -> bool:
    """Only explicit capability/parameter failures permit a single retry.

    Never retry credentials, credit, throttling, network failures, or generic
    bad requests. The caller additionally forbids replay after any output or
    tool activity, since the original request may already have been billed.
    """
    if error.upstream_status not in {None, 400, 404, 422, 501}:
        return False
    detail = f"{error.message} {error.upstream_code or ''} {error.upstream_param or ''}".lower()
    mode_field = any(word in detail for word in (
        "thinking", "reasoning", "enable_search", "web_search", "web search", "search_options", "联网", "思考",
    ))
    if (modes or {}).get("web_search"):
        mode_field = mode_field or any(word in detail for word in ("tools", "tool use", "tool_choice", "response_format"))
    rejection = any(word in detail for word in (
        "unsupported", "not supported", "does not support", "not support", "unrecognized", "unknown parameter",
        "invalid_parameter", "invalid parameter", "not available", "unavailable", "not enabled", "not allowed",
        "no endpoints found that support", "不支持", "不可用", "未开通", "无效参数",
    ))
    if error.upstream_status is None and error.upstream_code not in {"unsupported_parameter", "unsupported_value", "invalid_parameter", "unsupported_feature"}:
        return False
    return mode_field and rejection
