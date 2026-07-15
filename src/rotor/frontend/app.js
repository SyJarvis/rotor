// App entry — router, sidebar/topbar wiring, modal, theme/locale listeners.

import { t, applyLocale, toggleLocale } from "./i18n.js";
import { toggleTheme } from "./theme.js";
import { api, copyText } from "./api.js";
import { parseCsv, parseJson, refreshIcons, toast } from "./ui.js";
import { refreshTheme } from "./charts.js?v=8";

import * as overview from "./pages/overview.js";
import * as channels from "./pages/channels.js";
import { editState as channelEditState } from "./pages/channels.js";
import * as tokens from "./pages/tokens.js?v=8";
import * as usage from "./pages/usage.js?v=8";
import * as logs from "./pages/logs.js";

const PAGES = { overview, channels, tokens, usage, logs };
const TITLES = {
  overview: () => t("overview"),
  channels: () => t("channels"),
  tokens: () => t("apiKeys"),
  usage: () => t("usage"),
  logs: () => t("logs"),
};

let current = "overview";
let initialized = false;
let channelPresets = [];

async function loadChannelPresets() {
  channelPresets = await api("/api/admin/channels/presets");
  const select = document.getElementById("channelForm")?.elements.type;
  if (!select) return;
  const selected = select.value;
  select.innerHTML = channelPresets.map((preset) =>
    `<option value="${preset.id}">${preset.label}</option>`
  ).join("");
  select.value = channelPresets.some((preset) => preset.id === selected)
    ? selected
    : channelPresets[0]?.id || "openai";
  applyChannelPreset(select.value);
}

function applyChannelPreset(provider) {
  const preset = channelPresets.find((item) => item.id === provider);
  const form = document.getElementById("channelForm");
  if (!preset || !form) return;
  form.elements.base_url.value = preset.base_url;
  form.elements.protocol.value = preset.protocol;
  form.elements.models_path.value = preset.models_path;
  form.elements.request_path.value = preset.request_path;
  form.elements.auth_type.value = preset.auth_type;
}

export async function refreshActive() {
  const page = PAGES[current];
  if (page) {
    try { await page.load(); }
    catch (e) { toast(e.message, "error"); }
  }
}

function switchTab(tab) {
  if (current === tab) return;
  current = tab;
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
  document.querySelectorAll(".panel").forEach((el) => {
    el.classList.toggle("active", el.id === tab);
  });
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[tab]?.() || tab;
  document.title = `${TITLES[tab]?.()} · Rotor`;
  refreshActive();
}

/* ---------- navigation ---------- */
document.querySelectorAll(".nav-item").forEach((el) => {
  el.addEventListener("click", () => switchTab(el.dataset.tab));
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
document.getElementById("closeChannelModal")?.addEventListener("click", () => {
  channelModal.classList.add("hidden");
});

/* ---------- settings modal ---------- */
const settingsModal = document.getElementById("settingsModal");
const settingsForm = document.getElementById("settingsForm");

async function openSettings() {
  settingsModal.classList.remove("hidden");
  closeSidebar();
  try {
    const currentSettings = await api("/api/admin/settings");
    settingsForm.elements.strategy.value = currentSettings.routing.strategy;
    settingsForm.elements.affinity_enabled.checked =
      currentSettings.routing.affinity_enabled;
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
    await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        routing: {
          strategy: settingsForm.elements.strategy.value,
          affinity_enabled: settingsForm.elements.affinity_enabled.checked,
        },
      }),
    });
    settingsModal.classList.add("hidden");
    toast(t("settingsSaved"), "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
});

document.getElementById("probeModels")?.addEventListener("click", async () => {
  const form = new FormData(document.getElementById("channelForm"));
  const payload = {
    base_url: form.get("base_url"),
    key: form.get("key"),
    type: form.get("type"),
    protocol: form.get("protocol"),
    models_path: form.get("models_path"),
    auth_type: form.get("auth_type"),
  };
  if (!payload.base_url || !payload.key) {
    toast(t("requireProbeFields"), "warning");
    return;
  }
  try {
    const result = await api("/api/admin/channels/probe-models", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    document.getElementById("channelForm").elements.models.value = result.models.join(", ");
    const probeResult = document.getElementById("probeResult");
    probeResult.textContent = `${result.models.length} ${t("foundModels")} ${result.latency_ms} ms`;
    probeResult.className = "inline-result success";
  } catch (e) {
    const probeResult = document.getElementById("probeResult");
    probeResult.textContent = e.message;
    probeResult.className = "inline-result error";
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
      name: form.get("name"),
      type: form.get("type"),
      key: form.get("key") || undefined,
      base_url: form.get("base_url"),
      models: parseCsv(form.get("models")),
      model_mapping: parseJson(form.get("model_mapping"), {}),
      priority: Number(form.get("priority") || 1),
      weight: Number(form.get("weight") || 1),
      enabled: Boolean(form.get("enabled")),
      test_only: false,
      protocol: form.get("protocol"),
      extra: {
        ...extra,
        models_path: form.get("models_path"),
        request_path: form.get("request_path"),
        auth_type: form.get("auth_type"),
      },
    };
    const editingId = channelEditState.id;
    const method = editingId ? "PUT" : "POST";
    const url = editingId ? `/api/admin/channels/${editingId}` : "/api/admin/channels";
    await api(url, { method, body: JSON.stringify(payload) });
    formElement.reset();
    applyChannelPreset(formElement.elements.type.value);
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
  const titleEl = document.getElementById("pageTitle");
  if (titleEl) titleEl.textContent = TITLES[current]?.() || current;
});

/* ---------- keyboard ---------- */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!channelModal.classList.contains("hidden")) channelModal.classList.add("hidden");
    if (!settingsModal.classList.contains("hidden")) settingsModal.classList.add("hidden");
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
loadChannelPresets().catch((error) => toast(error.message, "error"));
refreshActive();
initialized = true;
