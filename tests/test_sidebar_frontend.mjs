import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/sidebar.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../src/rotor/frontend/index.html', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');
const storageKey = 'rotor.sidebar.collapsed';

async function environment({ width = 1200, saved, language = 'zh-CN', storageFails = false } = {}) {
  const storage = new Map([
    ['rotor.mindagent.conversations.v1', '[{"draft":"keep this"}]'],
    ...(saved === undefined ? [] : [[storageKey, saved]]),
  ]);
  const navigation = [];
  const actions = [];
  const nodes = new Map();
  let document;
  function element(id = '') {
    const attrs = new Map();
    const classes = new Set();
    const listeners = new Map();
    return {
      id, dataset: {}, style: {}, hidden: false, inert: false, title: '', textContent: '',
      children: [], parentNode: null,
      classList: {
        contains: (name) => classes.has(name), add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        toggle(name, force) { const enabled = force ?? !classes.has(name); enabled ? classes.add(name) : classes.delete(name); return enabled; },
      },
      setAttribute: (name, value) => attrs.set(name, String(value)),
      getAttribute: (name) => attrs.get(name), removeAttribute: (name) => attrs.delete(name),
      addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(callback); },
      dispatch(name, event = {}) { for (const callback of listeners.get(name) || []) callback({ target: this, ...event }); },
      appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); this.parentNode = null; },
      contains(target) { return target === this || this.children.some((child) => child.contains(target)); },
      getBoundingClientRect: () => ({ right: 64, top: 100, height: 36 }),
      querySelector: () => null, querySelectorAll: () => [],
      click() { this.dispatch('click'); },
      focus() { document.activeElement?.dispatch('blur'); document.activeElement = this; this.dispatch('focus'); },
    };
  }
  for (const id of ['sidebar', 'sidebarCollapseToggle', 'menuToggle', 'sidebarTooltip']) {
    const node = element(id);
    const tag = html.match(new RegExp(`<[^>]*id="${id}"[^>]*>`))?.[0];
    assert.ok(tag, `${id} exists in the real shell`);
    for (const match of tag.matchAll(/(aria-[\w-]+)="([^"]*)"/g)) node.setAttribute(match[1], match[2]);
    nodes.set(id, node);
  }
  const shell = element('shell');
  const nav = element('nav');
  const body = element('body');
  const sidebar = nodes.get('sidebar');
  shell.appendChild(sidebar);
  sidebar.appendChild(nav);
  body.appendChild(shell);
  body.appendChild(nodes.get('sidebarTooltip'));
  for (const match of html.matchAll(/<button class="nav-item[^"]*"([^>]*)>([\s\S]*?)<\/button>/g)) {
    const id = match[1].match(/id="([^"]+)"/)?.[1];
    const tab = match[1].match(/data-tab="([^"]+)"/)?.[1];
    const key = match[2].match(/data-i18n="([^"]+)"/)?.[1];
    const item = element(id || tab);
    const label = element();
    label.dataset.i18n = key;
    item.dataset.tab = tab;
    item.querySelector = (selector) => selector === '[data-i18n]' ? label : null;
    item.addEventListener('click', () => actions.push(id || tab));
    navigation.push(item);
    nav.appendChild(item);
  }
  sidebar.querySelectorAll = (selector) => selector === '.nav-item' ? navigation : [];
  sidebar.querySelector = (selector) => selector === '.nav' ? nav : null;
  document = element('document');
  Object.assign(document, {
    body, activeElement: null,
    getElementById: (id) => nodes.get(id),
    querySelector: (selector) => selector === '.app-shell' ? shell : null,
    createElement: () => element(),
  });
  const mobile = element('media');
  mobile.matches = width <= 860;
  const window = element('window');
  Object.assign(window, { innerWidth: width, innerHeight: 800, matchMedia: () => mobile });
  vm.runInNewContext(translations, { window });
  const context = vm.createContext({
    document, window,
    localStorage: {
      getItem(key) { if (storageFails) throw new Error('Storage blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (storageFails) throw new Error('Storage full'); storage.set(key, value); },
    },
  });
  const module = new vm.SourceTextModule(source, { context });
  await module.link(() => new vm.SyntheticModule(['t'], function () {
    this.setExport('t', (key) => window.ROTOR_UI_CONFIG.locales[language][key] ?? key);
  }, { context }));
  await module.evaluate();
  module.namespace.initSidebar();
  return {
    sidebar, shell, navigation, actions, storage, body, document,
    toggle: nodes.get('sidebarCollapseToggle'), menu: nodes.get('menuToggle'), tooltip: nodes.get('sidebarTooltip'),
    collapsed: () => shell.classList.contains('sidebar-collapsed'),
    overlays: () => body.children.filter((node) => node.className === 'sidebar-overlay'),
    resize(nextWidth) {
      const wasMobile = mobile.matches;
      window.innerWidth = nextWidth;
      mobile.matches = nextWidth <= 860;
      if (wasMobile !== mobile.matches) mobile.dispatch('change');
      window.dispatch('resize');
    },
    locale(nextLanguage) { language = nextLanguage; document.dispatch('localechange'); },
    escape() { document.dispatch('keydown', { key: 'Escape' }); },
  };
}

test('desktop collapse persists across reloads without changing conversation data', async () => {
  const env = await environment();
  assert.equal(env.collapsed(), false);
  assert.equal(env.toggle.getAttribute('aria-controls'), 'sidebar');
  assert.equal(env.toggle.getAttribute('aria-expanded'), 'true');
  env.toggle.click();
  assert.equal(env.collapsed(), true);
  assert.equal(env.toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(env.toggle.getAttribute('aria-label'), '展开侧栏');
  assert.equal(env.storage.get(storageKey), 'true');
  assert.equal(env.storage.get('rotor.mindagent.conversations.v1'), '[{"draft":"keep this"}]');
  const reloaded = await environment({ saved: env.storage.get(storageKey) });
  assert.equal(reloaded.collapsed(), true);
  reloaded.toggle.click();
  assert.equal(reloaded.collapsed(), false);
  assert.equal(reloaded.storage.get(storageKey), 'false');
  assert.equal(reloaded.toggle.getAttribute('aria-label'), '收起侧栏');
});

test('missing or invalid saved preferences start expanded and storage failures remain usable', async () => {
  for (const options of [{ saved: 'invalid' }, { saved: 'false' }, { storageFails: true }]) {
    const env = await environment(options);
    assert.equal(env.collapsed(), false);
    assert.doesNotThrow(() => env.toggle.click());
    assert.equal(env.collapsed(), true);
    assert.doesNotThrow(() => env.toggle.click());
    assert.equal(env.collapsed(), false);
  }
});

test('every collapsed navigation and settings button retains its action and accessible name', async () => {
  const env = await environment({ saved: 'true' });
  assert.equal(env.navigation.length, 9);
  for (const item of env.navigation) {
    assert.ok(item.getAttribute('aria-label'));
    item.dispatch('mouseenter');
    assert.equal(env.tooltip.hidden, false);
    assert.equal(env.tooltip.textContent, item.getAttribute('aria-label'));
    assert.equal(item.getAttribute('aria-describedby'), 'sidebarTooltip');
    item.click();
    assert.equal(env.actions.at(-1), item.id);
    assert.equal(env.tooltip.hidden, true);
    assert.equal(env.collapsed(), true);
  }
  assert.deepEqual(env.actions, ['overview', 'channels', 'tokens', 'usage', 'logs', 'monitoring', 'chat', 'users', 'openSettings']);
  assert.equal(env.overlays().length, 0);
});

test('keyboard focus shows names, language changes update visible labels, and Escape dismisses the tooltip', async () => {
  const env = await environment({ saved: 'true' });
  const item = env.navigation[1];
  item.focus();
  assert.equal(env.tooltip.hidden, false);
  assert.equal(env.tooltip.textContent, '模型渠道');
  item.dispatch('mouseenter');
  item.dispatch('mouseleave');
  assert.equal(env.tooltip.hidden, false, 'a focused item retains its tooltip after mouse leave');
  env.locale('en');
  assert.equal(item.getAttribute('aria-label'), 'Channels');
  assert.equal(env.tooltip.textContent, 'Channels');
  assert.equal(env.toggle.getAttribute('aria-label'), 'Expand sidebar');
  env.escape();
  assert.equal(env.tooltip.hidden, true);
  assert.equal(item.getAttribute('aria-describedby'), undefined);
  assert.equal(env.collapsed(), true);
  assert.equal(env.document.activeElement, item);
});

test('expanded desktop navigation does not show icon-only tooltips', async () => {
  const env = await environment();
  env.navigation[0].dispatch('mouseenter');
  env.navigation[0].focus();
  assert.equal(env.tooltip.hidden, true);
  assert.equal(env.overlays().length, 0);
  env.menu.click();
  assert.equal(env.overlays().length, 0, 'the mobile control cannot open a desktop overlay');
});

test('mobile ignores desktop collapse while preserving its preference and full drawer controls', async () => {
  const env = await environment({ width: 375, saved: 'true' });
  assert.equal(env.collapsed(), false);
  assert.equal(env.sidebar.inert, true);
  assert.equal(env.menu.getAttribute('aria-expanded'), 'false');
  assert.equal(env.menu.getAttribute('aria-controls'), 'sidebar');
  env.toggle.click();
  assert.equal(env.storage.get(storageKey), 'true');
  env.menu.click();
  assert.equal(env.sidebar.inert, false);
  assert.equal(env.sidebar.classList.contains('open'), true);
  assert.equal(env.menu.getAttribute('aria-expanded'), 'true');
  assert.equal(env.overlays().length, 1);
  env.navigation[0].dispatch('mouseenter');
  assert.equal(env.tooltip.hidden, true);
  env.locale('en');
  assert.equal(env.menu.getAttribute('aria-label'), 'Close navigation');
  env.overlays()[0].click();
  assert.equal(env.overlays().length, 0);
  assert.equal(env.sidebar.inert, true);
  assert.equal(env.menu.getAttribute('aria-label'), 'Open navigation');
  assert.equal(env.document.activeElement, env.menu);
});

test('crossing the exact mobile breakpoint cleans up overlays and restores desktop preference', async () => {
  const env = await environment({ width: 861, saved: 'true' });
  assert.equal(env.collapsed(), true);
  env.navigation[0].focus();
  env.resize(860);
  assert.equal(env.collapsed(), false);
  assert.equal(env.sidebar.inert, true);
  assert.equal(env.tooltip.hidden, true);
  assert.equal(env.document.activeElement, env.menu);
  env.menu.click();
  assert.equal(env.overlays().length, 1);
  env.resize(861);
  assert.equal(env.overlays().length, 0);
  assert.equal(env.sidebar.classList.contains('open'), false);
  assert.equal(env.sidebar.inert, false);
  assert.equal(env.collapsed(), true);
  env.resize(375);
  assert.equal(env.overlays().length, 0);
  assert.equal(env.sidebar.classList.contains('open'), false);
  assert.equal(env.menu.getAttribute('aria-expanded'), 'false');
  assert.equal(env.storage.get(storageKey), 'true');
});

test('mobile navigation including the current page and settings closes the drawer without blocking actions', async () => {
  const env = await environment({ width: 375 });
  for (const item of [env.navigation[0], env.navigation.at(-1)]) {
    env.menu.click();
    item.click();
    assert.equal(env.overlays().length, 0);
    assert.equal(env.sidebar.classList.contains('open'), false);
    assert.equal(env.actions.at(-1), item.id);
  }
  env.menu.click();
  env.escape();
  assert.equal(env.overlays().length, 0);
  assert.equal(env.document.activeElement, env.menu);
});
