// Logs page — filter bar + paginated table + expandable detail.

import { api } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml, badge, formatNumber, formatTimeInTimezone, skeletonRows,
  refreshIcons, toast,
} from "../ui.js?v=12";
import { renderStackedBar, isAvailable } from "../charts.js?v=8";
import {
  CALENDAR_PERIODS, bucketKey, localDate, periodDates, periodQuery, todayInTimezone,
} from "../periods.js";

const PAGE_SIZE = 50;
let state = {
  logs: [],
  total: 0,
  page: 0,
  range: "week",
  selectedDate: localDate(),
  displayTimezone: "",
  timeline: [],
  filter: { model: "", channel: "", status: "", q: "" },
  models: [],
  channels: [],
  autoRefresh: false,
  expanded: new Set(),
  detailCache: {},
  detailLoading: new Set(),
  detailErrors: {},
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
    if (!state.displayTimezone) {
      const settings = await api("/api/admin/settings").catch(() => null);
      state.displayTimezone = settings?.display_timezone || "Asia/Shanghai";
      state.selectedDate = todayInTimezone(state.displayTimezone);
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
  const filters = buildFilterQuery();
  const [logs, count, timeline] = await Promise.all([
    api(`/api/admin/logs?limit=${PAGE_SIZE}&skip=${skip}${filters}`),
    api(`/api/admin/logs/count?${filters.slice(1)}`),
    api(`/api/admin/logs/timeseries?bucket=hour${filters}`).catch(() => []),
  ]);
  state.logs = logs;
  state.total = count.count;
  state.timeline = timeline;
  state.seen = new Set(state.logs.map((l) => l.id));
}

function buildFilterQuery() {
  const params = new URLSearchParams();
  if (state.filter.model) params.set("model", state.filter.model);
  if (state.filter.channel) params.set("channel_id", state.filter.channel);
  if (state.filter.status === "success") params.set("success", "true");
  if (state.filter.status === "failed") params.set("success", "false");
  params.set("period", state.range);
  params.set("period_date", state.selectedDate);
  return params.toString() ? `&${params.toString()}` : "";
}

export function render() {
  const container = document.getElementById("logs");
  const cols = t("columns");
  const filtered = applyClientFilter(state.logs);
  const totalItems = state.filter.q ? filtered.length : state.total;
  const totalPages = state.filter.q ? 1 : Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const chartAvailable = isAvailable();
  const range = CALENDAR_PERIODS.find((item) => item.key === state.range) || CALENDAR_PERIODS[1];

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

    <div class="usage-period-controls logs-period-controls">
      <div class="range-switch" id="logRangeSwitch">
        ${CALENDAR_PERIODS.map((item) => `<button data-log-range="${item.key}" class="${item.key === state.range ? "active" : ""}">${item.label}</button>`).join("")}
      </div>
      <label class="usage-date-picker">
        <span>${t("usageDate")}</span>
        <input class="input" id="logDate" type="date" value="${state.selectedDate}" max="${localDate()}">
      </label>
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

    <div class="chart-card log-chart-card">
      <div class="card-head">
        <div class="card-title">${t("requestTrend")}</div>
        <div class="card-sub">${state.selectedDate} · ${state.displayTimezone}</div>
      </div>
      <div class="chart-canvas-wrap" style="height:260px">
        ${chartAvailable ? `<canvas id="logRequestsChart"></canvas>` : renderChartFallback()}
      </div>
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

    ${renderPagination(totalPages, totalItems)}
  `;

  bindControls();
  refreshIcons(container);
  if (chartAvailable) drawChart(range);
}

function renderRow(log) {
  const channel = state.channels.find((c) => c.id === log.channel_id);
  const chLabel = channel ? escapeHtml(channel.name) : (log.channel_id ?? "—");
  const isFail = !log.success;
  const expanded = state.expanded.has(log.id);
  const detail = state.detailCache[log.id];
  const createdAt = formatTimeInTimezone(log.created_at, state.displayTimezone);

  return `
    <tr class="log-row ${isFail ? "failed" : ""} ${expanded ? "expanded" : ""}">
      <td><button class="icon-btn log-expand-btn" type="button" data-log-expand="${log.id}"
        aria-expanded="${expanded}" aria-label="${t("logDetails")}">
        <i data-lucide="${expanded ? "chevron-down" : "chevron-right"}"></i>
      </button></td>
      <td><span class="mono text-sm">${escapeHtml(createdAt)}</span></td>
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
    const error = state.detailErrors[log.id];
    if (error) {
      return `<div class="log-detail-error" role="alert">
        <span>${escapeHtml(t("logDetailFailed"))}: ${escapeHtml(error)}</span>
        <button class="btn-secondary" type="button" data-log-detail-retry="${log.id}">
          <i data-lucide="refresh-cw"></i>${t("retry")}
        </button>
      </div>`;
    }
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

function renderPagination(totalPages, totalItems) {
  if (totalPages <= 1 && state.page === 0) return "";
  const first = totalItems ? state.page * PAGE_SIZE + 1 : 0;
  const last = Math.min((state.page + 1) * PAGE_SIZE, totalItems);
  return `
    <div class="pagination">
      <button class="btn-secondary" id="logPrev" ${state.page === 0 ? "disabled" : ""}>
        <i data-lucide="chevron-left"></i>${t("prev") || "上一页"}
      </button>
      <span class="muted text-sm">${first}–${last} / ${totalItems}</span>
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
  document.getElementById("logSearch")?.addEventListener("input", (e) => { state.filter.q = e.target.value; state.page = 0; debouncedRender(); });
  document.getElementById("logRangeSwitch")?.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-log-range]");
    if (!button) return;
    state.range = button.dataset.logRange;
    state.page = 0;
    load();
  });
  document.getElementById("logDate")?.addEventListener("change", (e) => {
    if (!e.target.value) return;
    state.selectedDate = e.target.value;
    state.page = 0;
    load();
  });
  document.getElementById("refreshLogsData")?.addEventListener("click", () => load());
  document.getElementById("logAutoRefresh")?.addEventListener("change", (e) => toggleAutoRefresh(e.target.checked));
  document.getElementById("logPrev")?.addEventListener("click", () => { if (state.page > 0) { state.page--; load(); } });
  document.getElementById("logNext")?.addEventListener("click", () => { state.page++; load(); });
}

function drawChart(range) {
  const canvas = document.getElementById("logRequestsChart");
  if (!canvas) return;
  const buckets = range.key === "day"
    ? Array.from({ length: 24 }, (_, hour) => `${state.selectedDate}T${String(hour).padStart(2, "0")}:00:00`)
    : periodDates(range.key, state.selectedDate);
  const values = new Map();
  state.timeline.forEach((row) => {
    const key = bucketKey(row.bucket, range.key, state.displayTimezone);
    const previous = values.get(key) || { success: 0, failed: 0 };
    previous.success += Number(row.success || 0);
    previous.failed += Number(row.failed || 0);
    values.set(key, previous);
  });
  const labels = buckets.map((bucket) => range.key === "day" ? bucket.slice(11, 16) : bucket.slice(5));
  renderStackedBar("logRequests", canvas, labels, [
    { label: t("reachable"), data: buckets.map((bucket) => values.get(bucket)?.success || 0) },
    { label: t("failed"), data: buckets.map((bucket) => values.get(bucket)?.failed || 0) },
  ], { yTitle: t("requestCount") || "Requests" });
}

function renderChartFallback() {
  return `<div class="empty-state"><i data-lucide="bar-chart-3"></i><h3>${formatNumber(state.total)} ${t("requestCount") || "Requests"}</h3></div>`;
}

export function setDisplayTimezone(value) {
  state.displayTimezone = value || "Asia/Shanghai";
}

export function showFailures() {
  state.filter.status = "failed";
  state.page = 0;
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
      state.total += newOnes.length;
      if (newOnes.length) render();
    }
  } catch (e) { /* swallow */ }
}

async function loadDetail(id) {
  if (state.detailCache[id] || state.detailLoading.has(id)) return;
  state.detailLoading.add(id);
  try {
    const detail = await api(`/api/admin/logs/${id}`);
    state.detailCache[id] = detail;
    delete state.detailErrors[id];
  } catch (e) {
    state.detailErrors[id] = e.message || t("logDetailFailed");
  } finally {
    state.detailLoading.delete(id);
    if (state.expanded.has(id) && state.logs.some((log) => log.id === id)) {
      render();
    }
  }
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
      render();
      loadDetail(id);
    }
    return true;
  }

  const retry = target.closest("[data-log-detail-retry]")?.dataset.logDetailRetry;
  if (retry) {
    const id = Number(retry);
    delete state.detailErrors[id];
    render();
    loadDetail(id);
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
