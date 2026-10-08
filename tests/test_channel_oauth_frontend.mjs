import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const root = new URL('../src/rotor/frontend/', import.meta.url);
const [helperSource, channelsSource, appSource, translations, html] = await Promise.all([
  'channel-form.js', 'pages/channels.js', 'app.js', 'config.js', 'index.html',
].map((path) => readFile(new URL(path, root), 'utf8')));
const presets = [
  { id: 'openai', label: 'OpenAI', protocol: 'openai', base_url: 'https://api.openai.com/v1', auth_type: 'bearer' },
  { id: 'openai_codex', label: 'ChatGPT OAuth', protocol: 'openai_responses', base_url: 'https://chatgpt.com/backend-api/codex', auth_type: 'bearer' },
];
const session = (overrides = {}) => ({
  session_id: 'one-time-session', status: 'pending',
  authorization_url: 'https://auth.openai.com/oauth/authorize?state=mock-state&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&code_challenge=mock-challenge',
  redirect_uri: 'http://localhost:1455/auth/callback',
  expires_at: 2000, ...overrides,
});
const callback = (code = 'mock-code', state = 'mock-state') => `http://localhost:1455/auth/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
const savedChannel = (overrides = {}) => ({
  id: 7, name: 'saved', auth_type: 'oauth', auth_status: 'connected',
  type: 'openai_codex', protocol: 'openai_responses',
  base_url: 'https://chatgpt.com/backend-api/codex', models: ['gpt-5'],
  extra: { request_path: '/responses', auth_type: 'bearer' }, ...overrides,
});

async function environment({ language = 'en', start = session(), parses = [], completes = [], probes = [], confirmResult = true, failSave = 0, onSave = null, listed = [], check = {} } = {}) {
  const nodes = new Map();
  const timers = [];
  const calls = [];
  const toasts = [];
  const confirmations = [];
  const documentListeners = {};
  let now = 1000;
  let detail = savedChannel();
  function element(id) {
    const classes = new Set(['hidden']);
    const listeners = {};
    let value = '';
    return {
      id, listeners, checked: false, disabled: false, required: false, innerHTML: '', textContent: '',
      get value() { return value; }, set value(next) { value = String(next); },
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name),
        toggle(name, force) { const add = force ?? !classes.has(name); add ? classes.add(name) : classes.delete(name); },
      },
      addEventListener: (event, callback) => { listeners[event] = callback; },
      querySelectorAll: () => [], setAttribute() {}, removeAttribute(name) { delete this[name]; },
    };
  }
  const getNode = (id) => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const form = getNode('channelForm');
  form.elements = Object.fromEntries([
    'name', 'credential_type', 'oauth_provider', 'type', 'protocol', 'base_url', 'key',
    'models_path', 'request_path', 'auth_type', 'priority', 'weight', 'models', 'model_mapping', 'extra', 'enabled',
  ].map((name) => [name, element(name)]));
  form.reset = () => {
    for (const field of Object.values(form.elements)) { field.value = ''; field.checked = false; }
    Object.assign(form.elements.type, { value: 'openai' });
    form.elements.protocol.value = 'openai';
    form.elements.credential_type.value = 'api_key';
  };
  form.reset();
  const save = getNode('saveChannelBtn');
  save.classList.remove('hidden');
  form.querySelector = () => save;
  const window = {};
  vm.runInNewContext(translations, { window });
  const locale = window.ROTOR_UI_CONFIG.locales[language];
  const context = vm.createContext({
    document: {
      getElementById: getNode, querySelectorAll: () => [], querySelector: () => null,
      addEventListener: (event, fn) => { (documentListeners[event] ||= []).push(fn); },
    },
    window: {}, Element: class {}, URL,
    confirm: (message) => { confirmations.push(message); return confirmResult; },
    Date: class extends Date { static now() { return now * 1000; } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms, cancelled: false }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; },
    FormData: class {
      constructor(target) { this.target = target; }
      get(name) {
        const field = this.target.elements[name];
        if (!field || field.disabled) return null;
        return name === 'enabled' ? field.checked ? 'on' : null : field.value;
      }
    },
  });
  const pendingParses = [...parses];
  const pendingCompletes = [...completes];
  const pendingProbes = [...probes];
  const bindings = {
    api: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, method: options.method || 'GET', body });
      if (url.endsWith('/presets')) return presets;
      if (url === '/api/admin/channels/oauth/sessions') {
        if (start instanceof Error) throw start;
        return typeof start === 'function' ? start() : start;
      }
      if (url.endsWith('/parse')) {
        const parsed = new URL(body.callback_url);
        const value = pendingParses.shift() ?? {
          code: parsed.searchParams.get('code'), state: parsed.searchParams.get('state'),
          redirect_uri: `${parsed.origin}${parsed.pathname}`,
        };
        if (value instanceof Error) throw value;
        return typeof value === 'function' ? value() : value;
      }
      if (url.endsWith('/complete')) {
        const value = pendingCompletes.shift() ?? session({ status: 'authorized', access_token: 'forbidden-secret' });
        if (value instanceof Error) throw value;
        return typeof value === 'function' ? value() : value;
      }
      if (url.endsWith('/probe-models')) {
        const value = pendingProbes.shift() ?? { models: ['catalog-a', 'catalog-b'], latency_ms: 3, raw_count: 2 };
        if (value instanceof Error) throw value;
        return typeof value === 'function' ? value() : value;
      }
      if (options.method === 'DELETE') return session({ status: 'cancelled' });
      if (url.endsWith('/test')) return { ok: true, latency_ms: 1, check_type: 'oauth', ...check };
      if (url.endsWith('/routing-state')) return { cooldowns: [] };
      if (options.method === 'POST' || options.method === 'PUT') {
        if (failSave-- > 0) throw new Error('Save temporarily failed');
        if (onSave) return onSave();
        return detail;
      }
      if (/\/channels\/\d+$/.test(url)) return detail;
      if (url === '/api/admin/channels') return listed;
      return {};
    },
    t: (key) => locale[key] ?? key,
    parseJson: (text, fallback) => text?.trim() ? JSON.parse(text) : fallback,
    parseCsv: (text) => (text || '').split(',').map((part) => part.trim()).filter(Boolean),
    escapeHtml: (text) => String(text ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    badge: (text) => `<span>${text}</span>`, statusBadge: () => '', toast: (...args) => toasts.push(args),
  };
  const helper = new vm.SourceTextModule(helperSource, { context });
  const channels = new vm.SourceTextModule(channelsSource, { context });
  const synthetic = (names) => new vm.SyntheticModule(names, function () {
    names.forEach((name) => this.setExport(name, bindings[name] ?? (() => {})));
  }, { context });
  const link = (specifier) => {
    if (specifier.includes('channel-form.js')) return helper;
    if (specifier.includes('pages/channels.js')) return channels;
    if (specifier.includes('api.js')) return synthetic(['api', 'copyText']);
    if (specifier.includes('i18n.js')) return synthetic(['t', 'applyLocale', 'toggleLocale']);
    if (specifier.includes('ui.js')) return synthetic(['parseCsv', 'parseJson', 'refreshIcons', 'toast', 'escapeHtml', 'badge', 'statusBadge', 'skeletonCards', 'showInline', 'hideInline']);
    if (specifier.includes('theme.js')) return synthetic(['toggleTheme']);
    if (specifier.includes('charts.js')) return synthetic(['refreshTheme']);
    return synthetic(['load', 'unload', 'renderHistory']);
  };
  await helper.link(link);
  await channels.link(link);
  const app = new vm.SourceTextModule(appSource, { context });
  await app.link(link);
  await app.evaluate();
  await Promise.resolve();
  return {
    form, nodes, timers, calls, toasts, confirmations, locale, page: channels.namespace, getNode,
    setNow: (next) => { now = next; },
    createOAuth() {
      channels.namespace.openCreate();
      form.elements.credential_type.value = 'oauth';
      form.elements.credential_type.listeners.change();
      form.elements.name.value = 'chatgpt'; form.elements.models.value = 'gpt-5';
    },
    edit: async (channel = savedChannel()) => { detail = channel; await channels.namespace.openEdit(channel); },
    login: () => getNode('channelOAuthLogin').listeners.click(),
    paste: (value = callback()) => { getNode('channelOAuthCallback').value = value; getNode('channelOAuthCallback').listeners.input(); },
    parse: () => getNode('channelOAuthParse').listeners.click(),
    complete: () => getNode('channelOAuthComplete').listeners.click(),
    probe: () => getNode('probeModels').listeners.click({ currentTarget: getNode('probeModels') }),
    editModels: (value) => { form.elements.models.value = value; form.elements.models.listeners.input(); },
    menuProbe: () => channels.namespace.onClick({ dataset: {}, closest: (selector) => selector === '[data-channel-probe]' ? { dataset: { channelProbe: '7' } } : null }),
    submit: () => form.listeners.submit({ currentTarget: form, preventDefault() {} }),
    dispatch: async (name, event) => { for (const fn of documentListeners[name] || []) await fn(event); },
    backdrop: async () => {
      const target = Object.assign(new context.Element(), {
        closest: (selector) => selector === '[data-close-modal]' ? { closest: () => getNode('channelModal') } : null,
      });
      for (const fn of documentListeners.click || []) await fn({ target });
    },
    saves: () => calls.filter((entry) => /^\/api\/admin\/channels(?:\/\d+)?$/.test(entry.url) && ['POST', 'PUT'].includes(entry.method)),
    deletes: () => calls.filter((entry) => entry.method === 'DELETE'),
    probes: () => calls.filter((entry) => entry.url.endsWith('/probe-models')),
  };
}

test('API key creation remains the default; OAuth fixes the connection and gates probing on authorization', async () => {
  const env = await environment();
  env.page.openCreate();
  assert.equal(env.form.elements.credential_type.value, 'api_key');
  assert.equal(env.form.elements.key.required, true);
  assert.equal(env.form.elements.key.disabled, false);
  env.form.elements.name.value = 'api'; env.form.elements.key.value = 'api-secret';
  await env.submit();
  assert.equal(env.saves()[0].body.auth_type, 'api_key');
  assert.equal(env.saves()[0].body.key, 'api-secret');
  env.createOAuth();
  for (const [name, value] of [['type', 'openai_codex'], ['protocol', 'openai_responses'], ['base_url', 'https://chatgpt.com/backend-api/codex']]) {
    assert.equal(env.form.elements[name].value, value);
    assert.equal(env.form.elements[name].disabled, true);
  }
  assert.equal(env.form.elements.key.required, false);
  assert.equal(env.form.elements.key.disabled, true);
  assert.equal(env.getNode('channelKeyField').classList.contains('hidden'), true);
  assert.equal(env.getNode('channelOAuthPanel').classList.contains('hidden'), false);
  assert.equal(env.getNode('probeModels').classList.contains('hidden'), false);
  assert.equal(env.getNode('probeModels').disabled, true);
  const before = env.calls.length;
  await env.getNode('probeModels').listeners.click({ currentTarget: env.getNode('probeModels') });
  assert.equal(env.calls.length, before);
});

test('authorization requires generate, paste, parse, explicit complete, then separate save', async () => {
  const env = await environment();
  env.createOAuth();
  assert.equal(env.getNode('channelOAuthComplete').disabled, true);
  await env.login();
  assert.deepEqual(env.calls.find((entry) => entry.url.endsWith('/oauth/sessions')).body, {});
  assert.equal(new URL(env.getNode('channelOAuthUrl').href).pathname, '/oauth/authorize');
  assert.equal(env.timers.length, 0, 'there is no OAuth polling timer');
  await env.complete();
  assert.equal(env.calls.filter((entry) => entry.url.endsWith('/complete')).length, 0);
  env.paste();
  await env.parse();
  assert.equal(env.getNode('channelOAuthCode').textContent, 'mock-code');
  assert.equal(env.getNode('channelOAuthState').textContent, 'mock-state');
  assert.equal(env.getNode('channelOAuthRedirect').textContent, 'http://localhost:1455/auth/callback');
  assert.equal(env.getNode('channelOAuthComplete').disabled, false);
  assert.equal(env.calls.filter((entry) => entry.url.endsWith('/complete')).length, 0, 'parsing does not exchange credentials');
  assert.equal(env.saves().length, 0);
  await env.complete();
  assert.match(env.getNode('channelOAuthStatus').textContent, /Authorization complete/);
  assert.equal(env.saves().length, 0, 'completion does not save the channel');
  await env.submit();
  const body = env.saves()[0].body;
  assert.equal(body.auth_type, 'oauth');
  assert.equal(body.oauth_session_id, 'one-time-session');
  assert.equal(body.type, 'openai_codex');
  assert.equal(body.extra.auth_type, 'bearer');
  assert.ok(!('key' in body));
  assert.doesNotMatch(JSON.stringify(body), /forbidden-secret|access_token|refresh_token|code_verifier|callback_url|mock-code|mock-state/);
  assert.deepEqual(env.calls.filter((entry) => entry.url.includes('/oauth/')).map((entry) => entry.url), [
    '/api/admin/channels/oauth/sessions', '/api/admin/channels/oauth/sessions/one-time-session/parse',
    '/api/admin/channels/oauth/sessions/one-time-session/complete',
  ]);
  assert.equal(env.deletes().length, 0, 'saving must not cancel a consumed session');
  assert.equal(env.form.elements.credential_type.value, 'api_key');
});

test('new OAuth channels require authorization and a failed save leaves authorization available for retry', async () => {
  const env = await environment({ failSave: 1 });
  env.createOAuth();
  await env.submit();
  assert.equal(env.saves().length, 0);
  assert.ok(env.toasts.some(([text]) => text === env.locale.oauthLoginRequired));
  await env.login(); env.paste(); await env.parse(); await env.complete();
  await env.submit();
  assert.equal(env.saves().length, 1);
  assert.equal(env.deletes().length, 0);
  assert.equal(env.form.elements.credential_type.value, 'oauth');
  await env.submit();
  assert.equal(env.saves().length, 2);
  assert.deepEqual(env.saves()[1].body, env.saves()[0].body);
});

test('editing OAuth can retain its credential or bind a replacement login to the channel', async () => {
  const env = await environment();
  await env.edit();
  assert.equal(env.form.elements.credential_type.value, 'oauth');
  assert.match(env.getNode('channelOAuthStatus').textContent, /Connected to ChatGPT/);
  await env.submit();
  assert.equal(env.saves()[0].url, '/api/admin/channels/7');
  assert.ok(!('oauth_session_id' in env.saves()[0].body));
  assert.ok(!('key' in env.saves()[0].body));
  await env.edit(); await env.login();
  assert.deepEqual(env.calls.find((entry) => entry.url.endsWith('/oauth/sessions')).body, { channel_id: 7 });
  env.paste(); await env.parse(); await env.complete(); await env.submit();
  assert.equal(env.saves()[1].body.oauth_session_id, 'one-time-session');
});

test('switching a saved OAuth channel to API key requires a replacement key', async () => {
  const env = await environment();
  await env.edit();
  env.form.elements.credential_type.value = 'api_key';
  env.form.elements.credential_type.listeners.change();
  assert.equal(env.form.elements.type.value, 'openai');
  assert.equal(env.form.elements.key.required, true);
  await env.submit();
  assert.equal(env.saves().length, 0);
  env.form.elements.key.value = 'replacement-key';
  await env.submit();
  assert.equal(env.saves()[0].body.key, 'replacement-key');
  assert.equal(env.saves()[0].body.auth_type, 'api_key');
});

test('invalid callbacks disable completion and changing input discards the previous parse', async () => {
  const env = await environment({ parses: [new Error('Invalid callback secret-token')] });
  env.createOAuth(); await env.login(); env.paste(); await env.parse();
  assert.equal(env.getNode('channelOAuthComplete').disabled, true);
  assert.equal(env.getNode('channelOAuthPreview').classList.contains('hidden'), true);
  assert.equal(env.getNode('channelOAuthStatus').textContent, env.locale.oauthCallbackInvalid);
  assert.doesNotMatch(env.getNode('channelOAuthStatus').textContent, /secret-token/);
  await env.parse();
  assert.equal(env.getNode('channelOAuthComplete').disabled, false);
  env.paste(callback('edited-code'));
  assert.equal(env.getNode('channelOAuthCode').textContent, '');
  assert.equal(env.getNode('channelOAuthComplete').disabled, true);
  await env.complete();
  assert.equal(env.calls.filter((entry) => entry.url.endsWith('/complete')).length, 0);
  await env.parse();
  assert.equal(env.getNode('channelOAuthCode').textContent, 'edited-code');
  assert.equal(env.getNode('channelOAuthComplete').disabled, false);
});

test('mismatched parse state or redirect is rejected, and code is displayed as plain text', async () => {
  for (const values of [
    { code: 'mock-code', state: 'other-state', redirect_uri: 'http://localhost:1455/auth/callback' },
    { code: 'mock-code', state: 'mock-state', redirect_uri: 'http://localhost:8010/auth/callback' },
  ]) {
    const env = await environment({ parses: [values] });
    env.createOAuth(); await env.login(); env.paste(); await env.parse();
    assert.equal(env.getNode('channelOAuthComplete').disabled, true);
    assert.equal(env.getNode('channelOAuthPreview').classList.contains('hidden'), true);
  }
  const env = await environment();
  env.createOAuth(); await env.login(); env.paste(callback('<img src=x onerror=alert(1)>')); await env.parse();
  assert.equal(env.getNode('channelOAuthCode').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(env.getNode('channelOAuthCode').innerHTML, '');
});

test('switching authentication cancels the session and clears callback values', async () => {
  const env = await environment();
  env.createOAuth(); await env.login(); env.paste(); await env.parse();
  env.form.elements.credential_type.value = 'api_key'; env.form.elements.credential_type.listeners.change();
  assert.equal(env.deletes().length, 1);
  assert.equal(env.form.elements.key.required, true);
  assert.equal(env.getNode('channelOAuthCallback').value, '');
  assert.equal(env.getNode('channelOAuthCode').textContent, '');
  assert.equal(env.getNode('channelOAuthComplete').disabled, true);
});

test('a start response arriving after close is cancelled without restoring its link', async () => {
  let resolveStart;
  const env = await environment({ start: () => new Promise((resolve) => { resolveStart = resolve; }) });
  env.createOAuth();
  const loggingIn = env.login();
  env.page.closeForm();
  resolveStart(session()); await loggingIn;
  assert.equal(env.deletes().length, 1);
  assert.equal(env.getNode('channelOAuthUrl').textContent, '');
  assert.equal(env.timers.length, 0);
});

test('Escape, backdrop, a new form, and unloading all cancel the session and clear parsed values', async () => {
  const env = await environment();
  for (const close of [
    () => env.dispatch('keydown', { key: 'Escape' }),
    () => env.getNode('closeChannelModal').listeners.click(),
    () => env.backdrop(),
    () => env.page.openCreate(),
    () => env.page.unload(),
  ]) {
    env.createOAuth(); await env.login(); env.paste(); await env.parse();
    const before = env.deletes().length;
    await close();
    assert.equal(env.deletes().length, before + 1);
    assert.equal(env.getNode('channelOAuthCode').textContent, '');
    assert.equal(env.getNode('channelOAuthComplete').disabled, true);
  }
});

test('completion failures and exchanging status remain safe and disable repeat completion', async () => {
  for (const response of [session({ status: 'failed', error: 'secret-token' }), session({ status: 'expired' }), session({ status: 'cancelled' }), session({ status: 'exchanging' })]) {
    const env = await environment({ completes: [response] });
    env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
    assert.equal(env.getNode('channelOAuthComplete').disabled, true);
    assert.doesNotMatch(env.getNode('channelOAuthStatus').textContent, /secret-token/);
    assert.throws(() => env.page.oauthSaveFields(env.form), /Parse the callback URL/);
    await env.complete();
    assert.equal(env.calls.filter((entry) => entry.url.endsWith('/complete')).length, 1);
  }
  let starts = 0;
  const retry = await environment({ start: () => session({ session_id: `session-${++starts}` }), completes: [new Error('503 secret-token')] });
  retry.createOAuth(); await retry.login(); retry.paste(); await retry.parse(); await retry.complete();
  assert.equal(retry.getNode('channelOAuthStatus').textContent, retry.locale.oauthCompleteFailed);
  assert.equal(retry.getNode('channelOAuthComplete').disabled, true);
  await retry.complete();
  assert.equal(retry.calls.filter((entry) => entry.url.endsWith('/complete')).length, 1, 'a lost completion response cannot replay its code');
  await retry.login(); retry.paste(); await retry.parse(); await retry.complete();
  assert.deepEqual(retry.calls.filter((entry) => entry.url.endsWith('/complete')).map((entry) => entry.url), [
    '/api/admin/channels/oauth/sessions/session-1/complete', '/api/admin/channels/oauth/sessions/session-2/complete',
  ]);
  assert.equal(retry.getNode('channelOAuthStatus').textContent, retry.locale.oauthAuthorized);
});

test('expired sessions cannot parse, complete, or save, including expiry after authorization', async () => {
  const env = await environment();
  env.createOAuth(); await env.login(); env.paste(); env.setNow(2001);
  const before = env.calls.length;
  await env.parse(); await env.complete();
  assert.equal(env.calls.length, before);
  assert.match(env.getNode('channelOAuthStatus').textContent, /expired/);
  const parsed = await environment();
  parsed.createOAuth(); await parsed.login(); parsed.paste(); await parsed.parse(); parsed.setNow(2001);
  await parsed.complete();
  assert.equal(parsed.calls.filter((entry) => entry.url.endsWith('/complete')).length, 0);
  assert.equal(parsed.getNode('channelOAuthComplete').disabled, true);
  const authorized = await environment();
  authorized.createOAuth(); await authorized.login(); authorized.paste(); await authorized.parse(); await authorized.complete();
  authorized.setNow(2001); await authorized.submit();
  assert.equal(authorized.saves().length, 0);
  assert.match(authorized.getNode('channelOAuthStatus').textContent, /expired/);
});

test('untrusted authorization URLs never become clickable', async () => {
  const query = new URL(session().authorization_url).search;
  for (const uri of [
    'javascript:alert(1)', `https://auth.openai.com.evil.test/oauth/authorize${query}`,
    `http://auth.openai.com/oauth/authorize${query}`, `https://auth.openai.com/oauth/token${query}`,
    `https://user@auth.openai.com/oauth/authorize${query}`, session().authorization_url.replace('1455', '8010'),
  ]) {
    const env = await environment({ start: session({ authorization_url: uri }) });
    env.createOAuth(); await env.login();
    assert.equal(env.getNode('channelOAuthUrl').href, undefined);
    assert.equal(env.getNode('channelOAuthStatus').textContent, env.locale.oauthFailed);
    assert.equal(env.timers.length, 0);
    assert.equal(env.deletes().length, 1);
  }
});

test('late parse results do not restore previews after input changes or a form is reopened', async () => {
  for (const interrupt of [(env) => env.paste(callback('different')), (env) => { env.page.closeForm(); env.createOAuth(); }]) {
    let finishParse;
    const env = await environment({ parses: [() => new Promise((resolve) => { finishParse = resolve; })] });
    env.createOAuth(); await env.login(); env.paste();
    const parsing = env.parse(); interrupt(env);
    finishParse({ code: 'mock-code', state: 'mock-state', redirect_uri: session().redirect_uri }); await parsing;
    assert.equal(env.getNode('channelOAuthPreview').classList.contains('hidden'), true);
    assert.equal(env.getNode('channelOAuthComplete').disabled, true);
    assert.equal(env.getNode('channelOAuthCode').textContent, '');
  }
});

test('late completion cannot authorize a closed or reopened form', async () => {
  let finishComplete;
  const env = await environment({ completes: [() => new Promise((resolve) => { finishComplete = resolve; })] });
  env.createOAuth(); await env.login(); env.paste(); await env.parse();
  const completing = env.complete();
  env.page.closeForm(); env.createOAuth(); await env.login();
  finishComplete(session({ status: 'authorized' })); await completing;
  assert.throws(() => env.page.oauthSaveFields(env.form), /Parse the callback URL/);
  assert.equal(env.getNode('channelOAuthStatus').textContent, env.locale.oauthPending);
  assert.equal(env.getNode('channelOAuthComplete').disabled, true);
});

test('start, parse, and complete each prevent repeated clicks while their request is pending', async () => {
  let finishStart;
  const env = await environment({ start: () => new Promise((resolve) => { finishStart = resolve; }) });
  env.createOAuth(); const starting = env.login(); await env.login();
  assert.equal(env.calls.filter((entry) => entry.url.endsWith('/oauth/sessions')).length, 1);
  finishStart(session()); await starting;
  let finishParse;
  let finishComplete;
  const busy = await environment({
    parses: [() => new Promise((resolve) => { finishParse = resolve; })],
    completes: [() => new Promise((resolve) => { finishComplete = resolve; })],
  });
  busy.createOAuth(); await busy.login(); busy.paste();
  const parsing = busy.parse(); await busy.parse(); await busy.complete();
  assert.equal(busy.calls.filter((entry) => entry.url.endsWith('/parse')).length, 1);
  assert.equal(busy.calls.filter((entry) => entry.url.endsWith('/complete')).length, 0);
  finishParse({ code: 'mock-code', state: 'mock-state', redirect_uri: session().redirect_uri }); await parsing;
  const completing = busy.complete(); await busy.complete();
  assert.equal(busy.calls.filter((entry) => entry.url.endsWith('/complete')).length, 1);
  finishComplete(session({ status: 'authorized' })); await completing;
});

test('old save success and finally cannot reset a newly opened form or its pending save button', async () => {
  const saves = [];
  const env = await environment({ onSave: () => new Promise((resolve) => { saves.push(resolve); }) });
  env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
  const savingOld = env.submit();
  env.page.closeForm(); env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
  const savingNew = env.submit();
  assert.equal(saves.length, 2, 'the new form can save while the old request is unresolved');
  assert.equal(env.getNode('saveChannelBtn').disabled, true);
  const caption = env.getNode('saveChannelBtn').innerHTML;
  saves[0](savedChannel()); await savingOld;
  assert.equal(env.getNode('saveChannelBtn').disabled, true);
  assert.equal(env.getNode('saveChannelBtn').innerHTML, caption);
  assert.equal(env.form.elements.credential_type.value, 'oauth');
  assert.equal(env.getNode('channelModal').classList.contains('hidden'), false);
  assert.equal(env.deletes().length, 1);
  assert.equal(env.toasts.length, 0);
  saves[1](savedChannel()); await savingNew;
  assert.equal(env.getNode('saveChannelBtn').disabled, false);
  assert.equal(env.getNode('channelModal').classList.contains('hidden'), true);
});

test('same-form authentication resets release save without allowing an old finally to unlock a new save', async () => {
  for (const reset of ['api_key', 'new_link']) {
    const saves = [];
    const env = await environment({ onSave: () => new Promise((resolve) => { saves.push(resolve); }) });
    env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
    const savingOld = env.submit();
    if (reset === 'api_key') {
      env.form.elements.credential_type.value = 'api_key'; env.form.elements.credential_type.listeners.change();
      env.form.elements.key.value = 'replacement-key';
    } else {
      await env.login(); env.paste(); await env.parse(); await env.complete();
    }
    assert.equal(env.getNode('saveChannelBtn').disabled, false);
    const savingNew = env.submit();
    assert.equal(saves.length, 2);
    assert.equal(env.getNode('saveChannelBtn').disabled, true);
    saves[0](savedChannel()); await savingOld;
    assert.equal(env.getNode('saveChannelBtn').disabled, true);
    saves[1](savedChannel()); await savingNew;
    assert.equal(env.getNode('saveChannelBtn').disabled, false);
  }
});

test('explicit probing after authorization fills the draft through the temporary session without saving', async () => {
  const env = await environment();
  env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
  assert.equal(env.probes().length, 0, 'completion does not automatically fetch models');
  assert.equal(env.form.elements.models.value, 'gpt-5');
  assert.equal(env.getNode('probeModels').disabled, false);
  await env.probe();
  assert.deepEqual(env.probes(), [{ url: '/api/admin/channels/oauth/sessions/one-time-session/probe-models', method: 'POST', body: null }]);
  assert.equal(env.form.elements.models.value, 'catalog-a, catalog-b');
  assert.equal(env.getNode('probeResult').textContent, `2 ${env.locale.foundModels} 3 ms`);
  assert.equal(env.saves().length, 0);
  await env.submit();
  assert.deepEqual(env.saves()[0].body.models, ['catalog-a', 'catalog-b']);
});

test('saved OAuth editing probes stored credentials, while a replacement session must finish first', async () => {
  const env = await environment();
  await env.edit(); await env.probe();
  assert.deepEqual(env.probes()[0], { url: '/api/admin/channels/7/probe-models', method: 'POST', body: null });
  await env.login();
  assert.equal(env.getNode('probeModels').disabled, true);
  await env.probe();
  assert.equal(env.probes().length, 1, 'pending replacement cannot fall back to the saved credential');
  env.paste(); await env.parse(); await env.complete(); await env.probe();
  assert.equal(env.probes()[1].url, '/api/admin/channels/oauth/sessions/one-time-session/probe-models');
  env.setNow(2001); await env.probe();
  assert.equal(env.probes().length, 2, 'expired replacement cannot fall back either');
  assert.equal(env.getNode('probeModels').disabled, true);
  assert.equal(env.getNode('channelOAuthStatus').textContent, env.locale.oauthExpired);
  env.page.cancelOAuthLogin(); await env.probe();
  assert.equal(env.probes()[2].url, '/api/admin/channels/7/probe-models');
  for (const status of ['failed', 'expired', 'exchanging', 'cancelled']) {
    const blocked = await environment({ completes: [session({ status })] });
    await blocked.edit(); await blocked.login(); blocked.paste(); await blocked.parse(); await blocked.complete();
    await blocked.probe();
    assert.equal(blocked.probes().length, 0, status);
    assert.equal(blocked.getNode('probeModels').disabled, true, status);
  }
});

test('OAuth menu probes use the saved endpoint and keep confirmation before updating models', async () => {
  for (const confirmResult of [true, false]) {
    const env = await environment({ listed: [savedChannel()], confirmResult });
    await env.page.load(); await env.menuProbe();
    assert.deepEqual(env.probes(), [{ url: '/api/admin/channels/7/probe-models', method: 'POST', body: null }]);
    assert.equal(env.confirmations.length, 1);
    assert.equal(env.saves().length, confirmResult ? 1 : 0);
    if (confirmResult) assert.deepEqual(env.saves()[0].body, { models: ['catalog-a', 'catalog-b'] });
    env.page.unload();
  }
});

test('probe failures preserve models and show safe OAuth feedback', async () => {
  const env = await environment({ probes: [new Error('upstream secret-token')] });
  await env.edit(); await env.probe();
  assert.equal(env.form.elements.models.value, 'gpt-5');
  assert.equal(env.getNode('probeResult').textContent, env.locale.oauthProbeFailed);
  assert.doesNotMatch(env.getNode('probeResult').textContent, /secret-token/);
  assert.equal(env.getNode('probeModels').disabled, false);
  const menu = await environment({ listed: [savedChannel()], probes: [new Error('upstream secret-token')] });
  await menu.page.load(); await menu.menuProbe();
  assert.equal(menu.saves().length, 0);
  assert.equal(menu.confirmations.length, 0);
  assert.ok(menu.toasts.some(([message]) => message === menu.locale.oauthProbeFailed));
  assert.doesNotMatch(JSON.stringify(menu.toasts), /secret-token/);
  await menu.menuProbe();
  assert.equal(menu.saves().length, 1, 'a failed menu request releases its duplicate guard for retry');
  menu.page.unload();
});

test('models input events preserve manual changes during a probe, including editing back to the original value', async () => {
  for (const editBack of [false, true]) {
    let finish;
    const env = await environment({ probes: [() => new Promise((resolve) => { finish = resolve; })] });
    await env.edit();
    const probing = env.probe();
    env.editModels('manual-model');
    if (editBack) env.editModels('gpt-5');
    finish({ models: ['catalog-a'], latency_ms: 1, raw_count: 1 }); await probing;
    assert.equal(env.form.elements.models.value, editBack ? 'gpt-5' : 'manual-model');
    assert.equal(env.getNode('probeResult').textContent, env.locale.probeModelsInputChanged);
    assert.equal(env.getNode('probeModels').disabled, false);
  }
});

test('late OAuth probe results cannot change a cancelled, closed, switched, reopened, or reauthorized form', async () => {
  const interrupts = [
    (env) => env.page.cancelOAuthLogin(), (env) => env.getNode('closeChannelModal').listeners.click(),
    (env) => env.dispatch('keydown', { key: 'Escape' }), (env) => env.backdrop(),
    (env) => env.page.unload(),
    (env) => { env.form.elements.credential_type.value = 'api_key'; env.form.elements.credential_type.listeners.change(); },
    (env) => env.edit(savedChannel({ id: 8, models: ['other-channel'] })),
    async (env) => { await env.login(); env.paste(); await env.parse(); await env.complete(); },
  ];
  for (const interrupt of interrupts) {
    let finish;
    const env = await environment({ probes: [() => new Promise((resolve) => { finish = resolve; })] });
    env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
    const probing = env.probe();
    await interrupt(env);
    const current = env.form.elements.models.value;
    finish({ models: ['late-result'], latency_ms: 1, raw_count: 1 }); await probing;
    assert.equal(env.form.elements.models.value, current);
    assert.equal(env.getNode('probeResult').textContent, '');
  }
});

test('probe duplicates are blocked and a stale finally cannot unlock a new form request', async () => {
  const finishes = [];
  const env = await environment({ probes: [0, 1].map(() => () => new Promise((resolve) => { finishes.push(resolve); })) });
  await env.edit();
  const old = env.probe(); await env.probe();
  assert.equal(env.probes().length, 1);
  await env.edit(savedChannel({ id: 8 }));
  const current = env.probe();
  assert.equal(env.probes().length, 2);
  const caption = env.getNode('probeModels').innerHTML;
  finishes[0]({ models: ['old'], latency_ms: 1 }); await old;
  assert.equal(env.getNode('probeModels').disabled, true);
  assert.equal(env.getNode('probeModels').innerHTML, caption);
  finishes[1]({ models: ['new'], latency_ms: 1 }); await current;
  assert.equal(env.getNode('probeModels').disabled, false);
  assert.equal(env.form.elements.models.value, 'new');
});

test('a late probe rejection does not show an error or unlock the current request', async () => {
  let failOld;
  let finishCurrent;
  const env = await environment({ probes: [
    () => new Promise((resolve, reject) => { failOld = reject; }),
    () => new Promise((resolve) => { finishCurrent = resolve; }),
  ] });
  await env.edit(); const old = env.probe();
  await env.edit(savedChannel({ id: 8 })); const current = env.probe();
  failOld(new Error('old secret-token')); await old;
  assert.equal(env.getNode('probeResult').textContent, '');
  assert.equal(env.getNode('probeModels').disabled, true);
  finishCurrent({ models: ['new'], latency_ms: 1 }); await current;
  assert.equal(env.form.elements.models.value, 'new');
});

test('expiry during a temporary probe discards its response and requires a new authorization', async () => {
  let finish;
  const env = await environment({ probes: [() => new Promise((resolve) => { finish = resolve; })] });
  env.createOAuth(); await env.login(); env.paste(); await env.parse(); await env.complete();
  const probing = env.probe(); env.setNow(2001);
  finish({ models: ['too-late'], latency_ms: 1 }); await probing;
  assert.equal(env.form.elements.models.value, 'gpt-5');
  assert.equal(env.getNode('probeModels').disabled, true);
  assert.equal(env.getNode('channelOAuthStatus').textContent, env.locale.oauthExpired);
});

test('saved menu prevents concurrent duplicate probes and updates', async () => {
  let finish;
  const env = await environment({ listed: [savedChannel()], probes: [() => new Promise((resolve) => { finish = resolve; })] });
  await env.page.load();
  const pending = env.menuProbe(); await env.menuProbe();
  assert.equal(env.probes().length, 1);
  finish({ models: ['catalog-a'], latency_ms: 1 }); await pending;
  assert.equal(env.saves().length, 1);
  env.page.unload();
});

for (const language of ['zh-CN', 'en']) {
  test(`OAuth cards expose authorization checks without catalog or generation claims (${language})`, async () => {
    const env = await environment({ language, listed: [savedChannel()] });
    await env.page.load();
    assert.match(env.getNode('channels').innerHTML, /data-channel-probe="7"/);
    assert.match(env.getNode('channels').innerHTML, /ChatGPT OAuth/);
    await env.page.onClick({ dataset: { channelTest: '7' }, closest: () => null });
    assert.ok(env.getNode('channels').innerHTML.includes(env.locale.oauthCheckPassed));
    assert.ok(!env.getNode('channels').innerHTML.includes(env.locale.modelCatalogReachable));
    env.page.unload();
  });
}

test('both locales contain OAuth labels and official links open safely', async () => {
  const window = {};
  vm.runInNewContext(translations, { window });
  const keys = ['credentialType', 'oauthCredentialType', 'oauthProvider', 'oauthLogin', 'oauthRelogin', 'oauthStarting', 'oauthDescription', 'oauthOpenUrl', 'oauthStepLink', 'oauthStepParse', 'oauthStepComplete', 'oauthCopyLink', 'oauthCallbackHint', 'oauthCallbackUrl', 'oauthParse', 'oauthParsing', 'oauthCallbackMatched', 'oauthCallbackInvalid', 'oauthComplete', 'oauthCompleting', 'oauthCompleteFailed', 'oauthPending', 'oauthAuthorized', 'oauthConnected', 'oauthNeedsLogin', 'oauthLoginRequired', 'oauthFailed', 'oauthExpired', 'oauthCancelled', 'oauthModelsHint', 'oauthProbeFailed', 'probeModelsInputChanged', 'oauthCheck', 'oauthCheckPassed', 'apiKeyRequired'];
  for (const language of ['zh-CN', 'en']) for (const key of keys) {
    assert.equal(typeof window.ROTOR_UI_CONFIG.locales[language][key], 'string', `${language}.${key}`);
  }
  assert.match(html, /id="channelOAuthUrl" target="_blank" rel="noopener noreferrer"/);
});
