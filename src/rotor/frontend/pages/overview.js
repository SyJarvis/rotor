// Overview page — shortcuts and connection addresses.

import { t } from "../i18n.js";
import { escapeHtml, refreshIcons, copy } from "../ui.js?v=12";

const protocols = {
  openai: {
    label: "OpenAI", basePath: "/v1",
    endpoints: [
      { method: "POST", name: "OpenAI Chat", path: "/v1/chat/completions" },
      { method: "POST", name: "OpenAI Responses", path: "/v1/responses" },
      { method: "POST", name: "OpenAI Embeddings", path: "/v1/embeddings" },
      { method: "GET", name: "Models", path: "/v1/models" },
    ],
  },
  anthropic: {
    label: "Anthropic", basePath: "/anthropic",
    endpoints: [{ method: "POST", name: "Anthropic Messages", path: "/anthropic/v1/messages" }],
  },
};
let protocol = "openai";

export function load() { render(); }

export function render() {
  const container = document.getElementById("overview");
  const expanded = container.querySelector("details")?.open || false;
  const selected = protocols[protocol];
  const origin = window.location.origin;
  const baseUrl = `${origin}${selected.basePath}`;
  container.innerHTML = `
    <div class="overview-layout">
      <section aria-labelledby="overviewShortcutsTitle">
        <h2 class="overview-section-title" id="overviewShortcutsTitle">${t("quickLinks")}</h2>
        <div class="overview-shortcuts">
          <button class="overview-shortcut" type="button" data-overview-tab="tokens"><i data-lucide="key-round" aria-hidden="true"></i><span>${t("apiKeys")}</span><i data-lucide="chevron-right" aria-hidden="true"></i></button>
          <button class="overview-shortcut" type="button" data-overview-tab="usage"><i data-lucide="chart-no-axes-combined" aria-hidden="true"></i><span>${t("usage")}</span><i data-lucide="chevron-right" aria-hidden="true"></i></button>
        </div>
      </section>
      <section aria-labelledby="overviewConfigTitle">
        <h2 class="overview-section-title" id="overviewConfigTitle">${t("accessConfiguration")}</h2>
        <div class="overview-config-card">
          <div class="overview-protocols" role="group" aria-label="${t("accessProtocol")}">
            ${Object.entries(protocols).map(([id, item]) => `<button type="button" data-overview-protocol="${id}" class="${id === protocol ? "active" : ""}" aria-pressed="${id === protocol}" aria-controls="overviewConfiguration">${item.label}</button>`).join("")}
          </div>
          <div id="overviewConfiguration">
            <div class="overview-base-label">Base URL</div>
            <div class="overview-base-row"><code>${escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${escapeHtml(baseUrl)}"><i data-lucide="copy" aria-hidden="true"></i>${t("copy")}</button></div>
            <details class="overview-endpoints"${expanded ? " open" : ""}>
              <summary>${t("endpoints")}</summary>
              <div class="overview-endpoint-list">${selected.endpoints.map((item) => {
                const url = `${origin}${item.path}`;
                return `<div class="overview-endpoint"><div class="overview-endpoint-heading"><strong>${item.name}</strong><span class="overview-endpoint-method">${item.method}</span></div><div class="overview-endpoint-address"><code>${escapeHtml(url)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${escapeHtml(url)}" aria-label="${t("copy")} ${item.name}"><i data-lucide="copy" aria-hidden="true"></i>${t("copy")}</button></div></div>`;
              }).join("")}</div>
            </details>
          </div>
        </div>
      </section>
    </div>`;
  refreshIcons(container);
}

export function onClick(target) {
  const shortcut = target.closest("[data-overview-tab]");
  if (shortcut && ["tokens", "usage"].includes(shortcut.dataset.overviewTab)) {
    document.dispatchEvent(new CustomEvent("overviewnavigate", { detail: { tab: shortcut.dataset.overviewTab } }));
    return true;
  }
  const button = target.closest("[data-overview-protocol]");
  if (button && Object.hasOwn(protocols, button.dataset.overviewProtocol)) {
    if (protocol !== button.dataset.overviewProtocol) {
      protocol = button.dataset.overviewProtocol;
      render();
      document.getElementById("overview").querySelector(`[data-overview-protocol="${protocol}"]`)?.focus();
    }
    return true;
  }
  const copyButton = target.closest("[data-copy-value]");
  if (copyButton) { copy(copyButton.dataset.copyValue); return true; }
  return false;
}

export function unload() {
  protocol = "openai";
  const container = document.getElementById("overview");
  if (container) container.innerHTML = "";
}
document.addEventListener("sessionended", unload);
