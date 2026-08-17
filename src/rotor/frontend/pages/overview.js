// Overview page — KPI cards + endpoints + system status.

import { api } from "../api.js";
import { locale, t } from "../i18n.js";
import {
  escapeHtml, formatNumber, formatLatency, formatTimeInTimezone, badge, refreshIcons,
  skeletonKpis, copy, toast,
} from "../ui.js?v=12";
import { sparkline } from "../charts.js";

const HEALTH_WINDOW_MINUTES = 15;
const FAILURE_WINDOW_HOURS = 24;

let state = {
  stats: null,
  healthStats: null,
  channels: [],
  timeline: [],
  recentFailures: [],
  latestSuccess: null,
  displayTimezone: "Asia/Shanghai",
};

export async function load() {
  const container = document.getElementById("overview");
  skeletonKpis(container);
  try {
    const [stats, channels, timeline, healthStats, recentFailures, latestSuccess, settings] = await Promise.all([
      api("/api/admin/logs/stats?days=7").catch(() => null),
      api("/api/admin/channels").catch(() => []),
      api("/api/admin/logs/timeseries?days=7&bucket=day").catch(() => []),
      api(`/api/admin/logs/stats?${timeWindowQuery(HEALTH_WINDOW_MINUTES)}`).catch(() => null),
      api(`/api/admin/logs?success=false&limit=5&${timeWindowQuery(FAILURE_WINDOW_HOURS * 60)}`).catch(() => []),
      api("/api/admin/logs?success=true&limit=1").then((logs) => logs[0] || null).catch(() => null),
      api("/api/admin/settings").catch(() => null),
    ]);
    state = {
      stats,
      channels,
      timeline,
      healthStats,
      recentFailures,
      latestSuccess,
      displayTimezone: settings?.display_timezone || "Asia/Shanghai",
    };
    render();
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><i data-lucide="alert-circle"></i><h3>${escapeHtml(e.message)}</h3></div>`;
    refreshIcons(container);
  }
}

export function render() {
  const container = document.getElementById("overview");
  const s = state.stats || {};
  const successRate = s.total_requests ? ((s.success_requests / s.total_requests) * 100) : 0;
  const health = getHealthSummary();

  const timeline = state.timeline || [];
  const reqSpark = sparkline(timeline.map((d) => d.requests));
  const tokSpark = sparkline(timeline.map((d) => d.tokens));

  container.innerHTML = `
    <div class="section-head">
      <div>
        <h2>${t("gatewayTitle")}</h2>
        <p>${t("gatewayDescription")}</p>
      </div>
      ${badge(health.label, health.badgeType)}
    </div>

    <div class="overview-summary-label">${t("last7Days")}</div>
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="activity"></i>${t("metrics")[0]}</div>
        <div class="kpi-value">${formatNumber(s.total_requests)}</div>
        <div class="kpi-delta">${s.success_requests || 0} ${t("successLabel") || "✓"} · ${s.failed_requests || 0} ${t("failedLabel") || "✗"}</div>
        ${reqSpark}
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="coins"></i>${t("metrics")[3]}</div>
        <div class="kpi-value">${formatNumber(s.total_tokens)}</div>
        <div class="kpi-delta">${formatNumber(s.prompt_tokens || 0)} in · ${formatNumber(s.completion_tokens || 0)} out</div>
        ${tokSpark}
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="check-circle"></i>${t("successRate") || "Success rate"}</div>
        <div class="kpi-value">${successRate.toFixed(1)}%</div>
        <div class="kpi-delta">${s.success_requests || 0} / ${s.total_requests || 0}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label"><i data-lucide="gauge"></i>${t("metrics")[7]}</div>
        <div class="kpi-value">${formatLatency(s.avg_latency)}</div>
        <div class="kpi-delta">${t("last7Days")}</div>
      </div>
    </div>

    <div class="overview-insight-grid">
      ${renderHealthCard(health)}
      ${renderFailuresCard()}
    </div>

    <div class="section-head mt-6">
      <div><h2>${t("endpoints") || "API Endpoints"}</h2><p>${t("gatewayDescription")}</p></div>
    </div>
    <div class="endpoint-grid">
      ${renderEndpoints()}
    </div>
  `;
  refreshIcons(container);
}

function timeWindowQuery(minutes) {
  const end = new Date();
  const start = new Date(end.getTime() - minutes * 60_000);
  return new URLSearchParams({
    start_time: start.toISOString(),
    end_time: end.toISOString(),
  }).toString();
}

function getHealthSummary() {
  const stats = state.healthStats || {};
  const total = Number(stats.total_requests || 0);
  const failed = Number(stats.failed_requests || 0);
  const succeeded = Number(stats.success_requests || 0);

  if (!total) {
    return {
      label: t("idle"),
      badgeType: "",
      icon: "pause-circle",
      detail: t("noRecentTraffic"),
      total,
      failed,
      succeeded,
    };
  }
  if (!failed) {
    return {
      label: t("healthy"),
      badgeType: "success",
      icon: "check-circle-2",
      detail: t("allRecentRequestsSucceeded"),
      total,
      failed,
      succeeded,
    };
  }
  if (!succeeded) {
    return {
      label: t("down"),
      badgeType: "danger",
      icon: "circle-x",
      detail: t("allRecentRequestsFailed"),
      total,
      failed,
      succeeded,
    };
  }
  return {
    label: t("degraded"),
    badgeType: "warning",
    icon: "triangle-alert",
    detail: t("someRecentRequestsFailed"),
    total,
    failed,
    succeeded,
  };
}

function renderHealthCard(health) {
  const lastSuccess = state.latestSuccess?.created_at;
  const lastSuccessText = lastSuccess
    ? formatElapsedSince(lastSuccess)
    : t("noSuccessfulRequests");
  const lastSuccessTitle = lastSuccess
    ? formatTimeInTimezone(lastSuccess, state.displayTimezone)
    : "";
  return `
    <section class="overview-insight-card overview-health-card">
      <div class="card-head">
        <div>
          <div class="card-title">${t("systemStatus")}</div>
          <div class="card-sub">${t("systemStatusHint")}</div>
        </div>
        ${badge(health.label, health.badgeType)}
      </div>
      <div class="overview-health-main ${health.badgeType || "idle"}">
        <i data-lucide="${health.icon}"></i>
        <div><strong>${health.label}</strong><span>${health.detail}</span></div>
      </div>
      <div class="overview-health-metrics">
        <div><span>${t("recentRequests")}</span><strong>${formatNumber(health.total)}</strong></div>
        <div><span>${t("successLabel")}</span><strong>${formatNumber(health.succeeded)}</strong></div>
        <div><span>${t("failedLabel")}</span><strong>${formatNumber(health.failed)}</strong></div>
        <div><span>${t("lastSuccess")}</span><strong title="${escapeHtml(lastSuccessTitle)}">${escapeHtml(lastSuccessText)}</strong></div>
      </div>
    </section>`;
}

function formatElapsedSince(value) {
  const raw = String(value);
  const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? raw : `${raw}Z`);
  if (Number.isNaN(date.getTime())) return raw;
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60_000));
  if (minutes === 0) return locale === "zh-CN" ? "刚刚" : "just now";

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 1) return locale === "zh-CN" ? `${minutes}分钟` : `${minutes}m`;

  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  if (days) {
    return locale === "zh-CN"
      ? `${days}天${remainingHours}小时`
      : `${days}d ${remainingHours}h`;
  }
  return locale === "zh-CN"
    ? `${hours}小时${remainingMinutes}分钟`
    : `${hours}h ${remainingMinutes}m`;
}

function renderFailuresCard() {
  const failures = state.recentFailures || [];
  return `
    <section class="overview-insight-card overview-failures-card">
      <div class="card-head">
        <div>
          <div class="card-title">${t("recentFailures")}</div>
          <div class="card-sub">${t("recentFailuresHint")}</div>
        </div>
        <button class="btn-ghost btn-sm" type="button" data-overview-open-logs>${t("viewAllLogs")}</button>
      </div>
      ${failures.length ? `<div class="overview-failure-list">${failures.map(renderFailure).join("")}</div>` : `
        <div class="overview-no-failures"><i data-lucide="shield-check"></i><span>${t("noRecentFailures")}</span></div>`}
    </section>`;
}

function renderFailure(log) {
  const channel = state.channels.find((item) => item.id === log.channel_id);
  const source = channel?.name || (log.channel_id ? `#${log.channel_id}` : "—");
  const error = log.error_code || log.error_message || t("failed");
  return `
    <button class="overview-failure" type="button" data-overview-open-log="${log.id}">
      <i data-lucide="circle-alert"></i>
      <span class="overview-failure-body">
        <strong>${escapeHtml(log.model || "—")}</strong>
        <small>${escapeHtml(source)} · ${escapeHtml(error)}</small>
      </span>
      <time>${escapeHtml(formatTimeInTimezone(log.created_at, state.displayTimezone))}</time>
    </button>`;
}

function renderEndpoints() {
  const origin = window.location.origin;
  const items = [
    { method: "POST", name: "OpenAI Chat", path: `${origin}/v1/chat/completions`, icon: "message-square" },
    { method: "POST", name: "OpenAI Responses", path: `${origin}/v1/responses`, icon: "square-stack" },
    { method: "POST", name: "Anthropic Messages", path: `${origin}/anthropic/v1/messages`, icon: "bot" },
    { method: "GET",  name: "Models", path: `${origin}/v1/models`, icon: "boxes" },
  ];
  return items.map((e) => `
    <div class="endpoint-card">
      <div class="proto-row">
        <div class="row"><i data-lucide="${e.icon}" style="width:16px;height:16px;color:var(--accent)"></i><strong>${e.name}</strong></div>
        <span class="method-badge">${e.method}</span>
      </div>
      <div class="endpoint-path">${escapeHtml(e.path)}</div>
      <button class="btn-secondary" data-copy-value="${escapeHtml(e.path)}" style="align-self:flex-start">
        <i data-lucide="copy"></i><span>${t("copy")}</span>
      </button>
    </div>
  `).join("");
}

// expose copy handler via document-level delegation in app.js
export function onClick(target) {
  const logButton = target.closest("[data-overview-open-log]");
  if (logButton) {
    document.dispatchEvent(new CustomEvent("logsopen", { detail: { failedOnly: true } }));
    return true;
  }
  if (target.closest("[data-overview-open-logs]")) {
    document.dispatchEvent(new CustomEvent("logsopen", { detail: { failedOnly: true } }));
    return true;
  }
  const copyBtn = target.closest("[data-copy-value]");
  if (copyBtn) {
    copy(copyBtn.dataset.copyValue);
    return true;
  }
  return false;
}
