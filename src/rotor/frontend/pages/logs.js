// Logs page — filter bar + paginated table + expandable detail.

import { api } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml, badge, relativeTime, formatTime, formatNumber, skeletonRows,
  refreshIcons, toast,
} from "../ui.js";

const PAGE_SIZE = 50;
let state = {
  logs: [],
  total: 0,
  page: 0,
  filter: { model: "", channel: "", status: "", q: "" },
  models: [],
  channels: [],
  autoRefresh: false,
  expanded: new Set(),
  detailCache: {},
  seen: new Set(),
  timer: null,
};

export async function load() {
  const container = document.getElementById("logs");
  skeletonRows(container, 5, 8);
  try {
    if (state.models.length === 0) {
      const [models, channels] = await Promise.all([
        api("/api/admin/logs/models?days=30&limit=30").catch(() => []),
        api("/api/admin/channels").catch(() => []),
      ]);
      state.models = models.map((m) => m.model);
      state.channels = channels;
    }
    await fetchPage();
    render();
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
  }
}

async function fetchPage() {
  const skip = state.page * PAGE_SIZE;
  state.logs = await api(`/api/admin/logs?limit=${PAGE_SIZE}&skip=${skip}${buildFilterQuery()}`);
  state.seen = new Set(state.logs.map((l) => l.id));
}

function buildFilterQuery() {
  const params = new URLSearchParams();
  if (state.filter.model) params.set("model", state.filter.model);
  if (state.filter.channel) params.set("channel_id", state.filter.channel);
  if (state.filter.status === "success") params.set("success", "true");
  if (state.filter.status === "failed") params.set("success", "false");
  return params.toString() ? `&${params.toString()}` : "";
}

export function render() {
  const container = document.getElementById("logs");
  const cols = t("columns");
  const filtered = applyClientFilter(state.logs);
  const totalPages = Math.max(1, Math.ceil((state.total || filtered.length) / PAGE_SIZE));

  container.innerHTML = `
    <div class="section-head">
      <div>
        <h2>${t("logs")}</h2>
        <p>${t("logsHint")}</p>
      </div>
      <div class="row">
        <label class="toggle-row">
          <span class="switch"><input type="checkbox" id="logAutoRefresh" ${state.autoRefresh ? "checked" : ""}><span></span>
          ${t("autoRefresh")}
        </label>
        <button class="btn-secondary" id="refreshLogsData"><i data-lucide="refresh-cw"></i>${t("refresh")}</button>
      </div>
    </div>

    <div class="filter-bar">
      <input class="input" id="logModelFilter" list="modelList" placeholder="${cols.models}" value="${escapeHtml(state.filter.model)}">
      <datalist id="modelList">${state.models.map((m) => `<option value="${escapeHtml(m)}">`).join("")}</datalist>
      <select class="select" id="logChannelFilter">
        <option value="">${cols.channel}</option>
        ${state.channels.map((c) => `<option value="${c.id}" ${String(state.filter.channel) === String(c.id) ? "selected" : ""}>${escapeHtml(c.name)} #${c.id}</option>`).join("")}
      </select>
      <select class="select" id="logStatusFilter">
        <option value="">${t("allStatus")}</option>
        <option value="success" ${state.filter.status === "success" ? "selected" : ""}>${t("reachable")}</option>
        <option value="failed" ${state.filter.status === "failed" ? "selected" : ""}>${t("failed")}</option>
      </select>
      <input class="input" id="logSearch" placeholder="${t("searchError")}" value="${escapeHtml(state.filter.q)}">
    </div>

    ${filtered.length === 0
      ? `<div class="empty-state"><i data-lucide="scroll-text"></i><h3>${t("noLogs")}</h3></div>`
      : `<div class="table-wrap"><div class="table-scroll"><table>
          <thead><tr>
            <th></th><th>${cols.time}</th><th>${cols.models}</th><th>${cols.token}</th><th>${cols.channel}</th>
            <th>${cols.tokens}</th><th>${cols.latency}</th><th>${cols.status}</th>
          </tr></thead>
          <tbody>
            ${filtered.map(renderRow).join("")}
          </tbody>
        </table></div></div>`}

    ${renderPagination(totalPages)}
  `;

  bindControls();
  refreshIcons(container);
  // lazy-load detail for expanded rows
  for (const id of state.expanded) loadDetail(id);
}

function renderRow(log) {
  const channel = state.channels.find((c) => c.id === log.channel_id);
  const chLabel = channel ? escapeHtml(channel.name) : (log.channel_id ?? "—");
  const isFail = !log.success;
  const expanded = state.expanded.has(log.id);
  const detail = state.detailCache[log.id];

  return `
    <tr class="log-row ${isFail ? "failed" : ""}">
      <td><button class="icon-btn" data-log-expand="${log.id}">
        <i data-lucide="${expanded ? "chevron-down" : "chevron-right"}"></i>
      </button></td>
      <td><span title="${escapeHtml(formatTime(log.created_at))}" class="mono text-sm">${escapeHtml(relativeTime(log.created_at))}</span></td>
      <td><code>${escapeHtml(log.model)}</code></td>
      <td class="muted mono">${log.token_id ?? "—"}</td>
      <td>
        <a class="text-sm" data-filter-channel="${log.channel_id ?? ""}">${chLabel}</a>
      </td>
      <td class="mono">${formatNumber(log.total_tokens)}</td>
      <td class="mono text-sm">${log.latency != null ? log.latency.toFixed(2) + "s" : "—"}</td>
      <td>${log.success ? badge(t("reachable"), "success") : badge(t("failed"), "danger")}</td>
    </tr>
    ${expanded ? `<tr class="log-detail"><td colspan="8">${renderDetail(log, detail)}</td></tr>` : ""}
  `;
}

function renderDetail(log, detail) {
  if (!detail) {
    return `<div class="skeleton block" style="height:120px"></div>`;
  }
  return `
    <div class="col" style="gap:14px;padding:8px 4px">
      <div class="row" style="gap:18px;flex-wrap:wrap">
        <div><span class="muted text-sm">ID</span> <code>#${log.id}</code></div>
        <div><span class="muted text-sm">IP</span> <code>${escapeHtml(log.ip || "—")}</code></div>
        <div><span class="muted text-sm">Request model</span> <code>${escapeHtml(log.request_model || "—")}</code></div>
        ${log.error_code ? `<div><span class="muted text-sm">Error</span> <code>${escapeHtml(log.error_code)}</code></div>` : ""}
      </div>
      ${log.error_message ? `
        <div>
          <div class="muted text-sm mb-2">${t("columns").error}</div>
          <pre class="code-block error-block">${escapeHtml(log.error_message)}</pre>
        </div>` : ""}
      <div class="detail-grid">
        <div>
          <div class="muted text-sm mb-2">Request body</div>
          <pre class="code-block">${detail.request_body ? escapeHtml(JSON.stringify(detail.request_body, null, 2)) : "—"}</pre>
        </div>
        <div>
          <div class="muted text-sm mb-2">Response body</div>
          <pre class="code-block">${detail.response_body ? escapeHtml(JSON.stringify(detail.response_body, null, 2)) : "—"}</pre>
        </div>
      </div>
    </div>
  `;
}

function renderPagination(totalPages) {
  if (totalPages <= 1 && state.page === 0) return "";
  return `
    <div class="pagination">
      <button class="btn-secondary" id="logPrev" ${state.page === 0 ? "disabled" : ""}>
        <i data-lucide="chevron-left"></i>${t("prev") || "上一页"}
      </button>
      <span class="muted text-sm">${state.page + 1} / ${totalPages}</span>
      <button class="btn-secondary" id="logNext" ${state.page >= totalPages - 1 ? "disabled" : ""}>
        ${t("next") || "下一页"}<i data-lucide="chevron-right"></i>
      </button>
    </div>
  `;
}

function applyClientFilter(logs) {
  return logs.filter((l) => {
    if (state.filter.q) {
      const text = `${l.error_message || ""} ${l.error_code || ""} ${l.model || ""}`.toLowerCase();
      if (!text.includes(state.filter.q.toLowerCase())) return false;
    }
    return true;
  });
}

function bindControls() {
  document.getElementById("logModelFilter")?.addEventListener("change", (e) => { state.filter.model = e.target.value; state.page = 0; load(); });
  document.getElementById("logChannelFilter")?.addEventListener("change", (e) => { state.filter.channel = e.target.value; state.page = 0; load(); });
  document.getElementById("logStatusFilter")?.addEventListener("change", (e) => { state.filter.status = e.target.value; state.page = 0; load(); });
  document.getElementById("logSearch")?.addEventListener("input", (e) => { state.filter.q = e.target.value; debouncedRender(); });
  document.getElementById("refreshLogsData")?.addEventListener("click", () => load());
  document.getElementById("logAutoRefresh")?.addEventListener("change", (e) => toggleAutoRefresh(e.target.checked));
  document.getElementById("logPrev")?.addEventListener("click", () => { if (state.page > 0) { state.page--; load(); } });
  document.getElementById("logNext")?.addEventListener("click", () => { state.page++; load(); });
}

let renderTimer;
function debouncedRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 200);
}

function toggleAutoRefresh(on) {
  state.autoRefresh = on;
  clearInterval(state.timer);
  if (on) {
    state.timer = setInterval(pollNew, 5000);
    toast(t("autoRefreshOn"), "info");
  }
}

async function pollNew() {
  try {
    const fresh = await api(`/api/admin/logs?limit=${PAGE_SIZE}${buildFilterQuery()}`);
    const newOnes = fresh.filter((l) => !state.seen.has(l.id));
    state.seen = new Set(fresh.map((l) => l.id));
    if (state.page === 0) {
      state.logs = fresh;
      if (newOnes.length) render();
    }
  } catch (e) { /* swallow */ }
}

async function loadDetail(id) {
  if (state.detailCache[id]) { render(); return; }
  try {
    const detail = await api(`/api/admin/logs/${id}`);
    state.detailCache[id] = detail;
    render();
  } catch (e) { /* keep skeleton */ }
}

export async function onClick(target) {
  const expand = target.closest("[data-log-expand]")?.dataset.logExpand;
  if (expand) {
    const id = Number(expand);
    if (state.expanded.has(id)) {
      state.expanded.delete(id);
      render();
    } else {
      state.expanded.add(id);
      if (!state.detailCache[id]) loadDetail(id);
      else render();
    }
    return true;
  }

  const filterChannel = target.closest("[data-filter-channel]")?.dataset.filterChannel;
  if (filterChannel) {
    state.filter.channel = filterChannel;
    state.page = 0;
    load();
    return true;
  }
  return false;
}
