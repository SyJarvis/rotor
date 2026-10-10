import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/rotor/frontend/app.js', import.meta.url), 'utf8');

async function start(href) {
  const opened = [], changes = [], requests = [];
  const node = { textContent: '', addEventListener() {}, classList: { toggle() {} } };
  const document = { getElementById(id) { return ['pageTitle', 'channelModal'].includes(id) ? node : null; }, querySelectorAll: () => [], querySelector: () => null, addEventListener() {} };
  const window = { location: { href }, history: { replaceState(_state, _title, url) { changes.push(url); window.location.href = new URL(url, window.location.href).href; } }, lucide: {} };
  const context = vm.createContext({ document, window, URL });
  const page = { load: () => {}, renderHistory: () => {} };
  const bindings = {
    t: (key) => key, applyLocale() {}, initSettings: () => ({ open: (section) => opened.push(section) }), isAdmin: () => false,
    initSidebar() {}, closeSidebar() {}, api: (path) => { requests.push(path); return Promise.resolve({}); }, copyText() {},
    parseCsv() {}, parseJson() {}, refreshIcons() {}, toast() {}, refreshTheme() {}, setChannelPresets() {}, applyChannelDefaults() {}, channelConnection() {},
  };
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const isPage = specifier.includes('/pages/');
    const values = isPage ? { ...page, editState: {}, checkEditedOAuth() {}, startOAuthLogin() {}, changeOAuthCallback() {}, parseOAuthCallback() {}, completeOAuthLogin() {} } : bindings;
    return new vm.SyntheticModule(Object.keys(values), function () { for (const [name, value] of Object.entries(values)) this.setExport(name, value); }, { context });
  });
  await module.evaluate();
  return { opened, changes, requests, window };
}

test('app startup consumes only the security deep link, preserves unrelated query and hash and opens settings once', async () => {
  const h = await start('http://localhost/?settings=security&other=keep&other=again#anchor');
  assert.deepEqual(h.opened, ['security']); assert.deepEqual(h.changes, ['/?other=keep&other=again#anchor']);
  assert.equal(new URL(h.window.location.href).searchParams.has('settings'), false);
  assert.equal(h.requests.some((path) => path.startsWith('/api/admin/')), false);
});

test('ordinary app startup and unknown settings links do not open a settings category or rewrite the URL', async () => {
  for (const href of ['http://localhost/', 'http://localhost/?settings=unknown&other=keep#anchor']) {
    const h = await start(href); assert.deepEqual(h.opened, []); assert.deepEqual(h.changes, []); assert.equal(h.window.location.href, href);
  }
});
