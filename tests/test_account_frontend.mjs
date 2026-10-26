import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
const accountSource = await readFile(new URL('../src/rotor/frontend/account.js', import.meta.url), 'utf8');
const apiSource = await readFile(new URL('../src/rotor/frontend/api.js', import.meta.url), 'utf8');
const deferred = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
async function setup() {
  const nodes = new Map(), events = {}, calls = [];
  const profile = { id: 7, username: 'ordinary', display_name: 'Person', role: 'user', default_token_id: 9 };
  let actor = { ...profile }, response = null;
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', disabled: false, classList: { toggle() {} }, listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } });
    return nodes.get(id);
  };
  const context = vm.createContext({ AbortController, document: { getElementById: get, addEventListener(name, callback) { events[name] = callback; } } });
  const bindings = { t: (key) => key, actor: () => actor, accountId: () => String(actor?.id ?? ''), setActor: (value) => { actor = value; events.sessionchange?.(); }, requestLogout() {}, api: async (path, options = {}) => { calls.push({ path, options }); return response ? await response : { ...profile, ...(options.body ? JSON.parse(options.body) : {}) }; } };
  const module = new vm.SourceTextModule(accountSource, { context });
  await module.link((specifier) => { const names = specifier.includes('api.js') ? ['api'] : specifier.includes('i18n') ? ['t'] : ['actor', 'accountId', 'setActor']; return new vm.SyntheticModule(names, function () { names.forEach((name) => this.setExport(name, bindings[name])); }, { context }); });
  await module.evaluate(); const account = module.namespace.initAccount();
  return { get, calls, account, get actor() { return actor; }, respond(value) { response = value; }, switchActor(value) { bindings.setActor(value); }, end() { actor = null; events.sessionended(); }, async input(id, value) { get(id).value = value; return get(id).listeners.input?.(); }, async click(id) { return get(id).listeners.click(); } };
}

test('account fills immutable username and saves only display_name while retaining role and default key', async () => {
  const h = await setup(); await h.account.load();
  assert.equal(h.get('accountUsername').textContent, 'ordinary'); assert.equal(h.get('accountRole').textContent, 'userRole');
  await h.input('accountDisplayName', 'Renamed'); await h.click('accountSave');
  assert.deepEqual(JSON.parse(h.calls[1].options.body), { display_name: 'Renamed' });
  assert.equal(h.actor.username, 'ordinary'); assert.equal(h.actor.role, 'user'); assert.equal(h.actor.default_token_id, 9);
  assert.equal(h.get('accountSave').disabled, true);
});

test('a late profile save cannot restore an ended session', async () => {
  const h = await setup(); await h.account.load();
  const pending = deferred(); h.respond(pending.promise); await h.input('accountDisplayName', 'Pending'); const saving = h.click('accountSave');
  h.end(); pending.resolve({ id: 7, username: 'ordinary', display_name: 'Pending', role: 'user' }); await saving;
  assert.equal(h.actor, null); assert.equal(h.get('accountUsername').textContent, ''); assert.equal(h.get('accountDisplayName').value, '');
});

test('authenticated API aborts old session requests and rejects late JSON instead of exposing its data', async () => {
  const events = {}, pending = deferred(); let request;
  const context = vm.createContext({ document: { cookie: 'rotor_admin_csrf=test', addEventListener(name, callback) { events[name] = callback; } }, window: { dispatchEvent() {} }, AbortController, DOMException, CustomEvent: class {}, fetch: async (_path, options) => { request = options; return { ok: true, status: 200, json: () => pending.promise }; } });
  const module = new vm.SourceTextModule(apiSource, { context }); await module.link(() => {}); await module.evaluate();
  const loading = module.namespace.api('/api/me', { method: 'PATCH', body: '{}' }); await Promise.resolve();
  assert.equal(request.headers['X-CSRF-Token'], 'test'); events.sessionended(); assert.equal(request.signal.aborted, true);
  pending.resolve({ username: 'previous-account' }); await assert.rejects(loading, (error) => error.name === 'AbortError');
});

test('profile draft survives closing without retaining an inflight request', async () => {
  const h = await setup(); await h.account.load(); await h.input('accountDisplayName', 'Draft');
  h.account.close(); await h.account.load();
  assert.equal(h.get('accountDisplayName').value, 'Draft'); assert.equal(h.get('accountSave').disabled, false);
  assert.equal(h.calls.length, 1);
});

test('same-owner actor updates keep a profile draft, owner changes clear it and ignore a late profile response', async () => {
  const h = await setup(); await h.account.load(); await h.input('accountDisplayName', 'Draft');
  h.switchActor({ ...h.actor, display_name: 'Updated elsewhere' });
  assert.equal(h.get('accountDisplayName').value, 'Draft'); assert.equal(h.get('accountSave').disabled, false);
  const pending = deferred(); h.respond(pending.promise); const saving = h.click('accountSave');
  h.switchActor({ id: 8, username: 'another', display_name: 'Another', role: 'user' });
  assert.equal(h.calls.at(-1).options.signal.aborted, true); assert.equal(h.get('accountDisplayName').value, '');
  pending.resolve({ id: 7, username: 'ordinary', display_name: 'Old result', role: 'user' }); await saving;
  assert.equal(h.actor.id, 8); assert.equal(h.get('accountUsername').textContent, 'another'); assert.equal(h.get('accountDisplayName').value, '');
});

test('closing during a profile lookup aborts it and a late response cannot refill the profile', async () => {
  const h = await setup(); const pending = deferred(); h.respond(pending.promise); const loading = h.account.load();
  h.account.close(); assert.equal(h.calls[0].options.signal.aborted, true);
  pending.resolve({ id: 7, username: 'ordinary', display_name: 'Late private profile', role: 'user' }); await loading;
  assert.equal(h.get('accountDisplayName').value, '');
});
