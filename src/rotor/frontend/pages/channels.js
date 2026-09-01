// Channels page — card grid with filter + actions.

import { api } from "../api.js?v=2";
import { t } from "../i18n.js";
import {
  escapeHtml, parseCsv, parseJson, badge, statusBadge,
  skeletonCards, showInline, hideInline, refreshIcons, toast,
} from "../ui.js";

let state = { channels: [], filter: { q: "", provider: "", status: "" }, testResult: {}, openMenu: null };

export const editState = { id: null };

/* ---------- modal openers ---------- */
function withForm(fn) {
  const form = document.getElementById("channelForm");
  if (!form) return;
  fn(form);
}

export function openCreate() {
  editState.id = null;
  withForm((form) => {
    form.reset();
    form.elements.models_path.value = "/models";
    form.elements.request_path.value = "/chat/completions";
    form.elements.priority.value = "1";
    form.elements.weight.value = "1";
    form.elements.enabled.checked = true;
    const keyInput = form.elements.key;
    keyInput.value = "";
    keyInput.required = true;
    keyInput.placeholder = "provider api key";
    // trigger preset population for the default type
    form.elements.type.dispatchEvent(new Event("change"));
  });
  setTitle(t("addChannelTitle"));
  document.getElementById("probeResult").className = "inline-result hidden";
  document.getElementById("channelModal").classList.remove("hidden");
  refreshIcons(document.getElementById("channelModal"));
}

export function openEdit(channel) {
  if (!channel) return;
  editState.id = channel.id;
  const extra = channel.extra || {};
  const { models_path, request_path, auth_type, ...restExtra } = extra;
  withForm((form) => {
    form.elements.name.value = channel.name || "";
    form.elements.type.value = channel.type || "openai";
    form.elements.base_url.value = channel.base_url || "";
    form.elements.protocol.value = channel.protocol || "openai";
    form.elements.auth_type.value = auth_type || "bearer";
    form.elements.models_path.value = models_path || "/models";
    form.elements.request_path.value = request_path || "/chat/completions";
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
  skeletonCards(container, 3);
  try {
    state.channels = await api("/api/admin/channels");
    render();
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
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

  return `
    <div class="channel-card ${c.enabled ? "" : "disabled"}">
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
              <i data-lucide="activity"></i>${t("test")}
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
        ${statusBadge(c.enabled)}
      </div>
      <div class="ch-base" title="${escapeHtml(c.base_url || "")}">${escapeHtml(c.base_url || "")}</div>
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
          <i data-lucide="pencil"></i>${t("edit")}
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
    if (channel) openEdit(channel);
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

async function runTest(id) {
  const card = document.querySelector(`[data-channel-test="${id}"]`)?.closest(".channel-card");
  if (card) {
    const btn = card.querySelector(`[data-channel-test="${id}"]`);
    btn.disabled = true;
    btn.innerHTML = `<i data-lucide="loader-2" class="spin"></i>${t("testing") || "..."}`;
    refreshIcons(card);
  }
  try {
    const result = await api(`/api/admin/channels/${id}/test`, { method: "POST" });
    state.testResult[id] = {
      ok: result.ok,
      message: result.ok
        ? `${t("reachable")} · ${result.latency_ms} ms · ${result.models.length} models`
        : `${t("failed")} · ${result.latency_ms} ms · ${escapeHtml(result.error || result.status_code)}`,
    };
  } catch (e) {
    state.testResult[id] = { ok: false, message: e.message };
  }
  render();
}

async function probeModels(id) {
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
    toast(error.message, "error", 5000);
  }
}
