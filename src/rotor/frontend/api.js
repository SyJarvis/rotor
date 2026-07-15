// fetch wrapper with unified error unfolding (migrated from app.js).

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";

  const response = await fetch(path, { ...options, headers });

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
