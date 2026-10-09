import { api, copyText } from "../api.js?v=3";
import { t } from "../i18n.js";
import { isAdmin } from "../session.js?v=1";
import { escapeHtml as esc, refreshIcons, toast, formatTime } from "../ui.js";
let users = [], invitations = [], query = "", editing = null, mode = null, inviteCode = "", busy = false, epoch = 0;

function channels(value) {
  const raw = value.trim(); if (raw === "*") return null; if (!raw) return [];
  const ids = raw.split(",").map((item) => Number(item.trim()));
  if (ids.some((id) => !Number.isInteger(id) || id < 1)) throw new Error(t("invalidChannelIds"));
  return [...new Set(ids)];
}
function permissions(form) {
  const quota = form.elements.quota.value === "" ? null : Number(form.elements.quota.value);
  if (quota !== null && (!Number.isSafeInteger(quota) || quota < 0)) throw new Error(t("invalidQuota"));
  const models = form.elements.allowed_models.value.trim();
  return { quota, allowed_channels: channels(form.elements.allowed_channels.value), allowed_models: models === "*" ? null : models.split(",").map((item) => item.trim()).filter(Boolean) };
}
export async function load() {
  if (!isAdmin()) return;
  const request = ++epoch;
  try {
    const result = await Promise.all([api("/api/admin/users"), api("/api/admin/invitations")]);
    if (request !== epoch || !isAdmin()) return;
    [users, invitations] = result; render();
  } catch (error) { if (request === epoch) { document.getElementById("users").textContent = error.message; } }
}
function row(user) {
  return `<tr><td><strong>${esc(user.username)}</strong><small class="user-display-name">${esc(user.display_name || "")}</small></td><td>${t(user.role === "admin" ? "adminRole" : "userRole")}</td><td>${t(user.enabled ? "enabled" : "disable")}</td><td>${esc(user.used_quota ?? 0)} / ${user.quota == null ? t("unlimited") : esc(user.quota)}</td><td><button class="btn-secondary btn-sm" data-user-edit="${user.id}">${t("edit")}</button> <button class="btn-secondary btn-sm" data-user-password="${user.id}">${t("resetPassword")}</button></td></tr>`;
}
function list() {
  const body = document.getElementById("usersBody"); if (body) body.innerHTML = users.filter((user) => `${user.username} ${user.display_name || ""}`.toLowerCase().includes(query.toLowerCase())).map(row).join("");
}
function restrictionFields(user = {}) {
  return `<label><span>${t("quotaOptional")}</span><input name="quota" type="number" min="0" step="1" value="${esc(user.quota ?? "")}"><small>${t("quotaPermissionHint")}</small></label>
    <label><span>${t("accountChannels")}</span><input name="allowed_channels" value="${esc(user.allowed_channels === null ? "*" : (user.allowed_channels || []).join(","))}"><small>${t("channelPermissionHint")}</small></label>
    <label class="span-2"><span>${t("accountModels")}</span><input name="allowed_models" value="${esc(user.allowed_models == null ? "*" : user.allowed_models.join(","))}"><small>${t("modelPermissionHint")}</small></label>`;
}
function modal() {
  if (!mode) return "";
  const user = editing || { quota: 0, allowed_channels: [], allowed_models: null };
  const title = t(mode === "password" ? "resetPassword" : mode === "invite" ? "createInvitation" : editing ? "editUser" : "createUser");
  return `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="userModalTitle"><div class="modal-backdrop" data-user-close></div><div class="modal-panel"><header class="modal-head"><h2 id="userModalTitle">${title}</h2><button class="icon-btn" data-user-close aria-label="${t("close")}"><i data-lucide="x"></i></button></header>
    <form id="userForm"><div class="form-grid">
      ${mode === "password" ? `<label class="span-2"><span>${esc(user.username)} · ${t("newPassword")}</span><input name="password" type="password" autocomplete="new-password" minlength="6" required></label>` : mode === "invite" ? `<label class="span-2"><span>${t("invitationHours")}</span><input name="expires_in_hours" type="number" min="1" max="720" value="72" required></label>${restrictionFields(user)}` : `
      <label><span>${t("adminUsername")}</span><input name="username" value="${esc(user.username || "")}" ${editing ? "readonly" : "required"} autocomplete="off"></label>
      <label><span>${t("displayName")}</span><input name="display_name" value="${esc(user.display_name || "")}" maxlength="100"></label>
      ${!editing ? `<label class="span-2"><span>${t("adminPassword")}</span><input name="password" type="password" autocomplete="new-password" minlength="6" required></label>` : ""}
      <label><span>${t("roleLabel")}</span><select name="role"><option value="user" ${user.role !== "admin" ? "selected" : ""}>${t("userRole")}</option><option value="admin" ${user.role === "admin" ? "selected" : ""}>${t("adminRole")}</option></select></label>
      ${editing ? `<label class="setting-toggle"><span>${t("enabled")}</span><input name="enabled" type="checkbox" ${user.enabled ? "checked" : ""}></label>` : ""}${restrictionFields(user)}`}
    </div><p class="auth-error hidden" id="userFormError" role="alert"></p><footer class="modal-actions"><button type="button" class="btn-secondary" data-user-close>${t("cancel")}</button><button class="btn-primary" type="submit">${t("save")}</button></footer></form></div></div>`;
}
function render() {
  const container = document.getElementById("users");
  container.innerHTML = `<div class="section-head"><div><h2>${t("usersTitle")}</h2><p>${t("usersDescription")}</p></div><div class="row"><button class="btn-secondary" data-invite-create>${t("createInvitation")}</button><button class="btn-primary" data-user-create>${t("createUser")}</button></div></div>
    <input id="usersSearch" class="user-search" placeholder="${t("search")}" value="${esc(query)}" aria-label="${t("search")}">
    <div class="table-wrap"><div class="table-scroll"><table><thead><tr><th>${t("adminUsername")}</th><th>${t("roleLabel")}</th><th>${t("status")}</th><th>${t("quotaOptional")}</th><th>${t("columns").actions}</th></tr></thead><tbody id="usersBody"></tbody></table></div></div>
    <section class="mt-6"><h3>${t("invitationsTitle")}</h3><p>${t("invitationRules")}</p>
      ${inviteCode ? `<div class="invitation-result"><p>${t("invitationCopyOnce")}</p><label for="invitationCode">${t("invitationCode")}</label><input id="invitationCode" readonly><button class="btn-secondary" data-invite-copy>${t("copyInvitationCode")}</button></div>` : ""}
      <div class="table-wrap"><div class="table-scroll"><table><thead><tr><th>ID</th><th>${t("expireTime")}</th><th>${t("status")}</th><th>${t("columns").actions}</th></tr></thead><tbody>${invitations.map((invite) => `<tr><td>${invite.id}</td><td>${esc(formatTime(invite.expires_at))}</td><td>${t(invite.revoked ? "invitationRevoked" : invite.used_at ? "invitationUsed" : new Date(invite.expires_at) < new Date() ? "expired" : "invitationPending")}</td><td>${!invite.revoked && !invite.used_at ? `<button class="btn-secondary btn-sm" data-invite-revoke="${invite.id}">${t("revokeInvitation")}</button>` : "—"}</td></tr>`).join("")}</tbody></table></div></div>
    </section>${modal()}`;
  list(); refreshIcons(container);
  if (inviteCode) document.getElementById("invitationCode").value = inviteCode;
  document.getElementById("usersSearch")?.addEventListener("input", (event) => { query = event.target.value; list(); });
  document.getElementById("userForm")?.addEventListener("submit", submit);
}
async function submit(event) {
  event.preventDefault(); if (busy) return;
  const form = event.currentTarget, button = form.querySelector('[type="submit"]'); busy = true; button.disabled = true;
  try {
    if (mode === "password") await api(`/api/admin/users/${editing.id}/reset-password`, { method: "POST", body: JSON.stringify({ password: form.elements.password.value }) });
    else if (mode === "invite") {
      const invitation = await api("/api/admin/invitations", { method: "POST", body: JSON.stringify({ ...permissions(form), expires_in_hours: Number(form.elements.expires_in_hours.value) }) });
      inviteCode = invitation.token;
    } else {
      const data = { ...permissions(form), display_name: form.elements.display_name.value, role: form.elements.role.value };
      if (editing) data.enabled = form.elements.enabled.checked;
      else { data.username = form.elements.username.value; data.password = form.elements.password.value; }
      await api(`/api/admin/users${editing ? `/${editing.id}` : ""}`, { method: editing ? "PATCH" : "POST", body: JSON.stringify(data) });
    }
    mode = null; editing = null; await load(); toast(t("updated"), "success");
  } catch (error) { const field = document.getElementById("userFormError"); if (field) { field.textContent = error.message; field.classList.remove("hidden"); } }
  finally { busy = false; button.disabled = false; }
}
export async function onClick(target) {
  if (!isAdmin()) return false;
  if (target.closest("[data-user-create]")) { mode = "user"; editing = null; }
  else if (target.closest("[data-user-edit]")) { mode = "user"; editing = users.find((item) => item.id === Number(target.closest("[data-user-edit]").dataset.userEdit)); }
  else if (target.closest("[data-user-password]")) { mode = "password"; editing = users.find((item) => item.id === Number(target.closest("[data-user-password]").dataset.userPassword)); }
  else if (target.closest("[data-invite-create]")) { mode = "invite"; editing = null; }
  else if (target.closest("[data-user-close]")) { if (busy) return true; mode = null; editing = null; }
  else if (target.closest("[data-invite-copy]")) { const copied = await copyText(inviteCode); toast(t(copied ? "copied" : "copyFailed"), copied ? "success" : "error"); return true; }
  else if (target.closest("[data-invite-revoke]")) { await api(`/api/admin/invitations/${target.closest("[data-invite-revoke]").dataset.inviteRevoke}`, { method: "DELETE" }); await load(); return true; }
  else return false;
  render(); document.getElementById("userForm")?.querySelector("input")?.focus(); return true;
}
export function unload() { ++epoch; mode = null; editing = null; inviteCode = ""; }
document.addEventListener("sessionended", () => { unload(); users = []; invitations = []; query = ""; });
