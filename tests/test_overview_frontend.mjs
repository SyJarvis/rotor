import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const [overviewSource, appSource, configSource, html] = await Promise.all(['pages/overview.js', 'app.js', 'config.js', 'index.html'].map((path) => readFile(new URL(`../src/rotor/frontend/${path}`, import.meta.url), 'utf8')));
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }

async function overview({ admin = false, language = 'en', autoload = true } = {}) {
  let owner = admin ? '1' : '7';
  const calls = [], copied = [], events = {}, container = { innerHTML: '' };
  const replies = new Map();
  const window = { location: { origin: 'https://rotor.test' } };
  vm.runInNewContext(configSource, { window });
  const api = async (url, options) => {
    calls.push({ url, options });
    const parsed = new URL(url, window.location.origin), path = parsed.pathname;
    if (replies.has(path)) { const value = replies.get(path); if (value instanceof Error) throw value; return typeof value === 'function' ? value(url) : value; }
    if (path.endsWith('/stats')) return { total_requests: 4, success_requests: 3, failed_requests: 1, total_tokens: 80, avg_latency: 0.2 };
    if (path.endsWith('/timeseries')) return [{ requests: 4, tokens: 80 }];
    if (path === '/api/admin/channels') return [{ id: 3, name: 'Global channel' }];
    if (path.endsWith('/channels')) return [{ channel_id: 3, channel_name: 'My used channel' }];
    if (path.endsWith('/logs')) return [{ id: 9, model: 'test-model', channel_id: 3, created_at: '2026-10-09T02:00:00Z', error_code: 'test-error' }];
    if (path === '/api/me' || path === '/api/admin/settings') return { display_timezone: 'UTC' };
    throw new Error(`Unexpected endpoint ${url}`);
  };
  const bindings = { api, locale: language, t: (key) => window.ROTOR_UI_CONFIG.locales[language][key], isAdmin: () => admin, accountId: () => owner,
    escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    formatNumber: (value) => String(value || 0), formatRequestCount: (value) => String(value || 0), formatLatency: String,
    formatTimeInTimezone: (value, zone) => `${zone}:${value}`, badge: (label) => label, refreshIcons() {},
    skeletonKpis: (node) => { node.innerHTML = 'Loading'; }, copy: (value) => copied.push(value), sparkline: () => '',
  };
  const context = vm.createContext({ window, document: { getElementById: () => container, addEventListener(name, callback) { events[name] = callback; } }, URLSearchParams, Date, AbortController });
  const page = new vm.SourceTextModule(overviewSource, { context });
  await page.link((specifier) => {
    const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n.js') ? ['locale', 't'] : specifier.includes('session.js') ? ['isAdmin', 'accountId'] : specifier.includes('charts.js') ? ['sparkline'] : ['escapeHtml', 'formatNumber', 'formatRequestCount', 'formatLatency', 'formatTimeInTimezone', 'badge', 'refreshIcons', 'skeletonKpis', 'copy'];
    return new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, bindings[name]); }, { context });
  });
  await page.evaluate(); if (autoload) await page.namespace.load();
  return { page: page.namespace, calls, copied, replies, html: () => container.innerHTML,
    changeActor(nextAdmin, id) { admin = nextAdmin; owner = id; }, endSession() { owner = ''; admin = false; events.sessionended(); },
  };
}

for (const language of ['zh-CN', 'en']) {
  test(`ordinary overview reads only personal reports and labels their scope (${language})`, async () => {
    const h = await overview({ language });
    assert.equal(h.calls.length, 7);
    assert.ok(h.calls.every(({ url }) => url === '/api/me' || url.startsWith('/api/me/logs')));
    assert.ok(h.calls.some(({ url }) => url === '/api/me/logs/channels'));
    assert.match(h.html(), /My used channel/); assert.match(h.html(), /UTC:2026-10-09/);
    assert.match(h.html(), language === 'en' ? /My request status/ : /本人调用状态/);
    assert.match(h.html(), language === 'en' ? /My requests · Last 7 days/ : /本人调用 · 最近 7 天/);
    assert.doesNotMatch(h.html(), /Global channel|System status|系统健康/);
    const endpoints = [...h.html().matchAll(/data-copy-value="([^"]+)"/g)].map((match) => match[1]);
    assert.deepEqual(endpoints, ['/v1/chat/completions', '/v1/embeddings', '/v1/responses', '/anthropic/v1/messages', '/v1/models'].map((path) => `https://rotor.test${path}`));
    h.page.onClick({ closest: (selector) => selector === '[data-copy-value]' ? { dataset: { copyValue: endpoints[0] } } : null });
    assert.deepEqual(h.copied, [endpoints[0]]);
  });
}

test('administrator overview keeps global endpoints and system labels', async () => {
  const h = await overview({ admin: true });
  assert.equal(h.calls.length, 7); assert.ok(h.calls.every(({ url }) => url.startsWith('/api/admin/')));
  assert.deepEqual(h.calls.map(({ url }) => url.split('?')[0]), ['/api/admin/logs/stats', '/api/admin/channels', '/api/admin/logs/timeseries', '/api/admin/logs/stats', '/api/admin/logs', '/api/admin/logs', '/api/admin/settings']);
  assert.match(h.html(), /System status/); assert.match(h.html(), /Global channel/); assert.doesNotMatch(h.html(), /My request status/);
});

test('personal request errors never fall back to admin or show previous global data', async () => {
  const h = await overview({ admin: true }); assert.match(h.html(), /Global channel/);
  h.changeActor(false, '8'); h.page.render(); assert.equal(h.html(), '');
  h.calls.length = 0; h.replies.set('/api/me/logs/stats', Object.assign(new Error('<Forbidden>'), { status: 403 }));
  await h.page.load();
  assert.ok(h.calls.every(({ url }) => url === '/api/me' || url.startsWith('/api/me/logs')));
  assert.match(h.html(), /&lt;Forbidden>/); assert.doesNotMatch(h.html(), /Global channel|kpi-value|System status/);
  assert.equal([...h.html().matchAll(/data-copy-value=/g)].length, 5);
});

test('late global requests and session-ended results cannot repopulate a personal overview', async () => {
  const gate = deferred(), h = await overview({ admin: true, autoload: false });
  h.replies.set('/api/admin/logs/stats', gate.promise);
  const pending = h.page.load(); const signal = h.calls[0].options.signal;
  h.page.unload(); h.changeActor(false, '8'); await h.page.load();
  const personal = h.html(); gate.resolve({ total_requests: 99999 }); await pending;
  assert.equal(signal.aborted, true); assert.equal(h.html(), personal); assert.doesNotMatch(h.html(), /99999|Global channel/);
  const other = deferred(); h.replies.set('/api/me/logs/stats', other.promise); const last = h.page.load();
  h.endSession(); assert.equal(h.html(), ''); other.resolve({ total_requests: 77777 }); await last;
  assert.equal(h.html(), '');
});

async function navigation(admin) {
  const loads = [], nodes = new Map();
  function element(id, dataset = {}) { const classes = new Set(); return { id, dataset, hidden: false, listeners: {}, textContent: '', classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }, contains(name) { return classes.has(name); } }, addEventListener(name, callback) { this.listeners[name] = callback; } }; }
  const nav = [...html.matchAll(/<button class="nav-item[^"]*"([^>]*data-tab="([^"]+)"[^>]*)>/g)].map((match) => ({ ...element(match[2], { tab: match[2] }), adminOnly: match[1].includes('data-admin-only') }));
  const panels = nav.map((item) => element(item.id));
  for (const id of ['content', 'pageTitle', '.main', '.app-shell']) nodes.set(id, element(id));
  const context = vm.createContext({ window: {}, Element: class {}, document: { getElementById: (id) => nodes.get(id), querySelector: (selector) => nodes.get(selector), querySelectorAll: (selector) => selector === '.nav-item[data-tab]' ? nav : selector === '.panel' ? panels : selector === '[data-admin-only]' ? nav.filter((item) => item.adminOnly) : [], addEventListener() {} } });
  const app = new vm.SourceTextModule(appSource, { context });
  await app.link((specifier) => {
    const page = specifier.match(/pages\/([^.?]+)[.]js/)?.[1];
    const names = page ? ['load', 'unload', 'renderHistory', 'resetSession', 'editState'] : specifier.includes('i18n') ? ['t', 'applyLocale'] : specifier.includes('settings') ? ['initSettings'] : specifier.includes('session') ? ['isAdmin'] : specifier.includes('sidebar') ? ['initSidebar', 'closeSidebar'] : specifier.includes('api.js') ? ['api', 'copyText'] : specifier.includes('ui.js') ? ['parseCsv', 'parseJson', 'refreshIcons', 'toast'] : specifier.includes('charts') ? ['refreshTheme'] : ['setChannelPresets', 'applyChannelDefaults', 'channelConnection'];
    return new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, name === 'isAdmin' ? () => admin : name === 't' ? (key) => key : name === 'load' ? () => loads.push(page) : name === 'api' ? async () => [] : name === 'editState' ? {} : () => {}); }, { context });
  });
  await app.evaluate();
  return { loads, nav, panels, async click(id) { await nav.find((item) => item.id === id).listeners.click(); } };
}

test('real navigation exposes personal overview while keeping channels, monitoring and users restricted', async () => {
  const h = await navigation(false); assert.deepEqual(h.loads, ['chat']);
  assert.equal(h.nav.find((item) => item.id === 'overview').hidden, false);
  await h.click('overview'); assert.deepEqual(h.loads, ['chat', 'overview']);
  for (const id of ['channels', 'monitoring', 'users']) {
    assert.equal(h.nav.find((item) => item.id === id).hidden, true); await h.click(id);
  }
  assert.deepEqual(h.loads, ['chat', 'overview']);
  assert.equal(h.panels.find((item) => item.id === 'overview').classList.contains('active'), true);
  const admin = await navigation(true); assert.equal(admin.loads[0], 'overview');
  for (const id of ['channels', 'monitoring', 'users']) { assert.equal(admin.nav.find((item) => item.id === id).hidden, false); await admin.click(id); }
  assert.deepEqual(admin.loads, ['overview', 'channels', 'monitoring', 'users']);
});
