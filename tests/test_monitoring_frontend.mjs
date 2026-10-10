import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/performance.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');
const endpoint = '/api/admin/monitoring/performance';
const snapshot = {
  pid: 101, started_at: '2026-10-10T01:02:03Z', generated_at: '2026-10-10T01:03:04Z',
  window_seconds: 300, sample_limit: 2048,
  requests: { chat: { total: 4, success: 3, failed: 1, cancelled: 0,
    latency_ms: { p95: 321, sample_count: 4 }, ttft_ms: { p95: 87, sample_count: 3 } },
    responses: { total: 2, success: 1, failed: 0, cancelled: 1 } },
  metrics: { 'db.acquire_ms': { p95: 12.5, sample_count: 8, count: 15 },
    'db.sql_ms': { p95: 24, sample_count: 7, count: 14 },
    'db.commit_ms': { p95: 31, sample_count: 6, count: 13 },
    'db.hold_ms': { p95: 42, sample_count: 5, count: 12 },
    'store.batch_size': { p95: 9, sample_count: 4, count: 11 },
    'store.batch_ms': { p95: 18, sample_count: 3, count: 10 },
    'store.queue_wait_ms': { p95: 22, sample_count: 2, count: 9 },
    'store.drain_ms': { p95: 23, sample_count: 1, count: 8 } },
  store: { enabled: true, worker_running: true, queue_size: 5, queue_maxsize: 100,
    dropped_queue_full: 2, dropped_retry_exhausted: 1 },
};

function assertReadOnlyPerformance(calls) {
  assert.ok(calls.length > 0);
  for (const { path, options } of calls) {
    assert.equal(path, endpoint);
    assert.equal(options.method ?? 'GET', 'GET');
    assert.ok(options.signal instanceof AbortSignal);
  }
}

async function page({ language = 'zh-CN', responder } = {}) {
  const calls = [];
  const timers = new Map();
  const documentListeners = {};
  const windowListeners = {};
  const rootListeners = {};
  const root = { innerHTML: '', addEventListener: (name, fn) => { rootListeners[name] = fn; } };
  const document = { hidden: false, addEventListener: (name, fn) => { documentListeners[name] = fn; } };
  const window = { addEventListener: (name, fn) => { windowListeners[name] = fn; } };
  vm.runInNewContext(translations, { window });
  let timerId = 0;
  const context = vm.createContext({ document, window, AbortController,
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  const api = async (path, options) => {
    calls.push({ path, options });
    return responder ? responder(path, options)
      : { pid: 101, window_seconds: 300, sample_limit: 2048, requests: {}, metrics: {}, store: {} };
  };
  const escapeHtml = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const bindings = { api, t: (key) => window.ROTOR_UI_CONFIG.locales[language][key] ?? key,
    escapeHtml, refreshIcons: () => {} };
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n.js') ? ['t'] : ['escapeHtml', 'refreshIcons'];
    return new vm.SyntheticModule(names, function () { names.forEach((name) => this.setExport(name, bindings[name])); }, { context });
  });
  await module.evaluate();
  return { module: module.namespace, root, calls, timers, document,
    changeAuto: (checked) => rootListeners.change({ target: { matches: () => true, checked } }),
    click: (action) => rootListeners.click({ target: { closest: () => ({ dataset: { performanceAction: action } }) } }),
    visibility: () => documentListeners.visibilitychange(),
    unauthorized: () => windowListeners.rotorauthrequired(),
    flush: async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); },
  };
}

for (const language of ['zh-CN', 'en']) {
  test(`empty samples remain dashes and labels exist (${language})`, async () => {
    const view = await page({ language });
    await view.module.load(view.root);
    assert.doesNotMatch(view.root.innerHTML, /0 ms|100%|undefined|NaN|perf[A-Z]/);
    assert.match(view.root.innerHTML, /300 s \/ 2,?048/);
    assert.equal((view.root.innerHTML.match(/<article>/g) ?? []).length, 3);
    assert.equal((view.root.innerHTML.match(/<section class="performance-section">/g) ?? []).length, 3);
    assert.deepEqual([...view.root.innerHTML.matchAll(/data-performance-action="([^"]+)"/g)].map((match) => match[1]), ['refresh']);
    assert.match(view.root.innerHTML, language === 'en' ? /Database/ : /数据库/);
    assert.match(view.root.innerHTML, language === 'en' ? /Archive/ : /归档/);
    assertReadOnlyPerformance(view.calls);
    assert.equal(view.timers.size, 0);
  });

  test(`performance values and process metadata remain visible (${language})`, async () => {
    const view = await page({ language, responder: () => snapshot });
    await view.module.load(view.root);
    assert.match(view.root.innerHTML, /<strong>6<\/strong>/);
    assert.match(view.root.innerHTML, /12\.5 ms/);
    assert.match(view.root.innerHTML, /<strong>5 <small>\/ 100<\/small>/);
    assert.match(view.root.innerHTML, /75%/);
    assert.match(view.root.innerHTML, /50%/);
    assert.match(view.root.innerHTML, /321 ms <small>n=4/);
    assert.match(view.root.innerHTML, /87 ms <small>n=3/);
    for (const value of [24, 31, 42, 18, 22, 23]) assert.match(view.root.innerHTML, new RegExp(`${value} ms`));
    assert.match(view.root.innerHTML, /<td>9<\/td>\s*<td>4<\/td>\s*<td>11<\/td>/);
    assert.match(view.root.innerHTML, /performance-status ok/);
    assert.match(view.root.innerHTML, language === 'en' ? /Running/ : /运行中/);
    assert.match(view.root.innerHTML, /PID: 101/);
    assert.match(view.root.innerHTML, /300 s \/ 2,?048/);
    assert.match(view.root.innerHTML, new RegExp(new Date(snapshot.started_at).toLocaleString().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.match(view.root.innerHTML, new RegExp(new Date(snapshot.generated_at).toLocaleString().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.doesNotMatch(view.root.innerHTML, /undefined|NaN|perf[A-Z]/);
    assertReadOnlyPerformance(view.calls);
  });

  test(`failed refresh clears old metrics, escapes errors, and can recover (${language})`, async () => {
    let fail = false;
    const view = await page({ language, responder: () => {
      if (fail) throw new Error('<script>offline</script>');
      return snapshot;
    } });
    await view.module.load(view.root);
    assert.match(view.root.innerHTML, /321 ms/);
    fail = true;
    await view.module.load(view.root);
    assert.match(view.root.innerHTML, /role="alert"/);
    assert.match(view.root.innerHTML, /&lt;script&gt;offline&lt;\/script&gt;/);
    assert.doesNotMatch(view.root.innerHTML, /<script>|321 ms|75%|PID: 101/);
    assert.match(view.root.innerHTML, language === 'en' ? /Could not load performance data/ : /性能数据加载失败/);
    fail = false;
    await view.module.load(view.root);
    assert.match(view.root.innerHTML, /321 ms/);
    assert.doesNotMatch(view.root.innerHTML, /role="alert"|offline/);
    assertReadOnlyPerformance(view.calls);
  });
}

test('refresh does not overlap; leaving aborts its request and ignores late replies', async () => {
  const replies = [];
  const view = await page({ responder: () => new Promise((resolve) => replies.push(resolve)) });
  const first = view.module.load(view.root);
  view.module.load(view.root);
  view.click('refresh');
  assert.equal(view.calls.length, 1);
  view.module.unload();
  assert.ok(view.calls.every(({ options }) => options.signal.aborted));
  const previous = view.root.innerHTML;
  replies.forEach((resolve) => resolve(snapshot));
  await first;
  assert.equal(view.root.innerHTML, previous);
  assert.equal(view.timers.size, 0);
  assertReadOnlyPerformance(view.calls);
});

test('auto refresh pauses hidden, resumes visible, and stops after 401 or unload', async () => {
  const view = await page();
  await view.module.load(view.root);
  view.changeAuto(true);
  assert.equal(view.timers.size, 1);
  assert.equal([...view.timers.values()][0].ms, 10_000);
  view.changeAuto(true);
  assert.equal(view.timers.size, 1);
  view.document.hidden = true;
  view.visibility();
  assert.equal(view.timers.size, 0);
  view.document.hidden = false;
  view.visibility();
  assert.equal(view.timers.size, 1);
  view.unauthorized();
  assert.equal(view.timers.size, 0);
  view.visibility();
  assert.equal(view.timers.size, 0);
  const oldCalls = view.calls.length;
  view.click('refresh');
  assert.equal(view.calls.length, oldCalls);
});

test('initial load, manual refresh, and automatic refresh only read performance', async () => {
  const view = await page();
  await view.module.load(view.root);
  assert.equal(view.calls.length, 1);
  view.click('refresh');
  await view.flush();
  assert.equal(view.calls.length, 2);
  view.changeAuto(true);
  const scheduled = [...view.timers.values()][0];
  assert.equal(scheduled.ms, 10_000);
  await scheduled.fn();
  assert.equal(view.calls.length, 3);
  assert.equal(view.timers.size, 1);
  view.click('reset');
  await view.flush();
  assert.equal(view.calls.length, 3);
  assertReadOnlyPerformance(view.calls);
  view.module.unload();
  assert.equal(view.timers.size, 0);
});

test('hiding aborts in-flight data; showing loads fresh data and ignores the old reply', async () => {
  const replies = [];
  const view = await page({ responder: () => new Promise((resolve) => replies.push(resolve)) });
  const first = view.module.load(view.root);
  view.document.hidden = true;
  view.visibility();
  assert.equal(view.calls[0].options.signal.aborted, true);
  view.click('refresh');
  assert.equal(view.calls.length, 1);
  view.document.hidden = false;
  view.visibility();
  assert.equal(view.calls.length, 2);
  replies[1](snapshot);
  await view.flush();
  const fresh = view.root.innerHTML;
  assert.match(fresh, /PID: 101/);
  replies[0]({ pid: 999, requests: {} });
  await first;
  assert.equal(view.root.innerHTML, fresh);
  assertReadOnlyPerformance(view.calls);
});

test('authorization loss aborts pending performance and blocks a late reply', async () => {
  const replies = [];
  const view = await page({ responder: () => new Promise((resolve) => replies.push(resolve)) });
  const pending = view.module.load(view.root);
  view.unauthorized();
  assert.equal(view.calls[0].options.signal.aborted, true);
  const previous = view.root.innerHTML;
  replies.forEach((resolve) => resolve(snapshot));
  await pending;
  assert.equal(view.root.innerHTML, previous);
  view.click('refresh');
  assert.equal(view.calls.length, 1);
  assert.equal(view.timers.size, 0);
});
