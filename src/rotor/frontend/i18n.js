// i18n — reads config from window.ROTOR_UI_CONFIG, dispatches event on change.

const STORAGE_KEY = "rotor.locale";
const config = window.ROTOR_UI_CONFIG;

let savedLocale;
try { savedLocale = localStorage.getItem(STORAGE_KEY); } catch {}
export let locale = config.locales[savedLocale] ? savedLocale : config.defaultLocale;

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
  document.dispatchEvent(new CustomEvent("localechange", { detail: { locale } }));
}

export function setLocale(value) {
  if (!config.locales[value] || value === locale) return;
  locale = value;
  try { localStorage.setItem(STORAGE_KEY, locale); } catch (e) {}
  applyLocale();
}

export function toggleLocale() {
  setLocale(locale === "zh-CN" ? "en" : "zh-CN");
}
