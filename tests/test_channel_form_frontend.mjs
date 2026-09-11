import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const helperSource = await readFile(new URL('src/rotor/frontend/channel-form.js', root), 'utf8');
const appSource = await readFile(new URL('src/rotor/frontend/app.js', root), 'utf8');
const channelsSource = await readFile(new URL('src/rotor/frontend/pages/channels.js', root), 'utf8');
const backend = JSON.parse(execFileSync(fileURLToPath(new URL('.venv/bin/python', root)), ['-c',
  'import json; from rotor.channels.presets import list_provider_presets, provider_defaults; p=list_provider_presets(); print(json.dumps({"presets":p,"defaults":[[x["id"],s,provider_defaults(x["id"],s)] for x in p for s in ["openai","openai_responses","anthropic","anthropic_messages"]]}))'], { cwd: root, encoding: 'utf8' }));

function element(value = '') {
  const listeners = {};
  return { value, listeners, checked: false, disabled: false, innerHTML: '', textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return true; } },
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelectorAll() { return []; }, setAttribute() {},
  };
}

async function environment() {
  const nodes = new Map();
  const getNode = (id) => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const form = getNode('channelForm');
  form.elements = Object.fromEntries(['name', 'type', 'protocol', 'base_url', 'models_path', 'request_path', 'auth_type', 'priority', 'weight', 'models', 'model_mapping', 'extra', 'enabled', 'key'].map((name) => [name, element()]));
  form.reset = () => {
    for (const field of Object.values(form.elements)) { field.value = ''; field.checked = false; }
    form.elements.type.value = 'openai'; form.elements.protocol.value = 'openai';
  };
  form.reset();
  form.querySelector = () => getNode('saveButton');
  const calls = [];
  const bindings = {
    api: async (url, options = {}) => {
      calls.push([url, options.body ? JSON.parse(options.body) : null]);
      if (url.endsWith('/presets')) return backend.presets;
      if (url.endsWith('/probe-models')) return { models: ['found'], latency_ms: 1 };
      return [];
    },
    t: (key) => key, parseJson: (text, fallback) => text?.trim() ? JSON.parse(text) : fallback,
    parseCsv: (text) => (text || '').split(',').map((item) => item.trim()).filter(Boolean),
    escapeHtml: (text) => String(text),
  };
  const context = vm.createContext({
    document: { getElementById: getNode, querySelectorAll: () => [], addEventListener() {}, },
    window: {}, Element: class {},
    FormData: class { constructor(target) { this.target = target; } get(name) { return name === 'enabled' ? (this.target.elements.enabled.checked ? 'on' : null) : this.target.elements[name]?.value; } },
  });
  const helper = new vm.SourceTextModule(helperSource, { context });
  const channels = new vm.SourceTextModule(channelsSource, { context });
  const synthetic = (names) => new vm.SyntheticModule(names, function () {
    for (const name of names) this.setExport(name, bindings[name] || (() => {}));
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
  return { form, helper: helper.namespace, channels: channels.namespace, calls, getNode };
}

test('frontend defaults match backend provider and protocol combinations', async () => {
  const env = await environment();
  for (const [provider, protocol, expected] of backend.defaults) {
    const actual = env.helper.channelDefaults(provider, protocol);
    for (const name of ['models_path', 'request_path', 'auth_type']) assert.equal(actual[name], expected[name], `${provider}/${protocol}/${name}`);
  }
});

test('editing legacy native/Responses channels fills protocol paths and provider auth', async () => {
  const env = await environment();
  for (const [provider, protocol, path, auth] of [['anthropic', 'anthropic', '/messages', 'x-api-key'], ['zhipu', 'anthropic', '/messages', 'bearer'], ['openai', 'openai_responses', '/responses', 'bearer']]) {
    env.channels.openEdit({ id: 7, name: 'legacy', type: provider, protocol, base_url: 'https://saved.example', extra: {} });
    assert.equal(env.form.elements.request_path.value, path);
    assert.equal(env.form.elements.auth_type.value, auth);
    assert.equal(env.form.elements.key.value, '');
    assert.equal(env.form.elements.key.required, false);
  }
});

test('switching protocol and provider migrates defaults but preserves custom connection values', async () => {
  const env = await environment();
  const fields = env.form.elements;
  env.channels.openEdit({ id: 1, type: 'zhipu', protocol: 'openai', base_url: 'https://private.example', extra: {
    request_path: '/my/messages', models_path: 'https://catalog.example/custom?tenant=x', auth_type: 'api-key', headers: { 'X-Custom': 'preserved' },
  } });
  fields.protocol.value = 'anthropic';
  fields.protocol.listeners.change({ target: fields.protocol });
  assert.equal(fields.request_path.value, '/my/messages');
  assert.equal(fields.auth_type.value, 'api-key');
  fields.type.value = 'anthropic';
  fields.type.listeners.change({ target: fields.type });
  assert.equal(fields.base_url.value, 'https://private.example');
  assert.equal(fields.models_path.value, 'https://catalog.example/custom?tenant=x');
  assert.equal(fields.request_path.value, '/my/messages');
  assert.equal(fields.auth_type.value, 'api-key');
  assert.equal(JSON.parse(fields.extra.value).headers['X-Custom'], 'preserved');
  env.channels.openEdit({ id: 2, type: 'zhipu', protocol: 'openai', base_url: backend.presets.find((p) => p.id === 'zhipu').base_url, extra: {} });
  fields.protocol.value = 'anthropic';
  fields.protocol.listeners.change({ target: fields.protocol });
  assert.equal(fields.request_path.value, '/messages');
  assert.equal(fields.auth_type.value, 'bearer');
  fields.type.value = 'anthropic'; fields.type.listeners.change({ target: fields.type });
  assert.equal(fields.auth_type.value, 'x-api-key');
  assert.equal(fields.base_url.value, 'https://api.anthropic.com/v1');
});

test('saved-key probe sends current draft; replacement-key probe uses unsaved endpoint; save matches draft', async () => {
  const env = await environment();
  const fields = env.form.elements;
  env.channels.openEdit({ id: 3, name: 'edit', type: 'zhipu', protocol: 'anthropic', base_url: 'https://old.example', models: ['old'], extra: {} });
  fields.base_url.value = 'https://draft.example/api/anthropic';
  fields.models_path.value = '/custom/models'; fields.request_path.value = ''; fields.auth_type.value = '';
  fields.extra.value = JSON.stringify({ headers: { 'X-Draft': 'value' } });
  const button = env.getNode('probeModels');
  await button.listeners.click({ currentTarget: button });
  const [savedUrl, savedDraft] = env.calls.at(-1);
  assert.equal(savedUrl, '/api/admin/channels/3/probe-models');
  assert.equal(savedDraft.base_url, 'https://draft.example/api/anthropic');
  assert.equal(savedDraft.extra.request_path, '/messages');
  assert.equal(savedDraft.extra.auth_type, 'bearer');
  assert.equal(savedDraft.extra.headers['X-Draft'], 'value');
  assert.ok(!('key' in savedDraft));
  fields.key.value = 'new-secret';
  await button.listeners.click({ currentTarget: button });
  const [unsavedUrl, unsavedDraft] = env.calls.at(-1);
  assert.equal(unsavedUrl, '/api/admin/channels/probe-models');
  assert.equal(unsavedDraft.key, 'new-secret');
  delete unsavedDraft.key;
  assert.deepEqual(unsavedDraft, savedDraft);
  await env.form.listeners.submit({ currentTarget: env.form, preventDefault() {} });
  const [saveUrl, saveBody] = env.calls.at(-1);
  assert.equal(saveUrl, '/api/admin/channels/3');
  for (const field of ['base_url', 'type', 'protocol', 'extra']) assert.deepEqual(saveBody[field], savedDraft[field]);
});
