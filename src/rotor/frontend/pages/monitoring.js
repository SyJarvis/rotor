// Monitoring Sources page — today's Coding Agent overview.

import { api } from "../api.js?v=2";
import { t } from "../i18n.js";
import {
  escapeHtml, formatRequestCount, formatTimeInTimezone, refreshIcons,
} from "../ui.js";

import * as performance from "./performance.js?v=2";

let loadSequence = 0;
let selected = "performance";

function safeText(value) {
  return escapeHtml(value);
}

function safeLabel(key) {
  return safeText(t(key));
}

function formatCount(value) {
  return formatRequestCount(value);
}

function formatTimestamp(value, timezone) {
  try {
    return formatTimeInTimezone(value, timezone);
  } catch (_error) {
    return "—";
  }
}

function renderLoading(container) {
  container.innerHTML = `
    <div class="monitoring-loading" aria-label="${safeLabel("monitoringSources")}">
      ${Array.from({ length: 3 }).map(() => `
        <div class="monitoring-loading-card">
          <div class="skeleton line short"></div>
          <div class="monitoring-loading-stats">
            ${Array.from({ length: 4 }).map(() => '<div class="skeleton block"></div>').join("")}
          </div>
        </div>
      `).join("")}
    </div>`;
}

function renderContext(data) {
  const timezone = String(data?.timezone || "Asia/Shanghai");
  return `
    <div class="monitoring-context" aria-label="${safeLabel("monitoringSnapshot")}">
      <div class="monitoring-context-item">
        <i data-lucide="calendar-days"></i>
        <span>${safeLabel("monitoringDate")}</span>
        <strong>${safeText(data?.date || "—")}</strong>
      </div>
      <div class="monitoring-context-item">
        <i data-lucide="globe-2"></i>
        <span>${safeLabel("monitoringTimezone")}</span>
        <strong>${safeText(timezone)}</strong>
      </div>
      <div class="monitoring-context-item">
        <i data-lucide="clock-3"></i>
        <span>${safeLabel("monitoringUpdatedAt")}</span>
        <strong>${safeText(formatTimestamp(data?.generated_at, timezone))}</strong>
      </div>
    </div>`;
}

function renderAgentStat(label, value) {
  return `
    <span class="monitoring-agent-stat">
      <span>${safeLabel(label)}</span>
      <strong>${formatCount(value)}</strong>
    </span>`;
}

function renderAgentStats(agent) {
  return [
    renderAgentStat("todayRequests", agent?.request_count),
    renderAgentStat("ungroupedRequests", agent?.ungrouped_request_count),
    renderAgentStat("todaySessions", agent?.session_count),
    renderAgentStat("activeSessions", agent?.active_session_count),
    renderAgentStat("todayTokens", agent?.total_tokens),
    renderAgentStat("requestTurns", agent?.turn_count),
  ].join("");
}

function renderAgentCard(agent) {
  const agentId = String(agent?.id || "");
  const agentName = String(agent?.name || agentId || "—");
  const requestCount = Number(agent?.request_count || 0);
  const statusKey = requestCount > 0 ? "requestsSeen" : "noRequestsSeen";
  return `
    <article class="monitoring-source-card">
      <div class="monitoring-source-head">
        <div class="monitoring-source-identity">
          <span class="monitoring-source-icon" aria-hidden="true"><i data-lucide="bot"></i></span>
          <span class="monitoring-source-name">
            <strong>${safeText(agentName)}</strong>
            <span>${safeText(agentId || "—")}</span>
          </span>
        </div>
      </div>
      <div class="monitoring-agent-stats">
        ${renderAgentStats(agent)}
      </div>
      <div class="monitoring-card-footer">
        <span class="monitoring-status ${requestCount > 0 ? "active" : "inactive"}">
          <span class="status-dot ${requestCount > 0 ? "on" : ""}"></span>
          ${safeLabel(statusKey)}
        </span>
      </div>
    </article>`;
}

function renderOverview(data, agents) {
  return `
    <div class="section-head">
      <div>
        <h2>${safeLabel("monitoringSources")}</h2>
        <p>${safeLabel("monitoringSourcesDescription")}</p>
      </div>
    </div>
    ${renderContext(data)}
    ${agents.length === 0
      ? `<div class="empty-state monitoring-empty-state">
          <i data-lucide="radio-tower"></i>
          <h3>${safeLabel("noMonitoringSources")}</h3>
          <p>${safeLabel("noMonitoringSourcesHint")}</p>
        </div>`
      : `<div class="monitoring-source-list" aria-label="${safeLabel("monitoringSources")}">
          ${agents.map((agent) => renderAgentCard(agent)).join("")}
        </div>`}
  `;
}

function render(data) {
  const container = document.getElementById("monitoringContent");
  if (!container) return;
  const agents = Array.isArray(data?.agents) ? data.agents : [];
  container.innerHTML = renderOverview(data, agents);
  refreshIcons(container);
}

function renderError(container, error) {
  container.innerHTML = `
    <div class="empty-state monitoring-empty-state">
      <i data-lucide="alert-circle"></i>
      <h3>${safeLabel("monitoringLoadFailed")}</h3>
      <p>${safeText(error?.message || error)}</p>
      <button class="btn-secondary monitoring-retry" id="retryMonitoring" type="button">
        <i data-lucide="refresh-cw"></i>${safeLabel("retry")}
      </button>
    </div>`;
  document.getElementById("retryMonitoring")?.addEventListener("click", () => load());
  refreshIcons(container);
}

async function loadSources() {
  const container = document.getElementById("monitoringContent");
  if (!container) return;
  const sequence = ++loadSequence;
  renderLoading(container);
  try {
    const data = await api("/api/admin/monitoring/sources");
    if (sequence !== loadSequence) return;
    render(data);
  } catch (error) {
    if (sequence !== loadSequence) return;
    renderError(container, error);
  }
}


function layout() {
  const root = document.getElementById("monitoring");
  if (!root) return null;
  if (!document.getElementById("monitoringContent")) {
    root.innerHTML = `<div class="monitoring-tabs" role="tablist" aria-label="${safeLabel("monitoringTitle")}">
      <button type="button" role="tab" id="performanceTab" aria-controls="monitoringContent" data-monitoring-tab="performance"></button>
      <button type="button" role="tab" id="sourcesTab" aria-controls="monitoringContent" data-monitoring-tab="sources"></button>
    </div><div id="monitoringContent" role="tabpanel"></div>`;
    root.querySelectorAll("[data-monitoring-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        if (selected === button.dataset.monitoringTab) return;
        unload();
        selected = button.dataset.monitoringTab;
        load();
      });
    });
  }
  root.querySelectorAll("[data-monitoring-tab]").forEach((button) => {
    const isPerformance = button.dataset.monitoringTab === "performance";
    button.textContent = t(isPerformance ? "perfTitle" : "monitoringSessionSources");
    button.setAttribute("aria-selected", String(button.dataset.monitoringTab === selected));
  });
  const content = document.getElementById("monitoringContent");
  content.setAttribute("aria-labelledby", selected === "performance" ? "performanceTab" : "sourcesTab");
  return content;
}

export function unload() {
  loadSequence += 1;
  performance.unload();
}

export function load() {
  const content = layout();
  if (!content) return;
  if (selected === "performance") return performance.load(content);
  performance.unload();
  return loadSources();
}
