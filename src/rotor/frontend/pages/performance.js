// Process-local performance snapshots and bounded, visibility-aware polling.
import { api } from "../api.js?v=2";
import { t } from "../i18n.js";
import { escapeHtml, refreshIcons } from "../ui.js";

let container;
let active = false;
let automatic = false;
let timer;
let controller;
let pending;
let generation = 0;
let performanceData = null;
let reconciliationData = null;
let errors = {};

const label = (key) => escapeHtml(t(key));
const number = (value) => value == null || !Number.isFinite(Number(value))
  ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
const duration = (value) => value == null ? "—" : `${number(value)} ms`;
const timestamp = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : escapeHtml(date.toLocaleString());
};
const states = {
  baseline: "perfBaseline", ok: "perfOk", mismatch: "perfMismatch",
  invalid: "perfInvalid", error: "perfError",
};

function stateLabel(data) {
  if (data?.status === "baseline" && !data.checked_at) return label("perfBaselinePending");
  return label(states[data?.status] || "perfNoData");
}

function metricRow(name, metric, unit = "ms") {
  const value = unit === "ms" ? duration : number;
  return `<tr><th scope="row">${label(name)}</th>
    <td>${value(metric?.p95)}</td><td>${number(metric?.sample_count)}</td>
    <td>${number(metric?.count)}</td></tr>`;
}

function metricTable(rows) {
  return `<div class="performance-table-wrap"><table class="performance-table">
    <thead><tr><th>${label("perfMetric")}</th><th>p95</th>
    <th>${label("perfSamples")}</th><th>${label("perfTotalCount")}</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function renderReconciliation(data) {
  const state = states[data?.status] ? data.status : "empty";
  const hint = state === "baseline" && !data.checked_at
    ? "perfBaselinePendingHint" : `${states[state] || "perfNoData"}Hint`;
  const hasDelta = ["ok", "mismatch"].includes(state);
  const cell = (value) => hasDelta ? number(value) : "—";
  return `<section class="performance-section">
    <div class="performance-section-head"><h3>${label("perfReconciliation")}</h3>
      <span class="performance-status ${state}">${stateLabel(data)}</span></div>
    <p class="performance-note">${label(hint)}</p>
    ${data?.reason ? `<p class="performance-note">${escapeHtml(data.reason)}</p>` : ""}
    <div class="performance-table-wrap"><table class="performance-table">
      <thead><tr><th>${label("perfMetric")}</th><th>${label("perfLedger")}</th>
      <th>${label("perfTokenCounters")}</th><th>${label("perfQuota")}</th></tr></thead>
      <tbody><tr><th scope="row">${label("perfRequests")}</th>
      <td>${cell(data?.requests?.ledger)}</td><td>${cell(data?.requests?.token)}</td><td>—</td></tr>
      <tr><th scope="row">${label("perfTokens")}</th><td>${cell(data?.total_tokens?.ledger)}</td>
      <td>${cell(data?.total_tokens?.token)}</td><td>${cell(data?.total_tokens?.quota)}</td></tr></tbody>
    </table></div>
    <div class="performance-reconciliation-footer">
      <div class="performance-note">${label("perfBaselineSince")}: ${timestamp(data?.since)}<br>
      ${label("perfCheckedAt")}: ${timestamp(data?.checked_at)} · ${label("perfCacheNote")}
      ${data?.cached ? ` · ${label("perfCached")}` : ""}<br>
      ${label("perfProcess")}: ${number(data?.pid)} · ${label("perfStartedAt")}: ${timestamp(data?.started_at)}</div>
      <button class="btn-secondary" type="button" data-performance-action="reset" ${pending ? "disabled" : ""}>
      ${label("perfResetBaseline")}</button>
    </div>
    <p class="performance-note">${label("perfResetHint")}</p>
  </section>`;
}

function render() {
  if (!active || !container) return;
  const data = performanceData;
  const metrics = data?.metrics || {};
  const store = data?.store;
  const requests = data?.requests || {};
  const total = data ? Object.values(requests).reduce((sum, row) => sum + (row.total || 0), 0) : null;
  const protocolRows = ["chat", "responses", "anthropic", "images", "other"].map((protocol) => {
    const row = requests[protocol];
    return `<tr><th scope="row">${protocol === "other" ? label("perfOther") : protocol}</th>
      <td>${number(row?.success)}</td><td>${number(row?.failed)}</td><td>${number(row?.cancelled)}</td>
      <td>${row?.total > 0 ? `${number(row.success / row.total * 100)}%` : "—"}</td>
      <td>${duration(row?.latency_ms?.p95)} <small>n=${number(row?.latency_ms?.sample_count)}</small></td>
      <td>${duration(row?.ttft_ms?.p95)} <small>n=${number(row?.ttft_ms?.sample_count)}</small></td></tr>`;
  }).join("");
  const state = states[reconciliationData?.status] ? reconciliationData.status : "empty";
  container.innerHTML = `
    <div class="section-head performance-heading"><div><h2>${label("perfTitle")}</h2>
      <p>${label("perfDescription")}</p></div>
      <div class="performance-actions"><label class="performance-auto"><input type="checkbox"
      data-performance-auto ${automatic ? "checked" : ""}>${label("perfAutoRefresh")}</label>
      <button class="btn-secondary" type="button" data-performance-action="refresh" ${pending ? "disabled" : ""}>
      <i data-lucide="refresh-cw"></i>${label(pending ? "perfRefreshing" : "refresh")}</button></div></div>
    ${Object.values(errors).map((error) => `<div class="performance-alert" role="alert">${escapeHtml(error)}</div>`).join("")}
    ${data?.pid != null && reconciliationData?.pid != null && data.pid !== reconciliationData.pid
      ? `<div class="performance-alert" role="status">${label("perfDifferentProcess")}</div>` : ""}
    <div class="performance-summary">
      <article><span>${label("perfRequests")}</span><strong>${number(total)}</strong><small>${label("perfSinceStart")}</small></article>
      <article><span>${label("perfAcquire")}</span><strong>${duration(metrics["db.acquire_ms"]?.p95)}</strong><small>p95</small></article>
      <article><span>${label("perfQueue")}</span><strong>${number(store?.queue_size)} <small>/ ${number(store?.queue_maxsize)}</small></strong><small>${label("perfPendingEvents")}</small></article>
      <article><span>${label("perfReconciliation")}</span><strong class="performance-state-text ${state}">${stateLabel(reconciliationData)}</strong><small>${label("perfIncremental")}</small></article>
    </div>
    <div class="performance-columns">
      <section class="performance-section"><h3>${label("perfDatabase")}</h3>
      ${metricTable([
        metricRow("perfAcquire", metrics["db.acquire_ms"]), metricRow("perfSql", metrics["db.sql_ms"]),
        metricRow("perfCommit", metrics["db.commit_ms"]), metricRow("perfHold", metrics["db.hold_ms"]),
      ].join(""))}<p class="performance-note">${label("perfDatabaseHint")}</p></section>
      <section class="performance-section"><div class="performance-section-head"><h3>${label("perfArchive")}</h3>
        <span class="performance-status ${store?.worker_running ? "ok" : "empty"}">
        ${label(!store ? "perfNoData" : !store.enabled ? "perfDisabled" : store.worker_running ? "perfRunning" : "perfStopped")}</span></div>
      ${metricTable([
        metricRow("perfBatchSize", metrics["store.batch_size"], "events"),
        metricRow("perfBatchTime", metrics["store.batch_ms"]),
        metricRow("perfQueueWait", metrics["store.queue_wait_ms"]),
        metricRow("perfDrain", metrics["store.drain_ms"]),
      ].join(""))}<p class="performance-note">${label("perfQueueDrops")}: ${number(store?.dropped_queue_full)} ·
      ${label("perfRetryDrops")}: ${number(store?.dropped_retry_exhausted)}</p></section>
    </div>
    <section class="performance-section"><h3>${label("perfProtocols")}</h3>
      <div class="performance-table-wrap"><table class="performance-table"><thead><tr>
        <th>${label("perfProtocol")}</th><th>${label("perfSuccess")}</th><th>${label("perfFailed")}</th>
        <th>${label("perfCancelled")}</th><th>${label("perfSuccessRate")}</th><th>${label("perfLatency")} p95</th><th>TTFT p95</th>
      </tr></thead><tbody>${protocolRows}</tbody></table></div>
      <p class="performance-note">${label("perfProtocolHint")}</p>
    </section>
    ${renderReconciliation(reconciliationData)}
    <footer class="performance-footnote">${label("perfSampleNote")} ${number(data?.window_seconds)} s / ${number(data?.sample_limit)}.
      ${label("perfCumulativeNote")}<br>${label("perfProcess")}: ${number(data?.pid)} ·
      ${label("perfStartedAt")}: ${timestamp(data?.started_at)} · ${label("monitoringUpdatedAt")}: ${timestamp(data?.generated_at)}</footer>`;
  refreshIcons(container);
}

function stopTimer() {
  clearTimeout(timer);
  timer = undefined;
}

function schedule() {
  stopTimer();
  if (active && automatic && !document.hidden && !pending) {
    timer = setTimeout(() => refresh(), 10_000);
  }
}

async function refresh(reset = false) {
  if (!active || document.hidden || pending) return pending;
  stopTimer();
  const sequence = generation;
  controller = new AbortController();
  const options = { signal: controller.signal };
  pending = Promise.allSettled([
    api("/api/admin/monitoring/performance", options),
    api(`/api/admin/monitoring/reconciliation${reset ? "/reset" : ""}`,
        { ...options, ...(reset ? { method: "POST" } : {}) }),
  ]);
  render();
  try {
    const [perf, reconciliation] = await pending;
    if (sequence !== generation || !active) return;
    errors = {};
    if (perf.status === "fulfilled") performanceData = perf.value;
    else {
      performanceData = null;
      errors.performance = `${t("perfLoadFailed")}: ${perf.reason?.message || perf.reason}`;
    }
    if (reconciliation.status === "fulfilled") reconciliationData = reconciliation.value;
    else {
      reconciliationData = { status: "error" };
      errors.reconciliation = `${t("perfReconciliationFailed")}: ${reconciliation.reason?.message || reconciliation.reason}`;
    }
  } finally {
    if (sequence === generation) {
      pending = null;
      controller = null;
      render();
      schedule();
    }
  }
}

function cancel() {
  stopTimer();
  generation += 1;
  controller?.abort();
  controller = null;
  pending = null;
}

export function unload() {
  active = false;
  cancel();
}

export function load(target) {
  if (container !== target) {
    cancel();
    container = target;
    container.addEventListener("click", (event) => {
      const action = event.target.closest("[data-performance-action]")?.dataset.performanceAction;
      if (action) refresh(action === "reset");
    });
    container.addEventListener("change", (event) => {
      if (event.target.matches("[data-performance-auto]")) {
        automatic = event.target.checked;
        schedule();
      }
    });
  }
  active = true;
  render();
  return refresh();
}

document.addEventListener("visibilitychange", () => {
  if (!active) return;
  if (document.hidden) cancel();
  else { render(); if (!performanceData) refresh(); else schedule(); }
});
window.addEventListener("rotorauthrequired", () => {
  automatic = false;
  unload();
});
