import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const authSource = await readFile(new URL('../src/rotor/frontend/auth.js', import.meta.url), 'utf8');
const configSource = await readFile(new URL('../src/rotor/frontend/config.js', import.meta.url), 'utf8');

async function environment({ networkFails = false, status = 200, importFails = false, language = 'zh-CN', mustChangePassword = false } = {}) {
  const nodes = new Map(); let activeElement;
  function element(id = '') {
    const classes = new Set(), listeners = new Map();
    const node = {
      id, textContent: '', children: [], disabled: false,
      classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value), contains: (value) => classes.has(value), toggle(value, yes) { yes ? classes.add(value) : classes.delete(value); } },
      appendChild(child) { this.children.push(child); nodes.set(child.id, child); },
      addEventListener(name, callback) { listeners.set(name, callback); },
      click() { return listeners.get('click')?.({ currentTarget: this }); },
      focus() { activeElement = this; },
      querySelector(selector) {
        if (selector.startsWith('#')) return nodes.get(selector.slice(1));
        if (selector === '[data-auth-error]') return this.error;
        if (selector === 'input, button') return this.firstInput || this.firstButton;
        return null;
      },
      querySelectorAll(selector) { return selector === '.auth-card > section' ? this.children : []; },
      set innerHTML(html) {
        this.markup = html;
        for (const match of html.matchAll(/<section id="([^"]+)"[^>]*>([\s\S]*?)<\/section>/g)) {
          const panel = element(match[1]); panel.error = element();
          for (const tag of match[2].matchAll(/<(form|input|button)\b([^>]*)>/g)) {
            const id = tag[2].match(/\bid="([^"]+)"/)?.[1] || '';
            const child = element(id);
            if (tag[1] === 'input' && !panel.firstInput) panel.firstInput = child;
            if (tag[1] === 'button' && !panel.firstButton) panel.firstButton = child;
            if (id) nodes.set(id, child);
          }
          this.appendChild(panel);
        }
      },
    };
    return node;
  }
  const body = element('body'); body.classList.add('auth-pending');
  const document = { cookie: 'rotor_admin_session=existing; rotor_admin_csrf=csrf', body,
    createElement: () => element(), getElementById: (id) => nodes.get(id) || null,
  };
  let reloads = 0, imports = 0, failImport = importFails;
  const window = { addEventListener() {}, location: { reload() { reloads++; } } };
  vm.runInNewContext(configSource, { window });
  const requests = [];
  const context = vm.createContext({ document, window, localStorage: { getItem: () => language },
    fetch: async (path, options) => {
      requests.push({ path, options });
      if (networkFails) throw new TypeError('Failed to fetch');
      return { ok: status === 200, status, json: async () => ({ must_change_password: mustChangePassword }) };
    },
  });
  const module = new vm.SourceTextModule(authSource + '\nexport { appLoaded, startApp };', { context,
    importModuleDynamically: async () => {
      imports++;
      if (failImport) throw new SyntaxError("The requested module './i18n.js' does not provide an export named 'setLocale'");
      const app = new vm.SyntheticModule([], () => {}, { context }); await app.link(() => {}); await app.evaluate(); return app;
    },
  });
  await module.link(() => {}); await module.evaluate();
  return { module, document, requests, get: (id) => nodes.get(id), get imports() { return imports; }, get reloads() { return reloads; }, get activeElement() { return activeElement; }, allowImport() { failImport = false; } };
}

test('module startup failure is distinct from service failure, keeps loaded false, and offers reload without clearing session', async () => {
  for (const language of ['zh-CN', 'en']) {
    const h = await environment({ importFails: true, language });
    assert.equal(h.module.namespace.appLoaded, false); assert.equal(h.imports, 1);
    assert.equal(h.get('adminStartupPanel').classList.contains('hidden'), false);
    assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), true);
    assert.match(h.get('adminStartupPanel').error.textContent, language === 'en' ? /page resources could not load/ : /页面资源未能加载/);
    assert.doesNotMatch(h.get('adminStartupPanel').error.textContent, /Unable to reach|无法连接/);
    assert.equal(h.activeElement, h.get('adminReloadApp'));
    h.get('adminReloadApp').click(); assert.equal(h.reloads, 1);
    assert.equal(h.document.cookie, 'rotor_admin_session=existing; rotor_admin_csrf=csrf');
    h.allowImport(); await h.module.namespace.startApp({ must_change_password: false });
    assert.equal(h.imports, 2); assert.equal(h.module.namespace.appLoaded, true);
    assert.equal(h.get('adminAuthScreen').classList.contains('hidden'), true); assert.equal(h.document.body.classList.contains('auth-pending'), false);
    await h.module.namespace.startApp({ must_change_password: false }); assert.equal(h.imports, 2);
  }
});

test('a real session fetch network failure retains the service-unavailable message and never imports app', async () => {
  const h = await environment({ networkFails: true });
  assert.equal(h.get('adminLoginPanel').classList.contains('hidden'), false);
  assert.equal(h.get('adminLoginPanel').error.textContent, '无法连接 Rotor 管理服务');
  assert.equal(h.imports, 0); assert.equal(h.module.namespace.appLoaded, false);
  assert.equal(h.requests[0].path, '/api/admin/auth/me'); assert.equal(h.requests[0].options.cache, 'no-store');
});

test('unauthenticated and password-change-required sessions retain their existing flows without importing app', async () => {
  const anonymous = await environment({ status: 401 });
  assert.equal(anonymous.get('adminLoginPanel').classList.contains('hidden'), false);
  assert.equal(anonymous.get('adminLoginPanel').error.textContent, ''); assert.equal(anonymous.imports, 0);
  const reset = await environment({ mustChangePassword: true });
  assert.equal(reset.get('adminPasswordPanel').classList.contains('hidden'), false); assert.equal(reset.imports, 0);
});
