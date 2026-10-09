import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/logs.js', import.meta.url), 'utf8');
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const log = (model, id = 1) => ({ id, model, channel_id: 7, total_tokens: 3, success: true, created_at: '2026-10-09T00:00:00Z' });

async function environment({ admin = true, intercept = () => undefined } = {}) {
  const nodes = new Map(), events = {}, intervals = new Map(), calls = [], destroyed = [];
  let nextTimer = 1, html = '';
  const container = {};
  Object.defineProperty(container, 'innerHTML', {
    get: () => html,
    set(value) { html = value; nodes.clear(); nodes.set('logs', container); for (const match of value.matchAll(/id="([^"]+)"/g)) nodes.set(match[1], { listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } }); },
  });
  nodes.set('logs', container);
  const result = (url) => {
    const path = url.split('?')[0], mine = path.startsWith('/api/me');
    if (path === '/api/admin/channels' || path === '/api/me/logs/channels') return [{ channel_id: 7, channel_name: mine ? 'Personal channel' : 'Global channel' }];
    if (path === '/api/admin/settings' || path === '/api/me') return { display_timezone: mine ? 'UTC' : 'Asia/Shanghai' };
    if (path.endsWith('/logs')) return [log(mine ? 'personal-model' : 'global-model')];
    if (path.endsWith('/count')) return { count: mine ? 1 : 99 };
    if (path.endsWith('/timeseries')) return [];
    if (path.endsWith('/models')) return [{ model: mine ? 'personal-model' : 'global-model' }];
    return { request_body: mine ? 'personal-detail' : 'global-detail' };
  };
  const bindings = {
    api: async (url, options) => { calls.push({ url, options }); const intercepted = intercept(url, options); return intercepted === undefined ? result(url) : await intercepted; },
    isAdmin: () => admin, t: key => key === 'columns' ? {} : key, escapeHtml: value => String(value ?? ''), badge: String, formatNumber: String, formatTimeInTimezone: String,
    skeletonRows: node => { node.innerHTML = 'LOADING'; }, refreshIcons() {}, toast() {}, renderStackedBar() {}, isAvailable: () => false, destroyChart: key => destroyed.push(key),
    TIME_RANGES: [], createTimeRange: () => ({ key: 'today', label: 'Today', bucket: 'hour', query: 'range=today', startDate: '2026-10-09', endDate: '2026-10-09' }), dateInTimezone: () => '2026-10-09', expectedRangeBuckets: () => [],
  };
  const context = vm.createContext({ document: { getElementById: id => nodes.get(id) || null, addEventListener(name, callback) { (events[name] ||= []).push(callback); } }, URLSearchParams, Date, Set, AbortController,
    setInterval(callback) { const id = nextTimer++; intervals.set(id, callback); return id; }, clearInterval: id => intervals.delete(id), setTimeout, clearTimeout });
  const module = new vm.SourceTextModule(source, { context });
  const namesFor = { api: ['api'], session: ['isAdmin'], i18n: ['t'], ui: ['escapeHtml', 'badge', 'formatNumber', 'formatTimeInTimezone', 'skeletonRows', 'refreshIcons', 'toast'], charts: ['renderStackedBar', 'isAvailable', 'destroyChart'], periods: ['TIME_RANGES', 'bucketKey', 'bucketLabel', 'createCustomTimeRange', 'createTimeRange', 'dateInTimezone', 'expectedRangeBuckets'] };
  await module.link(specifier => { const key = Object.keys(namesFor).find(name => specifier.includes(`/${name}.js`)); const names = namesFor[key]; return new vm.SyntheticModule(names, function () { for (const name of names) this.setExport(name, bindings[name] || (() => {})); }, { context }); });
  await module.evaluate();
  return { page: module.namespace, calls, destroyed, get html() { return html; }, container,
    async scope(value) { for (const callback of events.change) callback({ target: { id: 'logsReportScope', value } }); await flush(); },
    async end() { for (const callback of events.sessionended) callback(); await flush(); },
    startPoll() { nodes.get('logAutoRefresh').listeners.change({ target: { checked: true } }); },
    poll() { return [...intervals.values()][0]?.(); },
    async expand(id = 1) { await module.namespace.onClick({ closest: selector => selector === '[data-log-expand]' ? { dataset: { logExpand: String(id) } } : null }); await flush(); },
  };
}

test('late global rows/count/model data cannot replace the completed personal scope', async () => {
  const late = deferred(); let delay = false;
  const h = await environment({ intercept: url => delay && url.startsWith('/api/admin/logs?') ? late.promise : undefined });
  await h.page.load(); delay = true; const previous = h.page.load(); await flush();
  const obsolete = h.calls.findLast(call => call.url.startsWith('/api/admin/logs?'));
  await h.scope('mine'); const personalHtml = h.html;
  assert.match(personalHtml, /personal-model/); assert.doesNotMatch(personalHtml, /global-model/);
  assert.equal(obsolete.options.signal.aborted, true);
  late.resolve([log('stale-global')]); await previous;
  assert.equal(h.html, personalHtml);
});

for (const stage of ['channels', 'settings']) test(`late global ${stage} cannot poison personal metadata or launch obsolete follow-up requests`, async () => {
  const late = deferred();
  const h = await environment({ intercept: url => url === `/api/admin/${stage}` ? late.promise : undefined });
  const previous = h.page.load(); await flush();
  await h.scope('mine'); const personalHtml = h.html;
  assert.match(personalHtml, /Personal channel/); assert.match(personalHtml, /Today · UTC/);
  const callsAtSwitch = h.calls.length;
  late.resolve(stage === 'channels' ? [{ id: 7, name: 'stale-global-channel' }] : { display_timezone: 'stale-timezone' });
  await previous; assert.equal(h.html, personalHtml); assert.equal(h.calls.length, callsAtSwitch);
  await h.page.load(); assert.doesNotMatch(h.html, /stale-global-channel|stale-timezone/);
});

test('failure from an obsolete load cannot replace the active scope with an error', async () => {
  const late = deferred(); let delay = false;
  const h = await environment({ intercept: url => delay && url.startsWith('/api/admin/logs?') ? late.promise : undefined });
  await h.page.load(); delay = true; const previous = h.page.load(); await flush();
  await h.scope('mine'); const personalHtml = h.html;
  late.reject(new Error('obsolete global failure')); await previous;
  assert.equal(h.html, personalHtml);
});

test('late global poll cannot update personal rows, and further polls use personal endpoints', async () => {
  const late = deferred(); let delay = false;
  const h = await environment({ intercept: url => delay && url.startsWith('/api/admin/logs?') ? late.promise : undefined });
  await h.page.load(); h.startPoll(); delay = true; const previous = h.poll();
  await h.scope('mine'); const personalHtml = h.html;
  late.resolve([log('stale-polled-global', 2)]); await previous; assert.equal(h.html, personalHtml);
  await h.poll(); assert.ok(h.calls.at(-1).url.startsWith('/api/me/logs?'));
});

for (const fails of [false, true]) test(`late global detail ${fails ? 'error' : 'success'} cannot overwrite new-scope details or cache`, async () => {
  const late = deferred();
  const h = await environment({ intercept: url => url === '/api/admin/logs/1' ? late.promise : undefined });
  await h.page.load(); await h.expand(); await h.scope('mine'); await h.expand();
  assert.match(h.html, /personal-detail/); const personalHtml = h.html;
  if (fails) late.reject(new Error('stale global detail error')); else late.resolve({ request_body: 'stale-global-detail' });
  await flush(); assert.equal(h.html, personalHtml);
  await h.expand(); await h.expand(); assert.match(h.html, /personal-detail/); assert.doesNotMatch(h.html, /stale-global-detail|stale global detail error/);
});

for (const end of ['unload', 'sessionend']) test(`${end} invalidates pending rows, details, polls and their errors without repainting`, async () => {
  for (const kind of ['rows', 'detail', 'poll']) {
    const late = deferred(); let delay = false;
    const h = await environment({ intercept: url => delay && (kind === 'detail' ? url === '/api/admin/logs/1' : url.startsWith('/api/admin/logs?')) ? late.promise : undefined });
    await h.page.load(); delay = true;
    let previous;
    if (kind === 'rows') previous = h.page.load();
    else if (kind === 'detail') await h.expand();
    else { h.startPoll(); previous = h.poll(); }
    await flush(); const inflight = h.calls.at(-1);
    if (end === 'unload') h.page.unload(); else await h.end();
    h.container.innerHTML = 'DIFFERENT PAGE';
    late.reject(new Error(`obsolete ${kind}`)); await previous; await flush();
    assert.equal(h.html, 'DIFFERENT PAGE'); assert.equal(inflight.options.signal.aborted, true);
    assert.ok(h.destroyed.includes('logRequests'));
  }
});

test('ordinary logs use only personal routes and retain an active request failure', async () => {
  const h = await environment({ admin: false, intercept: url => url.startsWith('/api/me/logs?') ? Promise.reject(new Error('personal request failed')) : undefined });
  await h.page.load(); assert.ok(h.calls.every(({ url }) => url.startsWith('/api/me')));
  assert.match(h.html, /personal request failed/);
});
