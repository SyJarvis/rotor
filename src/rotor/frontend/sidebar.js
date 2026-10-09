import { t } from "./i18n.js";

const STORAGE_KEY = "rotor.sidebar.collapsed";
let sidebar;
let shell;
let collapseToggle;
let menuToggle;
let tooltip;
let mobile;
let overlay = null;
let collapsed = false;
let hoveredItem = null;
let focusedItem = null;
let tooltipItem = null;

function hideTooltip() {
  tooltipItem?.removeAttribute("aria-describedby");
  tooltipItem = null;
  if (tooltip) tooltip.hidden = true;
}

function updateTooltip() {
  hideTooltip();
  const item = hoveredItem || focusedItem;
  if (!item || !collapsed || mobile.matches) return;
  tooltipItem = item;
  tooltip.textContent = item.getAttribute("aria-label");
  tooltip.hidden = false;
  const rect = item.getBoundingClientRect();
  tooltip.style.left = `${sidebar.getBoundingClientRect().right + 8}px`;
  tooltip.style.top = `${Math.max(24, Math.min(window.innerHeight - 24, rect.top + rect.height / 2))}px`;
  item.setAttribute("aria-describedby", tooltip.id);
}

function updateLabels() {
  const label = t(collapsed ? "expandSidebar" : "collapseSidebar");
  collapseToggle.setAttribute("aria-label", label);
  collapseToggle.title = label;
  const menuLabel = t(sidebar.classList.contains("open") ? "closeNavigation" : "openNavigation");
  menuToggle.setAttribute("aria-label", menuLabel);
  menuToggle.title = menuLabel;
  sidebar.querySelectorAll(".nav-item").forEach((item) => {
    const labelKey = item.querySelector("[data-i18n]")?.dataset.i18n;
    if (labelKey) item.setAttribute("aria-label", t(labelKey));
  });
  updateTooltip();
}

function renderState() {
  const desktopCollapsed = collapsed && !mobile.matches;
  shell.classList.toggle("sidebar-collapsed", desktopCollapsed);
  collapseToggle.setAttribute("aria-expanded", String(!desktopCollapsed));
  menuToggle.setAttribute("aria-expanded", String(sidebar.classList.contains("open")));
  sidebar.inert = mobile.matches && !sidebar.classList.contains("open");
  updateLabels();
}

export function closeSidebar({ restoreFocus = false } = {}) {
  if (!sidebar) return;
  const wasOpen = sidebar.classList.contains("open");
  sidebar.classList.remove("open");
  overlay?.remove();
  overlay = null;
  hoveredItem = null;
  focusedItem = null;
  hideTooltip();
  renderState();
  if (restoreFocus && wasOpen && mobile.matches) menuToggle.focus();
}

function openSidebar() {
  if (!mobile.matches || overlay) return;
  sidebar.classList.add("open");
  overlay = document.createElement("div");
  overlay.className = "sidebar-overlay";
  overlay.addEventListener("click", () => closeSidebar({ restoreFocus: true }));
  document.body.appendChild(overlay);
  renderState();
}

export function initSidebar() {
  sidebar = document.getElementById("sidebar");
  shell = document.querySelector(".app-shell");
  collapseToggle = document.getElementById("sidebarCollapseToggle");
  menuToggle = document.getElementById("menuToggle");
  tooltip = document.getElementById("sidebarTooltip");
  if (!sidebar || !shell || !collapseToggle || !menuToggle || !tooltip) return;
  mobile = window.matchMedia("(max-width: 860px)");
  try { collapsed = localStorage.getItem(STORAGE_KEY) === "true"; } catch (e) {}

  collapseToggle.addEventListener("click", () => {
    if (mobile.matches) return;
    collapsed = !collapsed;
    try { localStorage.setItem(STORAGE_KEY, String(collapsed)); } catch (e) {}
    hoveredItem = null;
    focusedItem = null;
    renderState();
  });
  menuToggle.addEventListener("click", () => {
    sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
  });
  sidebar.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("mouseenter", () => { hoveredItem = item; updateTooltip(); });
    item.addEventListener("mouseleave", () => { hoveredItem = null; updateTooltip(); });
    item.addEventListener("focus", () => { focusedItem = item; updateTooltip(); });
    item.addEventListener("blur", () => { focusedItem = null; updateTooltip(); });
    item.addEventListener("click", () => closeSidebar());
  });
  sidebar.querySelector(".nav")?.addEventListener("scroll", updateTooltip);
  mobile.addEventListener("change", () => {
    const focusWasInSidebar = sidebar.contains(document.activeElement);
    closeSidebar();
    if (mobile.matches && focusWasInSidebar) menuToggle.focus();
    else if (!mobile.matches && document.activeElement === menuToggle) collapseToggle.focus();
  });
  window.addEventListener("resize", updateTooltip);
  document.addEventListener("localechange", updateLabels);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar({ restoreFocus: true });
  });
  renderState();
}
