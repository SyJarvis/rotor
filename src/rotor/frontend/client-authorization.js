import { api } from "./api.js?v=3";
import { t, applyLocale, toggleLocale } from "./i18n.js";
import { actor, accountId } from "./session.js?v=1";

const get = (id) => document.getElementById(id);
const parameters = new URL(window.location.href).searchParams;
const authorizationFields = ["response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope"];
const authorizationQuery = new URLSearchParams();
for (const name of authorizationFields) {
  for (const value of parameters.getAll(name)) authorizationQuery.append(name, value);
}
const authorizationBody = Object.fromEntries(authorizationQuery);
const requests = new Set();
let epoch = 0, metadata = null, pending = false, loading = false;

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
  metadata = null; pending = false; loading = false;
  for (const id of ["clientOAuthName", "clientOAuthUsername", "clientOAuthQuota", "clientOAuthCallback"]) get(id).textContent = "";
  get("clientOAuthModels").replaceChildren();
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

async function load() {
  if (!actor() || loading || pending) return;
  const requestEpoch = ++epoch, owner = accountId();
  loading = true; metadata = null;
  renderMetadata(); controls(); message(t("clientLoading"));
  try {
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

get("clientOAuthApprove").addEventListener("click", () => decide(true));
get("clientOAuthDeny").addEventListener("click", () => decide(false));
get("clientOAuthRetry").addEventListener("click", load);
get("clientOAuthLocale").addEventListener("click", toggleLocale);
let owner = accountId();
document.addEventListener("sessionended", () => { owner = ""; clearSession(); });
document.addEventListener("sessionchange", () => { if (owner === accountId()) return; owner = accountId(); clearSession(); void load(); });
document.addEventListener("localechange", () => {
  document.title = t("clientAuthorizationTitle");
  renderMetadata();
});
applyLocale();
void load();
