import { api } from "./api.js?v=3";
import { t, applyLocale, toggleLocale } from "./i18n.js";
import { actor, accountId } from "./session.js?v=1";

const get = (id) => document.getElementById(id);
const authorizing = window.location.pathname === "/oauth/authorize";
const parameters = new URL(window.location.href).searchParams;
const authorizationFields = ["response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope"];
const authorizationQuery = new URLSearchParams();
for (const name of authorizationFields) {
  for (const value of parameters.getAll(name)) authorizationQuery.append(name, value);
}
const authorizationBody = Object.fromEntries(authorizationQuery);
const requests = new Set();
let epoch = 0, metadata = null, grants = [], pending = false, loading = false;
const revoking = new Set();

function message(value = "", error = false) {
  get("clientOAuthStatus").textContent = error ? "" : value;
  get("clientOAuthError").textContent = error ? value : "";
  get("clientOAuthError").classList.toggle("hidden", !error);
  get("clientOAuthRetry").classList.toggle("hidden", !error);
}

function controls() {
  const unavailable = !metadata || !actor() || pending || loading;
  get("clientOAuthApprove").disabled = unavailable;
  get("clientOAuthDeny").disabled = unavailable;
  get("clientOAuthRetry").disabled = loading || pending;
}

function clearSession() {
  ++epoch;
  for (const request of requests) request.abort();
  requests.clear();
  metadata = null; grants = []; pending = false; loading = false; revoking.clear();
  for (const id of ["clientOAuthName", "clientOAuthUsername", "clientOAuthQuota", "clientOAuthCallback", "clientAppsAccount"]) get(id).textContent = "";
  get("clientOAuthModels").replaceChildren();
  get("clientAppsList").replaceChildren();
  message(); controls();
}

async function request(path, options = {}) {
  const controller = new AbortController();
  requests.add(controller);
  try { return await api(path, { ...options, cache: "no-store", signal: controller.signal }); }
  finally { requests.delete(controller); }
}

function current(requestEpoch, owner) {
  return requestEpoch === epoch && !!actor() && owner === accountId();
}

function renderMetadata() {
  get("clientOAuthName").textContent = metadata?.client_name || "";
  get("clientOAuthUsername").textContent = metadata?.username || "";
  get("clientOAuthQuota").textContent = !metadata ? "" : metadata.quota === null
    ? t("unlimited") : Math.max(0, metadata.quota - (metadata.used_quota ?? 0)).toLocaleString(document.documentElement.lang);
  get("clientOAuthCallback").textContent = metadata?.redirect_uri || "";
  get("clientOAuthModels").replaceChildren();
  if (!metadata) return;
  for (const model of metadata.models.length ? metadata.models : [t("clientNoModels")]) {
    const item = document.createElement("li");
    item.textContent = model;
    get("clientOAuthModels").appendChild(item);
  }
}

function timestamp(value) {
  if (!value) return t("clientNeverUsed");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(document.documentElement.lang);
}

function renderGrants() {
  get("clientAppsAccount").textContent = actor()?.username || "";
  get("clientAppsList").replaceChildren();
  if (!grants.length) {
    const empty = document.createElement("p");
    empty.textContent = t("clientNoGrants");
    get("clientAppsList").appendChild(empty);
  }
  for (const grant of grants) {
    const row = document.createElement("article"); row.className = "client-oauth-grant";
    const title = document.createElement("h2"); title.textContent = grant.client_name;
    const details = document.createElement("p"); details.className = "muted";
    details.textContent = `${t("createdAt")}: ${timestamp(grant.created_at)} · ${t("clientLastUsed")}: ${timestamp(grant.last_used_at)} · ${t("expireTime")}: ${timestamp(grant.expires_at)}`;
    const button = document.createElement("button"); button.type = "button"; button.className = "btn-secondary";
    button.textContent = t("clientRevoke"); button.disabled = revoking.has(grant.id);
    button.addEventListener("click", () => revoke(grant.id));
    row.appendChild(title); row.appendChild(details); row.appendChild(button);
    get("clientAppsList").appendChild(row);
  }
}

async function load() {
  if (!actor() || loading || pending) return;
  const requestEpoch = ++epoch, owner = accountId();
  loading = true; metadata = null; grants = [];
  renderMetadata(); renderGrants(); controls(); message(t("clientLoading"));
  try {
    if (authorizing) {
      const result = await request(`/api/oauth/authorization?${authorizationQuery}`);
      if (!current(requestEpoch, owner)) return;
      if (result.client_id !== authorizationBody.client_id || result.redirect_uri !== authorizationBody.redirect_uri
        || result.scope !== "inference" || result.username !== actor()?.username
        || typeof result.client_name !== "string" || !result.client_name
        || !(result.quota === null || (Number.isInteger(result.quota) && result.quota >= 0))
        || !(result.used_quota == null || (Number.isInteger(result.used_quota) && result.used_quota >= 0))
        || !Array.isArray(result.models) || !result.models.every((model) => typeof model === "string")) {
        throw new Error(t("clientInvalidAuthorization"));
      }
      metadata = result; renderMetadata();
    } else {
      const result = await request("/api/oauth/grants");
      if (!current(requestEpoch, owner)) return;
      if (!Array.isArray(result.items)) throw new Error(t("clientLoadFailed"));
      grants = result.items; renderGrants();
    }
    message();
  } catch (error) {
    if (current(requestEpoch, owner)) message(error.message || t("clientLoadFailed"), true);
  } finally {
    if (current(requestEpoch, owner)) { loading = false; controls(); }
  }
}

async function decide(approve) {
  if (!metadata || !actor() || pending || loading) return;
  const requestEpoch = epoch, owner = accountId();
  pending = true; controls(); message(t("clientAuthorizing"));
  try {
    const result = await request("/api/oauth/authorization", { method: "POST", body: JSON.stringify({ ...authorizationBody, approve }) });
    if (!current(requestEpoch, owner)) return;
    const target = new URL(result.redirect_url), callback = new URL(metadata.redirect_uri);
    if (target.origin !== callback.origin || target.pathname !== callback.pathname
      || target.searchParams.get("state") !== (authorizationBody.state || null)
      || !(target.searchParams.get("code") || target.searchParams.get("error"))) {
      throw new Error(t("clientInvalidAuthorization"));
    }
    window.location.assign(result.redirect_url);
  } catch (error) {
    if (current(requestEpoch, owner)) message(error.message || t("clientAuthorizationFailed"), true);
  } finally {
    if (current(requestEpoch, owner)) { pending = false; controls(); }
  }
}

async function revoke(id) {
  if (!actor() || revoking.has(id)) return;
  const requestEpoch = epoch, owner = accountId();
  revoking.add(id); renderGrants(); message();
  try {
    await request(`/api/oauth/grants/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!current(requestEpoch, owner)) return;
    grants = grants.filter((grant) => grant.id !== id);
    message(t("clientRevoked"));
  } catch (error) {
    if (current(requestEpoch, owner)) message(error.message || t("clientRevokeFailed"), true);
  } finally {
    if (current(requestEpoch, owner)) { revoking.delete(id); renderGrants(); }
  }
}

get("clientAuthorizationPanel").classList.toggle("hidden", !authorizing);
get("clientAppsPanel").classList.toggle("hidden", authorizing);
get("clientOAuthApprove").addEventListener("click", () => decide(true));
get("clientOAuthDeny").addEventListener("click", () => decide(false));
get("clientOAuthRetry").addEventListener("click", load);
get("clientOAuthLocale").addEventListener("click", toggleLocale);
document.addEventListener("sessionended", clearSession);
document.addEventListener("sessionchange", () => { clearSession(); void load(); });
document.addEventListener("localechange", () => {
  document.title = t(authorizing ? "clientAuthorizationTitle" : "clientAppsTitle");
  renderMetadata(); renderGrants();
});
applyLocale();
void load();
