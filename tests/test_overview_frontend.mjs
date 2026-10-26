import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const [overviewSource, appSource, configSource, html] = await Promise.all(['pages/overview.js', 'app.js', 'config.js', 'index.html'].map((path) => readFile(new URL(`../src/rotor/frontend/${path}`, import.meta.url), 'utf8')));
const origin = 'https://rotor.test:9000';
const decode = (value) => value.replaceAll('&quot;', '"').replaceAll('&amp;', '&');
function container() {
  const node = { renders: 0, markup: '', details: null, protocols: new Map(), focused: null,
    querySelector(selector) { return selector === 'details' ? this.details : this.protocols.get(selector.match(/data-overview-protocol="([^"]+)"/)?.[1]); },
    get innerHTML() { return this.markup; },
    set innerHTML(markup) {
      this.markup = markup; this.renders++; this.protocols.clear();
      this.details = markup.includes('<details') ? { open: /<details[^>]*\sopen[\s>]/.test(markup) } : null;
      for (const match of markup.matchAll(/<button[^>]+data-overview-protocol="([^"]+)"[^>]*aria-pressed="([^"]+)"[^>]*>/g)) {
        this.protocols.set(match[1], { pressed: match[2], focus: () => { this.focused = match[1]; } });
      }
    },
  };
  return node;
}
const action = (attribute, value) => ({ closest(selector) { return selector === `[${attribute}]` ? { dataset: { [attribute.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]: value } } : null; } });
const addresses = (markup) => [...markup.matchAll(/data-copy-value="([^"]+)"/g)].map((match) => decode(match[1]));
const methods = (markup) => [...markup.matchAll(/<strong>([^<]+)<\/strong><span class="overview-endpoint-method">([^<]+)<\/span>/g)].map((match) => [match[1], match[2]]);

async function overview({ admin = false, language = 'en' } = {}) {
  const node = container(), calls = [], copied = [], events = {}, dispatched = [];
  const window = { location: { origin }, ROTOR_ACTOR: { id: admin ? 1 : 7, role: admin ? 'admin' : 'user' } };
  vm.runInNewContext(configSource, { window });
  const bindings = { t: (key) => window.ROTOR_UI_CONFIG.locales[language][key],
    escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'), refreshIcons() {}, copy: (value) => copied.push(value),
    api: (...args) => { calls.push(args); throw new Error('Overview must not request an API'); },
  };
  const context = vm.createContext({ window, document: { getElementById: () => node, addEventListener(name, callback) { events[name] = callback; }, dispatchEvent(event) { dispatched.push(event); } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } }, fetch: (...args) => { calls.push(args); throw new Error('Overview must not fetch data'); },
  });
  const page = new vm.SourceTextModule(overviewSource, { context });
  await page.link((specifier) => {
    const names = specifier.includes('i18n.js') ? ['t'] : specifier.includes('api.js') ? ['api'] : ['escapeHtml', 'refreshIcons', 'copy'];
    return new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, bindings[name]); }, { context });
  });
  await page.evaluate(); page.namespace.load();
  return { page: page.namespace, node, calls, copied, dispatched, html: () => node.innerHTML, endSession: () => events.sessionended() };
}

for (const admin of [false, true]) for (const language of ['zh-CN', 'en']) {
  test(`overview is a request-free connection page (${admin ? 'admin' : 'user'}, ${language})`, async () => {
    const h = await overview({ admin, language });
    assert.match(h.html(), language === 'en' ? /Quick links/ : /快捷链接/);
    assert.match(h.html(), language === 'en' ? /Connection settings/ : /接入配置/);
    assert.deepEqual([...h.html().matchAll(/data-overview-tab="([^"]+)"/g)].map((match) => match[1]), ['tokens', 'usage']);
    assert.doesNotMatch(h.html(), /kpi-grid|kpi-value|health|failure|data-overview-open-log/);
    assert.equal(h.node.details.open, false);
    assert.equal(h.node.protocols.get('openai').pressed, 'true'); assert.equal(h.node.protocols.get('anthropic').pressed, 'false');
    assert.deepEqual(addresses(h.html()), ['/v1', '/v1/chat/completions', '/v1/responses', '/v1/embeddings', '/v1/models'].map((path) => `${origin}${path}`));
    assert.deepEqual(methods(h.html()), [['OpenAI Chat', 'POST'], ['OpenAI Responses', 'POST'], ['OpenAI Embeddings', 'POST'], ['Models', 'GET']]);
    for (const url of addresses(h.html())) h.page.onClick(action('data-copy-value', url));
    assert.deepEqual(h.copied, addresses(h.html()));
    h.page.load(); assert.equal(h.calls.length, 0);
  });
}

test('protocol switching updates addresses and pressed state, preserves expanded details and focus, and uses no network', async () => {
  const h = await overview(); h.node.details.open = true;
  assert.equal(h.page.onClick(action('data-overview-protocol', 'anthropic')), true);
  assert.deepEqual(addresses(h.html()), [`${origin}/anthropic`, `${origin}/anthropic/v1/messages`]);
  assert.deepEqual(methods(h.html()), [['Anthropic Messages', 'POST']]);
  assert.equal(h.node.protocols.get('anthropic').pressed, 'true'); assert.equal(h.node.protocols.get('openai').pressed, 'false');
  assert.equal(h.node.details.open, true); assert.equal(h.node.focused, 'anthropic');
  for (const url of addresses(h.html())) h.page.onClick(action('data-copy-value', url));
  assert.deepEqual(h.copied, [`${origin}/anthropic`, `${origin}/anthropic/v1/messages`]);
  const renders = h.node.renders; h.page.onClick(action('data-overview-protocol', 'anthropic')); assert.equal(h.node.renders, renders);
  assert.equal(h.page.onClick(action('data-overview-protocol', '__proto__')), false);
  h.page.onClick(action('data-overview-protocol', 'openai')); assert.equal(addresses(h.html())[0], `${origin}/v1`);
  h.endSession(); assert.equal(h.html(), ''); h.page.load(); assert.equal(h.node.details.open, false); assert.equal(h.node.protocols.get('openai').pressed, 'true');
  assert.equal(h.calls.length, 0);
});

async function navigation(admin) {
  const loads = [], nodes = new Map(), events = {}, copied = [];
  class Element {
    constructor(id = '', dataset = {}) { this.id = id; this.dataset = dataset; this.hidden = false; this.listeners = {}; this.textContent = ''; const classes = new Set(); this.classList = { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }, contains(name) { return classes.has(name); } }; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const nav = [...html.matchAll(/<button class="nav-item[^"]*"([^>]*data-tab="([^"]+)"[^>]*)>/g)].map((match) => Object.assign(new Element(match[2], { tab: match[2] }), { adminOnly: match[1].includes('data-admin-only') }));
  const panels = nav.map((item) => new Element(item.id));
  for (const id of ['content', 'pageTitle', '.main', '.app-shell']) nodes.set(id, new Element(id));
  nodes.set('overview', container());
  const document = { getElementById: (id) => nodes.get(id), querySelector: (selector) => nodes.get(selector), querySelectorAll: (selector) => selector === '.nav-item[data-tab]' ? nav : selector === '.panel' ? panels : selector === '[data-admin-only]' ? nav.filter((item) => item.adminOnly) : [],
    addEventListener(name, callback) { (events[name] ||= []).push(callback); }, dispatchEvent(event) { for (const callback of events[event.type] || []) callback(event); },
  };
  const context = vm.createContext({ window: { location: { origin, href: `${origin}/` } }, URL, Element, document, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } } });
  const app = new vm.SourceTextModule(appSource, { context }), realOverview = new vm.SourceTextModule(overviewSource, { context });
  const modules = new Map();
  const link = (specifier) => {
    if (specifier.includes('pages/overview.js')) return realOverview;
    if (modules.has(specifier)) return modules.get(specifier);
    const page = specifier.match(/pages\/([^.?]+)[.]js/)?.[1];
    const names = page ? ['load', 'unload', 'renderHistory', 'resetSession', 'editState'] : specifier.includes('i18n') ? ['t', 'applyLocale'] : specifier.includes('settings') ? ['initSettings'] : specifier.includes('session') ? ['isAdmin'] : specifier.includes('sidebar') ? ['initSidebar', 'closeSidebar'] : specifier.includes('api.js') ? ['api', 'copyText'] : specifier.includes('ui.js') ? ['parseCsv', 'parseJson', 'refreshIcons', 'toast', 'escapeHtml', 'copy'] : specifier.includes('charts') ? ['refreshTheme'] : ['setChannelPresets', 'applyChannelDefaults', 'channelConnection'];
    const bindings = { isAdmin: () => admin, t: (key) => key, load: () => loads.push(page), api: async () => [], editState: {}, escapeHtml: String, copy: (value) => copied.push(value), copyText: async (value) => { copied.push(value); return true; } };
    const module = new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, bindings[name] || (() => {})); }, { context }); modules.set(specifier, module); return module;
  };
  await app.link(link); await app.evaluate();
  return { loads, nav, panels, nodes, copied,
    async click(id) { await nav.find((item) => item.id === id).listeners.click(); },
    async shortcut(id) { const target = Object.assign(new Element(), action('data-overview-tab', id)); for (const callback of events.click) await callback({ target }); },
    async copy(url) { const target = Object.assign(new Element(), action('data-copy-value', url)); for (const callback of events.click) await callback({ target }); },
    dispatch(tab) { document.dispatchEvent({ type: 'overviewnavigate', detail: { tab } }); },
  };
}

for (const admin of [false, true]) test(`overview shortcuts use the existing application router (${admin ? 'admin' : 'user'})`, async () => {
  const h = await navigation(admin);
  assert.deepEqual(h.loads, admin ? [] : ['chat']);
  assert.equal(h.nav.find((item) => item.id === 'overview').hidden, false);
  await h.click('overview'); assert.match(h.nodes.get('overview').innerHTML, /data-overview-tab="tokens"/);
  await h.shortcut('tokens'); assert.equal(h.loads.at(-1), 'tokens'); assert.equal(h.panels.find((item) => item.id === 'tokens').classList.contains('active'), true);
  await h.click('overview'); await h.shortcut('usage'); assert.equal(h.loads.at(-1), 'usage');
  await h.click('overview'); await h.copy(`${origin}/v1`); assert.deepEqual(h.copied, [`${origin}/v1`]);
  const count = h.loads.length; await h.shortcut('channels'); h.dispatch('channels'); assert.equal(h.loads.length, count);
  for (const id of ['channels', 'monitoring', 'users']) {
    assert.equal(h.nav.find((item) => item.id === id).hidden, !admin); await h.click(id);
  }
  assert.equal(h.loads.length, count + (admin ? 3 : 0));
});
