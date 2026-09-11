import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/performance.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

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
    return responder ? responder(path, options) : path.endsWith('/performance')
      ? { pid: 101, window_seconds: 300, sample_limit: 2048, requests: {}, metrics: {}, store: {} }
      : { pid: 101, status: 'baseline', checked_at: null, since: null };
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
    assert.match(view.root.innerHTML, language === 'en' ? /does not yet confirm consistency/ : /尚未验证账务一致性/);
    assert.equal(view.timers.size, 0);
  });
}

test('refresh does not overlap; leaving aborts both requests and ignores late replies', async () => {
  const replies = [];
  const view = await page({ responder: () => new Promise((resolve) => replies.push(resolve)) });
  const first = view.module.load(view.root);
  view.module.load(view.root);
  view.click('refresh');
  assert.equal(view.calls.length, 2);
  view.module.unload();
  assert.ok(view.calls.every(({ options }) => options.signal.aborted));
  const previous = view.root.innerHTML;
  replies.forEach((resolve) => resolve({ status: 'ok' }));
  await first;
  assert.equal(view.root.innerHTML, previous);
  assert.equal(view.timers.size, 0);
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

test('reconciliation reset is POST; different worker identities are disclosed', async () => {
  const view = await page({ language: 'en', responder: (path) => path.endsWith('/performance')
    ? { pid: 11, requests: { chat: { total: 4, success: 3, failed: 1, cancelled: 0 } } }
    : { pid: 22, status: 'mismatch', reason: '<script>bad</script>', requests: { ledger: 4, token: 3 } } });
  await view.module.load(view.root);
  assert.match(view.root.innerHTML, /75%/);
  assert.match(view.root.innerHTML, /different processes/);
  assert.match(view.root.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(view.root.innerHTML, /<script>/);
  view.click('reset');
  await view.flush();
  assert.equal(view.calls.at(-1).path, '/api/admin/monitoring/reconciliation/reset');
  assert.equal(view.calls.at(-1).options.method, 'POST');
});

test('a failed refresh clears old healthy status', async () => {
  let fail = false;
  const view = await page({ language: 'en', responder: (path) => {
    if (fail) throw new Error('offline');
    return path.endsWith('/performance') ? {} : { status: 'ok' };
  } });
  await view.module.load(view.root);
  assert.match(view.root.innerHTML, /Deltas match/);
  fail = true;
  await view.module.load(view.root);
  assert.match(view.root.innerHTML, /Check failed/);
  assert.match(view.root.innerHTML, /offline/);
  assert.doesNotMatch(view.root.innerHTML, /Deltas match/);
});
