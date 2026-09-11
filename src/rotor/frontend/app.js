// App entry — router, sidebar/topbar wiring, modal, theme/locale listeners.

import { t, applyLocale, toggleLocale } from "./i18n.js";
import { toggleTheme } from "./theme.js";
import { api, copyText } from "./api.js?v=2";
import { parseCsv, parseJson, refreshIcons, toast } from "./ui.js?v=12";
import { refreshTheme } from "./charts.js?v=8";
import { setChannelPresets, applyChannelDefaults, channelConnection } from "./channel-form.js";

import * as overview from "./pages/overview.js?v=5";
import * as channels from "./pages/channels.js";
import { editState as channelEditState } from "./pages/channels.js";
import * as tokens from "./pages/tokens.js?v=9";
import * as usage from "./pages/usage.js?v=13";
import * as logs from "./pages/logs.js?v=4";
import * as monitoring from "./pages/monitoring.js?v=6";
import * as mindagent from "./pages/mindagent.js?v=15";

const PAGES = { overview, channels, tokens, usage, logs, monitoring, mindagent };
const TITLES = {
  overview: () => t("overview"),
  channels: () => t("channels"),
  tokens: () => t("apiKeys"),
  usage: () => t("usage"),
  logs: () => t("logs"),
  monitoring: () => t("monitoringTitle"),
  mindagent: () => t("mindagentChat"),
};

let current = "overview";
let initialized = false;
let channelPresets = [];

async function loadChannelPresets() {
  channelPresets = await api("/api/admin/channels/presets");
  setChannelPresets(channelPresets);
  const select = document.getElementById("channelForm")?.elements.type;
  if (!select) return;
  const selected = select.value;
  select.innerHTML = channelPresets.map((preset) =>
    `<option value="${preset.id}">${preset.label}</option>`
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
  if (current === tab) return refreshActive();
  PAGES[current]?.unload?.();
  current = tab;
  document.getElementById("content")?.classList.toggle("mindagent-content", tab === "mindagent");
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
  document.querySelectorAll(".panel").forEach((el) => {
    el.classList.toggle("active", el.id === tab);
  });
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[tab]?.() || tab;
  document.title = `${TITLES[tab]?.()} · Rotor`;
  return refreshActive();
}

/* ---------- navigation ---------- */
document.querySelectorAll(".nav-item").forEach((el) => {
  el.addEventListener("click", () => switchTab(el.dataset.tab));
});
document.addEventListener("mindagentconversationopen", () => switchTab("mindagent"));
document.addEventListener("logsopen", async (event) => {
  if (event.detail?.failedOnly) logs.showFailures?.();
  await switchTab("logs");
});

document.getElementById("themeToggle")?.addEventListener("click", () => {
  toggleTheme();
});
document.getElementById("localeToggle")?.addEventListener("click", toggleLocale);
document.getElementById("refreshBtn")?.addEventListener("click", () => refreshActive());

/* ---------- mobile sidebar ---------- */
const sidebar = document.getElementById("sidebar");
const menuToggle = document.getElementById("menuToggle");
let overlay = null;
function openSidebar() {
  sidebar?.classList.add("open");
  overlay = document.createElement("div");
  overlay.className = "sidebar-overlay";
  overlay.addEventListener("click", closeSidebar);
  document.body.appendChild(overlay);
}
function closeSidebar() {
  sidebar?.classList.remove("open");
  overlay?.remove();
  overlay = null;
}
menuToggle?.addEventListener("click", () => {
  sidebar?.classList.contains("open") ? closeSidebar() : openSidebar();
});

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
    modalClose.closest(".modal")?.classList.add("hidden");
    return;
  }

  // page-specific handlers
  for (const page of Object.values(PAGES)) {
    if (page.onClick && await page.onClick(target)) return;
  }
});

/* ---------- channel modal ---------- */
const channelModal = document.getElementById("channelModal");
document.getElementById("channelForm")?.elements.type.addEventListener("change", (event) => {
  applyChannelPreset(event.target.value);
});

document.getElementById("channelForm")?.elements.protocol.addEventListener("change", (event) => {
  const form = document.getElementById("channelForm");
  if (!form) return;
  applyChannelDefaults(form);
});
document.getElementById("closeChannelModal")?.addEventListener("click", () => {
  channelModal.classList.add("hidden");
});

/* ---------- settings modal ---------- */
const settingsModal = document.getElementById("settingsModal");
const settingsForm = document.getElementById("settingsForm");
let currentApplicationSettings = null;

async function openSettings() {
  settingsModal.classList.remove("hidden");
  closeSidebar();
  try {
    const currentSettings = await api("/api/admin/settings");
    currentApplicationSettings = currentSettings;
    settingsForm.elements.strategy.value = currentSettings.routing.strategy;
    settingsForm.elements.affinity_enabled.checked =
      currentSettings.routing.affinity_enabled;
    settingsForm.elements.session_lease_enabled.checked =
      currentSettings.routing.session_lease_enabled;
    settingsForm.elements.session_lease_idle_ttl_seconds.value =
      currentSettings.routing.session_lease_idle_ttl_seconds;
    settingsForm.elements.display_timezone.value =
      currentSettings.display_timezone;
    document.getElementById("settingsPath").textContent =
      currentSettings.settings_path;
  } catch (error) {
    settingsModal.classList.add("hidden");
    toast(error.message, "error");
  }
}

document.getElementById("openSettings")?.addEventListener("click", openSettings);
settingsForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitBtn = event.currentTarget.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  try {
    const savedSettings = await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        routing: {
          ...(currentApplicationSettings?.routing || {}),
          strategy: settingsForm.elements.strategy.value,
          affinity_enabled: settingsForm.elements.affinity_enabled.checked,
          session_lease_enabled:
            settingsForm.elements.session_lease_enabled.checked,
          session_lease_idle_ttl_seconds: Number(
            settingsForm.elements.session_lease_idle_ttl_seconds.value,
          ),
        },
        display_timezone: settingsForm.elements.display_timezone.value,
      }),
    });
    for (const page of Object.values(PAGES)) {
      page.setDisplayTimezone?.(savedSettings.display_timezone);
    }
    currentApplicationSettings = savedSettings;
    settingsModal.classList.add("hidden");
    toast(t("settingsSaved"), "success");
    refreshActive();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
});

document.getElementById("probeModels")?.addEventListener("click", async (event) => {
  const form = document.getElementById("channelForm");
  const keyInput = form?.elements.key?.value?.trim();
  const channelId = channelEditState.id;
  // Editing a saved channel with a blank key field → probe with the stored key.
  const useSaved = channelId && !keyInput;
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  const original = button.innerHTML;
  button.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("probing") || "..."}`;
  refreshIcons(button);
  try {
    const draft = channelConnection(form, parseJson(form.elements.extra.value, {}));
    let result;
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
    form.elements.models.value = result.models.join(", ");
    const probeResult = document.getElementById("probeResult");
    probeResult.textContent = `${result.models.length} ${t("foundModels")} ${result.latency_ms} ms`;
    probeResult.className = "inline-result success";
  } catch (e) {
    const probeResult = document.getElementById("probeResult");
    probeResult.textContent = e.message;
    probeResult.className = "inline-result error";
  } finally {
    button.disabled = false;
    button.innerHTML = original;
    refreshIcons(button);
  }
});

document.getElementById("channelForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const submitBtn = formElement.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  const original = submitBtn.innerHTML;
  submitBtn.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("saving")}`;
  refreshIcons(submitBtn);
  try {
    const form = new FormData(formElement);
    const extra = parseJson(form.get("extra"), {});
    const payload = {
      ...channelConnection(formElement, extra),
      name: form.get("name"),
      key: form.get("key") || undefined,
      models: parseCsv(form.get("models")),
      model_mapping: parseJson(form.get("model_mapping"), {}),
      priority: Number(form.get("priority") || 1),
      weight: Number(form.get("weight") || 1),
      enabled: Boolean(form.get("enabled")),
      test_only: false,
    };
    const editingId = channelEditState.id;
    const method = editingId ? "PUT" : "POST";
    const url = editingId ? `/api/admin/channels/${editingId}` : "/api/admin/channels";
    await api(url, { method, body: JSON.stringify(payload) });
    formElement.reset();
    applyChannelPreset(formElement.elements.type.value, true);
    channelModal.classList.add("hidden");
    toast(`${t("channels")} "${payload.name}" ${editingId ? t("updated") : t("created")}`, "success");
    channelEditState.id = null;
    if (current === "channels") await channels.load();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerHTML = original;
    refreshIcons(submitBtn);
  }
});

/* ---------- theme/locale change → re-render ---------- */
document.addEventListener("themechange", () => {
  refreshTheme();
});
document.addEventListener("localechange", () => {
  // refresh icons that may have been swapped
  refreshIcons(document);
  // re-render current page to apply new labels
  refreshActive();
  mindagent.renderHistory();
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[current]?.() || current;
});

/* ---------- keyboard ---------- */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!channelModal.classList.contains("hidden")) channelModal.classList.add("hidden");
    if (!settingsModal.classList.contains("hidden")) settingsModal.classList.add("hidden");
    document.getElementById("mindagentDeleteModal")?.classList.add("hidden");
    closeSidebar();
  }
});

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

initIcons();
applyLocale();
mindagent.renderHistory();
loadChannelPresets().catch((error) => toast(error.message, "error"));
refreshActive();
initialized = true;
