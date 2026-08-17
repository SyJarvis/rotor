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
  controlKeys: [],
  controlCreateOpen: false,
  controlViewing: null,
  controlDeleting: null,
};

export async function load() {
  const container = document.getElementById("tokens");
  skeletonRows(container, 3, 4);
  try {
    [state.tokens, state.controlKeys] = await Promise.all([
      api("/api/admin/tokens"),
      api("/api/admin/mcp-control-keys"),
    ]);
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

    <section class="mcp-control-key-section">
      <div class="section-head">
        <div>
          <h2>${t("mcpControlKeys")}</h2>
          <p>${t("mcpControlKeysDescription")}</p>
        </div>
        <button class="btn-secondary" data-control-key-create-open>
          <i data-lucide="shield-plus"></i>${t("createMcpControlKey")}
        </button>
      </div>
      ${state.controlKeys.length === 0
        ? `<div class="empty-state mcp-control-key-empty"><i data-lucide="shield-keyhole"></i><h3>${t("noMcpControlKeys")}</h3></div>`
        : `<div class="table-wrap"><div class="table-scroll"><table>
            <thead><tr>
              <th>${cols.name}</th>
              <th>${t("mcpControlKey")}</th>
              <th>${t("createdAt")}</th>
              <th>${cols.actions}</th>
            </tr></thead>
            <tbody>
              ${state.controlKeys.map(renderControlKeyRow).join("")}
            </tbody>
          </table></div></div>`}
    </section>

    ${state.createOpen ? renderCreateModal() : ""}
    ${state.viewing ? renderViewModal(state.viewing) : ""}
    ${state.deleting ? renderDeleteModal(state.deleting) : ""}
    ${state.controlCreateOpen ? renderControlKeyCreateModal() : ""}
    ${state.controlViewing ? renderControlKeyViewModal(state.controlViewing) : ""}
    ${state.controlDeleting ? renderControlKeyDeleteModal(state.controlDeleting) : ""}
  `;

  document.getElementById("tokenCreateForm")?.addEventListener("submit", onCreate);
  document.getElementById("tokenDeleteForm")?.addEventListener("submit", onDelete);
  document.getElementById("controlKeyCreateForm")?.addEventListener("submit", onControlKeyCreate);
  document.getElementById("controlKeyDeleteForm")?.addEventListener("submit", onControlKeyDelete);
  refreshIcons(container);
}

function renderControlKeyRow(key) {
  return `
    <tr>
      <td><strong>${escapeHtml(key.name)}</strong></td>
      <td><code class="key-cell">${escapeHtml(key.key_hint)}</code></td>
      <td class="text-sm">${escapeHtml(formatTime(key.created_at))}</td>
      <td>
        <div class="row inline-actions">
          <button class="btn-secondary btn-sm" data-control-key-toggle="${key.id}" title="${key.enabled ? t("disable") : t("enable")}">
            <i data-lucide="${key.enabled ? "pause" : "play"}"></i><span>${key.enabled ? t("disable") : t("enable")}</span>
          </button>
          <button class="btn-danger btn-sm" data-control-key-delete="${key.id}" title="${t("delete")}">
            <i data-lucide="trash-2"></i><span>${t("delete")}</span>
          </button>
        </div>
      </td>
    </tr>`;
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

function renderControlKeyCreateModal() {
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="controlKeyCreateTitle">
      <div class="modal-backdrop" data-control-key-close></div>
      <div class="modal-panel token-modal-panel">
        <header class="modal-head">
          <div>
            <h2 id="controlKeyCreateTitle">${t("createMcpControlKey")}</h2>
            <p>${t("mcpControlKeysDescription")}</p>
          </div>
          <button class="icon-btn" data-control-key-close aria-label="${t("cancel")}">
            <i data-lucide="x"></i>
          </button>
        </header>
        <form id="controlKeyCreateForm" class="modal-form">
          <div class="form-grid">
            <label class="span-2">
              <span>${t("keyName")}</span>
              <input name="name" placeholder="${t("mcpControlKeyNamePlaceholder")}" required autofocus>
            </label>
          </div>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-control-key-close>${t("cancel")}</button>
            <button type="submit" class="btn-primary">
              <i data-lucide="shield-plus"></i>${t("createMcpControlKey")}
            </button>
          </footer>
        </form>
      </div>
    </div>`;
}

function controlKeyEnvironment(key) {
  return [
    `ROTOR_CONTROL_API_URL=${key.control_api_url}`,
    `ROTOR_CONTROL_API_TOKEN=${key.key}`,
  ].join("\n");
}

function renderControlKeyViewModal(key) {
  const environment = controlKeyEnvironment(key);
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="controlKeyViewTitle">
      <div class="modal-backdrop" data-control-key-close></div>
      <div class="modal-panel token-modal-panel">
        <header class="modal-head">
          <div class="token-title">
            <div class="token-title-icon"><i data-lucide="shield-keyhole"></i></div>
            <div>
              <h2 id="controlKeyViewTitle">${escapeHtml(key.name)}</h2>
              <p>${t("securityTip")}</p>
            </div>
          </div>
          <button class="icon-btn" data-control-key-close aria-label="${t("cancel")}">
            <i data-lucide="x"></i>
          </button>
        </header>
        <div class="modal-form">
          <div class="token-warn">
            <i data-lucide="shield-alert"></i>
            <div>
              <strong>${t("securityTip")}</strong>
              <span>${t("mcpControlKeySecurityTip")}</span>
            </div>
          </div>
          <label class="token-key-label">
            <span>${t("mcpControlKey")}</span>
            <div class="token-key-box">
              <code>${escapeHtml(key.key)}</code>
              <button type="button" class="btn-secondary btn-sm" data-copy-value="${escapeHtml(key.key)}">
                <i data-lucide="copy"></i><span>${t("copyKey")}</span>
              </button>
            </div>
          </label>
          <label class="token-key-label">
            <span>${t("mcpControlKeyConfig")}</span>
            <div class="token-key-box control-key-config-box">
              <code>${escapeHtml(environment)}</code>
              <button type="button" class="btn-secondary btn-sm" data-copy-value="${escapeHtml(environment)}">
                <i data-lucide="copy"></i><span>${t("copyMcpConfig")}</span>
              </button>
            </div>
          </label>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-control-key-close>${t("closeAfterCopy")}</button>
          </footer>
        </div>
      </div>
    </div>`;
}

function renderControlKeyDeleteModal(key) {
  return `
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="controlKeyDeleteTitle">
      <div class="modal-backdrop" data-control-key-close></div>
      <div class="modal-panel confirm-panel">
        <header class="confirm-head">
          <div class="confirm-icon danger"><i data-lucide="alert-triangle"></i></div>
          <h2 id="controlKeyDeleteTitle">${t("deleteMcpControlKeyTitle")}</h2>
        </header>
        <form id="controlKeyDeleteForm" class="modal-form">
          <div class="confirm-body">
            <div class="confirm-target">
              <span class="muted">${t("mcpControlKey")}</span>
              <strong>${escapeHtml(key.name)}</strong>
              <code class="key-cell">${escapeHtml(key.key_hint)}</code>
            </div>
            <p class="confirm-warning">${t("deleteMcpControlKeyWarning")}</p>
          </div>
          <footer class="modal-actions">
            <button type="button" class="btn-secondary" data-control-key-close>${t("cancel")}</button>
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

async function onControlKeyCreate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submitBtn = form.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;

  try {
    const key = await api("/api/admin/mcp-control-keys", {
      method: "POST",
      body: JSON.stringify({ name: new FormData(form).get("name") }),
    });
    await copyText(key.key);
    state.controlCreateOpen = false;
    state.controlViewing = key;
    await refreshTokens();
    toast(t("mcpControlKeyGenerated"), "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
}

async function onControlKeyDelete(event) {
  event.preventDefault();
  const key = state.controlDeleting;
  if (!key) return;
  const submitBtn = event.currentTarget.querySelector('button[type="submit"]');
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;

  try {
    await api(`/api/admin/mcp-control-keys/${key.id}`, { method: "DELETE" });
    state.controlDeleting = null;
    await refreshTokens();
    toast(t("deleted"), "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    submitBtn.disabled = false;
  }
}

async function refreshTokens() {
  [state.tokens, state.controlKeys] = await Promise.all([
    api("/api/admin/tokens"),
    api("/api/admin/mcp-control-keys"),
  ]);
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

  if (target.closest("[data-control-key-create-open]")) {
    state.controlCreateOpen = true;
    state.controlViewing = null;
    state.controlDeleting = null;
    render();
    return true;
  }

  const controlDelete = target.closest("[data-control-key-delete]");
  if (controlDelete) {
    const keyId = Number(controlDelete.dataset.controlKeyDelete);
    state.controlDeleting = state.controlKeys.find((key) => key.id === keyId) || null;
    state.controlCreateOpen = false;
    state.controlViewing = null;
    render();
    return true;
  }

  const controlToggle = target.closest("[data-control-key-toggle]");
  if (controlToggle) {
    const key = state.controlKeys.find(
      (item) => item.id === Number(controlToggle.dataset.controlKeyToggle)
    );
    if (!key) return true;
    try {
      await api(
        `/api/admin/mcp-control-keys/${key.id}/${key.enabled ? "disable" : "enable"}`,
        { method: "POST" },
      );
      await refreshTokens();
      toast(t("updated"), "success");
    } catch (e) {
      toast(e.message, "error");
    }
    return true;
  }

  if (target.closest("[data-control-key-close]")) {
    state.controlCreateOpen = false;
    state.controlViewing = null;
    state.controlDeleting = null;
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
