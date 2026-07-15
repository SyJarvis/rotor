// Tokens page — compact API key list with create/view/delete modals.

import { api, copyText } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml, formatTime, skeletonRows, refreshIcons, toast,
} from "../ui.js";

let state = {
  tokens: [],
  viewing: null,
  deleting: null,
  createOpen: false,
};

export async function load() {
  const container = document.getElementById("tokens");
  skeletonRows(container, 3, 4);
  try {
    state.tokens = await api("/api/admin/tokens");
    render();
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
  }
}

export function render() {
  const container = document.getElementById("tokens");
  const cols = t("columns");

  container.innerHTML = `
    <div class="section-head">
      <div>
        <h2>${t("apiKeys")}</h2>
        <p>${t("keyDescription")}</p>
      </div>
      <button class="btn-primary" data-token-create-open>
        <i data-lucide="key-round"></i>${t("createKey")}
      </button>
    </div>

    ${state.tokens.length === 0
      ? `<div class="empty-state"><i data-lucide="key-round"></i><h3>${t("noTokens")}</h3></div>`
      : `<div class="table-wrap mt-6"><div class="table-scroll"><table>
          <thead><tr>
            <th>${cols.name}</th>
            <th>${cols.key}</th>
            <th>${t("createdAt")}</th>
            <th>${cols.actions}</th>
          </tr></thead>
          <tbody>
            ${state.tokens.map(renderRow).join("")}
          </tbody>
        </table></div></div>`}

    ${state.createOpen ? renderCreateModal() : ""}
    ${state.viewing ? renderViewModal(state.viewing) : ""}
    ${state.deleting ? renderDeleteModal(state.deleting) : ""}
  `;

  document.getElementById("tokenCreateForm")?.addEventListener("submit", onCreate);
  document.getElementById("tokenDeleteForm")?.addEventListener("submit", onDelete);
  refreshIcons(container);
}

function renderRow(token) {
  return `
    <tr>
      <td><strong>${escapeHtml(token.name)}</strong></td>
      <td><code class="key-cell">${escapeHtml(maskKey(token.key))}</code></td>
      <td class="text-sm">${escapeHtml(formatTime(token.created_at))}</td>
      <td>
        <div class="row inline-actions">
          <button class="btn-secondary btn-sm" data-token-view="${token.id}" title="${t("view")}">
            <i data-lucide="eye"></i><span>${t("view")}</span>
          </button>
          <button class="btn-danger btn-sm" data-token-delete="${token.id}" title="${t("delete")}">
            <i data-lucide="trash-2"></i><span>${t("delete")}</span>
          </button>
        </div>
      </td>
    </tr>`;
}

function renderCreateModal() {
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="tokenCreateTitle">
      <div class="modal-backdrop" data-token-close></div>
      <div class="modal-panel token-modal-panel">
        <header class="modal-head">
          <div>
            <h2 id="tokenCreateTitle">${t("createKey")}</h2>
            <p>${t("keyDescription")}</p>
          </div>
          <button class="icon-btn" data-token-close aria-label="${t("cancel")}">
            <i data-lucide="x"></i>
          </button>
        </header>
        <form id="tokenCreateForm" class="modal-form">
          <div class="form-grid">
            <label class="span-2">
              <span>${t("keyName")}</span>
              <input name="name" placeholder="${t("keyNamePlaceholder")}" required autofocus>
            </label>
          </div>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-token-close>${t("cancel")}</button>
            <button type="submit" class="btn-primary">
              <i data-lucide="sparkles"></i>${t("generateKey")}
            </button>
          </footer>
        </form>
      </div>
    </div>`;
}

function renderViewModal(token) {
  const isRevealed = Boolean(token.key && String(token.key).includes("•"));
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="tokenViewTitle">
      <div class="modal-backdrop" data-token-close></div>
      <div class="modal-panel token-modal-panel">
        <header class="modal-head">
          <div class="token-title">
            <div class="token-title-icon"><i data-lucide="key-round"></i></div>
            <div>
              <h2 id="tokenViewTitle">${escapeHtml(token.name)}</h2>
              <p>${escapeHtml(formatTime(token.created_at))}</p>
            </div>
          </div>
          <button class="icon-btn" data-token-close aria-label="${t("cancel")}">
            <i data-lucide="x"></i>
          </button>
        </header>
        <div class="modal-form">
          ${isRevealed ? "" : `
            <div class="token-warn">
              <i data-lucide="shield-alert"></i>
              <div>
                <strong>${t("securityTip")}</strong>
                <span>${t("securityTipBody")}</span>
              </div>
            </div>`}
          <label class="token-key-label">
            <span>${t("apiKeys")}</span>
            <div class="token-key-box">
              <code>${escapeHtml(token.key)}</code>
              <button type="button" class="btn-secondary btn-sm" data-copy-value="${escapeHtml(token.key)}">
                <i data-lucide="copy"></i><span>${t("copyKey")}</span>
              </button>
            </div>
          </label>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-token-close>${t("closeAfterCopy")}</button>
            <button type="button" class="btn-danger btn-sm" data-token-delete="${token.id}">
              <i data-lucide="trash-2"></i>${t("delete")}
            </button>
          </footer>
        </div>
      </div>
    </div>`;
}

function renderDeleteModal(token) {
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="tokenDeleteTitle">
      <div class="modal-backdrop" data-token-close></div>
      <div class="modal-panel confirm-panel">
        <header class="confirm-head">
          <div class="confirm-icon danger"><i data-lucide="alert-triangle"></i></div>
          <h2 id="tokenDeleteTitle">${t("deleteKeyTitle")}</h2>
        </header>
        <form id="tokenDeleteForm" class="modal-form">
          <div class="confirm-body">
            <div class="confirm-target">
              <span class="muted">${t("deleteKeyNameLabel")}</span>
              <strong>${escapeHtml(token.name)}</strong>
              <code class="key-cell">${escapeHtml(maskKey(token.key))}</code>
            </div>
            <p class="confirm-warning">${t("deleteKeyWarning")}</p>
          </div>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-token-close>${t("cancel")}</button>
            <button type="submit" class="btn-danger">
              <i data-lucide="trash-2"></i>${t("confirmDelete")}
            </button>
          </footer>
        </form>
      </div>
    </div>`;
}

function maskKey(key) {
  const value = String(key || "");
  if (value.length <= 14) return value;
  return `${value.slice(0, 7)}••••${value.slice(-4)}`;
}

async function onCreate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;

  try {
    const formData = new FormData(form);
    const params = new URLSearchParams();
    params.set("name", formData.get("name"));
    const token = await api(`/api/admin/tokens/generate?${params}`, { method: "POST" });
    await copyText(token.key);
    state.createOpen = false;
    state.viewing = token;
    await refreshTokens();
    toast(t("generated"), "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
}

async function onDelete(event) {
  event.preventDefault();
  const token = state.deleting;
  if (!token) return;
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;

  try {
    await api(`/api/admin/tokens/${token.id}`, { method: "DELETE" });
    state.deleting = null;
    state.viewing = null;
    await refreshTokens();
    toast(t("deleted"), "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
}

async function refreshTokens() {
  state.tokens = await api("/api/admin/tokens");
  render();
}

export async function onClick(target) {
  if (target.closest("[data-token-create-open]")) {
    state.createOpen = true;
    state.viewing = null;
    state.deleting = null;
    render();
    return true;
  }

  // Delete (from row or from inside the View modal) opens the confirm modal.
  const deleteFromView = target.closest("[data-token-delete]");
  if (deleteFromView) {
    const tokenId = Number(deleteFromView.dataset.tokenDelete);
    const token = state.tokens.find((tk) => tk.id === tokenId) || state.viewing;
    if (token) {
      state.deleting = token;
      state.viewing = null;
      state.createOpen = false;
    }
    render();
    return true;
  }

  if (target.closest("[data-token-close]")) {
    state.createOpen = false;
    state.viewing = null;
    state.deleting = null;
    render();
    return true;
  }

  const viewBtn = target.closest("[data-token-view]");
  if (viewBtn) {
    const tokenId = Number(viewBtn.dataset.tokenView);
    state.viewing = state.tokens.find((tk) => tk.id === tokenId) || null;
    state.createOpen = false;
    state.deleting = null;
    render();
    return true;
  }

  return false;
}
