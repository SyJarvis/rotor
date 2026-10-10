// Browser preferences apply immediately; server settings keep a draft until saved.
import { api } from "./api.js?v=3";
import { isAdmin } from "./session.js?v=1";
import { initAccount } from "./account.js?v=2";
import { initSecurity } from "./security.js?v=1";
import { t, locale, toggleLocale } from "./i18n.js";
import { currentTheme, setTheme } from "./theme.js";
import { closeSidebar } from "./sidebar.js?v=1";

const FIELDS = ["strategy", "affinity_enabled", "session_lease_enabled", "session_lease_idle_ttl_seconds", "display_timezone"];
const CHECKBOXES = new Set(["affinity_enabled", "session_lease_enabled"]);
function visibleSettings(settings) {
  return Object.fromEntries(FIELDS.map((name) => {
    const value = name === "display_timezone" ? settings[name] : settings.routing[name];
    return [name, CHECKBOXES.has(name) ? Boolean(value) : String(value)];
  }));
}

export function initSettings({ onSaved = () => {} } = {}) {
  const modal = document.getElementById("settingsModal");
  const form = document.getElementById("settingsForm");
  if (!modal || !form) return;
  const get = (id) => document.getElementById(id);
  const tabs = [...modal.querySelectorAll("[data-settings-tab]")];
  const panels = [...modal.querySelectorAll("[data-settings-panel]")];
  const themeButtons = [...modal.querySelectorAll("[data-settings-theme]")];
  const serverFields = [...form.querySelectorAll("[data-server-setting]")];
  const shell = document.querySelector(".app-shell");
  const account = initAccount();
  const security = initSecurity();
  let baseline = null, draft = null, tab = "general", opened = false;
  let loading = null, saving = false, revision = 0, loadEpoch = 0;
  let loadError = "", statusKey = "", saveError = "", returnFocus = null, background = null;

  const dirty = () => baseline && draft && JSON.stringify(draft) !== JSON.stringify(visibleSettings(baseline));
  function writeForm() {
    if (!draft) return;
    for (const name of FIELDS) {
      form.elements[name][CHECKBOXES.has(name) ? "checked" : "value"] = draft[name];
    }
    get("settingsPath").textContent = baseline?.settings_path || "—";
  }
  function render() {
    if (!isAdmin() && tab === "routing") tab = "general";
    get("settingsGeneralDescription").textContent = t(isAdmin() ? "generalSettingsDescription" : "personalSettingsDescription");
    modal.querySelectorAll("[data-admin-setting]").forEach((element) => { element.hidden = !isAdmin(); });
    get("settingsFooter").hidden = !isAdmin() || ["account", "security"].includes(tab);
    themeButtons.forEach((button) => {
      const active = button.dataset.settingsTheme === currentTheme();
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    get("settingsLocale").value = locale;
    get("settingsClose").setAttribute("aria-label", t("close"));
    modal.querySelector(".settings-nav").setAttribute("aria-label", t("settingsSections"));
    tabs.forEach((button) => {
      const active = button.dataset.settingsTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== tab; });
    const unavailable = !baseline || Boolean(loading) || Boolean(loadError) || saving;
    serverFields.forEach((field) => { field.disabled = unavailable; });
    get("settingsSave").disabled = unavailable || !dirty();
    get("settingsSave").setAttribute("aria-busy", String(saving));
    get("settingsRetry").disabled = Boolean(loading) || saving;
    get("settingsRetry").hidden = !loadError;
    get("settingsLoadNotice").classList.toggle("hidden", ["account", "security"].includes(tab) || (!loading && !loadError));
    get("settingsLoadMessage").textContent = loading ? t("settingsLoading") : loadError ? `${t("settingsLoadFailed")} ${loadError}` : "";
    get("settingsStatus").textContent = saving ? t("saving") : statusKey ? `${t(statusKey)}${saveError ? ` ${saveError}` : ""}` : dirty() ? t("settingsDirty") : "";
    get("settingsStatus").classList.toggle("error", Boolean(saveError) || statusKey.startsWith("settingsInvalid"));
  }
  async function load() {
    if (!isAdmin() || loading || saving || dirty()) return;
    const epoch = ++loadEpoch;
    const initialRevision = revision;
    const controller = new AbortController();
    loading = controller;
    statusKey = saveError = "";
    loadError = "";
    render();
    try {
      const result = await api("/api/admin/settings", { signal: controller.signal });
      if (epoch !== loadEpoch || initialRevision !== revision || !opened) return;
      baseline = result;
      draft = visibleSettings(result);
      writeForm();
    } catch (error) {
      if (epoch !== loadEpoch || !opened) return;
      loadError = error.message;
    } finally {
      if (epoch === loadEpoch) { loading = null; render(); }
    }
  }
  function focusable() {
    return [...modal.querySelectorAll('button, input, select, [tabindex="0"]')]
      .filter((element) => !element.disabled && !element.closest("[hidden]") && element.getClientRects().length);
  }
  function open(section) {
    if (tabs.some((button) => button.dataset.settingsTab === section && !button.hidden)) tab = section;
    if (opened) { render(); if (tab === "security") security.open(); else security.close(); return; }
    opened = true;
    returnFocus = document.activeElement;
    closeSidebar();
    background = { inert: shell?.inert, overflow: document.body.style.overflow, paddingRight: document.body.style.paddingRight };
    if (shell) shell.inert = true;
    const gap = Math.max(0, window.innerWidth - document.documentElement.clientWidth);
    if (gap) document.body.style.paddingRight = `${(parseFloat(getComputedStyle(document.body).paddingRight) || 0) + gap}px`;
    document.body.style.overflow = "hidden";
    modal.classList.remove("hidden");
    render();
    get("settingsClose").focus();
    account.load();
    if (tab === "security") security.open();
    if (isAdmin() && !dirty() && !saving) load();
  }
  function close(restoreFocus = true) {
    if (!opened) return;
    opened = false;
    security.close(); account.close();
    ++loadEpoch;
    loading?.abort();
    loading = null;
    modal.classList.add("hidden");
    if (shell) shell.inert = background.inert;
    document.body.style.overflow = background.overflow;
    document.body.style.paddingRight = background.paddingRight;
    const target = returnFocus !== document.body && returnFocus?.getClientRects().length && !returnFocus.closest("[inert]") ? returnFocus
      : [get("menuToggle"), get("openSettings")].find((button) => button?.getClientRects().length && !button.closest("[inert]"));
    if (restoreFocus) target?.focus();
  }
  function updateDraft(event) {
    if (!draft || saving || !FIELDS.includes(event.target.name) || event.target.disabled) return;
    const name = event.target.name;
    draft[name] = CHECKBOXES.has(name) ? event.target.checked : event.target.value;
    ++revision;
    statusKey = saveError = "";
    render();
  }
  function invalid(key, name, category) {
    statusKey = key;
    tab = category;
    render();
    form.elements[name].focus();
  }
  async function save(event) {
    event.preventDefault();
    if (!isAdmin() || ["account", "security"].includes(tab) || saving || loading || loadError || !dirty()) return;
    const ttl = Number(draft.session_lease_idle_ttl_seconds);
    if (!Number.isInteger(ttl) || ttl < 60 || ttl > 86400) return invalid("settingsInvalidTtl", "session_lease_idle_ttl_seconds", "routing");
    const timezone = draft.display_timezone.trim();
    try {
      if (!timezone || /^[+-]/.test(timezone)) throw new Error();
      new Intl.DateTimeFormat("en", { timeZone: timezone });
    } catch { return invalid("settingsInvalidTimezone", "display_timezone", "general"); }
    const payload = {
      routing: { ...baseline.routing, strategy: draft.strategy, affinity_enabled: draft.affinity_enabled,
        session_lease_enabled: draft.session_lease_enabled, session_lease_idle_ttl_seconds: ttl },
      display_timezone: timezone,
    };
    const initialRevision = revision;
    saving = true;
    statusKey = saveError = "";
    render();
    try {
      const result = await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(payload) });
      baseline = result;
      if (revision === initialRevision) { draft = visibleSettings(result); writeForm(); }
      statusKey = "settingsSaved";
      onSaved(result);
    } catch (error) {
      statusKey = "settingsSaveFailed";
      saveError = error.message;
    } finally { saving = false; render(); }
  }

  get("openSettings")?.addEventListener("click", (event) => { event.stopPropagation(); open(); });
  modal.addEventListener("click", (event) => {
    event.stopPropagation();
    if (event.target.closest("[data-settings-close]")) close();
    const button = event.target.closest("[data-settings-tab]");
    if (button) { tab = button.dataset.settingsTab; render(); if (tab === "security") security.open(); else security.close(); }
  });
  modal.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const elements = focusable();
      const first = elements[0], last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    if (event.target.closest("[data-settings-tab]") && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const availableTabs = tabs.filter((button) => !button.hidden);
      const index = availableTabs.findIndex((button) => button.dataset.settingsTab === tab);
      const next = event.key === "Home" ? availableTabs[0] : event.key === "End" ? availableTabs.at(-1) : availableTabs[(index + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + availableTabs.length) % availableTabs.length];
      tab = next.dataset.settingsTab; render(); if (tab === "security") security.open(); else security.close(); next.focus();
    }
  });
  window.addEventListener("rotorauthrequired", () => close(false));
  document.addEventListener("sessionended", () => close(false));
  document.addEventListener("focusin", (event) => {
    if (opened && !modal.contains(event.target)) get("settingsClose").focus();
  });
  form.addEventListener("input", updateDraft);
  form.addEventListener("change", updateDraft);
  form.addEventListener("submit", save);
  get("settingsRetry").addEventListener("click", load);
  themeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.settingsTheme !== currentTheme()) setTheme(button.dataset.settingsTheme);
    });
  });
  get("settingsLocale").addEventListener("change", (event) => {
    const selected = event.target.value;
    if (["zh-CN", "en"].includes(selected) && selected !== locale) toggleLocale();
  });
  document.addEventListener("localechange", render);
  document.addEventListener("themechange", render);
  render();
  return { open, close };
}
