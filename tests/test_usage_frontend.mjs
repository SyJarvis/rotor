import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const usageSource = await readFile(new URL('../src/rotor/frontend/pages/usage.js', import.meta.url), 'utf8');
const periodsSource = await readFile(new URL('../src/rotor/frontend/periods.js', import.meta.url), 'utf8');
const configSource = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');
const stats = { total_requests: 6, success_requests: 5, failed_requests: 1, total_tokens: 150,
  prompt_tokens: 100, completion_tokens: 50, cached_tokens: 20, cache_write_tokens: 5, cache_hit_rate: 20,
  costed_requests: 4, total_cost: null, currency: null, cost_totals_by_currency: { USD: 0.025, CNY: 1.5 }, avg_latency: 0.5 };
const timeline = [
  { bucket: '2026-10-09T01:00:00', model: 'alpha', tokens: 120, requests: 3, costed_requests: 2, total_cost: 0.025, currency: 'USD', cost_totals_by_currency: { USD: 0.025 } },
  { bucket: '2026-10-09T02:00:00', model: 'beta', tokens: 20, requests: 1, costed_requests: 0, total_cost: null, currency: null, cost_totals_by_currency: {} },
  { bucket: '2026-10-09T03:00:00', model: 'alpha', tokens: 0, requests: 1, costed_requests: 1, total_cost: 0, currency: 'USD', cost_totals_by_currency: { USD: 0 } },
  { bucket: '2026-10-09T04:00:00', model: 'beta', tokens: 10, requests: 1, costed_requests: 1, total_cost: 1.5, currency: 'CNY', cost_totals_by_currency: { CNY: 1.5 } },
];
const modelRows = [{ model: 'alpha', request_count: 4, prompt_tokens: 80, completion_tokens: 40, cached_tokens: 20, cache_hit_rate: 25 },
  { model: 'beta', request_count: 2, prompt_tokens: 20, completion_tokens: 10, cached_tokens: 0, cache_hit_rate: 0 }];

function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }

async function environment({ responses = {}, available = true, language = 'en', now = '2026-10-09T05:00:00Z', autoload = true } = {}) {
  const nodes = new Map();
  const calls = [];
  const charts = [];
  const destroyed = [];
  let html = '';
  const container = {};
  Object.defineProperty(container, 'innerHTML', {
    get: () => html,
    set(value) {
      html = value;
      nodes.clear(); nodes.set('usage', container);
      for (const match of value.matchAll(/id="([^"]+)"/g)) nodes.set(match[1], { id: match[1], listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } });
    },
  });
  nodes.set('usage', container);
  const window = {};
  vm.runInNewContext(configSource, { window });
  const t = (key) => window.ROTOR_UI_CONFIG.locales[language][key] ?? key;
  const defaults = {
    '/api/admin/settings': { display_timezone: 'Asia/Shanghai' },
    '/api/admin/channels': [{ id: 7, name: 'Lifetime channel', total_requests: 99999, success_requests: 99999 }],
    '/api/admin/logs/stats': stats,
    '/api/admin/logs/timeseries_by_model': timeline,
    '/api/admin/logs/models': modelRows,
    '/api/admin/logs/channels': [{ channel_id: 7, channel_name: 'Scoped channel', total_requests: 6, success_requests: 5, failed_requests: 1 }],
  };
  const api = async (url, options = {}) => {
    calls.push({ url, options });
    const path = url.split('?')[0];
    const value = Object.hasOwn(responses, path) ? responses[path] : defaults[path];
    if (value instanceof Error) throw value;
    return typeof value === 'function' ? value(url, options) : value;
  };
  class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return new Date(now).getTime(); } }
  const context = vm.createContext({ document: { getElementById: (id) => nodes.get(id) }, URLSearchParams, Intl, Date: FixedDate, AbortController });
  const periods = new vm.SourceTextModule(periodsSource, { context });
  await periods.link(() => {});
  await periods.evaluate();
  const helpers = {
    api, t, locale: language, escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    formatNumber: (value) => String(value || 0), formatRequestCount: (value) => String(value || 0), formatLatency: (value) => `${value} s`,
    skeletonKpis: (target) => { target.innerHTML = 'Loading'; }, refreshIcons() {}, badge: (text) => text, toast() {},
    isAvailable: () => available, destroyChart: (key) => destroyed.push(key),
    renderLine: (...args) => charts.push({ type: 'line', args }),
    renderDonut: (...args) => charts.push({ type: 'donut', args }),
    renderHBar: (...args) => charts.push({ type: 'hbar', args }),
  };
  const module = new vm.SourceTextModule(usageSource, { context });
  await module.link((specifier) => {
    if (specifier.includes('periods.js')) return periods;
    const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n.js') ? ['t', 'locale']
      : specifier.includes('charts.js') ? ['renderLine', 'renderDonut', 'renderHBar', 'destroyChart', 'isAvailable']
      : ['escapeHtml', 'formatNumber', 'formatRequestCount', 'formatLatency', 'skeletonKpis', 'refreshIcons', 'badge', 'toast'];
    return new vm.SyntheticModule(names, function () { names.forEach((name) => this.setExport(name, helpers[name])); }, { context });
  });
  await module.evaluate();
  if (autoload) await module.namespace.load();
  return { page: module.namespace, periods: periods.namespace, calls, charts, destroyed, nodes, html: () => html,
    latest: (type) => charts.filter((chart) => chart.type === type).at(-1),
    metric(value) { nodes.get('usageMetricSwitch').listeners.click({ target: { closest: () => ({ dataset: { metric: value } }) } }); },
    split(value) { nodes.get('usageByModel').listeners.change({ target: { checked: value } }); },
    currency(value) { nodes.get('usageCurrency').listeners.change({ target: { value } }); },
  };
}

test('all four regions share date/model/channel filters and channel rates use scoped logs', async () => {
  const env = await environment();
  await env.page.setRange('7d');
  await env.page.setFilter('modelFilter', 'alpha');
  await env.page.setFilter('channelFilter', '7');
  const calls = env.calls.slice(-4);
  const queries = calls.map(({ url }) => new URLSearchParams(url.split('?')[1]));
  for (const query of queries) {
    assert.equal(query.get('model'), 'alpha'); assert.equal(query.get('channel_id'), '7');
    assert.equal(query.get('start_time'), queries[0].get('start_time'));
    assert.equal(query.get('end_time'), queries[0].get('end_time'));
  }
  assert.equal(queries[1].get('bucket'), 'hour');
  assert.equal(queries[2].get('limit'), '8');
  assert.equal(env.latest('hbar').args[2][0], 'Scoped channel');
  assert.equal(env.latest('hbar').args[3][0], 5 / 6 * 100);
});

test('headline Token total uses stats without adding cache again and priced cost reports coverage per currency', async () => {
  const env = await environment();
  assert.match(env.html(), /kpi-value">150<\/div>/);
  assert.match(env.html(), /Prompt Tokens 100 · Completion Tokens 50/);
  assert.match(env.html(), /Cache hits 20 \(20\.0%\) · Cache writes 5/);
  assert.match(env.html(), /CNY 1\.50/); assert.match(env.html(), /USD 0\.025/);
  assert.match(env.html(), /Priced requests 4 \/ 6 \(66\.7%\)/);
  assert.match(env.html(), /Included|included/);
  assert.equal(env.latest('line').args[3].length, 1);
  assert.equal(env.latest('line').args[3][0].label, 'Total');
  assert.equal(env.latest('line').args[3][0].data.reduce((sum, value) => sum + value, 0), 150);
  assert.equal(env.latest('line').args[4].fill, true);
});

test('legacy unknown zero cost stays unknown, whereas calculated zero retains its currency', async () => {
  const unknown = await environment({ responses: { '/api/admin/logs/stats': { ...stats, costed_requests: 0, total_cost: 0, cost_totals_by_currency: {}, currency: null } } });
  assert.match(unknown.html(), /usage-cost-value">—/);
  assert.match(unknown.html(), /No priced cost is available/);
  const known = await environment({ responses: { '/api/admin/logs/stats': { ...stats, costed_requests: 6, total_cost: 0, cost_totals_by_currency: { USD: 0 }, currency: 'USD' } } });
  assert.match(known.html(), /USD 0\.00/);
});

test('requests and model comparison update the existing trend without network calls or stacked data', async () => {
  const env = await environment();
  const callCount = env.calls.length;
  env.metric('requests');
  assert.equal(env.latest('line').args[3][0].data.reduce((sum, value) => sum + value, 0), 6);
  env.split(true);
  assert.equal(env.latest('line').args[3].length, 2);
  assert.equal(env.latest('line').args[4].fill, false);
  assert.equal(env.calls.length, callCount);
  assert.ok(env.latest('donut')); assert.ok(env.latest('hbar'));
});

test('cost trend keeps currencies separate, unknown activity null, real zero and no-request buckets zero', async () => {
  const env = await environment();
  env.metric('cost');
  assert.equal(env.latest('line').args[4].yTitle, 'CNY');
  env.currency('USD');
  const [, , labels, datasets, options] = env.latest('line').args;
  assert.equal(options.yTitle, 'USD');
  assert.equal(datasets[0].data[labels.indexOf('09:00')], 0.025);
  assert.equal(datasets[0].data[labels.indexOf('10:00')], null);
  assert.equal(datasets[0].data[labels.indexOf('11:00')], 0);
  assert.equal(datasets[0].data[labels.indexOf('08:00')], 0);
  assert.equal(datasets[0].data[labels.indexOf('12:00')], 0, 'another currency is not added to USD');
  assert.equal(labels.at(-1), '13:00', 'future hours are not drawn as zero');
  env.split(true);
  const beta = env.latest('line').args[3].find((dataset) => dataset.label === 'beta');
  assert.equal(beta.data[labels.indexOf('10:00')], null);
});

test('stats failure shows unavailable dashes while successful trend and breakdowns remain usable', async () => {
  const env = await environment({ responses: { '/api/admin/logs/stats': new Error('<stats down>') } });
  assert.match(env.html(), /Statistics unavailable/); assert.match(env.html(), /&lt;stats down>/);
  assert.doesNotMatch(env.html(), /kpi-value">0/);
  assert.ok(env.latest('line')); assert.ok(env.latest('donut')); assert.ok(env.latest('hbar'));
});

test('partial timeline/model/channel failures are distinct from an empty successful result', async () => {
  const failed = await environment({ responses: {
    '/api/admin/logs/timeseries_by_model': new Error('Trend offline'), '/api/admin/logs/models': new Error('Models offline'), '/api/admin/logs/channels': new Error('Channels offline'),
  } });
  assert.match(failed.html(), /Trend offline/); assert.match(failed.html(), /Models offline/); assert.match(failed.html(), /Channels offline/);
  assert.doesNotMatch(failed.html(), /No data/); assert.equal(failed.charts.length, 0);
  const empty = await environment({ responses: { '/api/admin/logs/timeseries_by_model': [], '/api/admin/logs/models': [], '/api/admin/logs/channels': [] } });
  assert.match(empty.html(), /No data/); assert.doesNotMatch(empty.html(), /Unavailable/);
});

test('missing reporting timezone does not invent a timezone or request misleading aggregate data', async () => {
  const env = await environment({ responses: { '/api/admin/settings': new Error('Settings offline') } });
  assert.match(env.html(), /reporting timezone could not be loaded/);
  assert.equal(env.calls.filter(({ url }) => url.startsWith('/api/admin/logs/')).length, 0);
  assert.ok(env.nodes.has('retryUsage'));
});

test('a slower previous filter is aborted and cannot overwrite the newest totals', async () => {
  const old = deferred();
  const env = await environment({ responses: { '/api/admin/logs/stats': (url) => new URLSearchParams(url.split('?')[1]).get('model') === 'old' ? old.promise : { ...stats, total_requests: 42 } } });
  const pending = env.page.setFilter('modelFilter', 'old');
  await flush();
  const oldCall = env.calls.find(({ url }) => url.includes('/logs/stats?') && url.includes('model=old'));
  await env.page.setFilter('modelFilter', 'new');
  assert.equal(oldCall.options.signal.aborted, true);
  old.resolve({ ...stats, total_requests: 999 }); await pending;
  assert.match(env.html(), /kpi-value">42<\/div>/);
  assert.doesNotMatch(env.html(), /kpi-value">999/);
  assert.match(env.html(), /value="new" selected/);
});

test('leaving usage aborts outstanding requests and destroys charts without late rendering', async () => {
  const pendingStats = deferred();
  let pending = false;
  const env = await environment({ responses: { '/api/admin/logs/stats': () => pending ? pendingStats.promise : stats } });
  pending = true;
  const load = env.page.load(); await flush();
  const before = env.html();
  env.page.unload();
  assert.equal(env.calls.at(-1).options.signal.aborted, true);
  assert.ok(env.destroyed.includes('usage-trend-0'));
  assert.ok(env.destroyed.includes('usage-donut'));
  assert.ok(env.destroyed.includes('usage-channels'));
  pendingStats.resolve({ ...stats, total_requests: 999 }); await load;
  assert.equal(env.html(), before);
});

test('CDN fallback preserves filtered channel rates and separate cost values', async () => {
  const env = await environment({ available: false });
  assert.match(env.html(), /Chart library not loaded/);
  assert.match(env.html(), /Scoped channel/);
  assert.match(env.html(), /83\.3%/);
  assert.match(env.html(), /Per-model breakdown/);
  env.metric('cost'); env.currency('USD');
  assert.match(env.html(), /USD 0\.025/);
  assert.equal(env.charts.length, 0);
});

test('Chinese usage controls, unknown cost and errors are translated', async () => {
  const env = await environment({ language: 'zh-CN', responses: { '/api/admin/logs/stats': new Error('统计服务中断') } });
  assert.match(env.html(), /统计暂不可用/); assert.match(env.html(), /按模型拆分/); assert.match(env.html(), /已计价费用/); assert.match(env.html(), /今天/);
  assert.doesNotMatch(env.html(), />usage[A-Z_]/);
});

test('rolling day ranges retain both local boundary days and recent token totals across UTC midnight', async () => {
  const env = await environment({ now: '2026-10-09T20:00:00Z', responses: { '/api/admin/logs/timeseries_by_model': [
    { bucket: '2026-10-02T21:00:00', model: 'alpha', tokens: 5, requests: 1 },
    { bucket: '2026-10-09T19:00:00', model: 'alpha', tokens: 7, requests: 1 },
  ] } });
  for (const range of ['7d', '30d']) {
    await env.page.setRange(range);
    const [, , labels, datasets] = env.latest('line').args;
    assert.equal(labels.at(-1), '10-10');
    assert.equal(datasets[0].data.reduce((sum, value) => sum + value, 0), 12);
    if (range === '7d') assert.equal(labels[0], '10-03');
  }
});
