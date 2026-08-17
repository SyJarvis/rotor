// MindAgent chat page — local conversation history + streaming Rotor responses.

import { t } from "../i18n.js";
import { copyText } from "../api.js";
import { escapeHtml, refreshIcons, toast } from "../ui.js";

const STORAGE_KEY = "rotor.mindagent.conversations.v1";
const ACTIVE_KEY = "rotor.mindagent.active.v1";

let conversations = readConversations();
let activeId = localStorage.getItem(ACTIVE_KEY) || conversations[0]?.id || null;
let models = [];
let status = null;
let abortController = null;
let initialized = false;
let pendingAttachments = [];
let openConversationMenu = null;
let pendingDeleteConversationId = null;

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
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    return value.filter((item) => item && typeof item.id === "string" && Array.isArray(item.messages));
  } catch {
    return [];
  }
}

function saveConversations() {
  conversations = conversations
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
    .slice(0, 50);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  if (activeId) localStorage.setItem(ACTIVE_KEY, activeId);
}

function activeConversation() {
  return conversations.find((conversation) => conversation.id === activeId) || null;
}

function createConversation() {
  const now = new Date().toISOString();
  const conversation = {
    id: uid(),
    title: t("newConversation"),
    model: models[0] || "",
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
  conversations.unshift(conversation);
  activeId = conversation.id;
  saveConversations();
  return conversation;
}

function ensureConversation() {
  return activeConversation() || createConversation();
}

function renderShell() {
  const container = document.getElementById("mindagent");
  if (!container) return;
  container.classList.add("mindagent-panel");
  container.innerHTML = `
    <div class="agent-chat-shell">
      <section class="agent-chat-main">
        <header class="agent-chat-head">
          <div class="agent-identity">
            <div class="agent-avatar"><i data-lucide="sparkles"></i></div>
            <div>
              <strong>MindAgent</strong>
              <span id="agentRuntimeStatus">${t("agentReady")}</span>
            </div>
          </div>
          <label class="agent-model-picker">
            <span>${t("model")}</span>
            <select id="agentModel" aria-label="${t("model")}"></select>
          </label>
        </header>

        <div class="agent-messages" id="agentMessages" aria-live="polite"></div>

        <div class="agent-composer-wrap">
          <div class="agent-attachment-strip hidden" id="agentAttachmentStrip"></div>
          <div class="agent-composer">
            <div class="agent-add-wrap">
              <button class="agent-add" type="button" data-agent-action="attachment-menu" aria-label="${t("addAttachment")}" aria-expanded="false">
                <i data-lucide="plus"></i>
              </button>
              <div class="agent-add-menu hidden" id="agentAddMenu">
                <button type="button" data-agent-action="add-image">
                  <span class="agent-add-menu-icon image"><i data-lucide="image-plus"></i></span>
                  <span><strong>${t("addPhoto")}</strong><small>${t("addPhotoHint")}</small></span>
                </button>
                <button type="button" data-agent-action="add-file">
                  <span class="agent-add-menu-icon file"><i data-lucide="file-plus-2"></i></span>
                  <span><strong>${t("addFile")}</strong><small>${t("addFileHint")}</small></span>
                </button>
                <button type="button" class="disabled" data-agent-action="web-search" aria-disabled="true">
                  <span class="agent-add-menu-icon search"><i data-lucide="globe-2"></i></span>
                  <span><strong>${t("webSearch")}</strong><small>${t("comingSoon")}</small></span>
                </button>
              </div>
              <input id="agentImageInput" class="hidden" type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
              <input id="agentFileInput" class="hidden" type="file" accept="text/*,.md,.markdown,.json,.jsonl,.csv,.tsv,.xml,.yaml,.yml,.py,.js,.mjs,.cjs,.ts,.tsx,.jsx,.html,.css,.scss,.sql,.sh,.toml,.ini,.cfg,.log,.java,.go,.rs,.c,.h,.cpp,.hpp" multiple>
            </div>
            <textarea id="agentPrompt" rows="1" maxlength="100000" placeholder="${t("chatPlaceholder")}" aria-label="${t("chatPlaceholder")}"></textarea>
            <button class="agent-send" id="agentSend" type="button" aria-label="${t("sendMessage")}">
              <i data-lucide="arrow-up"></i>
            </button>
            <button class="agent-stop hidden" id="agentStop" type="button" aria-label="${t("stopGenerating")}">
              <i data-lucide="square"></i>
            </button>
          </div>
          <p class="agent-composer-hint">${t("chatHint")}</p>
        </div>
      </section>
    </div>`;

  container.querySelector("#agentPrompt")?.addEventListener("input", resizePrompt);
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
  container.querySelector("#agentModel")?.addEventListener("change", (event) => {
    const conversation = ensureConversation();
    conversation.model = event.target.value;
    conversation.updatedAt = new Date().toISOString();
    saveConversations();
  });
  initialized = true;
  renderPendingAttachments();
  refreshIcons(container);
}

function resizePrompt(event) {
  const textarea = event.currentTarget;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
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

async function handleImageFiles(event) {
  const files = [...(event.currentTarget.files || [])];
  event.currentTarget.value = "";
  document.getElementById("agentAddMenu")?.classList.add("hidden");
  for (const file of files) {
    if (pendingAttachments.length >= 6) {
      toast(t("tooManyAttachments"), "warning");
      break;
    }
    if (file.size > 5_000_000) {
      toast(`${file.name}: ${t("imageTooLarge")}`, "warning");
      continue;
    }
    if (pendingAttachments.reduce((total, item) => total + item.size, 0) + file.size > 10_000_000) {
      toast(t("attachmentsTooLarge"), "warning");
      break;
    }
    try {
      pendingAttachments.push({
        id: uid(),
        kind: "image",
        name: file.name,
        mimeType: file.type || "image/jpeg",
        size: file.size,
        dataUrl: await readAsDataUrl(file),
      });
    } catch (error) {
      toast(`${file.name}: ${error.message}`, "error");
    }
  }
  renderPendingAttachments();
}

async function handleTextFiles(event) {
  const files = [...(event.currentTarget.files || [])];
  event.currentTarget.value = "";
  document.getElementById("agentAddMenu")?.classList.add("hidden");
  for (const file of files) {
    if (pendingAttachments.length >= 6) {
      toast(t("tooManyAttachments"), "warning");
      break;
    }
    const isText = file.type.startsWith("text/")
      || ["application/json", "application/xml", "application/yaml"].includes(file.type)
      || TEXT_FILE_EXTENSIONS.has(fileExtension(file.name));
    if (!isText) {
      toast(`${file.name}: ${t("unsupportedFileType")}`, "warning");
      continue;
    }
    if (file.size > 500_000) {
      toast(`${file.name}: ${t("fileTooLarge")}`, "warning");
      continue;
    }
    if (pendingAttachments.reduce((total, item) => total + item.size, 0) + file.size > 10_000_000) {
      toast(t("attachmentsTooLarge"), "warning");
      break;
    }
    try {
      pendingAttachments.push({
        id: uid(),
        kind: "file",
        name: file.name,
        mimeType: file.type || "text/plain",
        size: file.size,
        content: await file.text(),
      });
    } catch (error) {
      toast(`${file.name}: ${error.message}`, "error");
    }
  }
  renderPendingAttachments();
}

function renderPendingAttachments() {
  const strip = document.getElementById("agentAttachmentStrip");
  if (!strip) return;
  strip.classList.toggle("hidden", !pendingAttachments.length);
  strip.innerHTML = pendingAttachments.map((attachment) => `
    <div class="agent-pending-attachment">
      ${attachment.kind === "image"
        ? `<img src="${attachment.dataUrl}" alt="">`
        : `<span class="agent-file-icon"><i data-lucide="file-text"></i></span>`}
      <span class="agent-attachment-name">
        <strong>${escapeHtml(attachment.name)}</strong>
        <small>${formatFileSize(attachment.size)}</small>
      </span>
      <button type="button" data-agent-action="remove-attachment" data-attachment-id="${escapeHtml(attachment.id)}" aria-label="${t("removeAttachment")}">
        <i data-lucide="x"></i>
      </button>
    </div>
  `).join("");
  refreshIcons(strip);
}

function renderThreads() {
  const list = document.getElementById("agentThreadList");
  if (!list) return;
  const newButton = document.querySelector(".sidebar-conversations-new");
  if (newButton) {
    newButton.title = t("newConversation");
    newButton.setAttribute("aria-label", t("newConversation"));
  }
  list.innerHTML = conversations.map((conversation) => {
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
          <strong>${escapeHtml(conversation.title || t("newConversation"))}</strong>
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
  }).join("");
  refreshIcons(list);
}

function showDeleteConfirmation(conversation) {
  const dialog = document.getElementById("mindagentDeleteModal");
  if (!dialog) return;
  pendingDeleteConversationId = conversation.id;
  const name = document.getElementById("mindagentDeleteConversationName");
  if (name) name.textContent = conversation.title || t("newConversation");
  dialog.classList.remove("hidden");
}

function closeDeleteConfirmation() {
  pendingDeleteConversationId = null;
  document.getElementById("mindagentDeleteModal")?.classList.add("hidden");
}

function deleteConversation(conversation) {
  conversations = conversations.filter((item) => item.id !== conversation.id);
  if (activeId === conversation.id) activeId = conversations[0]?.id || null;
  ensureConversation();
  saveConversations();
  renderThreads();
  renderMessages();
  renderModels();
}

function exportConversation(conversation) {
  const payload = {
    schema_version: 1,
    exported_at: new Date().toISOString(),
    conversation,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  const safeId = conversation.id.replace(/[^a-zA-Z0-9_-]/g, "_");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = `mindagent-conversation-${safeId}.json`;
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
    : `<span class="agent-typing"><i></i><i></i><i></i></span>`;
  const attachments = (message.attachments || []).length ? `
    <div class="agent-sent-attachments">
      ${message.attachments.map((attachment) => `
        <span>
          <i data-lucide="${attachment.kind === "image" ? "image" : "file-text"}"></i>
          ${escapeHtml(attachment.name)}
        </span>
      `).join("")}
    </div>` : "";
  return `
    <article class="agent-message ${role}" data-message-id="${escapeHtml(message.id)}">
      ${role === "assistant" ? `
        <div class="agent-message-meta">
          <span>MindAgent</span>
          ${message.content ? `
          <button type="button" data-agent-action="copy" data-message-id="${escapeHtml(message.id)}" aria-label="${t("copy")}">
            <i data-lucide="copy"></i>
          </button>` : ""}
        </div>` : ""}
      ${attachments}
      <div class="agent-message-content">${content}</div>
    </article>`;
}

function renderMessages() {
  const container = document.getElementById("agentMessages");
  if (!container) return;
  const conversation = ensureConversation();
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
  container.scrollTop = container.scrollHeight;
}

function renderModels() {
  const select = document.getElementById("agentModel");
  if (!select) return;
  const conversation = ensureConversation();
  if (!models.length) {
    select.innerHTML = `<option value="">${t("noModelsAvailable")}</option>`;
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("");
  if (!models.includes(conversation.model)) conversation.model = models[0];
  select.value = conversation.model;
  saveConversations();
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
  const model = document.getElementById("agentModel");
  if (model) model.disabled = generating || !models.length;
  setRuntimeStatus(generating ? t("agentThinking") : t("agentReady"), generating);
}

async function refreshModels() {
  const response = await fetch("/api/admin/mindagent/models");
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  status = await response.json();
  models = status.models || [];
  renderModels();
  if (!status.mindagent_available) setRuntimeStatus(t("mindagentUnavailable"));
  else if (!status.token_available) setRuntimeStatus(t("chatTokenUnavailable"));
}

function parseEventBlock(block) {
  const data = block.split("\n").find((line) => line.startsWith("data:"));
  if (!data) return null;
  try { return JSON.parse(data.slice(5).trim()); }
  catch { return null; }
}

async function sendMessage() {
  if (abortController) return;
  const textarea = document.getElementById("agentPrompt");
  const typedPrompt = textarea?.value.trim();
  if (!typedPrompt && !pendingAttachments.length) return;
  const prompt = typedPrompt || t("defaultAttachmentPrompt");

  const conversation = ensureConversation();
  if (!conversation.model) {
    toast(t("selectModelFirst"), "warning");
    return;
  }
  const outboundAttachments = [...pendingAttachments];
  const userMessage = {
    id: uid(),
    role: "user",
    content: prompt,
    attachments: outboundAttachments.map(({ kind, name, mimeType, size }) => ({
      kind, name, mimeType, size,
    })),
  };
  const assistantMessage = { id: uid(), role: "assistant", content: "" };
  conversation.messages.push(userMessage, assistantMessage);
  if (conversation.messages.filter((message) => message.role === "user").length === 1) {
    conversation.title = prompt.length > 32 ? `${prompt.slice(0, 32)}…` : prompt;
  }
  conversation.updatedAt = new Date().toISOString();
  textarea.value = "";
  textarea.style.height = "auto";
  pendingAttachments = [];
  renderPendingAttachments();
  saveConversations();
  renderThreads();
  renderMessages();
  setGenerating(true);

  abortController = new AbortController();
  try {
    const payloadMessages = conversation.messages
      .filter((message) => message.id !== assistantMessage.id)
      .map(({ role, content }) => ({ role, content }));
    const response = await fetch("/api/admin/mindagent/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Conversation-Id": conversation.id,
      },
      body: JSON.stringify({
        conversation_id: conversation.id,
        model: conversation.model,
        messages: payloadMessages,
        attachments: outboundAttachments.map((attachment) => ({
          kind: attachment.kind,
          name: attachment.name,
          mime_type: attachment.mimeType,
          size: attachment.size,
          data_url: attachment.dataUrl,
          content: attachment.content,
        })),
      }),
      signal: abortController.signal,
    });
    if (!response.ok) throw new Error(await response.text() || `${response.status} ${response.statusText}`);
    if (!response.body) throw new Error(t("streamUnavailable"));

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalAnswer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const event = parseEventBlock(block);
        if (!event) continue;
        if (event.type === "delta") {
          assistantMessage.content += event.delta || "";
          const content = document.querySelector(`[data-message-id="${assistantMessage.id}"] .agent-message-content`);
          if (content) {
            content.classList.add("streaming");
            content.innerHTML = renderMarkdown(assistantMessage.content);
          }
          const messages = document.getElementById("agentMessages");
          if (messages) messages.scrollTop = messages.scrollHeight;
        } else if (event.type === "done") {
          finalAnswer = event.answer || "";
        } else if (event.type === "error") {
          throw new Error(event.message || t("chatFailed"));
        }
      }
      if (done) break;
    }
    // The final MindAgent result is authoritative and also repairs a partial
    // browser stream if a proxy coalesced or dropped an intermediate chunk.
    assistantMessage.content = finalAnswer || assistantMessage.content || t("emptyResponse");
  } catch (error) {
    if (error.name === "AbortError") {
      assistantMessage.content ||= t("generationStopped");
    } else {
      assistantMessage.content = `${t("chatFailed")}: ${error.message}`;
      toast(error.message, "error");
    }
  } finally {
    abortController = null;
    conversation.updatedAt = new Date().toISOString();
    saveConversations();
    renderThreads();
    renderMessages();
    setGenerating(false);
    document.getElementById("agentPrompt")?.focus();
  }
}

function stopGenerating() {
  abortController?.abort();
}

export async function load() {
  if (!initialized || !document.querySelector("#mindagent .agent-chat-shell")) renderShell();
  ensureConversation();
  renderThreads();
  renderMessages();
  renderModels();
  try { await refreshModels(); }
  catch (error) {
    setRuntimeStatus(t("chatServiceUnavailable"));
    toast(error.message, "error");
  }
}

export function renderHistory() {
  renderThreads();
}

export async function onClick(target) {
  const button = target.closest("[data-agent-action]");
  if (!button) {
    if (openConversationMenu && !target.closest(".agent-thread-menu")) {
      openConversationMenu = null;
      renderThreads();
    }
    return false;
  }
  const action = button.dataset.agentAction;

  if (action === "new") {
    if (abortController) stopGenerating();
    pendingAttachments = [];
    openConversationMenu = null;
    createConversation();
    document.dispatchEvent(new CustomEvent("mindagentconversationopen"));
    renderThreads();
    renderMessages();
    renderModels();
    renderPendingAttachments();
    document.getElementById("agentPrompt")?.focus();
  } else if (action === "open") {
    if (abortController) return true;
    activeId = button.dataset.conversationId;
    openConversationMenu = null;
    saveConversations();
    document.dispatchEvent(new CustomEvent("mindagentconversationopen"));
    renderThreads();
    renderMessages();
    renderModels();
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
    if (conversation) exportConversation(conversation);
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
    if (conversation) deleteConversation(conversation);
  } else if (action === "suggest") {
    const textarea = document.getElementById("agentPrompt");
    if (textarea) {
      textarea.value = button.dataset.prompt || "";
      textarea.focus();
      textarea.dispatchEvent(new Event("input"));
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
  } else if (action === "web-search") {
    toast(t("webSearchComingSoon"), "info");
  } else if (action === "remove-attachment") {
    pendingAttachments = pendingAttachments.filter(
      (attachment) => attachment.id !== button.dataset.attachmentId
    );
    renderPendingAttachments();
  }
  return true;
}
