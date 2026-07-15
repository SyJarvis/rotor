// Chart.js wrapper with graceful CDN-failure degradation.

function available() {
  return typeof window !== "undefined" && typeof window.Chart === "function";
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function themeColors() {
  return {
    text: cssVar("--text") || "#0f172a",
    muted: cssVar("--muted") || "#64748b",
    line: cssVar("--line") || "#e2e8f0",
    panel: cssVar("--panel") || "#fff",
    accent: cssVar("--accent") || "#6366f1",
    accent2: cssVar("--accent-2") || "#06b6d4",
    success: cssVar("--success") || "#10b981",
    danger: cssVar("--danger") || "#ef4444",
    warning: cssVar("--warning") || "#f59e0b",
    info: cssVar("--info") || "#3b82f6",
  };
}

const registry = new Map();

export function destroyChart(key) {
  const c = registry.get(key);
  if (c) { c.destroy(); registry.delete(key); }
}

export function destroyAll() {
  for (const c of registry.values()) c.destroy();
  registry.clear();
}

// Rebuild all charts on theme change (colors must follow CSS vars).
export function refreshTheme() {
  destroyAll();
  for (const factory of snapshotStore.values()) factory();
}

const snapshotStore = new Map();
function register(key, factory) {
  snapshotStore.set(key, factory);
  const c = factory();
  if (c) registry.set(key, c);
}

export function isAvailable() { return available(); }

/* ---------- usage timeline (bar + line, dual axis) ---------- */
export function renderTimeline(key, canvas, data, opts = {}) {
  if (!available()) return null;
  const factory = () => {
    const ctx = canvas.getContext("2d");
    const colors = themeColors();
    const labels = data.map((d) => formatBucketLabel(d.bucket, opts.bucket));
    return new window.Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            type: "bar",
            label: opts.tRequests || "Requests",
            data: data.map((d) => d.requests),
            backgroundColor: hexA(colors.accent, 0.55),
            borderColor: colors.accent,
            borderWidth: 0,
            borderRadius: 4,
            yAxisID: "y",
            order: 2,
          },
          {
            type: "line",
            label: opts.tTokens || "Tokens",
            data: data.map((d) => d.tokens),
            borderColor: colors.accent2,
            backgroundColor: hexA(colors.accent2, 0.15),
            tension: 0.35,
            fill: true,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2,
            yAxisID: "y1",
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: colors.text, usePointStyle: true, boxWidth: 8 } },
          tooltip: { backgroundColor: colors.panel, titleColor: colors.text, bodyColor: colors.muted, borderColor: colors.line, borderWidth: 1 },
        },
        scales: {
          x: { ticks: { color: colors.muted, maxRotation: 0, autoSkipPadding: 16 }, grid: { display: false } },
          y: { type: "linear", position: "left", ticks: { color: colors.muted }, grid: { color: colors.line } },
          y1: { type: "linear", position: "right", ticks: { color: colors.muted }, grid: { display: false } },
        },
      },
    });
  };
  destroyChart(key);
  register(key, factory);
}

/* ---------- grouped bar histogram (per-model token consumption) ---------- */
export function renderStackedBar(key, canvas, labels, datasets, opts = {}) {
  if (!available()) return null;
  const factory = () => {
    const ctx = canvas.getContext("2d");
    const colors = themeColors();
    const palette = [colors.accent, colors.accent2, colors.success, colors.warning, colors.info, colors.danger, "#a855f7", "#ec4899", "#84cc16", "#f97316"];
    const chartDatasets = datasets.map((ds, i) => {
      const c = palette[i % palette.length];
      return {
        label: ds.label,
        data: ds.data,
        backgroundColor: hexA(c, 0.42),
        borderColor: c,
        borderWidth: 1,
        borderRadius: 3,
        borderSkipped: false,
        categoryPercentage: 0.76,
        barPercentage: 0.92,
        maxBarThickness: opts.maxBarThickness || 28,
      };
    });
    return new window.Chart(ctx, {
      type: "bar",
      data: { labels, datasets: chartDatasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: opts.legendPosition || "bottom",
            labels: {
              usePointStyle: true,
              boxWidth: 8,
              padding: 14,
              generateLabels(chart) {
                return chart.data.datasets.map((dataset, datasetIndex) => {
                  return {
                    text: dataset.label,
                    fillStyle: dataset.borderColor,
                    strokeStyle: dataset.borderColor,
                    fontColor: dataset.borderColor,
                    hidden: !chart.isDatasetVisible(datasetIndex),
                    datasetIndex,
                  };
                });
              },
            },
          },
          tooltip: {
            backgroundColor: colors.panel,
            titleColor: colors.text,
            bodyColor: colors.muted,
            borderColor: colors.line,
            borderWidth: 1,
            callbacks: opts.tooltipCallbacks || {},
          },
        },
        scales: {
          x: {
            ticks: { color: colors.muted, maxRotation: 0, autoSkipPadding: 12 },
            grid: { display: false },
          },
          y: {
            beginAtZero: true,
            title: { display: true, text: opts.yTitle || "Tokens", color: colors.muted },
            ticks: { color: colors.muted, callback: (v) => formatShort(v) },
            grid: { color: colors.line },
          },
        },
      },
    });
  };
  destroyChart(key);
  register(key, factory);
}

/* ---------- stacked line (per-model token consumption) ---------- */
export function renderStackedLine(key, canvas, labels, datasets, opts = {}) {
  if (!available()) return null;
  const factory = () => {
    const ctx = canvas.getContext("2d");
    const colors = themeColors();
    const palette = [colors.accent, colors.accent2, colors.success, colors.warning, colors.info, colors.danger, "#a855f7", "#ec4899", "#84cc16", "#f97316"];
    return new window.Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: datasets.map((ds, i) => {
          const c = palette[i % palette.length];
          return {
            label: ds.label,
            data: ds.data,
            borderColor: c,
            backgroundColor: hexA(c, 0.18),
            fill: true,
            tension: 0.35,
            pointRadius: 2,
            pointHoverRadius: 5,
            borderWidth: 2,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            labels: { color: colors.text, usePointStyle: true, boxWidth: 8, padding: 12 },
            position: opts.legendPosition || "top",
          },
          tooltip: {
            backgroundColor: colors.panel,
            titleColor: colors.text,
            bodyColor: colors.muted,
            borderColor: colors.line,
            borderWidth: 1,
            callbacks: opts.tooltipCallbacks || {},
          },
        },
        scales: {
          x: { ticks: { color: colors.muted, maxRotation: 0, autoSkipPadding: 16 }, grid: { display: false } },
          y: { stacked: true, ticks: { color: colors.muted, callback: (v) => formatShort(v) }, grid: { color: colors.line } },
        },
      },
    });
  };
  destroyChart(key);
  register(key, factory);
}

function formatShort(n) {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

/* ---------- donut chart (model distribution) ---------- */
export function renderDonut(key, canvas, labels, values, opts = {}) {
  if (!available()) return null;
  const factory = () => {
    const ctx = canvas.getContext("2d");
    const colors = themeColors();
    const palette = [colors.accent, colors.accent2, colors.success, colors.warning, colors.info, colors.danger, "#a855f7", "#ec4899"];
    return new window.Chart(ctx, {
      type: "doughnut",
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: labels.map((_, i) => palette[i % palette.length]),
          borderColor: colors.panel,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "65%",
        plugins: {
          legend: { position: "right", labels: { color: colors.text, usePointStyle: true, boxWidth: 8, padding: 12 } },
          tooltip: { backgroundColor: colors.panel, titleColor: colors.text, bodyColor: colors.muted, borderColor: colors.line, borderWidth: 1 },
        },
      },
    });
  };
  destroyChart(key);
  register(key, factory);
}

/* ---------- horizontal bar (channel success rate) ---------- */
export function renderHBar(key, canvas, labels, values, opts = {}) {
  if (!available()) return null;
  const factory = () => {
    const ctx = canvas.getContext("2d");
    const colors = themeColors();
    const max = Math.max(100, ...values);
    return new window.Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: opts.label || "%",
          data: values,
          backgroundColor: values.map((v) => v >= 95 ? colors.success : v >= 80 ? colors.warning : colors.danger),
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: colors.panel, titleColor: colors.text, bodyColor: colors.muted, borderColor: colors.line, borderWidth: 1 },
        },
        scales: {
          x: { min: 0, max: Math.min(max, 100), ticks: { color: colors.muted, callback: (v) => v + "%" }, grid: { color: colors.line } },
          y: { ticks: { color: colors.muted }, grid: { display: false } },
        },
      },
    });
  };
  destroyChart(key);
  register(key, factory);
}

/* ---------- sparkline (inline SVG, no Chart.js needed) ---------- */
export function sparkline(values, width = 100, height = 28) {
  if (!values || values.length === 0) return "";
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values.map((v, i) => {
    const x = i * step;
    const y = height - ((v - min) / range) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `
    <svg class="kpi-spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="sparkGrad${Math.random().toString(36).slice(2,7)}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.4"/>
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="${points}"/>
    </svg>`;
}

/* ---------- helpers ---------- */
function hexA(hex, a) {
  const h = hex.replace("#", "");
  if (h.length !== 6) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

function formatBucketLabel(bucket, bucketSize) {
  if (!bucket) return "";
  // SQLite returns string; PG may return Date
  const s = String(bucket);
  // try to parse "2026-07-09T08:00:00" or "2026-07-09"
  if (bucketSize === "hour") {
    const d = new Date(s.includes("T") ? s : s.replace(" ", "T"));
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    }
  }
  return s.slice(0, 10);
}
