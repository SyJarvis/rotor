// App entry — router, sidebar/topbar wiring, modal, theme/locale listeners.

import { t, applyLocale } from "./i18n.js";
import { initSettings } from "./settings.js?v=5";
import { isAdmin } from "./session.js?v=1";
import * as users from "./pages/users.js?v=3";
import { initSidebar, closeSidebar } from "./sidebar.js?v=1";
import { api, copyText } from "./api.js?v=3";
import { parseCsv, parseJson, refreshIcons, toast } from "./ui.js?v=12";
import { refreshTheme } from "./charts.js?v=10";
import { setChannelPresets, applyChannelDefaults, channelConnection } from "./channel-form.js?v=2";

import * as overview from "./pages/overview.js?v=8";
import * as channels from "./pages/channels.js?v=11";
import { editState as channelEditState } from "./pages/channels.js?v=11";
import * as tokens from "./pages/tokens.js?v=10";
import * as usage from "./pages/usage.js?v=16";
import * as logs from "./pages/logs.js?v=8";
import * as monitoring from "./pages/monitoring.js?v=8";
import * as chat from "./pages/chat.js?v=9";

const PAGES = { overview, channels, tokens, usage, logs, monitoring, chat, users };
const TITLES = {
  overview: () => t("overview"),
  channels: () => t("channels"),
  tokens: () => t("apiKeys"),
  usage: () => t("usage"),
  logs: () => t("logs"),
  monitoring: () => t("monitoringTitle"),
  chat: () => t("chatChat"),
  users: () => t("usersTitle"),
};

let current = isAdmin() ? "overview" : "chat";
let initialized = false;
let channelPresets = [];

async function loadChannelPresets() {
  channelPresets = await api("/api/admin/channels/presets");
  setChannelPresets(channelPresets);
  const select = document.getElementById("channelForm")?.elements.type;
  if (!select) return;
  const selected = select.value;
  select.innerHTML = channelPresets.map((preset) =>
    `<option value="${preset.id}"${preset.id === "openai_codex" ? " hidden" : ""}>${preset.label}</option>`
  ).join("");
  select.value = channelPresets.some((preset) => preset.id === selected)
    ? selected
    : channelPresets[0]?.id || "openai";
  applyChannelPreset(select.value, true);
}

function applyChannelPreset(provider, reset = false) {
  const preset = channelPresets.find((item) => item.id === provider);
  const form = document.getElementById("channelForm");
  if (!preset || !form) return;
  if (channels.usesOAuth(form)) return;
  applyChannelDefaults(form, { providerChanged: true, reset });
}

export async function refreshActive() {
  const page = PAGES[current];
  if (page) {
    try { await page.load(); }
    catch (e) { toast(e.message, "error"); }
  }
}

function switchTab(tab) {
  if (!PAGES[tab] || (!isAdmin() && !["overview", "chat", "tokens", "usage", "logs"].includes(tab))) return;
  if (current === tab) return refreshActive();
  PAGES[current]?.unload?.();
  current = tab;
  document.getElementById("content")?.classList.toggle("chat-content", tab === "chat");
  document.querySelector(".main")?.classList.toggle("chat-main", tab === "chat");
  document.querySelector(".app-shell")?.classList.toggle("chat-app", tab === "chat");
  document.querySelectorAll(".nav-item[data-tab]").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
  document.querySelectorAll(".panel").forEach((el) => {
    el.classList.toggle("active", el.id === tab);
  });
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[tab]?.() || tab;
  document.title = `${TITLES[tab]?.()} · Rotor`;
  closeSidebar();
  return refreshActive();
}

/* ---------- navigation ---------- */
document.querySelectorAll(".nav-item[data-tab]").forEach((el) => {
  el.addEventListener("click", () => switchTab(el.dataset.tab));
});
document.addEventListener("chatconversationopen", () => switchTab("chat"));
document.addEventListener("overviewnavigate", (event) => {
  if (["tokens", "usage"].includes(event.detail?.tab)) switchTab(event.detail.tab);
});

/* ---------- sidebar ---------- */
initSidebar();

/* ---------- global click delegation ---------- */
document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;

  // copy buttons (overview/tokens share this)
  const copyBtn = target.closest("[data-copy-value]");
  if (copyBtn) {
    const value = copyBtn.dataset.copyValue || "";
    const ok = await copyText(value);
    toast(ok ? t("copied") : value, ok ? "success" : "warning");
    return;
  }

  // copy by target id
  const idCopy = target.closest("[data-copy-target]");
  if (idCopy) {
    const el = document.getElementById(idCopy.dataset.copyTarget);
    if (el) {
      const ok = await copyText(el.textContent || "");
      toast(ok ? t("copied") : "", ok ? "success" : "warning");
    }
    return;
  }

  // modal close
  const modalClose = target.closest("[data-close-modal]");
  if (modalClose) {
    if (modalClose.closest(".modal")?.id === "channelModal") {
      channels.closeForm();
      return;
    }
    modalClose.closest(".modal")?.classList.add("hidden");
    return;
  }

  // page-specific handlers
  for (const page of Object.values(PAGES)) {
    try { if (page.onClick && await page.onClick(target)) return; } catch (error) { toast(error.message, "error"); return; }
  }
});

/* ---------- channel modal ---------- */
const channelModal = document.getElementById("channelModal");
document.getElementById("channelForm")?.elements.credential_type?.addEventListener("change", () => {
  channels.changeCredentialType(document.getElementById("channelForm"));
});
document.getElementById("channelOAuthCheck")?.addEventListener("click", channels.checkEditedOAuth);
document.getElementById("channelOAuthLogin")?.addEventListener("click", channels.startOAuthLogin);
document.getElementById("channelOAuthCancel")?.addEventListener("click", () => channels.cancelOAuthLogin());
document.getElementById("channelOAuthCallback")?.addEventListener("input", channels.changeOAuthCallback);
document.getElementById("channelOAuthParse")?.addEventListener("click", channels.parseOAuthCallback);
document.getElementById("channelOAuthComplete")?.addEventListener("click", channels.completeOAuthLogin);
let channelModelsRevision = 0;
document.getElementById("channelForm")?.elements.models.addEventListener("input", () => { channelModelsRevision += 1; });
document.getElementById("channelForm")?.elements.type.addEventListener("change", (event) => {
  applyChannelPreset(event.target.value);
});

document.getElementById("channelForm")?.elements.protocol.addEventListener("change", (event) => {
  const form = document.getElementById("channelForm");
  if (!form) return;
  applyChannelDefaults(form);
});
document.getElementById("closeChannelModal")?.addEventListener("click", () => {
  channels.closeForm();
});

/* ---------- settings modal ---------- */
const settings = initSettings({
  onSaved(savedSettings) {
    for (const page of Object.values(PAGES)) {
      page.setDisplayTimezone?.(savedSettings.display_timezone);
    }
    refreshActive();
  },
});

document.getElementById("probeModels")?.addEventListener("click", async (event) => {
  const form = document.getElementById("channelForm");
  const oauth = channels.usesOAuth(form);
  const oauthPath = oauth ? channels.oauthProbePath(form) : null;
  const probeResult = document.getElementById("probeResult");
  if (oauth && !oauthPath) {
    channels.refreshAuthLabels();
    probeResult.textContent = t("oauthLoginRequired");
    probeResult.className = "inline-result error";
    return;
  }
  const keyInput = form?.elements.key?.value?.trim();
  const channelId = channelEditState.id;
  // Editing a saved channel with a blank key field → probe with the stored key.
  const useSaved = channelId && !keyInput;
  const button = event.currentTarget;
  if (button.disabled) return;
  const generation = channels.authGeneration();
  const authRequest = oauth && oauthPath === `/api/admin/channels/${channelId}/probe-models`
    ? channels.beginAuthStateRequest(channelId) : null;
  const revision = channelModelsRevision;
  const initialModels = form.elements.models.value;
  channels.setModelsProbing(true);
  const original = button.innerHTML;
  button.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("probing") || "..."}`;
  refreshIcons(button);
  try {
    let result;
    if (oauth) {
      result = await api(oauthPath, { method: "POST" });
    } else {
      const draft = channelConnection(form, parseJson(form.elements.extra.value, {}));
      if (useSaved) {
        result = await api(`/api/admin/channels/${channelId}/probe-models`, {
          method: "POST",
          body: JSON.stringify(draft),
        });
      } else {
        result = await api("/api/admin/channels/probe-models", {
          method: "POST",
          body: JSON.stringify({
            ...draft,
            key: keyInput,
          }),
        });
      }
    }
    if (generation !== channels.authGeneration()) return;
    if (oauth && channels.oauthProbePath(form) !== oauthPath) {
      channels.refreshAuthLabels();
      probeResult.textContent = t("oauthLoginRequired");
      probeResult.className = "inline-result error";
      return;
    }
    if (revision !== channelModelsRevision || initialModels !== form.elements.models.value) {
      probeResult.textContent = t("probeModelsInputChanged");
      probeResult.className = "inline-result warning";
      return;
    }
    form.elements.models.value = result.models.join(", ");
    probeResult.textContent = `${result.models.length} ${t("foundModels")} ${result.latency_ms} ms`;
    probeResult.className = "inline-result success";
  } catch (e) {
    if (generation !== channels.authGeneration()) return;
    probeResult.textContent = oauth ? t("oauthProbeFailed") : e.message;
    probeResult.className = "inline-result error";
  } finally {
    if (authRequest) await channels.finishAuthStateRequest(authRequest);
    if (generation === channels.authGeneration()) {
      channels.setModelsProbing(false);
      button.innerHTML = original;
      refreshIcons(button);
    }
  }
});

document.getElementById("channelForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const submitBtn = formElement.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  const generation = channels.authGeneration();
  let finishGeneration = generation;
  const original = submitBtn.innerHTML;
  submitBtn.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("saving")}`;
  refreshIcons(submitBtn);
  try {
    const form = new FormData(formElement);
    const extra = parseJson(form.get("extra"), {});
    const oauth = channels.usesOAuth(formElement);
    if (!oauth && channelEditState.authType === "oauth" && !form.get("key")?.trim()) {
      throw new Error(t("apiKeyRequired"));
    }
    const payload = {
      ...channelConnection(formElement, extra),
      ...channels.oauthSaveFields(formElement),
      name: form.get("name"),
      key: oauth ? undefined : form.get("key") || undefined,
      models: parseCsv(form.get("models")),
      model_mapping: parseJson(form.get("model_mapping"), {}),
      priority: Number(form.get("priority") || 1),
      weight: Number(form.get("weight") || 1),
      enabled: Boolean(form.get("enabled")),
    };
    const editingId = channelEditState.id;
    const method = editingId ? "PUT" : "POST";
    const url = editingId ? `/api/admin/channels/${editingId}` : "/api/admin/channels";
    await api(url, { method, body: JSON.stringify(payload) });
    if (generation !== channels.authGeneration()) return;
    channels.cancelOAuthLogin({ consumed: true });
    formElement.reset();
    applyChannelPreset(formElement.elements.type.value, true);
    channelModal.classList.add("hidden");
    toast(`${t("channels")} "${payload.name}" ${editingId ? t("updated") : t("created")}`, "success");
    channelEditState.id = null;
    channelEditState.authType = "api_key";
    channelEditState.authStatus = null;
    channels.changeCredentialType(formElement);
    finishGeneration = channels.authGeneration();
    if (current === "channels") await channels.load();
  } catch (e) {
    if (generation !== channels.authGeneration()) return;
    toast(e.message, "error");
  } finally {
    if (finishGeneration === channels.authGeneration()) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = original;
      refreshIcons(submitBtn);
    }
  }
});

/* ---------- theme/locale change → re-render ---------- */
document.addEventListener("themechange", () => {
  refreshTheme();
});
document.addEventListener("localechange", () => {
  // refresh icons that may have been swapped
  refreshIcons(document);
  channels.refreshAuthLabels();
  // re-render current page to apply new labels
  refreshActive();
  chat.renderHistory();
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[current]?.() || current;
});

/* ---------- keyboard ---------- */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!channelModal.classList.contains("hidden")) channels.closeForm();
    document.getElementById("chatDeleteModal")?.classList.add("hidden");
    closeSidebar();
  }
});

document.querySelectorAll("[data-admin-only]").forEach((element) => { element.hidden = !isAdmin(); });
document.querySelectorAll(".panel").forEach((element) => element.classList.toggle("active", element.id === current));
document.querySelectorAll(".nav-item[data-tab]").forEach((element) => element.classList.toggle("active", element.dataset.tab === current));
document.getElementById("content")?.classList.toggle("chat-content", current === "chat");
document.querySelector(".main")?.classList.toggle("chat-main", current === "chat");
document.querySelector(".app-shell")?.classList.toggle("chat-app", current === "chat");
document.getElementById("pageTitle").textContent = TITLES[current]();
document.addEventListener("sessionended", () => {
  for (const page of Object.values(PAGES)) page.unload?.();
  chat.resetSession();
  document.querySelectorAll(".panel").forEach((element) => { element.innerHTML = ""; });
  document.querySelectorAll("[data-admin-only]").forEach((element) => { element.hidden = true; });
});

const entryUrl = new URL(window.location.href);
if (entryUrl.searchParams.get("settings") === "security") {
  entryUrl.searchParams.delete("settings");
  window.history.replaceState(null, "", `${entryUrl.pathname}${entryUrl.search}${entryUrl.hash}`);
  settings?.open("security");
}

/* ---------- bootstrap ---------- */
function initIcons() {
  // wait for lucide CDN if needed
  if (window.lucide) {
    refreshIcons(document);
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      if (window.lucide) refreshIcons(document);
    });
  }
}

async function loadServerVersion() {
  const pill = document.getElementById("versionPill");
  if (!pill) return;
  try {
    const info = await api("/api");
    if (info?.version) pill.textContent = `v${info.version}`;
  } catch (error) {
    // Keep the pill empty when the server cannot be reached.
  }
}

initIcons();
applyLocale();
chat.renderHistory();
loadServerVersion();
(isAdmin() ? loadChannelPresets() : Promise.resolve()).catch((error) => toast(error.message, "error"));
refreshActive();
initialized = true;
