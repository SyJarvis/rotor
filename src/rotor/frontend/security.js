import { api } from "./api.js?v=3";
import { t } from "./i18n.js";
import { actor, accountId, setActor, requestLogout } from "./session.js?v=1";

export function initSecurity() {
  const get = (id) => document.getElementById(id);
  const passwordFields = ["accountCurrentPassword", "accountNewPassword", "accountConfirmPassword"];
  const requests = new Set(), confirmations = new Set(), revoking = new Set(), errors = new Map();
  let opened = false, owner = accountId(), epoch = 0, grants = [], loaded = false, loading = false;
  let loadError = "", notice = "", passwordOpen = false, passwordPending = false, passwordStatus = "", passwordError = "";
  const current = (version, account) => opened && !!actor() && version === epoch && account === accountId();
  const text = (tag, value, className = "") => {
    const node = document.createElement(tag);
    node.textContent = value; node.className = className;
    return node;
  };
  function timestamp(value) {
    if (!value) return t("clientNeverUsed");
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(document.documentElement.lang);
  }
  function button(id, action, key, callback, disabled = false) {
    const node = text("button", t(key), action === "confirm" ? "btn-danger" : "btn-secondary");
    node.type = "button"; node.disabled = disabled;
    node.dataset.grantId = String(id); node.dataset.grantAction = action;
    node.addEventListener("click", callback);
    return node;
  }
  function focusGrant(id, action) {
    const buttons = [...get("securityDevicesList").querySelectorAll("button")];
    const target = buttons.find((node) => node.dataset.grantId === String(id) && node.dataset.grantAction === action && !node.disabled);
    if (target) target.focus();
    else if (action === "pending") [...get("securityDevicesList").children].find((node) => node.dataset.grantId === String(id))?.focus();
    else get("securityDevicesRefresh").focus();
  }
  function renderDevices(focus) {
    const active = document.activeElement;
    if (!focus && get("securityDevicesList").contains(active) && active?.dataset.grantId) {
      focus = [active.dataset.grantId, active.dataset.grantAction || "pending"];
    }
    get("securityDevicesCount").textContent = loaded ? t("deviceCount").replace("{count}", grants.length) : "";
    get("securityDevicesStatus").textContent = loading ? t("devicesLoading") : notice ? t(notice) : "";
    get("securityDevicesError").textContent = loadError;
    get("securityDevicesError").hidden = !loadError;
    get("securityDevicesRetry").hidden = !loadError;
    get("securityDevicesRetry").disabled = loading;
    get("securityDevicesRefresh").disabled = loading || revoking.size > 0;
    get("securityDevicesRefresh").setAttribute("aria-busy", String(loading));
    get("securityDevicesList").replaceChildren();
    if (loaded && !grants.length) {
      const empty = text("div", "", "security-devices-empty");
      empty.appendChild(text("p", t("devicesEmptyTitle")));
      empty.appendChild(text("p", t("devicesEmptyDescription"), "muted"));
      get("securityDevicesList").appendChild(empty);
    }
    for (const grant of grants) {
      const card = document.createElement("article"); card.className = "security-device-card";
      card.dataset.grantId = String(grant.id); card.tabIndex = -1;
      const heading = text("h5", grant.client_name); heading.id = `securityGrant-${grant.id}`;
      card.setAttribute("aria-labelledby", heading.id); card.appendChild(heading);
      const details = document.createElement("dl"); details.className = "security-device-details";
      for (const [key, value] of [["deviceAuthorizedAt", grant.created_at], ["clientLastUsed", grant.last_used_at], ["deviceExpiresAt", grant.expires_at]]) {
        details.appendChild(text("dt", t(key))); details.appendChild(text("dd", timestamp(value)));
      }
      card.appendChild(details);
      const actions = document.createElement("div"); actions.className = "security-device-actions";
      if (confirmations.has(grant.id)) {
        card.appendChild(text("p", t("deviceRevokeConfirm"), "security-device-confirm"));
        actions.appendChild(button(grant.id, "cancel", "cancel", () => { confirmations.delete(grant.id); errors.delete(grant.id); renderDevices([grant.id, "revoke"]); }, revoking.has(grant.id)));
        actions.appendChild(button(grant.id, "confirm", revoking.has(grant.id) ? "deviceRevoking" : "deviceConfirmRevoke", () => revoke(grant.id), revoking.has(grant.id)));
      } else {
        actions.appendChild(button(grant.id, "revoke", "clientRevoke", () => { confirmations.add(grant.id); renderDevices([grant.id, "cancel"]); }));
      }
      card.appendChild(actions);
      if (errors.has(grant.id)) {
        const error = text("p", errors.get(grant.id), "auth-error"); error.setAttribute("role", "alert"); card.appendChild(error);
      }
      get("securityDevicesList").appendChild(card);
    }
    if (focus && opened) focusGrant(...focus);
  }
  function renderPassword() {
    get("securityPasswordPanel").hidden = !passwordOpen;
    get("securityPasswordToggle").setAttribute("aria-expanded", String(passwordOpen));
    get("securityPasswordToggle").disabled = passwordPending;
    get("accountPasswordSave").disabled = passwordPending;
    get("securityPasswordCancel").disabled = passwordPending;
    passwordFields.forEach((id) => { get(id).disabled = passwordPending; });
    get("accountPasswordSave").setAttribute("aria-busy", String(passwordPending));
    get("securityPasswordStatus").textContent = passwordPending ? t("saving") : passwordError || (passwordStatus ? t(passwordStatus) : "");
    get("securityPasswordStatus").classList.toggle("error", !!passwordError);
  }
  function clearPassword() {
    passwordFields.forEach((id) => { get(id).value = ""; });
    passwordOpen = false; passwordPending = false; passwordStatus = passwordError = "";
    renderPassword();
  }
  function reset() {
    ++epoch;
    requests.forEach((controller) => controller.abort()); requests.clear();
    grants = []; loaded = loading = false; loadError = notice = "";
    confirmations.clear(); revoking.clear(); errors.clear();
    clearPassword(); renderDevices();
  }
  async function request(path, options = {}) {
    const controller = new AbortController(); requests.add(controller);
    try { return await api(path, { ...options, signal: controller.signal, cache: "no-store" }); }
    finally { requests.delete(controller); }
  }
  async function load() {
    if (!opened || !actor() || loading || revoking.size) return;
    const version = epoch, account = accountId();
    loading = true; loaded = false; grants = []; loadError = notice = ""; confirmations.clear(); errors.clear(); renderDevices();
    try {
      const result = await request("/api/oauth/grants");
      if (!current(version, account)) return;
      if (!Array.isArray(result.items) || !result.items.every((grant) => Number.isInteger(grant.id) && grant.id > 0 && typeof grant.client_name === "string")) throw new Error(t("devicesLoadFailed"));
      grants = result.items; loaded = true;
    } catch (error) { if (current(version, account)) loadError = error.message || t("devicesLoadFailed"); }
    finally { if (current(version, account)) { loading = false; renderDevices(); } }
  }
  async function revoke(id) {
    if (!opened || !actor() || !confirmations.has(id) || revoking.has(id)) return;
    const version = epoch, account = accountId(), index = grants.findIndex((grant) => grant.id === id);
    revoking.add(id); errors.delete(id); notice = ""; renderDevices([id, "pending"]);
    let removed = false;
    try {
      await request(`/api/oauth/grants/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (!current(version, account)) return;
      grants = grants.filter((grant) => grant.id !== id); confirmations.delete(id); removed = true; notice = "clientRevoked";
    } catch (error) { if (current(version, account)) errors.set(id, error.message || t("clientRevokeFailed")); }
    finally {
      if (current(version, account)) {
        revoking.delete(id);
        const neighbor = grants[Math.min(index, grants.length - 1)];
        renderDevices(removed ? [neighbor?.id, neighbor && confirmations.has(neighbor.id) ? "cancel" : "revoke"] : [id, "confirm"]);
      }
    }
  }
  async function savePassword() {
    if (!opened || !actor() || !passwordOpen || passwordPending) return;
    const currentPassword = get("accountCurrentPassword").value, newPassword = get("accountNewPassword").value;
    passwordStatus = passwordError = "";
    if (newPassword.length < 6) { passwordError = t("passwordMinimum"); renderPassword(); return; }
    if (newPassword !== get("accountConfirmPassword").value) { passwordError = t("passwordMismatch"); renderPassword(); return; }
    const version = epoch, account = accountId(); passwordPending = true; renderPassword();
    try {
      const result = await request("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
      if (!current(version, account)) return;
      setActor({ ...actor(), ...result });
      passwordFields.forEach((id) => { get(id).value = ""; }); passwordStatus = "passwordUpdated";
    } catch (error) { if (current(version, account)) passwordError = error.message; }
    finally { if (current(version, account)) { passwordPending = false; renderPassword(); } }
  }
  get("securityPasswordToggle").addEventListener("click", () => {
    if (passwordOpen) clearPassword();
    else { passwordOpen = true; passwordStatus = passwordError = ""; renderPassword(); get("accountCurrentPassword").focus(); }
  });
  get("securityPasswordCancel").addEventListener("click", () => { clearPassword(); get("securityPasswordToggle").focus(); });
  get("accountPasswordSave").addEventListener("click", savePassword);
  get("securityPasswordPanel").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && passwordFields.some((id) => get(id) === event.target)) { event.preventDefault(); event.stopPropagation(); void savePassword(); }
  });
  get("accountLogout").addEventListener("click", requestLogout);
  get("securityDevicesRefresh").addEventListener("click", load);
  get("securityDevicesRetry").addEventListener("click", load);
  document.addEventListener("sessionended", () => { opened = false; owner = ""; reset(); });
  document.addEventListener("sessionchange", () => {
    if (accountId() === owner) return;
    owner = accountId(); reset(); if (opened && actor()) void load();
  });
  document.addEventListener("localechange", () => { renderDevices(); renderPassword(); });
  renderPassword(); renderDevices();
  return {
    open() { if (opened) return; opened = true; owner = accountId(); void load(); },
    close() { opened = false; reset(); },
  };
}
