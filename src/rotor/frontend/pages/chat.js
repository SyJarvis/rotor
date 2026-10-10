// Basic chat with per-conversation settings, durable attachments and streaming responses.

import { t } from "../i18n.js";
import {
  adminCsrfHeaders,
  api,
  copyText,
  handleAdminUnauthorized,
} from "../api.js?v=3";
import { escapeHtml, refreshIcons, toast } from "../ui.js";
import { ChatStore } from "../chat-store.js?v=1";
import { accountId } from "../session.js?v=1";
const store = new ChatStore();
const syncSnapshots = new Map();
let syncError = null;
let owner = "";
let listLoaded = false;

const SETTINGS_VISIBLE_KEY = "rotor.chat.settings.visible.v2";
let conversations = [];
let activeId = null;
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
let settingsVisible = true;
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

function readSettingsVisibility() {
  try { return localStorage.getItem(`${SETTINGS_VISIBLE_KEY}.${accountId()}`) !== "false"; }
  catch { return true; }
}

function setSettingsVisibility(visible, restoreFocus = false) {
  settingsVisible = visible;
  try { localStorage.setItem(`${SETTINGS_VISIBLE_KEY}.${accountId()}`, String(visible)); } catch {}
  renderDrawers();
  if (restoreFocus) document.querySelector('[data-agent-action="details"]')?.focus();
}

function renderSyncStatus() {
  const element = document.getElementById("chatSyncStatus");
  if (!element) return;
  element.classList.toggle("hidden", !syncError);
  element.querySelector("span").textContent = syncError?.status === 409 ? t("chatRevisionConflict") : syncError ? `${t("chatSyncFailed")}: ${syncError.message}` : "";
  element.querySelector("button").textContent = syncError?.status === 409 ? t("chatKeepDraftCopy") : t("retrySettings");
}

function saveConversations(conversation = activeConversation(), force = false) {
  if (!conversation || !store.records.has(conversation.id)) return Promise.resolve();
  if (!force && generation?.conversation.id === conversation.id && !generation.inserted) return Promise.resolve();
  const changes = !force && generation?.conversation.id === conversation.id ? { draft: structuredClone(conversation.draft) } : { settings: { ...conversation.settings }, draft: structuredClone(conversation.draft) };
  for (const key of ["temperature", "max_tokens"]) if (changes.settings && changes.settings[key] !== null && changes.settings[key] !== "") changes.settings[key] = Number(changes.settings[key]);
  const snapshot = JSON.stringify(changes);
  if (syncSnapshots.get(conversation.id) === snapshot) return store.writers.get(conversation.id)?.running || Promise.resolve();
  syncSnapshots.set(conversation.id, snapshot);
  return store.save(conversation.id, changes).then((saved) => {
    conversation.revision = saved.revision; if (activeId === conversation.id) { syncError = null; renderSyncStatus(); }
  }).catch((error) => {
    if (error.name === "AbortError") return;
    syncSnapshots.delete(conversation.id);
    if (activeId === conversation.id) { syncError = error; renderSyncStatus(); }
    if (error.status === 409) {
      const remote = store.records.get(conversation.id);
      if (remote) { conversation.messages = remote.messages; conversation.revision = remote.revision; }
      if (activeId === conversation.id) renderMessages();
    }
  });
}

function normalize(conversation) {
  return { ...conversation, model: conversation.settings?.model || "", messages: conversation.messages || [], draft: conversation.draft || { text: "", attachments: [] } };
}

function activeConversation() {
  return conversations.find((conversation) => conversation.id === activeId) || null;
}

async function createConversation(inheritedSettings = null, draft) {
  const settings = inheritedSettings ? { ...inheritedSettings } : { model: models[0] || "", api_format: "chat", system_prompt: "", temperature: null, max_tokens: null };
  const conversation = normalize(await store.create(settings, draft));
  conversations.unshift(conversation);
  activeId = conversation.id;
  syncSnapshots.set(conversation.id, JSON.stringify({ settings: conversation.settings, draft: conversation.draft }));
  return conversation;
}

function ensureConversation() { return activeConversation(); }

function conversationTitle(conversation) {
  return conversation?.title || t("newConversation");
}

function renderCurrentTitle() {
  const title = document.getElementById("chatCurrentTitle");
  if (title) title.textContent = t("chatRecent");
  document.querySelector('[data-agent-action="history"]')?.setAttribute("title", t("chatRecent"));
}

function persistDraft(force = false) {
  const conversation = activeConversation();
  if (!conversation || composerConversationId !== conversation.id) return;
  const draft = { text: document.getElementById("agentPrompt")?.value || "", attachments: pendingAttachments.map(attachmentMetadata) };
  if (JSON.stringify(conversation.draft) !== JSON.stringify(draft)) conversation.updatedAt = new Date().toISOString();
  conversation.draft = draft;
  return saveConversations(conversation, force);
}

async function restoreComposer() {
  const conversation = ensureConversation();
  if (!conversation) return;
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
  await persistDraft();
  rememberScroll(); stopGenerating(); invalidateProbe(); releasePendingAttachments();
  const epoch = ++loadEpoch;
  const local = conversations.find((item) => item.id === id);
  const fresh = normalize(await store.get(id));
  if (epoch !== loadEpoch || !pageLoaded) return;
  const conflict = store.writers.get(id)?.conflict;
  if (conflict && local) { fresh.draft = local.draft; fresh.settings = local.settings; }
  conversations = conversations.map((item) => item.id === id ? fresh : item);
  activeId = id; syncError = conflict ? Object.assign(new Error(), { status: 409 }) : null;
  closeForConversationChange(); closeAttachmentMenu(); historyQuery = "";
  const search = document.getElementById("chatHistorySearch"); if (search) search.value = "";
  const restoring = restoreComposer();
  renderThreads(); renderMessages(); renderDetails(); setGenerating(false); renderSyncStatus();
  document.dispatchEvent(new CustomEvent("chatconversationopen"));
  await restoring;
}

async function newConversation() {
  await persistDraft();
  const current = ensureConversation();
  const settings = { ...current?.settings };
  if (current && !current.messages.length && !current.draft?.text && !current.draft?.attachments?.length) {
    releasePendingAttachments(); closeForConversationChange(); document.getElementById("agentPrompt")?.focus(); return;
  }
  const reusable = conversations.find((item) => item.messages && !item.messages.length && !item.messageCount && !item.draft?.text && !item.draft?.attachments?.length && store.records.has(item.id));
  const previous = activeId;
  if (reusable) { reusable.settings = settings; await saveConversations(reusable); }
  const created = reusable || await createConversation(settings);
  activeId = previous;
  await changeConversation(created.id);
  document.getElementById("agentPrompt")?.focus();
}

async function retrySync() {
  const conversation = activeConversation();
  if (!conversation) return;
  if (syncError?.status === 409) {
    const draft = structuredClone(conversation.draft), settings = { ...conversation.settings };
    // Attachment references are scoped to their conversation; upload copies first.
    const created = await createConversation(settings);
    for (const metadata of draft.attachments) {
      const payload = await store.attachment(conversation.id, metadata.id);
      const { id: ignored, ...content } = payload;
      const uploaded = await store.upload(created.id, content); metadata.id = uploaded.id;
    }
    created.draft = draft; syncError = null;
    await saveConversations(created); await changeConversation(created.id);
  } else { syncSnapshots.delete(conversation.id); await saveConversations(conversation); }
  renderSyncStatus();
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
          <div class="chat-sync-status hidden" id="chatSyncStatus" role="alert"><span></span><button type="button" class="btn-secondary" data-agent-action="retry-sync"></button></div>
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

async function toggleDrawer(drawer) {
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
  const session = owner;
  const summaries = await store.list();
  if (owner !== session || !pageLoaded) return;
  conversations = summaries.map((summary) => conversations.find((item) => item.id === summary.id) || normalize(summary));
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
  const source = ensureConversation()?.settings;
  if (!source) return null;
  const temperature = source.temperature == null || source.temperature === "" ? null : Number(source.temperature);
  const maxTokens = source.max_tokens == null || source.max_tokens === "" ? null : Number(source.max_tokens);
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
  if (button) button.disabled = Boolean(probe) || !models.length || status?.account_available === false;
  const result = document.getElementById("chatProbeResult");
  if (!result) return;
  result.textContent = probe ? t("chatTesting") : probeResult?.text || "";
  result.classList.toggle("error", probeResult?.error === true);
  result.classList.toggle("success", probeResult?.error === false);
}

async function testConnection() {
  if (status?.account_available === false) { toast(t("chatAccountUnavailable"), "warning"); return; }
  const settings = validateSettings();
  if (!settings || probe) return;
  const operation = { controller: new AbortController(), conversationId: activeId };
  probe = operation;
  probeResult = null;
  renderProbe();
  try {
    const result = await api("/api/chat/test", { method: "POST", body: JSON.stringify(settings), signal: operation.controller.signal });
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
      const uploaded = await store.upload(conversationId, { kind: attachment.kind, name: attachment.name, mime_type: attachment.mimeType, size: attachment.size, content: attachment.content, data_url: attachment.dataUrl });
      attachment.id = uploaded.id;
      if (epoch !== attachmentEpoch || conversationId !== activeId || !pageLoaded) {
        await store.removeAttachment(conversationId, attachment.id); break;
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

async function forgetAttachments(attachments, conversationId = activeId) {
  for (const attachment of attachments) {
    attachmentCache.delete(attachment.id);
    try { await store.removeAttachment(conversationId, attachment.id); } catch { /* Referenced attachments remain owned by their conversation. */ }
  }
}

function releasePendingAttachments() {
  attachmentEpoch++;
  attachmentCache.clear();
  pendingAttachments = [];
}

async function attachmentPayload(attachment, conversationId = activeId) {
  if (attachment.dataUrl !== undefined || attachment.content !== undefined) return attachment;
  if (!attachment.id) return null;
  if (attachmentCache.has(attachment.id)) return attachmentCache.get(attachment.id);
  const raw = await store.attachment(conversationId, attachment.id);
  const stored = raw ? { ...raw, mimeType: raw.mime_type, dataUrl: raw.data_url } : null;
  if (stored) attachmentCache.set(attachment.id, stored);
  return stored || null;
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
  if (!store.records.has(conversation.id)) await store.get(conversation.id);
  const deletingActive = activeId === conversation.id;
  if (deletingActive) { await persistDraft(); stopGenerating(); invalidateProbe(); }
  await store.remove(conversation.id);
  if (deletingActive) releasePendingAttachments();
  readingPositions.delete(conversation.id); syncSnapshots.delete(conversation.id);
  closeForConversationChange(); conversations = conversations.filter((item) => item.id !== conversation.id);
  if (deletingActive) {
    activeId = null;
    if (conversations.length) await changeConversation(conversations[0].id);
    else { await createConversation(conversation.settings); await restoreComposer(); renderMessages(); }
  }
  renderThreads(); renderDetails(); renderPendingAttachments();
}

async function exportConversation(conversation) {
  conversation = normalize(await store.get(conversation.id));
  const draftAttachments = await Promise.all((conversation.draft?.attachments || []).map(async (attachment) => await attachmentPayload(attachment, conversation.id) || { ...attachment, unavailable: true }));
  const messages = await Promise.all(conversation.messages.map(async (message) => ({ ...message, attachments: await Promise.all((message.attachments || []).map(async (attachment) => await attachmentPayload(attachment, conversation.id) || { ...attachment, unavailable: true })) })));
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
    : role === "assistant" && (!message.status || message.status === "running") ? `<span class="agent-typing"><i></i><i></i><i></i></span>` : "";
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
  if (!conversation) { container.innerHTML = ""; return; }
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
  if (!conversation) return;
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
  const ready = status?.account_available === false ? status.message || t("chatAccountUnavailable") : t("agentReady");
  setRuntimeStatus(generating ? t("agentThinking") : ready, generating);
}

async function refreshModels(epoch) {
  const response = await api("/api/chat/models");
  if (epoch !== loadEpoch || !pageLoaded) return;
  status = response;
  models = [...new Set(status.models || [])];
  const conversation = ensureConversation();
  if (!conversation) return;
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
  const textarea = document.getElementById("agentPrompt"), submittedText = textarea?.value || "";
  const content = submittedText.trim();
  if (!content && !pendingAttachments.length) return;
  if (status?.account_available === false) { toast(t("chatAccountUnavailable"), "warning"); return; }
  const settings = validateSettings(); if (!settings) return;
  const conversation = ensureConversation(), outbound = [...pendingAttachments];
  const operation = { controller: new AbortController(), conversation, assistantMessage: null, inserted: false, session: owner };
  generation = operation; setGenerating(true);
  try {
    await persistDraft(true);
    const canonical = await store.flush(conversation.id);
    if (generation !== operation) return;
    const response = await fetch("/api/chat/completions", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", ...adminCsrfHeaders() },
      body: JSON.stringify({ conversation_id: conversation.id, expected_revision: canonical.revision, settings, message: { content, attachments: outbound.map(({ id }) => ({ id })) } }), signal: operation.controller.signal,
    });
    if (generation !== operation) return;
    handleAdminUnauthorized(response);
    if (!response.ok) throw Object.assign(new Error(await responseError(response)), { status: response.status });
    if (!response.body) throw new Error(t("streamUnavailable"));
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "", completed = false;
    const consume = (block) => {
      const event = parseEventBlock(block); if (!event || completed) return;
      if (event.type === "start") {
        store.accept(event.conversation);
        conversation.messages = event.conversation.messages; conversation.revision = event.conversation.revision; conversation.title = event.conversation.title;
        operation.assistantMessage = conversation.messages.findLast((message) => message.role === "assistant"); operation.inserted = true;
        if (textarea.value === submittedText) { textarea.value = ""; textarea.style.height = "auto"; }
        pendingAttachments = pendingAttachments.filter((attachment) => !outbound.some((sent) => sent.id === attachment.id));
        conversation.draft = { text: textarea.value, attachments: pendingAttachments.map(attachmentMetadata) };
        syncSnapshots.delete(conversation.id); saveConversations(conversation);
        renderPendingAttachments(); renderThreads(); renderMessages({ forceBottom: true });
      } else if (event.type === "delta" && operation.assistantMessage) {
        const list = document.getElementById("agentMessages"); followOutput = followOutput && (smoothScrollPending || nearBottom(list));
        operation.assistantMessage.content += event.content || "";
        const element = document.querySelector(`[data-message-id="${operation.assistantMessage.id}"] .agent-message-content`);
        if (element) { element.classList.add("streaming"); element.innerHTML = renderMarkdown(operation.assistantMessage.content); }
        if (followOutput) scrollToBottom(); else updateScrollControl();
      } else if (event.type === "done" || event.type === "error") {
        completed = true;
        if (event.conversation) { store.accept(event.conversation); conversation.messages = event.conversation.messages; conversation.revision = event.conversation.revision; conversation.title = event.conversation.title; }
        if (event.type === "error") toast(event.message || t("chatFailed"), "error");
      }
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (generation !== operation) { await reader.cancel?.(); return; }
        buffer = (buffer + decoder.decode(value || new Uint8Array(), { stream: !done })).replaceAll("\r\n", "\n");
        const blocks = buffer.split("\n\n"); buffer = blocks.pop() || "";
        blocks.forEach(consume);
        if (done) { if (buffer.trim()) consume(buffer); break; }
      }
      if (!completed) throw new Error(t("chatStreamInterrupted"));
    } finally { if (!completed) { try { await reader.cancel?.(); } catch {} } reader.releaseLock?.(); }
  } catch (error) {
    if (generation !== operation || error.name === "AbortError") return;
    if (error.status === 409) {
      const localDraft = { text: textarea.value, attachments: pendingAttachments.map(attachmentMetadata) };
      try { const fresh = await store.get(conversation.id); conversation.messages = fresh.messages; conversation.revision = fresh.revision; conversation.draft = localDraft; } catch {}
      const writer = store.writers.get(conversation.id) || {}; writer.conflict = true; store.writers.set(conversation.id, writer);
      syncError = error; renderSyncStatus();
    } else {
      operation.controller.abort(); toast(error.message, "error");
      if (operation.assistantMessage) {
        const messageId = operation.assistantMessage.id;
        const failure = `${t("chatFailed")}: ${error.message}`;
        try {
          const fresh = await store.get(conversation.id);
          if (generation !== operation) return;
          conversation.messages = fresh.messages; conversation.revision = fresh.revision;
          operation.assistantMessage = conversation.messages.find((message) => message.id === messageId) || operation.assistantMessage;
        } catch {}
        operation.assistantMessage.error = failure;
      }
    }
  } finally {
    if (generation === operation) {
      generation = null;
      if (operation.inserted) { syncSnapshots.delete(conversation.id); await saveConversations(conversation); }
      if (pageLoaded && activeId === conversation.id) { renderThreads(); renderMessages(); renderDetails(); setGenerating(false); }
    }
  }
}

function stopGenerating() {
  const operation = generation; if (!operation) return;
  generation = null; operation.controller.abort();
  if (operation.assistantMessage) operation.assistantMessage.status = "stopped";
  if (pageLoaded && activeId === operation.conversation.id) { renderMessages(); setGenerating(false); }
  if (operation.inserted) {
    const session = owner, id = operation.conversation.id;
    setTimeout(async () => {
      if (owner !== session) return;
      try {
        const fresh = await store.get(id);
        if (owner !== session) return;
        operation.conversation.messages = fresh.messages; operation.conversation.revision = fresh.revision;
        syncSnapshots.delete(id); await saveConversations(operation.conversation);
        if (pageLoaded && activeId === id && !generation) renderMessages();
      } catch {}
    }, 300);
  }
}

export async function load() {
  if (!accountId()) return;
  if (owner !== accountId()) { resetSession(); owner = accountId(); settingsVisible = readSettingsVisibility(); }
  pageLoaded = true; const epoch = ++loadEpoch;
  const existingShell = initialized && document.querySelector("#chat .agent-chat-shell"); if (!existingShell) renderShell();
  refreshShellLabels();
  try {
    if (!listLoaded) {
      const [summaries, modelResult] = await Promise.all([store.list(), api("/api/chat/models")]);
      if (epoch !== loadEpoch || !pageLoaded) return;
      status = modelResult; models = [...new Set(status.models || [])]; conversations = summaries.map(normalize); listLoaded = true;
      if (conversations.length) {
        activeId = conversations[0].id; const fresh = normalize(await store.get(activeId)); conversations[0] = fresh;
      } else await createConversation();
    }
    if (epoch !== loadEpoch || !pageLoaded) return;
    if (composerConversationId !== activeId) await restoreComposer();
    renderThreads(); renderMessages(); renderDetails(); renderPendingAttachments(); renderDrawers(); renderSyncStatus(); setGenerating(Boolean(generation));
    await refreshModels(epoch);
  } catch (error) { if (epoch === loadEpoch && pageLoaded) { setRuntimeStatus(t("chatServiceUnavailable")); toast(error.message, "error"); } }
}

export function resetSession() {
  stopGenerating(); store.reset(); invalidateProbe(); ++loadEpoch; ++attachmentEpoch;
  pageLoaded = false; listLoaded = false; owner = ""; conversations = []; activeId = null; models = []; status = null;
  syncSnapshots.clear(); attachmentCache.clear(); readingPositions.clear(); pendingAttachments = [];
  composerConversationId = renderedConversationId = null; syncError = null; generation = null;
  const container = document.getElementById("chat"); if (container) container.innerHTML = "";
}
document.addEventListener("sessionended", resetSession);

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
    await toggleDrawer(action);
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

  } else if (action === "retry-sync") {
    await retrySync();
  } else if (action === "remove-attachment") {
    const removed = pendingAttachments.filter((attachment) => attachment.id === button.dataset.attachmentId);
    pendingAttachments = pendingAttachments.filter((attachment) => attachment.id !== button.dataset.attachmentId);
    await persistDraft();
    await forgetAttachments(removed);
    renderPendingAttachments();
  }
  return true;
}
