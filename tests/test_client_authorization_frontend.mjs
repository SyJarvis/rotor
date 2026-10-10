import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const frontend = new URL('../src/rotor/frontend/', import.meta.url);
const sources = Object.fromEntries(await Promise.all(['client-authorization.js', 'api.js', 'session.js', 'i18n.js', 'config.js', 'oauth.html'].map(async (name) => [name, await readFile(new URL(name, frontend), 'utf8')])));
const callback = 'http://localhost:8123/callback';
const query = new URLSearchParams({ response_type: 'code', client_id: 'coding-agent', redirect_uri: callback, state: 'state+value', code_challenge: 'challenge-value', code_challenge_method: 'S256', scope: 'inference' });
const details = { client_id: 'coding-agent', client_name: 'Trusted coding agent', redirect_uri: callback, scope: 'inference', username: 'alice', models: ['gpt-example'], quota: 10000, used_quota: 2000 };
const grant = { id: 5, client_id: 'coding-agent', client_name: 'Trusted coding agent', created_at: '2026-10-10T01:00:00Z', last_used_at: null, expires_at: '2026-11-10T01:00:00Z' };

function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function response(body, status = 200) { return { ok: status >= 200 && status < 300, status, statusText: 'Error', headers: { get: () => 'application/json' }, json: async () => body }; }

async function environment({ mode = 'authorize', language = 'zh-CN', metadata = details, grants = [grant], handler, search = query.toString() } = {}) {
  function events() {
    const listeners = new Map();
    return {
      addEventListener(name, callback) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(callback); },
      dispatchEvent(event) { for (const callback of listeners.get(event.type) || []) callback(event); },
    };
  }
  const nodes = new Map(), translatable = [];
  function element(id = '') {
    const classes = new Set();
    const node = { ...events(), id, textContent: '', children: [], disabled: false, dataset: {},
      classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value), contains: (value) => classes.has(value), toggle(value, yes) { yes ? classes.add(value) : classes.delete(value); } },
      appendChild(child) { this.children.push(child); }, replaceChildren(...children) { this.children = children; },
      click() { if (this.disabled) return; let result; for (const callback of this.clickListeners || []) result = callback({ currentTarget: this }); return result; },
      set innerHTML(_value) { throw new Error('Dynamic values must not be rendered as HTML'); },
    };
    const register = node.addEventListener;
    node.addEventListener = (name, callback) => { if (name === 'click') { node.clickListeners ||= []; node.clickListeners.push(callback); } else register(name, callback); };
    return node;
  }
  for (const tag of sources['oauth.html'].matchAll(/<\w+\b([^>]*)>/g)) {
    const attrs = tag[1], id = attrs.match(/\bid="([^"]+)"/)?.[1], key = attrs.match(/\bdata-i18n="([^"]+)"/)?.[1];
    if (!id && !key) continue;
    const node = element(id);
    if (attrs.includes(' disabled')) node.disabled = true;
    for (const value of attrs.match(/\bclass="([^"]+)"/)?.[1].split(' ') || []) node.classList.add(value);
    if (id) nodes.set(id, node);
    if (key) { node.dataset.i18n = key; translatable.push(node); }
  }
  const document = { ...events(), cookie: 'rotor_admin_session=session-cookie; rotor_admin_csrf=csrf%2Btoken', documentElement: { lang: language }, title: '',
    getElementById: (id) => nodes.get(id), createElement: () => element(), querySelectorAll: (selector) => selector === '[data-i18n]' ? translatable : [],
  };
  const redirects = [], window = { ...events(), ROTOR_ACTOR: { id: 7, username: 'alice', role: 'user' }, location: { pathname: `/oauth/${mode}`, href: `http://localhost/oauth/${mode}?${search}`, assign: (url) => redirects.push(url) } };
  const requests = [], context = vm.createContext({ document, window, URL, URLSearchParams, AbortController, DOMException, Date,
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    localStorage: { getItem: () => language, setItem() {} },
    fetch: async (path, options) => {
      const request = { path, options }; requests.push(request);
      const custom = await handler?.(request, requests);
      if (custom !== undefined) return custom;
      if (path.startsWith('/api/oauth/authorization?')) return response(metadata);
      if (path === '/api/oauth/grants') return response({ items: grants });
      if (options.method === 'DELETE') return response(null, 204);
      const body = JSON.parse(options.body);
      const result = new URL(callback); result.searchParams.set(body.approve ? 'code' : 'error', body.approve ? 'one-time-code' : 'access_denied'); result.searchParams.set('state', body.state);
      return response({ redirect_url: result.href });
    },
  });
  vm.runInContext(sources['config.js'], context);
  const modules = new Map();
  function module(name) {
    if (!modules.has(name)) modules.set(name, new vm.SourceTextModule(sources[name], { context }));
    return modules.get(name);
  }
  const entry = module('client-authorization.js');
  await entry.link((specifier) => module(specifier.replace('./', '').split('?')[0]));
  await entry.evaluate();
  await new Promise((resolve) => setImmediate(resolve));
  const session = modules.get('session.js').namespace;
  return { get: (id) => nodes.get(id), document, window, requests, redirects, session, locale: modules.get('i18n.js').namespace };
}

test('trusted metadata and models render as plain text in both locales without approving automatically', async () => {
  for (const language of ['zh-CN', 'en']) {
    const h = await environment({ language, metadata: { ...details, client_name: '<img src=x onerror=alert(1)>', models: ['<script>unsafe</script>', 'gpt-example'] } });
    assert.equal(h.get('clientOAuthName').textContent, '<img src=x onerror=alert(1)>');
    assert.deepEqual(h.get('clientOAuthModels').children.map((node) => node.textContent), ['<script>unsafe</script>', 'gpt-example']);
    assert.equal(h.get('clientOAuthUsername').textContent, 'alice');
    assert.equal(h.get('clientOAuthCallback').textContent, callback);
    assert.equal(h.get('clientOAuthApprove').disabled, false);
    assert.equal(h.get('clientOAuthApprove').textContent, language === 'en' ? 'Allow access' : '允许授权');
    assert.equal(h.requests.length, 1); assert.equal(h.redirects.length, 0);
    assert.equal(h.requests[0].options.credentials, 'same-origin'); assert.equal(h.requests[0].options.cache, 'no-store');
    h.get('clientOAuthLocale').click();
    assert.equal(h.document.documentElement.lang, language === 'en' ? 'zh-CN' : 'en');
    assert.equal(h.get('clientOAuthName').textContent, '<img src=x onerror=alert(1)>');
  }
});

test('a slow authorization lookup shows loading immediately and keeps consent disabled until metadata arrives', async () => {
  const pending = deferred();
  const h = await environment({ handler: ({ path }) => path.startsWith('/api/oauth/authorization?') ? pending.promise : undefined });
  assert.equal(h.get('clientOAuthStatus').textContent, '正在加载授权信息…');
  assert.equal(h.get('clientOAuthApprove').disabled, true); assert.equal(h.get('clientOAuthDeny').disabled, true);
  pending.resolve(response(details));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.get('clientOAuthApprove').disabled, false); assert.equal(h.get('clientOAuthStatus').textContent, '');
});

test('approval and denial preserve authorization parameters and state, use Cookie and CSRF, and only redirect after success', async () => {
  for (const approve of [true, false]) {
    const h = await environment();
    await h.get(approve ? 'clientOAuthApprove' : 'clientOAuthDeny').click();
    const request = h.requests[1];
    assert.equal(request.path, '/api/oauth/authorization'); assert.equal(request.options.method, 'POST');
    assert.deepEqual(JSON.parse(request.options.body), { ...Object.fromEntries(query), approve });
    assert.equal(request.options.headers['X-CSRF-Token'], 'csrf+token');
    assert.equal(request.options.headers.Authorization, undefined);
    assert.equal(h.redirects.length, 1);
    const target = new URL(h.redirects[0]);
    assert.equal(target.searchParams.get('state'), 'state+value');
    assert.equal(target.searchParams.get(approve ? 'code' : 'error'), approve ? 'one-time-code' : 'access_denied');
  }
});

test('invalid metadata leaves approval disabled and a failed metadata load can be retried', async () => {
  for (const invalid of [{ ...details, client_id: 'wrong' }, { ...details, username: 'other-user' }, { ...details, models: 'gpt-example' }]) {
    const h = await environment({ metadata: invalid });
    assert.equal(h.get('clientOAuthApprove').disabled, true); assert.equal(h.get('clientOAuthDeny').disabled, true);
    assert.match(h.get('clientOAuthError').textContent, /授权信息无效/);
    await h.get('clientOAuthApprove').click(); assert.equal(h.requests.length, 1);
  }
  let attempts = 0;
  const h = await environment({ handler: ({ path }) => {
    if (path.startsWith('/api/oauth/authorization?') && ++attempts === 1) throw new TypeError('Network failed');
  } });
  assert.equal(h.get('clientOAuthApprove').disabled, true);
  assert.equal(h.get('clientOAuthRetry').classList.contains('hidden'), false);
  await h.get('clientOAuthRetry').click();
  assert.equal(h.get('clientOAuthApprove').disabled, false); assert.equal(h.get('clientOAuthError').textContent, '');
});

test('failed approval permits a retry and mutual exclusion prevents approve/deny double submission', async () => {
  let attempts = 0; const pending = deferred();
  const h = await environment({ handler: ({ options }) => {
    if (options.method === 'POST' && ++attempts === 1) return response({ detail: 'Try again' }, 503);
    if (options.method === 'POST') return pending.promise;
  } });
  await h.get('clientOAuthApprove').click();
  assert.equal(h.redirects.length, 0); assert.equal(h.get('clientOAuthApprove').disabled, false);
  assert.equal(h.get('clientOAuthError').textContent, 'Try again');
  const approval = h.get('clientOAuthApprove').click();
  assert.equal(h.get('clientOAuthDeny').disabled, true);
  await h.get('clientOAuthDeny').click(); await h.get('clientOAuthApprove').click();
  assert.equal(attempts, 2);
  pending.resolve(response({ redirect_url: `${callback}?code=code&state=state%2Bvalue` }));
  await approval; assert.equal(h.redirects.length, 1);
});

test('logout and account changes abort inflight approval, erase account details and prevent stale redirects', async () => {
  for (const change of ['logout', 'account']) {
    const pending = deferred(); let metadataRequests = 0;
    const h = await environment({ handler: ({ options, path }) => {
      if (options.method === 'POST') return pending.promise;
      if (path.startsWith('/api/oauth/authorization?') && ++metadataRequests > 1) return response({ ...details, username: 'bob' });
    }, metadata: details });
    const approval = h.get('clientOAuthApprove').click();
    const request = h.requests.find(({ options }) => options.method === 'POST');
    assert.ok(request);
    if (change === 'account') h.session.setActor({ id: 8, username: 'bob', role: 'user' });
    else h.session.endSession();
    assert.equal(request.options.signal.aborted, true);
    assert.equal(h.get('clientOAuthUsername').textContent, ''); assert.equal(h.get('clientOAuthModels').children.length, 0);
    assert.equal(h.get('clientOAuthQuota').textContent, '');
    assert.equal(h.get('clientOAuthApprove').disabled, true);
    pending.resolve(response({ redirect_url: `${callback}?code=code&state=state%2Bvalue` }));
    await approval;
    assert.equal(h.redirects.length, 0);
  }
});

test('remaining account quota distinguishes unlimited, zero and exhausted allowance', async () => {
  for (const [quota, used_quota, text] of [[null, 2000, 'Unlimited'], [10000, 2000, '8,000'], [0, 0, '0'], [100, 101, '0'], [100, null, '100']]) {
    const h = await environment({ language: 'en', metadata: { ...details, quota, used_quota } });
    assert.equal(h.get('clientOAuthQuota').textContent, text);
    assert.equal(h.get('clientOAuthApprove').disabled, false);
  }
});

test('unauthorized response requests sign-in, and untrusted callback responses never navigate', async () => {
  const h = await environment({ handler: ({ options }) => options.method === 'POST' ? response({ detail: 'Sign in again' }, 401) : undefined });
  let signIns = 0;
  h.window.addEventListener('rotorauthrequired', () => { signIns++; h.session.endSession(); });
  await h.get('clientOAuthApprove').click();
  assert.equal(signIns, 1); assert.equal(h.get('clientOAuthApprove').disabled, true); assert.equal(h.redirects.length, 0);
  for (const redirect of ['https://evil.example/callback?code=x&state=state%2Bvalue', `${callback}?code=x&state=wrong`]) {
    const invalid = await environment({ handler: ({ options }) => options.method === 'POST' ? response({ redirect_url: redirect }) : undefined });
    await invalid.get('clientOAuthApprove').click(); assert.equal(invalid.redirects.length, 0);
    assert.match(invalid.get('clientOAuthError').textContent, /授权信息无效/);
  }
});

test('authorized clients list supports owner-scoped revocation with CSRF and retained rows on failure', async () => {
  let fail = true;
  const h = await environment({ mode: 'apps', grants: [{ ...grant, client_name: '<b>Agent</b>' }], handler: ({ options }) => options.method === 'DELETE' && fail ? response({ detail: 'Try again' }, 503) : undefined });
  assert.equal(h.requests[0].path, '/api/oauth/grants');
  assert.equal(h.get('clientAppsList').children[0].children[0].textContent, '<b>Agent</b>');
  await h.get('clientAppsList').children[0].children[2].click();
  assert.equal(h.get('clientAppsList').children.length, 1); assert.equal(h.get('clientOAuthError').textContent, 'Try again');
  fail = false;
  await h.get('clientAppsList').children[0].children[2].click();
  const request = h.requests[2];
  assert.equal(request.path, '/api/oauth/grants/5'); assert.equal(request.options.method, 'DELETE');
  assert.equal(request.options.headers['X-CSRF-Token'], 'csrf+token'); assert.equal(request.options.headers.Authorization, undefined);
  assert.equal(h.get('clientAppsList').children[0].textContent, '暂无已授权客户端');
  assert.equal(h.get('clientOAuthStatus').textContent, '授权已撤销');
  h.session.endSession(); assert.equal(h.get('clientAppsList').children.length, 0); assert.equal(h.get('clientAppsAccount').textContent, '');
});

test('authorization document only loads same-origin assets and applies no-referrer', () => {
  assert.match(sources['oauth.html'], /name="referrer" content="no-referrer"/);
  const assets = [...sources['oauth.html'].matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1]);
  assert.ok(assets.length > 0); assert.ok(assets.every((path) => path.startsWith('/') && !path.startsWith('//')));
});
