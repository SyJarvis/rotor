// Authenticated fetch wrapper with unified error unfolding.
let sessionEpoch = 0;
const sessionRequests = new Set();
document.addEventListener("sessionended", () => {
  sessionEpoch++;
  for (const controller of sessionRequests) controller.abort();
  sessionRequests.clear();
});

export function adminCsrfHeaders() {
  const prefix = "rotor_admin_csrf=";
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (!cookie) return {};
  return {
    "X-CSRF-Token": decodeURIComponent(cookie.slice(prefix.length)),
  };
}

export function handleAdminUnauthorized(response) {
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("rotorauthrequired"));
  }
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const method = String(options.method || "GET").toUpperCase();
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    Object.assign(headers, adminCsrfHeaders());
  }

  const epoch = sessionEpoch, controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  else options.signal?.addEventListener("abort", abort, { once: true });
  sessionRequests.add(controller);
  try {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    signal: controller.signal,
    headers,
  });
  handleAdminUnauthorized(response);
  if (epoch !== sessionEpoch) throw new DOMException("Session changed", "AbortError");

  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const body = await response.json();
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
        : body.detail;
      const error = new Error(detail || body.message || `${response.status} ${response.statusText}`);
      error.status = response.status;
      throw error;
    }
    const text = await response.text();
    const error = new Error(text || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) return null;
  const result = await response.json();
  if (epoch !== sessionEpoch) throw new DOMException("Session changed", "AbortError");
  return result;
  } finally {
    sessionRequests.delete(controller);
    options.signal?.removeEventListener("abort", abort);
  }
}

export async function copyText(value) {
  const text = String(value ?? "");
  try {
    if (
      typeof navigator !== "undefined"
      && navigator.clipboard
      && typeof window !== "undefined"
      && window.isSecureContext
    ) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path below. Clipboard API can fail when the
    // page is embedded, served over plain HTTP, or called after an async flow.
  }
  return fallbackCopyText(text);
}

function fallbackCopyText(value) {
  if (typeof document === "undefined") return false;
  if (!document.body) return false;

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  textarea.style.opacity = "0";

  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}
