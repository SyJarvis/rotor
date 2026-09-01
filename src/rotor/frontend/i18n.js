// i18n — reads config from window.ROTOR_UI_CONFIG, dispatches event on change.

const STORAGE_KEY = "rotor.locale";
const config = window.ROTOR_UI_CONFIG;

export let locale = localStorage.getItem(STORAGE_KEY) || config.defaultLocale;

export function t(key) {
  return key.split(".").reduce((value, part) => value?.[part], config.locales[locale]) ?? key;
}

export function applyLocale() {
  document.documentElement.lang = locale;
  document.title = t("pageTitle");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  const localeLabel = document.getElementById("localeLabel");
  if (localeLabel) localeLabel.textContent = locale === "zh-CN" ? "EN" : "中";
  document.dispatchEvent(new CustomEvent("localechange", { detail: { locale } }));
}

export function toggleLocale() {
  locale = locale === "zh-CN" ? "en" : "zh-CN";
  try { localStorage.setItem(STORAGE_KEY, locale); } catch (e) {}
  applyLocale();
}
