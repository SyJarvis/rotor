// Theme state management — reads/writes <html data-theme> and persists to localStorage.

const STORAGE_KEY = "rotor.theme";

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "light";
}

export function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem(STORAGE_KEY, theme); } catch (e) {}
  document.dispatchEvent(new CustomEvent("themechange", { detail: { theme } }));
}

export function toggleTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}

// Listen to OS preference changes only when the user hasn't explicitly chosen.
if (typeof window !== "undefined" && window.matchMedia) {
  const mql = window.matchMedia("(prefers-color-scheme: dark)");
  mql.addEventListener?.("change", (event) => {
    let saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    if (!saved) setTheme(event.matches ? "dark" : "light");
  });
}
