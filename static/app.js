"use strict";

/**
 * 肥鱼 FlowChat browser controller.
 *
 * The frontend deliberately uses no build step or framework: FastAPI serves
 * this file directly.  Keep server data authoritative; localStorage contains
 * only non-secret UI preferences and the selected profile ID.
 */

const MAX_IMPORT_JSON_BYTES = 100 * 1024 * 1024;
const APPROX_CHARS_PER_TOKEN = 2.7;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const byId = (id) => document.getElementById(id);

const els = {
  sidebar: byId("sidebar"), sidebarBackdrop: byId("sidebarBackdrop"), conversationList: byId("conversationList"),
  conversationSearch: byId("conversationSearch"), archiveCount: byId("archiveCount"), conversationTitle: byId("conversationTitle"),
  sourceBadge: byId("sourceBadge"), welcomeState: byId("welcomeState"), messages: byId("messages"), chatScroll: byId("chatScroll"),
  composerWrap: byId("composerWrap"), messageInput: byId("messageInput"), sendButton: byId("sendButton"), modelChip: byId("modelChip"),
  modelChipText: byId("modelChipText"), exportButton: byId("exportButton"), renameButton: byId("renameButton"),
  deleteButton: byId("deleteButton"), starButton: byId("starButton"), organizeButton: byId("organizeButton"), versionsButton: byId("versionsButton"),
  settingsDot: byId("settingsDot"), healthDot: byId("healthDot"), importDialog: byId("importDialog"), settingsDialog: byId("settingsDialog"),
  dataCenterDialog: byId("dataCenterDialog"), organizeDialog: byId("organizeDialog"), confirmDialog: byId("confirmDialog"),
  confirmTitle: byId("confirmTitle"), confirmCopy: byId("confirmCopy"), importError: byId("importError"), shareUrl: byId("shareUrl"),
  jsonFile: byId("jsonFile"), fileName: byId("fileName"), confirmImportButton: byId("confirmImportButton"), providerSelect: byId("providerSelect"),
  providerHint: byId("providerHint"), profileSelect: byId("profileSelect"), profileNameInput: byId("profileNameInput"),
  baseUrlInput: byId("baseUrlInput"), modelInput: byId("modelInput"), apiKeyInput: byId("apiKeyInput"),
  temperatureInput: byId("temperatureInput"), maxTokensInput: byId("maxTokensInput"), systemPromptInput: byId("systemPromptInput"),
  contextModeInput: byId("contextModeInput"), recentMessagesInput: byId("recentMessagesInput"), extraBodyInput: byId("extraBodyInput"),
  extraHeadersInput: byId("extraHeadersInput"), settingsError: byId("settingsError"), testResult: byId("testResult"),
  testConnectionButton: byId("testConnectionButton"), saveSettingsButton: byId("saveSettingsButton"), toastStack: byId("toastStack"),
  contextEstimate: byId("contextEstimate"), batchBar: byId("batchBar"), batchCount: byId("batchCount"),
  healthSummary: byId("healthSummary"), trashList: byId("trashList"), versionList: byId("versionList"),
  backupDestinationInput: byId("backupDestinationInput"), backupIntervalInput: byId("backupIntervalInput"),
  backupRetentionInput: byId("backupRetentionInput"), trashRetentionInput: byId("trashRetentionInput"),
  autoBackupInput: byId("autoBackupInput"), backupDownload: byId("backupDownload"), restoreBackupFile: byId("restoreBackupFile"),
  restoreConflictSelect: byId("restoreConflictSelect"), backupStatus: byId("backupStatus"), folderInput: byId("folderInput"), tagsInput: byId("tagsInput"),
  starredInput: byId("starredInput"), pinnedInput: byId("pinnedInput"),
  thinkingToggle: byId("thinkingToggle"), searchToggle: byId("searchToggle"), modeStatus: byId("modeStatus"),
};

const STORAGE_CONFIG = "flowchat.provider.v2";
const STORAGE_PROFILE = "flowchat.profile.v1";
const state = {
  token: "", summaries: [], active: null, providers: [], profiles: [], profileId: localStorage.getItem(STORAGE_PROFILE) || "",
  importTab: "deepseek", importFile: null, generating: false, abortController: null, batchMode: false,
  selectedIds: new Set(), organizeTarget: "active", config: loadLocalConfig(), filter: "all", followTail: true,
};

// ---------------------------------------------------------------------------
// Local preferences and HTTP transport
// ---------------------------------------------------------------------------

function loadLocalConfig() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE_CONFIG) || localStorage.getItem("flowchat.provider.v1") || "{}"); } catch { saved = {}; }
  const legacyKey = localStorage.getItem("flowchat.apiKey.v1") || sessionStorage.getItem("flowchat.apiKey.v1") || "";
  localStorage.removeItem("flowchat.apiKey.v1"); sessionStorage.removeItem("flowchat.apiKey.v1"); localStorage.removeItem("flowchat.rememberKey.v1");
  return {
    provider_id: saved.provider_id || "deepseek", base_url: saved.base_url || "https://api.deepseek.com",
    model: saved.model || "deepseek-flash", api_key: legacyKey, temperature: saved.temperature ?? 0.7,
    max_tokens: saved.max_tokens ?? null, system_prompt: saved.system_prompt || "", extra_body: saved.extra_body || {},
    extra_headers: saved.extra_headers || {}, timeout_seconds: saved.timeout_seconds || 120,
    context_mode: saved.context_mode || "all", recent_messages: saved.recent_messages || 20,
    attachment_mode: saved.attachment_mode || "text",
  };
}

function saveLocalConfig(config) {
  const { api_key, ...safe } = config;
  localStorage.setItem(STORAGE_CONFIG, JSON.stringify(safe));
  if (state.profileId) localStorage.setItem(STORAGE_PROFILE, state.profileId);
  else localStorage.removeItem(STORAGE_PROFILE);
  state.config = { ...config, api_key: "" };
  updateConfigIndicators();
}

async function api(path, options = {}) {
  // Every mutating API request needs the short-lived token obtained at boot.
  // The token is kept only in memory and changes whenever Python restarts.
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(typeof options.body === "string" ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  if (state.token) headers["X-FlowChat-Token"] = state.token;
  const response = await fetch(path, { ...options, method, headers });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = await response.json();
      message = payload?.error?.message || payload?.detail?.[0]?.msg || payload?.detail || message;
    } catch { /* fallback */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(message, type = "info") {
  const item = document.createElement("div"); item.className = `toast ${type}`; item.textContent = message;
  els.toastStack.append(item); setTimeout(() => item.remove(), 4200);
}
function showInline(element, message) { element.textContent = message; element.classList.toggle("hidden", !message); }
function setButtonBusy(button, busy, label = "处理中…") {
  if (busy) { button.dataset.label ||= button.textContent; button.textContent = label; button.disabled = true; }
  else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
}
function relativeTime(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return ""; if (seconds < 60) return "刚刚"; if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`; if (seconds < 604800) return `${Math.floor(seconds / 86400)} 天前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(new Date(iso));
}
function exactTime(iso) {
  const date = new Date(iso); if (!iso || Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
function sourceLabel(type) { return ({ deepseek_share: "DEEPSEEK 导入", json_import: "JSON 导入", local: "本地对话" })[type] || "本地归档"; }

// ---------------------------------------------------------------------------
// Archive list and transcript rendering
// ---------------------------------------------------------------------------

async function loadProviders() {
  const payload = await api("/api/providers"); state.providers = payload.providers || []; populateProviderSelect();
}
async function loadProfiles() {
  const payload = await api("/api/provider-profiles"); state.profiles = payload.profiles || []; populateProfiles();
  const selected = state.profiles.find((item) => item.id === state.profileId);
  if (!selected && state.profiles.length) state.profileId = state.profiles[0].id;
  if (state.profileId) applyProfileToState(state.profileId);
  updateConfigIndicators();
}
function populateProfiles() {
  els.profileSelect.replaceChildren(new Option("新建配置档案", ""));
  state.profiles.forEach((profile) => els.profileSelect.add(new Option(`${profile.name}${profile.key_stored ? " · Key 已保护" : ""}`, profile.id)));
}
function applyProfileToState(profileId) {
  const profile = state.profiles.find((item) => item.id === profileId); if (!profile) return;
  state.profileId = profile.id; state.config = { ...profile.provider, api_key: "", context_mode: state.config.context_mode || "all",
    recent_messages: state.config.recent_messages || 20 };
  localStorage.setItem(STORAGE_PROFILE, profile.id); updateConfigIndicators();
}

let searchTimer = null;
let searchSequence = 0;
async function loadConversations({ selectFirst = false } = {}) {
  const sequence = ++searchSequence;
  try {
    const q = els.conversationSearch.value.trim();
    const query = new URLSearchParams({ q });
    if (state.filter === "starred") query.set("starred", "true");
    const payload = await api(`/api/conversations?${query}`);
    if (sequence !== searchSequence) return;
    state.summaries = payload.conversations || []; renderConversationList();
    if (selectFirst && !state.active && state.summaries.length) await openConversation(state.summaries[0].id);
  } catch (error) { toast(`读取本地归档失败：${error.message}`, "error"); }
}
function renderConversationList() {
  els.archiveCount.textContent = state.summaries.length; els.conversationList.replaceChildren();
  if (!state.summaries.length) {
    const empty = document.createElement("div"); empty.className = "empty-list";
    empty.textContent = els.conversationSearch.value ? "没有匹配的归档" : "还没有本地归档\n导入链接或新建一段对话吧";
    els.conversationList.append(empty); return;
  }
  for (const item of state.summaries) {
    const button = document.createElement("button"); button.type = "button";
    button.className = `conversation-item ${item.source_type === "local" ? "local" : ""} ${state.active?.id === item.id ? "active" : ""} ${state.batchMode ? "batch-mode" : ""}`;
    button.title = item.title;
    button.setAttribute("aria-current", state.active?.id === item.id ? "true" : "false");
    if (state.batchMode) {
      const check = document.createElement("input"); check.type = "checkbox"; check.className = "conversation-check";
      check.checked = state.selectedIds.has(item.id); check.tabIndex = -1; button.append(check);
    }
    const name = document.createElement("span"); name.className = "conversation-name"; name.textContent = item.title;
    button.append(name);
    button.addEventListener("click", () => state.batchMode ? toggleBatchItem(item.id) : openConversation(item.id));
    els.conversationList.append(button);
  }
}
function toggleBatchItem(id) { state.selectedIds.has(id) ? state.selectedIds.delete(id) : state.selectedIds.add(id); updateBatchBar(); renderConversationList(); }
function updateBatchBar() { els.batchBar.classList.toggle("hidden", !state.batchMode); els.batchCount.textContent = `已选 ${state.selectedIds.size}`; }

function saveDraft() {
  if (!state.active) return;
  try { sessionStorage.setItem(`flowchat.draft.${state.active.id}`, els.messageInput.value); } catch { /* Browser quota is optional. */ }
}

async function openConversation(id) {
  if (state.generating && state.active?.id !== id) return toast("请先停止当前回复", "error");
  saveDraft();
  try { const payload = await api(`/api/conversations/${encodeURIComponent(id)}`); state.active = payload.conversation;
    els.messageInput.value = sessionStorage.getItem(`flowchat.draft.${id}`) || "";
    state.followTail = true; renderConversationList(); renderActiveConversation(); autoResizeInput(); closeSidebar(); }
  catch (error) { toast(error.message, "error"); await loadConversations(); }
}
function setEmptyState(empty) { els.welcomeState.classList.toggle("hidden", !empty); els.messages.classList.toggle("hidden", empty); els.composerWrap.classList.toggle("hidden", empty); }
function renderActiveConversation() {
  const conversation = state.active;
  const controls = [els.renameButton, els.deleteButton, els.starButton, els.organizeButton, els.versionsButton];
  if (!conversation) {
    setEmptyState(true); els.conversationTitle.textContent = "对话归档"; els.sourceBadge.classList.add("hidden");
    controls.forEach((item) => item.disabled = true); els.exportButton.classList.add("disabled"); els.exportButton.href = "#"; updateContextEstimate(); return;
  }
  setEmptyState(false); els.conversationTitle.textContent = conversation.title;
  const titleLabel = conversation.title_info?.method === "model" ? "AI 标题" : conversation.title_info?.method === "manual" ? "手动标题" : "备用标题";
  els.sourceBadge.textContent = `${sourceLabel(conversation.source?.type || "local")} · ${titleLabel}`; els.sourceBadge.classList.remove("hidden");
  controls.forEach((item) => item.disabled = false); els.starButton.textContent = conversation.starred ? "★" : "☆";
  els.exportButton.classList.remove("disabled"); els.exportButton.href = `/api/conversations/${conversation.id}/export`;
  els.messages.replaceChildren(); const metadata = conversation.message_metadata || []; const renderQueue = [];
  (conversation.messages || []).forEach((message, index) => {
    const meta = metadata[index] || {};
    const hiddenText = new Set((meta.details?.attachments || []).map((file) => file.text_index));
    const content = messageText(message.content, hiddenText);
    const article = createMessageElement(message.role, content, meta);
    els.messages.append(article);
    renderAttachments(article, message, meta, index);
    renderQueue.push({ node: article._contentNode, content });
    if (article._thinkingNode) renderQueue.push({ node: article._thinkingNode._textNode, content: metadata[index]?.reasoning_content || "" });
  });
  void renderRichContent(renderQueue);
  updateContextEstimate(); requestAnimationFrame(() => { if (state.followTail) scrollToBottom(); });
}

function messageText(content, excluded = new Set()) {
  return typeof content === "string" ? content : (content || []).filter((part, i) => part.type === "text" && !excluded.has(i)).map((part) => part.text).join("\n\n");
}

function formatBytes(size) { return Number.isFinite(size) ? size < 1048576 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1048576).toFixed(1)} MB` : ""; }

function renderAttachments(article, message, meta, messageIndex) {
  const parts = Array.isArray(message.content) ? message.content : [];
  const files = [...(meta.details?.attachments || [])];
  parts.forEach((part, index) => {
    if (["image_url", "file"].includes(part.type) && !files.some((f) => f.content_index === index)) {
      const uri = part.image_url?.url || part.file?.file_data || "";
      files.push({ name: part.file?.filename || "图片", kind: part.type === "image_url" ? "image" : "file", content_index: index,
        mime_type: uri.match(/^data:([^;]+)/)?.[1], status: uri.startsWith("data:") ? "saved" : "unavailable", error: "此附件仅有外部链接，未保存到本机" });
    }
  });
  if (!files.length) return;
  const list = document.createElement("div"); list.className = "attachment-list";
  for (const file of files) {
    const card = document.createElement("section"); card.className = "attachment-card";
    const saved = file.status === "saved" && Number.isInteger(file.content_index);
    const url = saved ? `/api/conversations/${state.active.id}/attachments/${messageIndex}/${file.content_index}` : null;
    if (saved && /^image\/(png|jpeg|gif|webp)$/.test(file.mime_type)) {
      const preview = document.createElement("button"); preview.type = "button"; preview.className = "attachment-preview"; preview.title = `放大查看 ${file.name}`;
      const image = document.createElement("img"); image.src = url; image.alt = file.name; image.loading = "lazy";
      image.addEventListener("load", () => { if (state.followTail) scrollToBottom(); });
      image.addEventListener("error", () => { image.hidden = true; preview.textContent = "预览不可用，可尝试下载文件"; });
      preview.append(image); preview.addEventListener("click", () => { byId("imagePreview").src = url; byId("imagePreview").alt = file.name; byId("imagePreviewTitle").textContent = file.name; byId("imageDialog").showModal(); }); card.append(preview);
    }
    const info = document.createElement("div"); info.className = "attachment-info";
    const label = document.createElement("div"), name = document.createElement("strong"), status = document.createElement("small");
    name.textContent = file.name;
    status.textContent = [saved ? file.variant === "preview" ? "预览副本已保存" : "已保存到本机" : "未取得文件", formatBytes(file.size), file.error || ""].filter(Boolean).join(" · ");
    label.append(name, status); info.append(label);
    if (url) { const download = document.createElement("a"); download.href = `${url}?download=true`; download.textContent = "下载"; download.className = "text-button"; info.append(download); }
    card.append(info);
    if (file.text) { const details = document.createElement("details"); details.className = "attachment-text";
      const summary = document.createElement("summary"); summary.textContent = `${file.text_source === "deepseek_ocr" ? "OCR 识别文字" : "提取文字"} · ${file.text.length.toLocaleString()} 字`;
      const text = document.createElement("pre"); text.textContent = file.text; details.append(summary, text); card.append(details);
    } else { const note = document.createElement("p"); note.className = "attachment-note"; note.textContent = "没有可用的识别文字。续聊时可在模型设置中启用图片或文件输入。"; card.append(note); }
    list.append(card);
  }
  article._bodyNode.append(list);
}

async function renderRichContent(entries) {
  // Render in bounded batches. If the user switches conversations while the
  // request is running, detached nodes are ignored when the result returns.
  for (let start = 0; start < entries.length; start += 100) {
    const batch = entries.slice(start, start + 100);
    try {
      const payload = await api("/api/render", { method: "POST", body: JSON.stringify({ contents: batch.map((item) => item.content) }) });
      batch.forEach((item, index) => {
        if (!item.node.isConnected || typeof payload.html?.[index] !== "string") return;
        item.node.innerHTML = payload.html[index];
        item.node.classList.add("rendered");
        item.node.querySelectorAll("a[href]").forEach((link) => {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        });
        item.node.querySelectorAll("pre").forEach((pre) => {
          const code = pre.querySelector("code"); if (!code) return;
          const button = document.createElement("button"); button.type = "button"; button.className = "copy-code"; button.textContent = "复制代码";
          button.addEventListener("click", async () => { try { await navigator.clipboard.writeText(code.textContent); button.textContent = "已复制"; } catch { toast("复制失败，请手动选择代码", "error"); } }); pre.prepend(button);
        });
      });
      if (state.followTail) scrollToBottom();
    } catch (error) {
      // Plain text is already present, so renderer outages degrade safely.
      console.warn("Rich message rendering failed", error);
    }
  }
}

function createMessageElement(role, content, metadata = {}, { streaming = false } = {}) {
  const article = document.createElement("article"); article.className = `message ${role}`;
  const avatar = document.createElement("div"); avatar.className = "message-avatar"; avatar.textContent = role === "user" ? "你" : role === "system" ? "S" : "AI";
  const body = document.createElement("div"), head = document.createElement("div"); body.className = "message-body"; head.className = "message-head";
  const author = document.createElement("span"); author.className = "message-author"; author.textContent = role === "user" ? "你" : role === "system" ? "系统" : "助手";
  const time = document.createElement("span"); time.className = "message-time"; time.textContent = exactTime(metadata.created_at);
  const copy = document.createElement("button"); copy.type = "button"; copy.className = "copy-message"; copy.textContent = "复制";
  copy.hidden = !content && !streaming;
  copy.addEventListener("click", async () => { await navigator.clipboard.writeText(article._rawContent || ""); copy.textContent = "已复制"; setTimeout(() => copy.textContent = "复制", 1200); });
  head.append(author, time, copy); body.append(head);
  if (metadata.reasoning_content) { const thinking = createThinkingBlock(metadata.reasoning_content); body.append(thinking); article._thinkingNode = thinking; }
  const node = document.createElement("div"); node.className = `message-content ${streaming ? "streaming" : ""}`; node.textContent = content || "";
  body.append(node); article.append(avatar, body); article._contentNode = node; article._bodyNode = body; article._rawContent = content || "";
  renderModeNotice(article, metadata.details?.mode_notice);
  renderSources(article, metadata.details?.provider_extensions);
  return article;
}

function renderModeNotice(article, message) {
  if (!message) return;
  let note = article.querySelector(".mode-notice");
  if (!note) { note = document.createElement("p"); note.className = "mode-notice"; article._bodyNode.append(note); }
  note.textContent = message;
}

function renderSources(article, extensions) {
  // Treat provider URLs as untrusted input, just like imported Markdown links.
  const candidates = (Array.isArray(extensions?.annotations) ? extensions.annotations : []).map((item) => item?.url_citation || item);
  for (const step of Array.isArray(extensions?.server_tool_calls) ? extensions.server_tool_calls : []) {
    for (const call of Array.isArray(step?.calls) ? step.calls : []) {
      candidates.push(...(Array.isArray(call?.sources) ? call.sources : []), ...(call?.page ? [call.page] : []));
    }
  }
  const sources = new Map();
  for (const source of candidates) {
    try {
      const url = new URL(source.url);
      if (["http:", "https:"].includes(url.protocol)) sources.set(url.href, source.title || url.hostname);
    } catch { /* Invalid or unsafe citations stay in JSON, never in link targets. */ }
  }
  if (!sources.size) return;
  const details = document.createElement("details"), summary = document.createElement("summary");
  details.className = "message-sources"; summary.textContent = `参考来源 · ${sources.size}`; details.append(summary);
  for (const [url, title] of sources) {
    const link = document.createElement("a"); link.href = url; link.textContent = title; link.target = "_blank"; link.rel = "noopener noreferrer"; details.append(link);
  }
  article._bodyNode.append(details);
}
function createThinkingBlock(content, open = false) {
  const details = document.createElement("details"); details.className = "thinking-block"; details.open = open;
  const summary = document.createElement("summary"); summary.textContent = "思考过程";
  const text = document.createElement("div"); text.className = "thinking-content"; text.textContent = content; details.append(summary, text); details._textNode = text; details._rawContent = content; return details;
}
function updateContextEstimate() {
  if (!state.active) { els.contextEstimate.textContent = "上下文约 0 tokens"; return; }
  let messages = state.active.messages || [];
  if (state.config.context_mode === "recent") messages = messages.slice(-(state.config.recent_messages || 20));
  const chars = messages.reduce((sum, item) => sum + messageText(item.content).length, 0) + (state.config.system_prompt?.length || 0);
  const attachments = messages.reduce((sum, item) => sum + (Array.isArray(item.content) ? item.content.filter((p) => p.type !== "text").length : 0), 0);
  const mode = { text: "文字 / OCR", images: "文字 + 图片", files: "文字 + 图片 + 文件" }[state.config.attachment_mode || "text"];
  els.contextEstimate.textContent = `${messages.length} 条消息 · 文字约 ${Math.ceil(chars / APPROX_CHARS_PER_TOKEN).toLocaleString()} tokens${attachments ? ` · ${attachments} 个附件 · 发送：${mode}（附件用量另计）` : ""}`;
}

async function createConversation() {
  try { const payload = await api("/api/conversations", { method: "POST", body: JSON.stringify({ title: "新对话" }) }); await loadConversations(); await openConversation(payload.conversation.id); }
  catch (error) { toast(error.message, "error"); }
}
function openImportDialog() { showInline(els.importError, ""); els.importDialog.showModal(); setTimeout(() => els.shareUrl.focus(), 50); }
function setImportTab(tab) { state.importTab = tab; $$('[data-import-tab]').forEach((button) => button.classList.toggle("active", button.dataset.importTab === tab)); byId("deepseekPanel").classList.toggle("hidden", tab !== "deepseek"); byId("jsonPanel").classList.toggle("hidden", tab !== "json"); }
function parseShareUrls(value) { return [...new Set((value.match(/https:\/\/chat\.deepseek\.com\/share\/[a-zA-Z0-9_-]+/g) || []))]; }
async function importConversation() {
  showInline(els.importError, ""); setButtonBusy(els.confirmImportButton, true, "正在读取…");
  try {
    let activeId = null;
    if (state.importTab === "deepseek") {
      const urls = parseShareUrls(els.shareUrl.value); if (!urls.length) throw new Error("请粘贴有效的 DeepSeek 分享链接");
      if (urls.length === 1) {
        const payload = await api("/api/import/deepseek", { method: "POST", body: JSON.stringify({ url: urls[0], profile_id: state.profileId || null, duplicate_action: byId("duplicateAction").value }) });
        activeId = payload.conversation.id; toast(payload.duplicate ? "该链接已导入，已打开现有归档" : `已归档 ${payload.conversation.messages.length} 条消息`, "success");
      } else {
        const payload = await api("/api/import/deepseek/batch", { method: "POST", body: JSON.stringify({ urls, profile_id: state.profileId || null, duplicate_action: byId("duplicateAction").value }) });
        activeId = payload.results.find((item) => item.ok)?.id || null;
        toast(`批量导入完成：成功 ${payload.success}，重复 ${payload.duplicates}，失败 ${payload.failed}`, payload.failed ? "info" : "success");
      }
    } else {
      if (!state.importFile) throw new Error("请选择 JSON 文件"); if (state.importFile.size > MAX_IMPORT_JSON_BYTES) throw new Error("JSON 文件不能超过 100 MB");
      let archive; try { archive = JSON.parse(await state.importFile.text()); } catch { throw new Error("文件不是有效 JSON"); }
      const payload = await api("/api/import/json", { method: "POST", body: JSON.stringify({ archive, fallback_title: state.importFile.name.replace(/\.json$/i, ""), profile_id: state.profileId || null }) });
      activeId = payload.conversation.id;
    }
    els.importDialog.close(); els.shareUrl.value = ""; state.importFile = null; els.fileName.textContent = "选择或拖入 JSON 文件";
    await loadConversations(); if (activeId) await openConversation(activeId);
  } catch (error) { showInline(els.importError, error.message); }
  finally { setButtonBusy(els.confirmImportButton, false); }
}

function populateProviderSelect() { els.providerSelect.replaceChildren(); state.providers.forEach((item) => els.providerSelect.add(new Option(item.name, item.id))); }

// ---------------------------------------------------------------------------
// Provider profiles and billable connection test
// ---------------------------------------------------------------------------
function openSettings() {
  populateProfiles(); els.profileSelect.value = state.profileId; const profile = state.profiles.find((item) => item.id === state.profileId);
  els.profileNameInput.value = profile?.name || `${state.providers.find((item) => item.id === state.config.provider_id)?.name || "模型"} 配置`;
  fillSettings(state.config, profile?.key_stored); showInline(els.settingsError, ""); showInline(els.testResult, ""); els.settingsDialog.showModal();
}
function fillSettings(config, keyStored = false) {
  els.providerSelect.value = state.providers.some((item) => item.id === config.provider_id) ? config.provider_id : "custom";
  els.baseUrlInput.value = config.base_url || ""; els.modelInput.value = config.model || ""; els.apiKeyInput.value = config.api_key || "";
  els.apiKeyInput.placeholder = keyStored ? "Key 已存入系统凭据库；留空表示保持不变" : "sk-…（本地服务可留空）";
  els.temperatureInput.value = config.temperature ?? 0.7; els.maxTokensInput.value = config.max_tokens ?? "";
  els.systemPromptInput.value = config.system_prompt || ""; els.contextModeInput.value = config.context_mode || "all";
  els.recentMessagesInput.value = config.recent_messages || 20;
  byId("attachmentModeInput").value = config.attachment_mode || "text";
  els.extraBodyInput.value = Object.keys(config.extra_body || {}).length ? JSON.stringify(config.extra_body, null, 2) : "";
  els.extraHeadersInput.value = Object.keys(config.extra_headers || {}).length ? JSON.stringify(config.extra_headers, null, 2) : ""; updateProviderHint();
}
function updateProviderHint() { els.providerHint.textContent = state.providers.find((item) => item.id === els.providerSelect.value)?.hint || ""; }
function applyProviderPreset() {
  const provider = state.providers.find((item) => item.id === els.providerSelect.value); if (!provider) return;
  els.baseUrlInput.value = provider.base_url || ""; els.modelInput.value = provider.model || "";
  if (!els.profileNameInput.value || els.profileNameInput.value.endsWith("配置")) els.profileNameInput.value = `${provider.name} 配置`; updateProviderHint();
}
function selectProfile() {
  const id = els.profileSelect.value; if (!id) { state.profileId = ""; els.profileNameInput.value = ""; return; }
  const profile = state.profiles.find((item) => item.id === id); if (!profile) return;
  state.profileId = id; els.profileNameInput.value = profile.name; fillSettings({ ...profile.provider, context_mode: state.config.context_mode, recent_messages: state.config.recent_messages }, profile.key_stored);
}
function parseJsonField(element, label) { const value = element.value.trim(); if (!value) return {}; let parsed; try { parsed = JSON.parse(value); } catch { throw new Error(`${label}不是有效 JSON`); } if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label}必须是 JSON 对象`); return parsed; }
function readSettingsForm() {
  const baseUrl = els.baseUrlInput.value.trim().replace(/\/$/, ""), model = els.modelInput.value.trim();
  if (!/^https?:\/\//i.test(baseUrl)) throw new Error("Base URL 必须以 http:// 或 https:// 开头"); if (!model) throw new Error("请填写模型 ID");
  const temperature = els.temperatureInput.value === "" ? null : Number(els.temperatureInput.value);
  const maxTokens = els.maxTokensInput.value === "" ? null : Number(els.maxTokensInput.value);
  if (temperature !== null && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) throw new Error("Temperature 必须在 0 到 2 之间");
  if (maxTokens !== null && (!Number.isInteger(maxTokens) || maxTokens < 1)) throw new Error("最大 Tokens 必须是正整数");
  return { provider_id: els.providerSelect.value || "custom", base_url: baseUrl, model, api_key: els.apiKeyInput.value.trim(), temperature,
    max_tokens: maxTokens, system_prompt: els.systemPromptInput.value, extra_body: parseJsonField(els.extraBodyInput, "额外请求体"),
    extra_headers: parseJsonField(els.extraHeadersInput, "额外请求头"), timeout_seconds: 120,
    context_mode: els.contextModeInput.value, recent_messages: Number(els.recentMessagesInput.value) || 20,
    attachment_mode: byId("attachmentModeInput").value };
}
async function saveSettings() {
  showInline(els.settingsError, "");
  try {
    const config = readSettingsForm(), name = els.profileNameInput.value.trim(); if (!name) throw new Error("请填写配置档案名称");
    const provider = { ...config }; delete provider.context_mode; delete provider.recent_messages;
    const payload = await api("/api/provider-profiles", { method: "POST", body: JSON.stringify({ id: state.profileId || null, name, provider }) });
    state.profileId = payload.profile.id; config.api_key = ""; saveLocalConfig(config); await loadProfiles(); els.settingsDialog.close();
    toast(payload.profile.secret_backend === "session_only" ? "配置已保存；系统凭据库不可用，Key 仅保留到程序关闭" : "配置和 Key 已安全保存", "success");
  } catch (error) { showInline(els.settingsError, error.message); }
}
async function testProviderConnection() {
  showInline(els.settingsError, ""); showInline(els.testResult, ""); let config; try { config = readSettingsForm(); } catch (error) { return showInline(els.settingsError, error.message); }
  setButtonBusy(els.testConnectionButton, true, "测试中…");
  try {
    const selected = state.profiles.find((item) => item.id === state.profileId);
    const useStoredProfile = selected && !config.api_key && selected.provider.base_url === config.base_url && selected.provider.model === config.model;
    const result = useStoredProfile
      ? await api(`/api/provider-profiles/${state.profileId}/test`, { method: "POST", body: "{}" })
      : await api("/api/providers/test", { method: "POST", body: JSON.stringify({ provider: config }) });
    const tokens = result.usage?.total_tokens; showInline(els.testResult, `${result.message} · ${result.model}${tokens ? ` · ${tokens} tokens` : ""}${result.sample ? ` · 回复：${result.sample.slice(0, 80)}` : ""}`);
  }
  catch (error) { showInline(els.settingsError, error.message); } finally { setButtonBusy(els.testConnectionButton, false); }
}
async function fetchModels() {
  if (!state.profileId) return toast("请先保存配置档案，再获取模型列表", "error");
  setButtonBusy(byId("fetchModelsButton"), true, "读取中");
  try { const payload = await api(`/api/provider-profiles/${state.profileId}/models`); if (!payload.models.length) throw new Error("服务商未返回模型"); const value = window.prompt(`可用模型（共 ${payload.models.length} 个），请输入要使用的模型：\n\n${payload.models.slice(0, 80).join("\n")}`, els.modelInput.value); if (value) els.modelInput.value = value.trim(); }
  catch (error) { toast(error.message, "error"); } finally { setButtonBusy(byId("fetchModelsButton"), false); }
}
async function deleteProfile() {
  if (!state.profileId) return; if (!await confirmAction("删除配置档案？", "配置和系统凭据库中的 API Key 都会删除，对话归档不受影响。")) return;
  await api(`/api/provider-profiles/${state.profileId}`, { method: "DELETE" }); state.profileId = ""; localStorage.removeItem(STORAGE_PROFILE); await loadProfiles(); els.settingsDialog.close(); toast("配置档案已删除", "success");
}
function updateConfigIndicators() {
  const profile = state.profiles.find((item) => item.id === state.profileId), configured = Boolean(profile);
  els.settingsDot.classList.toggle("configured", configured); els.modelChip.classList.toggle("configured", configured);
  els.modelChipText.textContent = profile ? `${profile.name} · ${profile.provider.model}` : "配置模型"; updateContextEstimate();
  updateModeControls();
}

function modePreferenceKey() {
  const profile = state.profiles.find((item) => item.id === state.profileId);
  // A changed model/endpoint gets fresh preferences; a rejected old parameter
  // is never carried silently into a newly edited profile.
  return profile ? `flowchat.modes.v1.${profile.id}.${profile.updated_at}` : "";
}

function currentModes() {
  const profile = state.profiles.find((item) => item.id === state.profileId);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(modePreferenceKey()) || "{}"); } catch { /* Use safe defaults. */ }
  if (!saved || typeof saved !== "object") saved = {};
  const modes = {};
  for (const name of ["thinking", "web_search"]) {
    modes[name] = profile?.capabilities?.[name]?.supported
      ? ([true, false, null].includes(saved[name]) ? saved[name] : false) : null;
  }
  return { ...modes, notice: typeof saved.notice === "string" ? saved.notice : "" };
}

function saveModes(modes, key = modePreferenceKey()) {
  if (key) localStorage.setItem(key, JSON.stringify(modes));
  updateModeControls();
}

function updateModeControls() {
  const profile = state.profiles.find((item) => item.id === state.profileId), modes = currentModes();
  const unavailable = [];
  for (const [name, button, label] of [["thinking", els.thinkingToggle, "思考"], ["web_search", els.searchToggle, "联网"]]) {
    const capability = profile?.capabilities?.[name], supported = Boolean(capability?.supported);
    const status = !supported ? "未适配" : modes[name] === null ? "默认" : modes[name] ? "开" : "关";
    button.setAttribute("aria-pressed", String(modes[name] === true));
    button.setAttribute("aria-label", `${label}：${status}`);
    // Unsupported controls remain focusable to explain the reason on mobile
    // and with a keyboard; only an in-flight request disables interaction.
    button.setAttribute("aria-disabled", String(state.generating));
    button.dataset.unsupported = String(!supported);
    button.disabled = state.generating;
    button.title = supported && modes[name] === null ? "已回到服务商默认模式；点击可重新启用。默认不等于关闭模型固有思考。" : capability?.reason || "请先保存模型配置";
    $(".mode-state", button).textContent = status;
    if (!supported) unavailable.push(label);
  }
  const message = !profile ? "选择模型后可设置思考与联网。" : modes.notice || (unavailable.length
    ? `${unavailable.join("、")}暂未适配当前模型，使用默认模式。点击按钮查看原因。`
    : modes.web_search ? "允许按需联网，可能产生搜索费用；参数不兼容时自动回退一次。"
    : modes.thinking ? "思考已开启，可能增加耗时与 token 费用。" : Object.values(modes).includes(null)
      ? "未开启的功能使用服务商默认模式；默认不等于关闭模型固有思考。"
      : "思考与联网已关闭；设置不兼容时自动回到模型默认模式。");
  els.modeStatus.textContent = message;
  els.modeStatus.classList.toggle("fallback", Boolean(modes.notice));
}

function toggleMode(name) {
  if (state.generating) return;
  const profile = state.profiles.find((item) => item.id === state.profileId);
  if (!profile?.capabilities?.[name]?.supported) return toast(profile?.capabilities?.[name]?.reason || "请先保存模型配置");
  const modes = currentModes(); modes[name] = modes[name] !== true; modes.notice = ""; saveModes(modes);
}

function autoResizeInput() { els.messageInput.style.height = "auto"; els.messageInput.style.height = `${Math.min(els.messageInput.scrollHeight, 190)}px`; els.sendButton.disabled = !state.generating && !els.messageInput.value.trim(); }
function setGenerating(value) { state.generating = value; els.messageInput.readOnly = value; els.sendButton.classList.toggle("generating", value); els.sendButton.disabled = value ? false : !els.messageInput.value.trim(); els.modelChip.disabled = value; updateModeControls(); }
function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}
function scrollToBottom() { els.chatScroll.scrollTop = els.chatScroll.scrollHeight; }
function appendStreamError(article, message) { article._contentNode.classList.remove("streaming"); const error = document.createElement("div"); error.className = "message-error"; error.textContent = message; article._bodyNode.append(error); }

function applyChatEvent(parsed, assistantArticle) {
  if (!parsed) return null;
  if (parsed.event === "delta") {
    assistantArticle._rawContent += parsed.data.content || "";
    assistantArticle._contentNode.textContent = assistantArticle._rawContent;
  } else if (parsed.event === "reasoning_delta") {
    if (!assistantArticle._thinkingNode) {
      assistantArticle._thinkingNode = createThinkingBlock("", true);
      assistantArticle._bodyNode.insertBefore(assistantArticle._thinkingNode, assistantArticle._contentNode);
    }
    assistantArticle._thinkingNode._rawContent += parsed.data.content || "";
    assistantArticle._thinkingNode._textNode.textContent = assistantArticle._thinkingNode._rawContent;
  } else if (parsed.event === "mode_status") {
    const previous = assistantArticle._modePreferences;
    assistantArticle._modePreferences = { ...previous, ...parsed.data.modes, notice: parsed.data.fallback ? parsed.data.message : previous.notice };
    saveModes(assistantArticle._modePreferences, assistantArticle._modeKey);
    if (parsed.data.fallback) { renderModeNotice(assistantArticle, parsed.data.message); toast(parsed.data.message); }
  } else if (parsed.event === "tool_activity") {
    els.modeStatus.textContent = parsed.data.message;
  } else if (parsed.event === "error") {
    return parsed.data.message || "模型请求失败";
  }
  if (state.followTail) scrollToBottom();
  return null;
}

async function consumeChatStream(response, assistantArticle) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let streamError = null;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() || "";
    if (done && buffer.trim()) blocks.push(buffer);

    for (const block of blocks) {
      const eventError = applyChatEvent(parseSseBlock(block), assistantArticle);
      streamError ||= eventError;
    }
    if (done) return streamError;
  }
}

async function sendMessage() {
  if (state.generating) { state.abortController?.abort(); return; } if (!state.active) return;
  const content = els.messageInput.value.trim(); if (!content) return; if (!state.profileId) { toast("请先保存模型配置档案", "error"); return openSettings(); }
  const { thinking, web_search } = currentModes();
  const userArticle = createMessageElement("user", content, { created_at: new Date().toISOString() });
  const assistantArticle = createMessageElement("assistant", "", {}, { streaming: true }); els.messages.append(userArticle, assistantArticle);
  assistantArticle._modePreferences = currentModes(); assistantArticle._modeKey = modePreferenceKey();
  els.messageInput.value = ""; saveDraft(); state.followTail = true; autoResizeInput(); scrollToBottom(); setGenerating(true); state.abortController = new AbortController();
  try {
    const response = await fetch(`/api/conversations/${state.active.id}/chat`, { method: "POST", headers: { "Content-Type": "application/json", "X-FlowChat-Token": state.token },
      body: JSON.stringify({ content, profile_id: state.profileId, thinking, web_search, context_mode: state.config.context_mode || "all", recent_messages: state.config.recent_messages || 20 }), signal: state.abortController.signal });
    if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(payload?.error?.message || `请求失败（${response.status}）`); }
    const streamError = await consumeChatStream(response, assistantArticle);
    assistantArticle._contentNode.classList.remove("streaming"); if (streamError) toast(streamError, "error");
  } catch (error) { if (error.name === "AbortError") toast("已停止生成；部分回复会尽量保存", "info"); else { toast(error.message, "error"); els.messageInput.value = content; saveDraft(); } }
  finally { setGenerating(false); state.abortController = null; await loadConversations(); if (state.active) await openConversation(state.active.id); }
}

async function renameActive() {
  if (!state.active) return; const title = window.prompt("新的对话标题", state.active.title); if (!title?.trim()) return;
  const payload = await api(`/api/conversations/${state.active.id}`, { method: "PATCH", body: JSON.stringify({ title: title.trim() }) }); state.active = payload.conversation; await loadConversations(); renderActiveConversation();
}
async function toggleStar() { if (!state.active) return; const payload = await api(`/api/conversations/${state.active.id}`, { method: "PATCH", body: JSON.stringify({ starred: !state.active.starred }) }); state.active = payload.conversation; await loadConversations(); renderActiveConversation(); }
function openOrganize(target = "active") {
  if (target === "batch" && !state.selectedIds.size) return toast("请先选择对话", "error"); state.organizeTarget = target;
  const source = target === "active" ? state.active : null; els.folderInput.value = source?.folder || ""; els.tagsInput.value = (source?.tags || []).join(", "); els.starredInput.checked = Boolean(source?.starred); els.pinnedInput.checked = Boolean(source?.pinned); els.organizeDialog.showModal();
}
async function saveOrganize() {
  const changes = { folder: els.folderInput.value.trim(), tags: els.tagsInput.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean), starred: els.starredInput.checked, pinned: els.pinnedInput.checked };
  if (state.organizeTarget === "batch") await api("/api/conversations/batch", { method: "POST", body: JSON.stringify({ ids: [...state.selectedIds], action: "organize", ...changes }) });
  else { const payload = await api(`/api/conversations/${state.active.id}`, { method: "PATCH", body: JSON.stringify(changes) }); state.active = payload.conversation; }
  els.organizeDialog.close(); await loadConversations(); renderActiveConversation(); toast("整理信息已保存", "success");
}
async function deleteActive() { if (!state.active || !await confirmAction("移至回收站？", `“${state.active.title}”可以在数据保全中心恢复。`)) return; await api(`/api/conversations/${state.active.id}`, { method: "DELETE" }); state.active = null; renderActiveConversation(); await loadConversations(); toast("已移至回收站", "success"); }
async function batchTrash() { if (!state.selectedIds.size || !await confirmAction("批量移至回收站？", `将移动 ${state.selectedIds.size} 个对话，可在数据保全中心恢复。`)) return; await api("/api/conversations/batch", { method: "POST", body: JSON.stringify({ ids: [...state.selectedIds], action: "trash" }) }); state.selectedIds.clear(); state.batchMode = false; updateBatchBar(); await loadConversations(); }
function confirmAction(title, copy) { return new Promise((resolve) => { els.confirmTitle.textContent = title; els.confirmCopy.textContent = copy; const close = () => { els.confirmDialog.removeEventListener("close", close); resolve(els.confirmDialog.returnValue === "confirm"); }; els.confirmDialog.addEventListener("close", close); els.confirmDialog.showModal(); }); }

async function openDataCenter() { els.dataCenterDialog.showModal(); await Promise.all([refreshHealth(), loadBackupSettings(), refreshTrash(), refreshVersions()]); }
async function refreshHealth() { try { const health = await api("/api/data/health"); els.healthSummary.className = `health-card ${health.ok ? "ok" : "warn"}`; els.healthSummary.textContent = health.ok ? `健康：${health.healthy_archives} 个归档通过检查` : `发现 ${health.issues.length} 个问题：${health.issues.slice(0, 3).map((item) => `${item.file} ${item.message}`).join("；")}`; els.healthDot.classList.toggle("configured", health.ok); } catch (error) { els.healthSummary.textContent = error.message; } }
async function repairData() { setButtonBusy(byId("repairDataButton"), true, "修复中…"); try { const result = await api("/api/data/repair", { method: "POST", body: "{}" }); toast(`修复完成：成功 ${result.repaired.length}，失败 ${result.failed.length}`, result.failed.length ? "info" : "success"); await refreshHealth(); } catch (error) { toast(error.message, "error"); } finally { setButtonBusy(byId("repairDataButton"), false); } }
async function loadBackupSettings() {
  const { settings } = await api("/api/backups/settings");
  els.autoBackupInput.checked = settings.auto_enabled;
  els.backupDestinationInput.value = settings.destination || "";
  els.backupIntervalInput.value = settings.interval_hours;
  els.backupRetentionInput.value = settings.retention_count;
  els.trashRetentionInput.value = settings.trash_retention_days;
  els.backupStatus.textContent = settings.last_error
    ? `最近失败：${settings.last_error}`
    : settings.last_backup_at
      ? `最近成功：${exactTime(settings.last_backup_at)} · ${settings.last_backup_path || ""}`
      : "尚无自动备份记录";
  els.backupStatus.classList.toggle("error", Boolean(settings.last_error));
}
async function saveBackupSettings() { const settings = { auto_enabled: els.autoBackupInput.checked, destination: els.backupDestinationInput.value.trim(), interval_hours: Number(els.backupIntervalInput.value), retention_count: Number(els.backupRetentionInput.value), trash_retention_days: Number(els.trashRetentionInput.value) }; await api("/api/backups/settings", { method: "PUT", body: JSON.stringify(settings) }); toast("自动备份设置已保存", "success"); }
async function createBackup() { const button = byId("createBackupButton"); setButtonBusy(button, true, "创建中…"); try { const result = await api("/api/backups/create", { method: "POST", body: "{}" }); els.backupDownload.href = result.download_url; els.backupDownload.download = result.name; els.backupDownload.textContent = `下载 ${result.name}（${Math.ceil(result.size / 1024)} KB）`; els.backupDownload.classList.remove("hidden"); toast("完整备份已创建并通过清单校验", "success"); } catch (error) { toast(error.message, "error"); } finally { setButtonBusy(button, false); } }
async function restoreBackup() { const file = els.restoreBackupFile.files?.[0]; if (!file) return toast("请选择备份 ZIP", "error"); if (!await confirmAction("恢复备份？", "系统会先验证校验和，再按所选冲突策略恢复。")) return; const response = await fetch(`/api/backups/restore?conflict=${encodeURIComponent(els.restoreConflictSelect.value)}`, { method: "POST", headers: { "Content-Type": "application/zip", "X-FlowChat-Token": state.token }, body: file }); const payload = await response.json(); if (!response.ok) return toast(payload?.error?.message || "恢复失败", "error"); toast(`恢复完成：新增 ${payload.imported}，替换 ${payload.replaced}，副本 ${payload.copied}，跳过 ${payload.skipped}`, "success"); await Promise.all([loadConversations(), refreshHealth()]); }
async function refreshTrash() { const payload = await api("/api/trash"); renderManagementList(els.trashList, payload.items, (item) => ({ title: item.title, meta: `${item.message_count} 条消息 · ${exactTime(item.deleted_at)}`, actions: [{ label: "恢复", run: () => restoreTrash(item.trash_name) }, { label: "永久删除", danger: true, run: () => permanentlyDeleteTrash(item.trash_name, item.title) }] })); }
async function restoreTrash(name) { await api(`/api/trash/${encodeURIComponent(name)}/restore`, { method: "POST", body: "{}" }); await Promise.all([refreshTrash(), loadConversations()]); toast("对话已恢复", "success"); }
async function permanentlyDeleteTrash(name, title) { if (!await confirmAction("永久删除？", `“${title}”将无法恢复。`)) return; await api(`/api/trash/${encodeURIComponent(name)}`, { method: "DELETE" }); await refreshTrash(); }
async function refreshVersions() { if (!state.active) { renderManagementList(els.versionList, [], () => ({})); return; } const payload = await api(`/api/conversations/${state.active.id}/versions`); renderManagementList(els.versionList, payload.versions, (item) => ({ title: `${item.message_count} 条消息`, meta: `${exactTime(item.updated_at)} · ${item.title}`, actions: [{ label: "恢复此版本", run: () => restoreVersion(item.name) }] })); }
async function restoreVersion(name) { if (!await confirmAction("恢复历史版本？", "当前状态也会先保存为版本，因此仍可撤销。")) return; const payload = await api(`/api/conversations/${state.active.id}/versions/${encodeURIComponent(name)}/restore`, { method: "POST", body: "{}" }); state.active = payload.conversation; renderActiveConversation(); await Promise.all([loadConversations(), refreshVersions()]); toast("已恢复历史版本", "success"); }
function renderManagementList(root, items, map) {
  root.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "management-empty";
    empty.textContent = "暂无内容";
    root.append(empty);
    return;
  }
  for (const item of items) {
    const view = map(item);
    const row = document.createElement("div");
    const text = document.createElement("div");
    const title = document.createElement("strong");
    const meta = document.createElement("small");
    const actions = document.createElement("div");
    row.className = "management-item";
    actions.className = "management-actions";
    title.textContent = view.title;
    meta.textContent = view.meta;
    text.append(title, meta);
    for (const action of view.actions || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action.label;
      if (action.danger) button.classList.add("danger");
      button.addEventListener("click", action.run);
      actions.append(button);
    }
    row.append(text, actions);
    root.append(row);
  }
}

function openSidebar() { els.sidebar.classList.add("open"); els.sidebarBackdrop.classList.add("open"); }
function closeSidebar() { els.sidebar.classList.remove("open"); els.sidebarBackdrop.classList.remove("open"); }
function bindEvents() {
  els.thinkingToggle.addEventListener("click", () => toggleMode("thinking"));
  els.searchToggle.addEventListener("click", () => toggleMode("web_search"));
  byId("imagePreviewClose").addEventListener("click", () => byId("imageDialog").close());
  $$('[data-archive-filter]').forEach((button) => button.addEventListener("click", () => { state.filter = button.dataset.archiveFilter; $$('[data-archive-filter]').forEach((item) => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); }); loadConversations(); }));
  byId("jumpLatest").addEventListener("click", () => { state.followTail = true; scrollToBottom(); });
  els.chatScroll.addEventListener("scroll", () => { state.followTail = els.chatScroll.scrollHeight - els.chatScroll.scrollTop - els.chatScroll.clientHeight < 100; byId("jumpLatest").classList.toggle("hidden", state.followTail); });
  document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSidebar(); els.conversationSearch.focus(); } });
  document.addEventListener("click", (event) => { const menu = byId("conversationMenu"); if (!menu.contains(event.target) || event.target.closest("button,a")) menu.open = false; });
  els.messageInput.addEventListener("input", saveDraft);
  window.addEventListener("beforeunload", saveDraft);
  byId("importButton").addEventListener("click", openImportDialog); byId("welcomeImportButton").addEventListener("click", openImportDialog);
  byId("newChatButton").addEventListener("click", createConversation); byId("welcomeNewButton").addEventListener("click", createConversation);
  byId("settingsButton").addEventListener("click", openSettings); els.modelChip.addEventListener("click", openSettings); byId("dataCenterButton").addEventListener("click", openDataCenter);
  els.conversationSearch.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadConversations, 220); });
  els.renameButton.addEventListener("click", renameActive); els.deleteButton.addEventListener("click", deleteActive); els.starButton.addEventListener("click", toggleStar);
  els.organizeButton.addEventListener("click", () => openOrganize("active")); els.versionsButton.addEventListener("click", openDataCenter);
  byId("openSidebarButton").addEventListener("click", openSidebar); byId("closeSidebarButton").addEventListener("click", closeSidebar); els.sidebarBackdrop.addEventListener("click", closeSidebar);
  byId("batchModeButton").addEventListener("click", () => { state.batchMode = !state.batchMode; state.selectedIds.clear(); updateBatchBar(); renderConversationList(); });
  byId("batchCancelButton").addEventListener("click", () => { state.batchMode = false; state.selectedIds.clear(); updateBatchBar(); renderConversationList(); });
  byId("batchOrganizeButton").addEventListener("click", () => openOrganize("batch")); byId("batchTrashButton").addEventListener("click", batchTrash);
  $$('[data-import-tab]').forEach((button) => button.addEventListener("click", () => setImportTab(button.dataset.importTab)));
  byId("pasteButton").addEventListener("click", async () => { try { els.shareUrl.value = await navigator.clipboard.readText(); } catch { toast("请手动粘贴链接", "error"); } });
  els.confirmImportButton.addEventListener("click", importConversation); byId("importForm").addEventListener("submit", (event) => { event.preventDefault(); importConversation(); });
  byId("importCloseButton").addEventListener("click", () => els.importDialog.close()); byId("importCancelButton").addEventListener("click", () => els.importDialog.close());
  els.jsonFile.addEventListener("change", () => { state.importFile = els.jsonFile.files?.[0] || null; els.fileName.textContent = state.importFile?.name || "选择或拖入 JSON 文件"; });
  const fileDrop = byId("fileDrop"); ["dragenter", "dragover"].forEach((type) => fileDrop.addEventListener(type, (event) => { event.preventDefault(); fileDrop.classList.add("dragging"); })); ["dragleave", "drop"].forEach((type) => fileDrop.addEventListener(type, (event) => { event.preventDefault(); fileDrop.classList.remove("dragging"); })); fileDrop.addEventListener("drop", (event) => { state.importFile = event.dataTransfer?.files?.[0] || null; els.fileName.textContent = state.importFile?.name || "选择或拖入 JSON 文件"; });
  els.profileSelect.addEventListener("change", selectProfile); els.providerSelect.addEventListener("change", applyProviderPreset); byId("fetchModelsButton").addEventListener("click", fetchModels);
  byId("passwordToggle").addEventListener("click", (event) => { const show = els.apiKeyInput.type === "password"; els.apiKeyInput.type = show ? "text" : "password"; event.currentTarget.textContent = show ? "隐藏" : "显示"; });
  els.saveSettingsButton.addEventListener("click", saveSettings); els.testConnectionButton.addEventListener("click", testProviderConnection); byId("deleteProfileButton").addEventListener("click", deleteProfile);
  byId("settingsForm").addEventListener("submit", (event) => { event.preventDefault(); saveSettings(); }); byId("settingsCloseButton").addEventListener("click", () => els.settingsDialog.close()); byId("settingsCancelButton").addEventListener("click", () => els.settingsDialog.close());
  els.contextModeInput.addEventListener("change", () => { state.config.context_mode = els.contextModeInput.value; updateContextEstimate(); });
  byId("dataCenterCloseButton").addEventListener("click", () => els.dataCenterDialog.close()); byId("repairDataButton").addEventListener("click", repairData);
  byId("createBackupButton").addEventListener("click", createBackup); byId("saveBackupSettingsButton").addEventListener("click", saveBackupSettings); byId("restoreBackupButton").addEventListener("click", restoreBackup); byId("refreshTrashButton").addEventListener("click", refreshTrash);
  byId("organizeCloseButton").addEventListener("click", () => els.organizeDialog.close()); byId("organizeCancelButton").addEventListener("click", () => els.organizeDialog.close()); byId("saveOrganizeButton").addEventListener("click", saveOrganize);
  els.messageInput.addEventListener("input", autoResizeInput); els.messageInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); } }); els.sendButton.addEventListener("click", sendMessage);
}

async function init() {
  try { const session = await fetch("/api/session").then((response) => response.json()); state.token = session.token; }
  catch { document.body.textContent = "无法建立本地安全会话，请重新启动肥鱼 FlowChat。"; return; }
  bindEvents();
  try { await Promise.all([loadProviders(), loadProfiles(), loadConversations(), refreshHealth()]); }
  catch (error) { toast(error.message, "error"); }
  renderActiveConversation(); autoResizeInput(); updateBatchBar();
}

init();
