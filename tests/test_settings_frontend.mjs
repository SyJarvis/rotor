import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const base = new URL('../src/rotor/frontend/', import.meta.url);
const sources = Object.fromEntries(await Promise.all(['settings.js', 'i18n.js', 'theme.js', 'config.js', 'index.html'].map(async (name) => [name, await readFile(new URL(name, base), 'utf8')])));
const savedSettings = () => ({
  display_timezone: 'Asia/Shanghai', settings_path: '/tmp/settings.json',
  routing: { strategy: 'adaptive', affinity_enabled: true, session_lease_enabled: true,
    session_lease_idle_ttl_seconds: 1800, session_lease_reassess_seconds: 300,
    adaptive_success_weight: .55, protocol_affinity_enabled: true, adaptive_prior_failures: 1 },
});
const settle = () => new Promise((resolve) => setImmediate(resolve));
// Public exports and toggle behavior from the module cached before Settings was introduced.
const cachedI18nSource = `
const config = window.ROTOR_UI_CONFIG;
export let locale = localStorage.getItem("rotor.locale") || config.defaultLocale;
export function t(key) { return config.locales[locale][key] ?? key; }
export function applyLocale() {
  document.documentElement.lang = locale;
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.dispatchEvent(new CustomEvent("localechange", { detail: { locale } }));
}
export function toggleLocale() {
  locale = locale === "zh-CN" ? "en" : "zh-CN";
  localStorage.setItem("rotor.locale", locale);
  applyLocale();
}
`;

async function environment({ storageFails = false, storedLocale, gap = 0, cachedI18n = false, role = 'admin' } = {}) {
  const storage = new Map(storedLocale ? [['rotor.locale', storedLocale]] : []);
  const nodes = new Map();
  let document;
  function element(tag = 'div', attrs = {}) {
    const listeners = new Map(), classes = new Set((attrs.class || '').split(' ').filter(Boolean));
    const node = {
      tagName: tag.toUpperCase(), id: attrs.id || '', name: attrs.name || '', dataset: {}, children: [], parentNode: null,
      hidden: 'hidden' in attrs, disabled: 'disabled' in attrs, inert: false, style: {}, value: attrs.value || '', checked: false,
      tabIndex: Number(attrs.tabindex ?? 0), textContent: '', clientWidth: 1280 - gap,
      classList: { contains: (name) => classes.has(name), add: (name) => classes.add(name), remove: (name) => classes.delete(name), toggle(name, force) { const yes = force ?? !classes.has(name); yes ? classes.add(name) : classes.delete(name); return yes; } },
      setAttribute(name, value) { attrs[name] = String(value); }, getAttribute: (name) => attrs[name] ?? null,
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      contains(target) { return target === this || this.children.some((child) => child.contains(target)); },
      matches(selector) {
        if (selector === '[hidden]') return this.hidden;
        if (selector === '[inert]') return this.inert;
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        const attr = selector.match(/^\[([^=]+)(?:="([^"]*)")?\]$/);
        if (attr) return attr[1] in attrs && (attr[2] === undefined || attrs[attr[1]] === attr[2]);
        return this.tagName.toLowerCase() === selector;
      },
      closest(selector) { for (let item = this; item; item = item.parentNode) if (item.matches(selector)) return item; return null; },
      querySelectorAll(selector) { const selectors = selector.split(',').map((value) => value.trim()); return this.children.flatMap((child) => [...(selectors.some((part) => child.matches(part)) ? [child] : []), ...child.querySelectorAll(selector)]); },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      getClientRects() { for (let item = this; item; item = item.parentNode) if (item.hidden || item.classList.contains('hidden')) return []; return [{}]; },
      addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(callback); },
      dispatchEvent(event) {
        event.target ??= this; event.currentTarget = this;
        const results = (listeners.get(event.type) || []).map((callback) => callback(event));
        if (event.bubbles && !event.stopped && this.parentNode) results.push(this.parentNode.dispatchEvent(event));
        return Promise.all(results);
      },
      emit(type, detail = {}) { return this.dispatchEvent({ type, target: this, bubbles: true, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...detail }); },
      focus() { document.activeElement = this; document.dispatchEvent({ type: 'focusin', target: this }); },
    };
    for (const [name, value] of Object.entries(attrs)) if (name.startsWith('data-')) node.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = value;
    if (node.id) nodes.set(node.id, node);
    return node;
  }
  document = element('document');
  document.documentElement = element('html', { 'data-theme': 'light' });
  document.body = document.appendChild(element('body'));
  document.body.style = { overflow: 'auto', paddingRight: '3px' };
  const shell = document.body.appendChild(element('div', { class: 'app-shell' }));
  const opener = shell.appendChild(element('button', { id: 'openSettings' }));
  shell.appendChild(element('button', { id: 'menuToggle' }));
  const fragment = sources['index.html'].split('    <div class="modal hidden" id="settingsModal"')[1].split('    <div class="modal hidden" id="chatDeleteModal"')[0];
  const html = '<div class="modal hidden" id="settingsModal"' + fragment;
  const stack = [document.body];
  for (const match of html.matchAll(/<\/?[a-z][^>]*>/gi)) {
    const token = match[0];
    if (token.startsWith('</')) { stack.pop(); continue; }
    const tag = token.match(/^<([^\s>]+)/)[1];
    const attrs = Object.fromEntries([...token.slice(tag.length + 1, -1).matchAll(/([^\s=]+)(?:="([^"]*)")?/g)].map((attr) => [attr[1], attr[2] || '']));
    const child = stack.at(-1).appendChild(element(tag, attrs));
    if (!['input', 'br', 'hr', 'meta', 'link', 'img'].includes(tag)) stack.push(child);
  }
  document.getElementById = (id) => nodes.get(id) || null;
  const modal = nodes.get('settingsModal'), form = nodes.get('settingsForm');
  form.elements = Object.fromEntries(form.querySelectorAll('input, select').filter((field) => field.name).map((field) => [field.name, field]));
  const window = element('window');
  Object.assign(window, { innerWidth: 1280, matchMedia: () => ({ addEventListener() {} }) });
  vm.runInNewContext(sources['config.js'], { window });
  const calls = [], saved = [], events = [];
  const context = vm.createContext({ document, window, AbortController, Intl, getComputedStyle: () => ({ paddingRight: '3px' }),
    CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
    localStorage: { getItem(key) { if (storageFails) throw new Error('blocked'); return storage.get(key) ?? null; }, setItem(key, value) { if (storageFails) throw new Error('blocked'); storage.set(key, value); } },
  });
  for (const name of ['localechange', 'themechange']) document.addEventListener(name, () => events.push(name));
  const modules = new Map();
  async function load(name) {
    if (modules.has(name)) return modules.get(name);
    const module = new vm.SourceTextModule(cachedI18n && name === 'i18n.js' ? cachedI18nSource : sources[name], { context, identifier: name });
    modules.set(name, module);
    await module.link(async (specifier) => {
      if (specifier.startsWith('./session.js')) return new vm.SyntheticModule(['isAdmin'], function () { this.setExport('isAdmin', () => role === 'admin'); }, { context });
      if (specifier.startsWith('./account.js')) return new vm.SyntheticModule(['initAccount'], function () { this.setExport('initAccount', () => ({ load() {}, close() {} })); }, { context });
      if (specifier.startsWith('./security.js')) return new vm.SyntheticModule(['initSecurity'], function () { this.setExport('initSecurity', () => ({ open() {}, close() {} })); }, { context });
      if (specifier.startsWith('./api.js')) return new vm.SyntheticModule(['api'], function () {
        this.setExport('api', (path, options = {}) => new Promise((resolve, reject) => calls.push({ path, options, resolve, reject })));
      }, { context });
      if (specifier.startsWith('./sidebar.js')) return new vm.SyntheticModule(['closeSidebar'], function () { this.setExport('closeSidebar', () => {}); }, { context });
      return load(specifier.slice(2));
    });
    return module;
  }
  const module = await load('settings.js'); await module.evaluate();
  const controller = module.namespace.initSettings({ onSaved: (settings) => saved.push(settings) });
  modules.get('i18n.js').namespace.applyLocale();
  async function openAndLoad(result = savedSettings()) { opener.focus(); await opener.emit('click'); calls.at(-1).resolve(result); await settle(); }
  async function edit(name, value) { const field = form.elements[name]; field[typeof value === 'boolean' ? 'checked' : 'value'] = value; await field.emit('input'); }
  return { get: (id) => nodes.get(id), form, modal, document, window, shell, opener, calls, saved, events, storage, controller, openAndLoad, edit,
    selectTab: (name) => nodes.get(`settingsTab${name[0].toUpperCase()}${name.slice(1)}`).emit('click'),
    submit: () => form.emit('submit'),
  };
}

test('load disables server controls, retains local preferences, and displays Adaptive and settings path', async () => {
  const h = await environment(); h.controller.open();
  assert.equal(h.calls.length, 1); h.controller.open(); assert.equal(h.calls.length, 1);
  assert.equal(h.form.elements.strategy.disabled, true); assert.equal(h.get('settingsThemeDark').disabled, false);
  assert.equal(h.get('settingsSave').disabled, true);
  assert.equal(h.get('settingsThemeLight').getAttribute('aria-pressed'), 'true');
  assert.equal(h.get('settingsThemeLight').classList.contains('active'), true);
  assert.equal(h.get('settingsThemeDark').getAttribute('aria-pressed'), 'false');
  h.calls[0].resolve(savedSettings()); await settle();
  assert.equal(h.form.elements.strategy.value, 'adaptive');
  assert.ok(h.form.elements.strategy.children.some((node) => node.value === 'adaptive'));
  assert.equal(h.get('settingsPath').textContent, '/tmp/settings.json');
  assert.equal(h.form.elements.strategy.disabled, false); assert.equal(h.get('settingsSave').disabled, true);
  await h.submit(); assert.equal(h.calls.length, 1);
});

test('closing and reopening retains dirty server values, category, and immediately applied theme', async () => {
  const h = await environment(); await h.openAndLoad(); await h.selectTab('routing');
  await h.edit('session_lease_idle_ttl_seconds', '2700'); await h.edit('affinity_enabled', false);
  await h.get('settingsThemeDark').emit('click');
  h.controller.close(); h.controller.open();
  assert.equal(h.calls.length, 1); assert.equal(h.get('settingsRouting').hidden, false);
  assert.equal(h.form.elements.session_lease_idle_ttl_seconds.value, '2700'); assert.equal(h.form.elements.affinity_enabled.checked, false);
  assert.equal(h.get('settingsThemeDark').getAttribute('aria-pressed'), 'true'); assert.equal(h.storage.get('rotor.theme'), 'dark');
  assert.equal(h.get('settingsSave').disabled, false);
});

test('save sends full hidden routing configuration, prevents duplicate requests, and stays on current category', async () => {
  const h = await environment(); await h.openAndLoad(); await h.selectTab('routing'); await h.edit('session_lease_idle_ttl_seconds', '2400');
  const request = h.submit(); await h.submit();
  assert.equal(h.calls.length, 2); const call = h.calls[1]; const payload = JSON.parse(call.options.body);
  assert.equal(call.options.method, 'PUT'); assert.deepEqual(payload.routing, { ...savedSettings().routing, session_lease_idle_ttl_seconds: 2400 });
  assert.equal('settings_path' in payload, false); assert.equal(h.form.elements.strategy.disabled, true);
  call.resolve({ ...savedSettings(), ...payload }); await request;
  assert.equal(h.modal.classList.contains('hidden'), false); assert.equal(h.get('settingsRouting').hidden, false);
  assert.equal(h.get('settingsStatus').textContent, '设置已保存并立即生效'); assert.equal(h.get('settingsSave').disabled, true);
  assert.equal(h.saved.length, 1); await h.submit(); assert.equal(h.calls.length, 2);
  await h.edit('session_lease_idle_ttl_seconds', '3000'); assert.equal(h.get('settingsSave').disabled, false);
  await h.edit('session_lease_idle_ttl_seconds', '2400'); assert.equal(h.get('settingsSave').disabled, true);
  assert.equal(h.form.elements.session_lease_idle_ttl_seconds.value, '2400');
  h.controller.close(); h.controller.open();
  assert.equal(h.get('settingsStatus').textContent, '');
  h.calls[2].reject(new Error('refresh failed')); await settle();
  assert.equal(h.get('settingsStatus').textContent, '');
  assert.match(h.get('settingsLoadMessage').textContent, /refresh failed/);
});

test('failed save preserves draft across close/reopen and can retry; pending save survives close without another GET', async () => {
  const h = await environment(); await h.openAndLoad(); await h.edit('display_timezone', 'UTC');
  let pending = h.submit(); h.controller.close(); h.controller.open();
  assert.equal(h.calls.length, 2); h.calls[1].reject(new Error('offline')); await pending;
  assert.equal(h.form.elements.display_timezone.value, 'UTC'); assert.match(h.get('settingsStatus').textContent, /offline/);
  assert.equal(h.get('settingsSave').disabled, false); h.controller.close(); h.controller.open(); assert.equal(h.calls.length, 2);
  pending = h.submit(); h.calls[2].resolve({ ...savedSettings(), display_timezone: 'UTC' }); await pending;
  assert.equal(h.get('settingsSave').disabled, true); assert.equal(h.saved.length, 1);
});

test('late GET after close cannot overwrite reopened newer settings or edits', async () => {
  const h = await environment(); h.controller.open(); const old = h.calls[0]; h.controller.close();
  assert.equal(old.options.signal.aborted, true); h.controller.open(); assert.equal(h.calls.length, 2);
  h.calls[1].resolve({ ...savedSettings(), display_timezone: 'UTC' }); await settle(); await h.edit('display_timezone', 'Europe/London');
  old.resolve(savedSettings()); await settle();
  assert.equal(h.form.elements.display_timezone.value, 'Europe/London'); assert.equal(h.get('settingsSave').disabled, false);
});

test('failed initial load leaves preferences usable and retry recovers; clean reopen refresh failure blocks stale server fields', async () => {
  const h = await environment(); h.controller.open(); h.calls[0].reject(new Error('unavailable')); await settle();
  assert.equal(h.modal.classList.contains('hidden'), false); assert.equal(h.get('settingsRetry').hidden, false);
  assert.equal(h.form.elements.display_timezone.disabled, true); assert.equal(h.get('settingsLocale').disabled, false);
  h.get('settingsLocale').value = 'en'; await h.get('settingsLocale').emit('change'); assert.match(h.get('settingsLoadMessage').textContent, /Server settings/);
  const retry = h.get('settingsRetry').emit('click'); await h.get('settingsRetry').emit('click'); assert.equal(h.calls.length, 2);
  h.calls[1].resolve(savedSettings()); await retry; assert.equal(h.form.elements.display_timezone.disabled, false);
  h.controller.close(); h.controller.open(); assert.equal(h.calls.length, 3); h.calls[2].reject(new Error('offline')); await settle();
  assert.equal(h.form.elements.strategy.disabled, true); assert.equal(h.get('settingsThemeDark').disabled, false);
});

test('immediate locale/theme controls use real shared modules and preserve focused server draft and category', async () => {
  const h = await environment(); await h.openAndLoad(); await h.edit('display_timezone', 'Europe/London'); await h.selectTab('routing');
  h.get('settingsLocale').focus(); h.get('settingsLocale').value = 'en'; await h.get('settingsLocale').emit('change');
  assert.equal(h.storage.get('rotor.locale'), 'en'); assert.equal(h.document.documentElement.lang, 'en');
  assert.equal(h.get('settingsModalTitle').textContent, 'Settings'); assert.equal(h.get('settingsClose').getAttribute('aria-label'), 'Close');
  assert.equal(h.get('settingsRouting').hidden, false); assert.equal(h.form.elements.display_timezone.value, 'Europe/London');
  assert.equal(h.document.activeElement, h.get('settingsLocale')); assert.equal(h.calls.length, 1);
  await h.get('settingsThemeDark').emit('click');
  assert.equal(h.document.documentElement.getAttribute('data-theme'), 'dark'); assert.ok(h.events.includes('themechange'));
  assert.equal(h.get('settingsThemeDark').classList.contains('active'), true);
  assert.equal(h.get('settingsThemeLight').getAttribute('aria-pressed'), 'false');
  const themeEvents = h.events.filter((name) => name === 'themechange').length;
  await h.get('settingsThemeDark').emit('click');
  assert.equal(h.events.filter((name) => name === 'themechange').length, themeEvents);
  assert.equal(h.get('settingsThemeLight').textContent, 'Light'); assert.equal(h.get('settingsThemeDark').textContent, 'Dark');
  h.controller.close(); h.controller.open();
  assert.equal(h.get('settingsLocale').value, 'en'); assert.equal(h.get('settingsThemeDark').getAttribute('aria-pressed'), 'true');
  assert.equal(h.form.elements.display_timezone.value, 'Europe/London');
});

test('invalid saved locale and unavailable storage do not prevent local preference changes', async () => {
  for (const options of [{ storedLocale: 'unknown' }, { storageFails: true }]) {
    const h = await environment(options); h.controller.open();
    assert.equal(h.get('settingsLocale').value, 'zh-CN'); h.get('settingsLocale').value = 'en'; await h.get('settingsLocale').emit('change');
    await h.get('settingsThemeDark').emit('click');
    assert.equal(h.get('settingsLocale').value, 'en'); assert.equal(h.get('settingsThemeDark').getAttribute('aria-pressed'), 'true');
    h.controller.close();
  }
});

test('TTL and timezone validation selects and focuses invalid field without sending a PUT', async () => {
  const h = await environment(); await h.openAndLoad();
  for (const value of ['59', '86401', '60.5', '']) {
    await h.edit('session_lease_idle_ttl_seconds', value); await h.selectTab('general'); await h.submit();
    assert.equal(h.calls.length, 1); assert.equal(h.get('settingsRouting').hidden, false); assert.equal(h.document.activeElement, h.form.elements.session_lease_idle_ttl_seconds);
  }
  await h.edit('session_lease_idle_ttl_seconds', '60'); await h.edit('display_timezone', 'Mars/Space'); await h.submit();
  assert.equal(h.calls.length, 1); assert.equal(h.get('settingsGeneral').hidden, false); assert.equal(h.document.activeElement, h.form.elements.display_timezone);
});

test('dialog contains focus, handles Escape and backdrop locally, restores opener and background styles', async () => {
  const h = await environment({ gap: 15 }); await h.openAndLoad();
  assert.equal(h.document.activeElement, h.get('settingsClose')); assert.equal(h.shell.inert, true);
  assert.equal(h.document.body.style.overflow, 'hidden'); assert.equal(h.document.body.style.paddingRight, '18px');
  await h.get('settingsClose').emit('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(h.document.activeElement, h.form.elements.display_timezone);
  await h.form.elements.display_timezone.emit('keydown', { key: 'Tab' }); assert.equal(h.document.activeElement, h.get('settingsClose'));
  h.opener.focus(); assert.equal(h.document.activeElement, h.get('settingsClose'));
  let escaped = 0, clicked = 0; h.document.addEventListener('keydown', () => escaped++); h.document.addEventListener('click', () => clicked++);
  await h.get('settingsClose').emit('keydown', { key: 'Escape' });
  assert.equal(escaped, 0); assert.equal(h.modal.classList.contains('hidden'), true); assert.equal(h.document.activeElement, h.opener);
  assert.equal(h.shell.inert, false); assert.equal(h.document.body.style.overflow, 'auto'); assert.equal(h.document.body.style.paddingRight, '3px');
  h.controller.open(); await h.modal.querySelector('.modal-backdrop').emit('click'); assert.equal(clicked, 0); assert.equal(h.modal.classList.contains('hidden'), true);
});

test('auth expiry releases the modal focus lock and background while retaining unsaved settings', async () => {
  const h = await environment(); await h.openAndLoad(); await h.edit('display_timezone', 'UTC');
  await h.window.emit('rotorauthrequired');
  assert.equal(h.modal.classList.contains('hidden'), true); assert.equal(h.shell.inert, false);
  h.opener.focus(); assert.equal(h.document.activeElement, h.opener);
  h.controller.open(); assert.equal(h.form.elements.display_timezone.value, 'UTC'); assert.equal(h.calls.length, 1);
});

test('Settings links against cached i18n exports and language selection only toggles valid changed values', async () => {
  const h = await environment({ cachedI18n: true }); await h.openAndLoad();
  const select = h.get('settingsLocale'); const initialEvents = h.events.length;
  select.value = 'zh-CN'; await select.emit('change'); assert.equal(h.events.length, initialEvents);
  select.value = 'unsupported'; await select.emit('change'); assert.equal(h.events.length, initialEvents);
  select.value = 'en'; await select.emit('change');
  assert.equal(h.document.documentElement.lang, 'en'); assert.equal(h.storage.get('rotor.locale'), 'en');
  assert.equal(h.get('settingsModalTitle').textContent, 'Settings');
  select.value = 'en'; await select.emit('change'); assert.equal(h.events.length, initialEvents + 1);
  select.value = 'zh-CN'; await select.emit('change');
  assert.equal(h.document.documentElement.lang, 'zh-CN'); assert.equal(h.get('settingsModalTitle').textContent, '设置');
});

test('ordinary user settings never request admin settings and expose General, Account and Login and security', async () => {
  const h = await environment({ role: 'user' }); h.controller.open();
  assert.equal(h.calls.length, 0); assert.equal(h.get('settingsTabRouting').hidden, true);
  assert.equal(h.get('settingsFooter').hidden, true); assert.equal(h.form.elements.display_timezone.closest('[data-admin-setting]').hidden, true);
  await h.get('settingsTabGeneral').emit('keydown', { key: 'ArrowRight' });
  assert.equal(h.get('settingsAccount').hidden, false); assert.equal(h.document.activeElement, h.get('settingsTabAccount'));
  await h.submit(); assert.equal(h.calls.length, 0);
  await h.get('settingsThemeDark').emit('click'); assert.equal(h.storage.get('rotor.theme'), 'dark');
});

test('security opens for an ordinary account and keeps server settings footer hidden', async () => {
  const h = await environment({ role: 'user' }); h.controller.open('security');
  assert.ok(h.get('settingsTabSecurity'), 'Login and security tab is present');
  assert.equal(h.get('settingsSecurity').hidden, false);
  assert.equal(h.get('settingsAccount').hidden, true);
  assert.equal(h.get('settingsFooter').hidden, true);
  assert.equal(h.calls.length, 0, 'ordinary account never loads admin settings');
  assert.equal(h.get('accountCurrentPassword').closest('[data-settings-panel]').dataset.settingsPanel, 'security');
  assert.equal(h.get('accountLogout').closest('[data-settings-panel]').dataset.settingsPanel, 'security');
});

test('security category preserves administrator draft and never submits it from account actions', async () => {
  const h = await environment(); await h.openAndLoad(); await h.edit('display_timezone', 'UTC');
  await h.selectTab('security'); assert.equal(h.get('settingsFooter').hidden, true);
  await h.submit(); assert.equal(h.calls.length, 1);
  h.controller.close(); h.controller.open(); assert.equal(h.get('settingsSecurity').hidden, false);
  await h.selectTab('general'); assert.equal(h.form.elements.display_timezone.value, 'UTC');
  assert.equal(h.get('settingsSave').disabled, false);
});

test('admin load errors are only shown in server settings categories and reappear with retry intact', async () => {
  const h = await environment(); h.controller.open(); h.calls[0].reject(new Error('admin offline')); await settle();
  assert.equal(h.get('settingsLoadNotice').classList.contains('hidden'), false);
  await h.selectTab('security'); assert.equal(h.get('settingsLoadNotice').classList.contains('hidden'), true);
  await h.selectTab('account'); assert.equal(h.get('settingsLoadNotice').classList.contains('hidden'), true);
  await h.selectTab('routing'); assert.equal(h.get('settingsLoadNotice').classList.contains('hidden'), false);
  assert.match(h.get('settingsLoadMessage').textContent, /admin offline/); assert.equal(h.get('settingsRetry').hidden, false);
});

test('deep-linked security opened from the document body restores focus to the navigation button on Escape', async () => {
  const h = await environment({ role: 'user' }); h.document.body.focus(); h.controller.open('security');
  await h.get('settingsClose').emit('keydown', { key: 'Escape' });
  assert.equal(h.document.activeElement, h.get('menuToggle'));
});

test('desktop deep-linked security restores focus to visible Settings when the mobile navigation button is hidden', async () => {
  const h = await environment({ role: 'user' }); h.get('menuToggle').getClientRects = () => [];
  h.document.body.focus(); h.controller.open('security');
  await h.get('settingsClose').emit('keydown', { key: 'Escape' });
  assert.equal(h.document.activeElement.id, 'openSettings');
});
