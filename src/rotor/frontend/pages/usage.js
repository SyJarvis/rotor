// Usage page — shared filtered totals, usage trends, model and channel breakdowns.

import { api } from "../api.js?v=3";
import { isAdmin } from "../session.js?v=1";
let reportScope = "all";
const adminScope = () => isAdmin() && reportScope === "all";
const reportingBase = () => adminScope() ? "/api/admin" : "/api/me";
import { t, locale } from "../i18n.js";
import {
  escapeHtml, formatNumber, formatRequestCount, formatLatency, skeletonKpis, refreshIcons, badge, toast,
} from "../ui.js?v=12";
import { renderLine, renderDonut, renderHBar, destroyChart, isAvailable } from "../charts.js?v=10";
import {
  TIME_RANGES, bucketKey, bucketLabel, createCustomTimeRange, createTimeRange,
  dateInTimezone, expectedRangeBuckets,
} from "../periods.js?v=3";

let state = {
  range: "today", timeRange: null, startDate: "", endDate: "", displayTimezone: "",
  modelFilter: "", channelFilter: "", metric: "tokens", byModel: false, currency: "",
  stats: null, timeline: null, modelsAgg: null, channelStats: null,
  channels: null, knownModels: new Set(), errors: {}, loading: false, snapshot: null,
};
let generation = 0;
let requestController = null;
let active = false;
const chartKeys = new Set();

function clearCharts() {
  for (const key of chartKeys) destroyChart(key);
  chartKeys.clear();
}

function selectedRange() {
  return state.range === "custom"
    ? createCustomTimeRange(state.startDate, state.endDate)
    : createTimeRange(state.range, state.displayTimezone, state.snapshot || new Date());
}

export async function load() {
  active = true;
  const current = ++generation;
  requestController?.abort();
  const controller = new AbortController();
  requestController = controller;
  const options = { signal: controller.signal };
  clearCharts();
  state.loading = true;
  state.snapshot = new Date();
  state.errors = {};
  state.stats = state.timeline = state.modelsAgg = state.channelStats = null;
  const container = document.getElementById("usage");
  if (state.displayTimezone) {
    state.timeRange = selectedRange();
    state.startDate = state.timeRange.startDate;
    state.endDate = state.timeRange.endDate;
    render();
  } else {
    skeletonKpis(container);
  }

  const [settings, channels] = await Promise.allSettled([
    state.displayTimezone ? Promise.resolve({ display_timezone: state.displayTimezone }) : api(adminScope() ? "/api/admin/settings" : "/api/me", options),
    state.channels ? Promise.resolve(state.channels) : api(adminScope() ? "/api/admin/channels" : "/api/me/logs/channels", options).then((rows) => rows.map((row) => ({ ...row, id: row.id ?? row.channel_id, name: row.name ?? row.channel_name }))),
  ]);
  if (!active || current !== generation) return;
  if (settings.status !== "fulfilled" || !settings.value?.display_timezone) {
    state.loading = false;
    container.innerHTML = `<div class="empty-state" role="status"><h3>${t("usageUnavailable")}</h3><p>${t("usageTimezoneUnavailable")}</p><button class="btn-secondary mt-4" id="retryUsage">${t("retry")}</button></div>`;
    document.getElementById("retryUsage")?.addEventListener("click", load);
    return;
  }
  state.displayTimezone = settings.value.display_timezone;
  if (channels.status === "fulfilled" && Array.isArray(channels.value)) state.channels = channels.value;
  else state.errors.channelOptions = errorMessage(channels);

  const range = selectedRange();
  state.timeRange = range;
  state.startDate = range.startDate;
  state.endDate = range.endDate;
  const params = new URLSearchParams(range.query);
  if (state.channelFilter) params.set("channel_id", state.channelFilter);
  if (state.modelFilter) params.set("model", state.modelFilter);
  const query = params.toString();
  const keys = ["stats", "timeline", "modelsAgg", "channelStats"];
  const results = await Promise.allSettled([
    api(`${reportingBase()}/logs/stats?${query}`, options),
    // Aggregate hourly rows locally so calendar-day labels follow the display timezone.
    api(`${reportingBase()}/logs/timeseries_by_model?${query}&bucket=hour`, options),
    api(`${reportingBase()}/logs/models?${query}&limit=8`, options),
    api(`${reportingBase()}/logs/channels?${query}`, options),
  ]);
  if (!active || current !== generation) return;
  results.forEach((result, index) => {
    const key = keys[index];
    const valid = result.status === "fulfilled" && (key === "stats"
      ? result.value && !Array.isArray(result.value) && Number.isFinite(result.value.total_requests)
      : Array.isArray(result.value));
    if (valid) state[key] = result.value;
    else state.errors[key] = errorMessage(result);
  });
  for (const row of [...(state.timeline || []), ...(state.modelsAgg || [])]) state.knownModels.add(row.model);
  if (state.modelFilter) state.knownModels.add(state.modelFilter);
  state.loading = false;
  render();
}

function errorMessage(result) {
  return result.status === "rejected" ? result.reason?.message || t("usageUnavailable") : t("usageUnavailable");
}

export function unload() {
  active = false;
  generation += 1;
  requestController?.abort();
  clearCharts();
}

export async function setRange(key) { state.range = key; await load(); }
export async function setFilter(key, value) {
  if (!["modelFilter", "channelFilter"].includes(key)) return;
  state[key] = value;
  await load();
}
export function setDisplayTimezone(value) { state.displayTimezone = value || "Asia/Shanghai"; }

function rangeLabel(range) {
  return range.key === "custom" ? t("customDateRange") : t(`usageRange_${range.key}`);
}

function sectionMessage(key, rows, allowEmpty = false) {
  if (state.loading) return `<div class="empty-state" role="status">${t("usageLoading")}</div>`;
  if (state.errors[key]) return `<div class="empty-state usage-unavailable" role="status"><h3>${t("usageUnavailable")}</h3><p>${escapeHtml(state.errors[key])}</p></div>`;
  if (!rows?.length && !allowEmpty) return `<div class="empty-state"><h3>${t("noData")}</h3></div>`;
  return "";
}

function number(value, formatter = formatNumber) {
  return value === null || value === undefined ? "—" : formatter(value);
}

function costEntries(row) {
  if (!row || !(Number(row.costed_requests) > 0)) return [];
  const entries = Object.entries(row.cost_totals_by_currency || {})
    .filter(([, value]) => value !== null && Number.isFinite(Number(value)))
    .map(([currency, value]) => [currency, Number(value)]);
  if (!entries.length && row.currency && row.total_cost !== null && Number.isFinite(Number(row.total_cost))) {
    entries.push([row.currency, Number(row.total_cost)]);
  }
  return entries.sort(([a], [b]) => a.localeCompare(b));
}

function formatCost(value) {
  return Number(value).toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 8 });
}

function renderKpis() {
  const s = state.stats;
  const rate = s?.total_requests ? s.success_requests / s.total_requests * 100 : null;
  const cacheRate = s?.prompt_tokens ? Number(s.cache_hit_rate || 0).toFixed(1) + "%" : "—";
  const costs = costEntries(s);
  const coverage = s?.total_requests ? Number(s.costed_requests || 0) / s.total_requests * 100 : null;
  const costValue = costs.length ? costs.map(([currency, value]) => `<span>${escapeHtml(currency)} ${formatCost(value)}</span>`).join("") : "—";
  return `${state.errors.stats ? `<div class="inline-result error mb-4" role="status">${t("usageStatsUnavailable")} · ${escapeHtml(state.errors.stats)}</div>` : ""}
    <div class="kpi-grid usage-kpis">
      <div class="kpi"><div class="kpi-label"><i data-lucide="activity"></i>${t("metrics")[0]}</div>
        <div class="kpi-value">${number(s?.total_requests, formatRequestCount)}</div>
        <div class="kpi-delta">${t("failed")} ${number(s?.failed_requests, formatRequestCount)}</div></div>
      <div class="kpi"><div class="kpi-label"><i data-lucide="coins"></i>${t("metrics")[3]}</div>
        <div class="kpi-value">${number(s?.total_tokens)}</div>
        <div class="usage-kpi-details"><span>${t("metrics")[4]} ${number(s?.prompt_tokens)} · ${t("metrics")[5]} ${number(s?.completion_tokens)}</span>
        <span>${t("cacheHitTokens")} ${number(s?.cached_tokens)} (${cacheRate}) · ${t("usageCacheWrite")} ${number(s?.cache_write_tokens)}</span>
        <small>${t("usageCacheIncluded")}</small></div></div>
      <div class="kpi"><div class="kpi-label"><i data-lucide="credit-card"></i>${t("usageCosted")}</div>
        <div class="kpi-value usage-cost-value">${costValue}</div>
        <div class="usage-kpi-details"><span>${t("usageCostCoverage")} ${number(s?.costed_requests, formatRequestCount)} / ${number(s?.total_requests, formatRequestCount)}${coverage === null ? "" : ` (${coverage.toFixed(1)}%)`}</span>
        <small>${s && !costs.length ? t("usageCostUnknown") : t("usageCostHint")}</small></div></div>
      <div class="kpi"><div class="kpi-label"><i data-lucide="check-circle"></i>${t("successRate")}</div>
        <div class="kpi-value">${rate === null ? "—" : rate.toFixed(1) + "%"}</div>
        <div class="kpi-delta">${t("successLabel")} ${number(s?.success_requests, formatRequestCount)}</div></div>
      <div class="kpi"><div class="kpi-label"><i data-lucide="gauge"></i>${t("metrics")[7]}</div>
        <div class="kpi-value">${s?.total_requests ? number(s.avg_latency, formatLatency) : "—"}</div>
        <div class="kpi-delta">${t("usageSelectedScope")}</div></div>
    </div>`;
}

export function render() {
  if (!active || !state.displayTimezone) return;
  clearCharts();
  const container = document.getElementById("usage");
  const range = state.timeRange || selectedRange();
  const today = dateInTimezone(new Date(), state.displayTimezone);
  const chartAvailable = isAvailable();
  const filtering = state.modelFilter || state.channelFilter;
  const allGroups = trendGroups(state.timeline || [], range);
  if (state.metric === "cost" && !allGroups.some((group) => group.unit === state.currency)) state.currency = allGroups[0]?.unit || "";
  const groups = state.metric === "cost" ? allGroups.filter((group) => group.unit === state.currency) : allGroups;
  const emptyToday = range.key === "today" && !state.byModel && state.metric !== "cost" && Array.isArray(state.timeline);
  const channelItems = filteredChannels();
  const channelOptions = [...(state.channels || [])];
  if (state.channelFilter && !channelOptions.some((channel) => String(channel.id) === state.channelFilter)) {
    channelOptions.push({ id: state.channelFilter, name: `${t("channels")} #${state.channelFilter}` });
  }
  container.innerHTML = `
    ${isAdmin() ? `<label class="report-scope">${t("dataScope")} <select class="select" id="usageReportScope"><option value="all" ${adminScope() ? "selected" : ""}>${t("globalScope")}</option><option value="mine" ${!adminScope() ? "selected" : ""}>${t("myScope")}</option></select></label>` : ""}
    <div class="section-head">
      <div><h2>${t("usage")}</h2><p>${t("usageHint")}</p></div>
      <div class="usage-period-controls">
        <div class="range-switch" id="rangeSwitch">${TIME_RANGES.map((r) => `<button data-range="${r.key}" class="${r.key === state.range ? "active" : ""}">${t(`usageRange_${r.key}`)}</button>`).join("")}</div>
        <label class="usage-date-picker"><span>${t("startDate")}</span><input class="input" id="usageStartDate" type="date" value="${state.startDate}" max="${state.endDate || today}"></label>
        <label class="usage-date-picker"><span>${t("endDate")}</span><input class="input" id="usageEndDate" type="date" value="${state.endDate}" min="${state.startDate}" max="${today}"></label>
      </div>
    </div>
    <div class="filter-bar usage-filters">
      <label class="filter-label"><span class="muted text-sm">${t("columns").models}</span><select class="select" id="usageModelFilter">
        <option value="">${t("allModels")}</option>${[...state.knownModels].sort().map((model) => `<option value="${escapeHtml(model)}" ${state.modelFilter === model ? "selected" : ""}>${escapeHtml(model)}</option>`).join("")}</select></label>
      <label class="filter-label"><span class="muted text-sm">${t("channels")}</span><select class="select" id="usageChannelFilter">
        <option value="">${t("allChannels")}</option>${channelOptions.map((channel) => `<option value="${escapeHtml(channel.id)}" ${String(state.channelFilter) === String(channel.id) ? "selected" : ""}>${escapeHtml(channel.name)} #${escapeHtml(channel.id)}</option>`).join("")}</select></label>
      ${filtering ? `<button class="btn-ghost" id="clearUsageFilter"><i data-lucide="x"></i>${t("clearFilter")}</button>` : ""}
      <span class="usage-scope">${escapeHtml(rangeLabel(range))} · ${escapeHtml(state.displayTimezone)}</span>
      ${Object.keys(state.errors).length ? `<button class="btn-secondary" id="retryUsage">${t("retry")}</button>` : ""}
    </div>
    ${state.errors.channelOptions ? `<p class="usage-unavailable mb-4" role="status">${t("usageChannelOptionsUnavailable")}</p>` : ""}
    ${renderKpis()}
    <div class="chart-card">
      <div class="card-head usage-trend-head"><div><div class="card-title">${t("usageTrend")}</div><div class="card-sub">${escapeHtml(rangeLabel(range))} · ${escapeHtml(state.displayTimezone)}</div></div>
        <div class="usage-trend-controls"><div class="range-switch" id="usageMetricSwitch">${["tokens", "requests", "cost"].map((metric) => `<button type="button" data-metric="${metric}" aria-pressed="${metric === state.metric}" class="${metric === state.metric ? "active" : ""}">${metricLabel(metric)}</button>`).join("")}</div>
        ${state.metric === "cost" && allGroups.length > 1 ? `<label class="usage-currency">${t("usageCurrency")}<select class="select" id="usageCurrency">${allGroups.map((group) => `<option value="${escapeHtml(group.unit)}" ${group.unit === state.currency ? "selected" : ""}>${escapeHtml(group.unit)}</option>`).join("")}</select></label>` : ""}
        <label class="usage-split"><input type="checkbox" id="usageByModel" ${state.byModel ? "checked" : ""}>${t("usageByModel")}</label></div></div>
      ${sectionMessage("timeline", state.timeline, emptyToday) || (groups.length ? groups.map((group, index) => `<div class="usage-trend-group">${state.metric === "cost" ? `<div class="card-sub mb-2">${escapeHtml(group.unit)}</div>` : ""}<div class="chart-canvas-wrap usage-main-chart">${chartAvailable ? `<canvas id="usageTrend${index}"></canvas>` : renderTrendFallback(group)}</div></div>`).join("") : `<div class="empty-state"><h3>—</h3><p>${t("usageCostUnknown")}</p></div>`)}
      ${range.key === "today" && !state.loading && !state.errors.timeline ? `<p class="usage-chart-note">${t("usageTodayHint")}</p>` : ""}
      ${state.metric === "cost" ? `<p class="usage-chart-note">${t("usageCostTrendHint")}</p>` : ""}
    </div>
    <div class="usage-grid">
      <div class="chart-card"><div class="card-head"><div class="card-title">${t("modelDist")}</div><div class="card-sub">${t("usageModelLimit")}</div></div>
        <div class="chart-canvas-wrap usage-breakdown-chart">${sectionMessage("modelsAgg", state.modelsAgg) || (chartAvailable ? '<canvas id="donutChart"></canvas>' : renderModelsFallback())}</div></div>
      <div class="chart-card"><div class="card-head"><div class="card-title">${t("channelSuccess")}</div><div class="card-sub">${t("usageSelectedScope")}</div></div>
        <div class="chart-canvas-wrap usage-breakdown-chart">${sectionMessage("channelStats", state.channelStats) || (chartAvailable ? '<canvas id="hbarChart"></canvas>' : renderChannelsFallback(channelItems))}</div></div>
    </div>
    <div class="chart-card"><div class="card-head"><div class="card-title">${t("modelTableTitle")}</div><div class="card-sub">${t("modelTableHint")} · ${t("usageModelLimit")}</div></div>
      ${sectionMessage("modelsAgg", state.modelsAgg) || renderModelTable()}
    </div>`;
  bindControls();
  refreshIcons(container);
  if (chartAvailable && !state.loading) drawCharts(groups, channelItems);
}

function renderModelTable() {
  return `<div class="table-wrap"><div class="table-scroll"><table><thead><tr>
    <th>${t("columns").models}</th><th>${t("metrics")[0]}</th><th>${t("metrics")[4]}</th><th>${t("metrics")[5]}</th><th>${t("cacheHitTokens")}</th><th>${t("cacheHitRate")}</th>
    </tr></thead><tbody>${state.modelsAgg.map((model) => {
      const cached = Number(model.cached_tokens || 0);
      const rate = Number(model.cache_hit_rate || 0);
      const rateBadge = cached > 0 ? badge(rate.toFixed(1) + "%", rate >= 50 ? "success" : rate >= 10 ? "info" : "") : '<span class="muted">—</span>';
      return `<tr><td><code>${escapeHtml(model.model)}</code></td><td class="mono">${formatRequestCount(model.request_count)}</td><td class="mono">${formatNumber(model.prompt_tokens)}</td><td class="mono">${formatNumber(model.completion_tokens)}</td><td class="mono">${formatNumber(cached)}</td><td>${rateBadge}</td></tr>`;
    }).join("")}</tbody></table></div></div>`;
}

function bindControls() {
  document.getElementById("rangeSwitch")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-range]");
    if (button) setRange(button.dataset.range);
  });
  document.getElementById("usageModelFilter")?.addEventListener("change", (event) => setFilter("modelFilter", event.target.value));
  document.getElementById("usageChannelFilter")?.addEventListener("change", (event) => setFilter("channelFilter", event.target.value));
  document.getElementById("usageStartDate")?.addEventListener("change", (event) => setCustomDate("startDate", event.target.value));
  document.getElementById("usageEndDate")?.addEventListener("change", (event) => setCustomDate("endDate", event.target.value));
  document.getElementById("clearUsageFilter")?.addEventListener("click", () => {
    state.modelFilter = state.channelFilter = "";
    load();
  });
  document.getElementById("retryUsage")?.addEventListener("click", load);
  document.getElementById("usageMetricSwitch")?.addEventListener("click", (event) => {
    const metric = event.target.closest("button[data-metric]")?.dataset.metric;
    if (!["tokens", "requests", "cost"].includes(metric)) return;
    state.metric = metric;
    render();
  });
  document.getElementById("usageCurrency")?.addEventListener("change", (event) => {
    state.currency = event.target.value;
    render();
  });
  document.getElementById("usageByModel")?.addEventListener("change", (event) => {
    state.byModel = event.target.checked;
    render();
  });
}

function setCustomDate(key, value) {
  const startDate = key === "startDate" ? value : state.startDate;
  const endDate = key === "endDate" ? value : state.endDate;
  if (!startDate || !endDate || startDate > endDate) { toast(t("invalidDateRange"), "error"); render(); return; }
  if (endDate > dateInTimezone(new Date(), state.displayTimezone)) { toast(t("futureEndDate"), "error"); render(); return; }
  state.startDate = startDate;
  state.endDate = endDate;
  state.range = "custom";
  load();
}

function metricLabel(metric) {
  return metric === "tokens" ? t("metrics")[3] : metric === "requests" ? t("metrics")[0] : t("usageCosted");
}

function trendGroups(rows, range) {
  const today = range.key === "today";
  const snapshot = today ? state.snapshot : new Date();
  const currentBucket = bucketKey(snapshot.toISOString(), range.bucket, state.displayTimezone);
  const buckets = expectedRangeBuckets(range, state.displayTimezone).filter((bucket) => today || bucket <= currentBucket);
  const labels = buckets.map((bucket) => bucketLabel(bucket, range));
  if (today) labels.push("24:00");
  const parts = today ? Object.fromEntries(new Intl.DateTimeFormat("en", { timeZone: state.displayTimezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(snapshot).map((part) => [part.type, part.value])) : null;
  const currentHour = parts ? Number(parts.hour) : null;
  const models = state.byModel ? [...new Set(rows.map((row) => row.model))].sort() : [null];
  const units = state.metric === "cost" ? [...new Set(rows.flatMap((row) => costEntries(row).map(([currency]) => currency)))].sort() : [state.metric];
  return units.map((unit) => {
    const datasets = models.map((model) => {
      const values = new Map();
      const unknownBuckets = new Set();
      for (const row of rows) {
        if (model !== null && row.model !== model) continue;
        const key = bucketKey(row.bucket, range.bucket, state.displayTimezone);
        if (state.metric === "cost" && Number(row.costed_requests || 0) < Number(row.requests || 0)) unknownBuckets.add(key);
        const value = state.metric === "cost" ? costEntries(row).find(([currency]) => currency === unit)?.[1] : Number(row[state.metric] || 0);
        if (value !== undefined) values.set(key, (values.get(key) || 0) + value);
      }
      return {
        label: model === null ? t("usageTotal") : model,
        data: [...buckets.map((bucket) => today && bucket > currentBucket ? null : values.has(bucket) ? values.get(bucket) : state.metric === "cost" && unknownBuckets.has(bucket) ? null : 0), ...(today ? [null] : [])],
      };
    }).filter((dataset) => state.metric !== "cost" || dataset.data.some((value) => value !== null));
    return { unit, labels, datasets, ...(today ? { currentHour, nowValue: currentHour + Number(parts.minute) / 60 } : {}) };
  });
}

function filteredChannels() {
  return (state.channelStats || []).filter((channel) => channel.total_requests > 0)
    .map((channel) => ({ name: channel.channel_name, rate: channel.success_requests / channel.total_requests * 100 }))
    .sort((a, b) => b.rate - a.rate).slice(0, 8);
}

function drawCharts(groups, channels) {
  if (!state.errors.timeline) groups.forEach((group, index) => {
    const canvas = document.getElementById(`usageTrend${index}`);
    if (!canvas) return;
    const key = `usage-trend-${index}`;
    const metric = state.metric;
    chartKeys.add(key);
    renderLine(key, canvas, group.labels, group.datasets, {
      fill: !state.byModel,
      yTitle: state.metric === "cost" ? group.unit : metricLabel(state.metric),
      ...(group.currentHour !== undefined ? { xAxis: { min: 0, max: 24, tickStep: 4 }, nowMarker: { value: group.nowValue, label: t("usageNow") } } : {}),
      tooltipCallbacks: {
        ...(group.currentHour !== undefined ? { title: (items) => {
          const index = items[0]?.dataIndex;
          if (index === undefined || index >= 24) return "";
          return `${group.labels[index]}–${group.labels[index + 1]}${index === group.currentHour ? ` · ${t("usageInProgress")}` : ""}`;
        } } : {}),
        label: (context) => ` ${context.dataset.label}: ${metric === "cost" ? `${group.unit} ${formatCost(context.parsed.y)}` : formatRequestCount(context.parsed.y)}`,
      },
    });
  });
  if (!state.errors.modelsAgg && state.modelsAgg?.length) {
    chartKeys.add("usage-donut");
    renderDonut("usage-donut", document.getElementById("donutChart"), state.modelsAgg.map((model) => model.model), state.modelsAgg.map((model) => model.request_count));
  }
  if (!state.errors.channelStats && channels.length) {
    chartKeys.add("usage-channels");
    renderHBar("usage-channels", document.getElementById("hbarChart"), channels.map((channel) => channel.name), channels.map((channel) => channel.rate), { label: "%" });
  }
}

function renderTrendFallback(group) {
  return `<div class="usage-chart-fallback"><p>${t("chartUnavailable")}</p>${group.datasets.map((dataset) => {
    const total = dataset.data.reduce((sum, value) => sum + (value ?? 0), 0);
    return `<div><span>${escapeHtml(dataset.label)}</span><strong>${state.metric === "cost" ? `${escapeHtml(group.unit)} ${formatCost(total)}` : formatNumber(total)}</strong></div>`;
  }).join("")}</div>`;
}

function renderModelsFallback() {
  const max = Math.max(...state.modelsAgg.map((model) => model.request_count), 1);
  return `<div class="usage-chart-fallback">${state.modelsAgg.map((model) => `<div><code>${escapeHtml(model.model)}</code><div class="progress"><span style="width:${model.request_count / max * 100}%"></span></div><span class="mono">${formatRequestCount(model.request_count)}</span></div>`).join("")}</div>`;
}

function renderChannelsFallback(channels) {
  return `<div class="usage-chart-fallback">${channels.map((channel) => `<div><code>${escapeHtml(channel.name)}</code><div class="progress ${channel.rate >= 95 ? "" : channel.rate >= 80 ? "warn" : "danger"}"><span style="width:${channel.rate}%"></span></div><span class="mono">${channel.rate.toFixed(1)}%</span></div>`).join("")}</div>`;
}

export async function onClick() { return false; }

document.addEventListener("change", (event) => {
  if (event.target.id !== "usageReportScope" || !isAdmin()) return;
  reportScope = event.target.value; state.channels = null; state.displayTimezone = "";
  state.modelFilter = state.channelFilter = ''; state.knownModels.clear();
  load();
});
document.addEventListener("sessionended", () => { unload(); state.stats = state.timeline = state.modelsAgg = state.channelStats = null; state.channels = null; state.knownModels.clear(); state.displayTimezone = ""; });
