// Overview page — KPI cards + endpoints + system status.

import { api } from "../api.js";
import { t } from "../i18n.js";
import {
  escapeHtml, formatNumber, formatLatency, badge, refreshIcons,
  skeletonKpis, copy, toast,
} from "../ui.js";
import { sparkline } from "../charts.js";

let state = { stats: null, channels: [], timeline: [] };

export async function load() {
  const container = document.getElementById("overview");
  skeletonKpis(container);
  try {
    const [stats, channels, timeline] = await Promise.all([
      api("/api/admin/logs/stats?days=7").catch(() => null),
      api("/api/admin/channels").catch(() => []),
      api("/api/admin/logs/timeseries?days=7&bucket=day").catch(() => []),
    ]);
    state = { stats, channels, timeline };
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
  const enabledCh = state.channels.filter((c) => c.enabled).length;
  const totalCh = state.channels.length;
  const healthRate = totalCh ? (enabledCh / totalCh) * 100 : 100;

  const healthBadge = healthRate >= 80
    ? badge(t("healthy"), "success")
    : healthRate > 0 ? badge(t("degraded"), "warning") : badge(t("down"), "danger");

  const timeline = state.timeline || [];
  const reqSpark = sparkline(timeline.map((d) => d.requests));
  const tokSpark = sparkline(timeline.map((d) => d.tokens));

  container.innerHTML = `
    <div class="section-head">
      <div>
        <h2>${t("gatewayTitle")}</h2>
        <p>${t("gatewayDescription")}</p>
      </div>
      ${healthBadge}
    </div>

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
        <div class="kpi-delta">${enabledCh}/${totalCh} ${t("channelsEnabled") || "channels on"}</div>
      </div>
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
  const copyBtn = target.closest("[data-copy-value]");
  if (copyBtn) {
    copy(copyBtn.dataset.copyValue);
    return true;
  }
  return false;
}
