// Usage page — per-model token histograms + model donut + channel success bars.

import { api } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml, formatNumber, formatRequestCount, formatLatency, skeletonKpis, refreshIcons, badge,
} from "../ui.js?v=11";
import { renderStackedBar, renderDonut, renderHBar, isAvailable } from "../charts.js?v=8";
import {
  CALENDAR_PERIODS, bucketKey, localDate, periodDates, periodQuery, todayInTimezone,
} from "../periods.js";

let state = {
  range: "week",
  selectedDate: localDate(),
  displayTimezone: "",
  modelFilter: "",
  channelFilter: "",
  stats: null,
  timeline: [],       // raw long-format rows from timeseries_by_model
  timelineLoaded: false,
  modelsAgg: [],      // /logs/models
  channels: [],
};

export async function load() {
  const container = document.getElementById("usage");
  skeletonKpis(container);
  try {
    if (state.channels.length === 0) {
      state.channels = await api("/api/admin/channels").catch(() => []);
    }
    if (!state.displayTimezone) {
      const settings = await api("/api/admin/settings").catch(() => null);
      state.displayTimezone = settings?.display_timezone || "Asia/Shanghai";
      state.selectedDate = todayInTimezone(state.displayTimezone);
    }
    await fetchAll();
    render();
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
  }
}

function filterQuery() {
  const params = new URLSearchParams();
  if (state.channelFilter) params.set("channel_id", state.channelFilter);
  if (state.modelFilter) params.set("model", state.modelFilter);
  return params.toString() ? `&${params.toString()}` : "";
}

async function fetchAll() {
  const range = CALENDAR_PERIODS.find((r) => r.key === state.range) || CALENDAR_PERIODS[1];
  const period = periodQuery(range.key, state.selectedDate);
  const q = filterQuery();
  const [stats, timeline, modelsAgg] = await Promise.all([
    api(`/api/admin/logs/stats?${period}${q}`).catch(() => null),
    api(`/api/admin/logs/timeseries_by_model?${period}&bucket=hour${q}`).catch(() => null),
    api(`/api/admin/logs/models?${period}&limit=8${q}`).catch(() => []),
  ]);
  state.stats = stats;
  state.timelineLoaded = Array.isArray(timeline);
  state.timeline = timeline || [];
  state.modelsAgg = modelsAgg;
}

export async function setRange(range) { state.range = range; await load(); }
export async function setFilter(key, value) { state[key] = value; await load(); }
export function setDisplayTimezone(value) { state.displayTimezone = value || "Asia/Shanghai"; }

export function render() {
  const container = document.getElementById("usage");
  // The chart rows are already constrained to the selected range and filters.
  // Derive the headline token total from those same rows so the KPI can never
  // accidentally show an all-time aggregate while the request KPI/chart show
  // only 7 or 30 days.
  const rangedTokenTotal = state.timeline.reduce(
    (total, row) => total + Number(row.tokens || 0),
    0,
  );
  const s = {
    ...(state.stats || {}),
    total_tokens: state.timelineLoaded
      ? rangedTokenTotal
      : (state.stats?.total_tokens || 0),
  };
  const range = CALENDAR_PERIODS.find((r) => r.key === state.range) || CALENDAR_PERIODS[1];
  const successRate = s.total_requests ? ((s.success_requests / s.total_requests) * 100) : 0;
  const chartAvailable = isAvailable();
  const filtering = state.modelFilter || state.channelFilter;

  // distinct models for the filter dropdown
  const knownModels = new Set(state.modelsAgg.map((m) => m.model));
  for (const row of state.timeline) knownModels.add(row.model);

  container.innerHTML = `
    <div class="section-head">
      <div><h2>${t("usage")}</h2><p>${t("usageHint")}</p></div>
      <div class="usage-period-controls">
        <div class="range-switch" id="rangeSwitch">
        ${CALENDAR_PERIODS.map((r) => `<button data-range="${r.key}" class="${r.key === state.range ? "active" : ""}">${r.label}</button>`).join("")}
        </div>
        <label class="usage-date-picker">
          <span>${t("usageDate")}</span>
          <input class="input" id="usageDate" type="date" value="${state.selectedDate}" max="${localDate()}">
        </label>
      </div>
    </div>

    <div class="filter-bar">
      <label class="filter-label">
        <span class="muted text-sm">${t("columns").models}</span>
        <select class="select" id="usageModelFilter">
          <option value="">${t("allModels") || "全部模型"}</option>
          ${[...knownModels].sort().map((m) => `<option value="${escapeHtml(m)}" ${state.modelFilter === m ? "selected" : ""}>${escapeHtml(m)}</option>`).join("")}
        </select>
      </label>
      <label class="filter-label">
        <span class="muted text-sm">${t("channels")}</span>
        <select class="select" id="usageChannelFilter">
          <option value="">${t("allChannels")}</option>
          ${state.channels.map((c) => `<option value="${c.id}" ${String(state.channelFilter) === String(c.id) ? "selected" : ""}>${escapeHtml(c.name)} #${c.id}</option>`).join("")}
        </select>
      </label>
      ${filtering ? `<button class="btn-ghost" id="clearUsageFilter"><i data-lucide="x"></i>${t("clearFilter")}</button>` : ""}
    </div>

    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="activity"></i>${t("metrics")[0]}</div>
        <div class="kpi-value">${formatRequestCount(s.total_requests)}</div>
        <div class="kpi-delta">${formatRequestCount(s.failed_requests)} ${t("failed")}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="coins"></i>${t("metrics")[3]}</div>
        <div class="kpi-value">${formatNumber(s.total_tokens)}</div>
        <div class="kpi-delta">${formatNumber(s.completion_tokens || 0)} out</div>
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="check-circle"></i>${t("successRate") || "Success"}</div>
        <div class="kpi-value">${successRate.toFixed(1)}%</div>
        <div class="kpi-delta">${formatRequestCount(s.success_requests)} ok</div>
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="gauge"></i>${t("metrics")[7]}</div>
        <div class="kpi-value">${formatLatency(s.avg_latency)}</div>
        <div class="kpi-delta">${state.timeline.length} ${t("buckets")}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="zap"></i>${t("cacheHitRate") || "Cache hit"}</div>
        <div class="kpi-value">${
          (s.prompt_tokens || 0) > 0
            ? Number(s.cache_hit_rate || 0).toFixed(1) + "%"
            : "—"
        }</div>
        <div class="kpi-delta">${formatNumber(s.cached_tokens || 0)} ${t("cacheHitTokens") || "hits"}</div>
      </div>
    </div>

    <div class="chart-card">
      <div class="card-head">
        <div class="card-title">${t("tokenByModel") || "按模型的 Token 消耗"}</div>
        <div class="card-sub">${chartAvailable ? `${state.selectedDate} · ${distinctModels(state.timeline).length} ${t("models")}` : t("chartUnavailable")}</div>
      </div>
      <div class="chart-canvas-wrap" style="height:320px">
        ${chartAvailable ? `<canvas id="stackedChart"></canvas>` : renderStackedFallback()}
      </div>
    </div>

    <div class="usage-grid">
      <div class="chart-card">
        <div class="card-head">
          <div class="card-title">${t("modelDist")}</div>
          <div class="card-sub">${state.modelsAgg.length} models</div>
        </div>
        <div class="chart-canvas-wrap" style="height:240px">
          ${chartAvailable ? `<canvas id="donutChart"></canvas>` : renderModelsFallback()}
        </div>
      </div>
      <div class="chart-card">
        <div class="card-head">
          <div class="card-title">${t("channelSuccess")}</div>
          <div class="card-sub">${state.channels.length} channels</div>
        </div>
        <div class="chart-canvas-wrap" style="height:240px">
          ${chartAvailable ? `<canvas id="hbarChart"></canvas>` : renderChannelsFallback()}
        </div>
      </div>
    </div>

    <div class="chart-card" style="margin-top:14px">
      <div class="card-head">
        <div class="card-title">${t("modelTableTitle") || "按模型明细"}</div>
        <div class="card-sub">${t("modelTableHint") || "输入/输出/缓存命中按模型拆分"}</div>
      </div>
      <div class="table-wrap">
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>${t("columns").models}</th>
              <th>${t("metrics")[0]}</th>
              <th>${t("metrics")[4] || "输入"}</th>
              <th>${t("metrics")[5] || "输出"}</th>
              <th>${t("cacheHitTokens") || "缓存命中"}</th>
              <th>${t("cacheHitRate") || "命中率"}</th>
            </tr>
          </thead>
          <tbody>
            ${(state.modelsAgg || []).map((m) => {
              const prompt = Number(m.prompt_tokens || 0);
              const completion = Number(m.completion_tokens || 0);
              const cached = Number(m.cached_tokens || 0);
              const rate = Number(m.cache_hit_rate || 0);
              const rateBadge = cached > 0
                ? badge(rate.toFixed(1) + "%", rate >= 50 ? "success" : rate >= 10 ? "info" : "")
                : '<span class="muted">—</span>';
              return `<tr>
                <td><code>${escapeHtml(m.model)}</code></td>
                <td class="mono">${formatRequestCount(m.request_count)}</td>
                <td class="mono">${formatNumber(prompt)}</td>
                <td class="mono">${formatNumber(completion)}</td>
                <td class="mono">${formatNumber(cached)}</td>
                <td>${rateBadge}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
      </div>
    </div>
  `;

  bindControls();
  refreshIcons(container);
  if (chartAvailable) drawCharts(range);
}

function bindControls() {
  document.getElementById("rangeSwitch")?.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (btn) setRange(btn.dataset.range);
  });
  document.getElementById("usageModelFilter")?.addEventListener("change", (e) => setFilter("modelFilter", e.target.value));
  document.getElementById("usageChannelFilter")?.addEventListener("change", (e) => setFilter("channelFilter", e.target.value));
  document.getElementById("usageDate")?.addEventListener("change", (e) => {
    if (!e.target.value) return;
    state.selectedDate = e.target.value;
    setRange("day");
  });
  document.getElementById("clearUsageFilter")?.addEventListener("click", () => {
    state.modelFilter = "";
    state.channelFilter = "";
    load();
  });
}

function drawCharts(range) {
  // Grouped histogram: every model starts from the same baseline, with one
  // colored bar per model inside each time bucket.
  const { labels, datasets } = pivotByModel(state.timeline, range.key);
  const stackedCanvas = document.getElementById("stackedChart");
  if (stackedCanvas) {
    if (datasets.length) {
      renderStackedBar("stacked", stackedCanvas, labels, datasets, {
        yTitle: t("metrics")[3] || "Tokens",
        tooltipCallbacks: {
          label: (ctx) => ` ${ctx.dataset.label}: ${formatNumber(ctx.parsed.y)}`,
        },
      });
    } else {
      stackedCanvas.parentElement.innerHTML = `<div class="empty-state"><i data-lucide="line-chart"></i><h3>${t("noData")}</h3></div>`;
    }
  }

  const donutCanvas = document.getElementById("donutChart");
  if (donutCanvas) {
    const labels = state.modelsAgg.map((m) => m.model);
    const values = state.modelsAgg.map((m) => m.request_count);
    if (labels.length) renderDonut("donut", donutCanvas, labels, values);
    else donutCanvas.parentElement.innerHTML = `<div class="empty-state"><i data-lucide="pie-chart"></i><h3>${t("noData")}</h3></div>`;
  }

  const hbarCanvas = document.getElementById("hbarChart");
  if (hbarCanvas) {
    const items = state.channels
      .map((c) => ({ name: c.name, rate: c.total_requests ? (c.success_requests / c.total_requests) * 100 : 0 }))
      .sort((a, b) => b.rate - a.rate)
      .slice(0, 8);
    if (items.length) {
      renderHBar("hbar", hbarCanvas, items.map((i) => i.name), items.map((i) => i.rate), { label: "%" });
    } else {
      hbarCanvas.parentElement.innerHTML = `<div class="empty-state"><i data-lucide="bar-chart-3"></i><h3>${t("noData")}</h3></div>`;
    }
  }
}

function distinctModels(rows) {
  return [...new Set(rows.map((r) => r.model))];
}

function pivotByModel(rows, period) {
  const range = CALENDAR_PERIODS.find((r) => r.key === state.range) || CALENDAR_PERIODS[1];
  const buckets = expectedBuckets(range);
  const models = distinctModels(rows).sort();
  const lookup = new Map();
  rows.forEach((row) => {
    const key = `${bucketKey(row.bucket, period, state.displayTimezone)}|${row.model}`;
    lookup.set(key, (lookup.get(key) || 0) + Number(row.tokens || 0));
  });
  const labels = buckets.map((bucket) => period === "day" ? bucket.slice(11, 16) : bucket.slice(5));
  const datasets = models
    .map((m) => {
      const data = buckets.map((b) => lookup.get(`${b}|${m}`) || 0);
      return {
        label: m,
        data,
        total: data.reduce((sum, value) => sum + value, 0),
      };
    })
    .sort((a, b) => b.total - a.total || a.label.localeCompare(b.label));
  return { labels, datasets };
}

function expectedBuckets(range) {
  if (range.key === "day") {
    return Array.from({ length: 24 }, (_, hour) => `${state.selectedDate}T${pad(hour)}:00:00`);
  }
  return periodDates(range.key, state.selectedDate);
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function renderStackedFallback() {
  const total = state.timeline.reduce((a, b) => a + (b.tokens || 0), 0);
  return `<div class="empty-state">
    <i data-lucide="bar-chart-3"></i>
    <h3>${formatNumber(total)} ${t("metrics")[3]}</h3>
    <p class="muted">${t("chartUnavailable")}</p>
  </div>`;
}

function renderModelsFallback() {
  if (!state.modelsAgg.length) return `<div class="empty-state"><i data-lucide="pie-chart"></i></div>`;
  const max = Math.max(...state.modelsAgg.map((m) => m.request_count), 1);
  return state.modelsAgg.map((m) => `
    <div class="row" style="gap:10px;margin-bottom:8px">
      <code style="min-width:140px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(m.model)}</code>
      <div class="progress" style="flex:1"><span style="width:${(m.request_count / max) * 100}%"></span></div>
      <span class="mono text-sm" style="min-width:60px;text-align:right">${formatRequestCount(m.request_count)}</span>
    </div>`).join("");
}

function renderChannelsFallback() {
  if (!state.channels.length) return `<div class="empty-state"><i data-lucide="bar-chart-3"></i></div>`;
  return state.channels.slice(0, 8).map((c) => {
    const rate = c.total_requests ? Math.round((c.success_requests / c.total_requests) * 100) : 0;
    const cls = rate >= 95 ? "" : rate >= 80 ? "warn" : "danger";
    return `<div class="row" style="gap:10px;margin-bottom:8px">
      <code style="min-width:100px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(c.name)}</code>
      <div class="progress ${cls}" style="flex:1"><span style="width:${rate}%"></span></div>
      <span class="mono text-sm" style="min-width:50px;text-align:right">${rate}%</span>
    </div>`;
  }).join("");
}

export async function onClick() { return false; }
