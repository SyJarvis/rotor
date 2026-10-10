import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const frontend = new URL('../src/rotor/frontend/', import.meta.url);
const sources = Object.fromEntries(await Promise.all(['security.js', 'api.js', 'session.js', 'i18n.js', 'config.js', 'index.html'].map(async (name) => [name, await readFile(new URL(name, frontend), 'utf8')])));
const grant = { id: 5, client_id: 'agent', client_name: 'Rotor Client', created_at: '2026-10-10T01:00:00Z', last_used_at: null, expires_at: '2026-11-10T01:00:00Z' };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, statusText: 'Error', headers: { get: () => 'application/json' }, json: async () => body });

async function environment({ language = 'zh-CN', grants = [grant], handler } = {}) {
  const nodes = new Map(); let document;
  function element(tag = 'div', attrs = {}) {
    const listeners = new Map(), classes = new Set((attrs.class || '').split(' ').filter(Boolean));
    const node = {
      tagName: tag.toUpperCase(), id: attrs.id || '', dataset: {}, children: [], parentNode: null, disabled: false, hidden: 'hidden' in attrs, value: '', textContent: '',
      classList: { contains: (name) => classes.has(name), toggle(name, on) { on ? classes.add(name) : classes.delete(name); } },
      setAttribute(name, value) { attrs[name] = String(value); }, getAttribute: (name) => attrs[name],
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      replaceChildren(...children) { this.children = children; },
      contains(target) { return this === target || this.children.some((child) => child.contains(target)); },
      querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.tagName.toLowerCase() === selector ? [child] : []), ...child.querySelectorAll(selector)]); },
      focus() { document.activeElement = this; },
      addEventListener(type, callback) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(callback); },
      dispatchEvent(event) { event.target ??= this; const results = (listeners.get(event.type) || []).map((callback) => callback(event)); if (event.bubbles && !event.stopped && this.parentNode) results.push(this.parentNode.dispatchEvent(event)); return Promise.all(results); },
      emit(type, detail = {}) { if (type === 'click' && this.disabled) return Promise.resolve(); return this.dispatchEvent({ type, target: this, bubbles: true, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...detail }); },
      set innerHTML(_value) { throw new Error('Client controlled values must be plain text'); },
    };
    for (const [name, value] of Object.entries(attrs)) if (name.startsWith('data-')) node.dataset[name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = value;
    if (node.id) nodes.set(node.id, node);
    return node;
  }
  document = element('document'); document.documentElement = { lang: language }; document.cookie = 'rotor_admin_session=session; rotor_admin_csrf=csrf%2Btoken';
  document.getElementById = (id) => nodes.get(id); document.createElement = element;
  const fragment = sources['index.html'].split('<section id="settingsSecurity"')[1].split('<section id="settingsRouting"')[0];
  const stack = [document];
  for (const match of ('<section id="settingsSecurity"' + fragment).matchAll(/<\/?[a-z][^>]*>/gi)) {
    const token = match[0];
    if (token.startsWith('</')) { stack.pop(); continue; }
    const tag = token.match(/^<([^\s>]+)/)[1];
    const attrs = Object.fromEntries([...token.slice(tag.length + 1, -1).matchAll(/([^\s=]+)(?:="([^"]*)")?/g)].map((attr) => [attr[1], attr[2] || '']));
    const child = stack.at(-1).appendChild(element(tag, attrs));
    if (!['input', 'br', 'hr'].includes(tag)) stack.push(child);
  }
  const window = element('window'); window.ROTOR_ACTOR = { id: 7, username: 'alice', role: 'user' };
  const requests = [], context = vm.createContext({ document, window, AbortController, DOMException, Date,
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    localStorage: { getItem: () => language, setItem() {} },
    fetch: async (path, options) => {
      const request = { path, options }; requests.push(request);
      const custom = await handler?.(request, requests);
      if (custom !== undefined) return custom;
      if (path === '/api/oauth/grants') return response({ items: grants });
      if (path === '/api/auth/change-password') return response({ id: 7, username: 'alice', role: 'user' });
      return response(null, 204);
    },
  });
  vm.runInContext(sources['config.js'], context);
  const modules = new Map();
  function module(name) { if (!modules.has(name)) modules.set(name, new vm.SourceTextModule(sources[name], { context })); return modules.get(name); }
  const entry = module('security.js'); await entry.link((specifier) => module(specifier.slice(2).split('?')[0])); await entry.evaluate();
  const controller = entry.namespace.initSecurity(); controller.open(); await settle();
  function card(id) { return nodes.get('securityDevicesList').children.find((node) => node.dataset.grantId === String(id)); }
  function action(id, name) { return card(id)?.querySelectorAll('button').find((node) => node.dataset.grantAction === name); }
  return { get: (id) => nodes.get(id), document, window, requests, controller, card, action, session: modules.get('session.js').namespace,
    async click(id, name) { const node = name ? action(id, name) : nodes.get(id); node.focus(); await node.emit('click'); },
    async locale() { modules.get('i18n.js').namespace.toggleLocale(); await settle(); },
  };
}

function texts(node) { return [node.textContent, ...node.children.flatMap(texts)].filter(Boolean); }

test('device records show separate grants, safe long names and all dates in both locales', async () => {
  for (const language of ['zh-CN', 'en']) {
    const h = await environment({ language, grants: [grant, { ...grant, id: 6, client_name: '<script>' + 'LongClient'.repeat(40), last_used_at: '2026-10-11T02:00:00Z' }] });
    assert.equal(h.get('securityDevicesList').children.length, 2);
    assert.equal(h.card(6).children[0].textContent, '<script>' + 'LongClient'.repeat(40));
    const labels = texts(h.card(5));
    assert.ok(labels.includes(language === 'en' ? 'Not used yet' : '尚未使用'));
    for (const label of language === 'en' ? ['Authorized', 'Last used', 'Expires at'] : ['授权时间', '最近使用', '到期时间']) assert.ok(labels.includes(label), label);
    assert.match(h.get('securityDevicesCount').textContent, /^2 /);
    assert.equal(h.requests[0].options.credentials, 'same-origin'); assert.equal(h.requests[0].options.cache, 'no-store');
    assert.equal(h.requests[0].options.headers.Authorization, undefined);
    h.action(5, 'revoke').focus(); await h.locale();
    assert.equal(h.document.activeElement.dataset.grantId, '5'); assert.equal(h.document.activeElement.dataset.grantAction, 'revoke');
  }
});

test('loading, empty, error and retry are distinct and refresh cannot duplicate a pending request', async () => {
  const slow = deferred(); let attempts = 0;
  const h = await environment({ handler: ({ path }) => {
    if (path === '/api/oauth/grants' && ++attempts === 1) return slow.promise;
    if (path === '/api/oauth/grants') return response({ items: [] });
  } });
  assert.equal(h.get('securityDevicesStatus').textContent, '正在加载授权记录…');
  assert.equal(h.get('securityDevicesList').children.length, 0);
  await h.click('securityDevicesRefresh'); assert.equal(attempts, 1);
  slow.resolve(response({ detail: 'network unavailable' }, 503)); await settle();
  assert.equal(h.get('securityDevicesError').textContent, 'network unavailable'); assert.equal(h.get('securityDevicesRetry').hidden, false);
  const retry = h.click('securityDevicesRetry'); await h.click('securityDevicesRetry'); await retry;
  assert.equal(attempts, 2); assert.equal(h.get('securityDevicesError').hidden, true);
  assert.ok(texts(h.get('securityDevicesList')).includes('暂无已授权设备'));
  await h.click('securityDevicesRefresh'); assert.equal(attempts, 3);
});

test('two-step revoke is per grant, cancel sends nothing, confirmation uses CSRF and restores keyboard focus', async () => {
  const h = await environment({ grants: [grant, { ...grant, id: 6 }] });
  await h.click(5, 'revoke'); assert.equal(h.document.activeElement, h.action(5, 'cancel'));
  assert.ok(h.action(6, 'revoke')); assert.equal(h.requests.length, 1);
  await h.click(5, 'cancel'); assert.equal(h.requests.length, 1); assert.equal(h.document.activeElement, h.action(5, 'revoke'));
  await h.click(5, 'revoke'); await h.click(5, 'confirm');
  const request = h.requests[1]; assert.equal(request.path, '/api/oauth/grants/5'); assert.equal(request.options.method, 'DELETE');
  assert.equal(request.options.headers['X-CSRF-Token'], 'csrf+token'); assert.equal(request.options.headers.Authorization, undefined);
  assert.equal(h.card(5), undefined); assert.ok(h.card(6)); assert.equal(h.document.activeElement, h.action(6, 'revoke'));
  await h.click(6, 'revoke'); await h.click(6, 'confirm');
  assert.equal(h.document.activeElement, h.get('securityDevicesRefresh')); assert.equal(h.get('securityDevicesStatus').textContent, '授权已撤销');
});

test('failed revocation retains its card and confirmation, disables duplicate requests and permits retry', async () => {
  const pending = deferred(); let attempts = 0;
  const h = await environment({ handler: ({ options }) => options.method === 'DELETE' && ++attempts === 1 ? pending.promise : undefined });
  await h.click(5, 'revoke'); const deleting = h.click(5, 'confirm');
  assert.equal(h.action(5, 'confirm').disabled, true); assert.equal(h.action(5, 'cancel').disabled, true);
  assert.equal(h.document.activeElement, h.card(5));
  await h.click(5, 'confirm'); await h.click('securityDevicesRefresh'); assert.equal(attempts, 1);
  pending.resolve(response({ detail: 'Try again' }, 503)); await deleting;
  assert.ok(texts(h.card(5)).includes('Try again')); assert.equal(h.document.activeElement, h.action(5, 'confirm'));
  await h.locale(); assert.ok(texts(h.card(5)).includes('Try again'));
  await h.click(5, 'confirm'); assert.equal(attempts, 2); assert.equal(h.card(5), undefined);
});

test('close, logout and owner changes abort loading and ignore late records', async () => {
  for (const change of ['close', 'logout', 'owner']) {
    const pending = deferred(); let attempts = 0;
    const h = await environment({ handler: ({ path }) => path === '/api/oauth/grants' && ++attempts === 1 ? pending.promise : response({ items: [] }) });
    const request = h.requests[0];
    if (change === 'close') h.controller.close();
    else if (change === 'logout') h.session.endSession();
    else h.session.setActor({ id: 8, username: 'bob', role: 'user' });
    assert.equal(request.options.signal.aborted, true);
    pending.resolve(response({ items: [grant] })); await settle();
    assert.equal(h.card(5), undefined); assert.equal(h.get('securityDevicesError').textContent, '');
    if (change === 'close') { h.controller.open(); await settle(); assert.equal(h.requests.length, 2); }
  }
});

test('same-account profile updates preserve devices and inflight revocation but owner change cannot remove a new account card', async () => {
  const pending = deferred();
  const h = await environment({ handler: ({ options }) => options.method === 'DELETE' ? pending.promise : undefined });
  await h.click(5, 'revoke'); const deleting = h.click(5, 'confirm');
  h.session.setActor({ id: 7, username: 'alice', display_name: 'Updated', role: 'user' });
  assert.equal(h.requests.length, 2); assert.equal(h.requests[1].options.signal.aborted, false);
  h.session.setActor({ id: 8, username: 'bob', role: 'user' }); await settle();
  assert.equal(h.requests[1].options.signal.aborted, true); assert.ok(h.card(5));
  pending.resolve(response(null, 204)); await deleting;
  assert.ok(h.card(5), 'late old-account DELETE response cannot remove the new account record');
  assert.equal(h.get('securityDevicesStatus').textContent, '');
});

test('password validates separately, Enter changes password without submitting settings, and cancel clears fields', async () => {
  const h = await environment(); let settingsSubmissions = 0;
  h.document.addEventListener('keydown', () => settingsSubmissions++);
  assert.equal(h.get('securityPasswordPanel').hidden, true); await h.click('securityPasswordToggle');
  assert.equal(h.document.activeElement, h.get('accountCurrentPassword'));
  h.get('accountNewPassword').value = 'short'; await h.click('accountPasswordSave'); assert.equal(h.requests.length, 1);
  h.get('accountNewPassword').value = 'new-password'; h.get('accountConfirmPassword').value = 'different'; await h.click('accountPasswordSave'); assert.equal(h.requests.length, 1);
  h.get('accountCurrentPassword').value = 'old-password'; h.get('accountConfirmPassword').value = 'new-password';
  await h.get('accountConfirmPassword').emit('keydown', { key: 'Enter' }); await settle();
  assert.equal(settingsSubmissions, 0); assert.equal(h.requests[1].path, '/api/auth/change-password');
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { current_password: 'old-password', new_password: 'new-password' });
  assert.equal(h.requests[1].options.headers['X-CSRF-Token'], 'csrf+token'); assert.equal(h.requests.length, 2, 'password setActor does not reload grants');
  assert.equal(h.get('securityPasswordStatus').textContent, '密码已更新'); assert.ok(h.card(5));
  for (const id of ['accountCurrentPassword', 'accountNewPassword', 'accountConfirmPassword']) assert.equal(h.get(id).value, '');
  h.get('accountNewPassword').value = 'unsaved'; await h.click('securityPasswordCancel');
  assert.equal(h.get('securityPasswordPanel').hidden, true); assert.equal(h.get('accountNewPassword').value, ''); assert.equal(h.document.activeElement, h.get('securityPasswordToggle'));
});

test('password errors survive locale changes, pending guard prevents repeats and close clears secrets and stale success', async () => {
  const pending = deferred(); let attempts = 0;
  const h = await environment({ handler: ({ path }) => {
    if (path === '/api/auth/change-password' && ++attempts === 1) return response({ detail: 'Incorrect current password' }, 400);
    if (path === '/api/auth/change-password') return pending.promise;
  } });
  await h.click('securityPasswordToggle');
  for (const id of ['accountCurrentPassword', 'accountNewPassword', 'accountConfirmPassword']) h.get(id).value = 'valid-password';
  await h.click('accountPasswordSave'); assert.equal(h.get('securityPasswordStatus').textContent, 'Incorrect current password');
  await h.locale(); assert.equal(h.get('securityPasswordStatus').textContent, 'Incorrect current password');
  const saving = h.click('accountPasswordSave'); await h.click('accountPasswordSave'); assert.equal(attempts, 2);
  h.controller.close(); assert.equal(h.requests.at(-1).options.signal.aborted, true);
  for (const id of ['accountCurrentPassword', 'accountNewPassword', 'accountConfirmPassword']) assert.equal(h.get(id).value, '');
  pending.resolve(response({ id: 7, username: 'alice', role: 'user' })); await saving;
  assert.equal(h.get('securityPasswordStatus').textContent, ''); assert.equal(h.get('securityDevicesList').children.length, 0);
});

test('logout or owner change during a password update clears secrets and cannot restore the old actor', async () => {
  for (const change of ['logout', 'owner']) {
    const pending = deferred();
    const h = await environment({ handler: ({ path }) => path === '/api/auth/change-password' ? pending.promise : undefined });
    await h.click('securityPasswordToggle');
    for (const id of ['accountCurrentPassword', 'accountNewPassword', 'accountConfirmPassword']) h.get(id).value = 'valid-password';
    const saving = h.click('accountPasswordSave'); const request = h.requests.at(-1);
    if (change === 'logout') h.session.endSession();
    else h.session.setActor({ id: 8, username: 'bob', role: 'user' });
    assert.equal(request.options.signal.aborted, true);
    pending.resolve(response({ id: 7, username: 'alice', role: 'user' })); await saving;
    assert.equal(h.window.ROTOR_ACTOR?.id ?? null, change === 'logout' ? null : 8);
    for (const id of ['accountCurrentPassword', 'accountNewPassword', 'accountConfirmPassword']) assert.equal(h.get(id).value, '');
    assert.equal(h.get('securityPasswordStatus').textContent, ''); assert.equal(h.get('securityPasswordPanel').hidden, true);
  }
});

test('account panel contains only profile actions and all security actions avoid submitting the outer settings form', () => {
  const account = sources['index.html'].split('<section id="settingsAccount"')[1].split('<section id="settingsSecurity"')[0];
  assert.ok(account.includes('id="accountSave"')); assert.ok(!account.includes('type="password"')); assert.ok(!account.includes('id="accountLogout"'));
  const security = sources['index.html'].split('<section id="settingsSecurity"')[1].split('<section id="settingsRouting"')[0];
  assert.ok([...security.matchAll(/<button\b([^>]*)>/g)].every((match) => match[1].includes('type="button"')));
  assert.ok(!sources['index.html'].includes('href="/oauth/apps"'));
});
