// Channels page — card grid with filter + actions.

import { api } from "../api.js?v=3";
import { t } from "../i18n.js";
import { channelDefaults, rememberChannelDefaults, applyChannelDefaults } from "../channel-form.js?v=2";
import {
  escapeHtml, parseCsv, parseJson, badge, statusBadge,
  skeletonCards, showInline, hideInline, refreshIcons, toast,
} from "../ui.js";

let state = { channels: [], filter: { q: "", provider: "", status: "" }, testResult: {}, authSyncError: {}, openMenu: null, routingState: {}, routingError: null };

export const editState = { id: null, authType: "api_key", authStatus: null, authExpiresAt: null, authError: null, authSyncError: false };
let editRequest = 0;
let oauthSession = null;
let oauthGeneration = 0;
let oauthStarting = false;
let oauthParsing = false;
let oauthCompleting = false;
let oauthInputRevision = 0;
let oauthPreview = null;
let oauthNotice = null;
let modelsProbing = false;
const probingChannels = new Set();
const authRequests = new Map();
const OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback";

const OAUTH_CONNECTION = {
  type: "openai_codex", protocol: "openai_responses",
  base_url: "https://chatgpt.com/backend-api/codex",
};

export function usesOAuth(form) {
  return form?.elements.credential_type?.value === "oauth";
}

export function authGeneration() { return oauthGeneration; }

export function oauthProbePath(form) {
  if (!usesOAuth(form) || oauthStarting) return null;
  if (oauthSession) {
    if (Date.now() / 1000 >= oauthSession.expires_at) {
      oauthSession.status = "expired";
      oauthNotice = null;
      clearOAuthPreview();
      return null;
    }
    return oauthSession.status === "authorized"
      ? `/api/admin/channels/oauth/sessions/${encodeURIComponent(oauthSession.session_id)}/probe-models`
      : null;
  }
  return editState.id && editState.authType === "oauth" && ["connected", "temporary_error", "error"].includes(editState.authStatus)
    ? `/api/admin/channels/${editState.id}/probe-models` : null;
}

export function setModelsProbing(probing) {
  modelsProbing = probing;
  renderOAuth();
}

function oauthStatus(message, type = "") {
  const result = document.getElementById("channelOAuthStatus");
  if (result) {
    result.textContent = message;
    result.className = `inline-result ${type}`;
  }
}

function renderOAuth() {
  const form = document.getElementById("channelForm");
  const oauth = usesOAuth(form);
  const probe = document.getElementById("probeModels");
  if (probe) probe.disabled = modelsProbing || (oauth && !oauthProbePath(form));
  const login = document.getElementById("channelOAuthLogin");
  if (login) {
    login.disabled = oauthStarting || oauthCompleting || oauthSession?.status === "exchanging";
    login.textContent = t(oauthStarting ? "oauthStarting" : editState.authType === "oauth" ? "oauthRelogin" : "oauthLogin");
  }
  const pending = oauthSession?.status === "pending";
  const callbackVisible = pending || oauthSession?.status === "exchanging";
  const expired = oauthSession && Date.now() / 1000 >= oauthSession.expires_at;
  document.getElementById("channelOAuthLink")?.classList.toggle("hidden", !oauth || !callbackVisible);
  document.getElementById("channelOAuthCallbackStep")?.classList.toggle("hidden", !oauth || !callbackVisible);
  document.getElementById("channelOAuthPreview")?.classList.toggle("hidden", !oauthPreview);
  const input = document.getElementById("channelOAuthCallback");
  if (input) input.disabled = oauthCompleting;
  const parse = document.getElementById("channelOAuthParse");
  if (parse) parse.disabled = !pending || expired || oauthParsing || oauthCompleting || !input?.value.trim();
  const complete = document.getElementById("channelOAuthComplete");
  if (complete) complete.disabled = !pending || expired || oauthParsing || oauthCompleting
    || !oauthPreview || oauthPreview.callback_url !== input?.value.trim();
  const cancel = document.getElementById("channelOAuthCancel");
  cancel?.classList.toggle("hidden", !oauthSession && !oauthStarting);
  const check = document.getElementById("channelOAuthCheck");
  check?.classList.toggle("hidden", !oauth || !editState.id || editState.authType !== "oauth" || !!oauthSession || oauthStarting);
  const checking = authRequests.get(String(editState.id));
  if (check) check.disabled = !!currentAuthStateRequest(checking) && checking.checking;
  const details = document.getElementById("channelOAuthDetails");
  details?.classList.toggle("hidden", !oauth || !!oauthSession || oauthStarting || editState.authType !== "oauth");
  if (details) details.textContent = savedOAuthDetails({
    auth_status: editState.authStatus, auth_expires_at: editState.authExpiresAt, auth_error: editState.authError,
  }, editState.authSyncError);
  if (!oauth) return;
  if (oauthStarting) oauthStatus(t("oauthStarting"));
  else if (oauthCompleting) oauthStatus(t("oauthCompleting"));
  else if (oauthParsing) oauthStatus(t("oauthParsing"));
  else if (oauthNotice) oauthStatus(t(oauthNotice.key), oauthNotice.type);
  else if (oauthSession) {
    const statusKeys = {
      pending: "oauthPending", authorized: "oauthAuthorized", failed: "oauthFailed",
      expired: "oauthExpired", cancelled: "oauthCancelled", consumed: "oauthExpired", exchanging: "oauthCompleting",
    };
    const status = oauthSession.status;
    oauthStatus(t(status === "pending" && oauthPreview ? "oauthCallbackMatched" : statusKeys[status] || "oauthFailed"),
      status === "authorized" || (status === "pending" && oauthPreview) ? "success" : ["pending", "exchanging"].includes(status) ? "" : "error");
  } else if (editState.authType === "oauth") {
    const saved = savedOAuthState(editState.authStatus, editState.authSyncError);
    oauthStatus(t(saved.key), saved.type);
  } else oauthStatus(t("oauthLoginRequired"));
}

function savedOAuthState(status, unavailable = false) {
  if (unavailable) return { key: "oauthStatusUnavailable", type: "warning" };
  switch (status) {
    case "connected": return { key: "oauthSaved", type: "info" };
    case "reauth_required": return { key: "oauthReauthRequired", type: "error" };
    case "temporary_error":
    case "error": return { key: "oauthTemporaryError", type: "warning" };
    default: return { key: "oauthUnauthed", type: "warning" };
  }
}

function savedOAuthDetails(channel, unavailable = false) {
  if (unavailable) return t("oauthRetryCheck");
  if (!channel.auth_status || channel.auth_status === "unauthed") return "";
  const parts = [];
  const expiry = Number(channel.auth_expires_at);
  const date = new Date(expiry * 1000);
  if (expiry > 0 && Number.isFinite(date.getTime())) {
    parts.push(`${t("oauthExpiry")}: ${date.toLocaleString()} (${t("oauthLocalTime")})`);
  }
  parts.push(t("oauthAutoRefresh"));
  if (["temporary_error", "error"].includes(channel.auth_status)) parts.push(t("oauthRetryCheck"));
  if (channel.auth_error) parts.push(channel.auth_error);
  return parts.join(" · ");
}

export function beginAuthStateRequest(id) {
  if (!id) return null;
  const request = { id: String(id), loadGeneration, authGeneration: oauthGeneration, editRequest, checking: true };
  authRequests.set(request.id, request);
  return request;
}

function currentAuthStateRequest(request) {
  return request && authRequests.get(request.id) === request && request.loadGeneration === loadGeneration
    && (String(editState.id) !== request.id || request.authGeneration === oauthGeneration);
}

export async function finishAuthStateRequest(request) {
  if (!currentAuthStateRequest(request)) return false;
  try {
    const channel = await api(`/api/admin/channels/${request.id}`);
    if (!currentAuthStateRequest(request)) return false;
    if (String(channel.id) !== request.id || channel.auth_type !== "oauth") throw new Error("Unexpected channel authorization state");
    const metadata = { auth_status: channel.auth_status, auth_expires_at: channel.auth_expires_at, auth_error: channel.auth_error };
    const saved = state.channels.find((entry) => String(entry.id) === request.id);
    if (saved?.auth_type === "oauth") Object.assign(saved, metadata);
    delete state.authSyncError[request.id];
    if (String(editState.id) === request.id && request.editRequest === editRequest && !oauthSession && !oauthStarting) {
      Object.assign(editState, { authStatus: metadata.auth_status, authExpiresAt: metadata.auth_expires_at, authError: metadata.auth_error, authSyncError: false });
    }
    return true;
  } catch (error) {
    if (!currentAuthStateRequest(request)) return false;
    state.authSyncError[request.id] = true;
    if (String(editState.id) === request.id && request.editRequest === editRequest && !oauthSession && !oauthStarting) editState.authSyncError = true;
    return false;
  } finally {
    if (currentAuthStateRequest(request)) {
      request.checking = false;
      renderOAuth();
      if (active) render();
    }
  }
}

export async function checkEditedOAuth() {
  if (editState.id && editState.authType === "oauth" && !oauthSession && !oauthStarting) await runTest(editState.id);
}

export function cancelOAuthLogin({ consumed = false } = {}) {
  const sessionId = oauthSession?.session_id;
  oauthGeneration += 1;
  resetSaveButton();
  modelsProbing = false;
  const probe = document.getElementById("probeModels");
  if (probe) {
    probe.innerHTML = `<i data-lucide="radar"></i><span>${t("probeModels")}</span>`;
    refreshIcons(probe);
  }
  const probeResult = document.getElementById("probeResult");
  if (probeResult) {
    probeResult.textContent = "";
    probeResult.className = "inline-result hidden";
  }
  oauthSession = null;
  oauthStarting = false;
  oauthParsing = false;
  oauthCompleting = false;
  oauthNotice = null;
  oauthInputRevision += 1;
  clearOAuthPreview();
  const input = document.getElementById("channelOAuthCallback");
  if (input) input.value = "";
  const link = document.getElementById("channelOAuthUrl");
  if (link) {
    link.textContent = "";
    link.removeAttribute?.("href");
  }
  if (sessionId && !consumed) {
    api(`/api/admin/channels/oauth/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }).catch(() => {});
  }
  renderOAuth();
}

function clearOAuthPreview() {
  oauthPreview = null;
  for (const id of ["channelOAuthCode", "channelOAuthState", "channelOAuthRedirect"]) {
    const el = document.getElementById(id);
    if (el) el.textContent = "";
  }
}

export function changeOAuthCallback() {
  oauthInputRevision += 1;
  oauthNotice = null;
  clearOAuthPreview();
  renderOAuth();
}

function oauthSessionReady() {
  if (!oauthSession || oauthSession.status !== "pending") return false;
  if (Date.now() / 1000 >= oauthSession.expires_at) {
    oauthSession.status = "expired";
    oauthNotice = null;
    clearOAuthPreview();
    renderOAuth();
    return false;
  }
  return true;
}

function resetSaveButton() {
  const button = document.getElementById("saveChannelBtn");
  if (button) {
    button.disabled = false;
    button.innerHTML = `<i data-lucide="check"></i><span>${t("saveChannel")}</span>`;
  }
}

export function closeForm() {
  editRequest += 1;
  cancelOAuthLogin();
  document.getElementById("channelModal")?.classList.add("hidden");
}

export function changeCredentialType(form, { resetConnection = true } = {}) {
  cancelOAuthLogin();
  const oauth = usesOAuth(form);
  for (const [name, id] of [
    ["type", "channelProviderField"], ["base_url", "channelBaseUrlField"],
    ["protocol", "channelProtocolField"], ["auth_type", "channelHeaderAuthField"],
    ["models_path", "channelModelsPathField"], ["request_path", "channelRequestPathField"],
    ["key", "channelKeyField"],
  ]) {
    if (form.elements[name]) form.elements[name].disabled = oauth;
    document.getElementById(id)?.classList.toggle("hidden", oauth);
  }
  document.getElementById("channelOAuthProviderField")?.classList.toggle("hidden", !oauth);
  document.getElementById("channelOAuthPanel")?.classList.toggle("hidden", !oauth);
  document.getElementById("probeModels")?.classList.remove("hidden");
  const key = form.elements.key;
  key.required = !oauth && (!editState.id || editState.authType === "oauth");
  key.placeholder = key.required ? "provider api key" : t("keepUnchanged");
  if (oauth) {
    key.value = "";
    for (const [name, value] of Object.entries(OAUTH_CONNECTION)) form.elements[name].value = value;
    form.elements.auth_type.value = "bearer";
    form.elements.models_path.value = "/models";
    form.elements.request_path.value = "/responses";
    rememberChannelDefaults(form);
  } else if (resetConnection && form.elements.type.value === "openai_codex") {
    form.elements.type.value = "openai";
    form.elements.protocol.value = "openai";
    applyChannelDefaults(form, { reset: true });
  }
  renderOAuth();
}

export async function startOAuthLogin() {
  const form = document.getElementById("channelForm");
  if (!usesOAuth(form) || oauthStarting || oauthCompleting) return;
  cancelOAuthLogin();
  const generation = oauthGeneration;
  oauthStarting = true;
  renderOAuth();
  let startedSessionId = null;
  try {
    const result = await api("/api/admin/channels/oauth/sessions", {
      method: "POST", body: JSON.stringify(editState.id ? { channel_id: editState.id } : {}),
    });
    if (generation !== oauthGeneration) {
      if (result.session_id) api(`/api/admin/channels/oauth/sessions/${encodeURIComponent(result.session_id)}`, { method: "DELETE" }).catch(() => {});
      return;
    }
    startedSessionId = result.session_id;
    const uri = new URL(result.authorization_url);
    if (uri.origin !== "https://auth.openai.com" || uri.pathname !== "/oauth/authorize"
      || uri.username || uri.password || uri.hash || result.redirect_uri !== OAUTH_REDIRECT_URI
      || uri.searchParams.get("redirect_uri") !== OAUTH_REDIRECT_URI || !uri.searchParams.get("state")) {
      throw new Error("Invalid authorization URL");
    }
    oauthSession = {
      session_id: result.session_id, status: result.status, expires_at: result.expires_at,
      authorization_url: uri.href,
    };
    document.getElementById("channelOAuthUrl").href = uri.href;
    document.getElementById("channelOAuthUrl").textContent = uri.href;
  } catch {
    if (generation !== oauthGeneration) return;
    if (startedSessionId) api(`/api/admin/channels/oauth/sessions/${encodeURIComponent(startedSessionId)}`, { method: "DELETE" }).catch(() => {});
    oauthSession = { status: "failed" };
  } finally {
    if (generation === oauthGeneration) {
      oauthStarting = false;
      renderOAuth();
    }
  }
}

export async function parseOAuthCallback() {
  const form = document.getElementById("channelForm");
  if (!usesOAuth(form) || oauthParsing || oauthCompleting || !oauthSessionReady()) return;
  const callbackUrl = document.getElementById("channelOAuthCallback")?.value.trim();
  if (!callbackUrl) return;
  const generation = oauthGeneration;
  const revision = oauthInputRevision;
  clearOAuthPreview();
  oauthNotice = null;
  oauthParsing = true;
  renderOAuth();
  try {
    const result = await api(`/api/admin/channels/oauth/sessions/${encodeURIComponent(oauthSession.session_id)}/parse`, {
      method: "POST", body: JSON.stringify({ callback_url: callbackUrl }),
    });
    if (generation !== oauthGeneration || revision !== oauthInputRevision || !oauthSessionReady()) return;
    const expectedState = new URL(oauthSession.authorization_url).searchParams.get("state");
    if (typeof result.code !== "string" || !result.code || result.state !== expectedState
      || result.redirect_uri !== OAUTH_REDIRECT_URI) throw new Error("Invalid callback values");
    oauthPreview = { callback_url: callbackUrl };
    document.getElementById("channelOAuthCode").textContent = result.code;
    document.getElementById("channelOAuthState").textContent = result.state;
    document.getElementById("channelOAuthRedirect").textContent = result.redirect_uri;
  } catch {
    if (generation === oauthGeneration && revision === oauthInputRevision) {
      oauthNotice = { key: "oauthCallbackInvalid", type: "error" };
    }
  } finally {
    if (generation === oauthGeneration) {
      oauthParsing = false;
      renderOAuth();
    }
  }
}

export async function completeOAuthLogin() {
  const form = document.getElementById("channelForm");
  if (!usesOAuth(form) || oauthParsing || oauthCompleting || !oauthSessionReady() || !oauthPreview) return;
  const callbackUrl = document.getElementById("channelOAuthCallback")?.value.trim();
  if (callbackUrl !== oauthPreview.callback_url) {
    changeOAuthCallback();
    return;
  }
  const generation = oauthGeneration;
  const revision = oauthInputRevision;
  oauthNotice = null;
  oauthCompleting = true;
  renderOAuth();
  try {
    const result = await api(`/api/admin/channels/oauth/sessions/${encodeURIComponent(oauthSession.session_id)}/complete`, {
      method: "POST", body: JSON.stringify({ callback_url: callbackUrl }),
    });
    if (generation !== oauthGeneration || !oauthSessionReady()) return;
    if (revision !== oauthInputRevision) {
      oauthSession.status = "failed";
      clearOAuthPreview();
      oauthNotice = { key: "oauthCompleteFailed", type: "error" };
      return;
    }
    oauthSession.status = result.status;
    clearOAuthPreview();
  } catch {
    if (generation === oauthGeneration) {
      oauthSession.status = "failed";
      clearOAuthPreview();
      oauthNotice = { key: "oauthCompleteFailed", type: "error" };
    }
  } finally {
    if (generation === oauthGeneration) {
      oauthCompleting = false;
      renderOAuth();
    }
  }
}

export function oauthSaveFields(form) {
  if (!usesOAuth(form)) return {};
  if (oauthSession?.status === "authorized") {
    if (Date.now() / 1000 < oauthSession.expires_at) return { oauth_session_id: oauthSession.session_id };
    oauthSession.status = "expired";
    renderOAuth();
  }
  if (!oauthSession && !oauthStarting && editState.id && editState.authType === "oauth"
    && ["connected", "temporary_error", "reauth_required", "error"].includes(editState.authStatus)) return {};
  throw new Error(t("oauthLoginRequired"));
}

export function refreshAuthLabels() { renderOAuth(); }

const cooldownKey = (model, channelId) => `${model}\u0000${channelId}`;

function routingEntries(channelId) {
  return Object.values(state.routingState).filter((entry) => entry.channel_id === channelId);
}

function routingLabel(entry) {
  return t(entry.phase === "probe" ? "recoveryProbeBadge" : "coolingDownBadge")
    .replace("{{model}}", entry.model)
    .replace("{{seconds}}", String(entry.remaining_seconds));
}

function routingBadges(channelId) {
  const entries = routingEntries(channelId);
  if (!entries.length) return "";
  const order = { cooldown: 0, probe: 1 };
  const badges = entries
    .sort((a, b) => (order[a.phase] - order[b.phase])
      || (b.remaining_seconds - a.remaining_seconds)
      || a.model.localeCompare(b.model))
    .map((entry) => {
      const type = entry.phase === "probe" ? "info" : "warning";
      const icon = entry.phase === "probe" ? "radar" : "hourglass";
      return `<span class="badge ${type} cooldown-badge" title="${escapeHtml(t("coolingDownHint"))}">`
        + `<i data-lucide="${icon}"></i>${escapeHtml(routingLabel(entry))}</span>`;
    });
  const action = `<button class="btn-secondary btn-tiny" data-channel-cooldown-reset="${channelId}"`
    + ` title="${escapeHtml(t("resetCooldownHint"))}">`
    + `<i data-lucide="zap"></i>${escapeHtml(t("resetCooldown"))}</button>`;
  return `${badges.join("")}${action}`;
}

function hasCooldown(channelId) {
  return routingEntries(channelId).length > 0;
}

function hasAnyCooldown() {
  return Object.keys(state.routingState).length > 0;
}

// Keep detecting new cooldowns while healthy, and refresh active cooldowns faster.
const ROUTING_POLL_MS = 5000;
const HEALTHY_ROUTING_POLL_MS = 15000;
let routingTimer = null;
let active = false;
let loadGeneration = 0;

function scheduleRoutingPoll() {
  clearTimeout(routingTimer);
  routingTimer = null;
  if (!active || document.hidden) return;
  const generation = loadGeneration;
  routingTimer = setTimeout(() => {
    if (generation === loadGeneration) return refreshRoutingState();
  }, hasAnyCooldown() ? ROUTING_POLL_MS : HEALTHY_ROUTING_POLL_MS);
  routingTimer?.unref?.();
}

async function refreshRoutingState() {
  if (!active || document.hidden) return;
  const generation = loadGeneration;
  const previousState = JSON.stringify(state.routingState);
  const previousError = state.routingError?.message;
  try {
    const routingState = await api("/api/admin/channels/routing-state");
    if (!active || generation !== loadGeneration) return;
    state.routingState = indexRoutingState(routingState);
    state.routingError = null;
  } catch (error) {
    if (!active || generation !== loadGeneration) return;
    state.routingError = error;
  } finally {
    if (active && generation === loadGeneration) {
      if (previousState !== JSON.stringify(state.routingState)
        || previousError !== state.routingError?.message) render();
      scheduleRoutingPoll();
    }
  }
}

function indexRoutingState(routingState) {
  return Object.fromEntries(
    (routingState?.cooldowns || [])
      .map((entry) => [cooldownKey(entry.model, entry.channel_id), entry]),
  );
}

export function unload() {
  active = false;
  loadGeneration += 1;
  editRequest += 1;
  clearTimeout(routingTimer);
  routingTimer = null;
  clearTimeout(renderTimer);
  cancelOAuthLogin();
}

document.addEventListener("visibilitychange", () => {
  if (!active) return;
  if (document.hidden) {
    clearTimeout(routingTimer);
    routingTimer = null;
  } else {
    return refreshRoutingState();
  }
});

function orphanRoutingNotices() {
  const known = new Set(state.channels.map((c) => c.id));
  const notices = Object.values(state.routingState)
    .filter((entry) => !known.has(entry.channel_id))
    .map((entry) => escapeHtml(
      `${routingLabel(entry)} · ${t("coolingDownUnknownChannel").replace("{{id}}", String(entry.channel_id))}`,
    ));
  return notices.length
    ? `<div class="inline-result warning">${notices.join("<br>")}</div>`
    : "";
}

/* ---------- modal openers ---------- */
function withForm(fn) {
  const form = document.getElementById("channelForm");
  if (!form) return;
  fn(form);
}

export function openCreate() {
  editRequest += 1;
  editState.id = null;
  editState.authType = "api_key";
  editState.authStatus = null;
  editState.authExpiresAt = null;
  editState.authError = null;
  editState.authSyncError = false;
  resetSaveButton();
  withForm((form) => {
    form.reset();
    form.elements.priority.value = "1";
    form.elements.weight.value = "1";
    form.elements.enabled.checked = true;
    const keyInput = form.elements.key;
    keyInput.value = "";
    keyInput.required = true;
    keyInput.placeholder = "provider api key";
    applyChannelDefaults(form, { reset: true });
    if (form.elements.credential_type) form.elements.credential_type.value = "api_key";
    changeCredentialType(form);
  });
  setTitle(t("addChannelTitle"));
  document.getElementById("probeResult").className = "inline-result hidden";
  document.getElementById("channelModal").classList.remove("hidden");
  refreshIcons(document.getElementById("channelModal"));
}

export async function openEdit(channel) {
  if (!channel) return;
  const request = ++editRequest;
  editState.id = null;
  cancelOAuthLogin();
  document.getElementById("channelModal").classList.add("hidden");
  try {
    channel = await api(`/api/admin/channels/${channel.id}`);
  } catch (error) {
    if (request === editRequest) toast(error.message, "error");
    return;
  }
  if (request !== editRequest) return;
  editState.id = channel.id;
  editState.authType = channel.auth_type || "api_key";
  editState.authStatus = channel.auth_status;
  editState.authExpiresAt = channel.auth_expires_at;
  editState.authError = channel.auth_error;
  editState.authSyncError = false;
  delete state.authSyncError[channel.id];
  const saved = state.channels.find((entry) => entry.id === channel.id);
  if (saved?.auth_type === "oauth" && channel.auth_type === "oauth") {
    Object.assign(saved, { auth_status: channel.auth_status, auth_expires_at: channel.auth_expires_at, auth_error: channel.auth_error });
    if (active) render();
  }
  resetSaveButton();
  const extra = channel.extra || {};
  const { models_path, request_path, auth_type, ...restExtra } = extra;
  withForm((form) => {
    form.elements.name.value = channel.name || "";
    form.elements.type.value = channel.type || "openai";
    form.elements.base_url.value = channel.base_url || "";
    form.elements.protocol.value = channel.protocol || "openai";
    const defaults = channelDefaults(form.elements.type.value, form.elements.protocol.value);
    form.elements.auth_type.value = auth_type || defaults.auth_type;
    form.elements.models_path.value = models_path || defaults.models_path;
    form.elements.request_path.value = request_path || defaults.request_path;
    form.elements.priority.value = channel.priority ?? 1;
    form.elements.weight.value = channel.weight ?? 1;
    form.elements.models.value = (channel.models || []).join(", ");
    form.elements.model_mapping.value = channel.model_mapping && Object.keys(channel.model_mapping).length
      ? JSON.stringify(channel.model_mapping, null, 2) : "";
    form.elements.extra.value = Object.keys(restExtra).length ? JSON.stringify(restExtra, null, 2) : "";
    form.elements.enabled.checked = channel.enabled !== false;
    const keyInput = form.elements.key;
    keyInput.value = "";
    keyInput.required = false;
    keyInput.placeholder = t("keepUnchanged");
    rememberChannelDefaults(form);
    if (form.elements.credential_type) form.elements.credential_type.value = editState.authType;
    changeCredentialType(form, { resetConnection: false });
  });
  setTitle(`${t("editChannel")} · ${channel.name}`);
  document.getElementById("probeResult").className = "inline-result hidden";
  document.getElementById("channelModal").classList.remove("hidden");
  refreshIcons(document.getElementById("channelModal"));
}

function setTitle(text) {
  const el = document.getElementById("channelModalTitle");
  if (el) el.textContent = text;
}

export async function load() {
  const container = document.getElementById("channels");
  const generation = ++loadGeneration;
  active = true;
  clearTimeout(routingTimer);
  routingTimer = null;
  skeletonCards(container, 3);
  let routingError = null;
  try {
    const [channels, routingState] = await Promise.all([
      api("/api/admin/channels"),
      api("/api/admin/channels/routing-state").catch((error) => {
        routingError = error;
        return null;
      }),
    ]);
    if (!active || generation !== loadGeneration) return;
    state.channels = channels;
    state.authSyncError = {};
    state.routingState = indexRoutingState(routingState);
    state.routingError = routingError;
    render();
  } catch (e) {
    if (!active || generation !== loadGeneration) return;
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
  } finally {
    if (active && generation === loadGeneration) scheduleRoutingPoll();
  }
}

export function render() {
  const container = document.getElementById("channels");
  const providers = Array.from(new Set(state.channels.map((c) => c.type))).sort();
  const filtered = state.channels.filter((c) => {
    if (state.filter.q) {
      const q = state.filter.q.toLowerCase();
      const hit =
        c.name?.toLowerCase().includes(q) ||
        c.type?.toLowerCase().includes(q) ||
        (c.models || []).some((m) => String(m).toLowerCase().includes(q));
      if (!hit) return false;
    }
    if (state.filter.provider && c.type !== state.filter.provider) return false;
    if (state.filter.status === "enabled" && !c.enabled) return false;
    if (state.filter.status === "disabled" && c.enabled) return false;
    if (state.filter.status === "cooling" && !hasCooldown(c.id)) return false;
    return true;
  });

  container.innerHTML = `
    <div class="section-head">
      <div>
        <h2>${t("channels")}</h2>
        <p>${t("channelsDescription")}</p>
      </div>
      <div class="actions">
        <button class="btn-secondary" id="refreshChannelsData"><i data-lucide="refresh-cw"></i>${t("refresh")}</button>
        <button class="btn-primary" id="openChannelModalBtn"><i data-lucide="plus"></i>${t("addChannel")}</button>
      </div>
    </div>

    ${state.routingError ? `<div class="inline-result error">${escapeHtml(t("routingStateUnavailable"))} · ${escapeHtml(state.routingError.message)}</div>` : ""}
    ${orphanRoutingNotices()}

    <div class="toolbar">
      <div class="search-box" style="display:flex">
        <i data-lucide="search"></i>
        <input id="chSearch" placeholder="${t("searchPlaceholder") || "搜索名称/Provider/模型..."}" value="${escapeHtml(state.filter.q)}">
      </div>
      <select class="select" id="chFilterProvider">
        <option value="">${t("allProviders") || "All providers"}</option>
        ${providers.map((p) => `<option value="${p}" ${state.filter.provider === p ? "selected" : ""}>${p}</option>`).join("")}
      </select>
      <select class="select" id="chFilterStatus">
        <option value="">${t("allStatus") || "All status"}</option>
        <option value="enabled" ${state.filter.status === "enabled" ? "selected" : ""}>${t("enable")}</option>
        <option value="disabled" ${state.filter.status === "disabled" ? "selected" : ""}>${t("disable")}</option>
        <option value="cooling" ${state.filter.status === "cooling" ? "selected" : ""}>${t("coolingDown")}</option>
      </select>
      <div class="spacer"></div>
      <span class="muted text-sm">${filtered.length} / ${state.channels.length}</span>
    </div>

    <div id="channelTestResult" class="inline-result hidden"></div>

    ${filtered.length === 0
      ? `<div class="empty-state"><i data-lucide="layers"></i><h3>${t("noChannels") || "暂无渠道"}</h3><p>${t("noChannelsHint") || "点击右上角新增一个渠道"}</p></div>`
      : `<div class="channel-grid">${filtered.map(renderCard).join("")}</div>`}
  `;

  bindToolbar();
  refreshIcons(container);
}

function renderCard(c) {
  const total = c.total_requests || 0;
  const succ = c.success_requests || 0;
  const fail = c.failed_requests || 0;
  const rate = total ? Math.round((succ / total) * 100) : 0;
  const succW = total ? (succ / total) * 100 : 0;
  const failW = total ? (fail / total) * 100 : 0;
  const models = (c.models || []).slice(0, 4);
  const more = (c.models || []).length - models.length;
  const test = state.testResult[c.id];
  const menuOpen = state.openMenu === c.id;
  const cooling = hasCooldown(c.id);
  const auth = savedOAuthState(c.auth_status, state.authSyncError[c.id]);

  return `
    <div class="channel-card ${c.enabled ? "" : "disabled"}${cooling ? " cooling" : ""}">
      <div class="ch-head">
        <span class="status-dot ${c.enabled ? "on" : ""}"></span>
        <span class="ch-name">${escapeHtml(c.name)}</span>
        <span class="ch-id">#${c.id}</span>
        <div class="dropdown">
          <button class="dropdown-trigger" data-channel-menu="${c.id}" aria-label="更多操作">
            <i data-lucide="more-vertical"></i>
          </button>
          <div class="dropdown-menu ${menuOpen ? "" : "hidden"}" data-menu-for="${c.id}">
            <button class="dropdown-item" data-channel-probe="${c.id}">
              <i data-lucide="radar"></i>${t("probeModels")}
            </button>
            <button class="dropdown-item" data-channel-test="${c.id}">
              <i data-lucide="activity"></i>${t(c.auth_type === "oauth" ? "oauthCheck" : "test")}
            </button>
            <button class="dropdown-item" data-channel-toggle="${c.id}" data-enabled="${c.enabled}">
              <i data-lucide="${c.enabled ? "pause" : "play"}"></i>${c.enabled ? t("disable") : t("enable")}
            </button>
            <div class="dropdown-divider"></div>
            <button class="dropdown-item danger" data-channel-delete="${c.id}">
              <i data-lucide="trash-2"></i>${t("delete")}
            </button>
          </div>
        </div>
      </div>
      <div class="ch-meta">
        ${badge(c.type, "accent")}
        ${badge(c.protocol, "info")}
        ${c.auth_type === "oauth" ? badge("ChatGPT OAuth", "accent") : ""}
        ${c.auth_type === "oauth" ? badge(t(auth.key), auth.type === "error" ? "danger" : auth.type) : ""}
        ${statusBadge(c.enabled)}
      </div>
      <div class="ch-base" title="${escapeHtml(c.base_url || "")}">${escapeHtml(c.base_url || "")}</div>
      ${c.auth_type === "oauth" ? `<p class="muted text-sm">${escapeHtml(savedOAuthDetails(c, state.authSyncError[c.id]))}</p>` : ""}
      ${cooling ? `<div class="ch-meta cooldown-row">${routingBadges(c.id)}</div>` : ""}
      <div class="chips">
        ${models.map((m) => `<span class="chip">${escapeHtml(m)}</span>`).join("")}
        ${more > 0 ? `<span class="chip muted">+${more}</span>` : ""}
      </div>
      ${test ? `<div class="inline-result ${test.ok ? "success" : "error"}">${escapeHtml(test.message)}</div>` : ""}
      <div class="ch-stats">
        <div class="stat-mini">
          <span class="lbl">${t("metrics")[0]}</span>
          <span class="val">${total}</span>
        </div>
        <div class="stat-mini">
          <span class="lbl">${t("successRate") || "Success"}</span>
          <span class="val">${rate}%</span>
        </div>
        <div class="stat-mini">
          <span class="lbl">${t("priority")}</span>
          <span class="val">${c.priority}</span>
        </div>
      </div>
      <div class="row" style="gap:10px;align-items:center">
        <div class="bar-mini" style="flex:1">
          <div class="seg-success" style="width:${succW}%"></div>
          <div class="seg-failed" style="width:${failW}%"></div>
        </div>
        <button class="btn-secondary" data-channel-edit="${c.id}">
          <i data-lucide="${c.auth_type === "oauth" && c.auth_status === "reauth_required" ? "log-in" : "pencil"}"></i>${t(c.auth_type === "oauth" && c.auth_status === "reauth_required" ? "oauthSignInAgain" : "edit")}
        </button>
      </div>
    </div>`;
}

function bindToolbar() {
  const search = document.getElementById("chSearch");
  search?.addEventListener("input", (e) => { state.filter.q = e.target.value; debouncedRender(); });
  document.getElementById("chFilterProvider")?.addEventListener("change", (e) => { state.filter.provider = e.target.value; render(); });
  document.getElementById("chFilterStatus")?.addEventListener("change", (e) => { state.filter.status = e.target.value; render(); });
  document.getElementById("refreshChannelsData")?.addEventListener("click", () => load());
  document.getElementById("openChannelModalBtn")?.addEventListener("click", openCreate);
}

let renderTimer;
function debouncedRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 200);
}

export async function onClick(target) {
  // toggle dropdown menu
  const menuTrigger = target.closest("[data-channel-menu]");
  if (menuTrigger) {
    const id = Number(menuTrigger.dataset.channelMenu);
    state.openMenu = state.openMenu === id ? null : id;
    render();
    return true;
  }

  const resetCooldown = target.closest("[data-channel-cooldown-reset]")?.dataset.channelCooldownReset;
  if (resetCooldown) {
    await resetChannelCooldown(resetCooldown);
    return true;
  }

  const test = target.dataset.channelTest;
  if (test) {
    state.openMenu = null;
    await runTest(test);
    return true;
  }

  const probe = target.closest("[data-channel-probe]")?.dataset.channelProbe;
  if (probe) {
    state.openMenu = null;
    render();
    await probeModels(probe);
    return true;
  }

  const editId = target.dataset.channelEdit;
  if (editId) {
    const channel = state.channels.find((c) => String(c.id) === String(editId));
    if (channel) await openEdit(channel);
    return true;
  }

  const toggle = target.dataset.channelToggle;
  if (toggle) {
    state.openMenu = null;
    const enabled = target.dataset.enabled === "true";
    await api(`/api/admin/channels/${toggle}/${enabled ? "disable" : "enable"}`, { method: "POST" });
    toast(t("updated"), "success");
    await load();
    return true;
  }

  const del = target.dataset.channelDelete;
  if (del && confirm(`${t("confirmDeleteChannel")} ${del}?`)) {
    state.openMenu = null;
    await api(`/api/admin/channels/${del}`, { method: "DELETE" });
    toast(t("deleted"), "success");
    await load();
    return true;
  }
  return false;
}

// Close any open channel menu when clicking outside (capture so it fires before click delegation).
if (typeof document !== "undefined") {
  document.addEventListener("mousedown", (event) => {
    if (state.openMenu === null) return;
    if (event.target instanceof Element && event.target.closest(".dropdown")) return;
    state.openMenu = null;
    render();
  }, true);
}

async function resetChannelCooldown(id) {
  if (!confirm(t("resetCooldownConfirm"))) return;
  try {
    const result = await api(`/api/admin/channels/${id}/cooldown/reset`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    const released = result?.released || [];
    toast(
      released.length
        ? `${t("cooldownCleared")} · ${released.join(", ")}`
        : t("cooldownNone"),
      released.length ? "success" : "warning",
    );
    await refreshRoutingState();
  } catch (error) {
    toast(error.message, "error", 5000);
  }
}

async function runTest(id) {
  const oauth = state.channels.find((channel) => String(channel.id) === String(id))?.auth_type === "oauth"
    || (String(editState.id) === String(id) && editState.authType === "oauth");
  const request = oauth ? beginAuthStateRequest(id) : null;
  const generation = loadGeneration;
  let outcome;
  if (oauth) renderOAuth();
  const card = document.querySelector(`[data-channel-test="${id}"]`)?.closest(".channel-card");
  if (card) {
    const btn = card.querySelector(`[data-channel-test="${id}"]`);
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("testing") || "..."}`;
    refreshIcons(card);
  }
  try {
    const result = await api(`/api/admin/channels/${id}/test`, { method: "POST" });
    outcome = {
      ok: result.ok,
      message: result.ok
        ? result.check_type === "oauth"
          ? `${t("oauthCheckPassed")} · ${result.latency_ms} ms`
          : `${t("modelCatalogReachable")} · ${result.latency_ms} ms · ${result.models.length} models`
        : `${t("failed")} · ${result.latency_ms} ms · ${result.error || result.status_code}`,
    };
  } catch (e) {
    outcome = { ok: false, message: e.message };
  }
  if (request) {
    await finishAuthStateRequest(request);
    if (!currentAuthStateRequest(request)) return;
  } else if (generation !== loadGeneration) return;
  state.testResult[id] = outcome;
  if (active) render();
}

async function probeModels(id) {
  if (probingChannels.has(String(id))) return;
  probingChannels.add(String(id));
  const oauth = state.channels.find((channel) => String(channel.id) === String(id))?.auth_type === "oauth";
  const authRequest = oauth ? beginAuthStateRequest(id) : null;
  try {
    const result = await api(`/api/admin/channels/${id}/probe-models`, {
      method: "POST",
    });
    const models = result.models || [];
    const channel = state.channels.find((c) => String(c.id) === String(id));
    if (!models.length) {
      toast(t("probeNoModels") || "upstream returned no models", "warning", 5000);
      return;
    }
    const current = (channel?.models || []).join(", ");
    const next = models.join(", ");
    const confirmed = current === next
      || confirm(
        (t("confirmUpdateModels") || "Update channel models to the probed list?") +
        `\n\n[${next}]\n\n→ #${id} ${channel?.name || ""}` +
        (current ? `\n\n${t("currentLabel") || "current"}: [${current}]` : ""),
      );
    if (!confirmed) return;
    await api(`/api/admin/channels/${id}`, {
      method: "PUT",
      body: JSON.stringify({ models }),
    });
    toast(
      `${t("updated")} · ${models.length} ${t("foundModels")} ${result.latency_ms} ms`,
      "success",
      5000,
    );
    await load();
  } catch (error) {
    const oauth = state.channels.find((c) => String(c.id) === String(id))?.auth_type === "oauth";
    toast(oauth ? t("oauthProbeFailed") : error.message, "error", 5000);
  } finally {
    if (authRequest) await finishAuthStateRequest(authRequest);
    probingChannels.delete(String(id));
  }
}
