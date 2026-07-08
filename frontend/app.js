const $ = (id) => document.getElementById(id);
let noticeTimer;
const uiConfig = window.ROTOR_UI_CONFIG;
let locale = localStorage.getItem("rotor.locale") || uiConfig.defaultLocale;

function t(key) {
  return key.split(".").reduce((value, part) => value?.[part], uiConfig.locales[locale]) ?? key;
}

function applyLocale() {
  document.documentElement.lang = locale;
  document.title = t("pageTitle");
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  renderGatewayEndpoints();
}

function renderGatewayEndpoints() {
  const origin = window.location.origin;
  $("gatewayBaseUrl").textContent = origin;
  const endpoints = [
    ["OpenAI Chat Completions", `${origin}/v1/chat/completions`],
    ["OpenAI Responses", `${origin}/v1/responses`],
    ["Anthropic Messages", `${origin}/anthropic/v1/messages`],
    ["Models", `${origin}/v1/models`],
  ];
  $("gatewayEndpoints").innerHTML = endpoints.map(([protocol, endpoint]) => `
    <div class="endpoint-row">
      <strong>${protocol}</strong>
      <code>${endpoint}</code>
      <button class="secondary copy-button" data-copy-value="${endpoint}">${t("copy")}</button>
    </div>
  `).join("");
}

function showNotice(message, isError = false) {
  const box = $("notice");
  clearTimeout(noticeTimer);
  box.textContent = message;
  box.className = `notice${isError ? " error" : ""}`;
  noticeTimer = setTimeout(() => box.classList.add("hidden"), 4000);
}

function headers(json = false) {
  const value = {};
  if (json) value["Content-Type"] = "application/json";
  return value;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...headers(Boolean(options.body)),
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
        : body.detail;
      throw new Error(detail || body.message || `${response.status} ${response.statusText}`);
    }
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }

  if (response.status === 204) return null;
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function parseCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseJson(value, fallback) {
  if (!value || !value.trim()) return fallback;
  return JSON.parse(value);
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

async function loadChannels() {
  const channels = await api("/api/admin/channels");
  const columns = t("columns");
  $("channelsTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>${columns.name}</th><th>${columns.provider}</th><th>${columns.baseUrl}</th><th>${columns.models}</th>
          <th>${columns.route}</th><th>${columns.status}</th><th>${columns.requests}</th><th>${columns.actions}</th>
        </tr>
      </thead>
      <tbody>
        ${channels.map((ch) => `
          <tr>
            <td><strong>${escapeHtml(ch.name)}</strong><br><span class="muted">#${ch.id}</span></td>
            <td>${escapeHtml(ch.type)} / ${escapeHtml(ch.protocol)}</td>
            <td><code>${escapeHtml(ch.base_url || "")}</code></td>
            <td><code>${escapeHtml((ch.models || []).join(", "))}</code></td>
            <td>${t("priority")} ${ch.priority}<br><span class="muted">${t("weight")} ${ch.weight}</span></td>
            <td>${ch.enabled ? t("enable") : t("disable")}</td>
            <td>${ch.success_requests || 0}/${ch.total_requests || 0}<br><span class="muted">${ch.failed_requests || 0} ${t("failed")}</span></td>
            <td>
              <button class="secondary" data-channel-test="${ch.id}">${t("test")}</button>
              <button class="secondary" data-channel-toggle="${ch.id}" data-enabled="${ch.enabled}">
                ${ch.enabled ? t("disable") : t("enable")}
              </button>
              <button class="danger" data-channel-delete="${ch.id}">${t("delete")}</button>
            </td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function openChannelModal() {
  $("channelModal").classList.remove("hidden");
}

function closeChannelModal() {
  $("channelModal").classList.add("hidden");
  $("probeResult").classList.add("hidden");
}

async function createChannel(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const submitButton = formElement.querySelector('button[type="submit"]');
  if (submitButton.disabled) return;

  submitButton.disabled = true;
  submitButton.textContent = t("saving");
  try {
    const form = new FormData(formElement);
    const payload = {
      name: form.get("name"),
      type: form.get("type"),
      key: form.get("key"),
      base_url: form.get("base_url"),
      models: parseCsv(form.get("models")),
      model_mapping: parseJson(form.get("model_mapping"), {}),
      priority: Number(form.get("priority") || 1),
      weight: Number(form.get("weight") || 1),
      enabled: Boolean(form.get("enabled")),
      test_only: false,
      protocol: form.get("protocol"),
      extra: {},
    };
    await api("/api/admin/channels", { method: "POST", body: JSON.stringify(payload) });
    formElement.reset();
    closeChannelModal();
    showNotice(`${t("channels")} "${payload.name}" ${t("created")}`);
    try {
      await loadChannels();
    } catch (error) {
      showNotice(`Channel created, but the list could not be refreshed: ${error.message}`, true);
    }
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = t("saveChannel");
  }
}

async function probeModels() {
  const form = new FormData($("channelForm"));
  const payload = {
    base_url: form.get("base_url"),
    key: form.get("key"),
    type: form.get("type"),
    protocol: form.get("protocol"),
  };
  if (!payload.base_url || !payload.key) {
    showNotice(t("requireProbeFields"), true);
    return;
  }
  const result = await api("/api/admin/channels/probe-models", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("channelForm").elements.models.value = result.models.join(", ");
  $("probeResult").textContent = `${result.models.length} ${t("foundModels")} ${result.latency_ms} ms`;
  $("probeResult").className = "inline-result";
}

async function loadTokens() {
  const tokens = await api("/api/admin/tokens");
  const columns = t("columns");
  $("tokensTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>${columns.id}</th><th>${columns.name}</th><th>${columns.key}</th><th>${columns.user}</th>
          <th>${columns.quota}</th><th>${columns.used}</th><th>${columns.status}</th><th>${columns.actions}</th>
        </tr>
      </thead>
      <tbody>
        ${tokens.map((token) => `
          <tr>
            <td>${token.id}</td>
            <td>${escapeHtml(token.name)}</td>
            <td><div class="key-cell"><code>${escapeHtml(token.key)}</code><button class="secondary copy-button" data-copy-value="${escapeHtml(token.key)}">${t("copy")}</button></div></td>
            <td>${escapeHtml(token.user_id || "")}</td>
            <td>${token.quota ?? t("unlimited")}</td>
            <td>${token.used_quota}</td>
            <td>${token.enabled ? t("enable") : t("disable")}</td>
            <td>
              <button class="secondary" data-token-toggle="${token.id}" data-enabled="${token.enabled}">
                ${token.enabled ? t("disable") : t("enable")}
              </button>
              <button class="secondary" data-token-reset="${token.id}">${t("resetUsage")}</button>
              <button class="danger" data-token-delete="${token.id}">${t("delete")}</button>
            </td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

async function generateToken(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const params = new URLSearchParams();
  params.set("name", form.get("name"));
  if (form.get("user_id")) params.set("user_id", form.get("user_id"));
  if (form.get("quota")) params.set("quota", form.get("quota"));
  const allowed = parseCsv(form.get("allowed_channels"));
  for (const id of allowed) params.append("allowed_channels", id);

  const token = await api(`/api/admin/tokens/generate?${params}`, { method: "POST" });
  const copied = await copyText(token.key);
  showNotice(copied ? t("generated") : `${t("created")}: ${token.key}`);
  event.currentTarget.reset();
  await loadTokens();
}

async function loadUsage() {
  const stats = await api("/api/admin/logs/stats");
  const labels = t("metrics");
  const rows = [
    [labels[0], stats.total_requests], [labels[1], stats.success_requests],
    [labels[2], stats.failed_requests], [labels[3], stats.total_tokens],
    [labels[4], stats.prompt_tokens], [labels[5], stats.completion_tokens],
    [labels[6], stats.total_cost], [labels[7], `${Number(stats.avg_latency || 0).toFixed(3)}s`],
  ];
  $("usageCards").innerHTML = rows.map(([label, value]) => `
    <div class="card"><span>${label}</span><strong>${value}</strong></div>
  `).join("");
}

async function loadLogs() {
  const logs = await api("/api/admin/logs?limit=100");
  const columns = t("columns");
  $("logsTable").innerHTML = `
    <table>
      <thead>
        <tr>
          <th>${columns.time}</th><th>${columns.models}</th><th>${columns.token}</th><th>${columns.channel}</th>
          <th>${columns.tokens}</th><th>${columns.status}</th><th>${columns.error}</th><th>${columns.latency}</th>
        </tr>
      </thead>
      <tbody>
        ${logs.map((log) => `
          <tr>
            <td>${escapeHtml(log.created_at)}</td>
            <td>${escapeHtml(log.model)}</td>
            <td>${log.token_id ?? ""}</td>
            <td>${log.channel_id ?? ""}</td>
            <td>${log.total_tokens}</td>
            <td>${log.success ? t("reachable") : t("failed")}</td>
            <td>${escapeHtml(log.error_message || "")}</td>
            <td>${log.latency ?? ""}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

async function refreshActive() {
  const active = document.querySelector(".panel.active")?.id;
  if (active === "overview") return;
  if (active === "channels") return loadChannels();
  if (active === "tokens") return loadTokens();
  if (active === "usage") return loadUsage();
  if (active === "logs") return loadLogs();
}

document.addEventListener("click", async (event) => {
  const copyButton = event.target.closest("[data-copy-value], [data-copy-target]");
  if (copyButton) {
    const value = copyButton.dataset.copyValue
      || $(copyButton.dataset.copyTarget)?.textContent;
    if (await copyText(value)) {
      showNotice(t("copied"));
    } else {
      showNotice(value);
    }
    return;
  }

  const tab = event.target.closest(".tab");
  if (tab) {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    $(tab.dataset.tab).classList.add("active");
    try {
      await refreshActive();
    } catch (error) {
      showNotice(error.message, true);
    }
    return;
  }

  const channelDelete = event.target.dataset.channelDelete;
  if (channelDelete && confirm(`${t("confirmDeleteChannel")} ${channelDelete}?`)) {
    await api(`/api/admin/channels/${channelDelete}`, { method: "DELETE" });
    await loadChannels();
  }

  const channelToggle = event.target.dataset.channelToggle;
  if (channelToggle) {
    const enabled = event.target.dataset.enabled === "true";
    await api(`/api/admin/channels/${channelToggle}/${enabled ? "disable" : "enable"}`, { method: "POST" });
    await loadChannels();
  }

  const channelTest = event.target.dataset.channelTest;
  if (channelTest) {
    const result = await api(`/api/admin/channels/${channelTest}/test`, { method: "POST" });
    const text = result.ok
      ? `${t("channels")} ${channelTest} ${t("reachable")}，${result.latency_ms} ms，${result.models.length} models`
      : `${t("channels")} ${channelTest} ${t("failed")}，${result.latency_ms} ms: ${result.error || result.status_code}`;
    $("channelTestResult").textContent = text;
    $("channelTestResult").className = `inline-result${result.ok ? "" : " error"}`;
  }

  const tokenDelete = event.target.dataset.tokenDelete;
  if (tokenDelete && confirm(`${t("confirmDeleteKey")} ${tokenDelete}?`)) {
    await api(`/api/admin/tokens/${tokenDelete}`, { method: "DELETE" });
    await loadTokens();
  }

  const tokenToggle = event.target.dataset.tokenToggle;
  if (tokenToggle) {
    const enabled = event.target.dataset.enabled === "true";
    await api(`/api/admin/tokens/${tokenToggle}/${enabled ? "disable" : "enable"}`, { method: "POST" });
    await loadTokens();
  }

  const tokenReset = event.target.dataset.tokenReset;
  if (tokenReset) {
    await api(`/api/admin/tokens/${tokenReset}/reset-quota`, { method: "POST" });
    await loadTokens();
  }
});

$("channelForm").addEventListener("submit", (event) => {
  createChannel(event).catch((error) => showNotice(error.message, true));
});

$("openChannelModal").addEventListener("click", openChannelModal);
$("closeChannelModal").addEventListener("click", closeChannelModal);
$("probeModels").addEventListener("click", () => probeModels().catch((error) => showNotice(error.message, true)));

$("tokenForm").addEventListener("submit", (event) => {
  generateToken(event).catch((error) => showNotice(error.message, true));
});

$("refreshChannels").addEventListener("click", () => loadChannels().catch((error) => showNotice(error.message, true)));
$("refreshTokens").addEventListener("click", () => loadTokens().catch((error) => showNotice(error.message, true)));
$("refreshUsage").addEventListener("click", () => loadUsage().catch((error) => showNotice(error.message, true)));
$("refreshLogs").addEventListener("click", () => loadLogs().catch((error) => showNotice(error.message, true)));
$("toggleLocale").addEventListener("click", async () => {
  locale = locale === "zh-CN" ? "en" : "zh-CN";
  localStorage.setItem("rotor.locale", locale);
  applyLocale();
  await refreshActive();
});

applyLocale();
refreshActive().catch((error) => showNotice(error.message, true));
