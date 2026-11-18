# 思考、联网与自动回退

文档核对日期：2026-11-18。应用使用 **Chat Completions**，不是各厂商的网页端或 Responses API。不同模型、地域、账户和网关可能提供不同能力，按钮的可用状态依据下面的保守适配表判断，不会为检测能力主动发起计费请求。

## 前端使用

打开或新建一段对话，保存模型配置后，输入框底部有“思考”和“联网”按钮。蓝色“开”表示启用，灰色“关”表示显式关闭；“未适配”可以点击查看原因。联网开关表示允许模型检索，不保证每条消息都执行搜索。

设置按配置档案保存在当前浏览器中；切换档案会恢复各自的状态。修改档案后重新初始化，避免沿用旧模型的参数。生成过程中锁定两个开关。思考可能增加 token 和延迟，联网可能额外收费，实际价格由提供商决定。

“默认”表示不附加本应用的模式参数，由模型和服务商自身决定行为；它不等于关闭模型固有的思考能力。再次点击可以重新启用。前端按钮优先于高级请求体中同类的思考、搜索字段，其他请求参数及自定义函数工具会保留。

## 当前适配范围

| 提供商 | 思考开关 | 联网开关 |
| --- | --- | --- |
| DeepSeek | `deepseek-flash`、`deepseek-v4-pro`：`thinking.type = enabled/disabled` | `deepseek-flash`：`tools: [{type: "deepseek_web_search"}]`；关闭时移除联网工具 |
| 阿里云百炼 | `qwen-plus`、`qwen-turbo`、`qwen-flash` 和下述 Qwen3 混合模型：`enable_thinking` | `qwen-plus`、`qwen-turbo`、`qwen-flash`、`qwen-max`：`enable_search` |
| SiliconFlow | Qwen3 混合模型和 `deepseek-ai/DeepSeek-V3.2`（含 `Pro/`）：`enable_thinking` | 暂未适配 |
| OpenAI | `gpt-5.1`、`gpt-5.2` 及日期快照：`reasoning_effort = medium/none` | 当前兼容聊天接口不提供通用开关；不自动切换到搜索专用模型 |
| OpenRouter | `deepseek/deepseek-v3.2`、`openai/gpt-5.1`、`openai/gpt-5.2`、Qwen3 混合模型：`reasoning.enabled` | 上述模型以及 `deepseek/deepseek-chat`、`openai/gpt-4o`、`openai/gpt-4o-mini`、`openai/gpt-4.1`、`openai/gpt-4.1-mini`：`openrouter:web_search` 服务端工具 |
| Ollama | `qwen3`（含 `:8b` 等标签）：`reasoning_effort = medium/none` | 当前兼容聊天接口没有内置搜索 |
| 自定义、其他模型 | 保持服务商默认模式 | 保持服务商默认模式 |

Qwen3 混合模型指 `qwen3-0.6b/1.7b/4b/8b/14b/32b/30b-a3b/235b-a22b`；SiliconFlow、OpenRouter 使用相应的 `Qwen/`、`qwen/` 前缀。固定 Instruct、Thinking 变体不包含在内。OpenAI 的 `o` 系列、GPT-OSS 等固有思考模型不能简单套用同一开关。未列出不代表厂商不支持，只表示应用尚未核验该组合。

OpenAI 开启推理时移除不兼容的采样参数，输出上限改用 `max_completion_tokens`。OpenRouter 使用新的服务端工具，并关闭旧的账户级 web 插件，避免重复检索。代码不会偷偷更换用户选择的模型、Base URL 或 Key。

## 回退规则

1. 已知未适配：发送前回到默认模式，仅发送一次请求，并提示哪些功能未适配。
2. 上游明确拒绝思考、搜索或相应工具参数：**尚未收到回答、思考、工具执行或用量事件**时，移除两个模式相关参数，使用同一模型、同一上下文重试 **一次**。
3. 回退后两个按钮显示“默认”，浏览器记住该状态；下一轮不再自动发送失败参数。回复旁和本地 JSON 中保存回退说明。
4. 第二次失败直接报告错误。鉴权、余额、限流、网络超时、普通服务故障、上下文过长不会触发此回退。
5. 已输出文字、思考或开始执行搜索后，不重新发送整轮请求，避免重复生成和重复执行工具；已收到的文字保留为部分回复。服务器可能对已处理的请求计费，回退不承诺免除原请求费用。

这不是无条件的网络重试。提供商若静默忽略参数、返回成功却没有检索，客户端无法可靠推断它“不支持”。应用也不会用普通回答冒充联网检索结果。

## 开发约定

`app/chat_modes.py` 是能力判断和参数映射的单一来源。`GET /api/provider-profiles` 在每个档案上返回不含密钥的 `capabilities.thinking/web_search`，包括 `supported` 和 `reason`。新增模型时先核验官方 **Chat Completions** 文档，再更新适配表、测试和本文。

`POST /api/conversations/{id}/chat` 接受 `thinking`、`web_search`：

```json
{
  "content": "请查阅相关资料",
  "profile_id": "配置档案 ID",
  "thinking": true,
  "web_search": true
}
```

显式 `null` 表示去掉模式覆盖并使用默认。两个字段都省略时，旧 API 调用方的高级请求体仍按原样处理。UI 会显式传入这两个字段。模式只修改本轮请求的深拷贝，不覆盖保存的模型档案。标题请求不继承聊天的联网工具。

传输分层：`stream_chat()` 负责能力检查和一次回退；`_stream_chat_once()` 只发送一次；路由只追加一次用户消息，避免重试造成重复归档。`ProviderError` 保留上游 HTTP 状态、code、param 供判定。增加回退条件时应先证明它不是鉴权、费用或通用请求错误。

新增 SSE `mode_status` 携带 `modes`、`fallback`、`message`；`tool_activity` 表示检索进度。回退状态写入 `archive.model.modes/mode_notice` 和助手 `message_metadata[].details.modes/mode_notice`。这些字段不含凭据。

搜索引用及扩展上下文保存在助手 sidecar 的 `details.provider_extensions`：`annotations`、`server_tool_calls`、`reasoning_details`。DeepSeek 流式思考按步骤记录，续聊时保留最后步骤的 `reasoning_content`。仅在同一个提供商、模型和 Base URL 下回传必要的扩展；切换模型不泄漏专用工具上下文。标准 `messages` 仍保留 OpenAI 兼容内容。前端来源链接只接受 HTTP(S)。

本地验证：

```powershell
python -m pytest tests/test_chat_modes.py -q
```

测试使用 HTTPX MockTransport 模拟请求，不调用真实计费服务。覆盖厂商请求体、显式关闭、深拷贝、未适配模型、一次回退、禁止重试的错误、输出后失败、多轮搜索上下文和归档。

## 官方依据

- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek Web Tools（含 Chat Completions 协议）](https://api-docs.deepseek.com/guides/web_tools/)
- [百炼深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking/)
- [百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search)
- [SiliconFlow 推理模型](https://docs.siliconflow.cn/docs/userguide/capabilities/reasoning)
- [OpenAI Chat Completions 参数](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [OpenRouter 推理参数](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [OpenRouter 服务端工具](https://openrouter.ai/docs/guides/features/server-tools/overview)
- [OpenRouter web 插件迁移](https://openrouter.ai/blog/agentic-web-tools/)
- [Ollama OpenAI 兼容接口](https://docs.ollama.com/api/openai-compatibility)
