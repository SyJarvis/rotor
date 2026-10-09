// Basic chat with per-conversation settings, durable attachments and streaming responses.

import { t } from "../i18n.js";
import {
  adminCsrfHeaders,
  api,
  copyText,
  handleAdminUnauthorized,
} from "../api.js?v=2";
import { escapeHtml, refreshIcons, toast } from "../ui.js";
import { saveAttachment, getAttachment, deleteAttachments } from "../chat-attachments.js?v=1";

const STORAGE_KEY = "rotor.chat.conversations.v1";
const ACTIVE_KEY = "rotor.chat.active.v1";
const SETTINGS_VISIBLE_KEY = "rotor.chat.settings.visible.v1";
const LEGACY_STORAGE_KEY = "rotor.mindagent.conversations.v1";
const LEGACY_ACTIVE_KEY = "rotor.mindagent.active.v1";

let conversations = readConversations();
let activeId = readActiveId();
let models = [];
let status = null;
let generation = null;
let probe = null;
let probeResult = null;
let loadEpoch = 0;
let attachmentEpoch = 0;
let pageLoaded = false;
const attachmentCache = new Map();
let initialized = false;
let pendingAttachments = [];
let openConversationMenu = null;
let pendingDeleteConversationId = null;
let historyOpen = false;
let settingsVisible = readSettingsVisibility();
let historyQuery = "";
let composerConversationId = null;
let renderedConversationId = null;
let followOutput = true;
let smoothScrollPending = false;
let scrollActivityTimer = null;
const readingPositions = new Map();

const TEXT_FILE_EXTENSIONS = new Set([
  "txt", "md", "markdown", "json", "jsonl", "csv", "tsv", "xml", "yaml", "yml",
  "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "html", "css", "scss", "sql",
  "sh", "bash", "toml", "ini", "cfg", "log", "java", "go", "rs", "c", "h",
  "cpp", "hpp",
]);

function uid() {
  return globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readConversations() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? localStorage.getItem(LEGACY_STORAGE_KEY) ?? "[]");
    if (!Array.isArray(value)) return [];
    return value.filter((item) => item && typeof item.id === "string" && Array.isArray(item.messages)).map((item) => ({
      ...item, draft: { text: typeof item.draft?.text === "string" ? item.draft.text : "", attachments: Array.isArray(item.draft?.attachments) ? item.draft.attachments : [] }, settings: { model: item.model || "", api_format: "chat", system_prompt: "", temperature: null, max_tokens: null, ...item.settings },
    }));
  } catch {
    return [];
  }
}

function readActiveId() {
  try { return localStorage.getItem(ACTIVE_KEY) || localStorage.getItem(LEGACY_ACTIVE_KEY) || conversations[0]?.id || null; }
  catch { return conversations[0]?.id || null; }
}

function readSettingsVisibility() {
  try { return localStorage.getItem(SETTINGS_VISIBLE_KEY) !== "false"; }
  catch { return true; }
}

function setSettingsVisibility(visible, restoreFocus = false) {
  settingsVisible = visible;
  try { localStorage.setItem(SETTINGS_VISIBLE_KEY, String(visible)); } catch {}
  renderDrawers();
  if (restoreFocus) document.querySelector('[data-agent-action="details"]')?.focus();
}

function saveConversations() {
  conversations = conversations
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
    .slice(0, 50);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
    if (activeId) localStorage.setItem(ACTIVE_KEY, activeId);
  } catch {
    toast(t("chatStorageFailed"), "error");
  }
}

function activeConversation() {
  return conversations.find((conversation) => conversation.id === activeId) || null;
}

function createConversation(inheritedSettings = null) {
  const now = new Date().toISOString();
  const conversation = {
    id: uid(),
    title: t("newConversation"),
    model: models[0] || "",
    settings: inheritedSettings ? { ...inheritedSettings } : { model: models[0] || "", api_format: "chat", system_prompt: "", temperature: null, max_tokens: null },
    draft: { text: "", attachments: [] },
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
  conversation.model = conversation.settings.model;
  conversations.unshift(conversation);
  activeId = conversation.id;
  saveConversations();
  return conversation;
}

function ensureConversation() {
  return activeConversation() || createConversation();
}

function conversationTitle(conversation) {
  return conversation?.messages.length ? conversation.title || t("newConversation") : t("newConversation");
}

function renderCurrentTitle() {
  const title = document.getElementById("chatCurrentTitle");
  if (title) title.textContent = t("chatRecent");
  document.querySelector('[data-agent-action="history"]')?.setAttribute("title", t("chatRecent"));
}

function persistDraft() {
  const conversation = activeConversation();
  if (!conversation || composerConversationId !== conversation.id) return;
  const draft = { text: document.getElementById("agentPrompt")?.value || "", attachments: pendingAttachments.map(attachmentMetadata) };
  if (JSON.stringify(conversation.draft) !== JSON.stringify(draft)) conversation.updatedAt = new Date().toISOString();
  conversation.draft = draft;
  saveConversations();
}

async function restoreComposer() {
  const conversation = ensureConversation();
  const epoch = ++attachmentEpoch;
  composerConversationId = conversation.id;
  pendingAttachments = (conversation.draft?.attachments || []).map((attachment) => ({ ...attachment }));
  const textarea = document.getElementById("agentPrompt");
  if (textarea) {
    textarea.value = conversation.draft?.text || "";
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }
  renderPendingAttachments();
  const restored = await Promise.all(pendingAttachments.map(async (metadata) => {
    try { return await attachmentPayload(metadata) || { ...metadata, unavailable: true }; }
    catch { return { ...metadata, unavailable: true }; }
  }));
  if (epoch !== attachmentEpoch || activeId !== conversation.id || !pageLoaded) return;
  const byId = new Map(restored.map((attachment) => [attachment.id, attachment]));
  pendingAttachments = pendingAttachments.map((attachment) => byId.get(attachment.id) || attachment);
  renderPendingAttachments();
  if (followOutput) scrollToBottom(); else updateScrollControl();
}

function nearBottom(element) {
  return !element || element.scrollHeight - element.clientHeight - element.scrollTop <= 96;
}

function rememberScroll() {
  const element = document.getElementById("agentMessages");
  if (element && renderedConversationId) readingPositions.set(renderedConversationId, { top: element.scrollTop, follow: followOutput });
}

function updateScrollControl() {
  const element = document.getElementById("agentMessages");
  document.getElementById("chatScrollBottom")?.classList.toggle("hidden", nearBottom(element));
  rememberScroll();
}

function onMessageScroll() {
  if (!pageLoaded) return;
  const element = document.getElementById("agentMessages");
  if (nearBottom(element)) smoothScrollPending = false;
  followOutput = smoothScrollPending || nearBottom(element);
  updateScrollControl();
  element?.classList.add("is-scrolling");
  clearTimeout(scrollActivityTimer);
  scrollActivityTimer = setTimeout(() => element?.classList.remove("is-scrolling"), 700);
}

function scrollToBottom(smooth = false) {
  const element = document.getElementById("agentMessages");
  if (!element) return;
  followOutput = true;
  const animate = smooth && !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  smoothScrollPending = Boolean(animate);
  if (animate && element.scrollTo) element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
  else element.scrollTop = element.scrollHeight;
  updateScrollControl();
}

async function changeConversation(id) {
  persistDraft();
  rememberScroll();
  stopGenerating();
  invalidateProbe();
  releasePendingAttachments();
  activeId = id;
  closeForConversationChange();
  closeAttachmentMenu();
  historyQuery = "";
  const search = document.getElementById("chatHistorySearch");
  if (search) search.value = "";
  saveConversations();
  const restoring = restoreComposer();
  renderThreads(); renderMessages(); renderDetails(); setGenerating(false);
  document.dispatchEvent(new CustomEvent("chatconversationopen"));
  await restoring;
}

async function newConversation() {
  persistDraft();
  const settings = { ...ensureConversation().settings };
  const reusable = conversations.find((conversation) => !conversation.messages.length && !conversation.draft?.text && !conversation.draft?.attachments?.length);
  if (reusable) {
    reusable.settings = settings;
    reusable.model = settings.model;
    await changeConversation(reusable.id);
  } else {
    const previous = activeId;
    const created = createConversation(settings);
    activeId = previous;
    await changeConversation(created.id);
  }
  document.getElementById("agentPrompt")?.focus();
}

function renderShell() {
  const container = document.getElementById("chat");
  if (!container) return;
  container.classList.add("chat-panel");
  container.innerHTML = `
    <div class="agent-chat-shell">
      <section class="agent-chat-main">
        <div class="agent-left-controls">
          <button class="agent-history-trigger" type="button" data-agent-action="history" aria-label="${t("chatRecent")}" aria-expanded="false" aria-controls="agentHistoryDrawer">
            <span id="chatCurrentTitle"></span><i data-lucide="chevron-down"></i>
          </button>
          <button class="agent-edge-button agent-new-conversation" type="button" data-agent-action="new" aria-label="${t("newConversation")}" title="${t("newConversation")}">
            <i data-lucide="plus"></i>
          </button>
        </div>
        <button class="agent-edge-button agent-details-trigger" type="button" data-agent-action="details" aria-label="${t("conversationSettings")}" aria-expanded="false" aria-controls="agentDetailsDrawer">
          <i data-lucide="panel-right"></i>
        </button>
        <div class="agent-message-area">
          <div class="agent-messages" id="agentMessages" aria-live="polite" tabindex="0"></div>
          <button class="chat-scroll-bottom hidden" id="chatScrollBottom" type="button" data-agent-action="scroll-bottom" aria-label="${t("chatBackToBottom")}" title="${t("chatBackToBottom")}"><i data-lucide="arrow-down"></i><span id="chatBackToBottomLabel">${t("chatBackToBottom")}</span></button>
        </div>

        <div class="agent-composer-wrap">
          <div class="agent-attachment-strip hidden" id="agentAttachmentStrip"></div>
          <div class="agent-composer">
            <textarea id="agentPrompt" rows="1" maxlength="100000" placeholder="${t("chatPlaceholder")}" aria-label="${t("chatPlaceholder")}" data-i18n-placeholder="chatPlaceholder"></textarea>
            <div class="agent-composer-tools">
            <div class="agent-add-wrap">
              <button class="agent-add" type="button" data-agent-action="attachment-menu" aria-label="${t("addAttachment")}" aria-expanded="false">
                <i data-lucide="plus"></i>
              </button>
              <div class="agent-add-menu hidden" id="agentAddMenu">
                <button type="button" data-agent-action="add-image">
                  <span class="agent-add-menu-icon image"><i data-lucide="image-plus"></i></span>
                  <span><strong id="chatAddPhotoLabel">${t("addPhoto")}</strong><small id="chatAddPhotoHint">${t("addPhotoHint")}</small></span>
                </button>
                <button type="button" data-agent-action="add-file">
                  <span class="agent-add-menu-icon file"><i data-lucide="file-plus-2"></i></span>
                  <span><strong id="chatAddFileLabel">${t("addFile")}</strong><small id="chatAddFileHint">${t("addFileHint")}</small></span>
                </button>
              </div>
              <input id="agentImageInput" class="hidden" type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
              <input id="agentFileInput" class="hidden" type="file" accept="text/*,.md,.markdown,.json,.jsonl,.csv,.tsv,.xml,.yaml,.yml,.py,.js,.mjs,.cjs,.ts,.tsx,.jsx,.html,.css,.scss,.sql,.sh,.toml,.ini,.cfg,.log,.java,.go,.rs,.c,.h,.cpp,.hpp,.pdf,.docx" multiple>
            </div>
            <button class="agent-send" id="agentSend" type="button" aria-label="${t("sendMessage")}">
              <i data-lucide="arrow-up"></i>
            </button>
            <button class="agent-stop hidden" id="agentStop" type="button" aria-label="${t("stopGenerating")}">
              <i data-lucide="square"></i>
            </button>
            </div>
          </div>
          <p class="agent-composer-hint"><span id="agentRuntimeStatus" role="status">${t("agentReady")}</span><span id="chatComposerHint" data-i18n="chatHint">${t("chatHint")}</span></p>
        </div>
      </section>
      <button class="agent-drawer-backdrop hidden" id="agentDrawerBackdrop" type="button" data-agent-action="close-drawer" aria-label="${t("close")}"></button>
      <aside class="agent-drawer agent-history-drawer hidden" id="agentHistoryDrawer" aria-labelledby="agentHistoryTitle">
        <div class="agent-drawer-head">
          <strong id="agentHistoryTitle" data-i18n="recentConversations">${t("recentConversations")}</strong>
          <button class="agent-edge-button" type="button" data-agent-action="close-drawer" aria-label="${t("close")}"><i data-lucide="x"></i></button>
        </div>
        <label class="chat-history-search"><i data-lucide="search"></i><input id="chatHistorySearch" type="search" autocomplete="off" placeholder="${t("chatSearchHistory")}" aria-label="${t("chatSearchHistory")}"></label>
        <div class="agent-thread-list" id="agentThreadList"></div>
      </aside>
      <aside class="agent-drawer agent-details-drawer hidden" id="agentDetailsDrawer" aria-labelledby="agentDetailsTitle">
        <div class="agent-drawer-head">
          <strong id="agentDetailsTitle" data-i18n="conversationSettings">${t("conversationSettings")}</strong>
          <button class="agent-edge-button" type="button" data-agent-action="close-drawer" aria-label="${t("close")}"><i data-lucide="x"></i></button>
        </div>
        <div class="agent-details-content" id="agentDetailsContent"></div>
      </aside>
    </div>`;

  container.querySelector("#agentPrompt")?.addEventListener("input", (event) => { resizePrompt(event); persistDraft(); });
  container.querySelector("#chatHistorySearch")?.addEventListener("input", (event) => { historyQuery = event.target.value; openConversationMenu = null; renderThreads(); });
  const messages = container.querySelector("#agentMessages");
  messages?.addEventListener("scroll", onMessageScroll, { passive: true });
  for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) messages?.addEventListener(event, () => { smoothScrollPending = false; });
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => {
    if (!pageLoaded) return;
    if (followOutput) scrollToBottom(); else updateScrollControl();
  }).observe(messages);
  container.querySelector("#agentPrompt")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  container.querySelector("#agentSend")?.addEventListener("click", sendMessage);
  container.querySelector("#agentStop")?.addEventListener("click", stopGenerating);
  container.querySelector("#agentImageInput")?.addEventListener("change", handleImageFiles);
  container.querySelector("#agentFileInput")?.addEventListener("change", handleTextFiles);
  if (!initialized) {
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      closeAttachmentMenu();
      closeHistory(true);
      closeDeleteConfirmation();
    });
  }
  initialized = true;
  historyOpen = false;
  renderDrawers();
  renderPendingAttachments();
  refreshIcons(container);
}

function refreshShellLabels() {
  const container = document.getElementById("chat");
  if (!container) return;
  const labels = {
    '#agentPrompt': "chatPlaceholder", '#agentSend': "sendMessage", '#agentStop': "stopGenerating",
    '[data-agent-action="new"]': "newConversation", '[data-agent-action="history"]': "chatRecent",
    '[data-agent-action="details"]': "conversationSettings", '[data-agent-action="attachment-menu"]': "addAttachment",
    '[data-agent-action="close-drawer"]': "close",
    '#chatScrollBottom': "chatBackToBottom", '#chatHistorySearch': "chatSearchHistory",
  };
  for (const [selector, key] of Object.entries(labels)) {
    container.querySelectorAll(selector).forEach((element) => element.setAttribute("aria-label", t(key)));
  }
  container.querySelector('[data-agent-action="new"]')?.setAttribute("title", t("newConversation"));
  document.getElementById("agentPrompt")?.setAttribute("placeholder", t("chatPlaceholder"));
  document.getElementById("chatHistorySearch")?.setAttribute("placeholder", t("chatSearchHistory"));
  document.getElementById("chatScrollBottom")?.setAttribute("title", t("chatBackToBottom"));
  const text = {
    chatAddPhotoLabel: "addPhoto", chatAddPhotoHint: "addPhotoHint", chatAddFileLabel: "addFile", chatAddFileHint: "addFileHint",
    chatBackToBottomLabel: "chatBackToBottom",
    agentHistoryTitle: "recentConversations", agentDetailsTitle: "conversationSettings", chatComposerHint: "chatHint",
  };
  for (const [id, key] of Object.entries(text)) {
    const element = document.getElementById(id);
    if (element) element.textContent = t(key);
  }
}

function closeAttachmentMenu() {
  document.getElementById("agentAddMenu")?.classList.add("hidden");
  document.querySelector('[data-agent-action="attachment-menu"]')?.setAttribute("aria-expanded", "false");
}

function renderDrawers() {
  document.getElementById("agentHistoryDrawer")?.classList.toggle("hidden", !historyOpen);
  document.getElementById("agentDetailsDrawer")?.classList.toggle("hidden", !settingsVisible);
  document.getElementById("agentDrawerBackdrop")?.classList.add("hidden");
  for (const action of ["history", "details"]) {
    const trigger = document.querySelector(`[data-agent-action="${action}"]`);
    trigger?.setAttribute("aria-expanded", String(action === "history" ? historyOpen : settingsVisible));
    trigger?.setAttribute("aria-label", t(action === "history" ? "chatRecent" : "conversationSettings"));
  }
}

function closeHistory(restoreFocus = false) {
  const wasOpen = historyOpen;
  historyOpen = false;
  openConversationMenu = null;
  renderDrawers();
  renderThreads();
  if (restoreFocus && wasOpen) document.querySelector('[data-agent-action="history"]')?.focus();
}

function closeForConversationChange() { closeHistory(); }

function toggleDrawer(drawer) {
  closeAttachmentMenu();
  if (drawer === "details") {
    setSettingsVisibility(!settingsVisible, settingsVisible);
    if (settingsVisible) {
      renderDetails();
      document.querySelector('#agentDetailsDrawer [data-agent-action="close-drawer"]')?.focus();
    }
    return;
  }
  if (historyOpen) {
    closeHistory(true);
    return;
  }
  historyOpen = true;
  openConversationMenu = null;
  renderThreads();
  renderDrawers();
  document.getElementById("chatHistorySearch")?.focus();
}

function renderDetails() {
  const container = document.getElementById("agentDetailsContent");
  const conversation = activeConversation();
  if (!container || !conversation) return;
  const settings = conversation.settings;
  const attachments = conversation.messages.flatMap((message) => message.attachments || []).concat(pendingAttachments);
  const updatedAt = new Date(conversation.updatedAt);
  const updated = Number.isNaN(updatedAt.getTime()) ? "—" : updatedAt.toLocaleString(document.documentElement.lang || undefined);
  container.innerHTML = `
    <form class="chat-settings" id="chatSettings" novalidate>
      <label for="agentModel">${t("model")}</label>
      <select id="agentModel" aria-label="${t("model")}"></select>
      <label for="chatApiFormat">${t("chatApiFormat")}</label>
      <select id="chatApiFormat"><option value="chat">Chat</option><option value="responses">Responses</option></select>
      <label for="chatSystemPrompt">System Prompt</label>
      <textarea id="chatSystemPrompt" rows="5" maxlength="100000" placeholder="${t("chatSystemPromptHint")}">${escapeHtml(settings.system_prompt)}</textarea>
      <div class="chat-setting-pair">
        <div><label for="chatTemperature">Temperature</label><input id="chatTemperature" type="number" min="0" max="2" step="0.1" value="${escapeHtml(settings.temperature ?? "")}" placeholder="${t("chatModelDefault")}" aria-describedby="chatTemperatureHint"></div>
        <div><label for="chatMaxTokens">Max tokens</label><input id="chatMaxTokens" type="number" min="1" max="1000000" step="1" value="${escapeHtml(settings.max_tokens ?? "")}" placeholder="${t("chatModelDefault")}" aria-describedby="chatMaxTokensHint"></div>
      </div>
      <p class="chat-setting-hint" id="chatTemperatureHint">${t("chatTemperatureHint")}</p>
      <p class="chat-setting-hint" id="chatMaxTokensHint">${t("chatMaxTokensHint")}</p>
      <p class="chat-setting-error hidden" id="chatSettingsError" role="alert"></p>
      <button class="btn-secondary chat-test-button" id="chatTestConnection" type="button"><i data-lucide="plug-zap"></i><span>${t("chatTestConnection")}</span></button>
      <p class="chat-probe-result" id="chatProbeResult" role="status"></p>
      <p class="chat-setting-hint">${t("chatTestHint")}</p>
      <p class="chat-setting-hint">${t("chatDocumentHint")}</p>
    </form>
    <dl class="agent-details-list">
      <dt>${t("conversationTitle")}</dt><dd>${escapeHtml(conversationTitle(conversation))}</dd>
      <dt>${t("conversationUpdated")}</dt><dd>${escapeHtml(updated)}</dd>
      <dt>${t("conversationAttachments")}</dt><dd>${attachments.length
        ? `<ul>${attachments.map((attachment) => `<li>${escapeHtml(attachment.name)} <small>${formatFileSize(attachment.size || 0)}</small>${(attachment.unavailable || (!attachment.id && attachment.content === undefined && !attachment.dataUrl)) ? `<small class="chat-missing-attachment">${t("chatAttachmentUnavailable")}</small>` : ""}</li>`).join("")}</ul>`
        : t("noConversationAttachments")}</dd>
    </dl>`;
  document.getElementById("chatApiFormat").value = settings.api_format;
  renderModels();
  const fields = { agentModel: "model", chatApiFormat: "api_format", chatSystemPrompt: "system_prompt", chatTemperature: "temperature", chatMaxTokens: "max_tokens" };
  for (const [id, key] of Object.entries(fields)) {
    const field = document.getElementById(id);
    field?.addEventListener(id === "agentModel" || id === "chatApiFormat" ? "change" : "input", (event) => {
      settings[key] = ["temperature", "max_tokens"].includes(key) ? (event.target.value === "" ? null : event.target.value) : event.target.value;
      conversation.model = settings.model;
      conversation.updatedAt = new Date().toISOString();
      invalidateProbe();
      validateSettings(false);
      saveConversations();
      renderThreads();
    });
  }
  document.getElementById("chatTestConnection")?.addEventListener("click", testConnection);
  document.getElementById("chatSettings")?.addEventListener("submit", (event) => event.preventDefault());
  renderProbe();
  refreshIcons(container);
}

function validateSettings(showToast = true) {
  const source = ensureConversation().settings;
  const temperature = source.temperature === null || source.temperature === "" ? null : Number(source.temperature);
  const maxTokens = source.max_tokens === null || source.max_tokens === "" ? null : Number(source.max_tokens);
  let error = "";
  if (!source.model || !models.includes(source.model)) error = t("selectModelFirst");
  else if (temperature !== null && (!Number.isFinite(temperature) || temperature < 0 || temperature > 2)) error = t("chatTemperatureInvalid");
  else if (maxTokens !== null && (!Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > 1_000_000)) error = t("chatMaxTokensInvalid");
  const element = document.getElementById("chatSettingsError");
  if (element) { element.textContent = error; element.classList.toggle("hidden", !error); }
  if (error) { if (showToast) toast(error, "warning"); return null; }
  return { model: source.model, api_format: source.api_format === "responses" ? "responses" : "chat", system_prompt: source.system_prompt || "", temperature, max_tokens: maxTokens };
}

function invalidateProbe() {
  probe?.controller.abort();
  probe = null;
  probeResult = null;
  renderProbe();
}

function renderProbe() {
  const button = document.getElementById("chatTestConnection");
  if (button) button.disabled = Boolean(probe) || !models.length || status?.token_available === false;
  const result = document.getElementById("chatProbeResult");
  if (!result) return;
  result.textContent = probe ? t("chatTesting") : probeResult?.text || "";
  result.classList.toggle("error", probeResult?.error === true);
  result.classList.toggle("success", probeResult?.error === false);
}

async function testConnection() {
  const settings = validateSettings();
  if (!settings || probe) return;
  const operation = { controller: new AbortController(), conversationId: activeId };
  probe = operation;
  probeResult = null;
  renderProbe();
  try {
    const result = await api("/api/admin/chat/test", { method: "POST", body: JSON.stringify(settings), signal: operation.controller.signal });
    if (probe !== operation || !pageLoaded || activeId !== operation.conversationId) return;
    if (!result?.success) throw new Error(result?.message || t("chatTestFailed"));
    probeResult = { text: `${t("chatTestSucceeded")} · ${Math.round(result.latency_ms)} ms`, error: false };
  } catch (error) {
    if (probe !== operation || error.name === "AbortError") return;
    probeResult = { text: `${t("chatTestFailed")}: ${error.message}`, error: true };
  } finally {
    if (probe === operation) { probe = null; renderProbe(); }
  }
}

function resizePrompt(event) {
  const textarea = event.currentTarget;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  if (followOutput) scrollToBottom(); else updateScrollControl();
}

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function fileExtension(name) {
  return String(name).split(".").pop()?.toLowerCase() || "";
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(t("fileReadFailed")));
    reader.readAsDataURL(file);
  });
}

async function handleImageFiles(event) { return handleFiles(event, true); }
async function handleTextFiles(event) { return handleFiles(event, false); }

let attachmentReadQueue = Promise.resolve();
function handleFiles(event, images) {
  const snapshot = { currentTarget: { files: [...(event.currentTarget.files || [])], value: "" } };
  event.currentTarget.value = "";
  const epoch = attachmentEpoch;
  const next = attachmentReadQueue.then(() => epoch === attachmentEpoch ? readFiles(snapshot, images) : undefined);
  attachmentReadQueue = next.catch(() => {});
  return next;
}

async function readFiles(event, images) {
  const files = [...(event.currentTarget.files || [])];
  event.currentTarget.value = "";
  closeAttachmentMenu();
  const epoch = attachmentEpoch;
  const conversationId = activeId;
  for (const file of files) {
    if (epoch !== attachmentEpoch || conversationId !== activeId || !pageLoaded) break;
    const extension = fileExtension(file.name);
    const isDocument = !images && ["pdf", "docx"].includes(extension);
    const isText = !images && (file.type.startsWith("text/")
      || ["application/json", "application/xml", "application/yaml"].includes(file.type)
      || TEXT_FILE_EXTENSIONS.has(extension));
    if (images ? !["image/jpeg", "image/png", "image/gif", "image/webp"].includes(file.type) : !isDocument && !isText) {
      toast(`${file.name}: ${t("unsupportedFileType")}`, "warning");
      continue;
    }
    if (file.size > (isText ? 500_000 : 5_000_000)) {
      toast(`${file.name}: ${t(isText ? "fileTooLarge" : images ? "imageTooLarge" : "documentTooLarge")}`, "warning");
      continue;
    }
    try {
      const mimeType = images ? file.type : isDocument
        ? (extension === "pdf" ? "application/pdf" : "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        : file.type || "text/plain";
      const attachment = { id: uid(), kind: images ? "image" : "file", name: file.name, mimeType, size: file.size };
      if (isText) {
        attachment.content = await file.text();
        attachment.size = new TextEncoder().encode(attachment.content).byteLength;
        if (attachment.size > 500_000) { toast(`${file.name}: ${t("fileTooLarge")}`, "warning"); continue; }
      }
      else attachment.dataUrl = (await readAsDataUrl(file)).replace(/^data:[^;]*;/, `data:${mimeType};`);
      if (epoch !== attachmentEpoch || conversationId !== activeId || !pageLoaded) break;
      if (pendingAttachments.length >= 6) { toast(t("tooManyAttachments"), "warning"); break; }
      if (pendingAttachments.reduce((total, item) => total + item.size, 0) + attachment.size > 10_000_000) {
        toast(t("attachmentsTooLarge"), "warning"); break;
      }
      await saveAttachment(attachment);
      if (epoch !== attachmentEpoch || conversationId !== activeId || !pageLoaded) {
        await deleteAttachments([attachment.id]); break;
      }
      attachmentCache.set(attachment.id, attachment);
      pendingAttachments.push(attachment);
      persistDraft();
      renderPendingAttachments();
    } catch (error) {
      toast(`${file.name}: ${t("chatAttachmentStorageFailed")} ${error.message}`, "error");
    }
  }
}

function attachmentMetadata({ id, kind, name, mimeType, size }) {
  return { id, kind, name, mimeType, size };
}

function forgetAttachments(attachments) {
  const ids = attachments.map((attachment) => attachment.id).filter(Boolean);
  for (const id of ids) attachmentCache.delete(id);
  deleteAttachments(ids).catch(() => {});
}

function releasePendingAttachments() {
  attachmentEpoch++;
  attachmentCache.clear();
  pendingAttachments = [];
}

async function attachmentPayload(attachment) {
  if (attachment.dataUrl !== undefined || attachment.content !== undefined) return attachment;
  if (!attachment.id) return null;
  if (attachmentCache.has(attachment.id)) return attachmentCache.get(attachment.id);
  const stored = await getAttachment(attachment.id);
  if (stored) attachmentCache.set(attachment.id, stored);
  return stored || null;
}

async function payloadMessages(messages) {
  const result = [];
  let missing = false;
  for (const message of messages) {
    if (!["user", "assistant"].includes(message.role) || message.status === "error") continue;
    const attachments = [];
    const unavailable = [];
    for (const metadata of message.attachments || []) {
      const attachment = await attachmentPayload(metadata);
      if (!attachment) { metadata.unavailable = true; unavailable.push(metadata.name); missing = true; continue; }
      delete metadata.unavailable;
      attachments.push({ kind: attachment.kind, name: attachment.name, mime_type: attachment.mimeType, size: attachment.size,
        data_url: attachment.dataUrl, content: attachment.content });
    }
    let content = message.content || "";
    if (unavailable.length) content += `\n[Attachment content unavailable; not included: ${unavailable.join(", ")}]`;
    if (content || attachments.length) result.push({ role: message.role, content, attachments });
  }
  if (missing) toast(t("chatLegacyAttachmentHint"), "warning");
  return result;
}

function renderPendingAttachments() {
  const strip = document.getElementById("agentAttachmentStrip");
  if (!strip) return;
  strip.classList.toggle("hidden", !pendingAttachments.length);
  strip.innerHTML = pendingAttachments.map((attachment) => `
    <div class="agent-pending-attachment">
      ${attachment.kind === "image" && attachment.dataUrl
        ? `<img src="${attachment.dataUrl}" alt="">`
        : `<span class="agent-file-icon"><i data-lucide="file-text"></i></span>`}
      <span class="agent-attachment-name">
        <strong>${escapeHtml(attachment.name)}</strong>
        <small>${attachment.unavailable ? t("chatAttachmentUnavailable") : formatFileSize(attachment.size)}</small>
      </span>
      <button type="button" data-agent-action="remove-attachment" data-attachment-id="${escapeHtml(attachment.id)}" aria-label="${t("removeAttachment")}">
        <i data-lucide="x"></i>
      </button>
    </div>
  `).join("");
  refreshIcons(strip);
  renderDetails();
}

function renderThreads() {
  const list = document.getElementById("agentThreadList");
  if (!list) return;
  renderCurrentTitle();
  const query = historyQuery.trim().toLocaleLowerCase();
  const matches = conversations.filter((conversation) => !query || `${conversationTitle(conversation)} ${conversation.model || ""}`.toLocaleLowerCase().includes(query));
  list.innerHTML = matches.map((conversation) => {
    const menu = openConversationMenu?.id === conversation.id
      ? openConversationMenu
      : null;
    const menuStyle = menu
      ? `style="top: ${menu.top}px; left: ${menu.left}px"`
      : "";
    return `
    <div class="agent-thread ${conversation.id === activeId ? "active" : ""}" data-conversation-id="${escapeHtml(conversation.id)}">
      <button type="button" class="agent-thread-open" data-agent-action="open" data-conversation-id="${escapeHtml(conversation.id)}">
        <i data-lucide="message-square"></i>
        <span>
          <strong>${escapeHtml(conversationTitle(conversation))}</strong>
          <small>${escapeHtml(conversation.model || t("selectModel"))}</small>
        </span>
      </button>
      <div class="agent-thread-menu">
        <button type="button" class="agent-thread-more" data-agent-action="conversation-menu" data-conversation-id="${escapeHtml(conversation.id)}" aria-label="${t("conversations")}" aria-expanded="${Boolean(menu)}">
          <i data-lucide="ellipsis"></i>
        </button>
        <div class="agent-thread-menu-popover ${menu ? "" : "hidden"} ${menu?.opensUp ? "opens-up" : ""}" ${menuStyle} role="menu">
          <button type="button" data-agent-action="export" data-conversation-id="${escapeHtml(conversation.id)}" role="menuitem">
            <i data-lucide="download"></i><span>${t("exportConversation")}</span>
          </button>
          <button type="button" class="danger" data-agent-action="delete" data-conversation-id="${escapeHtml(conversation.id)}" role="menuitem">
            <i data-lucide="trash-2"></i><span>${t("deleteConversation")}</span>
          </button>
        </div>
      </div>
    </div>
  `;
  }).join("") || `<p class="chat-history-empty">${t("chatNoHistoryMatches")}</p>`;
  refreshIcons(list);
}

function showDeleteConfirmation(conversation) {
  const dialog = document.getElementById("chatDeleteModal");
  if (!dialog) return;
  pendingDeleteConversationId = conversation.id;
  const name = document.getElementById("chatDeleteConversationName");
  if (name) name.textContent = conversation.title || t("newConversation");
  dialog.classList.remove("hidden");
}

function closeDeleteConfirmation() {
  pendingDeleteConversationId = null;
  document.getElementById("chatDeleteModal")?.classList.add("hidden");
}

async function deleteConversation(conversation) {
  const deletingActive = activeId === conversation.id;
  if (deletingActive) {
    stopGenerating(); invalidateProbe(); releasePendingAttachments();
  }
  forgetAttachments([...conversation.messages.flatMap((message) => message.attachments || []), ...(conversation.draft?.attachments || [])]);
  readingPositions.delete(conversation.id);
  closeForConversationChange();
  conversations = conversations.filter((item) => item.id !== conversation.id);
  if (deletingActive) {
    activeId = conversations[0]?.id || null;
    if (!activeId) createConversation(conversation.settings);
    const restoring = restoreComposer();
    renderMessages();
    await restoring;
  }
  saveConversations();
  renderThreads(); renderDetails(); renderPendingAttachments();
}

async function exportConversation(conversation) {
  const draftAttachments = await Promise.all((conversation.draft?.attachments || []).map(async (attachment) => await attachmentPayload(attachment) || { ...attachment, unavailable: true }));
  const messages = await Promise.all(conversation.messages.map(async (message) => ({ ...message, attachments: await Promise.all((message.attachments || []).map(async (attachment) => await attachmentPayload(attachment) || { ...attachment, unavailable: true })) })));
  const payload = {
    schema_version: 1,
    exported_at: new Date().toISOString(),
    conversation: { ...conversation, messages, draft: { ...conversation.draft, attachments: draftAttachments } },
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  const safeId = conversation.id.replace(/[^a-zA-Z0-9_-]/g, "_");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = `rotor-conversation-${safeId}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url));
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");
}

function renderMarkdown(value) {
  const source = String(value || "").replaceAll("\r\n", "\n");
  const codeBlocks = [];
  const tokenized = source.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (_match, language, code) => {
    const index = codeBlocks.length;
    codeBlocks.push({ language: language.trim(), code: code.replace(/\n$/, "") });
    return `\n@@ROTOR_CODE_${index}@@\n`;
  });
  const lines = tokenized.split("\n");
  const output = [];
  let listType = null;

  const closeList = () => {
    if (!listType) return;
    output.push(`</${listType}>`);
    listType = null;
  };

  for (const line of lines) {
    const codeMatch = line.match(/^@@ROTOR_CODE_(\d+)@@$/);
    if (codeMatch && codeBlocks[Number(codeMatch[1])]) {
      closeList();
      const block = codeBlocks[Number(codeMatch[1])];
      const language = block.language
        ? `<span class="agent-code-language">${escapeHtml(block.language)}</span>`
        : "";
      output.push(`<pre>${language}<code>${escapeHtml(block.code)}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const nextListType = unordered ? "ul" : "ol";
      if (listType !== nextListType) {
        closeList();
        listType = nextListType;
        output.push(`<${listType}>`);
      }
      output.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
      continue;
    }

    closeList();
    if (!line.trim()) continue;
    if (line.startsWith("> ")) {
      output.push(`<blockquote>${inlineMarkdown(line.slice(2))}</blockquote>`);
    } else if (/^---+$/.test(line.trim())) {
      output.push("<hr>");
    } else {
      output.push(`<p>${inlineMarkdown(line)}</p>`);
    }
  }
  closeList();

  // An unfinished fenced block is common while streaming. Keep it readable
  // until the closing fence arrives instead of exposing raw protocol text.
  if (source.includes("```") && (source.match(/```/g) || []).length % 2 === 1) {
    const lastFence = source.lastIndexOf("```");
    const stable = source.slice(0, lastFence);
    const unfinished = source.slice(lastFence + 3).replace(/^[^\n]*\n?/, "");
    return `${renderMarkdown(stable)}<pre class="streaming-code"><code>${escapeHtml(unfinished)}</code></pre>`;
  }
  return output.join("");
}

function messageMarkup(message) {
  const role = message.role === "user" ? "user" : "assistant";
  const content = message.content
    ? (role === "assistant" ? renderMarkdown(message.content) : escapeHtml(message.content))
    : role === "assistant" && !message.status ? `<span class="agent-typing"><i></i><i></i><i></i></span>` : "";
  const attachments = (message.attachments || []).length ? `
    <div class="agent-sent-attachments">
      ${message.attachments.map((attachment) => `
        <span>
          <i data-lucide="${attachment.kind === "image" ? "image" : "file-text"}"></i>
          ${escapeHtml(attachment.name)}${(attachment.unavailable || (!attachment.id && attachment.content === undefined && !attachment.dataUrl)) ? ` · ${t("chatAttachmentUnavailable")}` : ""}
        </span>
      `).join("")}
    </div>` : "";
  return `
    <article class="agent-message ${role}" data-message-id="${escapeHtml(message.id)}">
      ${role === "assistant" ? `
        <div class="agent-message-meta">
          <span>${escapeHtml(message.model || t("chatAssistant"))}</span>
          ${message.content ? `
          <button type="button" data-agent-action="copy" data-message-id="${escapeHtml(message.id)}" aria-label="${t("copy")}">
            <i data-lucide="copy"></i>
          </button>` : ""}
        </div>` : ""}
      ${attachments}
      <div class="agent-message-content">${content}</div>
      ${message.error ? `<p class="chat-message-error" role="status">${escapeHtml(message.error)}</p>` : ""}
    </article>`;
}

function renderMessages({ forceBottom = false } = {}) {
  const container = document.getElementById("agentMessages");
  if (!container) return;
  const conversation = ensureConversation();
  const previous = renderedConversationId === conversation.id ? { top: container.scrollTop, follow: followOutput && (smoothScrollPending || nearBottom(container)) } : readingPositions.get(conversation.id);
  if (!conversation.messages.length) {
    container.innerHTML = `
      <div class="agent-welcome">
        <div class="agent-welcome-icon"><i data-lucide="sparkles"></i></div>
        <h2>${t("chatWelcome")}</h2>
        <p>${t("chatWelcomeDescription")}</p>
        <div class="agent-suggestions">
          <button type="button" data-agent-action="suggest" data-prompt="${escapeHtml(t("chatSuggestionOne"))}">${t("chatSuggestionOne")}</button>
          <button type="button" data-agent-action="suggest" data-prompt="${escapeHtml(t("chatSuggestionTwo"))}">${t("chatSuggestionTwo")}</button>
          <button type="button" data-agent-action="suggest" data-prompt="${escapeHtml(t("chatSuggestionThree"))}">${t("chatSuggestionThree")}</button>
        </div>
      </div>`;
  } else {
    container.innerHTML = `<div class="agent-message-column">${conversation.messages.map(messageMarkup).join("")}</div>`;
  }
  refreshIcons(container);
  renderedConversationId = conversation.id;
  if (forceBottom || !previous || previous.follow) scrollToBottom();
  else { container.scrollTop = previous.top; followOutput = false; updateScrollControl(); }
}

function renderModels() {
  const select = document.getElementById("agentModel");
  if (!select) return;
  const conversation = ensureConversation();
  const model = conversation.settings.model;
  const available = [...models];
  if (model && !available.includes(model)) available.unshift(model);
  select.innerHTML = available.length
    ? available.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}${models.includes(value) ? "" : ` (${t("chatModelUnavailable")})`}</option>`).join("")
    : `<option value="">${t("noModelsAvailable")}</option>`;
  select.disabled = !models.length;
  select.value = model;
}

function setRuntimeStatus(text, active = false) {
  const element = document.getElementById("agentRuntimeStatus");
  if (!element) return;
  element.textContent = text;
  element.classList.toggle("active", active);
}

function setGenerating(generating) {
  document.getElementById("agentSend")?.classList.toggle("hidden", generating);
  document.getElementById("agentStop")?.classList.toggle("hidden", !generating);
  const ready = status?.token_available === false ? t("chatTokenUnavailable") : t("agentReady");
  setRuntimeStatus(generating ? t("agentThinking") : ready, generating);
}

async function refreshModels(epoch) {
  const response = await api("/api/admin/chat/models");
  if (epoch !== loadEpoch || !pageLoaded) return;
  status = response;
  models = [...new Set(status.models || [])];
  const conversation = ensureConversation();
  if (!conversation.settings.model && models.length) {
    conversation.settings.model = models[0];
    conversation.model = models[0];
    saveConversations();
  }
  renderDetails();
  setGenerating(Boolean(generation));
}

function parseEventBlock(block) {
  const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
  if (!data) return null;
  try { return JSON.parse(data); }
  catch { throw new Error(t("chatInvalidStream")); }
}

async function responseError(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    return Array.isArray(body.detail) ? body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ") : body.detail || body.message || text;
  } catch { return text || `${response.status} ${response.statusText}`; }
}

async function sendMessage() {
  if (generation) return;
  const textarea = document.getElementById("agentPrompt");
  const submittedText = textarea?.value || "";
  const typedPrompt = submittedText.trim();
  if (!typedPrompt && !pendingAttachments.length) return;
  const settings = validateSettings();
  if (!settings) return;
  if (status?.token_available === false) { toast(t("chatTokenUnavailable"), "warning"); return; }
  const conversation = ensureConversation();
  const outboundAttachments = [...pendingAttachments];
  const totalSize = conversation.messages.flatMap((message) => message.attachments || []).concat(outboundAttachments).reduce((total, attachment) => total + (attachment.size || 0), 0);
  if (totalSize > 20_000_000) { toast(t("chatHistoryTooLarge"), "warning"); return; }
  const userMessage = { id: uid(), role: "user", content: typedPrompt || t("defaultAttachmentPrompt"), attachments: outboundAttachments.map(attachmentMetadata) };
  const assistantMessage = { id: uid(), role: "assistant", content: "", model: settings.model };
  const operation = { controller: new AbortController(), conversation, assistantMessage, inserted: false };
  generation = operation;
  setGenerating(true);
  try {
    const messages = await payloadMessages([...conversation.messages, userMessage]);
    if (generation !== operation || operation.controller.signal.aborted) return;
    conversation.messages.push(userMessage, assistantMessage);
    operation.inserted = true;
    if (conversation.messages.filter((message) => message.role === "user").length === 1) {
      conversation.title = userMessage.content.length > 32 ? `${userMessage.content.slice(0, 32)}…` : userMessage.content;
    }
    conversation.updatedAt = new Date().toISOString();
    if (textarea.value === submittedText) {
      textarea.value = "";
      textarea.style.height = "auto";
    }
    pendingAttachments = pendingAttachments.filter((attachment) => !outboundAttachments.some((sent) => sent.id === attachment.id));
    persistDraft();
    renderPendingAttachments();
    saveConversations();
    renderThreads();
    renderMessages({ forceBottom: true });
    renderDetails();
    const response = await fetch("/api/admin/chat/completions", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Conversation-Id": conversation.id, ...adminCsrfHeaders() },
      body: JSON.stringify({ ...settings, conversation_id: conversation.id, messages }),
      signal: operation.controller.signal,
    });
    if (generation !== operation) return;
    handleAdminUnauthorized(response);
    if (!response.ok) throw new Error(await responseError(response));
    if (!response.body) throw new Error(t("streamUnavailable"));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed = false;
    const consume = (block) => {
      const event = parseEventBlock(block);
      if (!event || completed) return;
      if (event.type === "delta") {
        assistantMessage.content += event.content || "";
        if (pageLoaded && activeId === conversation.id && generation === operation) {
          const list = document.getElementById("agentMessages");
          followOutput = followOutput && (smoothScrollPending || nearBottom(list));
          const content = document.querySelector(`[data-message-id="${assistantMessage.id}"] .agent-message-content`);
          if (content) { content.classList.add("streaming"); content.innerHTML = renderMarkdown(assistantMessage.content); }
          if (list && followOutput) scrollToBottom();
          else updateScrollControl();
        }
      } else if (event.type === "done") {
        completed = true;
        assistantMessage.content = event.answer ?? assistantMessage.content;
        assistantMessage.content ||= t("emptyResponse");
        assistantMessage.status = "done";
        if (event.usage) assistantMessage.usage = event.usage;
      } else if (event.type === "error") throw new Error(event.message || t("chatFailed"));
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (generation !== operation || operation.controller.signal.aborted) { await reader.cancel?.(); return; }
        buffer = (buffer + decoder.decode(value || new Uint8Array(), { stream: !done })).replaceAll("\r\n", "\n");
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) consume(block);
        if (done) { if (buffer.trim()) consume(buffer); break; }
      }
      if (!completed) throw new Error(t("chatStreamInterrupted"));
    } finally {
      if (!completed) { try { await reader.cancel?.(); } catch { /* The request may already be aborted. */ } }
      reader.releaseLock?.();
    }
  } catch (error) {
    if (generation !== operation) return;
    if (error.name === "AbortError") {
      assistantMessage.content ||= t("generationStopped");
      assistantMessage.status = "stopped";
    } else {
      operation.controller.abort();
      assistantMessage.error = `${t("chatFailed")}: ${error.message}`;
      assistantMessage.status = "error";
      toast(error.message, "error");
    }
  } finally {
    if (generation === operation) {
      generation = null;
      if (operation.inserted && conversations.includes(conversation)) {
        conversation.updatedAt = new Date().toISOString();
        saveConversations();
      }
      if (pageLoaded && activeId === conversation.id) {
        renderThreads(); renderMessages(); renderDetails(); setGenerating(false);
      }
    }
  }
}

function stopGenerating() {
  const operation = generation;
  if (!operation) return;
  generation = null;
  operation.controller.abort();
  if (operation.inserted && conversations.includes(operation.conversation)) {
    operation.assistantMessage.content ||= t("generationStopped");
    operation.assistantMessage.status = "stopped";
    operation.conversation.updatedAt = new Date().toISOString();
    saveConversations();
  }
  if (pageLoaded && activeId === operation.conversation.id) {
    renderMessages(); renderDetails(); setGenerating(false);
  }
}

export async function load() {
  pageLoaded = true;
  const epoch = ++loadEpoch;
  const existingShell = initialized && document.querySelector("#chat .agent-chat-shell");
  if (!existingShell) renderShell();
  refreshShellLabels();
  ensureConversation();
  const restoring = composerConversationId !== activeId ? restoreComposer() : Promise.resolve();
  saveConversations();
  renderThreads(); renderMessages(); renderDetails(); renderPendingAttachments(); renderDrawers();
  setGenerating(Boolean(generation));
  await restoring;
  try { await refreshModels(epoch); }
  catch (error) {
    if (epoch !== loadEpoch || !pageLoaded) return;
    setRuntimeStatus(t("chatServiceUnavailable"));
    toast(error.message, "error");
  }
}

export function renderHistory() {
  refreshShellLabels();
  renderThreads();
  renderDetails();
  renderDrawers();
}

export function unload() {
  persistDraft();
  rememberScroll();
  pageLoaded = false;
  loadEpoch++;
  releasePendingAttachments();
  composerConversationId = null;
  stopGenerating();
  invalidateProbe();
  closeHistory();
  closeAttachmentMenu();
}

export async function onClick(target) {
  if (target.closest(".nav-item[data-tab]")) return false;
  const button = target.closest("[data-agent-action]");
  const action = button?.dataset.agentAction;
  if (!target.closest(".agent-add-wrap")) closeAttachmentMenu();
  if (historyOpen && action !== "close-drawer" && !target.closest(".agent-history-drawer, .agent-left-controls, #chatDeleteModal")) closeHistory();
  if (!button) {
    if (openConversationMenu && !target.closest(".agent-thread-menu")) {
      openConversationMenu = null;
      renderThreads();
    }
    return false;
  }
  if (action === "history" || action === "details") {
    toggleDrawer(action);
  } else if (action === "close-drawer") {
    if (button.closest("#agentDetailsDrawer")) setSettingsVisibility(false, true);
    else closeHistory(true);
  } else if (action === "new") {
    await newConversation();
  } else if (action === "open") {
    if (conversations.some((conversation) => conversation.id === button.dataset.conversationId)) await changeConversation(button.dataset.conversationId);
  } else if (action === "scroll-bottom") {
    scrollToBottom(true);
  } else if (action === "conversation-menu") {
    if (openConversationMenu?.id === button.dataset.conversationId) {
      openConversationMenu = null;
    } else {
      const bounds = button.getBoundingClientRect();
      const menuWidth = 176;
      const opensUp = bounds.bottom + 102 > window.innerHeight && bounds.top > 102;
      openConversationMenu = {
        id: button.dataset.conversationId,
        top: opensUp ? bounds.top - 6 : bounds.bottom + 6,
        left: Math.max(8, Math.min(bounds.right - menuWidth, window.innerWidth - menuWidth - 8)),
        opensUp,
      };
    }
    renderThreads();
  } else if (action === "export") {
    const conversation = conversations.find((item) => item.id === button.dataset.conversationId);
    if (conversation) {
      try { await exportConversation(conversation); }
      catch (error) { toast(error.message, "error"); }
    }
    openConversationMenu = null;
    renderThreads();
  } else if (action === "delete") {
    const conversation = conversations.find((item) => item.id === button.dataset.conversationId);
    if (conversation) showDeleteConfirmation(conversation);
    openConversationMenu = null;
    renderThreads();
  } else if (action === "cancel-delete") {
    closeDeleteConfirmation();
  } else if (action === "confirm-delete") {
    const conversation = conversations.find(
      (item) => item.id === pendingDeleteConversationId
    );
    closeDeleteConfirmation();
    if (conversation) await deleteConversation(conversation);
  } else if (action === "suggest") {
    const textarea = document.getElementById("agentPrompt");
    if (textarea) {
      textarea.value = button.dataset.prompt || "";
      textarea.focus();
      textarea.dispatchEvent(new Event("input"));
      persistDraft();
    }
  } else if (action === "copy") {
    const conversation = activeConversation();
    const message = conversation?.messages.find((item) => item.id === button.dataset.messageId);
    if (message) toast(await copyText(message.content) ? t("copied") : t("copyFailed"), "success");
  } else if (action === "attachment-menu") {
    const menu = document.getElementById("agentAddMenu");
    menu?.classList.toggle("hidden");
    button.setAttribute("aria-expanded", String(!menu?.classList.contains("hidden")));
  } else if (action === "add-image") {
    document.getElementById("agentImageInput")?.click();
  } else if (action === "add-file") {
    document.getElementById("agentFileInput")?.click();

  } else if (action === "remove-attachment") {
    forgetAttachments(pendingAttachments.filter((attachment) => attachment.id === button.dataset.attachmentId));
    pendingAttachments = pendingAttachments.filter((attachment) => attachment.id !== button.dataset.attachmentId);
    persistDraft();
    renderPendingAttachments();
  }
  return true;
}
