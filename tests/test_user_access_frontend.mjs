import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
const sources = Object.fromEntries(await Promise.all(['tokens', 'users'].map(async (name) => [name, await readFile(new URL(`../src/rotor/frontend/pages/${name}.js`, import.meta.url), 'utf8')])));
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function setup(page, { admin = false, denied = false, models = null } = {}) {
  const nodes = new Map(), events = {}, calls = [], notices = [], copies = [];
  let actor = { id: 7, user_id: 7, username: 'me', role: admin ? 'admin' : 'user', default_token_id: 9 };
  const tokens = [{ id: 9, name: 'base', key: 'masked', enabled: true, is_default: true, allowed_models: [] }, { id: 10, name: 'other', key: 'masked', enabled: true }];
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, { id, value: '', disabled: false, dataset: {}, listeners: {}, elements: {}, classList: { add() {}, remove() {} }, textContent: '',
      set innerHTML(html) { this.html = html; for (const match of html.matchAll(/\bid="([^"]+)"/g)) node(match[1]); }, get innerHTML() { return this.html || ''; },
      addEventListener(name, callback) { this.listeners[name] = callback; }, querySelector() { return node('submit'); }, focus() {},
    }); return nodes.get(id);
  };
  const api = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null; calls.push({ path, options, body });
    if (denied) throw Object.assign(new Error('Forbidden'), { status: 403 });
    if (path === '/api/me') return { ...actor };
    if (path.endsWith('/default')) { actor = { ...actor, default_token_id: Number(path.split('/').at(-2)) }; return actor; }
    if (path.endsWith('/rotate')) return { ...tokens[0], key: 'new-secret' };
    if (path === '/api/me/tokens' || path === '/api/admin/tokens') return tokens;
    if (path === '/api/admin/mcp-control-keys') return [];
    if (path === '/api/admin/users' && options.method === 'POST') return { id: 8, ...body };
    if (path === '/api/admin/users') return [{ ...actor, enabled: true, quota: 0, allowed_channels: [], allowed_models: models }];
    if (path === '/api/admin/invitations' && options.method === 'POST') return { id: 4, token: 'invite-token', expires_at: '2030-01-01T00:00:00Z' };
    if (path === '/api/admin/invitations') return [];
    return {};
  };
  const bindings = { api, isAdmin: () => admin, actor: () => actor, setActor: (value) => { actor = value; }, t: (key) => key === 'columns' ? { name: 'Name', key: 'Key', actions: 'Actions' } : key,
    copyText: async (value) => { copies.push(value); return true; }, escapeHtml: (value) => String(value ?? '').replaceAll('<', '&lt;').replaceAll('"', '&quot;'), refreshIcons() {}, toast: (...args) => notices.push(args), formatTime: String, skeletonRows() {},
  };
  const context = vm.createContext({ document: { getElementById: node, addEventListener(name, callback) { events[name] = callback; } }, window: { prompt: () => 'renamed', confirm: () => true, location: { origin: 'https://rotor.test', pathname: '/' } }, URLSearchParams, Date, FormData: class { constructor(form) { this.form = form; } get(name) { return this.form.elements[name]?.value; } } });
  const module = new vm.SourceTextModule(sources[page], { context });
  await module.link((specifier) => { const names = specifier.includes('api.js') ? ['api', 'copyText'] : specifier.includes('session') ? ['isAdmin', 'actor', 'setActor'] : specifier.includes('i18n') ? ['t'] : ['escapeHtml', 'formatTime', 'skeletonRows', 'refreshIcons', 'toast']; return new vm.SyntheticModule(names, function () { names.forEach((name) => this.setExport(name, bindings[name])); }, { context }); });
  await module.evaluate(); await module.namespace.load();
  return { page: module.namespace, node, calls, notices, copies, get actor() { return actor; },
    target(attribute, value = '') { const key = attribute.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase()); const button = { dataset: { [key]: String(value) }, disabled: false }; return { closest(selector) { return selector === `[${attribute}]` ? button : null; } }; },
    async submit(values) { const form = node('userForm'); form.elements = Object.fromEntries(Object.entries(values).map(([name, value]) => [name, { value, checked: value === true }])); await form.listeners.submit({ preventDefault() {}, currentTarget: form }); },
  };
}

test('ordinary key page requests only personal endpoints and identifies default without exposing control keys', async () => {
  const h = await setup('tokens');
  assert.ok(h.calls.every(({ path }) => path.startsWith('/api/me')));
  assert.match(h.node('tokens').innerHTML, /defaultKey/); assert.doesNotMatch(h.node('tokens').innerHTML, /mcp-control-key-section|tokenScope/);
  await h.page.onClick(h.target('data-token-delete', 9)); assert.doesNotMatch(h.node('tokens').innerHTML, /id="tokenDeleteForm"/);
  await h.page.onClick(h.target('data-token-default', 10)); assert.equal(h.actor.default_token_id, 10);
  assert.ok(h.calls.some(({ path, options }) => path === '/api/me/tokens/10/default' && options.method === 'POST'));
  await h.page.onClick(h.target('data-token-rename', 9)); assert.deepEqual(h.calls.find(({ options }) => options.method === 'PATCH').body, { name: 'renamed' });
  await h.page.onClick(h.target('data-token-toggle', 9));
  assert.deepEqual(h.calls.filter(({ options }) => options.method === 'PATCH').map(({ body }) => body), [{ name: 'renamed' }, { enabled: false }], 'renaming or disabling a key with no model permissions must not rewrite its restrictions');
});

test('admin starts with own default key and may explicitly switch to all keys and control-key management', async () => {
  const h = await setup('tokens', { admin: true });
  assert.ok(h.calls.every(({ path }) => path.startsWith('/api/me')));
  h.node('tokenScope').listeners.change({ target: { value: 'all' } }); await tick();
  assert.ok(h.calls.some(({ path }) => path === '/api/admin/tokens')); assert.ok(h.calls.some(({ path }) => path === '/api/admin/mcp-control-keys'));
  assert.doesNotMatch(h.node('tokens').innerHTML, /defaultKey/); assert.match(h.node('tokens').innerHTML, /mcp-control-key-section/);
});

test('forbidden personal key response shows failure without falling back to admin endpoints', async () => {
  const h = await setup('tokens', { denied: true }); assert.match(h.node('tokens').innerHTML, /Forbidden/);
  assert.ok(h.calls.every(({ path }) => path.startsWith('/api/me')));
});

test('ordinary accounts cannot load user management or trigger administrative mutations', async () => {
  const h = await setup('users'); assert.equal(h.calls.length, 0);
  for (const action of ['data-user-create', 'data-invite-create', 'data-invite-copy']) assert.equal(await h.page.onClick(h.target(action)), false);
  assert.equal(h.calls.length, 0); assert.equal(h.copies.length, 0); assert.equal(h.node('users').innerHTML, '');
});

test('admin invitation carries restrictions and reveals a copyable code without constructing a link', async () => {
  const h = await setup('users', { admin: true }); await h.page.onClick(h.target('data-invite-create'));
  await h.submit({ quota: '0', allowed_channels: '', allowed_models: 'model-a, model-b', expires_in_hours: '24' });
  assert.deepEqual(h.calls.find(({ path, options }) => path === '/api/admin/invitations' && options.method === 'POST').body, { quota: 0, allowed_channels: [], allowed_models: ['model-a', 'model-b'], expires_in_hours: 24 });
  assert.equal(h.node('invitationCode').value, 'invite-token');
  assert.doesNotMatch(h.node('users').innerHTML, /invite-token|\?invite=|invitationLink/);
  await h.page.onClick(h.target('data-invite-copy')); assert.deepEqual(h.copies, ['invite-token']);
  h.page.unload(); await h.page.load();
  assert.doesNotMatch(h.node('users').innerHTML, /id="invitationCode"/);
});

test('user creation validates channel restrictions before sending, then preserves zero quota and explicit role', async () => {
  const h = await setup('users', { admin: true }); await h.page.onClick(h.target('data-user-create'));
  const values = { username: 'new', display_name: 'New', role: 'user', password: 'a-valid-password', quota: '0', allowed_channels: 'not-an-id', allowed_models: '' };
  await h.submit(values); assert.equal(h.calls.filter(({ options }) => options.method === 'POST').length, 0);
  for (const quota of ['1.5', '-1', '9007199254740992']) {
    await h.submit({ ...values, allowed_channels: '*', quota });
    assert.equal(h.node('userFormError').textContent, 'invalidQuota');
    assert.equal(h.calls.filter(({ options }) => options.method === 'POST').length, 0);
  }
  await h.submit({ ...values, allowed_channels: '*' });
  const created = h.calls.find(({ path, options }) => path === '/api/admin/users' && options.method === 'POST');
  assert.equal(created.body.allowed_channels, null); assert.equal(created.body.quota, 0); assert.equal(created.body.role, 'user'); assert.equal(created.body.username, 'new'); assert.deepEqual(created.body.allowed_models, []);
});


test('editing only a display name preserves all, no, and specific model permissions from the rendered form', async () => {
  for (const models of [null, [], ['model-a', 'model-b']]) {
    const h = await setup('users', { admin: true, models });
    await h.page.onClick(h.target('data-user-edit', 7));
    const renderedModels = h.node('users').innerHTML.match(/name="allowed_models" value="([^"]*)"/)[1];
    assert.equal(renderedModels, models === null ? '*' : models.join(','));
    await h.submit({ display_name: 'New display name', role: 'admin', enabled: true, quota: '0', allowed_channels: '', allowed_models: renderedModels });
    const saved = h.calls.find(({ path, options }) => path === '/api/admin/users/7' && options.method === 'PATCH');
    assert.equal(saved.body.display_name, 'New display name');
    assert.deepEqual(saved.body.allowed_models, models);
  }
});
