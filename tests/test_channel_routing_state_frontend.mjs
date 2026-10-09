import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/pages/channels.js', import.meta.url), 'utf8');
const translations = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

const channel = {
  id: 1, name: 'openai-pro', type: 'openai', protocol: 'openai_responses',
  base_url: 'http://192.168.4.1:9090/v1', enabled: true, priority: 1, weight: 1,
  models: ['gpt-5.6-terra'], extra: {},
  total_requests: 4, success_requests: 3, failed_requests: 1,
};

// Mount the production page module with controlled timers so polling behavior is
// observable without waiting on wall-clock time.
async function mount({
  language, routingResponses, routingError = null, timers = [],
  failReset = null, confirmResult = true,
}) {
  let html = '';
  let renders = 0;
  const container = {
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; renders += 1; },
  };
  const window = {};
  vm.runInNewContext(translations, { window });
  const pending = [...routingResponses];
  const listeners = {};
  const document = {
    hidden: false,
    getElementById: (id) => (id === 'channels' ? container : null),
    querySelectorAll: () => [],
    addEventListener(name, fn) { listeners[name] = fn; },
  };
  const context = vm.createContext({
    document,
    window: {},
    Element: class {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; },
    confirm: () => confirmResult,
    setImmediate,
  });
  const requests = [];
  const toasts = [];
  const bindings = {
    api: async (url) => {
      requests.push(url);
      if (url.endsWith('/routing-state')) {
        if (routingError) throw new Error(routingError);
        const response = pending.length > 1 ? pending.shift() : pending[0] ?? { cooldowns: [] };
        if (response instanceof Error) throw response;
        return typeof response === 'function' ? response() : response;
      }
      if (url.endsWith('/cooldown/reset')) {
        if (failReset) throw new Error(failReset);
        return { channel_id: 1, released: ['gpt-5.6-terra'] };
      }
      return [channel];
    },
    t: (key) => window.ROTOR_UI_CONFIG.locales[language][key] ?? key,
    escapeHtml: (value) => String(value ?? '')
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;'),
    parseCsv: () => [], parseJson: () => ({}),
    refreshIcons() {}, toast: (...args) => toasts.push(args), badge: () => '',
    statusBadge: () => '',
    skeletonCards() {}, showInline() {}, hideInline() {},
  };
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const names = specifier.includes('api.js') ? ['api', 'copyText']
      : specifier.includes('i18n.js') ? ['t']
        : specifier.includes('channel-form.js') ? ['channelDefaults', 'rememberChannelDefaults', 'applyChannelDefaults']
          : ['escapeHtml', 'parseCsv', 'parseJson', 'badge', 'statusBadge', 'skeletonCards', 'showInline', 'hideInline', 'refreshIcons', 'toast'];
    return new vm.SyntheticModule(names, function () {
      names.forEach((name) => this.setExport(name, bindings[name] ?? (() => {})));
    }, { context });
  });
  await module.evaluate();
  return {
    namespace: module.namespace, container, requests, timers, toasts,
    get renders() { return renders; },
    setHidden(value) { document.hidden = value; return listeners.visibilitychange?.(); },
  };
}

async function render(language, cooldowns, options = {}) {
  const page = await mount({ language, routingResponses: [{ cooldowns }], ...options });
  await page.namespace.load();
  const html = page.container.innerHTML;
  page.namespace.unload();
  return html;
}

const cooldown = (overrides = {}) => ({
  model: 'gpt-5.6-terra', channel_id: 1, channel_name: 'openai-pro',
  phase: 'cooldown', remaining_seconds: 17, cooldown_seconds: 30,
  known_channel: true, ...overrides,
});

test('a coolable channel offers a one-click cooldown reset', async () => {
  const html = await render('en', [cooldown()]);
  assert.match(html, /data-channel-cooldown-reset="1"/);
  assert.match(html, /Clear cooldown/);
  assert.match(html, /btn-tiny/);
});

test('reset clears the channel via the API and refreshes badges', async () => {
  const timers = [];
  const page = await mount({
    language: 'en',
    routingResponses: [{ cooldowns: [cooldown()] }, { cooldowns: [] }],
    timers,
  });
  await page.namespace.load();
  assert.match(page.container.innerHTML, /cooldown-badge/);

  await page.namespace.onClick({
    closest: (selector) => (selector.includes('cooldown-reset')
      ? { dataset: { channelCooldownReset: '1' } }
      : null),
  });

  const reset = page.requests.findIndex((url) => url.endsWith('/cooldown/reset'));
  assert.ok(reset >= 0, page.requests.join(', '));
  assert.doesNotMatch(page.container.innerHTML, /cooldown-badge/);
  page.namespace.unload();
});

test('a rejected reset surfaces the error and keeps the badge', async () => {
  const timers = [];
  const page = await mount({
    language: 'en',
    routingResponses: [{ cooldowns: [cooldown()] }],
    timers,
    failReset: '404 No cooldown for model',
  });
  await page.namespace.load();
  await page.namespace.onClick({
    closest: (selector) => (selector.includes('cooldown-reset')
      ? { dataset: { channelCooldownReset: '1' } }
      : null),
  });
  assert.match(page.container.innerHTML, /cooldown-badge/);
  assert.ok(page.toasts.some(([message, type]) => type === 'error'
    && message.includes('No cooldown for model')));
  page.namespace.unload();
});

test('a declined confirmation sends no reset request', async () => {
  const page = await mount({
    language: 'en',
    routingResponses: [{ cooldowns: [cooldown()] }],
    confirmResult: false,
  });
  await page.namespace.load();
  await page.namespace.onClick({
    closest: (selector) => (selector.includes('cooldown-reset')
      ? { dataset: { channelCooldownReset: '1' } }
      : null),
  });
  assert.ok(!page.requests.some((url) => url.includes('/cooldown/reset')));
  assert.match(page.container.innerHTML, /cooldown-badge/);
  page.namespace.unload();
});

test('every runtime-state message exists in both locales', () => {
  const window = {};
  vm.runInNewContext(translations, { window });
  const keys = [
    'coolingDown', 'coolingDownHint', 'coolingDownBadge',
    'coolingDownUnknownChannel', 'recoveryProbeBadge', 'routingStateUnavailable',
  ];
  for (const key of keys) {
    for (const language of ['zh-CN', 'en']) {
      const value = window.ROTOR_UI_CONFIG.locales[language][key];
      assert.equal(typeof value, 'string', `${language}.${key} missing`);
      assert.ok(value.length > 0, `${language}.${key} empty`);
    }
  }
});

test('channel cards surface cooldown state with model and remaining seconds', async () => {
  const html = await render('zh-CN', [cooldown()]);
  assert.match(html, /channel-card [^"]*cooling/);
  assert.match(html, /cooldown-badge/);
  assert.match(html, /gpt-5\.6-terra/);
  assert.match(html, /17s/);
  assert.match(html, /冷却中/);
});

test('recovery probe and cooling phases use distinct badges', async () => {
  const html = await render('en', [cooldown({ model: 'glm-5.3', phase: 'probe', remaining_seconds: 0 })]);
  assert.match(html, /badge info cooldown-badge/);
  assert.match(html, /Recovery probe · glm-5\.3/);
});

test('a cooling channel renders both phases ordered by severity', async () => {
  const html = await render('en', [
    cooldown({ model: 'probe-model', phase: 'probe', remaining_seconds: 0 }),
    cooldown({ model: 'cool-model', remaining_seconds: 9 }),
  ]);
  assert.ok(html.indexOf('cool-model') < html.indexOf('probe-model'));
  assert.equal((html.match(/cooldown-badge/g) || []).length, 2);
});

test('channels without cooldown entries render no badge', async () => {
  const html = await render('en', []);
  assert.doesNotMatch(html, /cooldown-badge/);
  assert.doesNotMatch(html, /channel-card [^"]*cooling/);
  assert.doesNotMatch(html, /inline-result error/);
});

test('unknown runtime error is reported without breaking the channel grid', async () => {
  const html = await render('en', [], { routingError: 'boom' });
  assert.match(html, /Could not read runtime cooldown state/);
  assert.match(html, /boom/);
  assert.match(html, /openai-pro/);
});

test('cooldown entries for deleted channels are surfaced separately', async () => {
  const html = await render('en', [cooldown({ model: 'gone-model', channel_id: 42, channel_name: null, known_channel: false })]);
  assert.match(html, /Deleted channel #42/);
  assert.match(html, /gone-model/);
  assert.doesNotMatch(html, /channel-card [^"]*cooling/);
});

test('active cooldowns switch to slower polling once they clear', async () => {
  const timers = [];
  const page = await mount({
    language: 'en',
    routingResponses: [{ cooldowns: [cooldown()] }, { cooldowns: [] }],
    timers,
  });
  await page.namespace.load();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 5000);
  assert.match(page.container.innerHTML, /cooldown-badge/);

  // The poll replaces stale badges once the backend reports the channel healthy.
  await timers[0].fn();
  assert.doesNotMatch(page.container.innerHTML, /cooldown-badge/);
  assert.equal(timers.length, 2);
  assert.equal(timers[1].ms, 15000, 'healthy channels continue polling at a slower rate');
  page.namespace.unload();
});

test('leaving the page cancels pending polling', async () => {
  const timers = [];
  const page = await mount({ language: 'en', routingResponses: [{ cooldowns: [cooldown()] }], timers });
  await page.namespace.load();
  assert.equal(timers.length, 1);
  page.namespace.unload();
  const before = page.requests.length;
  await timers[0].fn();
  assert.equal(page.requests.length, before, 'no refetch after unload');
});

test('cooldowns are keyed by model and channel, not by channel alone', async () => {
  const timers = [];
  const page = await mount({
    language: 'en',
    routingResponses: [{ cooldowns: [
      cooldown({ model: 'alpha', remaining_seconds: 4 }),
      cooldown({ model: 'beta', remaining_seconds: 12 }),
      cooldown({ model: 'alpha', channel_id: 9, channel_name: null, known_channel: false }),
    ] }],
    timers,
  });
  await page.namespace.load();
  const html = page.container.innerHTML;
  assert.equal((html.match(/cooldown-badge/g) || []).length, 2);
  assert.match(html, /Deleted channel #9/);
  page.namespace.unload();
});

test('a routing-state failure schedules another read and recovers', async () => {
  const timers = [];
  const page = await mount({
    language: 'en', routingResponses: [new Error('down'), { cooldowns: [cooldown()] }], timers,
  });
  await page.namespace.load();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 15000);
  assert.match(page.container.innerHTML, /Could not read runtime cooldown state/);
  await timers[0].fn();
  assert.doesNotMatch(page.container.innerHTML, /Could not read runtime cooldown state/);
  assert.match(page.container.innerHTML, /cooldown-badge/);
  assert.equal(timers[1].ms, 5000);
  page.namespace.unload();
});

test('an initially healthy page discovers a later cooldown', async () => {
  const page = await mount({
    language: 'en', routingResponses: [{ cooldowns: [] }, { cooldowns: [cooldown()] }],
  });
  await page.namespace.load();
  assert.doesNotMatch(page.container.innerHTML, /cooldown-badge/);
  assert.equal(page.timers[0]?.ms, 15000);
  await page.timers[0].fn();
  assert.match(page.container.innerHTML, /cooldown-badge/);
  assert.equal(page.timers[1].ms, 5000);
  page.namespace.unload();
});

test('unchanged routing snapshots keep the existing toolbar in place', async () => {
  const page = await mount({ language: 'en', routingResponses: [{ cooldowns: [cooldown()] }] });
  await page.namespace.load();
  const before = page.renders;
  await page.timers[0].fn();
  assert.equal(page.renders, before, 'unchanged polling must not replace the search input');
  page.namespace.unload();
});

test('a polling response received after unload does not render or restart polling', async () => {
  let resolve;
  const response = new Promise((done) => { resolve = done; });
  const page = await mount({
    language: 'en', routingResponses: [{ cooldowns: [cooldown()] }, () => response],
  });
  await page.namespace.load();
  const request = page.timers[0].fn();
  page.namespace.unload();
  const before = page.renders;
  resolve({ cooldowns: [] });
  await request;
  assert.equal(page.renders, before);
  assert.equal(page.timers.length, 1);
  assert.match(page.container.innerHTML, /cooldown-badge/);
});

test('an initial routing response received after unload does not render or schedule polling', async () => {
  let resolve;
  const response = new Promise((done) => { resolve = done; });
  const page = await mount({ language: 'en', routingResponses: [() => response] });
  const loading = page.namespace.load();
  page.namespace.unload();
  const before = page.renders;
  resolve({ cooldowns: [cooldown()] });
  await loading;
  assert.equal(page.renders, before);
  assert.equal(page.timers.length, 0);
});

test('hidden pages pause polling and refresh when visible again', async () => {
  const page = await mount({
    language: 'en', routingResponses: [{ cooldowns: [cooldown()] }, { cooldowns: [] }],
  });
  await page.namespace.load();
  await page.setHidden(true);
  assert.equal(page.timers[0].cancelled, true);
  const before = page.requests.length;
  await page.timers[0].fn();
  assert.equal(page.requests.length, before);
  await page.setHidden(false);
  assert.doesNotMatch(page.container.innerHTML, /cooldown-badge/);
  assert.equal(page.timers[1].ms, 15000);
  page.namespace.unload();
  await page.setHidden(true);
  await page.setHidden(false);
  assert.equal(page.requests.length, before + 1, 'visibility events do not restart an unloaded page');
});
