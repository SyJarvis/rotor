// UI primitives — toast, skeleton, badges, progress, formatters, escape.

import { t } from "./i18n.js";
import { copyText } from "./api.js";

/* ---------- escape & parse ---------- */
export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function parseCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function parseJson(value, fallback) {
  if (!value || !value.trim()) return fallback;
  return JSON.parse(value);
}

/* ---------- format helpers ---------- */
export function formatNumber(n) {
  const num = Number(n || 0);
  if (Number.isNaN(num)) return "0";
  if (Math.abs(num) >= 1e9) return (num / 1e9).toFixed(2) + "B";
  if (Math.abs(num) >= 1e6) return (num / 1e6).toFixed(2) + "M";
  if (Math.abs(num) >= 1e3) return (num / 1e3).toFixed(1) + "K";
  return String(num);
}

export function formatLatency(seconds) {
  const s = Number(seconds || 0);
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  return `${s.toFixed(2)} s`;
}

export function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

export function relativeTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/* ---------- toast ---------- */
const toastStack = () => document.getElementById("toastStack");

const TOAST_ICONS = {
  success: "check-circle-2",
  error: "alert-circle",
  warning: "alert-triangle",
  info: "info",
};

export function toast(message, type = "info", duration = 4000) {
  const stack = toastStack();
  if (!stack) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<i data-lucide="${TOAST_ICONS[type] || TOAST_ICONS.info}"></i><div>${escapeHtml(message)}</div>`;
  stack.appendChild(el);
  if (window.lucide) window.lucide.createIcons({ root: el });
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  }, duration);
}

/* ---------- inline result ---------- */
export function showInline(el, message, type = "info") {
  if (!el) return;
  el.textContent = message;
  el.className = `inline-result ${type}`;
}

export function hideInline(el) {
  if (el) el.classList.add("hidden");
}

/* ---------- badges ---------- */
export function badge(text, type = "") {
  return `<span class="badge ${type}">${escapeHtml(text)}</span>`;
}

export function statusBadge(enabled) {
  return enabled
    ? badge(t("enable"), "success")
    : badge(t("disable"), "danger");
}

/* ---------- progress ---------- */
export function progressBar(percent, cls = "") {
  const pct = Math.max(0, Math.min(100, Number(percent || 0)));
  return `<div class="progress ${cls}"><span style="width:${pct}%"></span></div>`;
}

export function progressRing(percent, size = 44) {
  const pct = Math.max(0, Math.min(100, Number(percent || 0)));
  const stroke = 4;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - pct / 100);
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="transform: rotate(-90deg)">
      <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="var(--panel-2)" stroke-width="${stroke}"/>
      <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="url(#ringGrad)" stroke-width="${stroke}"
              stroke-dasharray="${c}" stroke-dashoffset="${offset}" stroke-linecap="round"
              style="transition: stroke-dashoffset 600ms ease-out"/>
      <defs>
        <linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="var(--accent)"/>
          <stop offset="100%" stop-color="var(--accent-2)"/>
        </linearGradient>
      </defs>
    </svg>`;
}

/* ---------- skeletons ---------- */
export function skeletonRows(container, count = 4, cols = 6) {
  container.innerHTML = `
    <div class="table-wrap"><div class="table-scroll"><table><tbody>
      ${Array.from({ length: count }).map(() => `
        <tr>${Array.from({ length: cols }).map(() => `<td><div class="skeleton line"></div></td>`).join("")}</tr>
      `).join("")}
    </tbody></table></div></div>`;
}

export function skeletonCards(container, count = 4) {
  container.innerHTML = `
    <div class="channel-grid">
      ${Array.from({ length: count }).map(() => `<div class="skeleton block"></div>`).join("")}
    </div>`;
}

export function skeletonKpis(container) {
  container.innerHTML = `
    <div class="skeleton-grid">
      ${Array.from({ length: 4 }).map(() => `<div class="skeleton block"></div>`).join("")}
    </div>`;
}

/* ---------- misc ---------- */
export function refreshIcons(root) {
  if (window.lucide) window.lucide.createIcons({ root: root || document });
}

export async function copy(value) {
  const ok = await copyText(value);
  toast(ok ? t("copied") : value, ok ? "success" : "warning");
}
